"""B1: production factory kwargs -> spawned worker -> real service/Analyzer/HTTP client."""
import dataclasses
import json
import sqlite3
import sys
import time
from pathlib import Path

from app.api.main import create_app
from app.inference import service
from app.jobs import runtime
from app.settings import Settings
from tests.api.harness import LiveServer, make_mov
from tests.inference.fakes import catalog, synthetic_rule


def test_backend_timeout_reaches_real_service_and_bounds_model_http(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    cat_path = tmp_path / "SYNTHETIC-catalog.json"
    cat_path.write_text(catalog(synthetic_rule()).model_dump_json(), encoding="utf-8")
    settings = Settings.from_env({
        "ASSAR_API_KEY": "synthetic", "ASSAR_MODEL_CALL_TIMEOUT_SEC": "2",
        "ASSAR_MODEL_CONFIG_PATH": str(root / "tests/fixtures/model-obs-v01.json"),
        "ASSAR_RULES_CATALOG_PATH": str(cat_path),
        "ASSAR_UPLOADS_DIR": str(tmp_path / "uploads"),
        "ASSAR_OUTPUTS_DIR": str(tmp_path / "outputs"),
        "ASSAR_ANALYSIS_BUDGET_SEC": "30", "ASSAR_MODEL_STOP_GRACE_SEC": "2",
    })
    # Explicit CPU seam: no real runtime files/readiness proof. Never edit any flags.
    monkeypatch.setattr(runtime, "model_config_problems", lambda cfg: [])
    monkeypatch.setattr(service, "check_runtime", lambda *args: None)
    spec = runtime.production_spec(settings)
    assert spec.analyzer_factory == "app.inference.service:create_analyzer"
    assert spec.analyzer_kwargs["call_timeout_sec"] == 2.0
    record = tmp_path / "request-metadata.json"
    spec = dataclasses.replace(spec, server_command=lambda port: [
        sys.executable, "-m", "tests.integration.slow_model", str(port), str(record)])
    clip = make_mov(tmp_path / "synthetic.mov").read_bytes()
    app = create_app(settings, lambda _: spec, web_dir=None)
    with LiveServer(app) as live:
        initial_pids = app.state.service.supervisor.runtime_pids()
        start = time.monotonic()
        status, accepted = live.upload("synthetic", clip)
        assert status == 202
        result = live.wait_terminal("synthetic", accepted["job_id"], timeout=10)
        elapsed = time.monotonic() - start
        assert result["status"] == "failed"
        # A default 120 s timeout would let the 5 s server response finish successfully.
        # A 30 s job deadline cannot explain this early AnalysisTimeoutError either.
        assert 1.8 <= elapsed < 4.5
        with sqlite3.connect(settings.outputs_dir / "jobs.sqlite3") as db:
            reason = db.execute("SELECT reason FROM jobs WHERE job_id=?", (accepted["job_id"],)).fetchone()[0]
        assert reason == "AnalysisTimeoutError"
        assert json.loads(record.read_text(encoding="utf-8")) == {
            "path": "/v1/chat/completions", "stream": False, "schema": "json_schema"}
        live.wait_accepting("synthetic")
        restarted = app.state.service.supervisor.runtime_pids()
        assert all(restarted[key] != initial_pids[key] for key in ("server", "worker"))
