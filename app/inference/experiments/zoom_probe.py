"""Пробный «найти и приблизить»: модель находит сейф на кадре (рамка 0–1000), затем по вырезанной и увеличенной
области сейфа отвечает, открыта ли дверца и оставлен ли ключ. Только эксперимент на train/dev.

Рамка ищется заново в каждом видео (не зависит от ракурса). Ответы и вырезки — локально в --out; в консоль —
агрегаты по видео (сколько кадров «открыт» / «ключ»), без описаний.

    python -m app.inference.experiments.zoom_probe --videos outputs/experiments/videos.train.local.json --variant 4b-q4km
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.contracts import AnalysisError

from ..video import probe, sample_frames
from ..vlm import LlamaClient, LlamaServer
from .compare import load_videos, server_config
from .fetch import ROOT, load_manifest

TRI = ["yes", "no", "unknown"]
LOCATE = ("This is one frame from a fixed overhead CCTV camera in a bank cash office. Find the safe (a metal "
          "cabinet or box with a door and a lock where cash is kept). If it is visible, return found=true and "
          "its bounding box as [x1, y1, x2, y2] in relative coordinates from 0 to 1000. If no safe is visible, "
          "return found=false.")
LOCATE_SCHEMA = {"type": "object", "properties": {"found": {"type": "boolean"},
                 "bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "integer"}}},
                 "required": ["found", "bbox"]}
STATE = ("You see {n} close-up crops of the SAME safe from a CCTV video, in time order, each preceded by its "
         "index. For EACH crop answer yes, no or unknown:\n"
         "- door_open: is the safe door open (gap, door swung out, interior visible)?\n"
         "- key_in_lock: is a key inserted in the safe lock or hanging from it?\n"
         "Use unknown only if the crop does not let you decide.")


def state_schema(idx: list[int]) -> dict:
    item = {"type": "object", "properties": {"index": {"type": "integer", "enum": idx},
                                             "door_open": {"type": "string", "enum": TRI},
                                             "key_in_lock": {"type": "string", "enum": TRI}},
            "required": ["index", "door_open", "key_in_lock"]}
    return {"type": "object", "properties": {"crops": {"type": "array", "minItems": len(idx), "maxItems": len(idx),
                                                       "items": item}}, "required": ["crops"]}


def crop(src: Path, bbox: list[int], dst: Path, pad: float = 0.25, target: int = 448) -> Path:
    with Image.open(src) as im:
        w, h = im.size
        x1, y1, x2, y2 = [max(0, min(1000, v)) for v in bbox]
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bad bbox")
        bw, bh = (x2 - x1) * w / 1000, (y2 - y1) * h / 1000
        cx, cy = (x1 + x2) * w / 2000, (y1 + y2) * h / 2000
        bw, bh = bw * (1 + 2 * pad), bh * (1 + 2 * pad)
        box = (int(max(0, cx - bw / 2)), int(max(0, cy - bh / 2)), int(min(w, cx + bw / 2)), int(min(h, cy + bh / 2)))
        c = im.crop(box)
        scale = target / max(c.size)
        c = c.resize((max(1, int(c.width * scale)), max(1, int(c.height * scale))), Image.LANCZOS)
        c.save(dst, "JPEG", quality=92)
    return dst


def ask(client: LlamaClient, parts, schema, max_tokens=600) -> dict | None:
    try:
        r = client.chat(parts, schema, max_tokens=max_tokens, timeout=120)
    except AnalysisError:
        return None
    ch = r["choices"][0]
    if ch.get("finish_reason") != "stop":
        return None
    try:
        return json.loads(ch["message"]["content"])
    except (ValueError, TypeError):
        return None


def probe_video(client: LlamaClient, v: dict, out: Path, step: float, batch: int) -> dict:
    info = probe(Path(v["path"]))
    frames = sample_frames(Path(v["path"]), out / "frames" / v["id"], step, info)
    ref_frames = [frames[len(frames) // 4], frames[len(frames) // 2], frames[(3 * len(frames)) // 4]]
    bbox = None
    for rf in ref_frames:  # рамка — по первому кадру, где сейф найден
        loc = ask(client, [LOCATE, rf.evidence_path], LOCATE_SCHEMA, max_tokens=60)
        if loc and loc.get("found") and len(loc.get("bbox", [])) == 4:
            bbox = loc["bbox"]
            break
    rec = {"id": v["id"], "label": v.get("source_label"), "bbox": bbox, "frames": len(frames), "states": []}
    if bbox is None:
        return rec
    cdir = out / "crops" / v["id"]
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        crops = [(f, crop(f.evidence_path, bbox, cdir / f"c_{f.index:05d}.jpg")) for f in frames]
    except ValueError:
        rec["bbox_error"] = True
        return rec
    for i in range(0, len(crops), batch):
        chunk = crops[i:i + batch]
        idx = [f.index for f, _ in chunk]
        parts: list = [STATE.format(n=len(chunk))]
        for f, c in chunk:
            parts += [f"Crop {f.index}, t={f.pts_sec:.1f}s:", c]
        res = ask(client, parts, state_schema(idx))
        for f, _ in chunk:
            row = next((x for x in (res or {}).get("crops", []) if x.get("index") == f.index), None)
            rec["states"].append([f.index, round(f.pts_sec, 2), (row or {}).get("door_open", "error"),
                                  (row or {}).get("key_in_lock", "error")])
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--variant", default="4b-q4km")
    ap.add_argument("--endpoint", help="уже запущенный OpenAI-совместимый сервер вместо собственного llama-server")
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                    default="windows-x64" if sys.platform == "win32" else "linux-x64")
    ap.add_argument("--step", type=float, default=1.0)
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "zoom")
    args = ap.parse_args()
    out = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    videos = load_videos(args.videos)
    server = None
    if args.endpoint:
        client = LlamaClient(args.endpoint)
    else:
        scfg = server_config(load_manifest(), args.variant, args.platform)
        scfg.extra_args = list(scfg.extra_args) + ["--image-min-tokens", "1024"]  # grounding в Qwen-VL просит ≥1024 токенов
        scfg.image_max_tokens, scfg.ctx = 1024, 16384
        server = LlamaServer(scfg, out / "llama-server.log")
        server.start(timeout=600)
        client = server.client
    t0 = time.monotonic()
    try:
        recs = [probe_video(client, v, out, args.step, args.batch) for v in videos]
    finally:
        if server:
            server.stop()
    (out / "zoom.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in recs:
        st = r["states"]
        o = sum(1 for s in st if s[2] == "yes")
        k = sum(1 for s in st if s[3] == "yes")
        u = sum(1 for s in st if s[2] == "unknown")
        e = sum(1 for s in st if s[2] == "error")
        print(f"{r['id']:>5} {str(r['label']):>12} safe_found={'yes' if r['bbox'] else 'no ':3} "
              f"door_open={o:>3} key={k:>3} unknown={u:>3} error={e:>3} of {len(st)}")
    print(f"sec_total={time.monotonic() - t0:.1f}  local run dir: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
