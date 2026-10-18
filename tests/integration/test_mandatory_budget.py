"""Required analysis cannot silently become a negative verdict. Synthetic pixels/facts only."""
import dataclasses
import json
import time
from pathlib import Path

import pytest
from PIL import Image

from app.contracts import AnalysisTimeoutError, DecodeError, ModelUnavailableError, UnassessableVideoError
from app.inference import analyzer as an, facts_v6, rules_v6
from app.inference.config import load_model_config
from app.inference.video import Frame, VideoInfo
from tests.inference.fakes import catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]


def execute(tmp_path, monkeypatch, *, mode="complete", cash=False, multi=False, active_cash=True, failure=None):
    cfg = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"),
                              prompt_version=facts_v6.PROMPT_VERSION, max_tokens=500)
    types = [rules_v6.TYPE_MULTI01] + ([rules_v6.TYPE_CASH03] if active_cash else [])
    cat = catalog(*[synthetic_rule(type_id=t, rule_id=f"TEST-{i}") for i, t in enumerate(types)])
    video = tmp_path / "synthetic.mov"
    video.write_bytes(b"synthetic probe seam")
    job = tmp_path / "job"
    job.mkdir()
    jpg = job / "synthetic.jpg"
    Image.new("RGB", (32, 32), "blue").save(jpg)
    frames = [Frame(i, float(i), jpg, jpg) for i in range(25)]
    monkeypatch.setattr(an, "probe", lambda *a, **k: VideoInfo(25, 0, 32, 32, 0, "synthetic", 1))
    monkeypatch.setattr(an, "sample_frames", lambda *a, **k: frames)

    def plan(budget, rest, n_passes):
        if mode == "desk" and rest[0][0].pts_sec >= 10:
            budget.skip_desk_from = 10.0
        if mode == "stride":
            budget.stride, budget.stride_from = 2, 0.0
    monkeypatch.setattr(an._Budget, "plan", plan)
    monkeypatch.setattr(an._Budget, "fits", lambda b, n: mode != "verify" and not (mode == "boxes" and n == 1))

    class Client:
        def chat(self, parts, schema, **kw):
            if "people" in schema["properties"]:
                if failure == "annotation":
                    raise AnalysisTimeoutError("synthetic annotation timeout")
                obj = {"people": []}
            else:
                props = schema["properties"]["frames"]["items"]["properties"]
                idx = props["index"]["enum"]
                if failure == "transport" and idx[0] >= 10:
                    raise ModelUnavailableError("synthetic transport failure")
                if "cashier" in props:
                    obj = {"frames": [{"index": i, "cashier": "no" if cash and i < 5 else "yes",
                                       "others": "2+" if multi and 5 <= i <= (6 if mode == "short" else 9) else "0"} for i in idx]}
                elif "desk_valuables" in props:
                    obj = {"frames": [{"index": i, "desk_valuables": "yes" if cash and i < 5 else "no"} for i in idx]}
                else:
                    obj = {"frames": [{"index": i, "two_others_inside": "yes"} for i in idx]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(obj)}}]}

    analyzer = an.Analyzer(cfg, cat, Client())
    return analyzer, lambda: analyzer.analyze_video(video, job, time.monotonic() + 60)


@pytest.mark.parametrize("mode,multi", [("desk", False), ("stride", False), ("verify", True)])
def test_unfinished_required_analysis_is_failed_without_events(tmp_path, monkeypatch, mode, multi):
    analyzer, run = execute(tmp_path, monkeypatch, mode=mode, multi=multi)
    with pytest.raises(AnalysisTimeoutError, match="mandatory analysis incomplete"):
        run()
    assert analyzer.last_trace["mandatory_analysis_complete"] is False


@pytest.mark.parametrize("mode,multi", [("desk", False), ("verify", True)])
def test_confirmed_event_survives_incomplete_other_checks_with_limitation(tmp_path, monkeypatch, mode, multi):
    analyzer, run = execute(tmp_path, monkeypatch, mode=mode, cash=True, multi=multi)
    result = run()
    assert result.verdict == "violation"
    assert [e.type_id for e in result.events] == [rules_v6.TYPE_CASH03]
    assert any("mandatory" in note and "skipped" in note for note in result.limitations)
    assert analyzer.last_trace["mandatory_analysis_complete"] is False
    from app.jobs.serialize import to_done_job
    public = to_done_job("synthetic", result, analyzer.catalog, 25, tmp_path / "job", 5 * 1024 * 1024)
    assert "Обязательный анализ завершён не полностью" in public.violations[0].description
    assert result.events[0].observed_fact.rstrip(".") in public.violations[0].description
    assert analyzer.catalog.rules[1].source_url in public.violations[0].description


def test_completed_negative_is_allowed(tmp_path, monkeypatch):
    analyzer, run = execute(tmp_path, monkeypatch)
    assert run().verdict == "no_violation"
    assert analyzer.last_trace["mandatory_analysis_complete"] is True


def test_three_frame_verification_is_not_claimed_for_two_frame_candidate(tmp_path, monkeypatch):
    _, run = execute(tmp_path, monkeypatch, mode="short", multi=True)
    # A successful decode with insufficient frames is distinct from a broken decoder.
    monkeypatch.setattr(an, "sample_range", lambda *a, **k: [])
    with pytest.raises(UnassessableVideoError, match="three distinct"):
        run()


@pytest.mark.parametrize("cash", [False, True])
def test_third_frame_decoder_error_is_failed_even_with_other_event(tmp_path, monkeypatch, cash):
    _, run = execute(tmp_path, monkeypatch, mode="short", multi=True, cash=cash)
    def broken(*a, **k):
        raise DecodeError("synthetic range decoder failure")
    monkeypatch.setattr(an, "sample_range", broken)
    with pytest.raises(DecodeError, match="synthetic range decoder failure"):
        run()


def test_insufficient_third_frame_preserves_other_event_with_limitation(tmp_path, monkeypatch):
    analyzer, run = execute(tmp_path, monkeypatch, mode="short", multi=True, cash=True)
    monkeypatch.setattr(an, "sample_range", lambda *a, **k: [])
    result = run()
    assert result.verdict == "violation"
    assert [e.type_id for e in result.events] == [rules_v6.TYPE_CASH03]
    assert analyzer.last_trace["mandatory_analysis_complete"] is False
    assert any("mandatory" in note for note in result.limitations)


def test_inactive_rule_does_not_require_desk_pass(tmp_path, monkeypatch):
    analyzer, run = execute(tmp_path, monkeypatch, mode="desk", active_cash=False)
    assert run().verdict == "no_violation"
    assert analyzer.last_trace["mandatory_analysis_complete"] is True


def test_optional_boxes_can_be_skipped(tmp_path, monkeypatch):
    analyzer, run = execute(tmp_path, monkeypatch, mode="boxes", multi=True)
    result = run()
    assert result.verdict == "violation" and len(result.events[0].evidence) == 3
    assert analyzer.last_trace["mandatory_analysis_complete"] is True


@pytest.mark.parametrize("failure,error", [("transport", ModelUnavailableError), ("annotation", AnalysisTimeoutError)])
def test_technical_error_remains_failed_even_after_confirmed_event(tmp_path, monkeypatch, failure, error):
    _, run = execute(tmp_path, monkeypatch, cash=True, multi=True, failure=failure)
    with pytest.raises(error):
        run()
