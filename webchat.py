#!/usr/bin/env python3
"""webchat.py — отправка вопроса в DeepSeek через CDP (вкладка chat.deepseek.com в Chromium).
Использование: python3 webchat.py "вопрос"
"""
import json, sys, time, urllib.request, websocket

CDP = "http://127.0.0.1:9222"

def get_deepseek_tab():
    pages = json.load(urllib.request.urlopen(CDP + "/json/list", timeout=5))
    for p in pages:
        if p.get('type') == 'page' and 'deepseek' in p.get('url', ''):
            return p
    # нет вкладки — открываем
    u = urllib.parse.quote("https://chat.deepseek.com", safe='')
    req = urllib.request.Request(f"{CDP}/json/new?{u}", method='PUT')
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def main(question):
    tab = get_deepseek_tab()
    ws = websocket.create_connection(tab['webSocketDebuggerUrl'], timeout=30)
    msg_id = 0
    def cmd(method, params=None):
        nonlocal msg_id
        msg_id += 1
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            r = json.loads(ws.recv())
            if r.get('id') == msg_id:
                return r.get('result', {})

    def js(expr):
        return cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True}).get('result', {}).get('value')

    # 1. Фокус на поле ввода
    js("document.querySelector('textarea')?.focus(); true")
    time.sleep(1)
    # 2. Считаем текущее число ответов ассистента
    before = js("document.querySelectorAll('.ds-assistant-message-main-content').length") or 0
    # 3. Вставляем текст реальными событиями
    cmd("Input.insertText", {"text": question})
    time.sleep(0.5)
    # 4. Enter
    cmd("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    # 5. Ждём ответ (до 180 сек)
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(4)
        now = js("document.querySelectorAll('.ds-assistant-message-main-content').length") or 0
        if now > before:
            break
    time.sleep(6)  # дать достримиться
    # 6. Извлекаем последний ответ
    ans = js("""
(() => {
    const nodes = document.querySelectorAll('.ds-assistant-message-main-content');
    if (!nodes.length) return null;
    return nodes[nodes.length - 1].innerText;
})()
""")
    print(ans if ans else "НЕТ ОТВЕТА (проверь вкладку вручную)")
    ws.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: webchat.py 'вопрос'", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
