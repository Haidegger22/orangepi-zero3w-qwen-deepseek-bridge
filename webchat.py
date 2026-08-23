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
    # 2. Снимок текущего состояния. ВАЖНО: считаем только НАСТОЯЩИЕ ответы,
    #    исключая blocks-рассуждения DeepSeek (класс ds-think-content),
    #    которые рендерятся в тот же контейнер и могут оказаться после ответа.
    #    Ниже "real answers" = .ds-assistant-message-main-content без предка ds-think.
    def snapshot():
        s = js("""
(() => {
    const nodes = Array.from(document.querySelectorAll('.ds-assistant-message-main-content'))
                      .filter(el => !el.closest('[class*="ds-think"]'));
    if (!nodes.length) return {count: 0, text: ''};
    const last = nodes[nodes.length-1];
    return {count: nodes.length, text: (last.textContent || '').trim()};
})()
""") or {}
        return s.get('count', 0), s.get('text', '') or ''
    before_count, before_text = snapshot()
    # 3. Вставляем текст реальными событиями
    cmd("Input.insertText", {"text": question})
    time.sleep(0.5)
    # 4. Enter
    cmd("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                    "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    # 5. Ждём завершения ответа. Новый ответ появился, когда:
    #    - увеличилось ЧИСЛО настоящих ответов (новый блок), либo
    #    - изменился текст последнего настоящего ответа (стриминг/дописывание).
    #    "Ответ готов" = текст последнего настоящего стабилен ~4 сек.
    deadline = time.time() + 180
    saw_new = False
    stable_since = None
    last_text = before_text
    while time.time() < deadline:
        time.sleep(2)
        cur_count, cur_text = snapshot()
        changed = (cur_count != before_count) or (cur_text != before_text and (before_text == '' or not cur_text.startswith(before_text)))
        if changed and not saw_new:
            saw_new = True
            last_text = cur_text
            stable_since = None
        elif saw_new:
            if cur_text != last_text:
                last_text = cur_text
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= 4:
                    break
    # 6. Извлекаем последний НАСТОЯЩИЙ ответ (без рассуждений ds-think)
    ans = js("""
(() => {
    const nodes = Array.from(document.querySelectorAll('.ds-assistant-message-main-content'))
                      .filter(el => !el.closest('[class*="ds-think"]'));
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
