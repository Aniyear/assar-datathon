"""Отсутствие кассира без нейросети: движение в автоматически найденной «зоне кассира». ТОЛЬКО ДЛЯ ОЦЕНКИ.

Кадр 1 раз в секунду уменьшается до 64×64 в оттенках серого. Сетка 8×8 ячеек; зона кассира — ZONE_CELLS ячеек с
наибольшим средним движением за ролик (кассир двигается почти всё время, камера неподвижна; ракурс не важен).
Секунда — «зона пуста», если движение в зоне ниже REL × медианы. Серия ≥ MIN_RUN секунд — кандидат ухода.
Сравнивается с метками и, если дан --results, с ответом модели «кассир на месте» по тем же секундам.
В консоль — агрегаты; по видео — <out>.

    python -m app.inference.experiments.absence_cv --videos outputs/experiments/videos.train.local.json \\
        [--results outputs/dev_eval/<run>/4b-q4km/results.json] [--types outputs/experiments/types.local.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

from .compare import load_videos

SIZE, GRID = 64, 8
ZONE_CELLS = 4
REL = 0.25
MIN_RUN = 3


def gray_frames(video: Path, ffmpeg: str = "ffmpeg") -> list[bytes]:
    r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(video), "-vf",
                        f"fps=1,scale={SIZE}:{SIZE},format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    n = SIZE * SIZE
    return [r.stdout[i:i + n] for i in range(0, len(r.stdout) - n + 1, n)]


def cell_motion(a: bytes, b: bytes) -> list[float]:
    c = SIZE // GRID
    out = []
    for gy in range(GRID):
        for gx in range(GRID):
            s = 0
            for y in range(gy * c, gy * c + c):
                row = y * SIZE
                for x in range(gx * c + row, gx * c + c + row):
                    s += abs(a[x] - b[x])
            out.append(s / (c * c))
    return out


def analyse(frames: list[bytes]) -> dict:
    if len(frames) < MIN_RUN + 2:
        return {"seconds": len(frames), "runs": [], "absent": []}
    motion = [cell_motion(frames[t - 1], frames[t]) for t in range(1, len(frames))]
    mean = [statistics.fmean(m[c] for m in motion) for c in range(GRID * GRID)]
    zone = sorted(range(GRID * GRID), key=lambda c: -mean[c])[:ZONE_CELLS]
    zm = [statistics.fmean(m[c] for c in zone) for m in motion]
    tau = REL * statistics.median(zm)
    absent = [False] + [z < tau for z in zm]  # секунда 0 без пары
    runs, start = [], None
    for t, a in enumerate(absent + [False]):
        if a and start is None:
            start = t
        elif not a and start is not None:
            if t - start >= MIN_RUN:
                runs.append([start, t - 1])
            start = None
    return {"seconds": len(frames), "zone": zone, "tau": round(tau, 3), "runs": runs, "absent": absent}


def vlm_absent(trace: dict | None) -> dict[int, bool]:
    if not trace or not trace.get("timeline"):
        return {}
    k = trace["fields"].index("cashier_at_workplace")
    return {round(row[1]): row[2 + k] == "no" for row in trace["timeline"] if row[2 + k] in ("yes", "no")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--results", type=Path, nargs="*", default=[])
    ap.add_argument("--types", type=Path)
    ap.add_argument("--out", type=Path, default=Path("outputs/absence_cv.local.json"))
    args = ap.parse_args()
    videos = load_videos(args.videos)
    traces = {r["id"]: r.get("trace") for p in args.results for r in json.loads(p.read_text(encoding="utf-8"))}
    types = ({row["id"]: (row.get("type_id") or "").strip()
              for row in csv.DictReader(args.types.open(encoding="utf-8-sig"))} if args.types else {})
    per, agg = {}, {}
    for v in videos:
        res = analyse(gray_frames(Path(v["path"])))
        vlm = vlm_absent(traces.get(v["id"]))
        both = [(res["absent"][t], a) for t, a in vlm.items() if t < len(res["absent"])]
        res["vs_model"] = {"cv_absent_model_absent": sum(c and m for c, m in both),
                           "cv_absent_model_present": sum(c and not m for c, m in both),
                           "cv_present_model_absent": sum(m and not c for c, m in both), "seconds": len(both)}
        per[v["id"]] = {k: res[k] for k in ("seconds", "runs", "vs_model")}
        for key in (f"label={v.get('source_label')}", f"type={types.get(v['id'], '?')}" if types else None):
            if key is None:
                continue
            b = agg.setdefault(key, {"videos": 0, "with_absence_run": 0, "absence_seconds": 0, "seconds": 0})
            b["videos"] += 1
            b["with_absence_run"] += bool(res["runs"])
            b["absence_seconds"] += sum(e - s + 1 for s, e in res["runs"])
            b["seconds"] += res["seconds"]
        print(f"{v['id']}: done", flush=True)
    tot = {k: sum(p["vs_model"][k] for p in per.values()) for k in
           ("cv_absent_model_absent", "cv_absent_model_present", "cv_present_model_absent", "seconds")}
    rep = {"params": {"zone_cells": ZONE_CELLS, "rel": REL, "min_run": MIN_RUN}, "groups": agg, "vs_model": tot}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"report": rep, "videos": per}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"local detail: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
