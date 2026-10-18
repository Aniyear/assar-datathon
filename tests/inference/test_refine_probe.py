"""refine_probe: отрезки, проверка постороннего с тестовым сервером, счёт движения. Синтетика."""
import json
import shutil
import subprocess

import pytest
from PIL import Image

from app.inference.experiments import refine_probe
from app.inference.vlm import LlamaClient
from tests.inference.fakes import FakeLlama

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def test_runs():
    assert refine_probe._runs(["no", "yes", "yes", "no", "yes"]) == [(1, 2), (4, 4)]


def test_access_verifies_runs(tmp_path):
    run = tmp_path / "run"
    fr = run / "jobs" / "a" / "frames"
    fr.mkdir(parents=True)
    for i in range(6):
        Image.new("RGB", (32, 24)).save(fr / f"e_{i + 1:05d}.jpg")
    tl = [[i, float(i)] + ["yes", "no", "yes" if 1 <= i <= 4 else "no"] + ["no"] * 6 for i in range(6)]
    res = [{"id": "a", "source_label": "violation", "trace": {"fields": ["cashier_at_workplace", "client_at_counter",
            "outsider_staff_side", "safe_open", "keys_left", "collector_present", "service_in_progress",
            "cashier_counting_cash", "bag_opened"], "timeline": tl}}]
    (run / "results.json").write_text(json.dumps(res), encoding="utf-8")
    srv = FakeLlama(lambda idx: (200, json.dumps({"frames": [{"index": i, "other_staff_side": "yes" if i != 4 else "no"}
                                                            for i in idx]}), "stop"))
    try:
        out = refine_probe.access(run, LlamaClient(srv.url))
    finally:
        srv.close()
    assert out[0]["runs"] == [{"len": 4, "checked": 3, "verified_yes": 2}]


@needs_ffmpeg
def test_motion_counts_activity(tmp_path):
    video = tmp_path / "v.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=10:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    zoom = tmp_path / "zoom.json"
    zoom.write_text(json.dumps([{"id": "a", "bbox": [0, 0, 1000, 1000]}, {"id": "b", "bbox": None}]), encoding="utf-8")
    out = refine_probe.motion([{"id": "a", "path": str(video), "source_label": "x"},
                               {"id": "b", "path": str(video), "source_label": "y"}], zoom, 4.0, 5.0, 0.5)
    assert out[0]["safe_found"] and out[0]["active_sec"] > 0 and out[1]["safe_found"] is False
