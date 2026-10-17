"""cash-facts-v2: CASH-01/02, COLLECT-02 по покадровым фактам и режим анализатора. Только синтетика."""
import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import UnassessableVideoError, validate_analysis_for_video, validate_analysis_rules
from app.inference import Analyzer, AnalyzerNotReadyError, LlamaClient, facts_v2, load_model_config, rules_v2
from app.inference.rules_v2 import FIELDS, Facts, evaluate
from tests.inference.fakes import FakeLlama, catalog, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
M = {"y": "yes", "n": "no", "u": "unknown"}
# символы по порядку FIELDS: cashier, client, collector, service, counting, bag
BASE = "yynnnn"   # кассир на месте, клиента нет, ничего не происходит


def tl(spec: str) -> list[Facts]:
    return [Facts(i, float(i), {k: M[c] for k, c in zip(FIELDS, s)}) for i, s in enumerate(spec.split())]


def pad(n=16, s="ynnnnn"):
    return " ".join([s] * n)


def one(cands, t):
    xs = [c for c in cands if c.type_id == t]
    assert len(xs) == 1, xs
    return xs[0]


def test_cash02_counting_after_client_left_is_event():
    c = one(evaluate(tl(f"{pad()} yyny nn yynynn ynnnyn ynnnyn ynnnyn {pad()}".replace("yyny nn", "yynynn"))),
            rules_v2.TYPE_CASH02)
    assert c.decision == "event" and c.checks["client_before_counting"].status == "confirmed"
    assert [f.index for f in c.evidence][0] == 17  # кадр, где клиент ещё был


@pytest.mark.parametrize("spec,expect", [
    ("yyynyn yyynyn yyynyn", "excluded"),        # пересчёт при клиенте — норма (+ инкассатор)
    ("yynnyn yynnyn yynnyn", "excluded"),        # пересчёт при клиенте
    ("ynnnyn ynnnyn ynnnyn", "excluded"),        # клиента в контексте не было — внутренняя операция
    ("yynynn yunnyn yunnyn ynnnyn", "insufficient"),  # клиент был, но во время пересчёта неизвестен
])
def test_cash02_counterexamples_and_unknown(spec, expect):
    assert one(evaluate(tl(f"{pad()} {spec} {pad()}")), rules_v2.TYPE_CASH02).decision == expect


def test_cash02_without_client_in_context_is_not_event():
    # dev: кассир один пересчитывает деньги — нормальное действие, не событие (даже при обрезанном контексте)
    assert one(evaluate(tl("ynnnyn ynnnyn ynnnyn ynnnnn")), rules_v2.TYPE_CASH02).decision == "insufficient"


def test_cash01_context_cut_by_start_still_event():
    c = one(evaluate(tl("nynnnn nynnnn nynnnn yynynn yynnnn")), rules_v2.TYPE_CASH01)
    assert c.decision == "event" and not c.context_complete  # FAQ 16: видимый уход не пропускаем


def test_collect02_opened_after_collector_left_is_event_and_with_collector_excluded():
    ok = one(evaluate(tl(f"{pad()} ynynnn ynnnnn ynnnny ynnnny {pad()}")), rules_v2.TYPE_COLLECT02)
    assert ok.decision == "event" and ok.checks["collector_before_opening"].status == "confirmed"
    bad = one(evaluate(tl(f"{pad()} ynynny ynynny {pad()}")), rules_v2.TYPE_COLLECT02)
    assert bad.decision == "excluded"


def test_cash01_v2_core_and_counterexample():
    ev = one(evaluate(tl(f"{pad(16, 'yynnnn')} yynynn nynnnn nynnnn nynnnn yynynn {pad(16, 'yynnnn')}")),
             rules_v2.TYPE_CASH01)
    assert ev.decision == "event" and ev.context_complete
    gone = one(evaluate(tl(f"{pad()} yynynn nnnnnn nnnnnn nnnnnn yynynn {pad()}")), rules_v2.TYPE_CASH01)
    assert gone.decision == "excluded"


def test_short_actions_ignored():
    assert evaluate(tl(f"{pad()} ynnnyn {pad()} ynnnny {pad()}")) == []


# --- анализатор ---------------------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(p)], check=True)
    return p


@pytest.fixture
def cfg():
    return dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v2.PROMPT_VERSION,
                               max_tokens=1024)


def cat3():
    return catalog(*[synthetic_rule(type_id=t, rule_id=f"S-{i}") for i, t in enumerate(rules_v2.SUPPORTED_TYPES)])


def responder(pattern: dict[int, str], default="ynnnnn"):
    def r(idx):
        return 200, json.dumps({"frames": [{"index": i, **{k: M[c] for k, c in zip(FIELDS, pattern.get(i, default))}}
                                           for i in idx]}), "stop"
    return r


def run(cfg, resp, video, tmp_path, cat=None):
    srv = FakeLlama(resp)
    try:
        job = tmp_path / "job"
        job.mkdir(exist_ok=True)
        a = Analyzer(cfg, cat or cat3(), LlamaClient(srv.url), call_timeout_sec=10)
        return a.analyze_video(video, job, time.monotonic() + 60), job, a
    finally:
        srv.close()


def test_v2_requires_supported_verified_types(cfg):
    with pytest.raises(AnalyzerNotReadyError):
        Analyzer(cfg, catalog(), LlamaClient("http://127.0.0.1:9"))
    with pytest.raises(AnalyzerNotReadyError):
        Analyzer(cfg, catalog(synthetic_rule(type_id="cash_bag_handover")), LlamaClient("http://127.0.0.1:9"))


@needs_ffmpeg
def test_v2_cash02_event_through_analyzer(cfg, video, tmp_path):
    pat = {8: "yynynn", 10: "ynnnyn", 11: "ynnnyn", 12: "ynnnyn"}  # клиент на 8 c, пересчёт без клиента 10–12 c
    res, job, a = run(cfg, responder(pat), video, tmp_path)
    assert res.verdict == "violation" and [e.type_id for e in res.events] == [rules_v2.TYPE_CASH02]
    ev = res.events[0]
    assert ev.timecode_sec == 10.0 and [e.timestamp_sec for e in ev.evidence] == [8.0, 10.0, 12.0]
    assert "10.0–12.0 с" in ev.observed_fact
    validate_analysis_for_video(res, 25.0, job)
    validate_analysis_rules(res, cat3())
    assert a.last_trace["candidates"][0]["type_id"] == rules_v2.TYPE_CASH02


@needs_ffmpeg
def test_v2_insufficient_is_no_violation_not_failed(cfg, video, tmp_path):
    pat = {10: "yunnyn", 11: "yunnyn", 12: "ynnnyn"}  # клиент неизвестен при пересчёте → не установлено
    res, _, _ = run(cfg, responder(pat), video, tmp_path)
    assert res.verdict == "no_violation" and any("not established" in x for x in res.limitations)


@needs_ffmpeg
def test_v2_mostly_unknown_is_unassessable(cfg, video, tmp_path):
    with pytest.raises(UnassessableVideoError):
        run(cfg, responder({}, default="uunnnn"), video, tmp_path)


@needs_ffmpeg
def test_v2_event_of_type_without_rule_is_not_reported(cfg, video, tmp_path):
    pat = {8: "yynynn", 10: "ynnnyn", 11: "ynnnyn", 12: "ynnnyn"}
    res, _, _ = run(cfg, responder(pat), video, tmp_path, cat=catalog(synthetic_rule(type_id=rules_v2.TYPE_CASH01)))
    assert res.verdict == "no_violation" and any("without a verified rule" in x for x in res.limitations)


# --- cash-events-v3: действия списком кадров ------------------------------------------------------------
from app.inference import facts_v3  # noqa: E402


def test_v3_schema_validate_and_rows():
    obj = {"presence": [{"index": i, "cashier": "yes", "client": "no", "collector": "unknown"} for i in (3, 4)],
           "counting_frames": [4], "bag_open_frames": [], "service_frames": [3]}
    assert facts_v3.validate(obj, [3, 4])
    rows = facts_v3.to_rows(obj)
    assert rows[1]["cashier_counting_cash"] == "yes" and rows[0]["cashier_counting_cash"] == "no"
    assert rows[0]["service_in_progress"] == "yes" and rows[0]["collector_present"] == "unknown"
    assert not facts_v3.validate(dict(obj, counting_frames=[9]), [3, 4])
    assert not facts_v3.validate(dict(obj, presence=obj["presence"][:1]), [3, 4])
    assert "maxLength" not in json.dumps(facts_v3.schema([3, 4]))


@needs_ffmpeg
def test_v3_cash02_event_through_analyzer(video, tmp_path):
    cfg3 = dataclasses.replace(load_model_config(ROOT / "configs/model.yaml"), prompt_version=facts_v3.PROMPT_VERSION,
                               max_tokens=512)

    def r(idx):
        pres = [{"index": i, "cashier": "yes", "client": "yes" if i == 8 else "no", "collector": "no"} for i in idx]
        return 200, json.dumps({"presence": pres, "counting_frames": [i for i in idx if 10 <= i <= 12],
                                "bag_open_frames": [], "service_frames": [i for i in idx if i == 8]}), "stop"
    res, _, _ = run(cfg3, r, video, tmp_path)
    assert res.verdict == "violation" and [e.type_id for e in res.events] == [rules_v2.TYPE_CASH02]
    assert res.events[0].timecode_sec == 10.0
