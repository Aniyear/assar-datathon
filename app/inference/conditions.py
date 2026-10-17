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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

Tri = Literal["yes", "no", "unknown"]
TRI = ("yes", "no", "unknown")

# Инженерные границы, заданы до прогонов и не подобраны по данным: при шаге 1 с — уход ≥ 2 с между
# первым и последним кадром отсутствия; контекст обслуживания — до 15 с до и после.
MIN_ABSENCE_FRAMES = 3
CONTEXT_SEC = 15.0


@dataclass(frozen=True)
class FrameFacts:
    index: int
    pts_sec: float
    cashier: Tri   # кассир на рабочем месте
    client: Tri    # клиент у кассы
    service: Tri   # видимое обслуживание: передача/пересчёт/обработка наличных или документов клиенту


@dataclass
class Candidate:
    pre: FrameFacts            # кадр обслуживания до ухода
    absent: list[FrameFacts]   # кадры отсутствия кассира
    post: FrameFacts           # кадр обслуживания после возвращения


@dataclass
class Cash01Outcome:
    events: list[Candidate] = field(default_factory=list)
    insufficient: list[str] = field(default_factory=list)   # причины «не установлено»
    excluded: list[str] = field(default_factory=list)       # контрпримеры / неполные условия
    absence_runs: int = 0


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


def _find_service(tl: Sequence[FrameFacts], edge: int, step: int, t_edge: float) -> tuple[str, FrameFacts | None]:
    """От края отсутствия идём наружу (step=-1 назад, +1 вперёд) в пределах CONTEXT_SEC.

    Возвращает ("ok", кадр) | ("insufficient", None) | ("excluded", None). Клиент должен быть у кассы на
    всех кадрах между краем и кадром обслуживания.
    """
    i, saw_unknown = edge, False
    while 0 <= i < len(tl) and abs(tl[i].pts_sec - t_edge) <= CONTEXT_SEC:
        f = tl[i]
        if f.client == "no":
            return "excluded", None
        if f.client == "unknown":
            return "insufficient", None
        if f.cashier == "yes" and f.service == "yes":
            return "ok", f
        if f.service == "unknown" or f.cashier == "unknown":
            saw_unknown = True
        i += step
    if not (0 <= i < len(tl)):  # контекст обрезан началом/концом видео
        return "insufficient", None
    return ("insufficient", None) if saw_unknown else ("excluded", None)


def evaluate_cash01(timeline: Sequence[FrameFacts]) -> Cash01Outcome:
    tl = sorted(timeline, key=lambda f: f.pts_sec)
    out = Cash01Outcome()
    for a, b in _runs(tl):
        run = tl[a:b + 1]
        if len(run) < MIN_ABSENCE_FRAMES:
            continue  # краткое отсутствие/перекрытие не доказывает уход
        out.absence_runs += 1
        where = f"{run[0].pts_sec:.1f}-{run[-1].pts_sec:.1f}s"
        if any(f.cashier == "unknown" for f in run):
            out.insufficient.append(f"{where}: cashier presence unknown")
            continue
        if any(f.client == "no" for f in run):
            out.excluded.append(f"{where}: client not at counter during absence")
            continue
        if any(f.client == "unknown" for f in run):
            out.insufficient.append(f"{where}: client presence unknown")
            continue
        if a == 0 or b == len(tl) - 1:
            out.insufficient.append(f"{where}: absence cut by video start/end")
            continue
        pre_state, pre = _find_service(tl, a - 1, -1, run[0].pts_sec)
        post_state, post = _find_service(tl, b + 1, +1, run[-1].pts_sec)
        if "excluded" in (pre_state, post_state):
            out.excluded.append(f"{where}: service before/after absence not established")
        elif "insufficient" in (pre_state, post_state):
            out.insufficient.append(f"{where}: service context unknown")
        else:
            out.events.append(Candidate(pre=pre, absent=list(run), post=post))
    return out


def describe(c: Candidate) -> str:
    """Индивидуальное обоснование из фактов с реальными временами кадров (без общих формулировок)."""
    a, b = c.absent[0].pts_sec, c.absent[-1].pts_sec
    return (f"{c.pre.pts_sec:.1f} с — у кассы клиент, видно обслуживание (передача или обработка наличных/"
            f"документов); {a:.1f}–{b:.1f} с — кассира нет на рабочем месте ({len(c.absent)} кадров подряд), "
            f"клиент остаётся у кассы; {c.post.pts_sec:.1f} с — кассир на месте, обслуживание клиента "
            "продолжается. Клиент у кассы присутствует непрерывно; личность не устанавливалась")
