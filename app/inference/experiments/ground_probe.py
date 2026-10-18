"""Разведка «найди → приблизь → проверь» (ТОЛЬКО train/dev, эксперимент).

1. Найди: модель обводит объекты на кадре (сетка 0–1000): кассир, другие люди, ноутбук, сейф, дверь, напиток,
   телефон, сумка, деньги.
2. Приблизь: самая крупная рамка каждого объекта вырезается из кадра исходного разрешения (с полями).
3. Проверь: узкий вопрос yes/no/unknown по вырезке (экран горит? ключ в замке? дверь открыта? это напиток?).
Признаки видео — доли кадров; отчёт сравнивает видео каждого типа нарушения с эталонами (label=no_violation):
признак полезен, только если он есть у нарушений этого типа и почти нет у эталонов.
В консоль — агрегаты по типам; по видео и кадрам — <out>/ground.local.json.

    python -m app.inference.experiments.ground_probe --videos outputs/experiments/videos.train.local.json \\
        outputs/experiments/videos.dev.local.json --types outputs/experiments/types.local.csv --out outputs/ground
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

from app.contracts import AnalysisError

from ..video import probe, sample_frames
from ..vlm import LlamaClient, LlamaServer
from .compare import load_videos, server_config
from .fetch import load_manifest
from .zoom_probe import crop

TRI = ["yes", "no", "unknown"]
LABELS = ["cashier", "other_person", "laptop", "safe", "door", "drink", "phone", "bag", "cash"]
GROUND_Q = (
    "The image is a frame from a fixed overhead CCTV camera in a bank cash office. The cashier is the employee at "
    "the staff side of the desk. Find these objects and give a bounding box for each instance: cashier; "
    "other_person (anyone else inside the room); laptop; safe (metal safe or cash cabinet); door (door of the "
    "room); drink (cup, mug, glass or bottle); phone (mobile phone); bag (handbag, backpack or personal bag); "
    "cash (banknotes, coins or a cash-collection bag outside the safe). bbox_2d is [x1, y1, x2, y2] in relative "
    "coordinates from 0 to 1000. List only objects you actually see. Do not identify people.")
CROP_Q = {
    "laptop": {"screen_on": "Is the laptop screen switched on and lit (showing an image), not black or closed?",
               "lid_open": "Is the laptop lid open?"},
    "safe": {"door_open": "Is the door of this safe open?",
             "key_in_lock": "Is a key inserted in the lock of this safe (a key or key ring sticking out of it)?"},
    "door": {"door_open": "Is this door open or ajar (not fully closed)?"},
    "drink": {"is_drink": "Is this a drink container (cup, mug, glass or bottle)?"},
    "phone": {"is_phone": "Is this a mobile phone?"},
    "bag": {"is_personal_bag": "Is this a personal bag (handbag, backpack, shopping bag)?"},
    "cash": {"is_cash": "Are these banknotes, coins or a cash-collection bag?"},
}


def ground_schema() -> dict:
    box = {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
    item = {"type": "object", "properties": {"label": {"type": "string", "enum": LABELS}, "bbox_2d": box},
            "required": ["label", "bbox_2d"]}
    return {"type": "object", "properties": {"objects": {"type": "array", "maxItems": 24, "items": item}},
            "required": ["objects"]}


def crop_schema(qs: dict) -> dict:
    return {"type": "object", "properties": {k: {"type": "string", "enum": TRI} for k in qs}, "required": list(qs)}


def _ask(client: LlamaClient, parts, schema, max_tokens: int) -> dict | None:
    try:
        r = client.chat(parts, schema, max_tokens=max_tokens, timeout=120)
    except AnalysisError:
        return None
    ch = r["choices"][0]
    if ch.get("finish_reason") != "stop":
        return None
    try:
        return json.loads(ch["message"].get("content") or "")
    except ValueError:
        return None


def _valid(b) -> bool:
    return (isinstance(b, list) and len(b) == 4 and all(isinstance(v, int) and 0 <= v <= 1000 for v in b)
            and b[0] < b[2] and b[1] < b[3])


def frame_facts(client: LlamaClient, f, tmp: Path) -> dict:
    obj = _ask(client, [GROUND_Q, f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path, "Answer in JSON."],
               ground_schema(), 700)
    rec: dict = {"t": round(f.pts_sec, 2), "ok": obj is not None, "count": {}, "crop": {}}
    if obj is None:
        return rec
    best: dict[str, list[int]] = {}
    for o in obj.get("objects", []):
        b = o.get("bbox_2d")
        if o.get("label") not in LABELS or not _valid(b):
            continue
        rec["count"][o["label"]] = rec["count"].get(o["label"], 0) + 1
        area = (b[2] - b[0]) * (b[3] - b[1])
        if o["label"] not in best or area > (best[o["label"]][2] - best[o["label"]][0]) * (
                best[o["label"]][3] - best[o["label"]][1]):
            best[o["label"]] = b
    for lab, qs in CROP_Q.items():
        if lab not in best:
            continue
        try:
            c = crop(f.evidence_path, best[lab], tmp / f"{f.index}_{lab}.jpg")
        except (ValueError, OSError):
            continue
        q = ("The image is a zoomed crop from a bank office CCTV frame. Answer yes, no or unknown:\n"
             + "\n".join(f"- {k}: {v}" for k, v in qs.items()))
        ans = _ask(client, [q, "Crop 0, t=0.0s:", c, "Answer in JSON."], crop_schema(qs), 120)
        if ans and all(ans.get(k) in TRI for k in qs):
            rec["crop"][lab] = {k: ans[k] for k in qs}
    return rec


def features(frames: list[dict]) -> dict:
    ok = [r for r in frames if r["ok"]]
    if not ok:
        return {}

    def yes(r, lab, k):
        return r["crop"].get(lab, {}).get(k) == "yes"
    absent = [r for r in ok if not r["count"].get("cashier")]
    frac = lambda xs, p: round(sum(1 for r in xs if p(r)) / len(xs), 3) if xs else 0.0  # noqa: E731
    return {
        "cashier_absent": frac(ok, lambda r: not r["count"].get("cashier")),
        "two_plus_others": frac(ok, lambda r: r["count"].get("other_person", 0) >= 2),
        "laptop_screen_on": frac(ok, lambda r: yes(r, "laptop", "screen_on")),
        "laptop_on_while_absent": frac(absent, lambda r: yes(r, "laptop", "screen_on")) if absent else 0.0,
        "safe_door_open": frac(ok, lambda r: yes(r, "safe", "door_open")),
        "safe_key_in_lock": frac(ok, lambda r: yes(r, "safe", "key_in_lock")),
        "door_open": frac(ok, lambda r: yes(r, "door", "door_open")),
        "door_open_while_absent": frac(absent, lambda r: yes(r, "door", "door_open")) if absent else 0.0,
        "drink": frac(ok, lambda r: yes(r, "drink", "is_drink")),
        "phone": frac(ok, lambda r: yes(r, "phone", "is_phone")),
        "personal_bag": frac(ok, lambda r: yes(r, "bag", "is_personal_bag")),
        "cash_visible": frac(ok, lambda r: yes(r, "cash", "is_cash")),
        "cash_while_absent": frac(absent, lambda r: yes(r, "cash", "is_cash")) if absent else 0.0,
        "frames_ok": len(ok), "frames": len(frames),
    }


def compare(per: dict[str, dict], meta: dict[str, dict]) -> dict:
    """По каждому типу: среднее признака у нарушений этого типа, у эталонов и сколько видео типа выше максимума
    эталонов (такой порог не даёт ни одного ложного на эталонах)."""
    ref = [per[v] for v, m in meta.items() if m["label"] != "violation" and per.get(v)]
    keys = [k for k in (ref[0] if ref else {}) if not k.startswith("frames")]
    out = {"reference": {"videos": len(ref), **{k: round(statistics.fmean(x[k] for x in ref), 3) for k in keys}}}
    ref_max = {k: max(x[k] for x in ref) for k in keys} if ref else {}
    types = sorted({m["type"] for m in meta.values() if m["label"] == "violation"})
    for t in types:
        vs = [per[v] for v, m in meta.items() if m["label"] == "violation" and m["type"] == t and per.get(v)]
        if not vs:
            continue
        out[f"type={t or '?'}"] = {"videos": len(vs), **{
            k: {"mean": round(statistics.fmean(x[k] for x in vs), 3),
                "above_all_references": sum(1 for x in vs if x[k] > ref_max[k])} for k in keys}}
    out["reference_max"] = ref_max
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, nargs="+", required=True)
    ap.add_argument("--types", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("outputs/ground"))
    ap.add_argument("--variant", default="4b-q4km")
    ap.add_argument("--endpoint")
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                    default="windows-x64" if sys.platform == "win32" else "linux-x64")
    ap.add_argument("--step", type=float, default=3.0)
    ap.add_argument("--max-frames", type=int, default=10)
    ap.add_argument("--image-max-tokens", type=int, default=256, help="как в production; больше — дороже на 3060")
    args = ap.parse_args()
    videos = [v for p in args.videos for v in load_videos(p)]
    types = {r["id"]: (r.get("type_id") or "").strip() for r in csv.DictReader(args.types.open(encoding="utf-8-sig"))}
    args.out.mkdir(parents=True, exist_ok=True)
    server = None
    if args.endpoint:
        client = LlamaClient(args.endpoint)
    else:
        scfg = server_config(load_manifest(), args.variant, args.platform)
        scfg.image_max_tokens = args.image_max_tokens
        server = LlamaServer(scfg, args.out / "ground-llama-server.log")
        server.start(timeout=600)
        client = server.client
    per, meta, detail = {}, {}, {}
    try:
        for v in videos:
            if v["id"] in meta:
                continue
            job = args.out / "jobs" / v["id"]
            info = probe(Path(v["path"]))
            frames = sample_frames(Path(v["path"]), job / "frames", args.step, info)
            if len(frames) > args.max_frames:
                k = len(frames) / args.max_frames
                frames = [frames[int(i * k)] for i in range(args.max_frames)]
            recs = [frame_facts(client, f, job) for f in frames]
            per[v["id"]] = features(recs)
            meta[v["id"]] = {"label": v.get("source_label"), "type": types.get(v["id"], ""), "role": v["role"]}
            detail[v["id"]] = recs
            print(f"{v['id']}: {per[v['id']].get('frames_ok', 0)}/{len(recs)} frames", flush=True)
    finally:
        if server:
            server.stop()
    rep = compare(per, meta)
    (args.out / "ground.local.json").write_text(json.dumps({"report": rep, "features": per, "meta": meta,
                                                            "frames": detail}, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    ref = rep["reference"]
    print(f"reference videos: {ref['videos']}")
    for t, row in rep.items():
        if not t.startswith("type="):
            continue
        hits = [f"{k} {v['mean']:.2f}/ref {ref[k]:.2f} above={v['above_all_references']}/{row['videos']}"
                for k, v in row.items() if k != "videos" and (v["above_all_references"] or v["mean"] - ref[k] >= 0.2)]
        print(f"{t} ({row['videos']}): " + ("; ".join(hits) if hits else "no separating feature"))
    print(f"local detail: {args.out / 'ground.local.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
