"""Production readiness refusal, store, instance lock and orphan protection."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.contracts import DoneJob
from app.jobs.procs import pid_alive
from app.jobs.runtime import REPO_ROOT, ServiceNotReady, production_spec
from app.jobs.store import JobStore
from app.jobs.supervisor import InstanceLock, StartupError
from app.settings import Settings
from tests.api.fakes import RULE


def _settings(tmp_path: Path, model: dict | None = None, rules: list | None = None) -> Settings:
    model_path, catalog_path = REPO_ROOT / "configs/model.yaml", REPO_ROOT / "app/rules/catalog.json"
    if model is not None:
        model_path = tmp_path / "model.yaml"
        model_path.write_text(json.dumps(model), encoding="utf-8")
    if rules is not None:
        catalog_path = tmp_path / "catalog.json"
        catalog_path.write_text(json.dumps({"schema_version": "0.1", "rules": rules}), encoding="utf-8")
    return Settings(api_key="k", outputs_dir=tmp_path / "out", model_config_path=model_path,
                    rules_catalog_path=catalog_path)


def test_current_repo_config_refuses_production(tmp_path):
    with pytest.raises(ServiceNotReady) as err:
        production_spec(_settings(tmp_path))
    text = str(err.value)
    assert "pending_integration_and_3060_validation" in text and "no verified rules" in text


def test_ready_model_still_needs_verified_rules_and_ml_module(tmp_path):
    ready = {"status": "ready", "validation": {"a": True}}
    with pytest.raises(ServiceNotReady, match="no verified rules"):
        production_spec(_settings(tmp_path, ready, [RULE.model_copy(update={"status": "candidate"}).model_dump()]))
    with pytest.raises(ServiceNotReady, match="app.inference.service"):
        production_spec(_settings(tmp_path, ready, [RULE.model_dump()]))
    with pytest.raises(ServiceNotReady, match="validation flags"):
        production_spec(_settings(tmp_path, {"status": "ready", "validation": {"a": False}}, [RULE.model_dump()]))


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_lifespan_fails_without_ready_model(tmp_path):
    from app.api.main import create_app
    from tests.api.harness import LiveServer
    with pytest.raises(RuntimeError, match="server failed to start"):
        with LiveServer(create_app(_settings(tmp_path), web_dir=None)):
            pass


def test_store_states_and_restart(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create("a")
    store.create("b")
    assert json.loads(store.public_json("a")) == {"job_id": "a", "status": "processing"}
    store.set_running("a")
    assert json.loads(store.public_json("a"))["status"] == "processing"
    store.set_failed("a", "WatchdogTimeout")
    store.set_done(DoneJob(job_id="a", status="done", verdict="no_violation", violations=[]))
    assert json.loads(store.public_json("a")) == {"job_id": "a", "status": "failed"}  # Terminal is final.
    assert store.fail_unfinished("ServiceRestart") == ["b"]
    assert json.loads(store.public_json("b"))["status"] == "failed"
    assert store.public_json("missing") is None
    store.close()


def test_second_instance_is_refused(tmp_path):
    first = InstanceLock(tmp_path / ".lock")
    with pytest.raises(StartupError):
        InstanceLock(tmp_path / ".lock")
    first.release()
    InstanceLock(tmp_path / ".lock").release()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object is the Windows orphan guard")
def test_owned_tree_dies_with_supervisor(tmp_path):
    pid_file = tmp_path / "pid"
    code = (f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
            "from tests.api.fakes import orphan_main; orphan_main()")
    subprocess.run([sys.executable, "-c", code, "orphan", str(pid_file)], check=True, timeout=60)
    pid = int(pid_file.read_text(encoding="utf-8"))
    limit = time.monotonic() + 10
    while pid_alive(pid) and time.monotonic() < limit:
        time.sleep(0.1)
    assert not pid_alive(pid)
