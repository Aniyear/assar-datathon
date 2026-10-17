import pytest

from app import contracts


def test_absolute_budget_decreases_across_stages(monkeypatch):
    monkeypatch.setattr(contracts.time, "monotonic", lambda: 100.0)
    assert contracts.remaining_seconds(112.5) == 12.5
    monkeypatch.setattr(contracts.time, "monotonic", lambda: 110.0)
    assert contracts.remaining_seconds(112.5) == 2.5
    monkeypatch.setattr(contracts.time, "monotonic", lambda: 112.5)
    with pytest.raises(contracts.AnalysisTimeoutError):
        contracts.remaining_seconds(112.5)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, "123", None])
def test_invalid_deadline_rejected(value):
    with pytest.raises(ValueError):
        contracts.remaining_seconds(value)


def test_expired_deadline_is_analysis_failure(monkeypatch):
    monkeypatch.setattr(contracts.time, "monotonic", lambda: 200.0)
    with pytest.raises(contracts.AnalysisError):
        contracts.remaining_seconds(199.0)
