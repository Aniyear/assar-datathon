"""Структура условий CASH-01, агрегаты dev_eval и страница/подсчёт проверки человеком. Только синтетика."""
import json
from pathlib import Path

from app.inference.conditions import CONDITIONS, FrameFacts, evaluate_cash01, trace
from app.inference.experiments import condition_review, dev_eval
from tests.inference.test_conditions import POSITIVE, tl


def test_candidate_checks_are_structured_with_frames():
    out = evaluate_cash01(tl(POSITIVE))
    c = out.candidates[0]
    assert list(c.checks) == list(CONDITIONS)
    assert all(v.status == "confirmed" for v in c.checks.values())
    assert c.checks["cashier_absent"].frames == [3, 4, 5, 6]
    assert c.checks["service_before"].frames[-1] == 1 and c.checks["service_after"].frames[-1] == 8
    t = trace(tl(POSITIVE), out)
    assert t["candidates"][0]["decision"] == "event" and len(t["timeline"]) == 10


def test_refuted_and_not_established_are_distinguished():
    pad = " ".join(["yyn"] * 16)
    excl = evaluate_cash01(tl(f"{pad} yyn yyy yyn nyn nnn nyn nyn yyn yyy yyn {pad}")).candidates[0]
    assert excl.checks["client_present_during_absence"].status == "refuted" and excl.decision == "excluded"
    unk = evaluate_cash01(tl(f"{pad} yyn yyy yyn nyn nun nyn nyn yyn yyy yyn {pad}")).candidates[0]
    assert unk.checks["client_present_during_absence"].status == "not_established" and unk.decision == "insufficient"


def _fake_run(tmp_path: Path) -> Path:
    run = tmp_path / "20261017-230000"
    pos = trace(tl(POSITIVE), evaluate_cash01(tl(POSITIVE)))
    clean = trace(tl("yyn yyy yyn"), evaluate_cash01(tl("yyn yyy yyn")))
    for lab, outcomes in {"2b-q8": ("violation", "no_violation"), "4b-q4km": ("no_violation", "failed:UnassessableVideoError")}.items():
        (run / lab).mkdir(parents=True)
        res = [{"id": "dev-a", "role": "dev", "source_label": "violation", "outcome": outcomes[0],
                "result": {"events": [{}] if outcomes[0] == "violation" else []}, "trace": pos, "sec": 10.0},
               {"id": "dev-b", "role": "dev", "source_label": "no_violation", "outcome": outcomes[1],
                "result": {"events": []}, "trace": clean, "sec": 5.0}]
        (run / lab / "results.json").write_text(json.dumps(res), encoding="utf-8")
    return run


def test_dev_eval_aggregate_counts_failed_separately(tmp_path):
    run = _fake_run(tmp_path)
    agg = dev_eval.aggregate(json.loads((run / "4b-q4km" / "results.json").read_text(encoding="utf-8")))
    g = agg["groups"]["all"]
    assert g["videos"] == 2 and g["failed"] == 1 and g["failed_share"] == 0.5 and g["label_agreement"] == 0
    assert g["candidates"]["event"] == 1 and g["frames"] == 13


def test_condition_review_build_and_score(tmp_path):
    run = _fake_run(tmp_path)
    page = condition_review.build(run, seed=0).read_text(encoding="utf-8")
    assert "Эталон" in page and "Модель 1" in page
    assert "2b-q8" not in page and "4b-q4km" not in page  # имя модели скрыто (подписи, поля, пути)
    m = {lab: a for a, lab in json.loads((run / "model_key.json").read_text(encoding="utf-8"))["key"].items()}["2b-q8"]
    export = {"run_id": run.name, "reviewer": "t", "items": {
        "ref|dev-a": "yes", "ref|dev-b": "no",
        f"event|dev-a|{m}|0": "correct",
        f"cond|dev-a|{m}|0|cashier_absent": "right", f"cond|dev-a|{m}|0|service_before": "wrong"}}
    (run / "x.json").write_text(json.dumps(export), encoding="utf-8")
    s = condition_review.score(run, run / "x.json")
    a, b = s["variants"]["2b-q8"], s["variants"]["4b-q4km"]
    assert (a["tp"], a["fp"], a["fn"], a["tn"], a["failed_total"]) == (1, 0, 0, 1, 0)
    assert (b["tp"], b["fn"], b["failed_ref_no"]) == (0, 1, 1)
    assert a["events_correct"] == 1 and a["conditions"]["service_before"] == {"right": 0, "wrong": 1, "unclear": 0}
