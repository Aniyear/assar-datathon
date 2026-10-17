"""discover: окно вокруг таймкода, страница выбора типа и подсчёт. Синтетическое видео и тестовый сервер."""
import json
import shutil
import subprocess

import pytest

from app.inference.experiments import discover
from app.inference.vlm import LlamaClient
from tests.inference.fakes import FakeLlama

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def test_run_page_count(tmp_path):
    video = tmp_path / "v.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    seen = []
    srv = FakeLlama(lambda idx: (seen.append(idx), (200, "SYNTH-TEXT", "stop"))[1])
    try:
        videos = [{"id": "a", "path": str(video), "role": "dev", "source_label": "violation", "source_timecode_sec": 20.0},
                  {"id": "b", "path": str(video), "role": "dev", "source_label": "no_violation", "source_timecode_sec": None}]
        out = tmp_path / "run"
        recs = discover.run(videos, LlamaClient(srv.url), out, 6.0, 1.0, 100, 10, None, False)
    finally:
        srv.close()
    assert [r["id"] for r in recs] == ["a"]  # отрицательные — только с --include-negatives
    pts = [f[1] for f in recs[0]["frames"]]
    assert min(pts) >= 14.0 - 1e-6 and max(pts) <= 26.0 + 1e-6 and len(seen[0]) == len(pts)
    (out / "discover.json").write_text(json.dumps(recs), encoding="utf-8")
    html_page = discover.page(recs, out).read_text(encoding="utf-8")
    assert "CASH-02" in html_page and "COLLECT-01" in html_page
    (out / "x.json").write_text(json.dumps({"run_id": "run", "items": {"type|a": "CASH-03"}}), encoding="utf-8")
    c = discover.count(out, out / "x.json")
    assert c == {"videos": 1, "types": {"CASH-03": 1}, "model_status": {"ok": 1}}
