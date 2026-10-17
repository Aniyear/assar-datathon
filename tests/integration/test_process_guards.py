"""B9 regression: stop confirms the whole owned synthetic tree, even after root exit."""
import subprocess
import sys
import time

import pytest

from app.jobs.procs import ProcessJob, pid_alive, spawn_in_job, stop_tree


@pytest.mark.skipif(sys.platform != "win32", reason="Target deployment: Windows Job Object")
def test_stop_confirms_descendant_exit_after_root_already_exited(tmp_path):
    pid_file = tmp_path / "synthetic-child.pid"
    code = ("import subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))")
    job = ProcessJob()
    proc = None
    try:
        proc = spawn_in_job([sys.executable, "-c", code], job, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        proc.wait(timeout=10)
        child = int(pid_file.read_text())
        assert proc.poll() is not None and pid_alive(child)
        assert job.active() >= 1

        def exited(timeout):
            try:
                proc.wait(timeout)
                return True
            except subprocess.TimeoutExpired:
                return False

        started = time.monotonic()
        assert stop_tree(job, proc.terminate, exited, grace_sec=2)
        assert time.monotonic() - started < 5
        assert job.active() == 0 and not pid_alive(child)
    finally:
        job.kill_all()
        job.close()
        if proc is not None:
            proc.wait(timeout=5)
