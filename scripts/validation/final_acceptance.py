"""Local-only train/dev HTTP acceptance. Prints aggregates, never scenes or credentials.

Uses the accepted config/catalog with real service, supervisor and HTTP API. The
explicit acceptance RuntimeSpec is needed before production readiness is earned;
it changes no model flags or outputs. Frozen test is not accepted as input.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.gpu_smoke import (append_metadata, gpu_snapshot, live_api, memory_by_device,
                                          suspend_owned_server, write_json)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def selection(split_csv, data_root):
    """Only exported train/dev rows; no frozen manifest or scene annotations."""
    from app.inference.video import probe
    rows = list(csv.DictReader(split_csv.open(encoding="utf-8-sig")))
    if not rows or any(r["split"] not in ("train", "dev") for r in rows):
        raise ValueError("only the train/dev export is allowed")
    groups = {}
    for row in sorted(rows, key=lambda r: (r["canonical_id"] != "X-4", r["canonical_id"])):
        if row["group_id"] in groups:
            if groups[row["group_id"]]["split"] != row["split"]:
                raise ValueError("group crosses split")
            continue
        path = (data_root / row["relative_path"]).resolve(strict=True)
        if not path.is_relative_to(data_root) or digest(path) != row["sha256"]:
            raise ValueError("train/dev file integrity failed")
        info = probe(path)
        groups[row["group_id"]] = dict(row, path=str(path), duration_sec=info.duration_sec)
    items = list(groups.values())
    dev = [dict(r, role="diagnostic" if r["canonical_id"] == "X-4" else "dev")
           for r in items if r["split"] == "dev"]
    counts = Counter((r["role"], r["verdict"]) for r in dev)
    if counts != Counter({("dev", "violation"): 3, ("dev", "no_violation"): 4,
                          ("diagnostic", "violation"): 1}):
        raise ValueError("dev export differs from handed-off 7 dev + 1 diagnostic")
    longest = dict(max(items, key=lambda r: r["duration_sec"]), role="longest")
    return dev + [longest], len(items)


def observed_factory(*, metadata, **kwargs):
    """Observe the actual adapter; no replacement of inference, conditions or results."""
    from app.inference import analyzer as module, service
    current = {}
    base_analyzer, base_client, base_sample = service.Analyzer, service.LlamaClient, module.sample_frames

    class Client(base_client):
        def chat(self, *args, **kw):
            start = time.monotonic()
            rec = {"kind": "chat", "job": current.get("job"), "timeout": kw["timeout"]}
            append_metadata(metadata, dict(rec, kind="chat_start"))
            try:
                response = super().chat(*args, **kw)
                rec["finish_reason"] = response["choices"][0].get("finish_reason")
                return response
            except Exception as exc:
                rec["error_class"] = type(exc).__name__
                raise
            finally:
                rec["elapsed_sec"] = time.monotonic() - start
                append_metadata(metadata, rec)

    class ObservedAnalyzer(base_analyzer):
        def analyze_video(self, video_path, job_dir, deadline_monotonic):
            current["job"] = job_dir.name
            rec = {"kind": "analysis", "job": job_dir.name}
            def sampled(*args, **kw):
                frames = base_sample(*args, **kw)
                rec["sampled_pts"] = [f.pts_sec for f in frames]
                return frames
            try:
                with patch.object(module, "sample_frames", sampled):
                    result = super().analyze_video(video_path, job_dir, deadline_monotonic)
                rec["mandatory_complete"] = bool(self.last_trace and self.last_trace.get("mandatory_analysis_complete"))
                rec["limitations"] = result.limitations
                rec["evidence_pts_match"] = all(
                    any(abs(e.timestamp_sec-p) < .001 for p in rec.get("sampled_pts", []))
                    for event in result.events for e in event.evidence)
                return result
            finally:
                # Full trace is private/local. No content is printed or sent to telemetry services.
                rec["trace"] = self.last_trace
                append_metadata(metadata, rec)

    with patch.object(service, "Analyzer", ObservedAnalyzer), patch.object(service, "LlamaClient", Client):
        analyze = service.create_analyzer(**kwargs)
    append_metadata(metadata, {"kind": "factory", "call_timeout_sec": kwargs["call_timeout_sec"]})
    return analyze


def aggregate(jobs, role):
    out = dict(TP=0, FN=0, FP=0, TN=0, failed=0)
    for job in jobs:
        if job["role"] != role:
            continue
        if job["status"] != "done":
            out["failed"] += 1
        else:
            positive, predicted = job["expected"] == "violation", job["verdict"] == "violation"
            out[("T" if positive == predicted else "F") + ("P" if predicted else "N")] += 1
    return out


def run(folder, cfg, items):
    from app.api.main import create_app
    from app.contracts import DoneJob, JOB_RESPONSE_ADAPTER, RuleCatalog, validate_done_for_video
    from app.inference import service
    from app.jobs.procs import pid_alive
    from app.jobs.supervisor import RuntimeSpec
    from app.settings import Settings
    from tests.api.harness import make_mov

    key = secrets.token_urlsafe(32)
    settings = Settings(api_key=key, uploads_dir=folder / "uploads", outputs_dir=folder / "service")
    cat = RuleCatalog.model_validate_json((ROOT / "app/rules/catalog.json").read_text(encoding="utf-8"))
    metadata = folder / "private-metadata.jsonl"
    spec = RuntimeSpec("scripts.validation.final_acceptance:observed_factory",
                       {"model_cfg": cfg, "catalog": cat, "repo_root": ROOT, "call_timeout_sec": 120.0,
                        "metadata": str(metadata)}, cat, lambda port: service.server_command(cfg, ROOT, port),
                       settings.outputs_dir / "logs")
    app = create_app(settings, lambda _: spec)
    receipt = {"jobs": [], "checks": {}, "gpu_samples": []}
    owned = set()
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            receipt["gpu_samples"].append({"monotonic": time.monotonic(), **gpu_snapshot()})
            stop.wait(1)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        with live_api(app, 400) as api:
            supervisor = app.state.service.supervisor
            initial = supervisor.runtime_pids()
            owned.update(initial.values())
            receipt["checks"]["static_and_auth"] = (
                "eventNavigator" in api.request("GET", "/")[1]
                and api.request("GET", "/api/internal/status")[0] == 401)

            def submit(item):
                start_pids = supervisor.runtime_pids()
                # Read bytes before timing: the measured interval is HTTP POST -> terminal response.
                clip = Path(item["path"]).read_bytes()
                start = time.monotonic()
                code, accepted = api.upload(key, clip, filename="upload.mov")
                if code != 202:
                    raise RuntimeError(f"upload HTTP {code}")
                job_id = accepted["job_id"]
                public = api.wait_terminal(key, job_id, timeout=305)
                elapsed = time.monotonic() - start
                write_json(folder / f"private-result-{job_id}.json", public)
                validated = JOB_RESPONSE_ADAPTER.validate_python(public)
                if isinstance(validated, DoneJob):
                    validate_done_for_video(validated, float(item["duration_sec"]))
                    for event in validated.violations:
                        rule = next(r for r in cat.rules if r.type_name_ru == event.type)
                        if rule.source_url not in event.description or rule.claim_template_ru not in event.description:
                            raise ValueError("source or normative caveat lost")
                with sqlite3.connect(settings.outputs_dir / "jobs.sqlite3") as db:
                    reason = db.execute("SELECT reason FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
                api.wait_accepting(key, timeout=220)
                pids = supervisor.runtime_pids()
                owned.update(pids.values())
                rec = {"job_id": job_id, "role": item["role"], "expected": item.get("verdict"),
                       "status": public["status"], "verdict": public.get("verdict"), "reason": reason,
                       "elapsed_sec": elapsed, "duration_sec": item["duration_sec"],
                       "reused": pids == start_pids, "pids": pids,
                       "events": len(public.get("violations", [])),
                       "jpeg_count": sum(len(e["evidence_frames"]) for e in public.get("violations", [])),
                       "cleanup_persistence": not (settings.uploads_dir / job_id).exists()
                       and not (settings.outputs_dir / "jobs" / job_id).exists()
                       and api.request("GET", f"/api/v1/jobs/{job_id}", key) == (200, public)}
                receipt["jobs"].append(rec)
                write_json(folder / "private-progress.json", receipt)
                print(f"completed_requests={len(receipt['jobs'])}; details saved locally", flush=True)
                return rec

            for item in items:
                submit(item)
            successful = [item for item, result in zip(items, receipt["jobs"]) if result["status"] == "done"]
            if not successful:
                raise RuntimeError("no successful real result available for repeat/recovery")
            repeat = dict(successful[0], role="repeat")
            submit(repeat)
            # True owned-process fault: the real 120 s per-call timeout triggers recovery,
            # bounded by the unchanged 240 s job budget and 300 s request deadline.
            old = supervisor.runtime_pids()
            suspend_owned_server(supervisor)
            synthetic = make_mov(folder / "synthetic-timeout.mov")
            timeout = submit({"role": "timeout", "path": str(synthetic), "duration_sec": 3.0})
            new = supervisor.runtime_pids()
            receipt["checks"]["timeout_recovery"] = (
                timeout["status"] == "failed" and timeout["reason"] in ("AnalysisTimeoutError", "WatchdogTimeout")
                and timeout["elapsed_sec"] < 300 and all(new[k] != old[k] for k in old)
                and all(not pid_alive(p) for p in old.values() if p))
            recovered = submit(dict(successful[0], role="recovered"))
            receipt["checks"]["real_job_after_recovery"] = recovered["status"] == "done"
    finally:
        stop.set()
        thread.join(timeout=25)
        receipt["owned_pids"] = sorted(p for p in owned if p)
        receipt["checks"]["owned_processes_stopped"] = bool(owned) and all(not pid_alive(p) for p in owned if p)
        write_json(folder / "private-run.json", receipt)
    records = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines()]
    analysis = {r["job"]: r for r in records if r["kind"] == "analysis"}
    normal = [j for j in receipt["jobs"] if j["role"] != "timeout"]
    receipt["checks"].update(
        all_real_jobs_done=bool(normal) and all(j["status"] == "done" for j in normal),
        under_300_seconds=all(j["elapsed_sec"] < 300 for j in receipt["jobs"]),
        persisted_after_cleanup=all(j["cleanup_persistence"] for j in receipt["jobs"]),
        process_reuse=all(j["reused"] for j in normal if j["status"] == "done"),
        real_positive_jpeg=any(j["status"] == "done" and j["expected"] == "violation"
                               and j["verdict"] == "violation" and j["jpeg_count"] for j in normal),
        real_completed_negative=any(j["status"] == "done" and j["expected"] == "no_violation"
                                   and j["verdict"] == "no_violation" for j in normal),
        pts_match=all(analysis.get(j["job_id"], {}).get("evidence_pts_match") for j in normal if j["status"] == "done"),
        negative_checks_complete=all(analysis.get(j["job_id"], {}).get("mandatory_complete")
                                    for j in normal if j["verdict"] == "no_violation"),
        finish_reason=all(r.get("finish_reason") == "stop" for r in records
                          if r["kind"] == "chat" and "error_class" not in r))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-dev-csv", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--weights-root", type=Path, required=True)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--run-gpu", action="store_true")
    args = parser.parse_args()
    from app.inference import service
    from app.settings import configure_offline_environment
    configure_offline_environment()
    folder = ROOT / "outputs/final-acceptance" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    folder.mkdir(parents=True)
    summary = {"status": "prepared", "checks": {}}
    try:
        items, count = selection(args.train_dev_csv.resolve(), args.data_root.resolve())
        write_json(folder / "private-selection.json", items)
        cfg = json.loads((ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
        for section, field, root in (("weights", "path", args.weights_root), ("mmproj", "path", args.weights_root),
                                     ("runtime", "server_bin", args.server_root)):
            cfg[section][field] = str((root / cfg[section][field]).resolve())
        service.check_runtime(cfg, ROOT)
        write_json(folder / "resolved-model.json", cfg)
        summary.update(runtime_sha256_verified=True, train_dev_groups=count,
                       dev_count=7, diagnostic_count=1, longest_duration_sec=items[-1]["duration_sec"],
                       train_dev_csv_sha256=digest(args.train_dev_csv), config_sha256=digest(ROOT / "configs/model.yaml"),
                       catalog_sha256=digest(ROOT / "app/rules/catalog.json"),
                       source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                       ml_handoff="8311a626f85d650dfa7d78a40f96fd1b4d5947b7")
        summary["code_sha256"] = {str(p.relative_to(ROOT)): digest(p) for p in
                                  [ROOT / "app/inference/analyzer.py", ROOT / "app/jobs/serialize.py",
                                   ROOT / "scripts/validation/final_acceptance.py"]}
        if args.run_gpu:
            processes = subprocess.run(["powershell", "-NoProfile", "-Command",
                                         "@(Get-Process llama-server,ollama -ErrorAction SilentlyContinue).Count"],
                                        capture_output=True, text=True, timeout=15)
            if processes.stdout.strip() != "0":
                raise RuntimeError("conflicting model process; nothing stopped")
            summary["gpu_before"] = gpu_snapshot()
            result = run(folder, cfg, items)
            summary["checks"] = result["checks"]
            summary["dev"] = aggregate(result["jobs"], "dev")
            summary["diagnostic"] = aggregate(result["jobs"], "diagnostic")
            summary["longest"] = aggregate(result["jobs"], "longest")
            summary["max_http_elapsed_sec"] = max(j["elapsed_sec"] for j in result["jobs"])
            summary["max_real_http_elapsed_sec"] = max(j["elapsed_sec"] for j in result["jobs"] if j["role"] != "timeout")
            memories = [v for sample in result["gpu_samples"] for v in memory_by_device(sample).values()]
            summary["whole_gpu_sampled_max_mib"] = max(memories) if memories else None
            summary["gpu_sampling"] = "nvidia-smi every 1 second plus query duration; whole GPU, not continuous peak or process-only"
            before = memory_by_device(summary["gpu_before"])
            for _ in range(15):
                summary["gpu_after"] = gpu_snapshot()
                after = memory_by_device(summary["gpu_after"])
                released = bool(before) and before.keys() == after.keys() and all(after[k] <= before[k] + 256 for k in before)
                if released:
                    break
                time.sleep(1)
            summary["checks"]["memory_released"] = released
            summary["status"] = "technical_acceptance_passed" if all(summary["checks"].values()) else "acceptance_failed"
    except Exception as exc:
        summary.update(status="acceptance_failed", error_class=type(exc).__name__)
        summary["error_trace"] = [{"file": Path(x.filename).name, "line": x.lineno, "function": x.name}
                                  for x in traceback.extract_tb(exc.__traceback__)]
    finally:
        write_json(folder / "receipt.json", summary)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    print("receipt=" + str((folder / "receipt.json").relative_to(ROOT)), flush=True)
    return 0 if summary["status"] in ("prepared", "technical_acceptance_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
