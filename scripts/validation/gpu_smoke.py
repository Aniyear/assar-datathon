"""Explicit TEST-ONLY GPU lifecycle harness. Default is preparation, never model startup.

Uses real service, supervisor and HTTP API with a separate synthetic catalog.
No production readiness/validation edits, dataset input, download or remote calls.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.settings import Settings, configure_offline_environment


CATALOG_PATH = ROOT / "tests/fixtures/gpu_smoke/catalog.json"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def append_metadata(path, record):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), **record}) + "\n")


def observed_create_analyzer(*, telemetry_path, **kwargs):
    """Transparent test observer: returns the real service callable, never alters a reply.

    Only timings, finish_reason, schema validity and exception CLASS are recorded.
    No prompt, content, observation, image or exception text is logged here.
    """
    from app.inference import prompts, service

    client_class = service.LlamaClient
    current = {}

    class ObservedClient(client_class):
        def chat(self, parts, schema, **options):
            start = time.monotonic()
            record = {"kind": "chat", "job": current.get("job"), "timeout": options["timeout"]}
            append_metadata(telemetry_path, {**record, "kind": "chat_start"})
            try:
                response = super().chat(parts, schema, **options)
                choice = response["choices"][0]
                record["finish_reason"] = choice.get("finish_reason")
                indices = [int(m.group(1)) for part in parts if isinstance(part, str)
                           for m in [re.match(r"Frame (\d+), t=", part)] if m]
                try:
                    record["schema_valid"] = prompts.validate(json.loads(choice["message"]["content"]), indices)
                except (ValueError, TypeError, KeyError):
                    record["schema_valid"] = False
                return response
            except Exception as exc:
                record["error_class"] = type(exc).__name__
                raise
            finally:
                record["elapsed_sec"] = time.monotonic() - start
                append_metadata(telemetry_path, record)

    # The real factory creates an actual Analyzer and actual local HTTP client.
    # Restore the module name immediately; the bound client retains the observer.
    with patch.object(service, "LlamaClient", ObservedClient):
        analyze = service.create_analyzer(**kwargs)
    append_metadata(telemetry_path, {"kind": "factory", "call_timeout_sec": kwargs["call_timeout_sec"]})

    def observed(video_path, job_dir, deadline_monotonic):
        current["job"] = job_dir.name
        append_metadata(telemetry_path, {"kind": "analysis", "job": job_dir.name,
                                         "deadline_remaining": deadline_monotonic - time.monotonic()})
        return analyze(video_path, job_dir, deadline_monotonic)

    return observed


def smoke_spec(settings, cfg, runtime_root, telemetry):
    from app.contracts import RuleCatalog
    from app.inference import service
    from app.jobs.supervisor import RuntimeSpec

    cat = RuleCatalog.model_validate_json(CATALOG_PATH.read_text(encoding="utf-8"))
    if not cat.rules or any(not r.rule_id.startswith("SYNTH-GPU-") or
                            r.source_url != "https://example.invalid/gpu-smoke" for r in cat.rules):
        raise ValueError("Only the committed SYNTHETIC GPU test catalog is allowed")
    return RuntimeSpec(
        "scripts.validation.gpu_smoke:observed_create_analyzer",
        {"model_cfg": cfg, "catalog": cat, "repo_root": runtime_root,
         "call_timeout_sec": float(settings.model_call_timeout_sec), "telemetry_path": str(telemetry)},
        cat, lambda port: service.server_command(cfg, runtime_root, port), settings.outputs_dir / "logs")


@contextmanager
def live_api(app, startup_timeout):
    from tests.api.harness import LiveServer

    live = LiveServer(app)
    live.thread.start()
    try:
        limit = time.monotonic() + startup_timeout
        while not live.server.started:
            if not live.thread.is_alive() or time.monotonic() >= limit:
                raise RuntimeError("technical GPU API startup failed or timed out")
            time.sleep(0.05)
        live.base = f"http://127.0.0.1:{live.server.servers[0].sockets[0].getsockname()[1]}"
        yield live
    finally:
        live.server.should_exit = True
        live.thread.join(timeout=startup_timeout)
        if live.thread.is_alive():
            raise RuntimeError("API shutdown not confirmed; inspect local owned-process receipt")


def gpu_snapshot():
    """Read-only device/process metadata; never terminate an unowned GPU process."""
    rows = {}
    for name, query in (("gpu", "--query-gpu=index,name,driver_version,memory.used"),
                        ("apps", "--query-compute-apps=pid,used_memory")):
        try:
            result = subprocess.run(["nvidia-smi", query, "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            rows[name] = result.stdout.strip().splitlines() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            rows[name] = None
    return rows


def memory_by_device(snapshot):
    try:
        return {row.split(",")[0].strip(): int(row.rsplit(",", 1)[1]) for row in snapshot["gpu"]}
    except (TypeError, ValueError, IndexError):
        return {}


def suspend_owned_server(supervisor):
    """Windows-only deterministic stall of THIS supervisor's server, using its live handle.

    Recovery/finally kills that owned tree. No PID lookup or process-name matching.
    """
    if sys.platform != "win32":
        raise RuntimeError("this fault injection is validated structurally for Windows only")
    proc = supervisor._server.proc
    if proc is None or proc.poll() is not None:
        raise RuntimeError("owned model server is not alive")
    suspend = ctypes.WinDLL("ntdll").NtSuspendProcess
    suspend.argtypes, suspend.restype = [ctypes.c_void_p], ctypes.c_long
    if suspend(int(proc._handle)) != 0:
        raise RuntimeError("could not suspend owned server")


def phase(folder, cfg, runtime_root, key, clip, *, deadline_fault=False, call_timeout=17):
    from app.api.main import create_app
    from app.contracts import DoneJob, JOB_RESPONSE_ADAPTER, validate_done_for_video
    from app.jobs.procs import pid_alive

    folder.mkdir()
    settings = Settings(api_key=key, uploads_dir=folder / "uploads", outputs_dir=folder / "outputs",
                        model_call_timeout_sec=call_timeout, model_start_timeout_sec=180,
                        model_stop_grace_sec=5, analysis_budget_sec=2 if deadline_fault else 240)
    telemetry = folder / "metadata.jsonl"
    spec = smoke_spec(settings, cfg, runtime_root, telemetry)
    app = create_app(settings, lambda _: spec, web_dir=None)
    result = {"jobs": [], "checks": {}, "settings_call_timeout": call_timeout}
    all_pids = set()
    # Persist progress even if startup, polling or a check fails.
    try:
        with live_api(app, 400) as live:
            supervisor = app.state.service.supervisor
            initial = supervisor.runtime_pids()
            all_pids.update(p for p in initial.values() if p)
            result["initial_pids"] = initial
            result["gpu_loaded"] = gpu_snapshot()
            if deadline_fault:
                suspend_owned_server(supervisor)  # Intentionally stalls only our model process.
            for number in range(1 if deadline_fault else 2):
                start = time.monotonic()
                code, accepted = live.upload(key, clip)
                if code != 202:
                    raise RuntimeError(f"upload not accepted: HTTP {code}")
                job_id = accepted["job_id"]
                public = live.wait_terminal(key, job_id, timeout=300)
                elapsed = time.monotonic() - start
                # Actual response stays local. Never print recognized content to the console.
                write_json(folder / f"result-{number}.json", public)
                validated = JOB_RESPONSE_ADAPTER.validate_python(public)
                if isinstance(validated, DoneJob):
                    validate_done_for_video(validated, 3.0)
                with sqlite3.connect(settings.outputs_dir / "jobs.sqlite3") as db:
                    reason = db.execute("SELECT reason FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
                live.wait_accepting(key, timeout=220)
                after = supervisor.runtime_pids()
                all_pids.update(p for p in after.values() if p)
                cleanup_ok = not (settings.uploads_dir / job_id).exists() and not (
                    settings.outputs_dir / "jobs" / job_id).exists()
                identical = live.request("GET", f"/api/v1/jobs/{job_id}", key) == (200, public)
                result["jobs"].append({"status": public["status"], "reason": reason, "elapsed_sec": elapsed,
                                       "cleanup_and_persistence": cleanup_ok and identical, "pids_after": after})
                if deadline_fault:
                    result["checks"]["deadline_failed_bounded"] = (
                        public["status"] == "failed" and reason in ("WatchdogTimeout", "AnalysisTimeoutError")
                        and elapsed < 8)
                    result["checks"]["restart_old_pids_dead"] = (
                        all(after[k] != initial[k] for k in initial)
                        and all(not pid_alive(p) for p in initial.values() if p))
                else:
                    result["checks"][f"reuse_{number}"] = after == initial
            records = [json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()]
            chats = [r for r in records if r["kind"] == "chat"]
            result["chat_metadata"] = chats
            result["checks"]["cleanup_and_persistence"] = all(j["cleanup_and_persistence"] for j in result["jobs"])
            factories = [r for r in records if r["kind"] == "factory"]
            result["checks"]["factory_timeout"] = bool(factories) and all(
                r["call_timeout_sec"] == float(call_timeout) for r in factories)
            if not deadline_fault:
                result["checks"]["finish_reason_and_schema"] = bool(chats) and all(
                    r.get("finish_reason") == "stop" and r.get("schema_valid") is True for r in chats)
                result["checks"]["terminal_policy"] = all(
                    j["status"] == "done" or j["reason"] == "UnassessableVideoError" for j in result["jobs"])
            # In the forced deadline case a killed worker may not flush its final chat record.
            # Require entry into the real adapter, but never fabricate a finish_reason for it.
            else:
                result["checks"]["adapter_entered"] = any(r["kind"] == "analysis" for r in records)
                result["checks"]["model_request_entered"] = any(r["kind"] == "chat_start" for r in records)
    finally:
        result["owned_pids"] = sorted(all_pids)
        result["checks"]["owned_pids_stopped"] = bool(all_pids) and all(not pid_alive(p) for p in all_pids)
        write_json(folder / "phase.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-gpu", action="store_true", help="Explicitly starts real GPU processes; separate user authorization required")
    parser.add_argument("--runtime-root", type=Path, default=ROOT,
                        help="Root containing the pinned models/ tree; does not copy or download weights")
    parser.add_argument("--call-timeout-sec", type=int, default=17)
    args = parser.parse_args()
    if args.call_timeout_sec <= 2:
        parser.error("call timeout must exceed the 2 s forced deadline")
    configure_offline_environment()
    from app.inference import service
    from tests.api.harness import make_mov

    folder = ROOT / "outputs" / "gpu-smoke" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    folder.mkdir(parents=True)
    cfg_path = ROOT / "configs/model.yaml"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    clip = make_mov(folder / "SYNTHETIC-test-pattern.mov").read_bytes()
    key = secrets.token_urlsafe(32)  # Kept only in memory, never printed.
    runtime_root = args.runtime_root.resolve()
    # Build the exact test spec without starting a process, even in preparation mode.
    smoke_spec(Settings(api_key=key), cfg, runtime_root, folder / "metadata.jsonl")
    receipt = {"purpose": "SYNTHETIC technical smoke; NOT recognition quality or production readiness",
               "status": "prepared_not_run", "gpu_launch_attempted": False,
               "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                         text=True, check=True).stdout.strip(),
               "config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
               "test_catalog_sha256": hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest(),
               "production_catalog_sha256": hashlib.sha256((ROOT / "app/rules/catalog.json").read_bytes()).hexdigest(),
               "checks": {}}
    try:
        if args.run_gpu:
            if sys.platform != "win32":
                raise RuntimeError("This GPU lifecycle harness targets Windows")
            service.check_runtime(cfg, runtime_root)  # Full SHA/size check, no download.
            receipt["runtime_hashes_checked"] = True
            receipt["gpu_before"] = gpu_snapshot()
            receipt["gpu_launch_attempted"] = True
            receipt["normal"] = phase(folder / "normal", cfg, runtime_root, key, clip,
                                       call_timeout=args.call_timeout_sec)
            receipt["deadline"] = phase(folder / "deadline", cfg, runtime_root, key, clip,
                                         deadline_fault=True, call_timeout=args.call_timeout_sec)
            # Allow the driver to release contexts; collect facts without killing other apps.
            before = memory_by_device(receipt["gpu_before"])
            for _ in range(10):
                receipt["gpu_after"] = gpu_snapshot()
                after = memory_by_device(receipt["gpu_after"])
                released = bool(before) and before.keys() == after.keys() and all(
                    after[k] <= before[k] + 256 for k in before)
                if released:
                    break
                time.sleep(1)
            loaded = memory_by_device(receipt["normal"]["gpu_loaded"])
            receipt["checks"]["gpu_memory_increased"] = bool(before) and any(
                loaded.get(k, 0) > before[k] + 256 for k in before)
            receipt["checks"]["memory_returned_within_256_MiB"] = released
            phase_ok = all(all(receipt[p]["checks"].values()) for p in ("normal", "deadline"))
            receipt["status"] = "technical_checks_passed" if phase_ok and all(receipt["checks"].values()) else "incomplete_or_failed"
    except Exception as exc:
        receipt["status"], receipt["error_class"] = "failed", type(exc).__name__
    finally:
        receipt["config_unchanged"] = hashlib.sha256(cfg_path.read_bytes()).hexdigest() == receipt["config_sha256"]
        receipt["production_catalog_unchanged"] = hashlib.sha256((ROOT / "app/rules/catalog.json").read_bytes()).hexdigest() == receipt["production_catalog_sha256"]
        write_json(folder / "receipt.json", receipt)
    # Relative ASCII path also works in Windows shells with a non-UTF-8 code page.
    print(f"status={receipt['status']} receipt={folder.relative_to(ROOT) / 'receipt.json'}")
    return 0 if receipt["status"] in ("prepared_not_run", "technical_checks_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
