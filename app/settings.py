"""Small environment-only settings shared by future backend and ML owners."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    api_key: SecretStr
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    uploads_dir: Path = Path("uploads")
    outputs_dir: Path = Path("outputs")
    model_config_path: Path = Path("configs/model.yaml")
    rules_catalog_path: Path = Path("app/rules/catalog.json")
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_jpeg_bytes: int = Field(default=5 * 1024 * 1024, gt=0, le=5 * 1024 * 1024)
    request_timeout_sec: int = Field(default=300, gt=0, le=300)
    analysis_budget_sec: int = Field(default=240, gt=0, lt=300)
    response_reserve_sec: int = Field(default=5, gt=0, lt=300)
    model_host: Literal["127.0.0.1"] = "127.0.0.1"
    model_port: int = Field(default=0, ge=0, le=65535)
    model_process_owner: Literal["service_supervisor"] = "service_supervisor"
    model_start_timeout_sec: int = Field(default=180, gt=0)
    model_call_timeout_sec: int = Field(default=120, gt=0)
    model_stop_grace_sec: int = Field(default=10, gt=0)
    gpu_workers: Literal[1] = 1
    max_inflight_jobs: Literal[1] = 1  # Reject overload until queue timings are measured.

    @field_validator("api_key")
    @classmethod
    def nonempty_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("ASSAR_API_KEY is required")
        return value

    @model_validator(mode="after")
    def budget_fits(self) -> Settings:
        if self.analysis_budget_sec + self.response_reserve_sec > self.request_timeout_sec:
            raise ValueError("Analysis budget must leave time for upload/response/recovery")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        values = {name: source[f"ASSAR_{name.upper()}"] for name in cls.model_fields
                  if f"ASSAR_{name.upper()}" in source}
        # Literal[int] deliberately does not coerce environment strings.
        for name in ("gpu_workers", "max_inflight_jobs"):
            if name in values:
                values[name] = int(values[name])
        return cls.model_validate(values)


def configure_offline_environment() -> None:
    """Call BEFORE importing model libraries. This is not an OS network firewall."""
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "DO_NOT_TRACK": "1", "WANDB_MODE": "disabled",
                       "OTEL_SDK_DISABLED": "true", "OTEL_TRACES_EXPORTER": "none",
                       "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none"})
