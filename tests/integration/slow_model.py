"""TEST ONLY: slow loopback model protocol; no weights, GPU or scene logging."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    port, record = int(sys.argv[1]), Path(sys.argv[2])
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, value):
            data = json.dumps(value).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ConnectionError, OSError):
                pass

        def do_GET(self):
            self.send({"status": "ok"})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # Only protocol metadata leaves this fake; discard all request content.
            record.write_text(json.dumps({"path": self.path, "stream": payload["stream"],
                                          "schema": payload["response_format"]["type"]}), encoding="utf-8")
            time.sleep(delay)
            self.send({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"frames_assessable": True, "observations": []})}}]})

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
