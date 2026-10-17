import os

from pydantic import ValidationError
import pytest

from app.settings import Settings, configure_offline_environment


def test_key_required_and_redacted():
    for env in ({}, {"ASSAR_API_KEY": " "}):
        with pytest.raises(ValidationError):
            Settings.from_env(env)
    settings = Settings.from_env({"ASSAR_API_KEY": "synthetic-test-key"})
    assert "synthetic-test-key" not in repr(settings)
    assert settings.api_key.get_secret_value() == "synthetic-test-key"


@pytest.mark.parametrize("overrides", [
    {"ASSAR_REQUEST_TIMEOUT_SEC": "301"}, {"ASSAR_ANALYSIS_BUDGET_SEC": "300"},
    {"ASSAR_REQUEST_TIMEOUT_SEC": "200"}, {"ASSAR_GPU_WORKERS": "2"},
    {"ASSAR_MAX_INFLIGHT_JOBS": "2"}, {"ASSAR_MAX_UPLOAD_BYTES": "0"},
])
def test_resource_limits(overrides):
    with pytest.raises(ValidationError):
        Settings.from_env({"ASSAR_API_KEY": "synthetic", **overrides})


def test_environment_numeric_parse():
    result = Settings.from_env({"ASSAR_API_KEY": "synthetic", "ASSAR_PORT": "8001",
                               "ASSAR_GPU_WORKERS": "1", "ASSAR_MAX_INFLIGHT_JOBS": "1"})
    assert result.port == 8001 and result.gpu_workers == 1


def test_offline_flags_override_conflicting_env(monkeypatch):
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                 "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK", "WANDB_MODE", "OTEL_SDK_DISABLED",
                 "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER"):
        monkeypatch.setenv(name, "0")
    configure_offline_environment()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["WANDB_MODE"] == "disabled"
    assert os.environ["OTEL_SDK_DISABLED"] == "true"
