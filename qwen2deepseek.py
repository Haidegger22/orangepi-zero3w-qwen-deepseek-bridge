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
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:3b"
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
MAX_HISTORY_PAIRS = 4  # сколько последних (user, assistant) пар держать максимум
MAX_HISTORY_CHARS = 2000  # макс суммарный объём истории в символах — компромисс скорость/качество

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
    """Отправить реплику в локальную qwen для ПРЯМОГО ответа (route=self)."""
    messages = [
        {"role": "system", "content": "Ты — полезный ассистент. Отвечай по существу."},
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    body = {
        "model": MODEL,
        "stream": False,
        "options": {"num_predict": 300},
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
        return (data.get("message", {}) or {}).get("content", "").strip()
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
    """Определить маршрут: по явному префиксу (символы) или qwen-роутером."""
    # 1. Жёсткие триггеры по тексту (символы/префиксы) — предсказуемо, без qwen.
    detected = detect_route_by_text(user_text)
    route_decision = detected if detected else ask_qwen_router(user_text, _history if keep_history else None)

    if route_decision == "deepseek":
        # текст запроса — сырая реплика, очищенная от префиксов (НЕ модель пересказывает)
        query = strip_cmd_prefix(user_text) or user_text
        print(f"→ маршрут: DeepSeek; вопрос: «{query}»", file=sys.stderr)
        deep_answer = ask_deepseek(query)
        final = f"[из DeepSeek]\n{deep_answer}"
        if keep_history:
            _history.append({"role": "user", "content": user_text})
            # сохраняем ОБРЕЗАННЫЙ ответ DeepSeek, чтобы не раздувать контекст qwen
            _history.append({"role": "assistant", "content": f"Ответ (из DeepSeek): {_shorten_history_entry(deep_answer)}"})
            _trim_history()
        return final

    # route=self — qwen отвечает сама
    print("→ маршрут: self (qwen)", file=sys.stderr)
    answer = ask_qwen_answer(user_text, _history if keep_history else None)
    if keep_history:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": answer})
        _trim_history()
    return answer


def main():
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        _reset_history()
        print(route(q, keep_history=False))
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
        try:
            print(route(u))
        except Exception as exc:
            print(f"Ошибка: {exc}")
        print(f"[{(time.time()-t0):.1f} c]")


if __name__ == "__main__":
    main()
