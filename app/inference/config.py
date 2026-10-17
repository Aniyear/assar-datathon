"""Чтение configs/model.yaml (JSON-совместимый YAML) и каталога правил для ML-адаптера.

Файлы принадлежат интегратору; здесь только чтение и проверка, без изменения схемы.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.contracts import RuleCatalog

from .vlm import ServerConfig


class ModelConfigError(ValueError):
    """configs/model.yaml отсутствует, повреждён или противоречит коду адаптера."""


@dataclass(frozen=True)
class FileSpec:
    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    model_revision: str
    quantization: str
    mmproj_quantization: str
    runtime_tag: str
    status: str
    server_sha256: str | None
    server: ServerConfig
    weights: FileSpec
    mmproj: FileSpec
    max_tokens: int
    temperature: float
    top_k: int
    seed: int
    cache_prompt: bool
    prompt_version: str
    step_sec: float
    window_sec: float
    model_height: int

    @property
    def revision_label(self) -> str:
        return (f"{self.model_id}@{self.model_revision} {self.quantization}+mmproj-{self.mmproj_quantization}; "
                f"llama.cpp {self.runtime_tag}; prompt {self.prompt_version}")


def _resolve(root: Path, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (root / path)


def load_model_config(path: Path, root: Path | None = None) -> ModelConfig:
    """root — база для относительных путей (по умолчанию корень репозитория над configs/)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ModelConfigError("cannot read model config") from e
    return parse_model_config(d, Path(root) if root is not None else Path(path).resolve().parent.parent)


def parse_model_config(d: dict, root: Path) -> ModelConfig:
    """configs/model.yaml, уже прочитанный stdlib json; относительные пути — от root."""
    root = Path(root)
    try:
        if not isinstance(d, dict):
            raise ModelConfigError("model config must be an object")
        if d["backend"] != "llama-server" or d.get("external_inference") is not False:
            raise ModelConfigError("only local llama-server backend is supported")
        if d["server"]["host"] != "127.0.0.1":
            raise ModelConfigError("server.host must be 127.0.0.1")
        if d["generation"]["response_format"] != "json_schema":
            raise ModelConfigError("generation.response_format must be json_schema")
        s, g, smp, rt = d["server"], d["generation"], d["sampling"], d["runtime"]
        weights = FileSpec(_resolve(root, d["weights"]["path"]), d["weights"]["sha256"], int(d["weights"]["bytes"]))
        mmproj = FileSpec(_resolve(root, d["mmproj"]["path"]), d["mmproj"]["sha256"], int(d["mmproj"]["bytes"]))
        server = ServerConfig(server_bin=_resolve(root, rt["server_bin"]), model=weights.path, mmproj=mmproj.path,
                              ctx=int(s["ctx"]), n_gpu_layers=int(s["n_gpu_layers"]), parallel=int(s["parallel"]),
                              port=int(s["port"]), image_max_tokens=int(s["image_max_tokens"]),
                              offline=bool(s["offline"]), no_webui=bool(s["no_webui"]), jinja=bool(s["jinja"]),
                              extra_args=[str(a) for a in s["extra_args"]])
        return ModelConfig(
            model_id=d["model_id"], model_revision=d["model_revision"], quantization=d["weights"]["quantization"],
            mmproj_quantization=d["mmproj"]["quantization"], runtime_tag=rt["tag"], status=d["status"],
            server_sha256=rt.get("server_sha256_reported"),
            server=server, weights=weights, mmproj=mmproj,
            max_tokens=int(g["max_tokens"]), temperature=float(g["temperature"]), top_k=int(g["top_k"]),
            seed=int(g["seed"]), cache_prompt=bool(g["cache_prompt"]), prompt_version=g["prompt_version"],
            step_sec=float(smp["step_sec"]), window_sec=float(smp["window_sec"]),
            model_height=int(smp["model_height"]))
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, ModelConfigError):
            raise
        raise ModelConfigError("model config has unexpected structure") from e


def load_rule_catalog(path: Path) -> RuleCatalog:
    try:
        return RuleCatalog.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ModelConfigError("cannot read rule catalog") from e


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_runtime_files(cfg: ModelConfig, full_hash: bool = True) -> None:
    """Проверка локальных файлов перед стартом сервиса. full_hash читает ~2,3 ГБ весов.

    Хеш llama-server.exe проверяется, если он есть в config (runtime.server_sha256_reported).
    CUDA DLL рядом с сервером config пока не перечисляет — их наличие/хеш здесь не проверяются.
    """
    for name, spec in (("weights", cfg.weights), ("mmproj", cfg.mmproj)):
        if not spec.path.is_file() or spec.path.stat().st_size != spec.bytes:
            raise ModelConfigError(f"{name} file missing or size mismatch")
        if full_hash and _sha256(spec.path) != spec.sha256:
            raise ModelConfigError(f"{name} SHA256 mismatch")
    if not cfg.server.server_bin.is_file():
        raise ModelConfigError("llama-server binary missing")
    if full_hash and cfg.server_sha256 and _sha256(cfg.server.server_bin) != cfg.server_sha256:
        raise ModelConfigError("llama-server SHA256 mismatch")
