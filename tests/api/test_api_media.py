"""probe_mov: one absolute deadline across stages; decoders owned and killed on shutdown."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from app.jobs.media import InvalidVideo, probe_mov
from app.jobs.procs import ProcessOwner, pid_alive
from tests.api.harness import have_ffmpeg, make_mov

pytestmark = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not in PATH")


def _shim(pid_file: Path, sleep: float, then: str = "") -> list[str]:
    """Fake tool: record pid, sleep, optionally run the real tool with the same args."""
    code = (f"import os, subprocess, sys, time; open({str(pid_file)!r}, 'a').write(f'{{os.getpid()}}\\n'); "
            f"time.sleep({sleep}); "
            + (f"sys.exit(subprocess.run([{then!r}] + sys.argv[1:]).returncode)" if then else "sys.exit(0)"))
    return [sys.executable, "-c", code]


def _pids(path: Path) -> list[int]:
    return [int(x) for x in path.read_text(encoding="utf-8").split()] if path.exists() else []


@pytest.fixture(scope="module")
def mov(tmp_path_factory) -> Path:
    return make_mov(tmp_path_factory.mktemp("v") / "synthetic.mov", 2)


def test_real_tools_accept_synthetic_mov(mov):
    owner = ProcessOwner()
    assert 1.5 < probe_mov(mov, time.monotonic() + 30, owner) < 2.5
    owner.close()


def test_slow_decoder_gets_only_the_remaining_time(mov, tmp_path):
    owner, pids = ProcessOwner(), tmp_path / "pids"
    started = time.monotonic()
    with pytest.raises(InvalidVideo):
        probe_mov(mov, started + 2, owner, ffmpeg=_shim(pids, 30))
    assert time.monotonic() - started < 4
    assert _pids(pids) and not any(pid_alive(p) for p in _pids(pids))
    owner.close()


def test_stages_share_one_deadline(mov, tmp_path):
    # Each stage alone fits in 2 s, together they do not: the old per-stage timeout accepted this.
    owner, pids = ProcessOwner(), tmp_path / "pids"
    started = time.monotonic()
    with pytest.raises(InvalidVideo):
        probe_mov(mov, started + 2, owner, ffprobe=_shim(pids, 1.2, "ffprobe"), ffmpeg=_shim(pids, 1.2))
    assert time.monotonic() - started < 3.5
    assert not any(pid_alive(p) for p in _pids(pids))
    owner.close()


def test_expired_deadline_starts_nothing(mov, tmp_path):
    owner, pids = ProcessOwner(), tmp_path / "pids"
    with pytest.raises(InvalidVideo):
        probe_mov(mov, time.monotonic() - 1, owner, ffprobe=_shim(pids, 0))
    assert not pids.exists()
    owner.close()


def test_owner_shutdown_kills_running_decoder(mov, tmp_path):
    owner, pids = ProcessOwner(), tmp_path / "pids"
    errors: list[BaseException] = []

    def run() -> None:
        try:
            probe_mov(mov, time.monotonic() + 120, owner, ffmpeg=_shim(pids, 120))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    limit = time.monotonic() + 30
    while not _pids(pids) and time.monotonic() < limit:
        time.sleep(0.05)
    assert _pids(pids)
    started = time.monotonic()
    owner.close()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 5
    assert errors and isinstance(errors[0], (InvalidVideo, RuntimeError))
    assert not any(pid_alive(p) for p in _pids(pids))
    with pytest.raises(RuntimeError):
        probe_mov(mov, time.monotonic() + 30, owner)  # No new decoders after shutdown.
