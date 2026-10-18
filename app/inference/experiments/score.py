"""Агрегаты ручной сверки по вариантам: подтверждённые / неподтверждённые / неясные наблюдения, пропуски.

Это не F1 событий: эталонной интервальной разметки нет, а «пропуск» — отметка проверяющего по окну.
Печатает только числа.

    python -m app.inference.experiments.score outputs/experiments/<run> --review review_export.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def score(run_dir: Path, review_path: Path, videos: list[dict] | None = None) -> dict:
    key = json.loads((run_dir / "blind_key.json").read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    if review.get("run_id") != key["run_id"]:
        raise SystemExit("review export belongs to another run")
    rows = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
    meta = {v["id"]: v for v in (videos or [])}
    missed = set(review.get("missed", []))
    groups: dict[str, dict[str, dict]] = {}

    def bucket(group: str, variant: str) -> dict:
        return groups.setdefault(group, {}).setdefault(variant, {
            "windows": 0, "windows_not_ok": 0, "observations": 0, "confirmed": 0, "not_confirmed": 0,
            "unclear": 0, "unreviewed": 0, "windows_marked_missed": 0})

    inverse = {wk: {v: s for s, v in m.items()} for wk, m in key["windows"].items()}
    for r in rows:
        wk = f"{r['video']}|{r['window']}"
        side = inverse[wk][r["variant"]]
        v = meta.get(r["video"], {})
        for g in ("all", f"role={v.get('role', 'unknown')}", f"label={v.get('source_label') or 'unknown'}"):
            b = bucket(g, r["variant"])
            b["windows"] += 1
            if r["status"] != "ok":
                b["windows_not_ok"] += 1
                continue
            if f"{wk}|{side}|missed" in missed:
                b["windows_marked_missed"] += 1
            for i, _ in enumerate(json.loads(r["content"])["observations"]):
                b["observations"] += 1
                verdict = review.get("items", {}).get(f"{wk}|{side}|{i}")
                b[verdict if verdict in ("confirmed", "not_confirmed", "unclear") else "unreviewed"] += 1
    for per_var in groups.values():
        for b in per_var.values():
            decided = b["confirmed"] + b["not_confirmed"]
            b["confirmed_share_of_decided"] = round(b["confirmed"] / decided, 3) if decided else None
    return {"run_id": key["run_id"], "reviewer_set": bool(review.get("reviewer")),
            "note": "Manual window-level check; not event F1 (no reference interval annotation).",
            "groups": groups}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--review", type=Path, required=True)
    ap.add_argument("--videos", type=Path, help="тот же videos.local.json для разбивки по role/label")
    args = ap.parse_args()
    videos = json.loads(args.videos.read_text(encoding="utf-8")) if args.videos else None
    result = score(args.run_dir, args.review, videos)
    (args.run_dir / "score.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
