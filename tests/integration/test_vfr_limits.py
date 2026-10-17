"""Keep duration limitations explicit, without claiming arbitrary MOV support."""
from types import SimpleNamespace

import pytest

from app.contracts import DecodeError
from app.inference import video
from scripts.validation.vfr_probe import diagnose


def test_input_duration_cutoff_and_combined_offset_rotation(tmp_path):
    report = diagnose(tmp_path)
    bframes = report["cases"]["vfr_bframes"]
    assert bframes["sampled_last_pts"] < bframes["duration"]
    assert bframes["sampled_last_pts"] >= bframes["duration"] - 0.101
    no_bframes = report["cases"]["vfr_no_bframes"]
    assert abs(no_bframes["sampled_last_pts"] - no_bframes["unbounded_last_pts"]) < 0.001
    combined = report["cases"]["offset_rotation"]
    assert combined["start"] == 2.0 and abs(combined["rotation"]) == 90
    assert combined["pts"] == [0.0, 1.0, 2.0, 3.0]
    assert combined["jpeg_dimensions"] == "284,160"
    # Characterization of an explicitly UNSUPPORTED duration mismatch, not success evidence.
    assert report["known_limit"]["actual_duration"] == 4.0
    assert report["known_limit"]["last_pts"] < 1.0


def test_out_of_range_pts_still_raises_instead_of_clamping(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "_run", lambda *args: SimpleNamespace(
        returncode=0, stderr="[Parsed_showinfo_1] n: 0 pts: 31 pts_time:3.1", stdout=""))
    with pytest.raises(DecodeError, match="PTS outside"):
        video.sample_frames(tmp_path / "synthetic.mov", tmp_path / "frames", 1.0,
                            video.VideoInfo(3.0, 0, 160, 120, 0, "h264", 30))
