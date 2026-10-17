"""Pre-admission upload check with FFmpeg under one absolute deadline. No image extraction."""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

from app.jobs.procs import ProcessOwner, spawn_in_job


class InvalidVideo(ValueError):
    """Upload is not a decodable QuickTime/MOV video (or probing ran out of time)."""


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InvalidVideo("probe deadline exceeded")
    return remaining


def _run(cmd: list[str], deadline: float, owner: ProcessOwner) -> tuple[int, bytes]:
    """Run one tool inside its own owned job; the whole tree is killed on exit."""
    timeout = _remaining(deadline)
    job = owner.new_job()
    try:
        try:
            proc = spawn_in_job(cmd, job, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RuntimeError(f"{Path(cmd[0]).name} is not available") from exc
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            job.kill_all()
            proc.kill()
            proc.communicate()
            raise InvalidVideo(f"{Path(cmd[0]).name} timeout") from None
        return proc.returncode, out
    finally:
        owner.release(job)  # Kills leftovers; also done by owner.close() on shutdown.


def probe_mov(path: Path, deadline_monotonic: float, owner: ProcessOwner,
              ffprobe: list[str] | None = None, ffmpeg: list[str] | None = None) -> float:
    """Return container duration in seconds; raise InvalidVideo if unusable or late.

    ``deadline_monotonic`` is absolute (time.monotonic()); every stage gets only
    the remaining time and the result is rejected if the deadline passed meanwhile.
    """
    ffprobe = ffprobe or ["ffprobe"]
    ffmpeg = ffmpeg or ["ffmpeg"]
    code, stdout = _run([*ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                         "-show_streams", "-select_streams", "v:0", str(path)], deadline_monotonic, owner)
    if code != 0:
        raise InvalidVideo("ffprobe failed")
    try:
        data = json.loads(stdout or b"{}")
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        duration = float(fmt.get("duration") or (streams[0].get("duration") if streams else 0) or 0)
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidVideo("unreadable probe output") from exc
    if "mov" not in str(fmt.get("format_name", "")).split(","):
        raise InvalidVideo("container is not QuickTime/MOV")
    if not streams or not streams[0].get("codec_name"):
        raise InvalidVideo("no video stream")
    if not math.isfinite(duration) or duration <= 0:
        raise InvalidVideo("unknown duration")
    code, _ = _run([*ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
                    "-frames:v", "1", "-f", "null", "-"], deadline_monotonic, owner)
    if code != 0:
        raise InvalidVideo("first frame does not decode")
    _remaining(deadline_monotonic)  # A decode that finished late is not accepted.
    return duration
