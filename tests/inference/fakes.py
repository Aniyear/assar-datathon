"""TEST-ONLY: синтетический каталог и поддельный loopback llama-server. Не импортируется из app/."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.contracts import RuleCatalog


def synthetic_rule(type_id="cash_bag_handover", status="verified", rule_id="SYNTH-01"):
    return {"rule_id": rule_id, "type_id": type_id, "type_name_ru": "Синтетический тип (тест)",
            "source_title": "Синтетический источник для тестов", "source_url": "https://example.invalid/synthetic",
            "clause": None, "effective_version_checked_at": "2026-10-17" if status == "verified" else None,
            "scope": "test only", "observable_condition": "synthetic", "required_context": [],
            "counterexamples": [], "claim_template_ru": "Синтетический шаблон", "status": status}


def catalog(*rules):
    return RuleCatalog.model_validate({"schema_version": "0.1", "rules": list(rules)})


class FakeLlama:
    """responder(frame_indices: list[int]) -> (http_status, content: str, finish_reason) или ('sleep', sec)."""

    def __init__(self, responder):
        self.responder, self.calls = responder, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"status": "ok"}) if self.path == "/health" else self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                texts = [c["text"] for c in body["messages"][0]["content"] if c["type"] == "text"]
                idx = [int(m.group(1)) for t in texts for m in [re.match(r"Frame (\d+), t=", t)] if m]
                outer.calls += 1
                r = outer.responder(idx)
                if r[0] == "sleep":
                    time.sleep(r[1])
                    r = (200, json.dumps({"frames_assessable": True, "observations": []}), "stop")
                code, content, finish = r
                self._send(code, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                  "usage": {}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(obs=(), assessable=True):
    return 200, json.dumps({"frames_assessable": assessable, "observations": list(obs)}), "stop"
