"""Local operator CLI. Production readiness is never overridden; network-test has no ML.

Start stays in the foreground. Ctrl+C or a separate `stop` requests graceful uvicorn
shutdown. Stop only writes a run-specific local control file; it never signals a PID.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CF_VERSION = "2026.10.1"
# Set from the publisher's GitHub release asset digest (not an unverified download).
CF_SHA256 = "4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6"


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # Windows readers briefly deny FILE_SHARE_DELETE. A concurrent status/stop
    # read must not shut down an otherwise healthy server.
    for attempt in range(21):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(0.05)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True)
        sid = re.search(r"S-1-[0-9-]+", result.stdout).group(0)
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       capture_output=True, check=True)
    else:
        path.chmod(0o700)


def key_file(state_dir: Path) -> Path:
    return state_dir / "private" / "api-key.txt"


def load_or_create_key(state_dir: Path) -> str:
    path = key_file(state_dir)
    private_directory(path.parent)
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    key = path.read_text(encoding="utf-8").strip()
    if len(key) < 32:
        raise ValueError("Local key is too short; replace it outside Git")
    return key


def resolved_config(source: Path, runtime_root: Path) -> dict:
    cfg = copy.deepcopy(json.loads(source.read_text(encoding="utf-8")))
    for section, field in (("runtime", "server_bin"), ("weights", "path"), ("mmproj", "path")):
        path = Path(cfg[section][field])
        cfg[section][field] = str((runtime_root / path).resolve())
    return cfg


def environment_problems() -> list[str]:
    problems = []
    if sys.version_info[:2] != (3, 11):
        problems.append("Нужен проверенный Python 3.11 и requirements.lock")
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if "==" not in line or line.startswith("#"):
            continue
        name, wanted = line.split("==")
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        if actual != wanted:
            problems.append(f"Зависимость {name}: {actual}; нужна {wanted}")
    return problems


def preflight(args) -> dict:
    report = {"mode": args.mode, "ready": False, "problems": environment_problems(),
              "runtime_hashes_checked": False, "gpu_started": False}
    if report["problems"] or args.mode == "network-test":
        report["ready"] = not report["problems"]
        return report
    from app.jobs.runtime import load_catalog, model_config_problems, verified_only
    from app.inference.service import check_runtime
    for binary in ("ffmpeg", "ffprobe", "nvidia-smi"):
        if not shutil.which(binary):
            report["problems"].append(f"Не найден {binary} в PATH")
    try:
        cfg = resolved_config(args.model_config, args.runtime_root)
        report["problems"].extend(model_config_problems(cfg))
        report["model_id"] = cfg["model_id"]
        check_runtime(cfg, ROOT)  # CPU-only full SHA256; never starts a process/model.
        report["runtime_hashes_checked"] = True
    except Exception as exc:
        report["problems"].append(f"Runtime/веса/конфиг не подтверждены ({type(exc).__name__}); проверьте --runtime-root")
    try:
        if not verified_only(load_catalog(ROOT / "app/rules/catalog.json")).rules:
            report["problems"].append("В production-каталоге нет verified-правил")
    except Exception as exc:
        report["problems"].append(f"Каталог не читается ({type(exc).__name__})")
    if shutil.which("nvidia-smi"):
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True)
            report["gpu_snapshot"] = gpu.stdout.strip().splitlines()
            processes = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
                                       capture_output=True, text=True, timeout=10, check=True)
            pids = [int(line) for line in processes.stdout.splitlines() if line.strip().isdigit()]
            report["compute_pids"] = pids
            if pids:
                report["problems"].append("GPU занят вычислительными процессами; согласуйте слот с Омаром. Ничего не остановлено")
        except (OSError, subprocess.SubprocessError):
            report["problems"].append("Не удалось проверить GPU через nvidia-smi")
    report["ready"] = not report["problems"]
    return report


def show_report(report: dict) -> None:
    print("ГОТОВО к запуску" if report["ready"] else "НЕ ГОТОВО к production-запуску", flush=True)
    if report["runtime_hashes_checked"]:
        print("SHA256 runtime/GGUF/mmproj подтверждены; модель не запускалась.", flush=True)
    for problem in report["problems"]:
        print(f"- {problem}", flush=True)


def validate_tunnel(path: Path) -> None:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != CF_SHA256:
        raise ValueError("cloudflared SHA256 mismatch")


def start(args, state_dir: Path) -> int:
    if args.mode == "production" and getattr(args, "ngrok", None) and not getattr(args, "publish", False):
        raise ValueError("Production ngrok requires explicit --publish")
    report = preflight(args)
    state_dir.mkdir(parents=True, exist_ok=True)
    if not report["ready"]:
        atomic_json(state_dir / "preflight.json", report)
        show_report(report)
        return 2
    from app.jobs.supervisor import InstanceLock
    from app.jobs.procs import ProcessJob, spawn_in_job, stop_tree
    import uvicorn

    lock = InstanceLock(state_dir / ".launcher.lock")
    try:
        atomic_json(state_dir / "preflight.json", report)
        show_report(report)
        if args.tunnel:
            validate_tunnel(args.tunnel)
        ngrok_binary = getattr(args, "ngrok", None)
        ngrok_config = state_dir / "private/ngrok.json"
        if ngrok_binary:
            from scripts import ngrok_support
            private_directory(ngrok_config.parent)
            if ngrok_support.import_official_auth(ngrok_config):
                print("Авторизация ngrok прочитана из стандартной конфигурации; токен остаётся локально.", flush=True)
            if not ngrok_config.is_file():
                print("Сначала выполните официальный ngrok config add-authtoken в своём PowerShell. Токен в чат не отправляйте.", flush=True)
                return 2
            ngrok_support.validate(ngrok_binary, ngrok_config)
        key = load_or_create_key(state_dir)
        run_id = uuid.uuid4().hex
        state = {"run_id": run_id, "pid": os.getpid(), "mode": args.mode, "phase": "starting",
                 "url": f"http://127.0.0.1:{args.port}", "public_url": None, "tunnel_pid": None,
                 "tunnel_provider": "ngrok" if ngrok_binary else "cloudflare" if args.tunnel else None,
                 "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
        state_path = state_dir / "state.json"
        stop_path = state_dir / f"stop-{run_id}.json"
        if args.mode == "network-test":
            from scripts.network_probe import create_probe
            app = create_probe(key, run_id)
        else:
            from app.api.main import create_app
            from app.settings import Settings
            cfg_path = state_dir / "resolved-model.json"
            atomic_json(cfg_path, resolved_config(args.model_config, args.runtime_root))
            env = dict(os.environ, ASSAR_API_KEY=key, ASSAR_HOST="127.0.0.1", ASSAR_PORT=str(args.port),
                       ASSAR_MODEL_CONFIG_PATH=str(cfg_path), ASSAR_RULES_CATALOG_PATH=str(ROOT / "app/rules/catalog.json"),
                       ASSAR_UPLOADS_DIR=str(state_dir / "uploads"), ASSAR_OUTPUTS_DIR=str(state_dir / "results"))
            # production_spec and its readiness checks still execute inside the real lifespan.
            app = create_app(Settings.from_env(env))
        config = uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False,
                                log_level="warning", timeout_graceful_shutdown=15)
        server = uvicorn.Server(config)
        finished = threading.Event()
        tunnel_job, tunnel, tunnel_log = ProcessJob(), None, None
        ngrok_facts, ngrok_reader = {}, None
        errors = []

        def monitor():
            nonlocal tunnel, tunnel_log, ngrok_reader
            try:
                while not finished.wait(0.2):
                    if stop_path.exists():
                        server.should_exit = True
                    if server.started and state["phase"] == "starting":
                        state["phase"] = "running"
                        print(f"{args.mode}: {state['url']}\nКлюч только в {key_file(state_dir)}\nОстановка: scripts/assar.ps1 stop --mode {args.mode}", flush=True)
                        if ngrok_binary:
                            # Ignore unrelated account/settings environment; private config is explicit.
                            env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NGROK_")}
                            tunnel = spawn_in_job(ngrok_support.command(ngrok_binary, ngrok_config, state["url"]),
                                                  tunnel_job, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
                            ngrok_reader = threading.Thread(target=ngrok_support.collect_output,
                                                           args=(tunnel.stdout, ngrok_facts), daemon=True)
                            ngrok_reader.start()
                            state["tunnel_pid"] = tunnel.pid
                            state["tunnel_started_at"] = time.time()
                        elif args.tunnel:
                            log_path = state_dir / f"tunnel-{run_id}.log"
                            tunnel_log = log_path.open("w", encoding="utf-8")
                            tunnel = spawn_in_job([str(args.tunnel), "tunnel", "--no-autoupdate", "--protocol", "http2",
                                                   "--url", state["url"], "--metrics", "127.0.0.1:0"],
                                                  tunnel_job, stdout=tunnel_log, stderr=subprocess.STDOUT)
                            state["tunnel_pid"] = tunnel.pid
                            state["tunnel_started_at"] = time.time()
                    if tunnel:
                        if tunnel.poll() is not None:
                            if ngrok_reader:
                                ngrok_reader.join(timeout=1)
                            state["tunnel_error_codes"] = ngrok_facts.get("error_codes", [])
                            raise RuntimeError("owned tunnel exited")
                        if not state["public_url"]:
                            if ngrok_binary:
                                public_url = ngrok_facts.get("public_url")
                            else:
                                text = log_path.read_text(encoding="utf-8", errors="replace")
                                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
                                public_url = match.group(0) if match else None
                            if public_url:
                                state["public_url"] = public_url
                                label = ("ТЕСТОВЫЙ HTTPS (без inference/загрузки)" if args.mode == "network-test"
                                         else "PRODUCTION HTTPS")
                                print(f"{label}: {state['public_url']}", flush=True)
                            elif time.time() - state["tunnel_started_at"] > 60:
                                raise RuntimeError("no tunnel URL after 60 seconds")
                    state["heartbeat"] = time.time()
                    atomic_json(state_path, state)
            except Exception as exc:
                errors.append(type(exc).__name__)
                server.should_exit = True

        atomic_json(state_path, state)
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        cleanup_ok = True
        try:
            server.run()
        finally:
            finished.set()
            thread.join(timeout=10)
            if tunnel:
                def wait(timeout):
                    try:
                        tunnel.wait(timeout=timeout)
                        return True
                    except subprocess.TimeoutExpired:
                        return False
                cleanup_ok = stop_tree(tunnel_job, tunnel.terminate, wait, 5)
            tunnel_job.close()
            if ngrok_reader:
                ngrok_reader.join(timeout=5)
                state["tunnel_error_codes"] = ngrok_facts.get("error_codes", [])
            if tunnel_log:
                tunnel_log.close()
            state.update(phase="stopped", heartbeat=time.time(), tunnel_cleanup_confirmed=cleanup_ok,
                         monitor_errors=errors)
            atomic_json(state_path, state)
            atomic_json(state_dir / f"run-{run_id}.json", state)
            print("Сервис остановлен; принадлежащий запуску туннель закрыт." if cleanup_ok else "Не подтверждена остановка собственного туннеля.", flush=True)
        return 0 if server.started and cleanup_ok and not errors else 2
    finally:
        lock.release()


def control(action: str, state_dir: Path) -> int:
    path = state_dir / "state.json"
    if not path.exists():
        print("Нет зарегистрированного запуска.")
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
    if action == "status":
        state["heartbeat_recent"] = time.time() - state.get("heartbeat", 0) < 5
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if state["phase"] == "stopped":
        ok = not state.get("monitor_errors") and state.get("tunnel_cleanup_confirmed")
        print("Уже остановлен." if ok else "Остановлен с ошибкой; см. state.json (без секретов).")
        return 0 if ok else 2
    atomic_json(state_dir / f"stop-{state['run_id']}.json", {"run_id": state["run_id"]})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current["run_id"] != state["run_id"]:
            print("Появился другой запуск; он не затронут.")
            return 2
        if current["phase"] == "stopped":
            ok = current.get("tunnel_cleanup_confirmed") and not current.get("monitor_errors")
            print("Штатная остановка подтверждена." if ok else "Процесс остановлен с ошибкой; см. state.json.")
            return 0 if ok else 2
        time.sleep(0.2)
    print("Остановка не подтверждена за 60 с. Чужие PID не останавливались; проверьте терминал запуска.")
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "start", "stop", "status"))
    parser.add_argument("--mode", choices=("production", "network-test"), default="production")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    parser.add_argument("--model-config", type=Path, default=ROOT / "configs/model.yaml")
    tunnels = parser.add_mutually_exclusive_group()
    tunnels.add_argument("--tunnel", type=Path, help="Pinned cloudflared binary; network-test ONLY")
    tunnels.add_argument("--ngrok", type=Path, help="Pinned ngrok binary; production additionally requires --publish")
    parser.add_argument("--publish", action="store_true", help="Explicitly publish a ready production service via ngrok")
    args = parser.parse_args(argv)
    if args.tunnel and args.mode != "network-test":
        parser.error("--tunnel разрешён только для network-test")
    if args.publish and (args.action != "start" or args.mode != "production" or not args.ngrok):
        parser.error("--publish требует start --mode production --ngrok")
    if args.ngrok and args.mode == "production" and not args.publish:
        parser.error("Production ngrok требует отдельного явного --publish; readiness остаётся обязательной")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    args.runtime_root, args.model_config = args.runtime_root.resolve(), args.model_config.resolve()
    if args.tunnel:
        args.tunnel = args.tunnel.resolve()
    if args.ngrok:
        args.ngrok = args.ngrok.resolve()
    state_dir = ROOT / "outputs" / "service" / args.mode
    try:
        if args.action in ("stop", "status"):
            return control(args.action, state_dir)
        if args.action == "check":
            report = preflight(args)
            state_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(state_dir / "preflight.json", report)
            show_report(report)
            return 0 if report["ready"] else 2
        return start(args, state_dir)
    except (Exception, SystemExit) as exc:
        # No exception values/tracebacks: environment validation may contain secrets.
        print(f"Запуск/управление не завершён ({type(exc).__name__}). Проверьте окружение, занятый порт и status.", flush=True)
        return 2


if __name__ == "__main__":
    # Windows redirected stdout can default to cp1252 even on a Russian checkout.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
