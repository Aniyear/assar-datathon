"""Эксперимент сравнения моделей на CPU: синтетическое видео, тестовые loopback-серверы вместо llama-server."""
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.inference import prompts
from app.inference.experiments import compare, fetch, review, score
from app.inference.vlm import LlamaClient
from tests.inference.fakes import FakeLlama, ok

ROOT = Path(__file__).resolve().parents[2]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")
SECRET = "SYNTHETIC-DESCRIPTION-MUST-NOT-LEAK"


def test_manifest_is_controlled_comparison():
    m = fetch.load_manifest()
    cfg = json.loads((ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
    s = m["shared"]
    assert m["prompt_version"] == prompts.PROMPT_VERSION == cfg["generation"]["prompt_version"]
    for k in ("ctx", "n_gpu_layers", "parallel", "image_max_tokens", "offline", "no_webui", "jinja", "extra_args"):
        assert s["server"][k] == cfg["server"][k], k
    for k in ("max_tokens", "temperature", "top_k", "seed", "cache_prompt"):
        assert s["generation"][k] == cfg["generation"][k], k
    assert s["sampling"] == cfg["sampling"]
    b = m["variants"]["2b-q8"]
    assert b["revision"] == cfg["model_revision"]
    for k in ("weights", "mmproj"):
        assert b[k]["sha256"] == cfg[k]["sha256"] and b[k]["bytes"] == cfg[k]["bytes"]
    c = m["variants"]["4b-q4km"]
    assert c["weights"]["quantization"] == "Q4_K_M" and c["mmproj"]["quantization"] == b["mmproj"]["quantization"]
    for v in m["variants"].values():
        assert re.fullmatch(r"[0-9a-f]{40}", v["revision"]) and re.fullmatch(r"[0-9a-f]{40}", v["base_revision"])
        assert v["license"] == "apache-2.0"
        assert all(re.fullmatch(r"[0-9a-f]{64}", v[k]["sha256"]) for k in ("weights", "mmproj"))


def test_videos_list_rejects_frozen_test(tmp_path):
    vid = tmp_path / "a.mov"
    vid.write_bytes(b"x")
    p = tmp_path / "v.json"
    p.write_text(json.dumps([{"id": "a", "path": str(vid), "role": "test"}]), encoding="utf-8")
    with pytest.raises(SystemExit):
        compare.load_videos(p)


def test_safe_extract_rejects_traversal(tmp_path):
    arc = tmp_path / "bad.tar.gz"
    with tarfile.open(arc, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("../evil.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    with pytest.raises(SystemExit):
        fetch._safe_extract(arc, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


class _Srv:
    def __init__(self, fake):
        self.fake, self.client = fake, LlamaClient(fake.url)

    def start(self):
        return 0.01

    def stop(self):
        pass


@needs_ffmpeg
def test_compare_review_score_end_to_end(tmp_path):
    video = tmp_path / "clip.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    videos = [{"id": "dev-a", "path": str(video), "role": "dev", "source_label": "violation"}]
    seen = {"2b-q8": [], "4b-q4km": []}

    def r2b(idx):
        seen["2b-q8"].append(idx)
        return ok([{"category": "cash_bag_handover", "frame_indices": idx[:1], "description": SECRET}])

    def r4b(idx):
        seen["4b-q4km"].append(idx)
        if idx[0] == 10:
            return 200, '{"frames_assessable": true, "observ', "length"
        return ok([] if idx[0] else [{"category": "cash_bag_opened", "frame_indices": idx[1:2],
                                       "description": SECRET}])

    fakes = {"2b-q8": FakeLlama(r2b), "4b-q4km": FakeLlama(r4b)}
    try:
        summary = compare.run_experiment(fetch.load_manifest(), videos, ["2b-q8", "4b-q4km"], tmp_path / "run",
                                         "windows-x64", server_factory=lambda n, cfg, log: _Srv(fakes[n]),
                                         use_vram=False)
    finally:
        for f in fakes.values():
            f.close()
    assert seen["2b-q8"] == seen["4b-q4km"] and len(seen["2b-q8"]) == 3  # одинаковые окна и кадры
    assert SECRET not in json.dumps(summary)
    a, b = summary["variants"]["2b-q8"], summary["variants"]["4b-q4km"]
    assert a["status"] == {"ok": 3} and a["observations"] == 3
    assert b["status"] == {"ok": 2, "truncated": 1} and b["observations"] == 1
    assert a["by_group"]["all"]["failed_technical"] == 0 and b["by_group"]["all"]["failed_technical_share"] == 1.0
    assert "label=violation" in a["by_group"] and "role=dev" in b["by_group"]

    run = tmp_path / "run"
    page = review.build(run, seed=1)
    assert page.is_file() and "Ответ A" in page.read_text(encoding="utf-8")
    key = json.loads((run / "blind_key.json").read_text(encoding="utf-8"))["windows"]
    side_2b = {wk: next(s for s, v in m.items() if v == "2b-q8") for wk, m in key.items()}
    side_4b = {wk: next(s for s, v in m.items() if v == "4b-q4km") for wk, m in key.items()}
    export = {"run_id": "run", "reviewer": "test", "items": {
        f"dev-a|0|{side_2b['dev-a|0']}|0": "confirmed",
        f"dev-a|1|{side_2b['dev-a|1']}|0": "not_confirmed",
        f"dev-a|2|{side_2b['dev-a|2']}|0": "unclear",
        f"dev-a|0|{side_4b['dev-a|0']}|0": "not_confirmed"},
        "missed": [f"dev-a|2|{side_4b['dev-a|2']}|missed"]}
    (run / "review_export.json").write_text(json.dumps(export), encoding="utf-8")
    s = score.score(run, run / "review_export.json", videos)
    g2, g4 = s["groups"]["all"]["2b-q8"], s["groups"]["all"]["4b-q4km"]
    assert (g2["confirmed"], g2["not_confirmed"], g2["unclear"], g2["unreviewed"]) == (1, 1, 1, 0)
    assert g2["confirmed_share_of_decided"] == 0.5
    assert (g4["observations"], g4["not_confirmed"], g4["windows_not_ok"], g4["windows_marked_missed"]) == (1, 1, 1, 1)
    assert SECRET not in json.dumps(s)
