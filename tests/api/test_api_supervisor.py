"""Supervisor lifecycle: final close during BUSY/RECOVERING, handle retention, owned trees, IPC."""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import pytest

import app.jobs.supervisor as supervisor_module
from app.jobs.procs import ProcessJob, pid_alive, spawn_in_job
from app.jobs.store import JobStore
from app.jobs.supervisor import RuntimeSpec, State, Supervisor, Ticket, _Owned, _read_messages
from app.settings import Settings
from tests.api.fakes import CATALOG
from tests.api.harness import fake_server_command

BUDGET = 2


def _wait(predicate, timeout: float = 30) -> None:
    limit = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > limit:
            raise AssertionError("condition not reached")
        time.sleep(0.05)


def _all_pids(tmp_path: Path) -> list[int]:
    lines = (tmp_path / "server.pid").read_text(encoding="utf-8").split()
    return [int(x) for x in lines]


@pytest.fixture
def env(tmp_path):
    control = tmp_path / "mode.txt"
    control.write_text("hang", encoding="utf-8")
    settings = Settings(api_key="k", uploads_dir=tmp_path / "u", outputs_dir=tmp_path / "o",
                        request_timeout_sec=30, analysis_budget_sec=BUDGET, response_reserve_sec=1,
                        model_start_timeout_sec=60, model_stop_grace_sec=2)
    store = JobStore(tmp_path / "jobs.sqlite3")
    spec = RuntimeSpec("tests.api.fakes:create", {"control": str(control)}, CATALOG,
                       fake_server_command(tmp_path / "server.pid"))
    sup = Supervisor(settings, spec, store)
    sup.start()
    yield sup, store, tmp_path, control
    sup.close()
    store.close()


def _submit(sup: Supervisor, store: JobStore, tmp_path: Path, job_id: str) -> Ticket:
    assert sup.try_reserve()
    video = tmp_path / "u" / job_id / "input.mov"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"synthetic")
    job_dir = tmp_path / "o" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    store.create(job_id)
    ticket = Ticket(job_id, video, job_dir, 3.0, time.monotonic() + 30)
    sup.submit(ticket)
    return ticket


def _assert_stays_stopped(sup: Supervisor, tmp_path: Path, owned: list[int]) -> None:
    starts = (tmp_path / "server.pid").read_text(encoding="utf-8")
    time.sleep(BUDGET + 2)  # Past the job deadline: a late watchdog/recovery would fire now.
    assert sup.state is State.STOPPED and not sup.try_reserve()
    assert sup.runtime_pids() == {"server": None, "worker": None}
    assert (tmp_path / "server.pid").read_text(encoding="utf-8") == starts  # No new server start.
    for pid in owned + _all_pids(tmp_path):
        assert not pid_alive(pid), pid


def test_close_during_busy_is_final(env):
    sup, store, tmp_path, _ = env
    ticket = _submit(sup, store, tmp_path, "busy")
    _wait(lambda: store.state("busy") == "running")
    worker = sup.runtime_pids()["worker"]
    started = time.monotonic()
    sup.close()
    assert time.monotonic() - started < 10
    assert store.state("busy") == "failed"
    assert not ticket.video_path.parent.exists() and not ticket.job_dir.exists()
    _assert_stays_stopped(sup, tmp_path, [worker])


def test_close_during_recovering_is_final(env):
    sup, store, tmp_path, _ = env
    worker = sup.runtime_pids()["worker"]
    (tmp_path / "server.pid.delay").write_text("60", encoding="utf-8")  # Restarted server never gets healthy.
    _submit(sup, store, tmp_path, "rec")
    _wait(lambda: sup.state is State.RECOVERING and len(_all_pids(tmp_path)) == 4)
    assert store.state("rec") == "failed"
    started = time.monotonic()
    sup.close()
    assert time.monotonic() - started < 10
    _assert_stays_stopped(sup, tmp_path, [worker])


def test_unconfirmed_stop_keeps_handles_and_blocks_restart(env, monkeypatch):
    sup, _, tmp_path, _ = env
    before = sup.runtime_pids()
    worker_owned, server_owned = sup._worker, sup._server
    monkeypatch.setattr(supervisor_module, "stop_tree", lambda *a, **k: False)
    sup._recover()
    assert sup.state is State.BROKEN and not sup.try_reserve()
    assert sup._worker is worker_owned and sup._server is server_owned  # Handles kept.
    assert not worker_owned.job.closed and not server_owned.job.closed
    assert sup.runtime_pids() == before and len(_all_pids(tmp_path)) == 2  # No second runtime.
    monkeypatch.undo()
    sup.close()
    for pid in [before["worker"], *_all_pids(tmp_path)]:
        assert not pid_alive(pid)


def test_worker_descendants_die_with_owned_job(env):
    sup, store, tmp_path, control = env
    control.write_text("hang_child", encoding="utf-8")
    _submit(sup, store, tmp_path, "tree")
    child_file = tmp_path / "worker_child.pid"
    _wait(lambda: child_file.exists() and child_file.read_text(encoding="utf-8").strip())
    child = int(child_file.read_text(encoding="utf-8").split()[0])
    assert pid_alive(child)
    _wait(lambda: store.state("tree") == "failed")
    _wait(lambda: sup.state is State.READY)
    assert not pid_alive(child)


def test_spawned_process_and_immediate_child_are_owned(tmp_path):
    job = ProcessJob()
    out = tmp_path / "child.pid"
    code = ("import subprocess, sys; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
            f"open({str(out)!r}, 'w').write(str(c.pid)); import time; time.sleep(600)")
    proc = spawn_in_job([sys.executable, "-c", code], job)
    _wait(lambda: out.exists() and out.read_text().strip())
    child = int(out.read_text())
    assert job.active() >= 2
    job.kill_all()
    _wait(lambda: job.active() == 0, 10)
    proc.wait(5)
    assert not pid_alive(child)
    job.close()


class _StuckConn:
    """recv() blocks forever, like a partially received message from a frozen writer."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def recv(self):
        self.release.wait()
        raise EOFError


def test_blocked_recv_does_not_block_watchdog(tmp_path):
    settings = Settings(api_key="k", outputs_dir=tmp_path)
    sup = Supervisor(settings, RuntimeSpec("x:y", {}, CATALOG), JobStore(tmp_path / "j.sqlite3"))
    conn = _StuckConn()
    owned = _Owned(conn=conn, inbox=queue.Queue())
    threading.Thread(target=_read_messages, args=(conn, owned.inbox), daemon=True).start()
    started = time.monotonic()
    assert sup._receive(owned, time.monotonic() + 0.5) is None
    assert time.monotonic() - started < 2
    conn.release.set()  # Worker killed -> recv raises -> reader reports EOF.
    assert owned.inbox.get(timeout=2) == supervisor_module._EOF
    owned.job.close()
