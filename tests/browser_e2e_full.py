"""Manual browser run of the saved equivalent evacuation scenario to a result page."""
import base64
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen

from browser_e2e_draft import CDP, PROMPT, ROOT, wait_for

OUTPUT_DIR = Path(os.environ.get("NIJIE_E2E_OUTPUT_DIR", ROOT / "test_output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def capture(cdp, name):
    data = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})["data"]
    (OUTPUT_DIR / f"{name}.png").write_bytes(base64.b64decode(data))


def main():
    tabs = json.load(urlopen("http://127.0.0.1:9223/json", timeout=10))
    cdp = CDP(next(item for item in tabs if item.get("type") == "page")["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": "http://127.0.0.1:8000/app/"})
    wait_for(cdp, "Boolean(document.querySelector('[data-action=\"open-create\"]'))")
    cdp.evaluate("document.querySelector('[data-action=\"open-create\"]').click()")
    wait_for(cdp, "Boolean(document.querySelector('#world-prompt'))")
    cdp.evaluate("(() => { const el=document.querySelector('#world-prompt'); "
                 f"el.value={json.dumps(PROMPT, ensure_ascii=False)}; "
                 "el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()")
    cdp.evaluate("document.querySelector('#draft-submit').click()")
    draft = wait_for(cdp, "document.querySelector('.config-page') ? 'ready' : "
        "(document.querySelector('#toast')?.textContent?.startsWith('场景初稿生成失败') ? 'failed' : '')", 240)
    if draft != "ready":
        capture(cdp, "browser-full-draft-failed")
        raise RuntimeError("browser draft generation failed")
    cdp.evaluate("document.querySelector('[data-section=\"advanced\"]').click()")
    wait_for(cdp, "Boolean(document.querySelector('#advanced-json'))")
    scene = cdp.evaluate("JSON.parse(document.querySelector('#advanced-json').value)")
    (OUTPUT_DIR / "browser-full-scene.json").write_text(
        json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
    time_fields = [field for field in scene["world_state_schema"]["fields"]
                   if "时间" in field["description"] and field["type"] == "int"]
    if not time_fields or not any(scene["initial_world_state"][f["path"]] == f["max"] for f in time_fields):
        raise AssertionError("generated browser draft did not retain a boundary initial time metric")
    print(json.dumps({"stage": "draft", "name": scene["scenario_name"],
        "agents": len(scene["agents"]), "actions": len(scene["action_specs"]),
        "max_rounds": scene["termination_policy"].get("max_rounds")}, ensure_ascii=False), flush=True)
    capture(cdp, "browser-full-draft")
    cdp.evaluate("document.querySelector('[data-action=\"start\"]').click()")
    started = wait_for(cdp, "document.querySelector('.live-page') ? 'live' : "
        "(document.querySelector('#toast')?.textContent?.includes('失败') ? 'failed' : '')", 60)
    if started != "live":
        capture(cdp, "browser-full-start-failed")
        raise RuntimeError("browser launch failed")
    capture(cdp, "browser-full-live")
    last_status = ""
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status = cdp.evaluate("({status:document.querySelector('.status-pill')?.textContent||'', "
            "event:document.querySelector('.live-toolbar')?.textContent?.match(/全局事件\\s*#(\\d+)/)?.[1]||'', "
            "finished:!!document.querySelector('.finish-banner')})")
        if status["status"] != last_status or int(status["event"] or 0) % 25 == 0:
            print(json.dumps({"stage": "running", **status}, ensure_ascii=False), flush=True)
            last_status = status["status"]
        if status["finished"]:
            break
        time.sleep(5)
    else:
        capture(cdp, "browser-full-timeout")
        raise TimeoutError("simulation did not reach result within 15 minutes")
    cdp.evaluate("document.querySelector('.finish-banner [data-action=\"results\"]').click()")
    wait_for(cdp, "Boolean(document.querySelector('.results-page'))")
    result = cdp.evaluate("({heading:document.querySelector('.results-hero h1')?.textContent, "
        "hero:document.querySelector('.results-hero-copy')?.textContent, "
        "checks:[...document.querySelectorAll('.goal-check')].map(x=>x.textContent), "
        "ranking:!!document.querySelector('.ranking-list')})")
    capture(cdp, "browser-full-result")
    (OUTPUT_DIR / "browser-full-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": "result", **result}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
