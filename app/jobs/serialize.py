"""Internal AnalysisResult -> exact public DoneJob. Legal source comes from the catalog only."""
from __future__ import annotations

import base64
from pathlib import Path

from app.contracts import (AnalysisResult, DoneJob, EvidenceFrame, Rule, RuleCatalog, Violation,
                           validate_analysis_for_video, validate_analysis_rules,
                           validate_done_for_video)


def describe(observed_fact: str, rule: Rule) -> str:
    """Observed fact; why it may be a violation; act, clause and URL."""
    source = rule.source_title + (f", {rule.clause}" if rule.clause else "")
    return f"{observed_fact.rstrip('.')}. {rule.claim_template_ru.strip()} Источник: {source}. {rule.source_url}"


def to_done_job(job_id: str, result: AnalysisResult, catalog: RuleCatalog, duration_sec: float,
                job_dir: Path, max_jpeg_bytes: int) -> DoneJob:
    """Raises ValueError/pydantic errors on any contract breach; caller marks failed."""
    result = validate_analysis_for_video(result, duration_sec, job_dir, max_jpeg_bytes)
    validate_analysis_rules(result, catalog)
    rules = {r.rule_id: r for r in catalog.rules if r.status == "verified"}
    violations = []
    for event in result.events:
        rule = rules[event.rule_id]
        description = describe(event.observed_fact, rule)
        if any(note.startswith("mandatory ") for note in result.limitations):
            description += (" Обязательный анализ завершён не полностью; "
                            "другие события могли быть пропущены.")
        frames = [EvidenceFrame(timestamp_sec=f.timestamp_sec,
                                image_base64=base64.b64encode(Path(f.local_jpeg_path).read_bytes()).decode("ascii"))
                  for f in event.evidence]
        violations.append(Violation(type=rule.type_name_ru, timecode_sec=event.timecode_sec,
                                    description=description,
                                    evidence_frames=frames))
    done = DoneJob(job_id=job_id, status="done", verdict=result.verdict, violations=violations)
    return validate_done_for_video(done, duration_sec)
