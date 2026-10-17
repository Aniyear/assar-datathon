"""Check ONLY this checkout's running synthetic network probe; never print its key."""
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

from scripts.service import ROOT, atomic_json, key_file
from scripts.ngrok_support import valid_origin as ngrok_origin


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A key must not follow a redirect to another origin.


def main() -> int:
    state_dir = ROOT / "outputs/service/network-test"
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "network-test" and state["phase"] == "running"
    assert (re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", state["public_url"])
            or ngrok_origin(state["public_url"]))
    key = key_file(state_dir).read_text(encoding="utf-8").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    receipt = {"mode": "network-test", "run_id": state["run_id"], "public_url": state["public_url"],
               "bank_data_used": False, "inference_tested": False,
               "browser_warning_bypassed_in_automated_check": ngrok_origin(state["public_url"]),
               "independent_network_confirmed": False, "checks": []}
    for origin in (state["url"], state["public_url"]):
        cases = [("page", "/", None, None, 200), ("health", "/health", None, None, 200),
                 ("no_key", "/api/test", None, None, 401), ("wrong_key", "/api/test", "synthetic-wrong", None, 401),
                 ("correct_key", "/api/test", key, None, 200), ("repeat", "/api/test", key, None, 200),
                 ("upload_blocked", "/api/v1/analyze", key, b"synthetic-network-only", 405),
                 ("private_path_blocked", "/outputs/service/network-test/private/api-key.txt", None, None, 404)]
        for name, path, credential, body, expected in cases:
            started = time.monotonic()
            check = {"origin": origin, "name": name, "expected": expected, "pass": False}
            req = urllib.request.Request(origin + path, data=body)
            if ngrok_origin(origin):
                req.add_header("ngrok-skip-browser-warning", "network-test")
            if credential:
                req.add_header("X-API-Key", credential)
            try:
                try:
                    response = opener.open(req, timeout=15)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    check["status"] = response.status
                    raw = response.read(16384)
                check["pass"] = check["status"] == expected
                if name in ("health", "correct_key", "repeat"):
                    result = json.loads(raw)
                    check["pass"] &= result.get("run_id") == state["run_id"] and result.get("mode") == "network-test"
                if name == "page":
                    check["pass"] &= "ТЕСТ СЕТИ".encode() in raw
            except Exception as exc:
                check["error_class"] = type(exc).__name__
            check["elapsed_sec"] = round(time.monotonic() - started, 3)
            receipt["checks"].append(check)
            print(f"{name} {origin}: {'PASS' if check['pass'] else 'FAIL'} ({check.get('status', check.get('error_class'))})")
    receipt["passed"] = all(c["pass"] for c in receipt["checks"])
    atomic_json(state_dir / f"network-receipt-{state['run_id']}.json", receipt)
    atomic_json(state_dir / "network-receipt.json", receipt)
    print(f"Receipt: {state_dir / 'network-receipt.json'}")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
