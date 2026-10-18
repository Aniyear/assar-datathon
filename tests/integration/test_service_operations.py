"""Operator entrypoint: no GPU. Test real HTTP, shutdown, isolation and refusal."""
import argparse
import copy
import json
from pathlib import Path
import socket
import threading
import time
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from scripts import service
from scripts.network_probe import create_probe
from tests.api.harness import LiveServer


def test_atomic_state_replace_survives_windows_reader(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    service.atomic_json(path, {"old": True})
    original = Path.replace
    calls = []
    def shared_reader(source, target):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("synthetic Windows sharing violation")
        return original(source, target)
    monkeypatch.setattr(Path, "replace", shared_reader)
    service.atomic_json(path, {"new": True})
    assert len(calls) == 3 and json.loads(path.read_text()) == {"new": True}


def test_redirected_windows_encoding_does_not_crash():
    result = subprocess.run([sys.executable, "-m", "scripts.service", "check", "--mode", "network-test"],
                            env=dict(os.environ, PYTHONIOENCODING="cp1252"), capture_output=True,
                            cwd=service.ROOT, timeout=10)
    assert result.returncode == 0
    assert "ГОТОВО" in result.stdout.decode("utf-8")


def test_network_probe_has_auth_and_no_upload_or_production_api():
    with LiveServer(create_probe("synthetic-test-key", "synthetic-run")) as live:
        assert "ТЕСТ СЕТИ" in live.request("GET", "/")[1]
        assert live.request("GET", "/health")[1]["inference"] is False
        assert live.request("GET", "/api/test")[0] == 401
        assert live.request("GET", "/api/test", "wrong")[0] == 401
        assert live.request("GET", "/api/test", "synthetic-test-key")[1]["result"] == "synthetic-network-ok"
        assert live.request("POST", "/api/v1/analyze", "synthetic-test-key", b"synthetic")[0] == 405
        assert live.request("GET", "/api/v1/jobs/anything", "synthetic-test-key")[0] == 404
        assert live.request("GET", "/outputs/service/network-test/private/api-key.txt")[0] == 404


def test_resolve_paths_preserves_readiness_and_source(tmp_path):
    source = service.ROOT / "configs/model.yaml"
    original = json.loads(source.read_text(encoding="utf-8"))
    cfg = service.resolved_config(source, tmp_path)
    expected = copy.deepcopy(original)
    for part, field in (("runtime", "server_bin"), ("weights", "path"), ("mmproj", "path")):
        expected[part][field] = str((tmp_path / original[part][field]).resolve())
    assert cfg == expected
    assert json.loads(source.read_text(encoding="utf-8")) == original


def test_tunnel_cannot_publish_production(capsys):
    with pytest.raises(SystemExit) as exc:
        service.main(["start", "--tunnel", "fake.exe"])
    assert exc.value.code == 2
    assert "network-test" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["start", "--publish"],
    ["check", "--ngrok", "fake.exe", "--publish"],
    ["start", "--mode", "network-test", "--ngrok", "fake.exe", "--publish"],
    ["start", "--ngrok", "fake.exe"],
])
def test_publication_requires_explicit_production_start(args, monkeypatch):
    monkeypatch.setattr(service, "start", lambda *args: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as exc:
        service.main(args)
    assert exc.value.code == 2


def test_explicit_publication_dispatch_keeps_production_mode(monkeypatch, tmp_path):
    calls = []
    def record(args, state_dir):
        calls.append((args.mode, args.publish, args.ngrok, args.model_config, state_dir))
        return 0
    monkeypatch.setattr(service, "start", record)
    assert service.main(["start", "--ngrok", str(tmp_path / "ngrok.exe"), "--publish",
                         "--model-config", str(tmp_path / "model.json")]) == 0
    assert calls == [("production", True, (tmp_path / "ngrok.exe").resolve(),
                      (tmp_path / "model.json").resolve(), service.ROOT / "outputs/service/production")]


def test_publish_flag_never_bypasses_readiness_or_imports_auth(tmp_path, monkeypatch):
    from scripts import ngrok_support
    monkeypatch.setattr(service, "preflight", lambda _: {"ready": False, "problems": ["synthetic blocked"],
                                                       "runtime_hashes_checked": False})
    monkeypatch.setattr(ngrok_support, "import_official_auth", lambda *args: pytest.fail("must not read auth"))
    args = argparse.Namespace(mode="production", publish=True, ngrok=tmp_path / "ngrok.exe")
    assert service.start(args, tmp_path) == 2
    assert not service.key_file(tmp_path).exists()
    assert not (tmp_path / "state.json").exists()


def test_direct_start_also_requires_publish_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "preflight", lambda _: pytest.fail("must not enter startup"))
    with pytest.raises(ValueError, match="explicit --publish"):
        service.start(argparse.Namespace(mode="production", ngrok=tmp_path / "ngrok.exe"), tmp_path)


def test_unverified_tunnel_binary_rejected(tmp_path):
    binary = tmp_path / "fake.exe"
    binary.write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="SHA256"):
        service.validate_tunnel(binary)


def test_not_ready_never_creates_key_server_or_resolved_config(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "preflight", lambda _: {"ready": False, "problems": ["synthetic blocked"],
                                                       "runtime_hashes_checked": False})
    assert service.start(argparse.Namespace(mode="production"), tmp_path) == 2
    assert not service.key_file(tmp_path).exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "resolved-model.json").exists()


def test_start_duplicate_stop_and_key_reuse_without_pid_signals(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = argparse.Namespace(mode="network-test", port=port, tunnel=None)
    result = []
    thread = threading.Thread(target=lambda: result.append(service.start(args, tmp_path)))
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            path = tmp_path / "state.json"
            if path.exists() and json.loads(path.read_text())["phase"] == "running":
                break
            time.sleep(.1)
        else:
            raise AssertionError("launcher did not start")
        key = service.key_file(tmp_path).read_text()
        assert service.load_or_create_key(tmp_path) == key
        state = path.read_text()
        assert key not in state
        from app.jobs.supervisor import StartupError
        with pytest.raises(StartupError):
            service.start(args, tmp_path)
        # A public GET cannot trigger shutdown; there is no remote control endpoint.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(f"http://127.0.0.1:{port}/stop", timeout=3)
        assert exc.value.code == 404
    finally:
        assert service.control("stop", tmp_path) == 0
        thread.join(timeout=10)
    assert not thread.is_alive() and result == [0]
    assert json.loads(path.read_text())["phase"] == "stopped"
    assert service.control("stop", tmp_path) == 0
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", port)) != 0
