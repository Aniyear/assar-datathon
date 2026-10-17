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

from . import conditions, facts_prompt, facts_v2, facts_v3, facts_v4, prompts, rules_v2, rules_v4
from .config import ModelConfig, load_model_config, load_rule_catalog
from .video import Frame, probe, sample_frames, windows
from .vlm import LlamaClient

PROBE_TIMEOUT_SEC = 30.0
# Категории промпта, не соответствующие ни одному кандидату нарушения (см. prompts.CATEGORIES).
GENERIC_CATEGORIES = frozenset({"other_notable_action"})
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
        # Режим выбирается версией промпта в config: obs-v0.1 (категории) или cash01-facts-v1 (факты/условия).
        if cfg.prompt_version == facts_prompt.FACTS_PROMPT_VERSION:
            self.mode = "facts"
            verified = [r for r in catalog.rules if r.status == "verified"]
            if [r.type_id for r in verified] != [facts_prompt.SUPPORTED_TYPE_ID]:
                raise AnalyzerNotReadyError(f"{facts_prompt.FACTS_PROMPT_VERSION} needs exactly one verified rule "
                                            f"with type_id {facts_prompt.SUPPORTED_TYPE_ID!r}")
            self.rules = {verified[0].type_id: verified[0]}
        elif cfg.prompt_version in (facts_v2.PROMPT_VERSION, facts_v3.PROMPT_VERSION, facts_v4.PROMPT_VERSION):
            self.mode = "facts_v2"
            self.facts_pm = {facts_v2.PROMPT_VERSION: facts_v2, facts_v3.PROMPT_VERSION: facts_v3,
                             facts_v4.PROMPT_VERSION: facts_v4}[cfg.prompt_version]
            self.rules_mod = rules_v4 if self.facts_pm is facts_v4 else rules_v2
            verified = [r for r in catalog.rules if r.status == "verified"]
            types = [r.type_id for r in verified]
            supported = self.facts_pm.SUPPORTED_TYPE_IDS
            if not verified or len(set(types)) != len(types) or not set(types) <= set(supported):
                raise AnalyzerNotReadyError(f"{cfg.prompt_version} needs 1+ verified rules, one per type_id, "
                                            f"from {supported}")
            self.rules = {r.type_id: r for r in verified}
        elif cfg.prompt_version == prompts.PROMPT_VERSION:
            self.mode = "observations"
            self.rules = verified_rules_by_type(catalog)
        else:
            raise AnalyzerNotReadyError(f"unsupported prompt version {cfg.prompt_version}")
        self.cfg = cfg
        self.catalog = catalog
        self.last_trace: dict | None = None
        self.extra_body: dict | None = None  # только экспериментальные серверы (dev_eval); production — None
        self.client = client
        self.call_timeout_sec = call_timeout_sec
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe

    def __call__(self, video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult:
        return self.analyze_video(video_path, job_dir, deadline_monotonic)

    # --- стадии ---------------------------------------------------------------------------------------

    def _ask(self, win: list[Frame], deadline: float, pm=prompts) -> dict:
        idx = [f.index for f in win]
        timeout = min(self.call_timeout_sec, remaining_seconds(deadline))
        resp = self.client.chat(pm.window_parts(win), pm.schema(idx), max_tokens=self.cfg.max_tokens,
                                timeout=timeout, temperature=self.cfg.temperature, top_k=self.cfg.top_k,
                                seed=self.cfg.seed, cache_prompt=self.cfg.cache_prompt,
                                **({"extra_body": self.extra_body} if self.extra_body else {}))
        remaining_seconds(deadline)
        choice = resp["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise InvalidAnalysisResultError("model output truncated or not finished")
        try:
            obj = json.loads(choice["message"].get("content") or "")
        except (ValueError, TypeError) as e:
            raise InvalidAnalysisResultError("model output is not JSON") from e
        if not pm.validate(obj, idx):
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
        try:
            remaining_seconds(deadline_monotonic)
        except ValueError as e:  # не конечное/не число: ошибка вызывающей стороны, отдельного типа нет
            raise AnalysisError("invalid deadline argument") from e
        video_path, job_dir = Path(video_path), Path(job_dir)
        if not video_path.is_absolute() or not video_path.is_file():
            raise DecodeError("input video path is not an absolute existing file")
        if not job_dir.is_absolute() or not job_dir.is_dir():
            # Нет подходящего наследника: каталог job создаёт backend. Базовый AnalysisError → жёсткий сбой.
            raise AnalysisError("job_dir is not an absolute existing directory")
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
        self.last_trace = None
        if self.mode == "facts_v2":
            return self._analyze_facts_v2(info, frames, job_dir, deadline_monotonic, timings, t_start)
        if self.mode == "facts":
            return self._analyze_facts(info, frames, job_dir, deadline_monotonic, timings, t_start)

        t = time.monotonic()
        wins = windows(frames, self.cfg.window_sec)
        by_index = {f.index: f for f in frames}
        observations: list[_Obs] = []
        unassessable = generic = 0
        unsupported = 0  # кандидатные категории без verified-правила
        for win in wins:
            obj = self._ask(win, deadline_monotonic)
            if not obj["frames_assessable"]:
                unassessable += 1
                continue
            for o in obj["observations"]:
                if o["category"] not in self.rules:
                    if o["category"] in GENERIC_CATEGORIES:
                        generic += 1
                    else:
                        unsupported += 1
                    continue
                fr = sorted({by_index[i] for i in o["frame_indices"]}, key=lambda f: f.pts_sec)
                observations.append(_Obs(o["category"], fr, [" ".join(o["description"].split())]))
        timings["inference"] = (time.monotonic() - t) * 1000
        if unassessable == len(wins):
            raise UnassessableVideoError("all windows reported as visually unassessable")
        if not observations:
            # no_violation только при полном покрытии и без сигналов, которые нечем проверить.
            if unsupported:
                raise InvalidAnalysisResultError("observations in candidate categories without a verified rule")
            if unassessable:
                raise UnassessableVideoError("part of the video is unassessable and no verified event was found")
            if generic:
                # MVP: неклассифицированное действие не доказывает отсутствие нарушения (и не является им).
                raise UnassessableVideoError("generic observations without a verified rule; no conclusion")

        t = time.monotonic()
        events = []
        for n, o in enumerate(self._merge(observations, gap_sec=1.5 * self.cfg.step_sec), start=1):
            rule = self.rules[o.category]
            events.append(Event(
                event_id=f"ev-{n:03d}", type_id=rule.type_id, type_name_ru=rule.type_name_ru,
                start_sec=o.start, end_sec=o.end, timecode_sec=o.start,
                observed_fact="; ".join(o.facts), rule_id=rule.rule_id,
                evidence=[LocalEvidence(timestamp_sec=f.pts_sec, local_jpeg_path=str(f.evidence_path.resolve()))
                          for f in _pick_evidence(o.frames)]))
        limitations = [f"sampling step {self.cfg.step_sec:g}s, window {self.cfg.window_sec:g}s: "
                       "short actions between sampled frames may be missed",
                       "events are model category matches to verified type_id; rule required_context and "
                       "counterexamples are not machine-checked"]
        if unassessable:
            limitations.append(f"{unassessable}/{len(wins)} windows reported visually unassessable")
        if unsupported:
            limitations.append(f"{unsupported} observations in candidate categories without a verified rule "
                               "were not reported")
        if generic:
            limitations.append(f"{generic} generic observations (no rule category) were not reported")
        timings["postprocess"] = (time.monotonic() - t) * 1000
        return self._finish(events, limitations, info, job_dir, deadline_monotonic, timings, t_start)

    def _analyze_facts(self, info, frames: list[Frame], job_dir: Path, deadline_monotonic: float,
                       timings: dict[str, float], t_start: float) -> AnalysisResult:
        """CASH-01: покадровые факты от модели по окнам, условия нормы — conditions.evaluate_cash01 по всему ролику."""
        t = time.monotonic()
        by_index = {f.index: f for f in frames}
        timeline: list[conditions.FrameFacts] = []
        for win in windows(frames, self.cfg.window_sec):
            obj = self._ask(win, deadline_monotonic, pm=facts_prompt)
            for e in obj["frames"]:
                f = by_index[e["index"]]
                timeline.append(conditions.FrameFacts(e["index"], f.pts_sec, e["cashier_at_workplace"],
                                                      e["client_at_counter"], e["service_in_progress"]))
        timings["inference"] = (time.monotonic() - t) * 1000
        t = time.monotonic()
        outcome = conditions.evaluate_cash01(timeline)
        # Локальная структура условий для dev-проверки человеком; в AnalysisResult не входит.
        self.last_trace = conditions.trace(timeline, outcome)
        unknown = sum(1 for x in timeline if "unknown" in (x.cashier, x.client))
        if not outcome.events and outcome.insufficient:
            # Неизвестное не превращается ни в нарушение, ни в его отсутствие.
            raise UnassessableVideoError("CASH-01 conditions could not be established for an absence interval")
        rule = self.rules[facts_prompt.SUPPORTED_TYPE_ID]
        events = []
        for n, c in enumerate(sorted(outcome.events, key=lambda c: c.absent[0].pts_sec), start=1):
            ev_frames = [by_index[c.pre.index], by_index[c.absent[len(c.absent) // 2].index], by_index[c.post.index]]
            events.append(Event(
                event_id=f"ev-{n:03d}", type_id=rule.type_id, type_name_ru=rule.type_name_ru,
                start_sec=c.pre.pts_sec, end_sec=c.post.pts_sec, timecode_sec=c.absent[0].pts_sec,
                observed_fact=conditions.describe(c), rule_id=rule.rule_id,
                evidence=[LocalEvidence(timestamp_sec=f.pts_sec, local_jpeg_path=str(f.evidence_path.resolve()))
                          for f in ev_frames]))
        limitations = [f"coverage: CASH-01 only ({facts_prompt.FACTS_PROMPT_VERSION}); other violation types are "
                       "not analysed",
                       f"sampling step {self.cfg.step_sec:g}s: absences shorter than "
                       f"{conditions.MIN_ABSENCE_FRAMES} sampled frames are ignored",
                       "unfinished service is inferred from visible service before and after the absence with the "
                       "client continuously at the counter; role and identity are not established",
                       f"{unknown}/{len(timeline)} frames with unknown cashier/client presence"]
        if outcome.insufficient:
            limitations.append(f"{len(outcome.insufficient)} absence intervals not established")
        if outcome.excluded:
            limitations.append(f"{len(outcome.excluded)} absence intervals excluded by counterexample conditions")
        timings["postprocess"] = (time.monotonic() - t) * 1000
        return self._finish(events, limitations, info, job_dir, deadline_monotonic, timings, t_start)

    def _analyze_facts_v2(self, info, frames: list[Frame], job_dir: Path, deadline_monotonic: float,
                          timings: dict[str, float], t_start: float) -> AnalysisResult:
        """CASH-01/02, COLLECT-02: факты по кадрам от модели, условия — rules_v2 по всему ролику."""
        t = time.monotonic()
        by_index = {f.index: f for f in frames}
        rm = self.rules_mod
        timeline: list[rules_v2.Facts] = []
        for win in windows(frames, self.cfg.window_sec):
            obj = self._ask(win, deadline_monotonic, pm=self.facts_pm)
            for e in self.facts_pm.to_rows(obj):
                timeline.append(rules_v2.Facts(e["index"], by_index[e["index"]].pts_sec,
                                               {k: e[k] for k in rm.FIELDS}))
        timings["inference"] = (time.monotonic() - t) * 1000
        t = time.monotonic()
        cands = rm.evaluate(timeline)
        if rm is rules_v4:
            for c in cands:
                if c.type_id == rules_v4.TYPE_ACCESS01 and c.decision == "event":
                    self._verify_access(c, by_index, deadline_monotonic)
        self.last_trace = rm.trace(timeline, cands)
        unknown = sum(1 for x in timeline if "unknown" in (x["cashier_at_workplace"], x["client_at_counter"]))
        cashier_unknown = sum(1 for x in timeline if x["cashier_at_workplace"] == "unknown")
        if cashier_unknown * 10 >= len(timeline) * 9:
            # Непригодно только если модель не может судить даже о кассире почти на всех кадрах; неуверенность
            # о клиенте — limitation, а не отказ (FAQ 15: злоупотребление «недостаточно данных» штрафуется).
            raise UnassessableVideoError("cashier presence cannot be judged on almost all frames")
        chosen = sorted((c for c in cands if c.decision == "event" and c.type_id in self.rules),
                        key=lambda c: c.interval[0].pts_sec)
        events = []
        for n, c in enumerate(chosen, start=1):
            rule = self.rules[c.type_id]
            ev = [by_index[f.index] for f in c.evidence]
            pts = [f.pts_sec for f in ev] + [c.interval[0].pts_sec, c.interval[-1].pts_sec]
            events.append(Event(
                event_id=f"ev-{n:03d}", type_id=rule.type_id, type_name_ru=rule.type_name_ru,
                start_sec=min(pts), end_sec=max(pts), timecode_sec=c.interval[0].pts_sec,
                observed_fact=rm.describe(c), rule_id=rule.rule_id,
                evidence=[LocalEvidence(timestamp_sec=f.pts_sec, local_jpeg_path=str(f.evidence_path.resolve()))
                          for f in ev]))
        unruled = sum(1 for c in cands if c.decision == "event" and c.type_id not in self.rules)
        limitations = [f"coverage ({self.cfg.prompt_version}): " + ", ".join(sorted(self.rules)),
                       f"sampling step {self.cfg.step_sec:g}s: shorter actions may be missed",
                       "facts are model judgements per frame; role and identity are not established",
                       f"{unknown}/{len(timeline)} frames with unknown cashier/client presence"]
        for label, n in (("candidates not established", sum(c.decision == "insufficient" for c in cands)),
                         ("candidates excluded by counterexamples", sum(c.decision == "excluded" for c in cands)),
                         ("events with incomplete context", sum(not c.context_complete for c in chosen)),
                         ("event candidates of types without a verified rule", unruled)):
            if n:
                limitations.append(f"{n} {label}")
        if any(c.type_id == rules_v2.TYPE_COLLECT02 for c in chosen):
            limitations.append("collection bag damage is not verified from video")
        timings["postprocess"] = (time.monotonic() - t) * 1000
        return self._finish(events, limitations, info, job_dir, deadline_monotonic, timings, t_start)

    def _verify_access(self, c, by_index: dict, deadline: float) -> None:
        """Второй запрос по трём кадрам кандидата; событие остаётся только если подтверждены все три."""
        run = c.interval
        picks = sorted({0, len(run) // 2, len(run) - 1})
        frames = [by_index[run[i].index] for i in picks]
        idx = [f.index for f in frames]
        resp = self.client.chat(facts_v4.verify_parts(frames), facts_v4.verify_schema(idx), max_tokens=200,
                                timeout=min(self.call_timeout_sec, remaining_seconds(deadline)),
                                temperature=self.cfg.temperature, top_k=self.cfg.top_k, seed=self.cfg.seed,
                                cache_prompt=self.cfg.cache_prompt,
                                **({"extra_body": self.extra_body} if self.extra_body else {}))
        remaining_seconds(deadline)
        choice = resp["choices"][0]
        try:
            obj = json.loads(choice["message"].get("content") or "")
        except (ValueError, TypeError):
            obj = None
        if choice.get("finish_reason") != "stop" or not facts_v4.verify_validate(obj, idx):
            raise InvalidAnalysisResultError("access verification output invalid")
        yes = [e["index"] for e in obj["frames"] if e["other_staff_side"] == "yes"]
        status = "confirmed" if len(yes) == len(idx) else (
            "refuted" if any(e["other_staff_side"] == "no" for e in obj["frames"]) else "not_established")
        c.checks["outsider_verified"] = rules_v2.Check(status, idx)
        c.core = tuple(c.core) + ("outsider_verified",)

    def _finish(self, events, limitations, info, job_dir: Path, deadline_monotonic: float,
                timings: dict[str, float], t_start: float) -> AnalysisResult:
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
