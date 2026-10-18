"""CPU checks of the future GPU harness. All servers and images are SYNTHETIC."""
import dataclasses
import json
import sys
import time
from pathlib import Path

import pytest

from app.contracts import InvalidAnalysisResultError
from scripts.validation import gpu_smoke
from tests.api.harness import make_mov
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_smoke_observer_preserves_finish_reason_and_logs_no_content(tmp_path, finish):
    content = json.dumps({"frames_assessable": True, "observations": []})
    server = FakeLlama(lambda indices: (200, content, finish))
    telemetry = tmp_path / "metadata.jsonl"
    try:
        analyze = gpu_smoke.observed_create_analyzer(
            telemetry_path=telemetry, endpoint=server.url,
            model_cfg=json.loads((ROOT / "tests/fixtures/model-obs-v01.json").read_text(encoding="utf-8")),
            catalog=catalog(synthetic_rule()), repo_root=ROOT, call_timeout_sec=17.0)
        video = make_mov(tmp_path / "synthetic.mov")
        job = tmp_path / "job"
        job.mkdir()
        if finish == "length":
            with pytest.raises(InvalidAnalysisResultError):
                analyze(video, job, time.monotonic() + 30)
        else:
            assert analyze(video, job, time.monotonic() + 30).verdict == "no_violation"
    finally:
        server.close()
    raw = telemetry.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    chat = next(r for r in records if r["kind"] == "chat")
    assert chat["finish_reason"] == finish and chat["schema_valid"] is True
    assert chat["timeout"] == 17.0
    assert "frames_assessable" not in raw and "observations" not in raw and "base64" not in raw


def test_gpu_harness_reuse_deadline_and_cleanup_on_cpu_fake_server(tmp_path, monkeypatch):
    # Keep real adapter, supervisor, HTTP API, observer and forced deadline mechanism.
    # Replace only GPU server argv and read-only GPU telemetry, never model results in the adapter.
    make_spec = gpu_smoke.smoke_spec

    def cpu_spec(settings, cfg, runtime_root, telemetry):
        spec = make_spec(settings, cfg, runtime_root, telemetry)
        return dataclasses.replace(spec, server_command=lambda port: [
            # Base interpreter avoids the venv launcher parent: like llama-server.exe,
            # the owned root must be the process actually serving HTTP when suspended.
            sys._base_executable, str(ROOT / "tests/integration/slow_model.py"), str(port),
            str(telemetry.parent / "protocol.json"), "0"])

    monkeypatch.setattr(gpu_smoke, "smoke_spec", cpu_spec)
    monkeypatch.setattr(gpu_smoke, "gpu_snapshot", lambda: {"gpu": None, "apps": None})
    cfg = json.loads((ROOT / "tests/fixtures/model-obs-v01.json").read_text(encoding="utf-8"))
    original_cfg = json.dumps(cfg, sort_keys=True)
    clip = make_mov(tmp_path / "synthetic.mov").read_bytes()
    normal = gpu_smoke.phase(tmp_path / "normal", cfg, ROOT, "synthetic", clip)
    assert all(normal["checks"].values()), normal["checks"]
    deadline = gpu_smoke.phase(tmp_path / "deadline", cfg, ROOT, "synthetic", clip, deadline_fault=True)
    assert all(deadline["checks"].values()), deadline["checks"]
    assert json.dumps(cfg, sort_keys=True) == original_cfg
