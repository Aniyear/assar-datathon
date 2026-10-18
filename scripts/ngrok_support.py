"""Pinned ngrok support; production requires explicit --publish and readiness in service.py.

Raw agent output and tokens are never persisted to logs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

VERSION = "3.39.11"
SHA256 = "d339bcbd0713233337e860163f5249eea679cf26750a5700510dbc241d201748"
FREE_ORIGIN = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.ngrok-free\.(?:app|dev)")


def isolated_config(token: str) -> dict:
    return {"version": "3", "agent": {"authtoken": token, "console_ui": False, "web_addr": False,
            "remote_management": False, "update_check": False, "log": "false"}}


def import_official_auth(destination: Path, source: Path | None = None) -> bool:
    """Use the user's official add-authtoken setup; no token values leave this process.

    Read only the simple authtoken scalar written by ngrok, never reuse other
    profile settings or tunnel definitions. Caller protects the destination directory.
    """
    if source is None:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        paths = [local / "ngrok/ngrok.yml",
                 Path.home() / ".ngrok2/ngrok.yml"]
        source = next((p for p in paths if p.is_file()), None)
        if source is None:
            # Microsoft Store redirects the official agent's LocalAppData here.
            store_paths = list((local / "Packages").glob("ngrok.ngrok_*/LocalCache/Local/ngrok/ngrok.yml"))
            if len(store_paths) > 1:
                raise ValueError("Multiple ngrok Store profiles found; select the intended config explicitly")
            source = next(iter(store_paths), None)
    if source is None:
        return False
    matches = re.findall(r"(?m)^\s*authtoken:[ \t]*(['\"]?)([A-Za-z0-9_-]{20,200})\1[ \t]*(?:#[^\n]*)?$",
                         source.read_text(encoding="utf-8-sig"))
    if len(matches) != 1:
        raise ValueError("Official ngrok config has no single supported authtoken scalar")
    destination.write_text(json.dumps(isolated_config(matches[0][1])), encoding="utf-8")
    return True


def valid_origin(value: str) -> bool:
    return isinstance(value, str) and FREE_ORIGIN.fullmatch(value) is not None


def validate(binary: Path, config: Path) -> None:
    with binary.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise ValueError("ngrok binary SHA256 mismatch")
    cfg = json.loads(config.read_text(encoding="utf-8"))
    token = cfg.get("agent", {}).get("authtoken", "")
    expected = isolated_config(token)
    if cfg != expected or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", token):
        raise ValueError("Invalid isolated ngrok config; repeat official ngrok config add-authtoken locally")


def command(binary: Path, config: Path, upstream: str) -> list[str]:
    # No --url: free accounts use their assigned persistent dev domain.
    # JSON agent output goes to a memory-only sanitizer, never a logfile.
    return [str(binary), "http", upstream, "--config", str(config), "--inspect=false",
            "--log", "stdout", "--log-format", "json", "--log-level", "info"]


def collect_output(stream, facts: dict) -> None:
    """Keep ONLY an allowlisted public origin and ngrok error codes, discard all else.

    Authentication failures may echo the token in their error text. Never store or
    print raw lines, even on failure. The caller joins this reader after tree cleanup.
    """
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            codes = re.findall(r"\bERR_NGROK_\d+\b", text)
            if codes:
                facts["error_codes"] = sorted(set(facts.get("error_codes", []) + codes))
            try:
                record = json.loads(text)
            except ValueError:
                continue
            if isinstance(record, dict) and valid_origin(record.get("url")):
                facts["public_url"] = record["url"]
    finally:
        stream.close()
