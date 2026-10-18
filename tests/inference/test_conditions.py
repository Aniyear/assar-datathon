"""CASH-01: условия по покадровым фактам и режим cash01-facts-v1 анализатора. Только синтетика."""
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import (AnalysisTimeoutError, InvalidAnalysisResultError, UnassessableVideoError,
                           validate_analysis_for_video, validate_analysis_rules)
from app.inference import Analyzer, AnalyzerNotReadyError, LlamaClient, facts_prompt, load_model_config
from app.inference.conditions import FrameFacts, evaluate_cash01
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
TYPE = facts_prompt.SUPPORTED_TYPE_ID


def tl(spec: str) -> list[FrameFacts]:
    """Каждый кадр — 3 символа cashier/client/service: y/n/u. Шаг 1 с."""
    m = {"y": "yes", "n": "no", "u": "unknown"}
    return [FrameFacts(i, float(i), m[s[0]], m[s[1]], m[s[2]]) for i, s in enumerate(spec.split())]


POSITIVE = "yyn yyy yyn nyn nyn nyn nyn yyn yyy yyn"


def test_positive_sequence_is_event():
    out = evaluate_cash01(tl(POSITIVE))
    assert len(out.events) == 1 and not out.insufficient
    c = out.events[0]
    assert (c.pre.pts_sec, c.absent[0].pts_sec, c.absent[-1].pts_sec, c.post.pts_sec) == (1, 3, 6, 8)


@pytest.mark.parametrize("spec,reason", [
    ("yyn yyn yyn nyn nyn nyn nyn yyn yyy yyn", "no service before absence"),
    ("yyn yyy yyn nyn nyn nyn nyn yyn yyn yyn", "no service after absence"),
    ("yyn yyy yyn nyn nnn nyn nyn yyn yyy yyn", "client left during absence"),
    ("yyn yyy yyn nyn nyn ynn yyn yyy yyn yyn", "absence shorter than minimum"),
])
def test_missing_condition_or_counterexample_is_not_event(spec, reason):
    pad = " ".join(["yyn"] * 16)  # контекст 15 с виден полностью, не обрезан краями видео
    out = evaluate_cash01(tl(f"{pad} {spec} {pad}"))
    assert out.events == [] and out.insufficient == [], reason


@pytest.mark.parametrize("spec", [
    "yyn yyy yyn nyn uyn nyn nyn yyn yyy yyn",   # присутствие кассира неизвестно внутри отсутствия
    "yyn yyy yyn nyn nun nyn nyn yyn yyy yyn",   # присутствие клиента неизвестно
    "yyn yyy yyn nyn nyn nyn nyn",               # отсутствие обрезано концом видео
    "nyn nyn nyn yyn yyy yyn",                   # отсутствие обрезано началом видео
    "yyn yyu yyn nyn nyn nyn nyn yyn yyy yyn",   # обслуживание до ухода неизвестно
])
def test_unknown_is_insufficient_not_verdict(spec):
    out = evaluate_cash01(tl(spec))
    assert out.events == [] and len(out.insufficient) == 1


def test_no_service_with_context_cut_by_video_start_is_insufficient():
    # обслуживание могло быть до начала записи: не контрпример, а «не установлено»
    out = evaluate_cash01(tl("yyn yyn yyn nyn nyn nyn nyn yyn yyy yyn"))
    assert out.events == [] and len(out.insufficient) == 1


def test_clean_timeline_has_nothing():
    out = evaluate_cash01(tl("yyn yyy yyy yyn yyn yny ynn"))
    assert out.events == [] and out.insufficient == [] and out.absence_runs == 0


# --- анализатор в режиме cash01-facts-v1 -----------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(p)], check=True)
    return p


@pytest.fixture
def cfg():
    base = load_model_config(ROOT / "configs/model.yaml")
    return dataclasses.replace(base, prompt_version=facts_prompt.FACTS_PROMPT_VERSION, max_tokens=512)


def facts_responder(pattern: dict[int, str], default="yyn"):
    m = {"y": "yes", "n": "no", "u": "unknown"}

    def r(idx):
        frames = [{"index": i, "cashier_at_workplace": m[pattern.get(i, default)[0]],
                   "client_at_counter": m[pattern.get(i, default)[1]],
                   "service_in_progress": m[pattern.get(i, default)[2]]} for i in idx]
        return 200, json.dumps({"frames": frames}), "stop"
    return r


def run(cfg, responder, video, tmp_path, budget=60.0, cat=None):
    srv = FakeLlama(responder)
    try:
        a = Analyzer(cfg, cat or catalog(synthetic_rule(type_id=TYPE)), LlamaClient(srv.url), call_timeout_sec=10)
        job = tmp_path / "job"
        job.mkdir(exist_ok=True)
        return a.analyze_video(video, job, time.monotonic() + budget), job
    finally:
        srv.close()


def test_facts_mode_requires_exactly_cash01_rule(cfg):
    for cat in (catalog(), catalog(synthetic_rule()), catalog(synthetic_rule(type_id=TYPE),
                                                            synthetic_rule(rule_id="S2"))):
        with pytest.raises(AnalyzerNotReadyError):
            Analyzer(cfg, cat, LlamaClient("http://127.0.0.1:9"))


@needs_ffmpeg
def test_facts_positive_violation_with_three_real_frames(cfg, video, tmp_path):
    # 25 c, шаг 1 c: обслуживание на 9 c, кассира нет 12–15 c (окно 2 начинается с 10), обслуживание на 18 c.
    pat = {9: "yyy", 12: "nyn", 13: "nyn", 14: "nyn", 15: "nyn", 18: "yyy"}
    res, job = run(cfg, facts_responder(pat), video, tmp_path)
    assert res.verdict == "violation" and len(res.events) == 1
    ev = res.events[0]
    assert (ev.start_sec, ev.timecode_sec, ev.end_sec) == (9.0, 12.0, 18.0)
    assert [e.timestamp_sec for e in ev.evidence] == [9.0, 14.0, 18.0]
    assert "12.0–15.0 с" in ev.observed_fact and "9.0 с" in ev.observed_fact
    assert any("coverage: CASH-01 only" in x for x in res.limitations)
    cat = catalog(synthetic_rule(type_id=TYPE))
    validate_analysis_for_video(res, 25.0, job)
    validate_analysis_rules(res, cat)


@needs_ffmpeg
def test_facts_absence_across_window_boundary(cfg, video, tmp_path):
    pat = {6: "yyy", 8: "nyn", 9: "nyn", 10: "nyn", 11: "nyn", 14: "yyy"}  # окно 0/1 граница на 10 c
    res, _ = run(cfg, facts_responder(pat), video, tmp_path)
    assert res.verdict == "violation" and res.events[0].timecode_sec == 8.0


@needs_ffmpeg
def test_facts_clean_video_no_violation(cfg, video, tmp_path):
    res, _ = run(cfg, facts_responder({}), video, tmp_path)
    assert res.verdict == "no_violation" and res.events == []


@needs_ffmpeg
def test_facts_unknown_gives_unassessable(cfg, video, tmp_path):
    pat = {9: "yyy", 12: "nyn", 13: "uyn", 14: "nyn", 18: "yyy"}
    with pytest.raises(UnassessableVideoError):
        run(cfg, facts_responder(pat), video, tmp_path)


@needs_ffmpeg
def test_facts_counterexample_client_left_is_no_violation(cfg, video, tmp_path):
    pat = {9: "yyy", 12: "nnn", 13: "nnn", 14: "nnn", 18: "yyy"}
    res, _ = run(cfg, facts_responder(pat), video, tmp_path)
    assert res.verdict == "no_violation"
    assert any("excluded by counterexample" in x for x in res.limitations)


@needs_ffmpeg
@pytest.mark.parametrize("bad", ["wrong_index", "missing_frame", "bad_value"])
def test_facts_invalid_frames_are_rejected(cfg, video, tmp_path, bad):
    def r(idx):
        frames = [{"index": i, "cashier_at_workplace": "yes", "client_at_counter": "yes",
                   "service_in_progress": "no"} for i in idx]
        if bad == "wrong_index":
            frames[0]["index"] = 999
        elif bad == "missing_frame":
            frames.pop()
        else:
            frames[0]["cashier_at_workplace"] = "maybe"
        return 200, json.dumps({"frames": frames}), "stop"
    with pytest.raises(InvalidAnalysisResultError):
        run(cfg, r, video, tmp_path)


@needs_ffmpeg
def test_facts_deadline_expired(cfg, video, tmp_path):
    with pytest.raises(AnalysisTimeoutError):
        run(cfg, facts_responder({}), video, tmp_path, budget=-1)


def test_facts_schema_fits_grammar_limits():
    s = facts_prompt.schema([3, 4, 5])
    assert s["properties"]["frames"]["minItems"] == 3 == s["properties"]["frames"]["maxItems"]
    assert "maxLength" not in json.dumps(s)  # llama.cpp разворачивает ограниченные строки (bad_alloc)
