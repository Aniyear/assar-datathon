"""Dev-прогон анализатора целиком (analyze_video, режим cash01-facts-v1) на заранее выбранных dev-видео.

ТОЛЬКО ДЛЯ ОЦЕНКИ. Production-каталог пуст; здесь в памяти создаётся кандидат CASH-01 из
docs/MVP_RULES_REVIEW.md с пометкой EVAL, чтобы пройти общие проверки результата. Он не записывается в
app/rules/catalog.json и не означает подтверждённой применимости.

Варианты моделей запускаются по очереди (один сервер на GPU):
  --variants 2b-q8,4b-q4km         собственный llama-server по manifest эксперимента;
  --endpoint http://127.0.0.1:PORT уже запущенный OpenAI-совместимый сервер (например, vLLM)
                                   с --label и при необходимости --extra-body.
Результаты по видео (вердикт, события, структура условий) — локально в --out; в консоль только агрегаты.
source_label используется оценщиком и не передаётся в модель.

    python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.local.json --variants 2b-q8,4b-q4km
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

from .. import facts_prompt, facts_v2, facts_v3, facts_v4, rules_v2, rules_v4
from ..analyzer import Analyzer
from ..bench_baseline import VramMonitor
from ..config import load_model_config
from ..vlm import LlamaClient, LlamaServer
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


_SRC = {"source_title": EVAL_RULE["source_title"], "source_url": EVAL_RULE["source_url"],
        "effective_version_checked_at": EVAL_RULE["effective_version_checked_at"], "status": "verified",
        "required_context": [], "counterexamples": []}
EVAL_RULES_V2 = [
    dict(EVAL_RULE, type_id=rules_v2.TYPE_CASH01, rule_id="EVAL-CASH-01-candidate"),
    dict(_SRC, rule_id="EVAL-CASH-02-candidate", type_id=rules_v2.TYPE_CASH02,
         type_name_ru="Пересчёт наличных не в присутствии клиента", clause="п. 20, абзац первый",
         scope="EVAL ONLY: приём и пересчёт наличных от клиента",
         observable_condition="cashier counts cash >= 2 frames with no client at counter; client shortly before",
         claim_template_ru="Это может указывать на пересчёт принятых наличных не в присутствии клиента (п. 20); "
                           "требуется проверка человеком."),
    dict(_SRC, rule_id="EVAL-COLLECT-02-candidate", type_id=rules_v2.TYPE_COLLECT02,
         type_name_ru="Вскрытие инкассаторской сумки без инкассатора", clause="п. 25",
         scope="EVAL ONLY: приём инкассаторской сумки",
         observable_condition="collection bag opened >= 2 frames with no collector; collector shortly before",
         claim_template_ru="Это может указывать на вскрытие и пересчёт сумки не в присутствии работника "
                           "инкассации (п. 25); повреждение сумки по видео не проверялось; требуется проверка "
                           "человеком."),
]


EVAL_RULES_V4 = [EVAL_RULES_V2[0], EVAL_RULES_V2[1],
    dict(_SRC, rule_id="EVAL-SAFE-01-candidate", type_id=rules_v4.TYPE_SAFE01,
         type_name_ru="Сейф оставлен открытым или с ключом при уходе кассира", clause="п. 20 (абзац второй), п. 80",
         scope="EVAL ONLY: хранение наличных денег на рабочем месте кассового работника",
         observable_condition="cashier absent >= 3 frames while safe open or key left",
         claim_template_ru="Это может указывать на нарушение порядка хранения наличных денег в устройствах, "
                           "закрывающихся на ключ, и порядка хранения ключей (пп. 20, 80); требуется проверка "
                           "человеком."),
    dict(_SRC, rule_id="EVAL-ACCESS-01-candidate", type_id=rules_v4.TYPE_ACCESS01,
         type_name_ru="Посторонний в зоне кассира или у сейфа", clause="п. 75, п. 76",
         scope="EVAL ONLY: доступ в кладовую/сейфовую комнату; применимость к помещению подтверждает команда",
         observable_condition="person other than the cashier on the staff side / at the safe >= 2 frames",
         claim_template_ru="Это может указывать на доступ к сейфу или ключам/кодам лица, не ответственного за "
                           "сохранность наличных денег (пп. 75, 76); полномочия по видео не устанавливаются; "
                           "требуется проверка человеком."),
]


def eval_catalog(mode: str = "v1") -> RuleCatalog:
    rules = EVAL_RULES_V4 if mode == "v4" else EVAL_RULES_V2 if mode in ("v2", "v3") else [EVAL_RULE]
    return RuleCatalog.model_validate({"schema_version": "0.1", "rules": rules})


def run_videos(analyzer: Analyzer, videos: list[dict], out: Path, budget_sec: float) -> list[dict]:
    results = []
    for item in videos:
        job = out / "jobs" / item["id"]
        job.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        rec = {"id": item["id"], "role": item["role"], "source_label": item.get("source_label")}
        try:
            res = analyzer.analyze_video(Path(item["path"]).resolve(), job.resolve(), time.monotonic() + budget_sec)
            rec.update(outcome=res.verdict, result=res.model_dump())
        except AnalysisError as e:
            rec.update(outcome=f"failed:{type(e).__name__}")
        rec["trace"] = analyzer.last_trace
        rec["sec"] = round(time.monotonic() - t0, 3)
        results.append(rec)
    return results


def aggregate(results: list[dict]) -> dict:
    groups: dict[str, dict] = {}
    for r in results:
        cands = (r.get("trace") or {}).get("candidates", [])
        tl = (r.get("trace") or {}).get("timeline", [])
        for g in ("all", f"label={r['source_label'] or 'unknown'}", f"role={r['role']}"):
            b = groups.setdefault(g, {"videos": 0, "outcomes": {}, "label_agreement": 0, "failed": 0,
                                      "events": 0, "candidates": {"event": 0, "excluded": 0, "insufficient": 0},
                                      "frames": 0, "frames_unknown_presence": 0})
            b["videos"] += 1
            b["outcomes"][r["outcome"]] = b["outcomes"].get(r["outcome"], 0) + 1
            b["label_agreement"] += r["outcome"] == r["source_label"]
            b["failed"] += r["outcome"].startswith("failed")
            b["events"] += len((r.get("result") or {}).get("events", []))
            for c in cands:
                b["candidates"][c["decision"]] += 1
                if c["decision"] == "event":
                    t = c.get("type_id", "cashier_leaves_during_service")
                    et = b.setdefault("event_types", {})
                    et[t] = et.get(t, 0) + 1
            b["frames"] += len(tl)
            b["frames_unknown_presence"] += sum(1 for f in tl if "unknown" in (f[2], f[3]))
    for b in groups.values():
        b["failed_share"] = round(b["failed"] / b["videos"], 3) if b["videos"] else None
    secs = [r["sec"] for r in results]
    return {"groups": groups, "video_sec_mean": round(statistics.fmean(secs), 3) if secs else None,
            "video_sec_max": max(secs) if secs else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--variants", default="4b-q4km", help="через запятую из model_compare.json")
    ap.add_argument("--endpoint", help="внешний OpenAI-совместимый сервер на 127.0.0.1 вместо --variants")
    ap.add_argument("--label", default="external", help="имя варианта для --endpoint")
    ap.add_argument("--extra-body", default="", help='JSON, добавляемый в запрос (например, model/chat_template_kwargs)')
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                    default="windows-x64" if sys.platform == "win32" else "linux-x64")
    ap.add_argument("--gpu", type=int, default=0, help="индекс GPU для замера памяти")
    ap.add_argument("--mode", choices=["v1", "v2", "v3", "v4"], default="v4",
                    help="v1: CASH-01; v2: CASH-01/02, COLLECT-02 (6 полей на кадр); v3: то же, действия списком")
    ap.add_argument("--max-tokens", type=int, default=0, help="0 = 512 для v1/v3, 1024 для v2")
    ap.add_argument("--call-timeout", type=float, default=120.0)
    ap.add_argument("--budget-sec", type=float, default=240.0)
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "dev_eval")
    args = ap.parse_args()

    videos = load_videos(args.videos)
    manifest = load_manifest()
    base = load_model_config(ROOT / "configs/model.yaml")
    run_root = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    extra = json.loads(args.extra_body) if args.extra_body else None
    prompt_version = {"v1": facts_prompt.FACTS_PROMPT_VERSION, "v2": facts_v2.PROMPT_VERSION,
                      "v3": facts_v3.PROMPT_VERSION, "v4": facts_v4.PROMPT_VERSION}[args.mode]
    args.max_tokens = args.max_tokens or {"v1": 512, "v2": 1024, "v3": 512, "v4": 900}[args.mode]
    plan = [(args.label, None)] if args.endpoint else [(v.strip(), v.strip()) for v in args.variants.split(",")]
    report = {"prompt_version": prompt_version, "max_tokens": args.max_tokens,
              "rules": [r["rule_id"] for r in eval_catalog(args.mode).model_dump()["rules"]] + ["(EVAL, not production)"], "videos": len(videos), "variants": {},
              "note": "label_agreement compares binary source labels only (type/timing not verified); "
                      "failed is counted separately, never as negative; coverage = rules listed"}
    for label, variant in plan:
        out = run_root / label
        out.mkdir(parents=True, exist_ok=True)
        cfg = dataclasses.replace(base, prompt_version=prompt_version, max_tokens=args.max_tokens)
        if variant:
            v = manifest["variants"][variant]
            cfg = dataclasses.replace(cfg, server=server_config(manifest, variant, args.platform), model_id=v["repo"],
                                      model_revision=v["revision"], quantization=v["weights"]["quantization"],
                                      mmproj_quantization=v["mmproj"]["quantization"])
        else:
            cfg = dataclasses.replace(cfg, model_id=label, model_revision="external", quantization="external",
                                      mmproj_quantization="external")
        before = VramMonitor.snapshot(args.gpu)
        mon = VramMonitor(args.gpu)
        mon.start()
        server = None
        rep = {"model": cfg.revision_label, "gpu": before["name"], "vram_before_mib": before["used_mib"]}
        try:
            if variant:
                server = LlamaServer(cfg.server, out / "llama-server.log")
                rep["load_sec"] = round(server.start(timeout=600), 3)
                client = server.client
            else:
                client = LlamaClient(args.endpoint)
            analyzer = Analyzer(cfg, eval_catalog(args.mode), client, call_timeout_sec=args.call_timeout)
            analyzer.extra_body = extra
            results = run_videos(analyzer, videos, out, args.budget_sec)
        finally:
            if server:
                server.stop()
            time.sleep(0.3)
            mon.stop()
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        rep.update(aggregate(results), vram_peak_mib=mon.peak, vram_peak_delta_mib=mon.peak - before["used_mib"])
        report["variants"][label] = rep
    (run_root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"local run dir: {run_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
