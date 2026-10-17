"""cash-events-v4: SAFE-01, ACCESS-01 (+ CASH-01/02 из rules_v2) и режим анализатора. Только синтетика."""
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import validate_analysis_for_video, validate_analysis_rules
from app.inference import Analyzer, LlamaClient, load_model_config
from app.inference import facts_v4, rules_v4
from app.inference.rules_v2 import Facts
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
M = {"y": "yes", "n": "no", "u": "unknown"}
KEYS = ("cashier_at_workplace", "client_at_counter", "outsider_staff_side", "safe_open", "keys_left")


def tl(spec: str) -> list[Facts]:
    """5 символов на кадр: cashier, client, outsider, safe_open, keys_left; действия — no."""
    out = []
    for i, s in enumerate(spec.split()):
        v = {k: M[c] for k, c in zip(KEYS, s)}
        v.update(collector_present="unknown", service_in_progress="no", cashier_counting_cash="no", bag_opened="no")
        out.append(Facts(i, float(i), v))
    return out


def of(cands, t):
    return [c for c in cands if c.type_id == t]


def test_safe01_leaving_at_end_with_open_safe_is_event():
    [c] = of(rules_v4.evaluate(tl("ynnnn ynnyn ynnyn nnnyn nnnyn nnnyn")), rules_v4.TYPE_SAFE01)
    assert c.decision == "event" and c.checks["safe_unsecured"].status == "confirmed"


def test_safe01_key_left_is_event_and_closed_safe_is_not():
    [k] = of(rules_v4.evaluate(tl("ynnnn ynnny nnnnn nnnnn nnnny")), rules_v4.TYPE_SAFE01)
    assert k.decision == "event"
    [closed] = of(rules_v4.evaluate(tl("ynnnn ynnnn nnnnn nnnnn nnnnn")), rules_v4.TYPE_SAFE01)
    assert closed.decision == "excluded"
    [unk] = of(rules_v4.evaluate(tl("ynnnn ynnun nnnun nnnun nnnun")), rules_v4.TYPE_SAFE01)
    assert unk.decision == "insufficient"


def test_access01_outsider_run():
    [c] = of(rules_v4.evaluate(tl("ynnnn ynynn ynynn ynnnn")), rules_v4.TYPE_ACCESS01)
    assert c.decision == "event" and [f.index for f in c.evidence] == [1, 2]
    assert of(rules_v4.evaluate(tl("ynnnn ynynn ynnnn")), rules_v4.TYPE_ACCESS01) == []


def test_v4_schema_and_rows():
    obj = {"presence": [{"index": i, "cashier": "yes", "client": "no", "outsider": "no", "safe_open": "yes",
                         "keys_left": "unknown"} for i in (3, 4)], "counting_frames": [4], "service_frames": []}
    assert facts_v4.validate(obj, [3, 4])
    rows = facts_v4.to_rows(obj)
    assert rows[0]["safe_open"] == "yes" and rows[1]["cashier_counting_cash"] == "yes"
    assert not facts_v4.validate(dict(obj, service_frames=[7]), [3, 4])
    assert "maxLength" not in json.dumps(facts_v4.schema([3, 4]))


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(p)], check=True)
    return p


@needs_ffmpeg
def test_v4_safe01_through_analyzer(video, tmp_path):
    cfg = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v4.PROMPT_VERSION,
                              max_tokens=900)
    cat = catalog(*[synthetic_rule(type_id=t, rule_id=f"S{i}") for i, t in enumerate(rules_v4.SUPPORTED_TYPES)])

    def r(idx):  # кассир уходит с 20 c, сейф открыт с 19 c до конца
        pres = [{"index": i, "cashier": "no" if i >= 20 else "yes", "client": "no", "outsider": "no",
                 "safe_open": "yes" if i >= 19 else "no", "keys_left": "no"} for i in idx]
        return 200, json.dumps({"presence": pres, "counting_frames": [], "service_frames": []}), "stop"
    srv = FakeLlama(r)
    try:
        job = tmp_path / "job"
        job.mkdir()
        res = Analyzer(cfg, cat, LlamaClient(srv.url), call_timeout_sec=10).analyze_video(video, job,
                                                                                          time.monotonic() + 60)
    finally:
        srv.close()
    assert res.verdict == "violation" and [e.type_id for e in res.events] == [rules_v4.TYPE_SAFE01]
    assert res.events[0].timecode_sec == 20.0 and "сейф открыт" in res.events[0].observed_fact
    validate_analysis_for_video(res, 25.0, job)
    validate_analysis_rules(res, cat)
