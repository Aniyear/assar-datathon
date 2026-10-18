"""experiments.ground_probe: признаки по кадрам и сравнение типов с эталонами (без модели)."""
from app.inference.experiments import ground_probe as gp


def fr(cashier=1, others=0, crop=None, ok=True):
    return {"t": 0.0, "ok": ok, "count": {"cashier": cashier, "other_person": others}, "crop": crop or {}}


def test_features_and_compare():
    lap_on = {"laptop": {"screen_on": "yes", "lid_open": "yes"}}
    viol = gp.features([fr(), fr(cashier=0, crop=lap_on), fr(cashier=0, crop=lap_on), fr(ok=False)])
    assert viol["cashier_absent"] == round(2 / 3, 3) and viol["laptop_on_while_absent"] == 1.0
    assert viol["frames_ok"] == 3 and viol["frames"] == 4
    ref = gp.features([fr(), fr(cashier=0), fr(others=2)])
    assert ref["laptop_on_while_absent"] == 0.0 and ref["two_plus_others"] == round(1 / 3, 3)
    rep = gp.compare({"A": viol, "B": ref}, {"A": {"label": "violation", "type": "laptop_not_locked"},
                                             "B": {"label": "no_violation", "type": ""}})
    t = rep["type=laptop_not_locked"]
    assert t["laptop_on_while_absent"] == {"mean": 1.0, "above_all_references": 1}
    assert t["two_plus_others"]["above_all_references"] == 0
    assert rep["reference"]["videos"] == 1


def test_bbox_validation_and_schema():
    assert gp._valid([1, 2, 3, 4]) and not gp._valid([3, 2, 1, 4]) and not gp._valid([0, 0, 1, 1001])
    s = gp.ground_schema()
    assert s["properties"]["objects"]["items"]["properties"]["label"]["enum"] == gp.LABELS
    assert set(gp.crop_schema(gp.CROP_Q["safe"])["required"]) == {"door_open", "key_in_lock"}


def test_added_value_over_v6():
    f = {"A": {"bag": 0.1}, "B": {"bag": 0.3}, "C": {"bag": 0.2}, "D": {"bag": 0.0}, "E": {"bag": 0.3}}
    meta = {"A": {"role": "train", "label": "violation"}, "B": {"role": "train", "label": "violation"},
            "C": {"role": "train", "label": "no_violation"}, "D": {"role": "dev", "label": "violation"},
            "E": {"role": "dev", "label": "no_violation"}}
    [r] = gp.added_value(f, meta, {"A": "violation"}, max_fp=0)
    assert r["threshold"] == 0.3 and r["train"] == {"tp": 1, "fp": 0} and r["dev"] == {"tp": 0, "fp": 1}
    [r1] = gp.added_value(f, meta, {}, max_fp=1)
    assert r1["threshold"] == 0.1 and r1["train"] == {"tp": 2, "fp": 1}
