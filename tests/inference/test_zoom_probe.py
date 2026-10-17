"""zoom_probe: вырезка по рамке 0–1000 и сбор состояний с тестовым сервером. Синтетика."""
import json
import shutil
import subprocess

import pytest
from PIL import Image

from app.inference.experiments import zoom_probe
from app.inference.vlm import LlamaClient
from tests.inference.fakes import FakeLlama


def test_crop_scales_and_bounds(tmp_path):
    src = tmp_path / "f.jpg"
    Image.new("RGB", (464, 848), (10, 20, 30)).save(src)
    out = zoom_probe.crop(src, [100, 100, 300, 200], tmp_path / "c.jpg")
    with Image.open(out) as im:
        assert max(im.size) == 448
    with pytest.raises(ValueError):
        zoom_probe.crop(src, [300, 300, 100, 100], tmp_path / "bad.jpg")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")
def test_probe_video_locates_then_reads_states(tmp_path):
    video = tmp_path / "v.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=10:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)

    def r(idx):
        if not idx:  # запрос поиска рамки (без кадров с подписью "Frame")
            return 200, json.dumps({"found": True, "bbox": [100, 100, 400, 400]}), "stop"
        return 200, json.dumps({"crops": [{"index": i, "door_open": "yes" if i == 2 else "no",
                                           "key_in_lock": "no"} for i in idx]}), "stop"
    srv = FakeLlama(r)
    try:
        rec = zoom_probe.probe_video(LlamaClient(srv.url), {"id": "a", "path": str(video), "source_label": "x"},
                                     tmp_path / "out", 1.0, 4)
    finally:
        srv.close()
    assert rec["bbox"] == [100, 100, 400, 400] and len(rec["states"]) == rec["frames"]
    assert sum(1 for s in rec["states"] if s[2] == "yes") == 1
