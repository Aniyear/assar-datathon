"""TEST-ONLY: run the real ASGI app under uvicorn in a thread and talk HTTP via urllib."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_mov(path: Path, seconds: int = 3) -> Path:
    """Synthetic test pattern (no dataset). Requires ffmpeg in PATH."""
    for codec in (["-c:v", "libx264", "-pix_fmt", "yuv420p"], ["-c:v", "mpeg4"]):
        result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                 f"testsrc=duration={seconds}:size=160x120:rate=10", *codec,
                                 "-f", "mov", str(path)], capture_output=True, check=False)
        if result.returncode == 0:
            return path
    raise RuntimeError("ffmpeg could not create a synthetic MOV")


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def fake_server_command(pid_file: Path):
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from tests.api.fakes import server_main; server_main()")
    return lambda port: [sys.executable, "-c", code, "server", str(port), str(pid_file)]


class LiveServer:
    def __init__(self, app) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.base = ""

    def __enter__(self) -> LiveServer:
        self.thread.start()
        limit = time.monotonic() + 60
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > limit:
                raise RuntimeError("server failed to start")
            time.sleep(0.05)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.server.should_exit = True
        self.thread.join(timeout=60)

    def request(self, method: str, path: str, key: str | None = None, body: bytes | None = None,
                content_type: str | None = None) -> tuple[int, dict | str]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if key is not None:
            req.add_header("X-API-Key", key)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with _OPENER.open(req, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode("utf-8", "replace")

    def upload(self, key: str | None, data: bytes, filename: str = "clip.mov",
               field: str = "file") -> tuple[int, dict | str]:
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: video/quicktime\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", "/api/v1/analyze", key, body, f"multipart/form-data; boundary={boundary}")

    def wait_terminal(self, key: str, job_id: str, timeout: float = 60) -> dict:
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            status, body = self.request("GET", f"/api/v1/jobs/{job_id}", key)
            assert status == 200, body
            if body["status"] != "processing":
                return body
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    def wait_accepting(self, key: str, timeout: float = 60) -> None:
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if self.request("GET", "/api/internal/status", key)[1].get("accepting"):
                return
            time.sleep(0.1)
        raise AssertionError("service did not reopen admission")
