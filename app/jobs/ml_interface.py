"""What the backend imports from ML. Backend-side description only; no ML code here.

Omar implements module ``app.inference.service`` with exactly these three functions.
The backend imports it lazily, after ``configure_offline_environment()``; if the
module or a function is missing, production startup is refused (no fallback).
See app/jobs/ML_INTERFACE.md for the full behavioural requirements.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from app.contracts import RuleCatalog, VideoAnalyzer

ML_SERVICE_MODULE = "app.inference.service"


class CheckRuntime(Protocol):
    """API process, once at startup. Verify server binary, GGUF and mmproj exist and
    match configs/model.yaml (SHA256/size). No spawn, no network, no GPU.
    Raise app.contracts.ModelUnavailableError on any mismatch."""
    def __call__(self, model_cfg: dict[str, Any], repo_root: Path) -> None: ...


class ServerCommand(Protocol):
    """API process. Pure function: llama-server argv for 127.0.0.1:<port> from the
    pinned config (--offline, -np 1, --no-webui, local paths). Must not spawn."""
    def __call__(self, model_cfg: dict[str, Any], repo_root: Path, port: int) -> list[str]: ...


class CreateAnalyzer(Protocol):
    """Worker process, once per worker start, after the supervisor's server is
    healthy. Returns analyze_video bound to a client of ``endpoint``. Must not start,
    stop or download anything; should return within a few seconds."""
    def __call__(self, endpoint: str, model_cfg: dict[str, Any], catalog: RuleCatalog,
                 repo_root: Path, call_timeout_sec: float = 120.0) -> VideoAnalyzer: ...
