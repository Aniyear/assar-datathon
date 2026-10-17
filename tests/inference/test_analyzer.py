"""analyze_video на синтетическом видео и синтетическом каталоге; модель заменена тестовым loopback-сервером.

Тестовый сервер существует только здесь и не подключается в runtime.
"""
import json
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.contracts import (AnalysisTimeoutError, DecodeError, InvalidAnalysisResultError, ModelUnavailableError,
                           RuleCatalog, UnassessableVideoError, validate_analysis_for_video, validate_analysis_rules)
from app.inference import Analyzer, AnalyzerNotReadyError, LlamaClient, load_model_config, load_rule_catalog
from app.inference import analyzer as analyzer_mod

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")


def synthetic_rule(type_id="cash_bag_handover", status="verified", rule_id="SYNTH-01"):
    return {"rule_id": rule_id, "type_id": type_id, "type_name_ru": "Синтетический тип (тест)",
            "source_title": "Синтетический источник для тестов", "source_url": "https://example.invalid/synthetic",
            "clause": None, "effective_version_checked_at": "2026-10-17" if status == "verified" else None,
            "scope": "test only", "observable_condition": "synthetic", "required_context": [],
            "counterexamples": [], "claim_template_ru": "Синтетический шаблон", "status": status}


def catalog(*rules):
    return RuleCatalog.model_validate({"schema_version": "0.1", "rules": list(rules)})


class FakeLlama:
    """responder(frame_indices: list[int]) -> (http_status, content: str, finish_reason) или ('sleep', sec)."""

    def __init__(self, responder):
        self.responder, self.calls = responder, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"status": "ok"}) if self.path == "/health" else self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                texts = [c["text"] for c in body["messages"][0]["content"] if c["type"] == "text"]
                idx = [int(m.group(1)) for t in texts for m in [re.match(r"Frame (\d+), t=", t)] if m]
                outer.calls += 1
                r = outer.responder(idx)
                if r[0] == "sleep":
                    time.sleep(r[1])
                    r = (200, json.dumps({"frames_assessable": True, "observations": []}), "stop")
                code, content, finish = r
                self._send(code, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                  "usage": {}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ok(obs=(), assessable=True):
    return 200, json.dumps({"frames_assessable": assessable, "observations": list(obs)}), "stop"


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "upload-random.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x284:rate=30:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(p)], check=True)
    return p


@pytest.fixture
def cfg():
    return load_model_config(ROOT / "configs/model.yaml")


def run(cfg, cat, responder, video, tmp_path, budget=60.0):
    srv = FakeLlama(responder)
    try:
        a = Analyzer(cfg, cat, LlamaClient(srv.url), call_timeout_sec=10)
        job = tmp_path / "job"
        job.mkdir()
        return a.analyze_video(video, job, time.monotonic() + budget), job, srv
    finally:
        srv.close()


def test_repo_config_matches_working_baseline(cfg):
    s = cfg.server
    assert (s.ctx, s.image_max_tokens, s.n_gpu_layers, s.parallel, s.offline) == (8192, 256, 99, 1, True)
    assert (cfg.max_tokens, cfg.temperature, cfg.top_k, cfg.seed, cfg.cache_prompt) == (384, 0.0, 1, 0, False)
    assert (cfg.step_sec, cfg.window_sec, cfg.model_height) == (1.0, 10.0, 0)
    assert (cfg.quantization, cfg.mmproj_quantization, cfg.runtime_tag) == ("Q8_0", "Q8_0", "b11669")


def test_empty_repo_catalog_blocks_production(cfg):
    cat = load_rule_catalog(ROOT / "app/rules/catalog.json")
    with pytest.raises(AnalyzerNotReadyError):
        Analyzer(cfg, cat, LlamaClient("http://127.0.0.1:9"))


def test_candidate_rules_are_not_activated(cfg):
    with pytest.raises(AnalyzerNotReadyError):
        Analyzer(cfg, catalog(synthetic_rule(status="candidate")), LlamaClient("http://127.0.0.1:9"))


def test_verified_type_must_be_a_prompt_category(cfg):
    with pytest.raises(AnalyzerNotReadyError):
        Analyzer(cfg, catalog(synthetic_rule(type_id="not_in_prompt")), LlamaClient("http://127.0.0.1:9"))


def test_client_rejects_non_loopback():
    for url in ("http://localhost:1", "https://127.0.0.1:1", "http://10.0.0.1:1", "http://127.0.0.1:1/x"):
        with pytest.raises(ValueError):
            LlamaClient(url)


def test_violation_uses_real_pts_and_jpeg(cfg, video, tmp_path):
    def responder(idx):
        if idx[0] == 0:
            return ok([{"category": "cash_bag_handover", "frame_indices": idx[2:7], "description": "synthetic"}])
        return ok()
    cat = catalog(synthetic_rule())
    res, job, _ = run(cfg, cat, responder, video, tmp_path)
    assert res.verdict == "violation" and len(res.events) == 1
    ev = res.events[0]
    assert ev.rule_id == "SYNTH-01" and ev.type_id == "cash_bag_handover"
    assert 1 <= len(ev.evidence) <= 3
    assert ev.start_sec <= ev.timecode_sec <= ev.end_sec
    for e in ev.evidence:
        p = Path(e.local_jpeg_path)
        assert p.is_absolute() and p.is_relative_to(job.resolve())
        assert abs(e.timestamp_sec * 30 - round(e.timestamp_sec * 30)) < 1e-3  # реальный кадр 30 FPS
    validate_analysis_for_video(res, 25.0, job)
    validate_analysis_rules(res, cat)
    assert res.model_revision.startswith("Qwen/Qwen3-VL-2B-Instruct-GGUF@52d6c8f")


def test_adjacent_windows_merge_distinct_kept(cfg, video, tmp_path):
    def responder(idx):
        if idx[0] in (0, 10):  # конец 1-го окна и начало 2-го — одно действие
            fi = idx[-3:] if idx[0] == 0 else idx[:3]
            return ok([{"category": "cash_bag_handover", "frame_indices": fi, "description": "a"}])
        return ok([{"category": "cash_bag_handover", "frame_indices": idx[-1:], "description": "b"}])
    res, _, _ = run(cfg, catalog(synthetic_rule()), responder, video, tmp_path)
    assert len(res.events) == 2
    assert [e.timecode_sec for e in res.events] == sorted(e.timecode_sec for e in res.events)
    assert res.events[0].end_sec - res.events[0].start_sec >= 4.0


def test_no_verified_category_gives_no_violation_with_limitation(cfg, video, tmp_path):
    responder = lambda idx: ok([{"category": "other_notable_action", "frame_indices": idx[:1], "description": "x"}])
    res, _, _ = run(cfg, catalog(synthetic_rule()), responder, video, tmp_path)
    assert res.verdict == "no_violation" and res.events == []
    assert any("without a verified rule" in x for x in res.limitations)


def test_all_unassessable_raises(cfg, video, tmp_path):
    with pytest.raises(UnassessableVideoError):
        run(cfg, catalog(synthetic_rule()), lambda idx: ok(assessable=False), video, tmp_path)


@pytest.mark.parametrize("reply", [
    (200, '{"frames_assessable": true, "observ', "length"),
    (200, "not json", "stop"),
    (200, json.dumps({"frames_assessable": True, "observations": [
        {"category": "cash_bag_handover", "frame_indices": [999], "description": "x"}]}), "stop"),
])
def test_invalid_model_output_fails(cfg, video, tmp_path, reply):
    with pytest.raises(InvalidAnalysisResultError):
        run(cfg, catalog(synthetic_rule()), lambda idx: reply, video, tmp_path)


def test_server_error_is_model_unavailable(cfg, video, tmp_path):
    with pytest.raises(ModelUnavailableError):
        run(cfg, catalog(synthetic_rule()), lambda idx: (500, "{}", "stop"), video, tmp_path)


def test_server_down_is_model_unavailable(cfg, video, tmp_path):
    srv = FakeLlama(lambda idx: ok())
    url = srv.url
    srv.close()
    job = tmp_path / "job"
    job.mkdir()
    a = Analyzer(cfg, catalog(synthetic_rule()), LlamaClient(url), call_timeout_sec=5)
    with pytest.raises(ModelUnavailableError):
        a.analyze_video(video, job, time.monotonic() + 30)


def test_expired_deadline(cfg, video, tmp_path):
    with pytest.raises(AnalysisTimeoutError):
        run(cfg, catalog(synthetic_rule()), lambda idx: ok(), video, tmp_path, budget=-1)


def test_deadline_caps_model_call(cfg, video, tmp_path):
    t0 = time.monotonic()
    with pytest.raises(AnalysisTimeoutError):
        run(cfg, catalog(synthetic_rule()), lambda idx: ("sleep", 5), video, tmp_path, budget=2.0)
    assert time.monotonic() - t0 < 4.5


def test_proxy_env_is_bypassed(cfg, video, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    res, _, _ = run(cfg, catalog(synthetic_rule()), lambda idx: ok(), video, tmp_path)
    assert res.verdict == "no_violation"


def test_corrupt_upload_is_decode_error(cfg, tmp_path):
    bad = tmp_path / "bad.mov"
    bad.write_bytes(b"\x00" * 2048)
    with pytest.raises(DecodeError):
        run(cfg, catalog(synthetic_rule()), lambda idx: ok(), bad, tmp_path)


def test_module_function_requires_init(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer_mod, "_ANALYZER", None)
    with pytest.raises(ModelUnavailableError):
        analyzer_mod.analyze_video(tmp_path / "x.mov", tmp_path, time.monotonic() + 10)


def test_init_worker_with_synthetic_catalog(tmp_path, monkeypatch):
    cat_path = tmp_path / "catalog.json"
    cat_path.write_text(json.dumps({"schema_version": "0.1", "rules": [synthetic_rule()]}), encoding="utf-8")
    srv = FakeLlama(lambda idx: ok())
    try:
        monkeypatch.setattr(analyzer_mod, "_ANALYZER", None)
        a = analyzer_mod.init_worker(model_config_path=ROOT / "configs/model.yaml", rules_catalog_path=cat_path,
                                     server_url=srv.url)
        assert analyzer_mod._ANALYZER is a
    finally:
        srv.close()
