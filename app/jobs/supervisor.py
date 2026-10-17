"""Supervisor: owns llama-server and the analysis worker, admission and the watchdog.

One accepted job at a time (settings.max_inflight_jobs == 1). Admission is closed
while starting, running a job, recovering, broken or stopped. A hung or crashed
job is failed and BOTH owned processes are stopped (confirmed) and restarted
before the slot opens again. Only processes spawned here are ever terminated.

close() is final: STOPPED is never left, every wait observes the closing flag,
and process start/stop are serialized so recovery cannot race shutdown.
"""
from __future__ import annotations

import enum
import json
import logging
import multiprocessing
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.contracts import AnalysisResult, RuleCatalog
from app.jobs.procs import ProcessJob, spawn_in_job, stop_tree
from app.jobs.serialize import to_done_job
from app.jobs.store import JobStore
from app.jobs.worker import worker_main
from app.settings import Settings

log = logging.getLogger("assar.jobs")

# Analysis errors after which the owned server is known to be in a sane state.
SOFT_ERRORS = frozenset({"DecodeError", "UnassessableVideoError", "InvalidAnalysisResultError"})
RECOVERY_ATTEMPTS = 2
_TICK = 0.1
_EOF = ("eof",)


class State(enum.Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    RECOVERING = "recovering"
    BROKEN = "broken"
    STOPPED = "stopped"


class StartupError(RuntimeError):
    """Owned runtime could not be started; the service must not accept jobs."""


class _Stopping(Exception):
    """close() was called while waiting."""


@dataclass(frozen=True)
class RuntimeSpec:
    """What to run. Production values come from app.jobs.runtime; tests inject fakes.

    analyzer_factory is "module:function" imported inside the worker and called as
    factory(endpoint=..., **analyzer_kwargs) -> VideoAnalyzer.
    server_command(port) returns llama-server argv, or None for no model server.
    """
    analyzer_factory: str
    analyzer_kwargs: dict[str, Any]
    catalog: RuleCatalog
    server_command: Callable[[int], list[str]] | None = None
    log_dir: Path | None = None


@dataclass
class Ticket:
    job_id: str
    video_path: Path
    job_dir: Path
    duration_sec: float
    request_deadline: float  # time.monotonic() at acceptance + request_timeout_sec


@dataclass
class _Owned:
    """Handles of one owned process. Kept until its exit is confirmed."""
    job: ProcessJob = field(default_factory=ProcessJob)
    proc: Any = None
    pid: int | None = None
    conn: Any = None
    inbox: queue.Queue | None = None
    reader: threading.Thread | None = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002
        return None


_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _healthy(endpoint: str, timeout: float) -> bool:
    try:
        with _LOOPBACK.open(f"{endpoint}/health", timeout=timeout) as response:
            return response.status == 200 and json.loads(response.read(4096)).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _read_messages(conn, inbox: queue.Queue) -> None:
    """Reader thread: a recv() blocked on a partial message never blocks the supervisor."""
    try:
        while True:
            inbox.put(conn.recv())
    except Exception:  # noqa: BLE001 - EOF, broken pipe, unpickling error: worker unusable.
        inbox.put(_EOF)


class Supervisor:
    def __init__(self, settings: Settings, spec: RuntimeSpec, store: JobStore) -> None:
        self.settings = settings
        self.spec = spec
        self.store = store
        self._lock = threading.Lock()       # state
        self._life = threading.RLock()      # process start/stop, never concurrent
        self._closing = threading.Event()
        self._state = State.STARTING
        self._queue: queue.Queue = queue.Queue()
        self._server = _Owned()
        self._worker = _Owned()
        self._endpoint: str | None = None
        self._thread: threading.Thread | None = None
        self._current: Ticket | None = None

    # ----- public, thread-safe -------------------------------------------------
    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def start(self) -> None:
        """Blocking: start owned server + worker; raise StartupError if impossible."""
        try:
            with self._life:
                if self._closing.is_set():
                    raise _Stopping
                self._start_runtime()
        except Exception as exc:
            with self._life:
                self._stop_runtime()
            self._set(State.BROKEN)
            raise StartupError("owned runtime failed to start") from exc
        self._thread = threading.Thread(target=self._loop, name="assar-supervisor", daemon=True)
        self._thread.start()
        self._set(State.READY)

    def try_reserve(self) -> bool:
        """Take the single job slot, or False (-> 503). Never waits."""
        with self._lock:
            if self._state is not State.READY:
                return False
            if not self._runtime_alive():
                self._state = State.RECOVERING
                self._queue.put(None)  # Recover in the supervisor thread.
                return False
            self._state = State.BUSY
            return True

    def release(self) -> None:
        """Give back a reserved slot when the upload is rejected before job creation."""
        with self._lock:
            if self._state is State.BUSY:
                self._state = State.READY

    def submit(self, ticket: Ticket) -> None:
        """Hand over an accepted job; the slot must have been reserved."""
        self._queue.put(ticket)

    def runtime_pids(self) -> dict[str, int | None]:
        """For tests/diagnostics: PIDs of owned processes."""
        return {"server": self._server.pid, "worker": self._worker.pid}

    def close(self) -> None:
        """Final shutdown. Safe during BUSY/RECOVERING; idempotent."""
        with self._lock:
            self._state = State.STOPPED
        self._closing.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            self._queue.put(False)
            # Every wait in the thread observes _closing; stop is bounded by the grace.
            thread.join(timeout=self.settings.model_stop_grace_sec * 6 + 5)
            if thread.is_alive():
                log.critical("supervisor thread did not stop in time")
        with self._life:
            self._stop_runtime(final=True)
        self._fail_pending()

    # ----- supervisor thread ---------------------------------------------------
    def _set(self, state: State) -> None:
        with self._lock:
            if self._state is not State.STOPPED:  # Only close() sets STOPPED; never left.
                self._state = state

    def _reopen(self) -> None:
        with self._lock:  # BROKEN and STOPPED stay closed.
            if self._state in (State.BUSY, State.RECOVERING):
                self._state = State.READY

    def _loop(self) -> None:
        while not self._closing.is_set():
            item = self._queue.get()
            if item is False or self._closing.is_set():
                if isinstance(item, Ticket):
                    self._queue.put(item)  # _fail_pending marks it failed.
                return
            if item is None:
                self._recover()
                self._reopen()
                continue
            self._current = item
            restart = True
            try:
                restart = self._run(item)
            except Exception:  # noqa: BLE001 - never leave a job processing.
                log.exception("supervisor error for job %s", item.job_id)
                self.store.set_failed(item.job_id, "SupervisorError")
            if restart and not self._closing.is_set():
                self._recover()  # Stops owned processes before files are removed.
            if self._closing.is_set():
                return  # close() stops the runtime, then cleans this job's files.
            self._cleanup(item)
            self._current = None
            self._reopen()

    def _run(self, t: Ticket) -> bool:
        """Run one job; return True if the owned runtime must be restarted."""
        self.store.set_running(t.job_id)
        s = self.settings
        deadline = min(t.request_deadline - s.response_reserve_sec, time.monotonic() + s.analysis_budget_sec)
        if deadline <= time.monotonic():
            self.store.set_failed(t.job_id, "DeadlineBeforeStart")
            return False
        try:
            self._worker.conn.send(("job", str(t.video_path), str(t.job_dir), deadline))
        except (OSError, ValueError, AttributeError):
            self.store.set_failed(t.job_id, "WorkerDied")
            return True
        try:
            message = self._receive(self._worker, deadline)
        except _Stopping:
            self.store.set_failed(t.job_id, "ServiceStopping")
            return False
        if message is None:
            self.store.set_failed(t.job_id, "WatchdogTimeout")
            log.warning("job %s: watchdog timeout, restarting owned runtime", t.job_id)
            return True
        if message == _EOF:
            self.store.set_failed(t.job_id, "WorkerDied")
            log.warning("job %s: worker died", t.job_id)
            return True
        if message[0] != "ok":
            code = str(message[1]) if len(message) > 1 else "UnknownError"
            self.store.set_failed(t.job_id, code)
            log.warning("job %s failed: %s", t.job_id, code)
            return code not in SOFT_ERRORS or not self._server_ok()
        if time.monotonic() > deadline:
            self.store.set_failed(t.job_id, "LateResult")
            return False
        try:
            result = AnalysisResult.model_validate(message[1])
            done = to_done_job(t.job_id, result, self.spec.catalog, t.duration_sec, t.job_dir,
                               s.max_jpeg_bytes)
        except Exception as exc:  # noqa: BLE001 - contract breach is failed, not no_violation.
            self.store.set_failed(t.job_id, f"InvalidResult:{type(exc).__name__}")
            log.warning("job %s: result rejected (%s)", t.job_id, type(exc).__name__)
            return False
        if time.monotonic() > t.request_deadline - s.response_reserve_sec:
            self.store.set_failed(t.job_id, "LateResult")
            return False
        self.store.set_done(done)
        return False

    def _receive(self, owned: _Owned, deadline: float):
        """Next worker message, None at the deadline, _EOF if the worker is gone."""
        while True:
            if self._closing.is_set():
                raise _Stopping
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return owned.inbox.get(timeout=min(_TICK, remaining))
            except queue.Empty:
                continue

    def _server_ok(self) -> bool:
        if self.spec.server_command is None:
            return True
        return self._server.proc.poll() is None and _healthy(self._endpoint, 2)

    def _runtime_alive(self) -> bool:
        worker = self._worker.proc
        if worker is None or not worker.is_alive():
            return False
        server = self._server.proc
        return self.spec.server_command is None or (server is not None and server.poll() is None)

    def _recover(self) -> None:
        self._set(State.RECOVERING)
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            with self._life:
                if self._closing.is_set():
                    return
                if not self._stop_runtime():
                    log.critical("old owned processes not confirmed stopped; not starting new ones")
                    break
                try:
                    self._start_runtime()
                except _Stopping:
                    return
                except Exception as exc:  # noqa: BLE001
                    log.error("recovery attempt %d failed: %s", attempt, type(exc).__name__)
                    continue
            log.info("owned runtime recovered (attempt %d)", attempt)
            return
        with self._life:
            if not self._closing.is_set():
                self._stop_runtime()  # Do not leave a half-started runtime.
        log.critical("owned runtime could not be recovered; admission stays closed")
        self._set(State.BROKEN)

    def _cleanup(self, t: Ticket) -> None:
        shutil.rmtree(t.video_path.parent, ignore_errors=True)
        shutil.rmtree(t.job_dir, ignore_errors=True)

    def _fail_pending(self) -> None:
        """After close: current and queued jobs can never finish."""
        pending = [self._current] if self._current else []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Ticket):
                pending.append(item)
        for ticket in pending:
            self.store.set_failed(ticket.job_id, "ServiceStopping")  # No-op if already terminal.
            self._cleanup(ticket)
        self._current = None

    # ----- process lifecycle (call with self._life held) -------------------------
    def _start_runtime(self) -> None:
        if self.spec.server_command is not None:
            self._start_server()
        self._start_worker()

    def _log_file(self, name: str):
        if self.spec.log_dir is None:
            return subprocess.DEVNULL
        self.spec.log_dir.mkdir(parents=True, exist_ok=True)
        return open(self.spec.log_dir / name, "ab")  # noqa: SIM115 - child inherits it.

    def _start_server(self) -> None:
        s = self.settings
        port = s.model_port or _free_port()
        argv = self.spec.server_command(port)
        owned = _Owned()
        out = self._log_file("llama-server.log")
        try:
            # Created suspended and resumed inside its job (Windows): no escape window.
            owned.proc = spawn_in_job(argv, owned.job, stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=subprocess.STDOUT)
        except BaseException:
            owned.job.close()
            raise
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        owned.pid = owned.proc.pid
        self._server = owned  # Track identity before waiting for health.
        endpoint = f"http://127.0.0.1:{port}"
        limit = time.monotonic() + s.model_start_timeout_sec
        while time.monotonic() < limit:
            if self._closing.is_set():
                raise _Stopping
            if owned.proc.poll() is not None:
                raise StartupError("model server exited during startup")
            if _healthy(endpoint, min(2.0, max(0.1, limit - time.monotonic()))):
                if not self._closing.wait(0.2) and owned.proc.poll() is None:
                    self._endpoint = endpoint  # Health came from our live process.
                    return
            self._closing.wait(0.25)
        raise StartupError("model server health timeout")

    def _start_worker(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        kwargs = dict(self.spec.analyzer_kwargs, endpoint=self._endpoint)
        owned = _Owned(conn=parent, inbox=queue.Queue())
        owned.proc = ctx.Process(target=worker_main, args=(child, self.spec.analyzer_factory, kwargs),
                                 name="assar-worker", daemon=True)
        owned.proc.start()
        child.close()
        owned.pid = owned.proc.pid
        self._worker = owned
        # The worker waits for "go" before importing anything or spawning children,
        # so nothing it creates can exist outside the job.
        owned.job.assign(owned.pid)
        owned.reader = threading.Thread(target=_read_messages, args=(parent, owned.inbox),
                                        name="assar-worker-reader", daemon=True)
        owned.reader.start()
        parent.send(("go",))
        message = self._receive(owned, time.monotonic() + self.settings.model_start_timeout_sec)
        if message != ("ready",):
            raise StartupError("analysis worker failed to initialise")

    def _stop_runtime(self, final: bool = False) -> bool:
        """Stop worker then server. Handles of a process whose exit is not confirmed
        are kept (state goes BROKEN) unless ``final``, where closing the job handle
        is the last resort (kill-on-close)."""
        grace = self.settings.model_stop_grace_sec
        ok = True
        worker = self._worker
        if worker.proc is not None:
            proc = worker.proc

            def wait_worker(timeout: float) -> bool:
                proc.join(timeout)
                return proc.exitcode is not None
            if stop_tree(worker.job, proc.terminate, wait_worker, grace):
                if worker.reader is not None:
                    worker.reader.join(timeout=grace)
                worker.conn.close()
                worker.job.close()
                self._worker = _Owned()
            else:
                ok = False
        server = self._server
        if server.proc is not None:
            sproc = server.proc

            def wait_server(timeout: float) -> bool:
                try:
                    sproc.wait(timeout)
                    return True
                except subprocess.TimeoutExpired:
                    return False
            if stop_tree(server.job, sproc.terminate, wait_server, grace):
                server.job.close()
                self._server, self._endpoint = _Owned(), None
            else:
                ok = False
        if not ok:
            log.critical("owned process did not exit after kill")
            if final:
                self._worker.job.close()
                self._server.job.close()
        return ok


class InstanceLock:
    """Refuse a second service instance (e.g. uvicorn --workers 2) on the same outputs dir."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a+b")  # noqa: SIM115
        try:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise StartupError("another assar service instance is running") from exc

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._file.close()
