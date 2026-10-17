"""Production RuntimeSpec: refuses to start without a ready model and verified rules."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from app.contracts import RuleCatalog
from app.jobs.ml_interface import ML_SERVICE_MODULE
from app.jobs.supervisor import RuntimeSpec
from app.settings import Settings, configure_offline_environment

REPO_ROOT = Path(__file__).resolve().parents[2]


class ServiceNotReady(RuntimeError):
    """Explicit production refusal. The message lists reasons, never secrets or scene data."""


def load_catalog(path: Path) -> RuleCatalog:
    return RuleCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def verified_only(catalog: RuleCatalog) -> RuleCatalog:
    return RuleCatalog(schema_version="0.1", rules=[r for r in catalog.rules if r.status == "verified"])


def production_spec(settings: Settings) -> RuntimeSpec:
    configure_offline_environment()  # Before importing the ML module.
    reasons: list[str] = []
    try:
        model_cfg = json.loads(settings.model_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ServiceNotReady("model config is missing or not valid JSON") from None
    if model_cfg.get("status") != "ready":
        reasons.append(f"model status is {model_cfg.get('status')!r}, expected 'ready'")
    validation = model_cfg.get("validation") or {}
    if not validation or not all(value is True for value in validation.values()):
        reasons.append("model validation flags are not all true")
    try:
        catalog = verified_only(load_catalog(settings.rules_catalog_path))
    except (OSError, ValueError):
        raise ServiceNotReady("rules catalog is missing or invalid") from None
    if not catalog.rules:
        reasons.append("rules catalog has no verified rules")
    service = None
    try:
        service = importlib.import_module(ML_SERVICE_MODULE)
    except ImportError:
        reasons.append(f"{ML_SERVICE_MODULE} is not available")
    if service is not None:
        missing = [n for n in ("check_runtime", "server_command", "create_analyzer") if not hasattr(service, n)]
        if missing:
            reasons.append(f"{ML_SERVICE_MODULE} lacks {', '.join(missing)}")
    if reasons:
        raise ServiceNotReady("; ".join(reasons))
    try:
        service.check_runtime(model_cfg, REPO_ROOT)
    except Exception as exc:  # noqa: BLE001
        raise ServiceNotReady(f"model runtime check failed ({type(exc).__name__})") from None
    return RuntimeSpec(
        analyzer_factory=f"{ML_SERVICE_MODULE}:create_analyzer",
        analyzer_kwargs={"model_cfg": model_cfg, "catalog": catalog, "repo_root": REPO_ROOT},
        catalog=catalog,
        server_command=lambda port: service.server_command(model_cfg, REPO_ROOT, port),
        log_dir=settings.outputs_dir / "logs",
    )
