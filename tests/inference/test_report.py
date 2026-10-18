"""experiments.report: метрики по ролям, пересчёт вердикта по trace, покрытие, устойчивость, пороги."""
import json
import sys

from app.inference import rules_v6
from app.inference.experiments import report
from app.inference.rules_v2 import Facts

M = {"y": "yes", "n": "no", "u": "unknown"}
C = {"0": "0", "1": "1", "2": "2+"}


def trace(spec, verified="confirmed", dense=None):
    tl = [Facts(i, float(i), {"cashier_at_workplace": M[s[0]], "others_count": C[s[1]], "desk_valuables": M[s[2]]})
          for i, s in enumerate(spec.split())]
    cands = rules_v6.evaluate(tl)
    tr = rules_v6.trace(tl, cands)
    for c in tr["candidates"]:
        if c["type_id"] == rules_v6.TYPE_MULTI01 and c["decision"] == "event":
            c["checks"]["multi_verified"] = {"status": verified, "frames": [], "note": ""}
            if verified != "confirmed":
                c["decision"] = "excluded"
    tr["dense"] = dense or []
    return tr


def rec(vid, role, label, outcome, tr=None, types=()):
    return {"id": vid, "role": role, "source_label": label, "outcome": outcome, "sec": 10.0, "trace": tr,
            "result": {"events": [{"type_id": t} for t in types]}}


def test_metrics_counts_failed_separately():
    m = report.metrics([("violation", "violation"), ("violation", "no_violation"), ("no_violation", "violation"),
                        ("no_violation", "no_violation"), ("violation", "failed:X")])
    assert (m["TP"], m["FN"], m["FP"], m["TN"], m["failed"]) == (1, 1, 1, 1, 1)
    assert m["recall"] == 0.5 and m["precision"] == 0.5 and m["f1"] == 0.5


def test_recompute_matches_and_dense_whatif():
    multi = "y1n y2n y2n y2n y1n"
    assert report.recompute(trace(multi))[0] == "violation"
    assert report.recompute(trace(multi, verified="refuted"))[0] == "no_violation"
    assert report.recompute(trace(multi), require_dense=True)[0] == "no_violation"
    ok = trace(multi, dense=[{"start_sec": 1.0, "status": "confirmed"}])
    assert report.recompute(ok, require_dense=True)[0] == "violation"
    assert report.recompute(trace("y0y n0y n0y n0y"))[0] == "violation"  # CASH-03 без проверки
    with report._with_params(MIN_MULTI_FRAMES=4):
        assert report.recompute(trace(multi))[0] == "no_violation"
    assert rules_v6.MIN_MULTI_FRAMES == 2


def test_main_report(tmp_path, monkeypatch, capsys):
    multi, quiet = "y1n y2n y2n y2n y1n", "y1n y1n y1n y1n"
    rs = [rec("X-1", "train", "violation", "violation", trace(multi), [rules_v6.TYPE_MULTI01]),
          rec("X-2", "train", "violation", "no_violation", trace(quiet)),
          rec("X-3", "train", "no_violation", "no_violation", trace(quiet)),
          rec("X-4", "dev", "violation", "no_violation", trace(quiet)),
          rec("X-4~hflip", "dev", "violation", "violation", trace(multi)),
          rec("X-5", "dev", "no_violation", "no_violation", trace(quiet))]
    p = tmp_path / "results.json"
    p.write_text(json.dumps(rs), encoding="utf-8")
    t = tmp_path / "types.csv"
    t.write_text("id,type_id\nX-1,two_clients_at_once\nX-2,key_left_in_safe\nX-4,valuables_left_unattended\n",
                 encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["report", "--results", str(p), "--types", str(t), "--tune"])
    assert report.main() == 0
    out = json.loads((tmp_path / "report.local.json").read_text(encoding="utf-8"))["report"]
    assert out["metrics"]["train"]["TP"] == 1 and out["metrics"]["dev"]["FN"] == 1
    assert out["metrics"]["all"]["videos"] == 5
    assert out["coverage"]["role=train"] == {"covered_found": 1, "uncovered_missed_coverage": 1}
    assert out["coverage"]["role=dev"] == {"covered_missed_recognition": 1}
    assert out["stability"]["hflip"]["pairs"] == 1 and out["stability"]["hflip"]["same_verdict"] == 0
    assert out["tune"]["selected_on"] == "train"
