"""Manual Chrome CDP smoke test for the saved equivalent evacuation prompt.

Launch a separate Chrome with --headless=new --remote-debugging-port=9223 first.
Uses only the Python standard library so the browser check has no test dependency.
"""
import base64
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time
from urllib.request import urlopen
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
PROMPT = (ROOT / "tests/regression_scenarios/last_evacuation_ship_equivalent.txt").read_text(encoding="utf-8")


class CDP:
    def __init__(self, url):
        parsed = urlparse(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=20)
        self.socket.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.socket.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.socket.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {response[:100]!r}")
        self.pending = response.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def read_exact(self, length):
        while len(self.pending) < length:
            self.pending += self.socket.recv(max(4096, length - len(self.pending)))
        result, self.pending = self.pending[:length], self.pending[length:]
        return result

    def message(self):
        while True:
            first, second = self.read_exact(2)
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_exact(8))[0]
            mask = self.read_exact(4) if second & 128 else None
            data = self.read_exact(length)
            if mask:
                data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
            if first & 15 == 1:
                return json.loads(data)
            if first & 15 == 8:
                raise RuntimeError("Chrome closed CDP connection")

    def call(self, method, params=None):
        self.next_id += 1
        data = json.dumps({"id": self.next_id, "method": method, "params": params or {}}).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(data)) if len(data) < 65536 else (
            bytes([0x81, 0x80 | 127]) + struct.pack("!Q", len(data)))
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        self.socket.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))
        while True:
            response = self.message()
            if response.get("id") == self.next_id:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression,
            "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")


def wait_for(cdp, expression, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(0.5)
    raise TimeoutError(f"browser state not reached: {expression}")


def main():
    tabs = json.load(urlopen("http://127.0.0.1:9223/json", timeout=10))
    tab = next(item for item in tabs if item.get("type") == "page")
    cdp = CDP(tab["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    if "--inspect-only" not in sys.argv:
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:8000/app/"})
        wait_for(cdp, "Boolean(document.querySelector('[data-action=\"open-create\"]'))")
        cdp.evaluate("document.querySelector('[data-action=\"open-create\"]').click()")
        wait_for(cdp, "Boolean(document.querySelector('#world-prompt'))")
        cdp.evaluate("(() => { const el=document.querySelector('#world-prompt'); "
                     f"el.value={json.dumps(PROMPT, ensure_ascii=False)}; "
                     "el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()")
        cdp.evaluate("document.querySelector('#draft-submit').click()")
        deadline = time.monotonic() + 180
        result = None
        while time.monotonic() < deadline:
            result = cdp.evaluate("({url:location.href, config:!!document.querySelector('.config-page'), "
                "modal:!!document.querySelector('#world-prompt'), toast:document.querySelector('#toast')?.textContent||'', "
                "title:document.querySelector('.config-title h1')?.textContent||''})")
            if result["config"] or (result["modal"] and result["toast"].startswith("场景初稿")):
                break
            time.sleep(1)
    else:
        result = cdp.evaluate("({url:location.href, config:!!document.querySelector('.config-page'), "
            "modal:!!document.querySelector('#world-prompt'), toast:document.querySelector('#toast')?.textContent||'', "
            "title:document.querySelector('.config-title h1')?.textContent||''})")
    screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    (ROOT / "test_output/browser-equivalent-draft.png").write_bytes(base64.b64decode(screenshot["data"]))
    if result and result["config"]:
        cdp.evaluate("document.querySelector('[data-section=\"state\"]').click()")
        wait_for(cdp, "Boolean(document.querySelector('.state-row'))")
        details = cdp.evaluate("(() => ({name:document.querySelector('.config-title h1')?.textContent, "
            "rows:[...document.querySelectorAll('.state-row')].map(row=>({"
            "name:row.querySelector('strong')?.textContent, "
            "initial:row.querySelector('input')?.value, "
            "maximum:row.querySelector('span')?.textContent}))}))()")
        (ROOT / "test_output/browser-equivalent-draft.json").write_text(
            json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        result["draft"] = details
    print(json.dumps(result, ensure_ascii=False))
    if not result or not result["config"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
