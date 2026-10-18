"""CPU integration probes. All observations, images and rules here are SYNTHETIC.

Fixed handoff regressions remain ordinary tests; no xfail masks a requirement.
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.contracts import DoneJob, InvalidAnalysisResultError, UnassessableVideoError
from app.inference import Analyzer, load_model_config
from app.inference import analyzer as ml
from app.inference.video import Frame, VideoInfo
from app.jobs import media
from app.jobs import runtime
from app.jobs.serialize import to_done_job
from app.jobs.procs import ProcessOwner
from app.jobs.store import JobStore
from app.jobs.supervisor import State, Supervisor, Ticket
from app.settings import Settings
from tests.inference.fakes import catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def synthetic_analysis(tmp_path, monkeypatch):
    upload = tmp_path / "upload"
    upload.mkdir()
    video = upload / "input.mov"
    video.write_bytes(b"SYNTHETIC: decoder is replaced in this test")
    job = tmp_path / "job"
    job.mkdir()
    frames = []
    for i, pts in enumerate((0.0, 10.0, 20.0)):
        jpeg = job / f"synthetic-{i}.jpg"
        Image.new("RGB", (32, 24), (i * 40, 20, 80)).save(jpeg, "JPEG")
        frames.append(Frame(i, pts, jpeg, jpeg))
    monkeypatch.setattr(ml, "probe", lambda *a, **k: VideoInfo(25, 0, 32, 24, 0, "synthetic", 1))
    monkeypatch.setattr(ml, "sample_frames", lambda *a, **k: frames)
    cat = catalog(synthetic_rule())
    cfg = load_model_config(ROOT / "tests/fixtures/model-obs-v01.json")

    def run(replies):
        iterator = iter(replies)

        class Client:
            def chat(self, *a, **k):
                return {"choices": [{"message": {"content": json.dumps(next(iterator))},
                                     "finish_reason": "stop"}]}

        result = Analyzer(cfg, cat, Client()).analyze_video(video, job, time.monotonic() + 30)
        return result, cat, video, job

    return run


def reply(description=None, *, index=0, assessable=True, category="cash_bag_handover"):
    obs = [] if description is None else [
        {"category": category, "frame_indices": [index], "description": description}]
    return {"frames_assessable": assessable, "observations": obs}


def test_ml_observation_and_source_survive_serialization_cleanup_and_store_reopen(synthetic_analysis, tmp_path):
    facts = ["SYNTHETIC observation A", "SYNTHETIC observation B"]
    result, cat, video, job = synthetic_analysis([reply(facts[0]), reply(facts[1], index=1), reply()])
    done = to_done_job("synthetic-job", result, cat, 25, job, 5 * 1024 * 1024)
    db = tmp_path / "jobs.sqlite3"
    store = JobStore(db)
    store.create(done.job_id)
    store.set_done(done)
    store.close()
    Supervisor._cleanup(None, Ticket(done.job_id, video, job, 25, time.monotonic() + 30))
    assert not job.exists() and not video.parent.exists()
    reopened = JobStore(db)
    try:
        saved = DoneJob.model_validate_json(reopened.public_json(done.job_id))
    finally:
        reopened.close()
    assert len(saved.violations) == 2
    for violation, fact in zip(saved.violations, facts):
        assert fact in violation.description
        assert cat.rules[0].claim_template_ru in violation.description
        assert cat.rules[0].source_title in violation.description
        assert cat.rules[0].source_url in violation.description
        for frame in violation.evidence_frames:
            with Image.open(io.BytesIO(base64.b64decode(frame.image_base64, validate=True))) as image:
                assert image.format == "JPEG"
                image.verify()


def test_recovery_after_close_cannot_spawn(tmp_path, monkeypatch):
    from app.jobs.supervisor import RuntimeSpec
    store = JobStore(tmp_path / "shutdown.sqlite3")
    supervisor = Supervisor(Settings(api_key="synthetic-test-key"),
                            RuntimeSpec("unused:factory", {}, catalog(synthetic_rule())), store)
    starts = []
    monkeypatch.setattr(supervisor, "_start_runtime", lambda: starts.append("spawn"))
    try:
        supervisor.close()  # Sets STOPPED and the new closing event; starts no processes.
        supervisor._recover()  # A late failed _run can enter here after close().
        supervisor._reopen()
        assert starts == []
        assert supervisor.state is State.STOPPED
    finally:
        supervisor._worker.job.close()
        supervisor._server.job.close()
        store.close()


def test_admission_probe_spends_one_shared_budget(tmp_path, monkeypatch):
    now = [100.0]
    budgets = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    class SyntheticProcess:
        returncode = 0

        def communicate(self, timeout):
            budgets.append(timeout)
            # First stage consumes four seconds, second fits in the remainder.
            now[0] += 4.0 if len(budgets) == 1 else 0.5
            payload = {"format": {"format_name": "mov", "duration": "25"},
                       "streams": [{"codec_name": "synthetic"}]}
            return json.dumps(payload).encode(), None

    monkeypatch.setattr(media, "spawn_in_job", lambda *a, **k: SyntheticProcess())
    owner = ProcessOwner()
    try:
        media.probe_mov(tmp_path / "synthetic.mov", deadline_monotonic=105, owner=owner)
    finally:
        owner.close()
    assert len(budgets) == 2
    assert budgets[0] == 5
    assert 0 < budgets[1] <= 1.0


def test_unreadable_window_cannot_prove_no_violation(synthetic_analysis):
    with pytest.raises(UnassessableVideoError):
        synthetic_analysis([reply(), reply(assessable=False), reply()])


def test_empty_individual_observation_is_rejected(synthetic_analysis):
    with pytest.raises(InvalidAnalysisResultError):
        synthetic_analysis([reply("   "), reply(), reply()])


def test_unmapped_observation_cannot_prove_no_violation(synthetic_analysis):
    with pytest.raises(UnassessableVideoError):
        synthetic_analysis([reply("SYNTHETIC unmapped", category="other_notable_action"), reply(), reply()])


def test_unsupported_candidate_observation_is_rejected(synthetic_analysis):
    # Candidate-category and generic observations keep distinct failure types.
    with pytest.raises(InvalidAnalysisResultError):
        synthetic_analysis([reply("SYNTHETIC candidate", category="cash_bag_opened"), reply(), reply()])


def test_readiness_requires_named_evidence_flags(tmp_path, monkeypatch):
    cfg_path, cat_path = tmp_path / "model.json", tmp_path / "catalog.json"
    cfg = json.loads((ROOT / "tests/fixtures/model-obs-v01.json").read_text(encoding="utf-8"))
    cfg.update(status="ready", validation={"unrelated": True})
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    cat_path.write_text(catalog(synthetic_rule()).model_dump_json(), encoding="utf-8")
    service = SimpleNamespace(check_runtime=lambda *a: None, server_command=lambda *a: [],
                              create_analyzer=lambda **k: None)
    monkeypatch.setattr(runtime.importlib, "import_module", lambda *a: service)
    # Offline environment changes are also restored after this test.
    monkeypatch.setattr(runtime, "configure_offline_environment", lambda: None)
    settings = Settings(api_key="synthetic-test-key", model_config_path=cfg_path, rules_catalog_path=cat_path)
    with pytest.raises(runtime.ServiceNotReady, match="validation"):
        runtime.production_spec(settings)
