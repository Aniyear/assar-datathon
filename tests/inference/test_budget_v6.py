"""v6: укладка в дедлайн, плотная перепроверка MULTI-01, рамки на доказательном кадре, выборка отрезка."""
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import validate_analysis_for_video
from app.inference import Analyzer, LlamaClient, analyzer as an, facts_v6, load_model_config, rules_v6
from app.inference.video import probe, sample_frames, sample_range
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


@pytest.fixture(autouse=True)
def _dense_on(monkeypatch):
    monkeypatch.setattr(an, "DENSE_CHECK", True)  # выключено в production; здесь проверяется сама логика


def _video(tmp_path, duration=25):
    video = tmp_path / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=160x284:rate=30:duration={duration}",
                    "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    return video


def test_budget_skips_desk_then_thins():
    def plan(remaining):
        b = an._Budget(time.monotonic() + remaining)
        b.note(10, 2.0)  # 0.25 с на кадр с запасом 1.25
        b.plan([[_F(10.0)] * 10, [_F(20.0)] * 10], n_passes=2)
        return b.skip_desk_from, b.stride
    assert plan(30) == (None, 1)
    assert plan(20) == (10.0, 1)
    assert plan(16) == (10.0, 3)


class _F:
    def __init__(self, pts):
        self.pts_sec = pts


@needs_ffmpeg
def test_sample_range_is_half_step_offset(tmp_path):
    video = _video(tmp_path)
    info = probe(video)
    full = sample_frames(video, tmp_path / "f", 1.0, info)
    rng = sample_range(video, tmp_path / "r", 0.5, info, 9.5, 14.0, 10, 1000)
    assert [f.index for f in rng][:2] == [1000, 1001]
    assert [round(f.pts_sec, 2) for f in rng] == [9.5, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0]
    assert {round(f.pts_sec, 2) for f in full} >= {10.0, 11.0, 12.0, 13.0, 14.0}


def _multi_run(tmp_path, boxes, dense_others, in_core=False, monkeypatch=None):
    video = _video(tmp_path)
    cfg = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v6.PROMPT_VERSION,
                              max_tokens=500)
    cat = catalog(synthetic_rule(type_id=rules_v6.TYPE_MULTI01, rule_id="M1"))
    n = {"main": 0}

    def r(idx):
        if idx and idx[0] >= an.DENSE_INDEX_BASE:
            return 200, json.dumps({"frames": [{"index": i, "cashier": "yes", "others": dense_others} for i in idx]}), "stop"
        if len(idx) == 1:
            return 200, json.dumps({"people": [{"bbox_2d": b} for b in boxes]}), "stop"
        if n["main"] >= 6:  # узкая проверка по трём кадрам
            return 200, json.dumps({"frames": [{"index": i, "two_others_inside": "yes"} for i in idx]}), "stop"
        k = n["main"] % 2
        n["main"] += 1
        if k == 0:
            body = {"frames": [{"index": i, "cashier": "yes", "others": "2+" if 5 <= i <= 9 else "1"} for i in idx]}
        else:
            body = {"frames": [{"index": i, "desk_valuables": "no"} for i in idx]}
        return 200, json.dumps(body), "stop"
    srv = FakeLlama(r)
    try:
        job = tmp_path / "job"
        job.mkdir()
        a = Analyzer(cfg, cat, LlamaClient(srv.url), call_timeout_sec=10)
        res = a.analyze_video(video, job, time.monotonic() + 60)
    finally:
        srv.close()
    return a, res, job


@needs_ffmpeg
def test_multi_dense_and_boxes(tmp_path):
    a, res, job = _multi_run(tmp_path, [[100, 200, 300, 900], [500, 200, 700, 900]], "2+")
    assert res.verdict == "violation"
    [ev] = res.events
    paths = [Path(e.local_jpeg_path) for e in ev.evidence]
    assert paths[len(paths) // 2].parent == (job / "annotated").resolve()
    assert all(p.parent.name == "frames" for i, p in enumerate(paths) if i != len(paths) // 2)
    assert any("model-drawn boxes" in x for x in res.limitations)
    [d] = a.last_trace["dense"]
    assert d["status"] == "confirmed" and d["longest_run"] >= an.DENSE_MIN_RUN
    validate_analysis_for_video(res, 25.0, job)


@needs_ffmpeg
def test_dense_refuted_is_record_only_and_one_box_not_drawn(tmp_path):
    a, res, job = _multi_run(tmp_path, [[100, 200, 300, 900]], "1")
    assert res.verdict == "violation"  # MULTI_DENSE_IN_CORE = False: только запись в trace
    assert a.last_trace["dense"][0]["status"] == "refuted"
    assert all(Path(e.local_jpeg_path).parent.name == "frames" for e in res.events[0].evidence)
    assert not any("model-drawn boxes" in x for x in res.limitations)


@needs_ffmpeg
def test_dense_in_core_excludes(tmp_path, monkeypatch):
    monkeypatch.setattr(an, "MULTI_DENSE_IN_CORE", True)
    _, res, _ = _multi_run(tmp_path, [], "1")
    assert res.verdict == "no_violation"


def test_box_validation():
    assert facts_v6.boxes({"people": [{"bbox_2d": [1, 2, 3, 4]}]}) == [(1, 2, 3, 4)]
    assert facts_v6.boxes({"people": [{"bbox_2d": [1, 2, 3, 1001]}]}) is None
    assert facts_v6.boxes({"people": [{"bbox_2d": [5, 2, 3, 4]}]}) == []
    assert facts_v6.boxes({"x": 1}) is None
