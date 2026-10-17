"""Pre-admission upload check with bounded FFmpeg calls. Does not extract images."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from app.jobs.procs import popen_kwargs


class InvalidVideo(ValueError):
    """Upload is not a decodable QuickTime/MOV video. Detail stays local."""


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL, **popen_kwargs())
    except subprocess.TimeoutExpired as exc:  # subprocess.run kills the child.
        raise InvalidVideo(f"{Path(cmd[0]).name} timeout") from exc
    except OSError as exc:
        raise RuntimeError(f"{Path(cmd[0]).name} is not available") from exc


def probe_mov(path: Path, timeout: float, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> float:
    """Return container duration in seconds; raise InvalidVideo if unusable.

    Reads the container, then fully decodes the first video frame, so a renamed
    or truncated file is rejected before a job exists.
    """
    if timeout <= 0:
        raise InvalidVideo("no time left for probing")
    result = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                   "-show_streams", "-select_streams", "v:0", str(path)], timeout)
    if result.returncode != 0:
        raise InvalidVideo("ffprobe failed")
    try:
        data = json.loads(result.stdout or b"{}")
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
    decoded = _run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
                    "-frames:v", "1", "-f", "null", "-"], timeout)
    if decoded.returncode != 0:
        raise InvalidVideo("first frame does not decode")
    return duration
