"""Проба и выборка кадров через FFmpeg с сохранением реальных PTS исходного видео."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.contracts import AnalysisTimeoutError, DecodeError

__all__ = ["DecodeError", "VideoInfo", "Frame", "probe", "sample_frames", "sample_range", "annotate_boxes", "windows"]

# Допуск только на округление float в выводе showinfo/ffprobe, не на ошибку PTS.
_PTS_EPS = 1e-3


@dataclass(frozen=True)
class VideoInfo:
    duration_sec: float
    start_time_sec: float
    width: int
    height: int
    rotation: int
    codec: str
    avg_fps: float


@dataclass(frozen=True)
class Frame:
    index: int          # порядковый номер среди выбранных кадров
    pts_sec: float      # секунды от начала видео по фактическому PTS
    evidence_path: Path  # JPEG исходного разрешения (после поворота)
    model_path: Path     # JPEG для модели (может быть уменьшен)


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run убивает дочерний процесс при TimeoutExpired."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as e:
        raise AnalysisTimeoutError(f"{Path(cmd[0]).name} timeout after {timeout:.1f}s") from e
    except OSError as e:
        raise DecodeError(f"cannot start {Path(cmd[0]).name}") from e


def probe(video: Path, ffprobe: str = "ffprobe", timeout: float = 30) -> VideoInfo:
    r = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
              "-select_streams", "v:0", str(video)], timeout)
    if r.returncode != 0:
        raise DecodeError("ffprobe failed")
    try:
        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            raise DecodeError("no video stream")
        s, fmt = streams[0], data.get("format", {})
        rotation = 0
        for sd in s.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])
        if "rotate" in (s.get("tags") or {}):
            rotation = int(s["tags"]["rotate"])
        num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
        avg_fps = float(num) / float(den) if float(den or 0) else 0.0
        duration = float(fmt.get("duration") or s.get("duration") or 0)
        info = VideoInfo(duration_sec=duration, start_time_sec=float(fmt.get("start_time") or 0),
                         width=int(s["width"]), height=int(s["height"]), rotation=rotation,
                         codec=s.get("codec_name", ""), avg_fps=avg_fps)
    except (ValueError, KeyError, TypeError) as e:
        raise DecodeError("unparseable ffprobe output") from e
    if not duration > 0:
        raise DecodeError("unknown duration")
    return info


_PTS_RE = re.compile(r"Parsed_showinfo.*?\bn:\s*(\d+).*?\bpts_time:\s*([-\d.]+)")


def sample_frames(video: Path, out_dir: Path, step_sec: float, info: VideoInfo, model_height: int = 0,
                  ffmpeg: str = "ffmpeg", timeout: float = 120) -> list[Frame]:
    """Выбирает реальные кадры не чаще одного на step_sec секунд по всему ролику.

    Время кадра — PTS декодированного кадра из showinfo, а не номинальный FPS. Без -copyts FFmpeg
    уже отсчитывает PTS от start_time файла, поэтому повторно его не вычитаем. Декодируется только
    диапазон [0, duration) контейнера (-t); PTS вне [0, duration] после этого — ошибка декодирования,
    а не значение для clamp.
    FFmpeg применяет rotation metadata автоматически (autorotate по умолчанию).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    sel = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step_sec})',showinfo"
    if model_height:
        graph = f"[0:v]{sel},split=2[e][m];[m]scale=-2:{model_height}[ms]"
        maps = ["-map", "[e]", "-fps_mode", "vfr", "-q:v", "2", str(out_dir / "e_%05d.jpg"),
                "-map", "[ms]", "-fps_mode", "vfr", "-q:v", "3", str(out_dir / "m_%05d.jpg")]
    else:
        graph = f"[0:v]{sel}[e]"
        maps = ["-map", "[e]", "-fps_mode", "vfr", "-q:v", "2", str(out_dir / "e_%05d.jpg")]
    # -t <duration> на входе: только представленный диапазон контейнера. В MOV с VFR и B-frames
    # edit list бывает короче последних декодируемых кадров (см. README, B8); такие кадры не входят
    # в длительность ролика, которую видит backend/плеер, и не используются как доказательства.
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-xerror",
           "-t", f"{info.duration_sec:.6f}", "-i", str(video),
           "-filter_complex", graph, *maps, "-y"]
    r = _run(cmd, timeout)
    if r.returncode != 0:
        raise DecodeError("ffmpeg decode failed")
    pts = [float(m.group(2)) for m in _PTS_RE.finditer(r.stderr)]
    frames = []
    for i, t in enumerate(pts):
        if not -_PTS_EPS <= t <= info.duration_sec + _PTS_EPS:
            raise DecodeError("frame PTS outside video duration")
        e = out_dir / f"e_{i + 1:05d}.jpg"
        m = out_dir / f"m_{i + 1:05d}.jpg" if model_height else e
        if not e.exists() or not m.exists():
            raise DecodeError("frame count mismatch between showinfo and written files")
        frames.append(Frame(index=i, pts_sec=min(max(t, 0.0), info.duration_sec), evidence_path=e, model_path=m))
    if not frames:
        raise DecodeError("no frames decoded")
    if any(b.pts_sec <= a.pts_sec for a, b in zip(frames, frames[1:])):
        raise DecodeError("non-monotonic frame PTS")
    return frames


def sample_range(video: Path, out_dir: Path, step_sec: float, info: VideoInfo, start_sec: float, end_sec: float,
                 max_frames: int, index_base: int, model_height: int = 0, ffmpeg: str = "ffmpeg",
                 timeout: float = 60) -> list[Frame]:
    """Кадры не чаще step_sec только в [start_sec, end_sec] (секунды от начала, как у sample_frames).

    Быстрый seek на входе (-ss до ближайшего ключевого кадра) с -copyts: PTS остаются исходными, поэтому
    start_time контейнера вычитается здесь один раз. Индексы начинаются с index_base, чтобы не совпадать с
    основной выборкой.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    st = info.start_time_sec
    a, b = max(0.0, start_sec), min(info.duration_sec, end_sec)
    seek = max(0.0, a - 2.0)
    sel = (f"select='gte(t\\,{a + st:.6f})*lte(t\\,{b + st:.6f})*"
           f"(isnan(prev_selected_t)+gte(t-prev_selected_t\\,{step_sec}))',showinfo")
    n = ["-frames:v", str(max_frames)]
    if model_height:
        graph = f"[0:v]{sel},split=2[e][m];[m]scale=-2:{model_height}[ms]"
        maps = ["-map", "[e]", *n, "-fps_mode", "vfr", "-q:v", "2", str(out_dir / "e_%05d.jpg"),
                "-map", "[ms]", *n, "-fps_mode", "vfr", "-q:v", "3", str(out_dir / "m_%05d.jpg")]
    else:
        graph = f"[0:v]{sel}[e]"
        maps = ["-map", "[e]", *n, "-fps_mode", "vfr", "-q:v", "2", str(out_dir / "e_%05d.jpg")]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-xerror",
           "-ss", f"{seek + st:.6f}", "-copyts", "-t", f"{b - seek + 0.5:.6f}",
           "-i", str(video), "-filter_complex", graph, *maps, "-y"]
    r = _run(cmd, timeout)
    if r.returncode != 0:
        raise DecodeError("ffmpeg range decode failed")
    frames = []
    for i, t in enumerate(float(m.group(2)) - st for m in _PTS_RE.finditer(r.stderr)):
        if i >= max_frames:
            break
        if not -_PTS_EPS <= t <= info.duration_sec + _PTS_EPS:
            raise DecodeError("frame PTS outside video duration")
        e = out_dir / f"e_{i + 1:05d}.jpg"
        m = out_dir / f"m_{i + 1:05d}.jpg" if model_height else e
        if not e.exists() or not m.exists():
            raise DecodeError("frame count mismatch between showinfo and written files")
        frames.append(Frame(index=index_base + i, pts_sec=min(max(t, 0.0), info.duration_sec),
                            evidence_path=e, model_path=m))
    return frames


def annotate_boxes(src: Path, dst: Path, boxes, grid: int, ffmpeg: str = "ffmpeg", timeout: float = 30) -> None:
    """Копия JPEG с рамками; координаты — в относительной сетке grid × grid."""
    if timeout <= 0:
        raise AnalysisTimeoutError("no time left to annotate evidence")
    draw = ",".join(f"drawbox=x=iw*{x1}/{grid}:y=ih*{y1}/{grid}:w=iw*{x2 - x1}/{grid}:h=ih*{y2 - y1}/{grid}"
                    f":color=red@0.9:t=4" for x1, y1, x2, y2 in boxes)
    r = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", draw,
              "-frames:v", "1", "-q:v", "2", "-y", str(dst)], timeout)
    if r.returncode != 0 or not dst.is_file():
        raise DecodeError("ffmpeg annotate failed")


def windows(frames: list[Frame], window_sec: float) -> list[list[Frame]]:
    """Последовательные непересекающиеся окна по времени, покрывающие все выбранные кадры."""
    out: list[list[Frame]] = []
    for f in frames:
        if not out or f.pts_sec - out[-1][0].pts_sec >= window_sec:
            out.append([])
        out[-1].append(f)
    return out
