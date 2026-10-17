"""Local SQLite job store. Public JSON is stored only after full validation."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from app.contracts import DoneJob, FailedJob, ProcessingJob

# Internal states; queued/running are shown to the jury as "processing".
QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, state TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, reason TEXT, result_json TEXT)""")

    def create(self, job_id: str) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, NULL, NULL)", (job_id, QUEUED, now, now))

    def set_running(self, job_id: str) -> None:
        self._update(job_id, RUNNING, None, None)

    def set_done(self, done: DoneJob) -> None:
        self._update(done.job_id, DONE, None, done.model_dump_json())

    def set_failed(self, job_id: str, reason: str) -> None:
        """reason is a short internal code (exception class), never shown to clients."""
        self._update(job_id, FAILED, reason, None)

    def _update(self, job_id: str, state: str, reason: str | None, result: str | None) -> None:
        with self._lock:
            # Terminal states are final: a late result never overwrites failed.
            self._db.execute("UPDATE jobs SET state=?, updated=?, reason=?, result_json=? "
                             "WHERE job_id=? AND state NOT IN (?, ?)",
                             (state, time.time(), reason, result, job_id, DONE, FAILED))

    def state(self, job_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def public_json(self, job_id: str) -> str | None:
        """Exact public JSON. Done results were fully validated before storing."""
        with self._lock:
            row = self._db.execute("SELECT state, result_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        state, result = row
        if state == DONE:
            return result
        if state == FAILED:
            return FailedJob(job_id=job_id, status="failed").model_dump_json()
        return ProcessingJob(job_id=job_id, status="processing").model_dump_json()

    def fail_unfinished(self, reason: str) -> list[str]:
        """On startup: jobs of a previous process can never finish."""
        with self._lock:
            ids = [r[0] for r in self._db.execute(
                "SELECT job_id FROM jobs WHERE state IN (?, ?)", (QUEUED, RUNNING))]
            self._db.execute("UPDATE jobs SET state=?, updated=?, reason=? WHERE state IN (?, ?)",
                             (FAILED, time.time(), reason, QUEUED, RUNNING))
        return ids

    def close(self) -> None:
        with self._lock:
            self._db.close()
