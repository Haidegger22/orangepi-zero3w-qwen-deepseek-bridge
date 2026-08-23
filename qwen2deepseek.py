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
Правила:
1. Если запрос можно закрыть твоими знаниями (без поиска в интернете) — ответь сам, обычным текстом.
2. Если пользователь явно сказал «спроси у deepseek» (или схожее: «спроси у джипити», «спроси в интернете»,
   «посмотри в Web», «обратись к deepseek») — ты НЕ отвечаешь сам, а отвечаешь ровно одной строкой:
   DEEPSEEK: <вопрос, который надо задать>
   Никакого текста вокруг, только эта строка. <вопрос> — это то, ЧТО именно спросить у DeepSeek,
   очищенное от добавок вроде «спроси у deepseek».
3. Если это просьба об актуальном/свежем/недоступном тебе (новости, текущие цены, сегодняшняя дата,
   «поищи/найди») — тоже отвечай строкой DEEPSEEK: <вопрос>.
Никогда не выдумывай факты как достоверные, если не уверен."""


def ask_qwen(user_text: str) -> str:
    """Отправить реплику в локальную qwen, вернуть текст ответа."""
    body = {
        "model": MODEL,
        "stream": False,
        "options": {"num_predict": 300},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
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


def route(user_text: str) -> str:
    """Спросить qwen и решить: ответить самой или отправить в DeepSeek."""
    reply = ask_qwen(user_text)
    m = re.search(r"DEEPSEEK:\s*(.+)", reply, flags=re.IGNORECASE | re.DOTALL)
    if m:
        question = m.group(1).strip().strip('"').strip()
        print(f"→ qwen маршрутизирует в DeepSeek: «{question}»", file=sys.stderr)
        deep_answer = ask_deepseek(question)
        return f"[из DeepSeek]\n{deep_answer}"
    return reply


def main():
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print(route(q))
        return

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
