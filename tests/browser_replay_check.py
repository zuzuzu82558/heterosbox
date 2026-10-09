"""Manual browser replay check on the completed equivalent evacuation run."""
import json
import os
from pathlib import Path
from urllib.request import urlopen

from browser_e2e_draft import CDP, wait_for, ROOT

OUTPUT_DIR = Path(os.environ.get("NIJIE_E2E_OUTPUT_DIR", ROOT / "test_output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def facts(cdp):
    return cdp.evaluate("Object.fromEntries([...document.querySelectorAll('.world-fact')].map(x=>["
        "x.querySelector('small')?.textContent,x.querySelector('strong')?.textContent]))")


def main():
    tabs = json.load(urlopen("http://127.0.0.1:9223/json", timeout=10))
    cdp = CDP(next(item for item in tabs if item.get("type") == "page")["webSocketDebuggerUrl"])
    cdp.call("Runtime.enable")
    cdp.evaluate("document.querySelector('.results-bottom [data-action=\"live\"]').click()")
    wait_for(cdp, "Boolean(document.querySelector('.live-page'))")
    latest = facts(cdp)
    cdp.evaluate("(() => { const el=document.querySelector('#timeline'); el.value='1'; "
                 "el.dispatchEvent(new Event('change',{bubbles:true})); return true; })()")
    wait_for(cdp, "document.querySelector('.status-pill')?.textContent==='历史回放'")
    earlier = facts(cdp)
    result = {"latest_time": latest.get("剩余港口时间"),
              "earlier_time": earlier.get("剩余港口时间"),
              "latest_boarded": latest.get("已登船人数"),
              "earlier_boarded": earlier.get("已登船人数")}
    (OUTPUT_DIR / "browser-replay-check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    assert int(result["earlier_time"]) > int(result["latest_time"])
    assert int(result["earlier_boarded"]) < int(result["latest_boarded"])


if __name__ == "__main__":
    main()
