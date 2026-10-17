"""Isolated analysis worker process (multiprocessing spawn). Owned by the supervisor.

Messages parent -> child: ("job", video_path, job_dir, deadline_monotonic).
Messages child -> parent: ("ready",), ("init_failed", code), ("ok", result_dict),
("error", code). Codes are exception class names; no message text or traceback.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path


def load_factory(dotted: str):
    module, _, name = dotted.partition(":")
    return getattr(importlib.import_module(module), name)


def worker_main(conn, factory: str, kwargs: dict) -> None:
    if hasattr(os, "setsid"):
        os.setsid()  # POSIX: own process group so the supervisor can kill decoders too.
    from app.settings import configure_offline_environment
    configure_offline_environment()  # Before any ML import.
    try:
        analyze = load_factory(factory)(**kwargs)
    except BaseException as exc:  # noqa: BLE001 - reported as a code, process exits.
        conn.send(("init_failed", type(exc).__name__))
        return
    conn.send(("ready",))
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        _, video_path, job_dir, deadline = message
        try:
            result = analyze(Path(video_path), Path(job_dir), deadline)
            payload = ("ok", result.model_dump())
        except Exception as exc:  # noqa: BLE001 - classified by the supervisor.
            payload = ("error", type(exc).__name__)
        conn.send(payload)
