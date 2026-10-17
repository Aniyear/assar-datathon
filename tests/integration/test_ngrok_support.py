"""No account or network: pinning, output redaction and endpoint isolation."""
import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import ngrok_support, service


def test_store_official_profile_is_found_without_printing_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    source = tmp_path / "Packages/ngrok.ngrok_synthetic/LocalCache/Local/ngrok/ngrok.yml"
    source.parent.mkdir(parents=True)
    source.write_text('version: "3"\nagent:\n  authtoken: SYNTHETIC_NOT_A_REAL_ACCOUNT_TOKEN\n')
    dest = tmp_path / "private.json"
    assert ngrok_support.import_official_auth(dest)
    assert json.loads(dest.read_text()) == ngrok_support.isolated_config("SYNTHETIC_NOT_A_REAL_ACCOUNT_TOKEN")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("version,quote", [("2", ""), ("3", '"'), ("3", "'")])
def test_official_config_imports_only_token_without_mutating_source(tmp_path, capsys, version, quote):
    token = "SYNTHETIC_SECRET_NOT_A_REAL_NGROK_TOKEN"
    source = tmp_path / "official.yml"
    text = f'version: "{version}"\nagent:\n  authtoken: {quote}{token}{quote}\n  web_addr: 0.0.0.0:4040\n'
    source.write_text(text)
    dest = tmp_path / "private.json"
    assert ngrok_support.import_official_auth(dest, source)
    assert source.read_text() == text
    cfg = json.loads(dest.read_text())
    assert cfg["agent"]["authtoken"] == token and cfg["agent"]["web_addr"] is False
    assert capsys.readouterr().out == ""


def test_ambiguous_official_auth_is_rejected_without_echo(tmp_path):
    token = "SYNTHETIC_SECRET_NOT_A_REAL_NGROK_TOKEN"
    source = tmp_path / "official.yml"
    source.write_text(f"authtoken: {token}\nauthtoken: {token}\n")
    dest = tmp_path / "private.json"
    with pytest.raises(ValueError) as exc:
        ngrok_support.import_official_auth(dest, source)
    assert token not in str(exc.value) and not dest.exists()


@pytest.mark.parametrize("url", ["https://synthetic-dev.ngrok-free.app", "https://synthetic-dev.ngrok-free.dev"])
def test_only_assigned_free_https_origin_allowed(url):
    assert ngrok_support.valid_origin(url)
    for bad in (url + ".evil.example", url + "/private", url + "?key=synthetic",
                url.replace("https://", "http://"), url.replace("https://", "https://user@"), None):
        assert not ngrok_support.valid_origin(bad)


def test_agent_output_discarded_except_public_origin_and_error_code(capsys):
    token = "SYNTHETIC_SECRET_NOT_A_REAL_NGROK_TOKEN"
    messages = [json.dumps({"err": f"bad auth {token}: ERR_NGROK_105"}),
                json.dumps({"url": "https://attacker.example", "authtoken": token}),
                "invalid json", json.dumps({"url": "https://synthetic-dev.ngrok-free.app", "token": token})]
    stream = io.BytesIO(("\n".join(messages) + "\n").encode())
    facts = {}
    ngrok_support.collect_output(stream, facts)
    assert facts == {"error_codes": ["ERR_NGROK_105"], "public_url": "https://synthetic-dev.ngrok-free.app"}
    assert token not in repr(facts) and capsys.readouterr().out == ""
    assert stream.closed


def test_command_disables_inspection_and_keeps_token_out_of_argv(tmp_path):
    config = tmp_path / "private.json"
    token = "SYNTHETIC_SECRET_NOT_A_REAL_NGROK_TOKEN"
    config.write_text(json.dumps({"agent": {"authtoken": token}}))
    argv = ngrok_support.command(tmp_path / "ngrok.exe", config, "http://127.0.0.1:18766")
    assert "--inspect=false" in argv
    assert token not in repr(argv)
    assert "--url" not in argv  # Ask ngrok for the account's assigned domain.


def test_pinned_binary_and_isolated_config_required(tmp_path, monkeypatch):
    binary = tmp_path / "ngrok.exe"
    binary.write_bytes(b"synthetic-binary")
    config = tmp_path / "private.json"
    cfg = {"version": "3", "agent": {"authtoken": "synthetic_token_12345678901234567890",
            "console_ui": False, "web_addr": False, "remote_management": False,
            "update_check": False, "log": "false"}}
    config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="SHA256"):
        ngrok_support.validate(binary, config)
    monkeypatch.setattr(ngrok_support, "SHA256", hashlib.sha256(binary.read_bytes()).hexdigest())
    ngrok_support.validate(binary, config)
    cfg["agent"]["web_addr"] = "0.0.0.0:4040"
    config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError) as exc:
        ngrok_support.validate(binary, config)
    assert cfg["agent"]["authtoken"] not in str(exc.value)


def test_ngrok_cannot_publish_production_or_combine_tunnels():
    with pytest.raises(SystemExit) as exc:
        service.main(["start", "--ngrok", "fake.exe"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        service.main(["start", "--mode", "network-test", "--ngrok", "fake.exe", "--tunnel", "another.exe"])
    assert exc.value.code == 2
