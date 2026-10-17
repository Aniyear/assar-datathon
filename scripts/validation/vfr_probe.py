"""CPU-only duration diagnostics on generated patterns. Prints technical metadata only."""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.inference.video import probe, sample_frames


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)


def diagnose(work: Path) -> dict:
    report = {"ffmpeg": run(["ffmpeg", "-version"]).stdout.splitlines()[0], "cases": {}}
    for name, extra in (("vfr_bframes", []), ("vfr_no_bframes", ["-bf", "0"])):
        video = work / f"{name}.mov"
        run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=4",
             "-vf", "setpts='if(lt(N,60),N/30,2+(N-60)/10)/TB'", "-fps_mode", "vfr",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", *extra, "-y", str(video)])
        info = probe(video)
        raw = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(video), "-vf", "showinfo", "-f", "null", "-"])
        all_pts = [float(x) for x in re.findall(r"pts_time:([-\d.]+)", raw.stderr)]
        frames = sample_frames(video, work / name, 0.05, info)
        report["cases"][name] = {
            "duration": info.duration_sec, "unbounded_last_pts": max(all_pts),
            "sampled_last_pts": frames[-1].pts_sec, "sampled_frames": len(frames),
            "unbounded_frames_at_or_after_duration": sum(t >= info.duration_sec for t in all_pts),
        }
    # Combined offset AND rotation: preserve normalized PTS and rotated dimensions.
    base = work / "base.mov"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(base)])
    moved = work / "offset-rotation.mov"
    run(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(base),
         "-c", "copy", "-output_ts_offset", "2", "-y", str(moved)])
    info = probe(moved)
    frames = sample_frames(moved, work / "combined", 1.0, info)
    dimensions = run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                      "-of", "csv=p=0", str(frames[0].evidence_path)]).stdout.strip()
    report["cases"]["offset_rotation"] = {
        "duration": info.duration_sec, "start": info.start_time_sec, "rotation": info.rotation,
        "pts": [f.pts_sec for f in frames], "jpeg_dimensions": dimensions,
    }
    # Deliberately false probe metadata, not a claim to support a malformed MOV.
    # Shows that input -t is a cutoff, NOT an independent duration validator.
    correct = probe(base)
    shortened = sample_frames(base, work / "false-duration", 0.25,
                              dataclasses.replace(correct, duration_sec=1.0))
    report["known_limit"] = {"actual_duration": correct.duration_sec, "injected_duration": 1.0,
                             "last_pts": shortened[-1].pts_sec, "silent_cutoff": True}
    return report


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="assar-synthetic-vfr-") as folder:
        print(json.dumps(diagnose(Path(folder)), indent=2))
