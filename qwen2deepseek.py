#!/usr/bin/env python3
"""qwen2deepseek.py — мост: локальная qwen (Ollama) сама решает, когда спросить DeepSeek.

Цикл:
  юзер → qwen (локальная) → либо прямой ответ, либо маркер "DEEPSEEK: <вопрос>"
  если маркер → скрипт отправляет <вопрос> в веб DeepSeek (webchat.py) и возвращает ответ.

Запуск:
  python3 qwen2deepseek.py            # интерактивный цикл
  python3 qwen2deepseek.py "вопрос"   # разовый запрос

Зависимости: requests, websocket-client (уже есть в ~/webchat.py окружении).
"""
import json
import os
import re
import sys
import subprocess
import time
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:3b"
WEBCAT = os.environ.get("WEBCAT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webchat.py"))
MODEL = os.environ.get("QWEN_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = """Ты — автономный посредник между пользователем и внешней нейросетью DeepSeek.
Правила СТРОГИЕ:
1. Если запрос можно закрыть твоими знаниями (без поиска в интернете) — ответь сам, обычным текстом.
2. Если нужно спросить DeepSeek (пользователь сказал «спроси у deepseek», или запрос про
   актуальное/свежее/точные цифры), ты ОБЯЗАН ответить ровно одной строкой, начинающейся с токена:
   DEEPSEEK: <вопрос, который надо задать>
   Никакого текста вокруг токена DEEPSEEK. Это очень важно: строка должна НАЧИНАТЬСЯ строго с "DEEPSEEK:".
   Токен пиши ЗАГЛАВНЫМИ латинскими буквами, ничего не добавляя до него.
3. <вопрос> — уточни то, что именно спросить, расписав полный смысл, особенно если реплика короткая
   (например «а дыня?» → «какого цвета дыня?»). Опирайся на историю диалога, чтобы понять контекст.
Никогда НЕ придумывай ссылку/токен вида ARBER, DEEPS, DS: и т.п. — только ровно DEEPSEEK:
Никогда не выдумывай факты как достоверные, если не уверен."""


# История диалога (готовится в route, подаётся в ask_qwen).
# Нужна, чтобы qwen понимала короткие вопросы в контексте
# (например «а дыня?» после «какого цвета арбуз?»).
_history: list = []
MAX_HISTORY_PAIRS = 4  # сколько последних (user, assistant) пар держать максимум


def _looks_unsure(text: str) -> bool:
    """Признаки, что qwen не знает ответа сама и стоит спросить DeepSeek."""
    t = text.lower()
    unsure = [
        "не знаю", "не могу", "не уверен", "не уверена", "уточните", "уточнить",
        "недостаточно", "нет информации", "не имею", "затрудняюсь",
        "я не могу дать", "нет доступа", "недоступн", "попробуйте", "не понимаю",
        "пожалуйста, уточните", "не хватает", "не в курсе", "не подскажу",
    ]
    return any(u in t for u in unsure)


def _extract_marker(reply: str):
    """Вытащить текст ИЗ маркера вида 'СЛОВО: вопрос' (толерaнтно к регистру и мусорным токенам).

    Ловит DEEPSEEK, ARBUZ, ARBER, DS, И т.п. — любой одиночный токен, за которым ': '.
    Возвращает вопрос, либо None если похожи на маркер нет.
    """
    m = re.search(r"^\s*[A-Za-zА-Яа-яЁё_ -]{2,25}:\s*(.+)$", reply.strip(), flags=re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1).strip().strip('"').strip()
    return None


def _trim_history() -> None:
    """Ограничить историю последними MAX_HISTORY_PAIRS парами user/assistant."""
    if len(_history) > MAX_HISTORY_PAIRS * 2:
        del _history[:len(_history) - MAX_HISTORY_PAIRS * 2]


def _reset_history() -> None:
    _history.clear()


def ask_qwen(user_text: str, history=None) -> str:
    """Отправить реплику в локальную qwen, вернуть текст ответа."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # подкладываем накопленную историю, если есть
    if history is None:
        history = _history
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
    """Спросить qwen и решить: ответить самой или отправить в DeepSeek."""
    reply = ask_qwen(user_text, _history if keep_history else None)
    q = _extract_marker(reply)
    if not q and _looks_unsure(reply):
        # qwen прямо говорит, что не знает → используем её реплику как запрос к DeepSeek
        q = reply
    if q:
        print(f"→ qwen маршрутизирует в DeepSeek: «{q}»", file=sys.stderr)
        deep_answer = ask_deepseek(q)
        final = f"[из DeepSeek]\n{deep_answer}"
        if keep_history:
            _history.append({"role": "user", "content": user_text})
            _history.append({"role": "assistant", "content": f"Ответ (из DeepSeek): {deep_answer}"})
            _trim_history()
        return final
    if keep_history:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": reply})
        _trim_history()
    return reply


def main():
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        _reset_history()  # разовый вызов — без накопленного контекста
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
            print(route(u))  # keep_history=True по умолчанию — контекст диалога
        except Exception as exc:
            print(f"Ошибка: {exc}")
        print(f"[{(time.time()-t0):.1f} c]")


if __name__ == "__main__":
    main()
