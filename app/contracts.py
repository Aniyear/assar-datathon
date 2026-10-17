"""Contract v0.1. No API server, model, serializer or fixture fallback lives here.

Backend must call the video-aware validators before publishing a done result.
Pydantic errors may contain private input: never send them to clients/telemetry.
"""
from __future__ import annotations

import base64
import binascii
from io import BytesIO
import math
from pathlib import Path
from typing import Annotated, Literal, Protocol
import warnings

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter
from pydantic import field_validator, model_validator

SCHEMA_VERSION = "0.1"
MAX_JPEG_BYTES = 5 * 1024 * 1024  # Team resource limit, not a jury requirement.
MAX_JPEG_PIXELS = 16_000_000
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Verdict = Literal["violation", "no_violation"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True,
                              hide_input_in_errors=True)


def validate_jpeg_bytes(data: bytes, max_bytes: int = MAX_JPEG_BYTES) -> None:
    """Decode locally; do not display or transmit evidence."""
    if not data or len(data) > max_bytes:
        raise ValueError("JPEG is empty or exceeds the configured size limit")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("Expected a complete JPEG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG" or image.width * image.height > MAX_JPEG_PIXELS:
                    raise ValueError("Invalid JPEG format or pixel limit exceeded")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()  # verify alone does not fully decode JPEG pixel data.
    except Exception as exc:
        raise ValueError("JPEG decoding failed") from exc


class AnalyzeAccepted(ContractModel):
    job_id: Text


class EvidenceFrame(ContractModel):
    timestamp_sec: Seconds
    image_base64: Annotated[str, Field(min_length=1, max_length=4 * ((MAX_JPEG_BYTES + 2) // 3))]

    @field_validator("image_base64")
    @classmethod
    def jpeg_base64(cls, value: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Expected plain base64 JPEG without a data: prefix") from exc
        validate_jpeg_bytes(data)
        return value


class Violation(ContractModel):
    type: Text
    timecode_sec: Seconds
    description: Text
    evidence_frames: Annotated[list[EvidenceFrame], Field(min_length=1, max_length=3)]


class ProcessingJob(ContractModel):
    job_id: Text
    status: Literal["processing"]


class FailedJob(ContractModel):
    job_id: Text
    status: Literal["failed"]


def _check_verdict(verdict: Verdict, events: list) -> None:
    if (verdict == "violation") != bool(events):
        raise ValueError("verdict and event list disagree")
    times = [event.timecode_sec for event in events]
    if times != sorted(times):
        raise ValueError("Events must be sorted by timecode_sec")


class DoneJob(ContractModel):
    job_id: Text
    status: Literal["done"]
    verdict: Verdict
    violations: list[Violation]

    @model_validator(mode="after")
    def consistent_result(self) -> DoneJob:
        _check_verdict(self.verdict, self.violations)
        return self


JobResponse = Annotated[ProcessingJob | DoneJob | FailedJob, Field(discriminator="status")]
JOB_RESPONSE_ADAPTER = TypeAdapter(JobResponse)


class LocalEvidence(ContractModel):
    timestamp_sec: Seconds
    local_jpeg_path: Text  # Absolute file path inside this job's directory.


class Event(ContractModel):
    event_id: Text
    type_id: Text
    type_name_ru: Text
    start_sec: Seconds
    end_sec: Seconds
    timecode_sec: Seconds
    observed_fact: Text
    rule_id: Text
    evidence: Annotated[list[LocalEvidence], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def ordered_interval(self) -> Event:
        if not self.start_sec <= self.timecode_sec <= self.end_sec:
            raise ValueError("Expected start_sec <= timecode_sec <= end_sec")
        return self


class AnalysisResult(ContractModel):
    schema_version: Literal["0.1"]
    verdict: Verdict
    events: list[Event]
    limitations: list[Text]
    timings_ms: dict[Text, Seconds]
    model_revision: Text

    @model_validator(mode="after")
    def consistent_result(self) -> AnalysisResult:
        _check_verdict(self.verdict, self.events)
        if len({e.event_id for e in self.events}) != len(self.events):
            raise ValueError("event_id must be unique within a result")
        return self


class Rule(ContractModel):
    rule_id: Text
    type_id: Text
    type_name_ru: Text
    source_title: Text
    source_url: Text
    clause: Text | None  # FAQ allows a source without an exact clause.
    effective_version_checked_at: Text | None
    scope: Text
    observable_condition: Text
    required_context: list[Text]
    counterexamples: list[Text]
    claim_template_ru: Text
    status: Literal["candidate", "verified", "disabled"]

    @field_validator("source_url")
    @classmethod
    def http_source(cls, value: str) -> str:
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Expected an HTTP(S) source URL without credentials")
        return value

    @model_validator(mode="after")
    def verified_metadata(self) -> Rule:
        if self.status == "verified" and self.effective_version_checked_at is None:
            raise ValueError("A verified rule needs a checked version/date")
        return self


class RuleCatalog(ContractModel):
    schema_version: Literal["0.1"]
    rules: list[Rule]

    @model_validator(mode="after")
    def unique_rules(self) -> RuleCatalog:
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("Duplicate rule_id")
        return self


def _duration(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("duration_sec must be finite and positive")
    return value


def validate_done_for_video(result: DoneJob, duration_sec: float) -> DoneJob:
    """Revalidate (including mutable lists) and enforce the known video duration."""
    duration = _duration(duration_sec)
    result = DoneJob.model_validate(result.model_dump())
    for event in result.violations:
        if event.timecode_sec > duration or any(f.timestamp_sec > duration for f in event.evidence_frames):
            raise ValueError("Public timestamp exceeds video duration")
    return result


def validate_analysis_for_video(result: AnalysisResult, duration_sec: float, job_dir: Path,
                                max_jpeg_bytes: int = MAX_JPEG_BYTES) -> AnalysisResult:
    """Shared boundary check. Backend still owns encoding and public serialization."""
    duration = _duration(duration_sec)
    result = AnalysisResult.model_validate(result.model_dump())
    root = job_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("job_dir must be a directory")
    for event in result.events:
        if event.end_sec > duration:
            raise ValueError("Event interval exceeds video duration")
        for frame in event.evidence:
            if frame.timestamp_sec > duration:
                raise ValueError("Evidence timestamp exceeds video duration")
            path = Path(frame.local_jpeg_path)
            if not path.is_absolute():
                raise ValueError("Evidence path must be absolute")
            path = path.resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Evidence must be a file inside job_dir")
            with path.open("rb") as stream:
                validate_jpeg_bytes(stream.read(max_jpeg_bytes + 1), max_jpeg_bytes)
    return result


def validate_analysis_rules(result: AnalysisResult, catalog: RuleCatalog) -> None:
    """Validate IDs only; legal applicability still needs the captain's review."""
    result = AnalysisResult.model_validate(result.model_dump())
    catalog = RuleCatalog.model_validate(catalog.model_dump())
    rules = {r.rule_id: r for r in catalog.rules if r.status == "verified"}
    if not rules:
        raise ValueError("Analysis requires at least one verified catalog rule")
    for event in result.events:
        rule = rules.get(event.rule_id)
        if rule is None or (event.type_id, event.type_name_ru) != (rule.type_id, rule.type_name_ru):
            raise ValueError("Event requires a matching verified catalog rule")


class AnalysisError(RuntimeError):
    """Worker failure: backend must mark failed, never synthesize no_violation."""


class DecodeError(AnalysisError):
    pass


class ModelUnavailableError(AnalysisError):
    pass


class AnalysisTimeoutError(AnalysisError):
    pass


class UnassessableVideoError(AnalysisError):
    pass


class InvalidAnalysisResultError(AnalysisError):
    pass


class VideoAnalyzer(Protocol):
    def __call__(self, video_path: Path, job_dir: Path,
                 deadline_monotonic: float) -> AnalysisResult: ...
