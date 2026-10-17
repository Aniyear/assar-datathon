"""Точка подключения ML к backend supervisor (app/jobs/ML_INTERFACE.md, backend 1a3bda0).

Тонкий адаптер над config/vlm/analyzer: процессов не запускает. llama-server запускает, проверяет
и останавливает только supervisor; здесь — проверка файлов, argv сервера и analyzer для worker'а.
Наружу уходят только наследники AnalysisError без внутренних путей и ответов модели.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from app.contracts import AnalysisError, AnalysisResult, ModelUnavailableError, RuleCatalog, VideoAnalyzer

from .analyzer import Analyzer
from .config import ModelConfig, ModelConfigError, parse_model_config, verify_runtime_files
from .vlm import LlamaClient, build_command

HEALTH_TIMEOUT_SEC = 2.0


def _config(model_cfg: dict[str, Any], repo_root: Path) -> ModelConfig:
    try:
        cfg = parse_model_config(model_cfg, Path(repo_root))
    except ModelConfigError as e:
        raise ModelUnavailableError("model config is invalid") from e
    s = cfg.server
    # Требования ML_INTERFACE к argv; отличие config — отказ старта, а не молчаливая подмена.
    if not (s.offline and s.no_webui and s.jinja and s.parallel == 1):
        raise ModelUnavailableError("model config must keep --offline, --no-webui, --jinja and -np 1")
    return cfg


def check_runtime(model_cfg: dict[str, Any], repo_root: Path) -> None:
    """Процесс API, один раз при старте: размер и SHA256 весов, mmproj и llama-server. Без процессов и сети."""
    cfg = _config(model_cfg, repo_root)
    try:
        verify_runtime_files(cfg, full_hash=True)
    except (ModelConfigError, OSError) as e:
        raise ModelUnavailableError("model runtime files missing or do not match config") from e


def server_command(model_cfg: dict[str, Any], repo_root: Path, port: int) -> list[str]:
    """Чистая функция: argv llama-server для 127.0.0.1:<port> по закреплённым параметрам."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ModelUnavailableError("invalid model server port")
    return build_command(_config(model_cfg, repo_root).server, port)


def create_analyzer(endpoint: str, model_cfg: dict[str, Any], catalog: RuleCatalog,
                    repo_root: Path, call_timeout_sec: float = 120.0) -> VideoAnalyzer:
    """Worker, один раз после health сервера supervisor'а. Возвращает analyze_video для этого endpoint.

    call_timeout_sec — предел одного запроса к модели (Settings.model_call_timeout_sec); каждый запрос
    дополнительно ограничен остатком общего deadline задания.

    Ошибки инициализации (нет verified-правил, несовместимый config, сервер не отвечает) поднимаются
    как есть — worker сообщит init_failed, и supervisor не откроет приём.
    """
    if (isinstance(call_timeout_sec, bool) or not isinstance(call_timeout_sec, (int, float))
            or not math.isfinite(call_timeout_sec) or call_timeout_sec <= 0):
        raise ModelUnavailableError("call_timeout_sec must be a finite positive number")
    cfg = _config(model_cfg, repo_root)
    try:
        client = LlamaClient(endpoint)
    except ValueError as e:
        raise ModelUnavailableError("model endpoint must be http://127.0.0.1:<port>") from e
    analyzer = Analyzer(cfg, catalog, client, call_timeout_sec=float(call_timeout_sec))
    if not client.health(timeout=HEALTH_TIMEOUT_SEC):
        raise ModelUnavailableError("model server is not healthy")

    def analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
        try:
            return analyzer.analyze_video(video_path, job_dir, deadline_monotonic)
        except AnalysisError:
            raise
        except Exception as e:  # noqa: BLE001 — непредвиденный сбой адаптера; причина сохраняется в __cause__
            raise AnalysisError("unexpected analyzer failure") from e

    return analyze_video


__all__ = ["check_runtime", "server_command", "create_analyzer"]
