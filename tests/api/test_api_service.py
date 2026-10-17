"""End-to-end API tests over real HTTP with a synthetic analyzer (test-only)."""
from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest

from app.api.main import create_app
from app.contracts import JOB_RESPONSE_ADAPTER, DoneJob, validate_done_for_video, validate_jpeg_bytes
from app.jobs.procs import pid_alive
from app.jobs.supervisor import RuntimeSpec
from app.settings import Settings
from tests.api.fakes import CATALOG
from tests.api.harness import LiveServer, fake_server_command, have_ffmpeg, make_mov

pytestmark = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not in PATH")
KEY = "test-key-not-a-secret"
DURATION = 3.0


@pytest.fixture(scope="module")
def mov(tmp_path_factory) -> bytes:
    return make_mov(tmp_path_factory.mktemp("video") / "synthetic.mov", int(DURATION)).read_bytes()


@pytest.fixture
def control(tmp_path) -> Path:
    path = tmp_path / "mode.txt"
    path.write_text("violation", encoding="utf-8")
    return path


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(api_key=KEY, uploads_dir=tmp_path / "uploads", outputs_dir=tmp_path / "outputs",
                  request_timeout_sec=30, analysis_budget_sec=6, response_reserve_sec=1,
                  model_start_timeout_sec=60, model_stop_grace_sec=3)
    values.update(overrides)
    return Settings(**values)


def _live(tmp_path: Path, control: Path, with_server: bool = False, web_dir: Path | None = None,
          **overrides) -> LiveServer:
    settings = _settings(tmp_path, **overrides)
    command = fake_server_command(tmp_path / "server.pid") if with_server else None
    spec = RuntimeSpec(analyzer_factory="tests.api.fakes:create", analyzer_kwargs={"control": str(control)},
                       catalog=CATALOG, server_command=command)
    return LiveServer(create_app(settings, lambda s: spec, web_dir=web_dir))


def _server_pids(tmp_path: Path) -> tuple[int, int]:
    server, child = (tmp_path / "server.pid").read_text(encoding="utf-8").split()
    return int(server), int(child)


def _pids(live: LiveServer) -> dict:
    return live.server.config.app.state.service.supervisor.runtime_pids()


def test_key_required_for_both_endpoints(tmp_path, control, mov):
    with _live(tmp_path, control) as live:
        assert live.upload(None, mov)[0] == 401
        assert live.upload("wrong", mov)[0] == 401
        status, body = live.upload(KEY, mov)
        assert status == 202 and set(body) == {"job_id"}
        assert live.request("GET", f"/api/v1/jobs/{body['job_id']}", "wrong")[0] == 401
        assert live.request("GET", f"/api/v1/jobs/{body['job_id']}")[0] == 401
        assert KEY not in str(live.request("GET", "/api/v1/jobs/x", "wrong")[1])


def test_unknown_job_is_404(tmp_path, control):
    with _live(tmp_path, control) as live:
        assert live.request("GET", "/api/v1/jobs/does-not-exist", KEY)[0] == 404


def test_violation_has_exact_public_schema(tmp_path, control, mov):
    with _live(tmp_path, control) as live:
        status, accepted = live.upload(KEY, mov, filename="Какое-то ИМЯ.MOV")
        assert status == 202
        body = live.wait_terminal(KEY, accepted["job_id"])
        assert set(body) == {"job_id", "status", "verdict", "violations"}
        job = JOB_RESPONSE_ADAPTER.validate_python(body)
        assert isinstance(job, DoneJob) and job.verdict == "violation"
        validate_done_for_video(job, DURATION)
        event = body["violations"][0]
        assert set(event) == {"type", "timecode_sec", "description", "evidence_frames"}
        assert event["type"] == "Синтетический тип"
        assert "https://example.invalid/synthetic" in event["description"]
        assert "Синтетическое наблюдение" in event["description"]
        assert 1 <= len(event["evidence_frames"]) <= 3
        for frame in event["evidence_frames"]:
            assert set(frame) == {"timestamp_sec", "image_base64"}
            assert not frame["image_base64"].startswith("data:")
            validate_jpeg_bytes(base64.b64decode(frame["image_base64"], validate=True))
        # Upload and frame files are removed after the result is stored.
        assert not any((tmp_path / "uploads").iterdir())
        assert not any((tmp_path / "outputs" / "jobs").iterdir())


def test_no_violation_has_empty_list(tmp_path, control, mov):
    control.write_text("no_violation", encoding="utf-8")
    with _live(tmp_path, control) as live:
        body = live.wait_terminal(KEY, live.upload(KEY, mov)[1]["job_id"])
        assert body == {"job_id": body["job_id"], "status": "done", "verdict": "no_violation", "violations": []}


@pytest.mark.parametrize("filename,content_type,status", [
    ("clip.mp4", None, 415),
    ("clip", None, 415),
])
def test_wrong_extension_is_415(tmp_path, control, mov, filename, content_type, status):
    with _live(tmp_path, control) as live:
        assert live.upload(KEY, mov, filename=filename)[0] == status


def test_non_multipart_is_415(tmp_path, control, mov):
    with _live(tmp_path, control) as live:
        assert live.request("POST", "/api/v1/analyze", KEY, mov, "video/quicktime")[0] == 415


def test_corrupt_and_missing_files_are_422_and_free_the_slot(tmp_path, control, mov):
    with _live(tmp_path, control) as live:
        assert live.upload(KEY, b"not a video at all" * 100)[0] == 422
        assert live.upload(KEY, mov[: len(mov) // 3])[0] == 422  # Truncated container.
        assert live.upload(KEY, mov, field="video")[0] == 422
        assert live.upload(KEY, b"")[0] == 422
        assert live.upload(KEY, mov)[0] == 202  # Rejections released the single slot.


def test_oversize_is_413(tmp_path, control, mov):
    with _live(tmp_path, control, max_upload_bytes=1024) as live:
        assert live.upload(KEY, mov)[0] == 413
        assert not any((tmp_path / "uploads").iterdir())


@pytest.mark.parametrize("mode", ["decode_error", "invalid", "outside_path", "unknown_rule",
                                  "beyond_duration"])
def test_analysis_errors_are_failed_not_no_violation(tmp_path, control, mov, mode):
    control.write_text(mode, encoding="utf-8")
    with _live(tmp_path, control, with_server=True) as live:
        before = _pids(live)
        body = live.wait_terminal(KEY, live.upload(KEY, mov)[1]["job_id"])
        assert body == {"job_id": body["job_id"], "status": "failed"}
        live.wait_accepting(KEY)
        assert _pids(live) == before  # Soft errors reuse the healthy owned runtime.


def test_hang_is_failed_by_watchdog_and_runtime_restarts(tmp_path, control, mov):
    control.write_text("hang", encoding="utf-8")
    with _live(tmp_path, control, with_server=True) as live:
        before = _pids(live)
        old_server, old_grandchild = _server_pids(tmp_path)
        started = time.monotonic()
        job_id = live.upload(KEY, mov)[1]["job_id"]
        assert live.upload(KEY, mov)[0] == 503  # One in-flight job, no queue.
        assert live.request("GET", f"/api/v1/jobs/{job_id}", KEY)[1]["status"] == "processing"
        body = live.wait_terminal(KEY, job_id)
        assert body["status"] == "failed"
        assert time.monotonic() - started < 6 + 5  # analysis_budget_sec + slack, < request timeout.
        live.wait_accepting(KEY)
        after = _pids(live)
        assert after["worker"] != before["worker"] and after["server"] != before["server"]
        for pid in (before["worker"], old_server, old_grandchild):
            assert not pid_alive(pid)
        control.write_text("violation", encoding="utf-8")
        assert live.wait_terminal(KEY, live.upload(KEY, mov)[1]["job_id"])["status"] == "done"


@pytest.mark.parametrize("mode", ["crash", "model_unavailable", "not_a_result"])
def test_crash_or_model_loss_restarts_both_processes(tmp_path, control, mov, mode):
    control.write_text(mode, encoding="utf-8")
    with _live(tmp_path, control, with_server=True) as live:
        before = _pids(live)
        body = live.wait_terminal(KEY, live.upload(KEY, mov)[1]["job_id"])
        assert body["status"] == "failed"
        live.wait_accepting(KEY)
        after = _pids(live)
        assert after["worker"] != before["worker"] and after["server"] != before["server"]
        assert not pid_alive(before["server"])


def test_shutdown_stops_owned_server_tree(tmp_path, control):
    with _live(tmp_path, control, with_server=True) as live:
        pids = _pids(live)
        # Popen pid may be the venv launcher; the interpreter is its child in the same job.
        server, grandchild = _server_pids(tmp_path)
        assert all(pid_alive(p) for p in (pids["server"], server, grandchild))
    for pid in (pids["worker"], pids["server"], server, grandchild):
        assert not pid_alive(pid)


def test_static_web_is_optional(tmp_path, control):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>assar</title>", encoding="utf-8")
    with _live(tmp_path, control, web_dir=web) as live:
        status, body = live.request("GET", "/")
        assert status == 200 and "assar" in body
        assert live.request("GET", "/api/v1/jobs/x", KEY)[0] == 404
