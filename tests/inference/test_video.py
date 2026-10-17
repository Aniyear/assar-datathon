"""Декодирование и таймкоды на синтетическом видео FFmpeg (без данных датасета)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.inference import prompts
from app.inference.video import DecodeError, probe, sample_frames, windows

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def _make(path: Path, seconds: int, extra: list[str] = ()) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=240x426:rate=30:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", *extra, "-y", str(path)], check=True)
    return path


def test_sample_frames_uses_real_pts(tmp_path):
    video = _make(tmp_path / "a.mov", 12)
    info = probe(video)
    assert 11.9 < info.duration_sec < 12.1
    frames = sample_frames(video, tmp_path / "f", 1.0, info, model_height=200)
    assert 11 <= len(frames) <= 13
    assert all(b.pts_sec > a.pts_sec for a, b in zip(frames, frames[1:]))
    assert all(0 <= f.pts_sec <= info.duration_sec for f in frames)
    # PTS реального кадра при 30 FPS кратен 1/30
    assert all(abs(f.pts_sec * 30 - round(f.pts_sec * 30)) < 1e-3 for f in frames)
    assert all(f.evidence_path.read_bytes()[:2] == b"\xff\xd8" for f in frames)
    ws = windows(frames, 5.0)
    assert sum(len(w) for w in ws) == len(frames)
    assert all(w[-1].pts_sec - w[0].pts_sec < 5.0 for w in ws)


def test_rotation_metadata_applied(tmp_path):
    base = _make(tmp_path / "base.mov", 2)
    video = tmp_path / "r.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(base), "-c", "copy", "-y",
                    str(video)], check=True)
    info = probe(video)
    assert abs(info.rotation) == 90
    frames = sample_frames(video, tmp_path / "f", 1.0, info)
    head = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                           str(frames[0].evidence_path)], capture_output=True, text=True, check=True).stdout
    w, h = map(int, head.strip().split(","))
    assert (w, h) == (426, 240)


def test_nonzero_start_time_not_clamped(tmp_path):
    base = _make(tmp_path / "b.mov", 4)
    video = tmp_path / "off.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(base), "-c", "copy", "-output_ts_offset", "2", "-y",
                    str(video)], check=True)
    info = probe(video)
    assert abs(info.start_time_sec - 2.0) < 1e-3
    frames = sample_frames(video, tmp_path / "f", 1.0, info)
    assert [round(f.pts_sec, 3) for f in frames] == [0.0, 1.0, 2.0, 3.0]


def test_vfr_uses_actual_frame_times(tmp_path):
    # 0–2 с при 30 FPS, далее кадры каждые 0,1 с
    video = tmp_path / "vfr.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=4",
                    "-vf", "setpts='if(lt(N,60),N/30,2+(N-60)/10)/TB'", "-fps_mode", "vfr",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    info = probe(video)
    frames = sample_frames(video, tmp_path / "f", 0.25, info)
    early = [f.pts_sec for f in frames if f.pts_sec < 2.0]
    late = [f.pts_sec for f in frames if f.pts_sec >= 2.0]
    assert all(abs(t * 30 - round(t * 30)) < 1e-3 for t in early)
    assert all(abs((t - 2.0) * 10 - round((t - 2.0) * 10)) < 1e-3 for t in late)
    assert all(b - a >= 0.25 - 1e-3 for a, b in zip([f.pts_sec for f in frames], [f.pts_sec for f in frames][1:]))
    assert late and max(late) <= info.duration_sec


def test_corrupt_video_raises(tmp_path):
    bad = tmp_path / "bad.mov"
    bad.write_bytes(b"\x00" * 1024)
    with pytest.raises(DecodeError):
        probe(bad)


def test_validate_rejects_unknown_frames():
    ok = {"frames_assessable": True, "observations": [
        {"category": prompts.CATEGORIES[0], "frame_indices": [3], "description": "x"}]}
    assert prompts.validate(ok, [3, 4])
    assert not prompts.validate(ok, [4, 5])
    assert not prompts.validate({"frames_assessable": True}, [3])
