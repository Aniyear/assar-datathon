import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
from pydantic import ValidationError
import pytest

from app.contracts import (AnalysisResult, AnalyzeAccepted, DoneJob, JOB_RESPONSE_ADAPTER,
                           RuleCatalog, validate_analysis_for_video, validate_analysis_rules,
                           validate_done_for_video)

FIXTURES = Path(__file__).parent / "fixtures/contracts"


def fixture(name="done_violation"):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["processing", "failed", "done_no_violation", "done_violation"])
def test_public_roundtrip_has_exact_fields(name):
    payload = fixture(name)
    result = JOB_RESPONSE_ADAPTER.validate_python(payload)
    assert result.model_dump(mode="json") == payload
    if isinstance(result, DoneJob):
        validate_done_for_video(result, 5.0)


def test_post_response():
    assert AnalyzeAccepted.model_validate(fixture("accepted")).model_dump() == fixture("accepted")


@pytest.mark.parametrize("status", ["processing", "failed"])
def test_unfinished_jobs_cannot_have_verdict(status):
    with pytest.raises(ValidationError):
        JOB_RESPONSE_ADAPTER.validate_python({**fixture(status), "verdict": "no_violation"})


@pytest.mark.parametrize("extra", [{"error": "private path"}, {"progress": 10}])
def test_no_public_extensions(extra):
    with pytest.raises(ValidationError):
        JOB_RESPONSE_ADAPTER.validate_python({**fixture("failed"), **extra})


@pytest.mark.parametrize("verdict,events", [("no_violation", True), ("violation", False), ("uncertain", False)])
def test_verdict_cannot_mask_failure(verdict, events):
    payload = fixture()
    payload.update(verdict=verdict, violations=payload["violations"] if events else [])
    with pytest.raises(ValidationError):
        DoneJob.model_validate(payload)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True, "1.0"])
def test_invalid_time_rejected(value):
    payload = fixture()
    payload["violations"][0]["timecode_sec"] = value
    with pytest.raises(ValidationError):
        DoneJob.model_validate(payload)


@pytest.mark.parametrize("count", [0, 4])
def test_frame_count(count):
    payload = fixture()
    event = payload["violations"][0]
    event["evidence_frames"] = event["evidence_frames"] * count
    with pytest.raises(ValidationError):
        DoneJob.model_validate(payload)


@pytest.mark.parametrize("kind", ["prefix", "garbage", "png", "truncated"])
def test_evidence_is_really_jpeg(kind):
    payload = fixture()
    frame = payload["violations"][0]["evidence_frames"][0]
    if kind == "prefix":
        frame["image_base64"] = "data:image/jpeg;base64," + frame["image_base64"]
    elif kind == "garbage":
        frame["image_base64"] = "###"
    elif kind == "truncated":
        raw = base64.b64decode(frame["image_base64"])
        frame["image_base64"] = base64.b64encode(raw[:100] + b"\xff\xd9").decode()
    else:
        buf = BytesIO()
        Image.new("RGB", (2, 2)).save(buf, "PNG")
        frame["image_base64"] = base64.b64encode(buf.getvalue()).decode()
    with pytest.raises(ValidationError):
        DoneJob.model_validate(payload)


def test_chronology_and_duration():
    payload = fixture()
    payload["violations"].reverse()
    with pytest.raises(ValidationError):
        DoneJob.model_validate(payload)
    valid = DoneJob.model_validate(fixture())
    with pytest.raises(ValueError):
        validate_done_for_video(valid, 3.1)  # Last evidence at 3.2, event itself at 3.0.
    valid.violations.clear()  # Revalidate mutable lists at the publication boundary.
    with pytest.raises(ValueError):
        validate_done_for_video(valid, 5.0)


def analysis(path):
    return AnalysisResult.model_validate({"schema_version": "0.1", "verdict": "violation",
        "events": [{"event_id": "synthetic-event", "type_id": "synthetic", "type_name_ru": "Тест",
                    "start_sec": 1.0, "end_sec": 3.0, "timecode_sec": 2.0,
                    "observed_fact": "Синтетический тест", "rule_id": "synthetic-rule",
                    "evidence": [{"timestamp_sec": 2.0, "local_jpeg_path": str(path)}]}],
        "limitations": [], "timings_ms": {"total": 1.0}, "model_revision": "synthetic-fixture-v1"})


def test_private_boundary(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    evidence = job / "frame.jpg"
    evidence.write_bytes(base64.b64decode(fixture()["violations"][0]["evidence_frames"][0]["image_base64"]))
    result = analysis(evidence)
    validate_analysis_for_video(result, 5.0, job)
    with pytest.raises(ValueError):
        validate_analysis_for_video(result, 2.0, job)
    with pytest.raises(ValueError):
        validate_analysis_for_video(result, 5.0, job, max_jpeg_bytes=10)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError):
        validate_analysis_for_video(analysis(evidence), 5.0, other)
    with pytest.raises(ValueError):
        validate_analysis_for_video(analysis("frame.jpg"), 5.0, job)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(evidence.read_bytes())
    with pytest.raises(ValueError):
        validate_analysis_for_video(analysis(job / ".." / outside.name), 5.0, job)
    with pytest.raises(ValueError):
        validate_analysis_rules(result, RuleCatalog(schema_version="0.1", rules=[]))


@pytest.mark.parametrize("change", ["interval", "duplicate", "nan", "missing_revision"])
def test_invalid_model_results(change):
    payload = analysis(Path("fixture.jpg")).model_dump()
    if change == "interval":
        payload["events"][0]["end_sec"] = 0.5
    elif change == "duplicate":
        payload["events"].append(deepcopy(payload["events"][0]))
    elif change == "nan":
        payload["timings_ms"]["total"] = float("nan")
    else:
        del payload["model_revision"]
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def rule(status="verified"):
    return {"rule_id": "synthetic-rule", "type_id": "synthetic", "type_name_ru": "Тест",
            "source_title": "Синтетический источник, не нормативный акт",
            "source_url": "https://example.invalid/synthetic", "clause": None,
            "effective_version_checked_at": "2026-10-17" if status == "verified" else None,
            "scope": "Только unit test", "observable_condition": "Synthetic",
            "required_context": [], "counterexamples": [], "claim_template_ru": "Synthetic", "status": status}


@pytest.mark.parametrize("rules", [[], [rule("candidate")], [rule("disabled")]])
def test_negative_result_requires_active_catalog(rules):
    negative = AnalysisResult(schema_version="0.1", verdict="no_violation", events=[],
                              limitations=[], timings_ms={}, model_revision="synthetic")
    with pytest.raises(ValueError):
        validate_analysis_rules(negative, RuleCatalog.model_validate({"schema_version": "0.1", "rules": rules}))


def test_rule_source_without_clause_is_valid_but_must_match_type():
    catalog = RuleCatalog.model_validate({"schema_version": "0.1", "rules": [rule()]})
    result = analysis(Path("synthetic.jpg"))
    validate_analysis_rules(result, catalog)
    result.events[0].type_id = "unknown"
    with pytest.raises(ValueError):
        validate_analysis_rules(result, catalog)
