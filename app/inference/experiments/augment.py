"""Проверка устойчивости к ракурсу: копии dev-видео с другой геометрией кадра (ТОЛЬКО ДЛЯ ОЦЕНКИ).

Финальные видео сняты под другим углом. Здесь из выбранных видео делаются копии: зеркало, кадрирование 80%,
поворот на 6°, перспектива (имитация другого угла камеры). Метки и роли копируются; id копии — «<id>~<aug>».
Список для dev_eval пишется вместе с исходными видео, сравнение вердиктов — в report.py (раздел stability).

    python -m app.inference.experiments.augment --videos outputs/experiments/videos.dev.local.json \\
        --out outputs/aug_dev
    python -m app.inference.experiments.dev_eval --videos outputs/aug_dev/videos.aug.json --mode v6
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .compare import load_videos

AUGS = {
    "hflip": "hflip",
    "crop": "crop=trunc(iw*0.8/2)*2:trunc(ih*0.8/2)*2,scale=trunc(iw/0.8/2)*2:trunc(ih/0.8/2)*2",
    "rot": "rotate=6*PI/180:fillcolor=black",
    "persp": "perspective=x0=W*0.06:y0=H*0.05:x1=W*0.94:y1=0:x2=0:y2=H:x3=W:y3=H*0.95:sense=source",
}


def make(src: Path, dst: Path, vf: str, ffmpeg: str = "ffmpeg") -> None:
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(src), "-vf", vf, "-an", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y",
                    str(dst)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--augs", default=",".join(AUGS))
    ap.add_argument("--no-originals", action="store_true", help="не включать исходные видео в список")
    args = ap.parse_args()
    videos = load_videos(args.videos)
    augs = [a.strip() for a in args.augs.split(",") if a.strip()]
    if unknown := set(augs) - set(AUGS):
        raise SystemExit(f"unknown augs: {sorted(unknown)}")
    args.out.mkdir(parents=True, exist_ok=True)
    items = [] if args.no_originals else [dict(v) for v in videos]
    for v in videos:
        for a in augs:
            dst = (args.out / f"{v['id']}~{a}.mp4").resolve()
            if not dst.is_file():
                make(Path(v["path"]), dst, AUGS[a])
            items.append(dict(v, id=f"{v['id']}~{a}", path=str(dst), augmentation=a))
        print(f"{v['id']}: {len(augs)} copies", flush=True)
    out = args.out / "videos.aug.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(items)} videos -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
