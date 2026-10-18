"""Production readiness refusal, store, instance lock and orphan protection."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from app.contracts import DoneJob, ModelUnavailableError
from app.jobs.procs import pid_alive
from app.jobs.runtime import REPO_ROOT, REQUIRED_VALIDATION, ServiceNotReady, model_config_problems, production_spec
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


def test_pending_config_and_empty_catalog_refuse_production(tmp_path):
    # Holds whether or not the ML module is present in this checkout.
    with pytest.raises(ServiceNotReady) as err:
        production_spec(_settings(tmp_path, json.loads((REPO_ROOT / "tests/fixtures/model-obs-v01.json").read_text(encoding="utf-8")), []))
    text = str(err.value)
    assert "pending_integration_and_3060_validation" in text and "no verified rules" in text
    assert "validation flags are not all true" in text


def _ready_config() -> dict:
    cfg = json.loads((REPO_ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
    cfg["status"] = "ready"
    cfg["validation"] = {name: True for name in REQUIRED_VALIDATION}
    return cfg


def _no_ml_module(monkeypatch) -> None:
    # Independent of the checkout: a None entry makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "app.inference.service", None)


def _variant(**changes) -> dict:
    cfg = _ready_config()
    for key, value in changes.items():
        if value is _DROP:
            cfg.pop(key)
        else:
            cfg[key] = value
    return cfg


_DROP = object()


def test_ready_config_passes_model_checks():
    assert model_config_problems(_ready_config()) == []
    assert set(json.loads((REPO_ROOT / "configs/model.yaml").read_text(encoding="utf-8"))["validation"])         == REQUIRED_VALIDATION  # Flags pinned to the shared config.


@pytest.mark.parametrize("validation", [
    {"x": True},                                           # Arbitrary non-empty dict of true.
    {"weights_checked_on_this_host": True, "gpu_3060_measured": True},  # Missing flag.
    {**{n: True for n in REQUIRED_VALIDATION}, "extra": True},           # Unknown flag.
    {**{n: True for n in REQUIRED_VALIDATION}, "gpu_3060_measured": "true"},
    {**{n: True for n in REQUIRED_VALIDATION}, "gpu_3060_measured": 1},
    {**{n: True for n in REQUIRED_VALIDATION}, "gpu_3060_measured": False},
    [], "true",
])
def test_validation_flags_must_match_exactly(tmp_path, monkeypatch, validation):
    _no_ml_module(monkeypatch)
    with pytest.raises(ServiceNotReady, match="validation"):
        production_spec(_settings(tmp_path, _variant(validation=validation), [RULE.model_dump()]))


@pytest.mark.parametrize("changes", [
    {"status": "READY"}, {"status": True}, {"external_inference": True}, {"audio_enabled": 0},
    {"process_owner": "user"}, {"reuse_between_jobs": 1}, {"backend": "transformers"},
    {"weights": "models/x.gguf"}, {"model_revision": _DROP}, {"schema_version": 0.1},
])
def test_config_fields_and_types_are_checked(tmp_path, monkeypatch, changes):
    _no_ml_module(monkeypatch)
    with pytest.raises(ServiceNotReady, match="model "):
        production_spec(_settings(tmp_path, _variant(**changes), [RULE.model_dump()]))


def test_ready_model_still_needs_verified_rules_and_ml_module(tmp_path, monkeypatch):
    _no_ml_module(monkeypatch)
    with pytest.raises(ServiceNotReady, match="no verified rules"):
        production_spec(_settings(tmp_path, _ready_config(),
                                  [RULE.model_copy(update={"status": "candidate"}).model_dump()]))
    with pytest.raises(ServiceNotReady) as err:
        production_spec(_settings(tmp_path, _ready_config(), [RULE.model_dump()]))
    assert str(err.value) == "app.inference.service is not available"  # Only the module is missing.


def test_ml_module_without_interface_is_refused(tmp_path, monkeypatch):
    partial = types.ModuleType("app.inference.service")
    partial.check_runtime = lambda cfg, root: None
    monkeypatch.setitem(sys.modules, "app.inference.service", partial)
    with pytest.raises(ServiceNotReady, match="lacks server_command, create_analyzer"):
        production_spec(_settings(tmp_path, _ready_config(), [RULE.model_dump()]))


def test_failing_runtime_check_is_refused(tmp_path, monkeypatch):
    fake = types.ModuleType("app.inference.service")

    def check_runtime(cfg, root):
        raise ModelUnavailableError("weights missing")
    fake.check_runtime, fake.server_command, fake.create_analyzer = check_runtime, None, None
    monkeypatch.setitem(sys.modules, "app.inference.service", fake)
    with pytest.raises(ServiceNotReady, match="ModelUnavailableError"):
        production_spec(_settings(tmp_path, _ready_config(), [RULE.model_dump()]))


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
