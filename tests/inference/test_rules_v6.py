"""cash-specialists-v6: CASH-03 (ценности на столе при уходе), MULTI-01 (два клиента) и режим анализатора."""
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import validate_analysis_for_video, validate_analysis_rules
from app.inference import Analyzer, LlamaClient, load_model_config
from app.inference import facts_v6, rules_v6
from app.inference.rules_v2 import Facts
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
C = {"0": "0", "1": "1", "2": "2+", "u": "unknown"}
M = {"y": "yes", "n": "no", "u": "unknown"}


def tl(spec):
    """3 символа на кадр: cashier (y/n/u), others (0/1/2/u), desk (y/n/u)."""
    return [Facts(i, float(i), {"cashier_at_workplace": M[s[0]], "others_count": C[s[1]], "desk_valuables": M[s[2]]})
            for i, s in enumerate(spec.split())]


def of(cands, t):
    return [c for c in cands if c.type_id == t]


def test_cash03_left_with_cash_on_desk_and_clear_desk():
    [ev] = of(rules_v6.evaluate(tl("y0y y0y n0y n0y n0y")), rules_v6.TYPE_CASH03)
    assert ev.decision == "event"
    [ok] = of(rules_v6.evaluate(tl("y0y y0n n0n n0n n0n")), rules_v6.TYPE_CASH03)
    assert ok.decision == "excluded"
    [unk] = of(rules_v6.evaluate(tl("y0y n0u n0u n0y")), rules_v6.TYPE_CASH03)
    assert unk.decision == "insufficient"


def test_multi01_two_others_with_cashier():
    [ev] = of(rules_v6.evaluate(tl("y1n y2n y2n y1n")), rules_v6.TYPE_MULTI01)
    assert ev.decision == "event"
    assert of(rules_v6.evaluate(tl("y1n y2n y1n")), rules_v6.TYPE_MULTI01) == []
    [no_cashier] = of(rules_v6.evaluate(tl("n2n n2n")), rules_v6.TYPE_MULTI01)
    assert no_cashier.decision == "excluded"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")
def test_v6_through_analyzer(tmp_path):
    video = tmp_path / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    cfg = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v6.PROMPT_VERSION,
                              max_tokens=500)
    cat = catalog(*[synthetic_rule(type_id=t, rule_id=f"S{i}") for i, t in enumerate(rules_v6.SUPPORTED_TYPES)])
    n = {"k": 0}

    def r(idx):
        k = n["k"] % 2
        n["k"] += 1
        if k == 0:
            body = {"frames": [{"index": i, "cashier": "no" if i >= 20 else "yes", "others": "0"} for i in idx]}
        else:
            body = {"frames": [{"index": i, "desk_valuables": "yes"} for i in idx]}
        return 200, json.dumps(body), "stop"
    srv = FakeLlama(r)
    try:
        job = tmp_path / "job"
        job.mkdir()
        res = Analyzer(cfg, cat, LlamaClient(srv.url), call_timeout_sec=10).analyze_video(video, job,
                                                                                          time.monotonic() + 60)
    finally:
        srv.close()
    assert res.verdict == "violation" and [e.type_id for e in res.events] == [rules_v6.TYPE_CASH03]
    assert res.events[0].timecode_sec == 20.0
    validate_analysis_for_video(res, 25.0, job)
    validate_analysis_rules(res, cat)



@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")
@pytest.mark.parametrize("answers,expect", [(["yes", "yes", "yes"], "violation"), (["yes", "no", "yes"], "no_violation")])
def test_v6_multi_verification(tmp_path, answers, expect):
    video = tmp_path / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    cfg = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v6.PROMPT_VERSION,
                              max_tokens=500)
    cat = catalog(synthetic_rule(type_id=rules_v6.TYPE_MULTI01, rule_id="M1"))
    n = {"k": 0}

    def r(idx):
        if len(idx) == 3 and 5 <= idx[0] and idx[-1] <= 9:  # запрос проверки
            return 200, json.dumps({"frames": [{"index": i, "two_others_inside": a} for i, a in zip(idx, answers)]}), "stop"
        k = n["k"] % 2
        n["k"] += 1
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
    assert res.verdict == expect
