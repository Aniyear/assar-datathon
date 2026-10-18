"""Два пробных уточнения поверх готовых прогонов (только train/dev, эксперимент):

access — проверка кандидатов «посторонний в зоне кассира»: по кадрам отрезков outsider=yes из результатов dev_eval
         модель отвечает на один узкий вопрос (человек на стороне сотрудника, а не клиент напротив?).
motion — движение у сейфа перед концом ролика: по рамке сейфа из zoom_probe вырезаем область на 5 кадрах/с за
         последние N секунд и считаем, сколько секунд в ней было движение (рука у замка). Модель не нужна.

В консоль — числа по видео, без описаний.

    python -m app.inference.experiments.refine_probe access --run outputs/final_4b_train/<время>/4b-q4km --variant 4b-q4km
    python -m app.inference.experiments.refine_probe motion --videos outputs/experiments/videos.train.local.json --zoom outputs/zoom/<время>/zoom.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from app.contracts import AnalysisError

from ..video import probe
from ..vlm import LlamaClient, LlamaServer
from .compare import load_videos, server_config
from .fetch import load_manifest
from .zoom_probe import crop

TRI = ["yes", "no", "unknown"]
ACCESS_Q = ("Each frame shows a bank cash office from a fixed overhead camera. The cashier is the employee who "
            "works at the desk on the staff side (computer, cash machine, safe). For EACH frame answer yes, no or "
            "unknown: is there ANOTHER person (not the cashier) on the staff side of the desk — standing or sitting "
            "next to, behind or leaning over the cashier, or at the safe, cash machine or computer? A customer who "
            "sits or stands across the desk on the customer side does NOT count.")


def _runs(vals: list[str], want: str = "yes") -> list[tuple[int, int]]:
    out, s = [], None
    for i, v in enumerate(vals + ["#"]):
        if v == want and s is None:
            s = i
        elif v != want and s is not None:
            out.append((s, i - 1))
            s = None
    return out


def access(run: Path, client: LlamaClient, min_run: int = 2) -> list[dict]:
    rs = json.loads((run / "results.json").read_text(encoding="utf-8"))
    out = []
    for r in rs:
        tr = r.get("trace") or {}
        tl, F = tr.get("timeline", []), tr.get("fields", [])
        rec = {"id": r["id"], "label": r["source_label"], "runs": []}
        if tl and "outsider_staff_side" in F:
            col = [row[2 + F.index("outsider_staff_side")] for row in tl]
            for a, b in _runs(col):
                if b - a + 1 < min_run:
                    continue
                pick = sorted({a, (a + b) // 2, b})
                idx = [tl[i][0] for i in pick]
                parts: list = [ACCESS_Q]
                for i in pick:
                    fi, t = tl[i][0], tl[i][1]
                    parts += [f"Frame {fi}, t={t:.1f}s:", run / "jobs" / r["id"] / "frames" / f"e_{fi + 1:05d}.jpg"]
                schema = {"type": "object", "properties": {"frames": {"type": "array", "minItems": len(idx),
                          "maxItems": len(idx), "items": {"type": "object", "properties": {
                              "index": {"type": "integer", "enum": idx}, "other_staff_side": {"type": "string",
                                                                                              "enum": TRI}},
                          "required": ["index", "other_staff_side"]}}}, "required": ["frames"]}
                yes = None
                try:
                    resp = client.chat(parts, schema, max_tokens=200, timeout=120)
                    yes = sum(1 for x in json.loads(resp["choices"][0]["message"]["content"])["frames"]
                              if x["other_staff_side"] == "yes")
                except (AnalysisError, ValueError, KeyError, TypeError):
                    pass
                rec["runs"].append({"len": b - a + 1, "checked": len(idx), "verified_yes": yes})
        out.append(rec)
    return out


def motion(videos: list[dict], zoom: Path, last_sec: float, fps: float, thr: float) -> list[dict]:
    boxes = {z["id"]: z.get("bbox") for z in json.loads(zoom.read_text(encoding="utf-8"))}
    out = []
    for v in videos:
        bbox = boxes.get(v["id"])
        rec = {"id": v["id"], "label": v.get("source_label"), "safe_found": bool(bbox)}
        if not bbox:
            out.append(rec)
            continue
        dur = probe(Path(v["path"])).duration_sec
        start = max(0.0, dur - last_sec)
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", v["path"], "-vf", f"fps={fps}",
                            "-q:v", "3", str(Path(td) / "f_%04d.jpg")], check=True)
            frames = sorted(Path(td).glob("f_*.jpg"))
            prev, diffs = None, []
            for f in frames:
                c = crop(f, bbox, Path(td) / "c.jpg", pad=0.15, target=96)
                with Image.open(c) as im:
                    g = im.convert("L").resize((64, 64))
                    g.load()
                if prev is not None:
                    diffs.append(ImageStat.Stat(ImageChops.difference(g, prev)).mean[0])
                prev = g
        active = [d for d in diffs if d > thr]
        rec.update(window_sec=round(dur - start, 1), active_sec=round(len(active) / fps, 1),
                   mean_diff=round(sum(diffs) / len(diffs), 2) if diffs else None,
                   max_diff=round(max(diffs), 1) if diffs else None)
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("access")
    a.add_argument("--run", type=Path, required=True, help="каталог варианта dev_eval (с results.json и jobs/)")
    a.add_argument("--variant", default="4b-q4km")
    a.add_argument("--endpoint")
    a.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                   default="windows-x64" if sys.platform == "win32" else "linux-x64")
    a.add_argument("--min-run", type=int, default=2)
    a.add_argument("--image-max-tokens", type=int, default=1024)
    m = sub.add_parser("motion")
    m.add_argument("--videos", type=Path, required=True)
    m.add_argument("--zoom", type=Path, required=True)
    m.add_argument("--last-sec", type=float, default=20.0)
    m.add_argument("--fps", type=float, default=5.0)
    m.add_argument("--thr", type=float, default=6.0, help="порог средней разницы яркости (0–255)")
    args = ap.parse_args()
    if args.cmd == "motion":
        for r in motion(load_videos(args.videos), args.zoom, args.last_sec, args.fps, args.thr):
            print(f"{r['id']:>5} {str(r['label']):>12} safe={'yes' if r['safe_found'] else 'no '} "
                  + (f"active_sec={r['active_sec']:>4} of {r['window_sec']} mean={r['mean_diff']} max={r['max_diff']}"
                     if r["safe_found"] else ""))
        return 0
    server = None
    if args.endpoint:
        client = LlamaClient(args.endpoint)
    else:
        scfg = server_config(load_manifest(), args.variant, args.platform)
        scfg.image_max_tokens, scfg.ctx = args.image_max_tokens, 12288
        server = LlamaServer(scfg, args.run / "refine-llama-server.log")
        server.start(timeout=600)
        client = server.client
    try:
        recs = access(args.run, client, args.min_run)
    finally:
        if server:
            server.stop()
    for r in recs:
        if r["runs"]:
            print(f"{r['id']:>5} {str(r['label']):>12} " + "  ".join(
                f"[len={x['len']} verified={x['verified_yes']}/{x['checked']}]" for x in r["runs"]))
    print("videos with outsider runs:", sum(1 for r in recs if r["runs"]), "of", len(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
