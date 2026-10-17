"""TEST-ONLY synthetic analyzer and fake model server. Never imported by app/."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PIL import Image

from app.contracts import (AnalysisResult, DecodeError, Event, InvalidAnalysisResultError, LocalEvidence,
                           ModelUnavailableError, Rule, RuleCatalog)

RULE = Rule(rule_id="synthetic-rule", type_id="synthetic_type", type_name_ru="Синтетический тип",
            source_title="Синтетический тестовый источник", source_url="https://example.invalid/synthetic",
            clause="п. 0", effective_version_checked_at="2026-10-17", scope="тест",
            observable_condition="тест", required_context=[], counterexamples=[],
            claim_template_ru="Тестовое обоснование, не правовое.", status="verified")
CATALOG = RuleCatalog(schema_version="0.1", rules=[RULE])


def _jpeg(path: Path) -> Path:
    Image.new("RGB", (32, 24), (40, 90, 160)).save(path, "JPEG")
    return path


def create(endpoint: str | None, control: str) -> object:
    """Factory called inside the worker. Mode is read from ``control`` on every call."""
    started_with = endpoint

    def analyze(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
        mode = Path(control).read_text(encoding="utf-8").strip()
        assert video_path.name == "input.mov" and video_path.is_file()
        if started_with is not None:
            assert started_with.startswith("http://127.0.0.1:")
        if mode == "hang":
            while True:
                time.sleep(1)
        if mode == "crash":
            os._exit(3)
        if mode == "decode_error":
            raise DecodeError("synthetic")
        if mode == "model_unavailable":
            raise ModelUnavailableError("synthetic")
        if mode == "invalid":
            raise InvalidAnalysisResultError("synthetic")
        if mode == "no_violation":
            return AnalysisResult(schema_version="0.1", verdict="no_violation", events=[],
                                  limitations=[], timings_ms={"total": 1.0}, model_revision="synthetic")
        if mode == "not_a_result":
            return {"verdict": "no_violation"}  # type: ignore[return-value]
        frame_dir = job_dir if mode != "outside_path" else job_dir.parent
        evidence = [LocalEvidence(timestamp_sec=t, local_jpeg_path=str(_jpeg(frame_dir / f"f{i}.jpg")))
                    for i, t in enumerate((0.5, 1.0))]
        rule_id = "unknown-rule" if mode == "unknown_rule" else RULE.rule_id
        late = 999.0 if mode == "beyond_duration" else 1.5
        event = Event(event_id="e1", type_id=RULE.type_id, type_name_ru=RULE.type_name_ru, start_sec=0.5,
                      end_sec=late, timecode_sec=1.0, observed_fact="Синтетическое наблюдение",
                      rule_id=rule_id, evidence=evidence)
        return AnalysisResult(schema_version="0.1", verdict="violation", events=[event], limitations=[],
                              timings_ms={"total": 1.0}, model_revision="synthetic")

    return analyze


def server_main() -> None:
    """Fake llama-server: /health ok; spawns one grandchild to test tree cleanup."""
    import http.server
    import subprocess

    port, pid_file = int(sys.argv[2]), Path(sys.argv[3])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    pid_file.write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok"}'
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # noqa: ANN002
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def orphan_main() -> None:
    """Simulate supervisor death: own a sleeper via ProcessJob, then exit hard."""
    from app.jobs.procs import ProcessJob, popen_kwargs
    import subprocess

    job = ProcessJob()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], **popen_kwargs())
    job.assign(child.pid)
    Path(sys.argv[2]).write_text(str(child.pid), encoding="utf-8")
    os._exit(0)  # No cleanup code runs; the OS closes the job handle.


if __name__ == "__main__":
    {"server": server_main, "orphan": orphan_main}[sys.argv[1]]()
