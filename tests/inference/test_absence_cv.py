"""experiments.absence_cv: зона движения и серии «зона пуста»."""
from app.inference.experiments import absence_cv as cv

N = cv.SIZE * cv.SIZE


def frame(t, moving):
    """Фон 100; в левом верхнем углу (ячейка 0) «кассир» мигает, пока moving."""
    px = bytearray([100]) * N
    if moving:
        for y in range(8):
            for x in range(8):
                px[y * cv.SIZE + x] = 200 if t % 2 else 20
    return bytes(px)


def test_absence_run_found_in_motion_zone():
    pattern = [True] * 8 + [False] * 5 + [True] * 8
    res = cv.analyse([frame(t, m) for t, m in enumerate(pattern)])
    assert res["zone"][0] == 0
    assert res["runs"] == [[9, 12]]  # первая секунда без движения ещё отличается от предыдущей


def test_vlm_absent_mapping():
    tr = {"fields": ["cashier_at_workplace", "others_count"], "timeline": [[0, 0.0, "yes", "1"], [1, 1.02, "no", "0"],
                                                                         [2, 2.0, "unknown", "0"]]}
    assert cv.vlm_absent(tr) == {0: False, 1: True}
