"""ML-адаптер: analyze_video(video_path, job_dir, deadline_monotonic) -> AnalysisResult.

Работает в worker'е backend supervisor'а с уже запущенным собственным llama-server: адаптер не запускает,
не скачивает и не останавливает модель. Событие создаётся только для наблюдения, категория которого
совпадает с type_id правила со статусом verified. Без verified-правил адаптер не инициализируется:
пустой каталог — блокер production, а не повод вернуть no_violation.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.contracts import (SCHEMA_VERSION, AnalysisError, AnalysisResult, AnalysisTimeoutError, DecodeError,
                           Event, InvalidAnalysisResultError, LocalEvidence, ModelUnavailableError, Rule,
                           RuleCatalog, UnassessableVideoError, remaining_seconds, validate_analysis_for_video,
                           validate_analysis_rules)

from . import prompts
from .config import ModelConfig, load_model_config, load_rule_catalog
from .video import Frame, probe, sample_frames, windows
from .vlm import LlamaClient

PROBE_TIMEOUT_SEC = 30.0
DECODE_TIMEOUT_SEC = 120.0


class AnalyzerNotReadyError(RuntimeError):
    """Инициализация worker невозможна (нет verified-правил, несовместимый config, нет сервера).

    Это ошибка старта сервиса, а не результата job: supervisor не открывает приём заданий.
    """


@dataclass
class _Obs:
    category: str
    frames: list[Frame]
    facts: list[str] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.frames[0].pts_sec

    @property
    def end(self) -> float:
        return self.frames[-1].pts_sec


def verified_rules_by_type(catalog: RuleCatalog) -> dict[str, Rule]:
    """type_id verified-правила ↔ категория наблюдения промпта (соглашение адаптера, см. handoff)."""
    rules: dict[str, Rule] = {}
    for r in catalog.rules:
        if r.status != "verified":
            continue
        if r.type_id in rules:
            raise AnalyzerNotReadyError(f"several verified rules share type_id {r.type_id!r}")
        if r.type_id not in prompts.CATEGORIES:
            raise AnalyzerNotReadyError(f"verified type_id {r.type_id!r} is not produced by prompt "
                                        f"{prompts.PROMPT_VERSION}")
        rules[r.type_id] = r
    if not rules:
        raise AnalyzerNotReadyError("rule catalog has no verified rules; production analysis is blocked")
    return rules


def _pick_evidence(frames: list[Frame]) -> list[Frame]:
    """До трёх реальных кадров: первый, средний, последний."""
    if len(frames) <= 3:
        return list(frames)
    return [frames[0], frames[len(frames) // 2], frames[-1]]


class Analyzer:
    def __init__(self, cfg: ModelConfig, catalog: RuleCatalog, client: LlamaClient, *,
                 call_timeout_sec: float = 120.0, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        if cfg.prompt_version != prompts.PROMPT_VERSION:
            raise AnalyzerNotReadyError(f"config prompt {cfg.prompt_version} != code {prompts.PROMPT_VERSION}")
        self.cfg = cfg
        self.catalog = catalog
        self.rules = verified_rules_by_type(catalog)
        self.client = client
        self.call_timeout_sec = call_timeout_sec
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe

    def __call__(self, video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
        return self.analyze_video(video_path, job_dir, deadline_monotonic)

    # --- стадии ---------------------------------------------------------------------------------------

    def _ask(self, win: list[Frame], deadline: float) -> dict:
        idx = [f.index for f in win]
        timeout = min(self.call_timeout_sec, remaining_seconds(deadline))
        resp = self.client.chat(prompts.window_parts(win), prompts.schema(idx), max_tokens=self.cfg.max_tokens,
                                timeout=timeout, temperature=self.cfg.temperature, top_k=self.cfg.top_k,
                                seed=self.cfg.seed, cache_prompt=self.cfg.cache_prompt)
        remaining_seconds(deadline)
        choice = resp["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise InvalidAnalysisResultError("model output truncated or not finished")
        try:
            obj = json.loads(choice["message"].get("content") or "")
        except (ValueError, TypeError) as e:
            raise InvalidAnalysisResultError("model output is not JSON") from e
        if not prompts.validate(obj, idx):
            raise InvalidAnalysisResultError("model output violates observation schema")
        return obj

    @staticmethod
    def _merge(obs: list[_Obs], gap_sec: float) -> list[_Obs]:
        """Смежные наблюдения одной категории (разрыв ≤ gap_sec) — одно событие; разные — раздельно."""
        merged: list[_Obs] = []
        for o in sorted(obs, key=lambda o: (o.category, o.start)):
            last = merged[-1] if merged else None
            if last and last.category == o.category and o.start - last.end <= gap_sec:
                seen = {f.index for f in last.frames}
                last.frames = sorted(last.frames + [f for f in o.frames if f.index not in seen],
                                     key=lambda f: f.pts_sec)
                last.facts += [x for x in o.facts if x not in last.facts]
            else:
                merged.append(_Obs(o.category, list(o.frames), list(o.facts)))
        return sorted(merged, key=lambda o: o.start)

    def analyze_video(self, video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
        t_start = time.monotonic()
        remaining_seconds(deadline_monotonic)
        video_path, job_dir = Path(video_path), Path(job_dir)
        if not video_path.is_absolute() or not job_dir.is_absolute():
            raise ValueError("video_path and job_dir must be absolute")
        if not video_path.is_file():
            raise DecodeError("input video not found")
        if not job_dir.is_dir():
            raise ValueError("job_dir must be an existing directory")
        timings: dict[str, float] = {}

        t = time.monotonic()
        info = probe(video_path, self.ffprobe, timeout=min(PROBE_TIMEOUT_SEC, remaining_seconds(deadline_monotonic)))
        timings["probe"] = (time.monotonic() - t) * 1000
        remaining_seconds(deadline_monotonic)

        t = time.monotonic()
        frames = sample_frames(video_path, job_dir / "frames", self.cfg.step_sec, info,
                               model_height=self.cfg.model_height, ffmpeg=self.ffmpeg,
                               timeout=min(DECODE_TIMEOUT_SEC, remaining_seconds(deadline_monotonic)))
        timings["decode"] = (time.monotonic() - t) * 1000
        remaining_seconds(deadline_monotonic)

        t = time.monotonic()
        wins = windows(frames, self.cfg.window_sec)
        by_index = {f.index: f for f in frames}
        observations: list[_Obs] = []
        unassessable = ignored = 0
        for win in wins:
            obj = self._ask(win, deadline_monotonic)
            if not obj["frames_assessable"]:
                unassessable += 1
                continue
            for o in obj["observations"]:
                if o["category"] not in self.rules:
                    ignored += 1
                    continue
                fr = sorted({by_index[i] for i in o["frame_indices"]}, key=lambda f: f.pts_sec)
                fact = " ".join(o["description"].split())
                observations.append(_Obs(o["category"], fr, [fact] if fact else []))
        timings["inference"] = (time.monotonic() - t) * 1000
        if unassessable == len(wins):
            raise UnassessableVideoError("all windows reported as visually unassessable")

        t = time.monotonic()
        events = []
        for n, o in enumerate(self._merge(observations, gap_sec=1.5 * self.cfg.step_sec), start=1):
            rule = self.rules[o.category]
            events.append(Event(
                event_id=f"ev-{n:03d}", type_id=rule.type_id, type_name_ru=rule.type_name_ru,
                start_sec=o.start, end_sec=o.end, timecode_sec=o.start,
                observed_fact="; ".join(o.facts) or (
                    f"Модель отметила категорию {o.category} на кадрах "
                    + ", ".join(f"{f.pts_sec:.1f} с" for f in o.frames)),
                rule_id=rule.rule_id,
                evidence=[LocalEvidence(timestamp_sec=f.pts_sec, local_jpeg_path=str(f.evidence_path.resolve()))
                          for f in _pick_evidence(o.frames)]))
        limitations = [f"sampling step {self.cfg.step_sec:g}s, window {self.cfg.window_sec:g}s: "
                       "short actions between sampled frames may be missed"]
        if unassessable:
            limitations.append(f"{unassessable}/{len(wins)} windows reported visually unassessable")
        if ignored:
            limitations.append(f"{ignored} observations in categories without a verified rule were not reported")
        timings["postprocess"] = (time.monotonic() - t) * 1000
        timings["total"] = (time.monotonic() - t_start) * 1000
        try:
            result = AnalysisResult(schema_version=SCHEMA_VERSION, verdict="violation" if events else "no_violation",
                                    events=events, limitations=limitations,
                                    timings_ms={k: round(v, 3) for k, v in timings.items()},
                                    model_revision=self.cfg.revision_label)
            result = validate_analysis_for_video(result, info.duration_sec, job_dir)
            validate_analysis_rules(result, self.catalog)
        except (ValueError, OSError) as e:
            raise InvalidAnalysisResultError("analysis result failed contract validation") from e
        remaining_seconds(deadline_monotonic)
        return result


# --- функция worker'а -----------------------------------------------------------------------------

_ANALYZER: Analyzer | None = None


def init_worker(*, model_config_path: Path, rules_catalog_path: Path, server_url: str,
                root: Path | None = None, call_timeout_sec: float = 120.0, health_timeout_sec: float = 5.0,
                ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Analyzer:
    """Один раз при старте worker'а. server_url — loopback-адрес сервера, запущенного supervisor'ом."""
    global _ANALYZER
    cfg = load_model_config(model_config_path, root)
    catalog = load_rule_catalog(rules_catalog_path)
    client = LlamaClient(server_url)
    analyzer = Analyzer(cfg, catalog, client, call_timeout_sec=call_timeout_sec, ffmpeg=ffmpeg, ffprobe=ffprobe)
    if not client.health(timeout=health_timeout_sec):
        raise AnalyzerNotReadyError("llama-server is not healthy")
    _ANALYZER = analyzer
    return analyzer


def analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
    """Контракт VideoAnalyzer. Сбой — AnalysisError (или ValueError на неверные аргументы), не no_violation."""
    if _ANALYZER is None:
        raise ModelUnavailableError("analyzer worker is not initialized")
    return _ANALYZER.analyze_video(video_path, job_dir, deadline_monotonic)


__all__ = ["Analyzer", "AnalyzerNotReadyError", "AnalysisError", "AnalysisTimeoutError", "analyze_video",
           "init_worker", "verified_rules_by_type"]
