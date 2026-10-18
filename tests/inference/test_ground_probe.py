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
