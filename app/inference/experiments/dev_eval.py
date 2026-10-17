"""Dev-прогон анализатора целиком (analyze_video) на заранее выбранных dev-видео с настоящим llama-server.

ТОЛЬКО ДЛЯ ОЦЕНКИ. Production-каталог пуст; здесь в памяти создаётся кандидат CASH-01 из
docs/MVP_RULES_REVIEW.md с пометкой EVAL, чтобы пройти общие проверки результата. Он не записывается в
app/rules/catalog.json и не означает подтверждённой применимости. Результаты по видео — локально в --out;
в консоль только агрегаты. source_label используется оценщиком и не передаётся в модель.

    python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.local.json --variant 4b-q4km
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from app.contracts import AnalysisError, RuleCatalog

from .. import facts_prompt
from ..analyzer import Analyzer
from ..bench_baseline import VramMonitor
from ..config import load_model_config
from ..vlm import LlamaServer
from .compare import load_videos, server_config
from .fetch import ROOT, load_manifest

EVAL_RULE = {
    "rule_id": "EVAL-CASH-01-candidate", "type_id": facts_prompt.SUPPORTED_TYPE_ID,
    "type_name_ru": "Уход кассира с рабочего места до окончания обслуживания клиента",
    "source_title": "Правила осуществления кассовых операций… (пост. Правления НБРК от 29.11.2019 № 231)",
    "source_url": "https://adilet.zan.kz/rus/docs/V1900019680", "clause": "п. 22",
    "effective_version_checked_at": "EVAL ONLY: consolidated text 12.07.2026 per MVP_RULES_REVIEW.md",
    "scope": "EVAL ONLY: cash acceptance service of a client at a bank cash desk",
    "observable_condition": "cashier absent >= 3 sampled frames while client stays; service before and after",
    "required_context": ["fixed camera at a cash desk", "service visible before and after the absence"],
    "counterexamples": ["standing/bending at the workplace", "client also left", "service not visible"],
    "claim_template_ru": "Это может указывать на уход кассового работника до окончания обслуживания клиента "
                         "(п. 22); требуется проверка человеком.",
    "status": "verified"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--variant", default="2b-q8")
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                    default="windows-x64" if sys.platform == "win32" else "linux-x64")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--budget-sec", type=float, default=240.0)
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "dev_eval")
    args = ap.parse_args()

    videos = load_videos(args.videos)
    manifest = load_manifest()
    scfg = server_config(manifest, args.variant, args.platform)
    base = load_model_config(ROOT / "configs/model.yaml")
    v = manifest["variants"][args.variant]
    cfg = dataclasses.replace(base, server=scfg, prompt_version=facts_prompt.FACTS_PROMPT_VERSION,
                              max_tokens=args.max_tokens, model_id=v["repo"], model_revision=v["revision"],
                              quantization=v["weights"]["quantization"],
                              mmproj_quantization=v["mmproj"]["quantization"])
    catalog = RuleCatalog.model_validate({"schema_version": "0.1", "rules": [EVAL_RULE]})
    run_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    before = VramMonitor.snapshot()
    mon = VramMonitor()
    mon.start()
    server = LlamaServer(scfg, run_dir / "llama-server.log")
    results, summary = [], {"variant": args.variant, "prompt_version": cfg.prompt_version,
                            "max_tokens": cfg.max_tokens, "rule": "EVAL-CASH-01-candidate (not production)",
                            "gpu": before["name"], "vram_before_mib": before["used_mib"]}
    try:
        summary["load_sec"] = round(server.start(), 3)
        analyzer = Analyzer(cfg, catalog, server.client, call_timeout_sec=120.0)
        for item in videos:
            job = run_dir / "jobs" / item["id"]
            job.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()
            rec = {"id": item["id"], "role": item["role"], "source_label": item.get("source_label")}
            try:
                res = analyzer.analyze_video(Path(item["path"]).resolve(), job.resolve(),
                                             time.monotonic() + args.budget_sec)
                rec.update(outcome=res.verdict, result=res.model_dump())
            except AnalysisError as e:
                rec.update(outcome=f"failed:{type(e).__name__}")
            rec["sec"] = round(time.monotonic() - t0, 3)
            results.append(rec)
    finally:
        server.stop()
        time.sleep(0.3)
        mon.stop()
    (run_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    groups: dict[str, dict] = {}
    for r in results:
        for g in ("all", f"label={r['source_label'] or 'unknown'}", f"role={r['role']}"):
            b = groups.setdefault(g, {"videos": 0, "outcomes": {}, "label_agreement": 0, "events": 0})
            b["videos"] += 1
            b["outcomes"][r["outcome"]] = b["outcomes"].get(r["outcome"], 0) + 1
            b["label_agreement"] += r["outcome"] == r["source_label"]
            b["events"] += len(r.get("result", {}).get("events", []))
    secs = [r["sec"] for r in results]
    summary.update(groups=groups, video_sec_mean=round(statistics.fmean(secs), 3) if secs else None,
                   video_sec_max=max(secs) if secs else None, vram_peak_mib=mon.peak,
                   note="label_agreement compares binary source labels only; violation type and timing are "
                        "not verified by labels; failed is counted separately and never as negative")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"local run dir: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
