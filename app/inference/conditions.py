"""Проверка условий CASH-01 (п. 22 Правил № 231) по покадровым фактам. Чистый Python, без модели.

Норма: кассовому работнику нельзя покидать рабочее место до окончания обслуживания клиента.
Модель по каждому выбранному кадру отвечает «yes / no / unknown» на три вопроса (facts_prompt.py);
решение о событии принимает только этот код:

1. Уход: не менее MIN_ABSENCE_FRAMES подряд кадров, где кассира нет на рабочем месте (cashier=no).
   Более короткое отсутствие (встал, наклонился, перекрыт) событием не считается.
2. Клиент у кассы на каждом кадре отсутствия (client=yes). Клиент тоже ушёл — контрпример.
3. До ухода (≤ CONTEXT_SEC) виден кадр обслуживания: кассир и клиент на месте, service=yes; клиент у кассы
   непрерывно от этого кадра до ухода.
4. После возвращения (≤ CONTEXT_SEC) снова виден кадр обслуживания при непрерывном присутствии клиента.
   Пункты 3–4 — наблюдаемая замена «операция не завершена»: обслуживание идёт до и после ухода, клиент
   у кассы всё время. Личность клиента не устанавливается: «непрерывно присутствует», не «тот же человек».

Неизвестное (unknown) в проверяемом интервале, отсутствие, обрезанное началом/концом видео, и отсутствие
неизвестной длины — «не установлено» (insufficient), а не нарушение и не его отсутствие.

Каждое условие кандидата хранится структурированно: confirmed / refuted / not_established и кадры,
на которых основана оценка. Это оценки модели по кадрам, а не гарантированная истина.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

Tri = Literal["yes", "no", "unknown"]
TRI = ("yes", "no", "unknown")
Status = Literal["confirmed", "refuted", "not_established"]

# Инженерные границы, заданы до прогонов и не подобраны по данным: при шаге 1 с — уход ≥ 2 с между
# первым и последним кадром отсутствия; контекст обслуживания — до 15 с до и после.
MIN_ABSENCE_FRAMES = 3
CONTEXT_SEC = 15.0

CONDITIONS = ("cashier_absent", "client_present_during_absence", "service_before", "service_after",
              "interval_complete")
CONDITION_RU = {
    "cashier_absent": f"Кассира нет на рабочем месте ≥ {MIN_ABSENCE_FRAMES} кадров подряд",
    "client_present_during_absence": "Клиент у кассы на каждом кадре отсутствия",
    "service_before": f"До ухода (≤ {CONTEXT_SEC:g} с) видно обслуживание, клиент у кассы непрерывно",
    "service_after": f"После возвращения (≤ {CONTEXT_SEC:g} с) видно обслуживание, клиент у кассы непрерывно",
    "interval_complete": "Отсутствие не обрезано началом/концом видео",
}


@dataclass(frozen=True)
class FrameFacts:
    index: int
    pts_sec: float
    cashier: Tri   # кассир на рабочем месте
    client: Tri    # клиент у кассы
    service: Tri   # видимое обслуживание: передача/пересчёт/обработка наличных или документов клиенту


@dataclass
class ConditionCheck:
    status: Status
    frames: list[int]   # индексы кадров, на которых основана оценка
    note: str = ""


@dataclass
class Candidate:
    """Отсутствие кассира ≥ MIN_ABSENCE_FRAMES кадров и оценка каждого условия."""
    absent: list[FrameFacts]
    checks: dict[str, ConditionCheck]
    pre: FrameFacts | None = None    # кадр обслуживания до ухода
    post: FrameFacts | None = None   # кадр обслуживания после возвращения

    @property
    def decision(self) -> Literal["event", "excluded", "insufficient"]:
        statuses = [c.status for c in self.checks.values()]
        if "refuted" in statuses:
            return "excluded"
        if "not_established" in statuses:
            return "insufficient"
        return "event"


@dataclass
class Cash01Outcome:
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def events(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "event"]

    @property
    def insufficient(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "insufficient"]

    @property
    def excluded(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "excluded"]

    @property
    def absence_runs(self) -> int:
        return len(self.candidates)


def _runs(tl: Sequence[FrameFacts]) -> list[tuple[int, int]]:
    """Максимальные отрезки [i, j], где кассир не подтверждён на месте (no/unknown)."""
    out, start = [], None
    for i, f in enumerate(tl):
        if f.cashier != "yes":
            start = i if start is None else start
        elif start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(tl) - 1))
    return out


def _find_service(tl: Sequence[FrameFacts], edge: int, step: int, t_edge: float) -> tuple[ConditionCheck, FrameFacts | None]:
    """От края отсутствия идём наружу (step=-1 назад, +1 вперёд) в пределах CONTEXT_SEC.

    Клиент должен быть у кассы на всех кадрах между краем и кадром обслуживания.
    """
    i, seen, saw_unknown = edge, [], False
    while 0 <= i < len(tl) and abs(tl[i].pts_sec - t_edge) <= CONTEXT_SEC:
        f = tl[i]
        seen.append(f.index)
        if f.client == "no":
            return ConditionCheck("refuted", seen, "client not at counter between service and absence"), None
        if f.client == "unknown":
            return ConditionCheck("not_established", seen, "client presence unknown"), None
        if f.cashier == "yes" and f.service == "yes":
            return ConditionCheck("confirmed", seen), f
        if f.service == "unknown" or f.cashier == "unknown":
            saw_unknown = True
        i += step
    if not (0 <= i < len(tl)):
        return ConditionCheck("not_established", seen, "context cut by video start/end"), None
    if saw_unknown:
        return ConditionCheck("not_established", seen, "service unknown in context"), None
    return ConditionCheck("refuted", seen, "no visible service in context"), None


def _assess(tl: Sequence[FrameFacts], a: int, b: int) -> Candidate:
    run = list(tl[a:b + 1])
    idx = [f.index for f in run]
    checks: dict[str, ConditionCheck] = {}
    if any(f.cashier == "unknown" for f in run):
        checks["cashier_absent"] = ConditionCheck("not_established", idx, "cashier presence unknown")
    else:
        checks["cashier_absent"] = ConditionCheck("confirmed", idx)
    if any(f.client == "no" for f in run):
        checks["client_present_during_absence"] = ConditionCheck("refuted", idx, "client left")
    elif any(f.client == "unknown" for f in run):
        checks["client_present_during_absence"] = ConditionCheck("not_established", idx, "client unknown")
    else:
        checks["client_present_during_absence"] = ConditionCheck("confirmed", idx)
    cut = a == 0 or b == len(tl) - 1
    checks["interval_complete"] = ConditionCheck("not_established" if cut else "confirmed",
                                                 [idx[0] if a == 0 else idx[-1]] if cut else [])
    pre = post = None
    if a > 0:
        checks["service_before"], pre = _find_service(tl, a - 1, -1, run[0].pts_sec)
    else:
        checks["service_before"] = ConditionCheck("not_established", [], "absence starts at video start")
    if b < len(tl) - 1:
        checks["service_after"], post = _find_service(tl, b + 1, +1, run[-1].pts_sec)
    else:
        checks["service_after"] = ConditionCheck("not_established", [], "absence ends at video end")
    return Candidate(absent=run, checks={k: checks[k] for k in CONDITIONS}, pre=pre, post=post)


def evaluate_cash01(timeline: Sequence[FrameFacts]) -> Cash01Outcome:
    tl = sorted(timeline, key=lambda f: f.pts_sec)
    out = Cash01Outcome()
    for a, b in _runs(tl):
        if b - a + 1 >= MIN_ABSENCE_FRAMES:  # краткое отсутствие/перекрытие не доказывает уход
            out.candidates.append(_assess(tl, a, b))
    return out


def describe(c: Candidate) -> str:
    """Индивидуальное обоснование из фактов с реальными временами кадров (без общих формулировок)."""
    a, b = c.absent[0].pts_sec, c.absent[-1].pts_sec
    return (f"{c.pre.pts_sec:.1f} с — у кассы клиент, видно обслуживание (передача или обработка наличных/"
            f"документов); {a:.1f}–{b:.1f} с — кассира нет на рабочем месте ({len(c.absent)} кадров подряд), "
            f"клиент остаётся у кассы; {c.post.pts_sec:.1f} с — кассир на месте, обслуживание клиента "
            "продолжается. Клиент у кассы присутствует непрерывно; личность не устанавливалась")


def trace(timeline: Sequence[FrameFacts], outcome: Cash01Outcome) -> dict:
    """Локальная структура для проверки человеком (не входит в публичный результат)."""
    return {
        "timeline": [[f.index, round(f.pts_sec, 3), f.cashier, f.client, f.service]
                     for f in sorted(timeline, key=lambda f: f.pts_sec)],
        "candidates": [{
            "decision": c.decision,
            "absent": [c.absent[0].pts_sec, c.absent[-1].pts_sec],
            "pre": c.pre.index if c.pre else None, "post": c.post.index if c.post else None,
            "checks": {k: {"status": v.status, "frames": v.frames, "note": v.note} for k, v in c.checks.items()},
        } for c in outcome.candidates],
    }
