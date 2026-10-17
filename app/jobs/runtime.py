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


# Exact readiness flags of configs/model.yaml; each must be the JSON boolean true.
REQUIRED_VALIDATION = frozenset({"weights_checked_on_this_host", "gpu_3060_measured",
                                 "analyze_video_implemented"})
# Required fields/types of the pinned config, plus safety flags with fixed values.
_TYPED_FIELDS = {"schema_version": str, "status": str, "backend": str, "model_id": str,
                 "model_revision": str, "runtime": dict, "weights": dict, "mmproj": dict,
                 "server": dict, "generation": dict, "sampling": dict, "validation": dict}
_FIXED_VALUES = {"schema_version": "0.1", "backend": "llama-server", "process_owner": "service_supervisor",
                 "reuse_between_jobs": True, "external_inference": False, "audio_enabled": False,
                 "face_recognition_enabled": False}


def model_config_problems(cfg: object) -> list[str]:
    """Readiness problems of a parsed model config; empty list means ready."""
    if not isinstance(cfg, dict):
        return ["model config is not a JSON object"]
    problems = [f"model config field {name!r} must be {kind.__name__}"
                for name, kind in _TYPED_FIELDS.items() if type(cfg.get(name)) is not kind]
    problems += [f"model config field {name!r} must be {value!r}"
                 for name, value in _FIXED_VALUES.items()
                 if type(cfg.get(name)) is not type(value) or cfg.get(name) != value]
    if cfg.get("status") != "ready":
        problems.append(f"model status is {cfg.get('status')!r}, expected 'ready'")
    validation = cfg.get("validation")
    if isinstance(validation, dict):
        keys = set(validation)
        if keys != REQUIRED_VALIDATION:
            problems.append("model validation flags must be exactly "
                            + ", ".join(sorted(REQUIRED_VALIDATION)))
        if not all(validation.get(k) is True for k in REQUIRED_VALIDATION):
            problems.append("model validation flags are not all true")
    return problems


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
    reasons += model_config_problems(model_cfg)
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
