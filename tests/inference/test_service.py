"""app.inference.service — интерфейс backend (ML_INTERFACE.md @ 1a3bda0) на CPU с синтетическими данными."""
import copy
import hashlib
import json
import math
import pickle
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.contracts import (AnalysisError, AnalysisResult, AnalysisTimeoutError, DecodeError,
                           InvalidAnalysisResultError, ModelUnavailableError)
from app.inference import AnalyzerNotReadyError, service
from tests.inference.fakes import FakeLlama, catalog, ok, synthetic_rule

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def test_backend_names_exist():
    for name in ("check_runtime", "server_command", "create_analyzer"):
        assert callable(getattr(service, name))


def test_server_command_flags():
    argv = service.server_command(CFG, ROOT, 4321)
    joined = " ".join(argv)
    for flag in ("--host 127.0.0.1", "--port 4321", "--offline", "-np 1", "--no-webui", "--jinja",
                 "-c 8192", "-ngl 99", "--image-max-tokens 256"):
        assert flag in joined
    assert Path(argv[0]) == ROOT / "models/llama.cpp-b11669/llama-server.exe"
    assert Path(argv[argv.index("-m") + 1]).is_absolute()


@pytest.mark.parametrize("port", [0, 70000, True, "8080"])
def test_server_command_rejects_bad_port(port):
    with pytest.raises(ModelUnavailableError):
        service.server_command(CFG, ROOT, port)


def test_server_command_rejects_unpinned_flags():
    cfg = copy.deepcopy(CFG)
    cfg["server"]["offline"] = False
    with pytest.raises(ModelUnavailableError):
        service.server_command(cfg, ROOT, 4321)


def _fake_runtime(tmp_path):
    cfg = copy.deepcopy(CFG)
    files = {"weights": b"w" * 64, "mmproj": b"m" * 32}
    for key, data in files.items():
        p = tmp_path / f"{key}.bin"
        p.write_bytes(data)
        cfg[key].update(path=p.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    server = tmp_path / "server.exe"
    server.write_bytes(b"s")
    cfg["runtime"].update(server_bin=server.name, server_sha256_reported=hashlib.sha256(b"s").hexdigest())
    return cfg


def test_check_runtime_ok_and_mismatch(tmp_path):
    cfg = _fake_runtime(tmp_path)
    service.check_runtime(cfg, tmp_path)
    (tmp_path / "mmproj.bin").write_bytes(b"x" * 32)  # тот же размер, другой хеш
    with pytest.raises(ModelUnavailableError):
        service.check_runtime(cfg, tmp_path)


def test_check_runtime_missing_and_invalid(tmp_path):
    with pytest.raises(ModelUnavailableError):
        service.check_runtime(CFG, tmp_path)  # файлов нет
    with pytest.raises(ModelUnavailableError):
        service.check_runtime({"backend": "llama-server"}, tmp_path)


def test_check_runtime_server_hash(tmp_path):
    cfg = _fake_runtime(tmp_path)
    (tmp_path / "server.exe").write_bytes(b"t")
    with pytest.raises(ModelUnavailableError):
        service.check_runtime(cfg, tmp_path)


def test_create_analyzer_init_failures():
    srv = FakeLlama(lambda idx: ok())
    try:
        with pytest.raises(AnalyzerNotReadyError):
            service.create_analyzer(srv.url, CFG, catalog(), ROOT)
        with pytest.raises(ModelUnavailableError):
            service.create_analyzer("http://localhost:1", CFG, catalog(synthetic_rule()), ROOT)
    finally:
        srv.close()
    with pytest.raises(ModelUnavailableError):  # сервер уже остановлен
        service.create_analyzer(srv.url, CFG, catalog(synthetic_rule()), ROOT)


@pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan, True, "120"])
def test_create_analyzer_rejects_bad_call_timeout(bad):
    with pytest.raises(ModelUnavailableError):
        service.create_analyzer("http://127.0.0.1:9", CFG, catalog(synthetic_rule()), ROOT, call_timeout_sec=bad)


@needs_ffmpeg
def test_call_timeout_sec_limits_each_request(tmp_path):
    """Значение ≠ 120 доходит до запроса: сервер отвечает через 4 с, предел 1,5 с, deadline 30 с."""
    srv = FakeLlama(lambda idx: ("sleep", 4))
    try:
        fn = service.create_analyzer(srv.url, CFG, catalog(synthetic_rule()), ROOT, call_timeout_sec=1.5)
        video = tmp_path / "input.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
        job = tmp_path / "job"
        job.mkdir()
        t0 = time.monotonic()
        with pytest.raises(AnalysisTimeoutError):
            fn(video, job, time.monotonic() + 30)
        assert 1.4 < time.monotonic() - t0 < 3.5
        # и общий deadline меньше call_timeout_sec: ограничивает остаток бюджета
        fn2 = service.create_analyzer(srv.url, CFG, catalog(synthetic_rule()), ROOT, call_timeout_sec=60)
        t0 = time.monotonic()
        with pytest.raises(AnalysisTimeoutError):
            fn2(video, job, time.monotonic() + 2.0)
        assert time.monotonic() - t0 < 3.5
    finally:
        srv.close()


@pytest.fixture
def bound(tmp_path):
    srv = FakeLlama(lambda idx: ok([{"category": "cash_bag_handover", "frame_indices": idx[:2],
                                     "description": "synthetic"}]) if idx[0] == 0 else ok())
    yield service.create_analyzer(srv.url, CFG, catalog(synthetic_rule()), ROOT)
    srv.close()


@needs_ffmpeg
def test_bound_analyzer_result_pickles(bound, tmp_path):
    video = tmp_path / "in" / "input.mov"
    video.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    job = tmp_path / "job"
    job.mkdir()
    res = bound(video, job, time.monotonic() + 30)
    assert isinstance(res, AnalysisResult) and res.verdict == "violation"
    again = AnalysisResult.model_validate(pickle.loads(pickle.dumps(res.model_dump())))
    assert again == res


def test_argument_errors_are_analysis_errors(bound, tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(DecodeError):
        bound(Path("relative.mov"), job, time.monotonic() + 10)
    with pytest.raises(DecodeError):
        bound(tmp_path / "missing.mov", job, time.monotonic() + 10)
    video = tmp_path / "input.mov"
    video.write_bytes(b"\0")
    for bad_job in (Path("rel"), tmp_path / "nojob"):
        with pytest.raises(AnalysisError) as e:
            bound(video, bad_job, time.monotonic() + 10)
        assert type(e.value) is AnalysisError and str(tmp_path) not in str(e.value)
    with pytest.raises(AnalysisError) as e:
        bound(video, job, math.nan)
    assert isinstance(e.value.__cause__, ValueError)


@needs_ffmpeg
def test_unexpected_failure_is_wrapped(bound, tmp_path, monkeypatch):
    from app.inference import analyzer as analyzer_mod
    video = tmp_path / "input.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
    job = tmp_path / "job"
    job.mkdir()

    def boom(*a, **k):
        raise OSError("disk full at C:\\secret\\path")
    monkeypatch.setattr(analyzer_mod, "windows", boom)
    with pytest.raises(AnalysisError) as e:
        bound(video, job, time.monotonic() + 10)
    assert type(e.value) is AnalysisError and "secret" not in str(e.value)
    assert isinstance(e.value.__cause__, OSError)


def test_invalid_model_output_stays_soft_error(tmp_path):
    srv = FakeLlama(lambda idx: (200, "not json", "stop"))
    try:
        fn = service.create_analyzer(srv.url, CFG, catalog(synthetic_rule()), ROOT)
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not in PATH")
        video = tmp_path / "input.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)], check=True)
        job = tmp_path / "job"
        job.mkdir()
        with pytest.raises(InvalidAnalysisResultError):
            fn(video, job, time.monotonic() + 10)
    finally:
        srv.close()
