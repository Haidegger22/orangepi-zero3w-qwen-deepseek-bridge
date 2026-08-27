#!/usr/bin/env python3
"""qwen2deepseek.py — мост: локальная qwen (Ollama) маршрутизирует в DeepSeek.

Архитектура (по рекомендации Джарвиса, v0.5):
  - qwen = БИНАРНЫЙ РОУТЕР. Отвечает только {"route":"self"} или {"route":"deepseek"},
    temperature=0, format=json. НЕ формулирует и НЕ пересказывает вопрос.
  - Текст запроса в DeepSeek = СЫРАЯ реплика пользователя, очищенная регэкспом
    от командных префиксов ("спроси у deepseek" и т.п.) — не моделью.
  - Так исключается искажение контекста ("завтра"→"сегодня") и пересказ от третьего лица.

Запуск:
  python3 qwen2deepseek.py            # интерактивный цикл
  python3 qwen2deepseek.py "вопрос"   # разовый запрос
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:1.5b"  # быстрая модель для Zero 3W (скорость выше, чем 3b)
WEBCAT = os.environ.get("WEBCAT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webchat.py"))
MODEL = os.environ.get("QWEN_MODEL", DEFAULT_MODEL)

# qwen = чистый роутер: только JSON-флаг, БЕЗ пересказа вопроса
ROUTER_PROMPT = """Ты — маршрутизатор. Реши, кто ответит пользователю.
Ответь СТРОГО одним JSON-объектом, без пояснений и без пересказа вопроса:
{"route":"self"} или {"route":"deepseek"}
"deepseek" — ТОЛЬКО если информация СВЕЖАЯ/АКТУАЛЬНАЯ (новости, даты, курсы, погода,
забронировать, свежие события) ЛИБО пользователь явно просит «спроси у deepseek»,
ЛИБО вопрос явно выходит за пределы твоих знаний.
"self" — простые известные факты и обычные вопросы (математика, простые определения,
названия, объяснения), а также приветствие, команда, личный диалог.
ВСЕГДА предпочитай "self", если можешь ответить сама базовыми знаниями.
Не отправляй в deepseek то, с чем справишься сама.
Выведи ТОЛЬКО JSON. Ничего больше."""

# Промпт для обычного self-ответа (без RAG-контекста)
SYSTEM_ANSWER_PROMPT = "Ты — полезный ассистент. Отвечай по существу."

# Промпт для self-ответа С RAG-контекстом из локальной Википедии
# ВАЖНО: контекст — ДОПОЛНЕНИЕ к знаниям qwen, а не замена. qwen комбинирует оба источника.
RAG_SYSTEM_PROMPT = """Ты — полезный ассистент, сочетающий собственные знания с дополнительным
контекстом из Википедии. 
- Используй СВОИ знания и опыт — отвечай полно и естественно.
- Если дан контекст из Википедии — используй его как уточнение/подтверждение (особенно для цифр,
дат, названий). Можешь дополнять им свой ответ.
- Если контекст противоречит твоим знаниям — опирайся на наиболее достоверное.
- Не говори «в базе этого нет», если знаешь ответ сама.
- Отвечай по-русски, по существу. Где уместно — ссылайся на источник как [источник: <название статьи>]."""

# Регэксп-префиксы для очистки сырой реплики пользователя перед отправкой в DeepSeek.
# Важно: НЕ модель формулирует вопрос — только эти шаблоны вырезают вводные слова.
CMD_PREFIX_PATTERNS = [
    r"^\s*спроси\s+у\s+deepseek[\s,::]*",
    r"^\s*спроси\s+deepseek[\s,::]*",
    r"^\s*спроси\s+у\s+джарвиса[\s,::]*",
    r"^\s*deepseek[\s,::]*",
    r"^\s*передай\s+джарвису[\s,::]*",
    r"^\s*посмотри\s+в\s+интернет[е]?[\s,::]*",
    r"^\s*найди[\s,::]*",
    r"^\s*(спроси|спрашивай)\s+в\s+deepseek[\s,::]*",
]


# Явные триггеры-префиксы: если реплика НАЧИНАЕТСЯ с этих слов/символов — сразу DeepSeek,
# без опроса qwen-роутера. Это жёсткая, предсказуемая маршрутизация по символам.
DEEPSEEK_TRIGGERS = [
    r"спроси\s+у\s+deepseek",
    r"спроси\s+в\s+deepseek",
    r"спроси\s+deepseek",
    r"^\s*deepseek[\s:,-]*",
    r"^\s*dipsyk[\s:,-]*",
    r"^\s*дипсик[\s:,-]*",
    r"спроси\s+у\s+джарвиса",
    r"посмотри\s+в\s+интернет[е]?",
    r"^\s*найди\s+в\s+(интернет|deepseek)",
]

# Явные триггеры для self: если реплика начинается с них — сразу qwen отвечает сама.
SELF_TRIGGERS = [
    r"отве[тт]ь\s+(сам|сама)",
    r"^\s*ты\s+(ответь|скажи)\s+сам",
    r"не\s+ходи\s+в\s+deepseek",
]


def detect_route_by_text(user_text: str):
    """Определить маршрут по буквальному префиксу/ключевым словам. 
    Возвращает 'deepseek', 'self' или None (если явного маркера нет → решает qwen-роутер)."""
    t = user_text.lower().strip()
    for pat in DEEPSEEK_TRIGGERS:
        if re.search(pat, t):
            return "deepseek"
    for pat in SELF_TRIGGERS:
        if re.search(pat, t):
            return "self"
    return None


def strip_cmd_prefix(raw: str) -> str:
    """Очистить реплику пользователя от вводных префиксов (регэксп, НЕ модель)."""
    t = raw.strip()
    for pat in CMD_PREFIX_PATTERNS:
        t = re.sub(pat, "", t, count=1, flags=re.IGNORECASE).strip()
    return t


def _history_context() -> list:
    """Вернуть историю последних пар. Обрезаем до разумного размера."""
    return list(_history)


_history: list = []
_last_was_self_stream = False  # True, если последний ответ qwen уже отстримлен (чтобы не дублировать)
MAX_HISTORY_PAIRS = 4  # сколько последних (user, assistant) пар держать максимум
MAX_HISTORY_CHARS = 1000  # макс суммарный объём истории в символах — баланс памяти и скорости

# Ограничить размер фрагмента ответа DeepSeek, попадающего в историю qwen.
# Полный длинный ответ DeepSeek раздувает контекст маленькой qwen → она медленно
# генерирует на следующем простом вопросе (замер: «спасибо» после новостей — 119 с).
DEEPSEEK_HISTORY_PREVIEW = 200  # символов ответа DeepSeek сохранять в историю


def _shorten_history_entry(text: str, limit: int = DEEPSEEK_HISTORY_PREVIEW) -> str:
    """Обрезать длинный текст до limit символов + многоточие, чтобы не раздувать контекст."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _trim_history() -> None:
    """Урезать историю: по числу пар И по суммарному объёму символов."""
    # 1) по количеству пар
    if len(_history) > MAX_HISTORY_PAIRS * 2:
        del _history[:len(_history) - MAX_HISTORY_PAIRS * 2]
    # 2) по объёму — пока суммарный вес больше лимита, срезаем самые старые пары
    while len(_history) >= 2:
        total = sum(len(m.get("content", "")) for m in _history)
        if total <= MAX_HISTORY_CHARS:
            break
        del _history[:2]


def _reset_history() -> None:
    _history.clear()


def ask_qwen_router(user_text: str, history=None) -> str:
    """Спросить qwen-роутера. Возвращает строку JSON вида {"route":"self"} или {"route":"deepseek"}.
    При ошибке/не-парсинге возвращает {"route":"self"} (безопасный дефолт)."""
    messages = [{"role": "system", "content": ROUTER_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    body = {
        "model": MODEL,
        "stream": False,
        "format": "json",          # строгий JSON-вывод
        "options": {"num_predict": 60, "temperature": 0},
        "messages": messages,
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.load(resp)
        reply = (data.get("message", {}) or {}).get("content", "").strip()
        # извлекаем только поле "route" (не доверяем всему ответу)
        m = re.search(r'"route"\s*:\s*"([a-zA-Z]+)"', reply)
        if m:
            return m.group(1).strip().lower()
        return "self"  # нет route — безопасный дефолт
    except Exception as exc:
        # не падаем — дефолт self, и залогируем
        return "self"


def ask_qwen_answer(user_text: str, history=None) -> str:
    """Отправить реплику в локальную qwen для ПРЯМОГО ответа (route=self).
    stream=True — генерация идёт по токенам, печатается в терминал как в `ollama run`.
    Возвращает полный текст (для сохранения в историю)."""
    messages = [
        {"role": "system", "content": SYSTEM_ANSWER_PROMPT},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    body = {
        "model": MODEL,
        "stream": True,          # ← стримим ответ
        "options": {"num_predict": 300},
        "messages": messages,
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    full = ""
    try:
        # Стрим Ollama = NDJSON: каждая строка — это JSON без префикса "data:".
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # если где-то есть префикс "data:" (другие клиенты), срежем жа
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("[DONE]", ""):
                    break
                try:
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    piece = msg.get("content", "")
                    if piece:
                        full += piece
                        print(piece, end="", flush=True)  # живой стримминг
                    if msg.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
        print()  # перевод строки после ответа
        global _last_was_self_stream
        if full.strip():
            _last_was_self_stream = True  # ответ уже напечатан стриммингом
        return full.strip()
    except Exception as exc:
        return f"ОШИБКА запроса к qwen: {exc}"


# ── Локальный RAG (Википедия) ────────────────────────────────────────────
RAG_DB = "/home/orangepi/ragwiki/rag.db"
RAG_TOP = 3          # сколько фрагментов подтягивать
RAG_PREVIEW = 400    # длина фрагмента


def _rag_terms(query):
    """Извлечь значимые слова из запроса для FTS5-поиска (кириллица+латиница)."""
    STOP = {"какие","какой","какая","какое","каких","в","на","по","из","о","с","у","для","что",
            "как","сколько","это","не","и","или","дай","расскажи","да","есть","где","кто",
            "такое","про","об","все","через","до","при","между","почему","зачем","чем","како",
            # приветствия и общие фразы — НЕ искать в базе
            "привет","приветствие","здравствуй","здравствуйте","добрый","пока","спасибо","благодарю",
            "помоги","помочь","можно","хочу","нужен","нужна","нужно","давай","могу","сделай",
            "подскажи","ответ","вопрос","обычно","вообще","её","ему","вот","так","все","где",
            "дела","дело","нового","новое","новости","добрый","вечер","день","час","время",
            "сегодня","завтра","вчера","неделя","недели","уметь","умеешь","мочь","может","можешь",
            "себе","работаешь","работа","расскажи","собой","сама","сам"}
    words = re.findall(r"[а-яёa-z]{3,}", query.lower())
    out = []
    for w in words:
        if w in STOP:
            continue
        # грубый стемминг окончаний
        for suf in ("ового","ового","овых","овый","овая","овое","ный","ная","ное","ции","ию",
                    "ия","ях","ами","ев","ов","ах","ам","ом","ем","ье","ов"):
            if w.endswith(suf) and len(w)-len(suf) >= 3:
                w = w[:-len(suf)]
                break
        if w not in out:
            out.append(w)
    return out


def _rag_search(query, top=RAG_TOP):
    """Поиск фрагментов в локальной базе Википедии. Возвращает только релевантные.
    Если в запросе НЕТ значимых тем (только приветствие/служ.слова) — вернёт [].
    Фильтр: реальное ключевое слово должно встречаться в теле статьи."""
    if not os.path.exists(RAG_DB):
        return []
    terms = _rag_terms(query)
    if not terms:
        return []   # нет значимых терминов (приветствие и т.п.) — RAG не ищем
    try:
        conn = sqlite3.connect(RAG_DB)
        match_q = " OR ".join(f'"{t}"' for t in terms)
        rows = conn.execute(
            "SELECT title, body FROM articles_fts WHERE articles_fts MATCH ? "
            "ORDER BY bm25(articles_fts) LIMIT ?", (match_q, top)
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    # фильтр: статья должна содержать реальное значащее слово запроса
    res = []
    for title, body in rows:
        bl = body.lower()
        if any(t in bl for t in terms):
            frag = _rag_fragment(body, terms, RAG_PREVIEW)
            res.append((title, frag))
            if len(res) >= top:
                break
    return res


def _rag_fragment(body, terms, length):
    """Вырезать фрагмент вокруг первого вхождения значимого слова."""
    bl = body.lower()
    pos = -1
    for t in terms:
        idx = bl.find(t)
        if idx != -1 and (pos == -1 or idx < pos):
            pos = idx
    if pos == -1:
        return body[:length]
    start = max(0, pos - length // 4)
    return body[start:start + length]


def ask_qwen_answer_rag(user_text: str, history=None) -> str:
    """qwen отвечает САМА, но с RAG-контекстом из локальной Википедии (стриминг).
    Если фрагменты найдены — подкладываем в промпт. Возвращает полный текст."""
    results = _rag_search(user_text)
    # строим системный/промпт
    if results:
        ctx = "\n".join(f"[Фрагмент {i+1}] (источник: {t})\n{f}\n" for i, (t, f) in enumerate(results))
        system = RAG_SYSTEM_PROMPT
        user_msg = f"Контекст из Википедии:\n{ctx}\n\nВопрос: {user_text}\n\nДай ответ, опираясь на контекст."
        print(f"→ RAG: найдено {len(results)} фраг.", file=sys.stderr)
    else:
        system = SYSTEM_ANSWER_PROMPT
        user_msg = user_text
        print("→ RAG: база пуста/не найдено — обычный ответ.", file=sys.stderr)
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_msg})
    body = {
        "model": MODEL,
        "stream": True,
        "options": {"num_predict": 400},
        "messages": messages,
    }
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    full = ""
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("[DONE]", ""):
                    break
                try:
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    piece = msg.get("content", "")
                    if piece:
                        full += piece
                        print(piece, end="", flush=True)
                    if msg.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
        print()
        global _last_was_self_stream
        if full.strip():
            _last_was_self_stream = True
        return full.strip()
    except Exception as exc:
        return f"ОШИБКА запроса к qwen: {exc}"


def ask_deepseek(question: str) -> str:
    """Запустить webchat.py с вопросом, вернуть ответ DeepSeek."""
    try:
        proc = subprocess.run(
            [sys.executable, WEBCAT, question],
            capture_output=True, text=True, timeout=240,
        )
        out = (proc.stdout or "").strip()
        if not out:
            return "DeepSeek не дал ответа (проверь вкладку). ERR: " + (proc.stderr or "").strip()
        return out
    except subprocess.TimeoutExpired:
        return "DeepSeek не ответил за 240 c (таймаут)."
    except FileNotFoundError as exc:
        return f"Не найден скрипт {WEBCAT} ({exc})"


def route(user_text: str, keep_history: bool = True) -> str:
    """Маршрутизация: если запрос начинается с 'deepseek'/«спроси у deepseek» → DeepSeek,
    иначе — self (qwen отвечает сама, с RAG-контекстом из локальной Википедии)."""
    # 1. Явный префикс deepseek → только тогда идём в DeepSeek
    detected = detect_route_by_text(user_text)
    go_deepseek = detected == "deepseek"

    if go_deepseek:
        query = strip_cmd_prefix(user_text) or user_text
        print(f"→ маршрут: DeepSeek; вопрос: «{query}»", file=sys.stderr)
        deep_answer = ask_deepseek(query)
        final = f"[из DeepSeek]\n{deep_answer}"
        if keep_history:
            _history.append({"role": "user", "content": user_text})
            _history.append({"role": "assistant", "content": f"Ответ (из DeepSeek): {_shorten_history_entry(deep_answer)}"})
            _trim_history()
        _last_was_self_stream = False
        return final

    # 2. self + RAG — qwen отвечает сама, подтягивая фрагменты из локальной Википедии
    print("→ маршрут: self (qwen + RAG)", file=sys.stderr)
    answer = ask_qwen_answer_rag(user_text, _history if keep_history else None)
    if keep_history:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": answer})
        _trim_history()
    return answer


def main():
    global _last_was_self_stream
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        _reset_history()
        result = route(q, keep_history=False)
        # в разовом режиме для self ответ уже отстримлен в ask_qwen_answer —
        # печатаем только если маршрут не self (deepseek или ошибка)
        if not _last_was_self_stream:
            print(result)
        return

    _reset_history()
    print("Мост qwen ↔ DeepSeek. Пиши запрос, «exit»/«quit» для выхода.")
    print("Пример: «спроси у deepseek последние новости»\n")
    while True:
        try:
            u = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not u:
            continue
        if u.lower() in ("exit", "quit", "выход"):
            break
        t0 = time.time()
        _last_was_self_stream = False
        try:
            result = route(u)
            # self уже отстримлен → дубль не печатаем; deepseek → печатаем
            if not _last_was_self_stream:
                print(result)
        except Exception as exc:
            print(f"Ошибка: {exc}")
        print(f"[{(time.time()-t0):.1f} c]")


if __name__ == "__main__":
    main()
