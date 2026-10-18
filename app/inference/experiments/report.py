"""Отчёт по results.json прогонов dev_eval (cash-specialists-v6) без GPU. ТОЛЬКО ДЛЯ ОЦЕНКИ.

Разделы:
  metrics    — TP/FN/FP/TN, recall, precision, F1 отдельно по ролям (train, dev); failed отдельно, не как negative;
  coverage   — пропуски нарушений: тип не покрыт правилами или тип покрыт, но модель не распознала
               (нужен локальный --types CSV «id,type_id» по разбору видео);
  stability  — копии augment.py («<id>~<aug>»): совпадение вердикта с исходным видео;
  dense      — «что если» событие MULTI-01 требует согласия плотной перепроверки (голос через полшага);
  tune       — пороги rules_v6 по trace: выбор только по train, проверка на dev.
В консоль — агрегаты; по видео — <out>/report.local.json.

    python -m app.inference.experiments.report --results outputs/dev_eval/<run>/4b-q4km/results.json \\
        [--types outputs/experiments/types.local.csv] [--tune]
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from pathlib import Path

from .. import rules_v6
from ..rules_v2 import Facts

POS = "violation"


def split_id(vid: str) -> tuple[str, str]:
    base, _, aug = vid.partition("~")
    return base, aug or "orig"


def metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs: (метка, итог). failed не считается ни положительным, ни отрицательным."""
    tp = sum(lab == POS and out == POS for lab, out in pairs)
    fn = sum(lab == POS and out == "no_violation" for lab, out in pairs)
    fp = sum(lab != POS and out == POS for lab, out in pairs)
    tn = sum(lab != POS and out == "no_violation" for lab, out in pairs)
    failed = sum(out.startswith("failed") for _, out in pairs)
    rec = tp / (tp + fn) if tp + fn else None
    prec = tp / (tp + fp) if tp + fp else None
    f1 = 2 * rec * prec / (rec + prec) if rec and prec else 0.0 if rec is not None and prec is not None else None
    r = lambda x: None if x is None else round(x, 3)  # noqa: E731
    return {"videos": len(pairs), "TP": tp, "FN": fn, "FP": fp, "TN": tn, "failed": failed,
            "correct": tp + tn, "accuracy": r((tp + tn) / len(pairs)) if pairs else None,
            "recall": r(rec), "precision": r(prec), "f1": r(f1)}


def _overlap(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _timeline(trace: dict) -> list[Facts]:
    fields = trace["fields"]
    return [Facts(row[0], row[1], dict(zip(fields, row[2:]))) for row in trace["timeline"]]


def recompute(trace: dict, *, require_dense: bool = False, unverified: str = "strict") -> tuple[str, dict]:
    """Вердикт по trace при текущих порогах rules_v6. Проверку MULTI-01 берём из исходного прогона по пересечению
    интервала; новый кандидат без неё: strict — не засчитывается, optimistic — засчитывается."""
    orig = [c for c in trace.get("candidates", []) if c["type_id"] == rules_v6.TYPE_MULTI01]
    dense = trace.get("dense") or []
    stats = {"multi_unverified": 0}
    events = []
    for c in rules_v6.evaluate(_timeline(trace)):
        if c.decision != "event":
            continue
        if c.type_id == rules_v6.TYPE_MULTI01:
            span = [c.interval[0].pts_sec, c.interval[-1].pts_sec]
            ver = [o["checks"].get("multi_verified", {}).get("status") for o in orig if _overlap(o["absent"], span)]
            ver = [v for v in ver if v]
            if not ver:
                stats["multi_unverified"] += 1
                if unverified == "strict":
                    continue
            elif "confirmed" not in ver:
                continue
            if require_dense:
                ds = [d["status"] for d in dense if span[0] - 2.0 <= d["start_sec"] <= span[1]]
                if "confirmed" not in ds:
                    continue
        events.append(c.type_id)
    return (POS if events else "no_violation"), {**stats, "types": events}


def _with_params(**kw):
    old = {k: getattr(rules_v6, k) for k in kw}

    class _Ctx:
        def __enter__(self):
            for k, v in kw.items():
                setattr(rules_v6, k, v)

        def __exit__(self, *a):
            for k, v in old.items():
                setattr(rules_v6, k, v)
    return _Ctx()


def load(paths: list[Path]) -> list[dict]:
    out, seen = [], set()
    for p in paths:
        for r in json.loads(p.read_text(encoding="utf-8")):
            if r["id"] in seen:
                raise SystemExit(f"duplicate video id across results: {r['id']}")
            seen.add(r["id"])
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, nargs="+", required=True)
    ap.add_argument("--types", type=Path, help="локальный CSV id,type_id (тип нарушения по разбору; пусто — эталон)")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--out", type=Path, help="каталог для report.local.json (по умолчанию — рядом с первым results)")
    args = ap.parse_args()
    rs = load(args.results)
    rep: dict = {"note": "binary labels only; failed counted separately; train and dev reported separately"}
    detail: dict = {}

    orig = [r for r in rs if split_id(r["id"])[1] == "orig"]
    roles = sorted({r["role"] for r in orig})
    rep["metrics"] = {role: metrics([(r["source_label"], r["outcome"]) for r in orig if r["role"] == role])
                      for role in roles}
    rep["metrics"]["all"] = metrics([(r["source_label"], r["outcome"]) for r in orig])
    secs = [r["sec"] for r in rs]
    rep["sec_per_video"] = {"mean": round(sum(secs) / len(secs), 1), "max": max(secs)} if secs else None
    budget = [r["trace"].get("budget") for r in rs if r.get("trace") and r["trace"].get("budget")]
    rep["budget_degraded_videos"] = sum(1 for b in budget if b["desk_skipped_from_sec"] is not None or b["stride"] > 1)

    if args.types:
        types = {row["id"]: (row.get("type_id") or "").strip() for row in csv.DictReader(args.types.open(encoding="utf-8-sig"))}
        cov: dict = {}
        for r in orig:
            if r["source_label"] != POS:
                continue
            t = types.get(split_id(r["id"])[0])
            if t is None:
                kind = "type_unknown"
            else:
                covered = t in rules_v6.SUPPORTED_TYPES
                found = r["outcome"] == POS
                got = [e["type_id"] for e in (r.get("result") or {}).get("events", [])]
                kind = ("covered_found" + ("" if t in got else "_wrong_type") if covered and found else
                        "covered_missed_recognition" if covered else
                        "uncovered_found_other_type" if found else "uncovered_missed_coverage")
            if r["outcome"].startswith("failed"):
                kind = "failed"
            key = f"role={r['role']}"
            cov.setdefault(key, {}).setdefault(kind, 0)
            cov[key][kind] += 1
            detail.setdefault("coverage", {})[r["id"]] = kind
        rep["coverage"] = cov

    augs = [r for r in rs if split_id(r["id"])[1] != "orig"]
    if augs:
        by_base = {r["id"]: r for r in orig}
        st: dict = {}
        for r in augs:
            base, aug = split_id(r["id"])
            b = st.setdefault(aug, {"pairs": 0, "same_verdict": 0, "_m": []})
            if base in by_base:
                b["pairs"] += 1
                b["same_verdict"] += r["outcome"] == by_base[base]["outcome"]
            b["_m"].append((r["source_label"], r["outcome"]))
        for aug, b in st.items():
            b["metrics"] = metrics(b.pop("_m"))
        rep["stability"] = st

    traced = [r for r in orig if r.get("trace") and r["trace"].get("timeline")]
    if any((r["trace"].get("dense") or []) for r in traced):
        rep["dense"] = {}
        for req in (False, True):
            for role in roles:
                pairs = [(r["source_label"], recompute(r["trace"], require_dense=req)[0])
                         for r in traced if r["role"] == role]
                rep["dense"][f"require_dense={req} role={role}"] = metrics(pairs)

    if args.tune and traced:
        grid = {"MIN_MULTI_FRAMES": [1, 2, 3, 4], "MIN_ABSENCE_FRAMES": [2, 3, 4, 5], "MIN_DESK_FRAMES": [1, 2, 3]}
        rows = []
        for vals in itertools.product(*grid.values()):
            kw = dict(zip(grid, vals))
            with _with_params(**kw):
                res = {}
                for role in roles:
                    outs = [(r["source_label"], recompute(r["trace"])) for r in traced if r["role"] == role]
                    res[role] = metrics([(lab, o[0]) for lab, o in outs])
                    res[role]["multi_unverified"] = sum(o[1]["multi_unverified"] for _, o in outs)
            rows.append({"params": kw, **res})
        if "train" in roles:
            # выбор по train: больше верных, при равенстве — меньше ложных, затем ближе к текущим порогам
            cur = {k: getattr(rules_v6, k) for k in grid}
            rows.sort(key=lambda x: (-x["train"]["correct"], x["train"]["FP"],
                                     sum(abs(x["params"][k] - cur[k]) for k in grid)))
            best = rows[0]
            rep["tune"] = {"selected_on": "train", "current": cur,
                           "current_result": next(x for x in rows if x["params"] == cur),
                           "best": best, "unverified_note": "new MULTI-01 candidates without a verification call "
                                                            "are not counted (strict)"}
        detail["tune_grid"] = rows

    out_dir = args.out or args.results[0].parent
    (out_dir / "report.local.json").write_text(json.dumps({"report": rep, "detail": detail}, ensure_ascii=False,
                                                          indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"local detail: {out_dir / 'report.local.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
