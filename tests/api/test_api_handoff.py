"""Integrator's backend reproducers (codex/integration-cpu f2bf612, tests/integration/
test_handoff.py) adapted to the fixed signatures with the same meaning, plus B1."""
from __future__ import annotations

import dataclasses
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import media, runtime
from app.jobs.procs import ProcessOwner
from app.jobs.store import JobStore
from app.jobs.supervisor import State, Supervisor
from app.settings import Settings
from tests.api.fakes import CATALOG

ROOT = Path(__file__).resolve().parents[2]


def _ready_files(tmp_path: Path, validation: dict) -> tuple[Path, Path]:
    cfg = json.loads((ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
    cfg.update(status="ready", validation=validation)
    cfg_path, cat_path = tmp_path / "model.json", tmp_path / "catalog.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    cat_path.write_text(CATALOG.model_dump_json(), encoding="utf-8")
    return cfg_path, cat_path


def test_recovery_after_close_cannot_spawn(tmp_path, monkeypatch):
    # B2 (original: object.__new__ + STOPPED state). Now: a real close(), then a late _recover.
    supervisor = Supervisor(Settings(api_key="synthetic"), runtime.RuntimeSpec("x:y", {}, CATALOG),
                            JobStore(tmp_path / "j.sqlite3"))
    supervisor.close()
    starts = []
    monkeypatch.setattr(supervisor, "_stop_runtime", lambda final=False: True)
    monkeypatch.setattr(supervisor, "_start_runtime", lambda: starts.append("spawn"))
    supervisor._recover()  # A late failed _run can enter here after close().
    supervisor._reopen()
    assert starts == []
    assert supervisor.state is State.STOPPED
    assert not supervisor.try_reserve()


def test_admission_probe_spends_one_shared_budget(tmp_path, monkeypatch):
    # B3 (original: probe_mov(path, timeout=5), fake _run(cmd, timeout)). Now the budget
    # each stage receives is the remaining part of one absolute deadline.
    now = [100.0]
    budgets = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def fake_run(cmd, deadline, owner):
        budgets.append(deadline - now[0])
        now[0] += 4.0  # First probe consumes four seconds of a five-second total.
        payload = {"format": {"format_name": "mov", "duration": "25"},
                   "streams": [{"codec_name": "synthetic"}]}
        return 0, json.dumps(payload).encode()

    monkeypatch.setattr(media, "_run", fake_run)
    with pytest.raises(media.InvalidVideo):  # 8 s spent of 5 s: the late decode is rejected.
        media.probe_mov(tmp_path / "synthetic.mov", now[0] + 5, ProcessOwner())
    assert len(budgets) == 2
    assert 0 < budgets[1] <= 1.0


def test_readiness_requires_named_evidence_flags(tmp_path, monkeypatch):
    # B7, unchanged except the catalog fixture source.
    cfg_path, cat_path = _ready_files(tmp_path, {"unrelated": True})
    service = SimpleNamespace(check_runtime=lambda *a: None, server_command=lambda *a: [],
                              create_analyzer=lambda **k: None)
    monkeypatch.setattr(runtime.importlib, "import_module", lambda *a: service)
    monkeypatch.setattr(runtime, "configure_offline_environment", lambda: None)
    settings = Settings(api_key="synthetic-test-key", model_config_path=cfg_path, rules_catalog_path=cat_path)
    with pytest.raises(runtime.ServiceNotReady, match="validation"):
        runtime.production_spec(settings)


def _ready_spec(tmp_path: Path, monkeypatch, **settings_overrides) -> runtime.RuntimeSpec:
    cfg_path, cat_path = _ready_files(tmp_path, {name: True for name in runtime.REQUIRED_VALIDATION})
    service = types.ModuleType("app.inference.service")
    service.check_runtime = lambda cfg, root: None
    service.server_command = lambda cfg, root, port: []
    service.create_analyzer = lambda **k: None
    monkeypatch.setitem(sys.modules, "app.inference.service", service)
    settings = Settings(api_key="synthetic", model_config_path=cfg_path, rules_catalog_path=cat_path,
                        **settings_overrides)
    return runtime.production_spec(settings)


def test_call_timeout_default_is_passed(tmp_path, monkeypatch):
    spec = _ready_spec(tmp_path, monkeypatch)
    assert spec.analyzer_kwargs["call_timeout_sec"] == 120.0
    assert type(spec.analyzer_kwargs["call_timeout_sec"]) is float


def test_custom_call_timeout_reaches_factory_in_worker(tmp_path, monkeypatch):
    # B1: ASSAR_MODEL_CALL_TIMEOUT_SEC=7 must arrive at create_analyzer in the spawned worker.
    settings = Settings.from_env({"ASSAR_API_KEY": "synthetic", "ASSAR_MODEL_CALL_TIMEOUT_SEC": "7"})
    spec = _ready_spec(tmp_path, monkeypatch, model_call_timeout_sec=settings.model_call_timeout_sec)
    assert spec.analyzer_kwargs["call_timeout_sec"] == 7.0
    record = tmp_path / "factory_kwargs.json"
    monkeypatch.setenv("ASSAR_TEST_RECORD", str(record))  # Inherited by the spawned worker.
    # Same production kwargs; only the factory is a test recorder (no ML, no model server).
    spec = dataclasses.replace(spec, analyzer_factory="tests.api.fakes:record_kwargs", server_command=None)
    supervisor = Supervisor(Settings(api_key="synthetic", outputs_dir=tmp_path / "o",
                                     model_start_timeout_sec=60, model_stop_grace_sec=2),
                            spec, JobStore(tmp_path / "j.sqlite3"))
    supervisor.start()
    try:
        received = json.loads(record.read_text(encoding="utf-8"))
    finally:
        supervisor.close()
    assert received == {"endpoint": None, "model_cfg": "dict", "catalog": "RuleCatalog",
                        "repo_root": "WindowsPath" if sys.platform == "win32" else "PosixPath",
                        "call_timeout_sec": 7.0}
