"""Список видео для dev_eval/compare из CSV split'а интегратора, с проверкой SHA256 и без дубликатов групп.

Метки читаются локально только для выбранных файлов и передаются оценщику (source_label), не модели.
Одна запись на group_id (точные копии не считаются независимыми примерами). Печатаются только счётчики.

    python -m app.inference.experiments.make_video_list --split-csv outputs/experiments/dev_files_for_omar.csv ^
        --videos-dir <папка с видео> --labels-csv <labels_normalized.csv> --diagnostic X-4 ^
        --out outputs/experiments/videos.dev.local.json --remote-dir /home/user/assar-ml/data/dev
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

_CYR = str.maketrans({"Х": "X", "х": "X"})


def canonical(name: str) -> str:
    m = re.fullmatch(r"X-?(\d+)", Path(name).stem.strip().translate(_CYR).upper())
    return f"X-{int(m.group(1))}" if m else name


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def build(split_csv: Path, videos_dir: Path, labels_csv: Path | None, diagnostic: set[str],
          allowed_splits: set[str]) -> list[dict]:
    rows = list(csv.DictReader(open(split_csv, encoding="utf-8-sig")))
    if any(r["split"] not in allowed_splits for r in rows):
        raise SystemExit(f"split CSV contains rows outside {sorted(allowed_splits)} (frozen test is not allowed)")
    files = {canonical(p.name): p for p in videos_dir.iterdir() if p.suffix.lower() == ".mov"}
    labels, timecodes = {}, {}
    if labels_csv:
        wanted = {r["canonical_id"] for r in rows}
        for r in csv.DictReader(open(labels_csv, encoding="utf-8-sig")):
            if r["video_id"] in wanted:  # только выбранные файлы
                labels[r["video_id"]] = "violation" if str(r["label"]).strip() in ("1", "1.0") else "no_violation"
                tc = str(r.get("timecode_sec") or "").strip()
                timecodes[r["video_id"]] = float(tc) if tc not in ("", "nan") else None
    out, seen_groups = [], set()
    for r in rows:
        cid = r["canonical_id"]
        p = files.get(cid)
        if p is None:
            raise SystemExit(f"file for {cid} not found in videos dir")
        if sha256(p) != r["sha256"]:
            raise SystemExit(f"SHA256 mismatch for {cid}")
        if r["group_id"] in seen_groups:
            continue
        seen_groups.add(r["group_id"])
        out.append({"id": cid, "path": str(p.resolve()), "file": p.name,
                    "role": "diagnostic" if cid in diagnostic else "dev",
                    "source_label": labels.get(cid),
                    # только для разметки/оценки (discover, dev_eval), не для inference
                    "source_timecode_sec": timecodes.get(cid)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split-csv", type=Path, required=True)
    ap.add_argument("--videos-dir", type=Path, required=True)
    ap.add_argument("--labels-csv", type=Path)
    ap.add_argument("--diagnostic", default="X-4")
    ap.add_argument("--splits", default="dev,train")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--remote-dir", help="каталог видео на сервер: создаёт <out>.remote.json и команду scp")
    ap.add_argument("--remote-host", default="user@remote-host")
    ap.add_argument("--remote-root", default="~/assar/code", help="корень checkout на сервер")
    args = ap.parse_args()
    items = build(args.split_csv, args.videos_dir, args.labels_csv, set(args.diagnostic.split(",")),
                  set(args.splits.split(",")))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for it in items:
        k = f"{it['role']}/{it['source_label'] or 'unknown'}"
        counts[k] = counts.get(k, 0) + 1
    print(f"{len(items)} unique groups, SHA256 verified; by role/label: {counts}")
    print(f"local list: {args.out}")
    if args.remote_dir:
        remote = [dict(it, path=f"{args.remote_dir.rstrip('/')}/{it['id']}.mov") for it in items]
        rpath = args.out.with_suffix(".remote.json")
        rpath.write_text(json.dumps(remote, ensure_ascii=False, indent=1), encoding="utf-8")
        cmd = args.out.with_suffix(".scp.ps1")
        lists_dir = f"{args.remote_root.rstrip('/')}/outputs/experiments"
        lines = [f"ssh {args.remote_host} \"mkdir -p {args.remote_dir} {lists_dir}\""]
        lines += [f'scp "{it["path"]}" "{args.remote_host}:{args.remote_dir.rstrip("/")}/{it["id"]}.mov"' for it in items]
        lines.append(f'scp "{rpath.resolve()}" "{args.remote_host}:{lists_dir}/{args.out.name}"')
        # UTF-8 с BOM: иначе Windows PowerShell 5.1 читает кириллицу в именах файлов как ANSI
        cmd.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        print(f"remote list: {rpath}\nupload script (PowerShell): {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
