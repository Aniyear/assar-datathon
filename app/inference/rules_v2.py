"""Проверка условий нескольких типов (режим cash-facts-v2) по покадровым фактам. Чистый Python, без модели.

Модель по каждому кадру отвечает yes / no / unknown на шесть вопросов (facts_v2.py). Решение принимает код:

- CASH-01 (п. 22 Правил № 231): кассира нет ≥ 3 кадров подряд, клиент у кассы всё это время; контекст —
  обслуживание до ухода и после возвращения.
- CASH-02 (п. 20 абз. 1): кассир пересчитывает деньги ≥ 2 кадров подряд, клиента у кассы нет; контекст —
  клиент был у кассы незадолго до пересчёта (пересчёт связан с только что закончившимся обслуживанием).
  Контрпример: при пересчёте у кассы клиент (норма) или инкассатор (другая процедура).
- COLLECT-02 (п. 25): инкассаторская сумка вскрыта / из неё достают содержимое ≥ 2 кадров подряд, инкассатора
  при этом нет; контекст — инкассатор был незадолго до вскрытия. Контрпример: вскрытие при инкассаторе.
  Повреждение сумки по видео не проверяется — это ограничение, указываемое в результате.

Каждое условие: confirmed / refuted / not_established с кадрами. Условия делятся на основные (core) и
контекстные. Опровергнуто любое → кандидат исключён; не установлено основное → «не установлено»; основные
подтверждены, а контекст не установлен (например, обрезан началом видео) → событие с пометкой. Так система не
пропускает видимое нарушение из-за обрезанного контекста (FAQ 16), но и не выдаёт его при контрпримере.
Это оценки модели по кадрам, а не гарантированная истина.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

TRI = ("yes", "no", "unknown")
FIELDS = ("cashier_at_workplace", "client_at_counter", "collector_present", "service_in_progress",
          "cashier_counting_cash", "bag_opened")
Status = Literal["confirmed", "refuted", "not_established"]

MIN_ABSENCE_FRAMES = 3
MIN_ACTION_FRAMES = 2
CONTEXT_SEC = 15.0

TYPE_CASH01 = "cashier_leaves_during_service"
TYPE_CASH02 = "cash_counted_without_client"
TYPE_COLLECT02 = "collection_bag_opened_without_collector"
SUPPORTED_TYPES = (TYPE_CASH01, TYPE_CASH02, TYPE_COLLECT02)

CONDITION_RU = {
    "cashier_absent": f"Кассира нет на рабочем месте ≥ {MIN_ABSENCE_FRAMES} кадров подряд",
    "client_present_during_absence": "Клиент у кассы на каждом кадре отсутствия",
    "service_before": f"До ухода (≤ {CONTEXT_SEC:g} с) видно обслуживание, клиент у кассы непрерывно",
    "service_after": f"После возвращения (≤ {CONTEXT_SEC:g} с) видно обслуживание, клиент у кассы непрерывно",
    "interval_complete": "Интервал не обрезан началом/концом видео",
    "counting": f"Кассир пересчитывает деньги ≥ {MIN_ACTION_FRAMES} кадров подряд",
    "client_absent_during_counting": "Клиента у кассы нет во время пересчёта",
    "client_before_counting": f"Клиент был у кассы незадолго до пересчёта (≤ {CONTEXT_SEC:g} с)",
    "no_collector_during_counting": "Инкассатора нет во время пересчёта",
    "bag_opened": f"Инкассаторская сумка вскрыта / из неё достают содержимое ≥ {MIN_ACTION_FRAMES} кадров подряд",
    "collector_absent_during_opening": "Инкассатора нет во время вскрытия",
    "collector_before_opening": f"Инкассатор был незадолго до вскрытия (≤ {CONTEXT_SEC:g} с)",
}


@dataclass(frozen=True)
class Facts:
    index: int
    pts_sec: float
    v: dict  # поле -> yes/no/unknown

    def __getitem__(self, k: str) -> str:
        return self.v[k]


@dataclass
class Check:
    status: Status
    frames: list[int]
    note: str = ""


@dataclass
class Candidate:
    type_id: str
    interval: list[Facts]
    checks: dict[str, Check]
    core: tuple[str, ...]
    evidence: list[Facts] = field(default_factory=list)

    @property
    def decision(self) -> Literal["event", "excluded", "insufficient"]:
        if any(c.status == "refuted" for c in self.checks.values()):
            return "excluded"
        if any(self.checks[k].status == "not_established" for k in self.core):
            return "insufficient"
        return "event"

    @property
    def context_complete(self) -> bool:
        return all(c.status == "confirmed" for c in self.checks.values())


def _runs(tl: Sequence[Facts], pred: Callable[[Facts], bool]) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(tl):
        if pred(f):
            start = i if start is None else start
        elif start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(tl) - 1))
    return out


def _all(frames: Sequence[Facts], key: str, want: str, bad: str) -> Check:
    """confirmed — все кадры want; refuted — хоть один bad; иначе (есть unknown) not_established."""
    idx = [f.index for f in frames]
    if any(f[key] == bad for f in frames):
        return Check("refuted", idx, f"{key}={bad} on some frames")
    if all(f[key] == want for f in frames):
        return Check("confirmed", idx)
    return Check("not_established", idx, f"{key} unknown")


def _context(tl: Sequence[Facts], edge: int, step: int, t_edge: float, hit: Callable[[Facts], str],
             ) -> tuple[Check, Facts | None]:
    """Поиск наружу от края интервала в пределах CONTEXT_SEC. hit(f) -> 'yes' | 'no' | 'unknown'."""
    i, seen, saw_unknown = edge, [], False
    while 0 <= i < len(tl) and abs(tl[i].pts_sec - t_edge) <= CONTEXT_SEC:
        f = tl[i]
        seen.append(f.index)
        h = hit(f)
        if h == "yes":
            return Check("confirmed", seen), f
        saw_unknown |= h == "unknown"
        i += step
    if not (0 <= i < len(tl)):
        return Check("not_established", seen, "context cut by video start/end"), None
    if saw_unknown:
        return Check("not_established", seen, "context unknown"), None
    return Check("refuted", seen, "not seen in context"), None


def _cash01(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["cashier_at_workplace"] != "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ABSENCE_FRAMES:
            continue
        checks = {"cashier_absent": _all(run, "cashier_at_workplace", "no", "yes"),
                  "client_present_during_absence": _all(run, "client_at_counter", "yes", "no")}

        def service(f: Facts) -> str:
            if f["client_at_counter"] == "no":
                return "no_client"
            if "unknown" in (f["client_at_counter"], f["service_in_progress"], f["cashier_at_workplace"]):
                return "unknown"
            return "yes" if f["service_in_progress"] == "yes" and f["cashier_at_workplace"] == "yes" else "no"

        def ctx(edge: int, step: int, t: float) -> tuple[Check, Facts | None]:
            # клиент должен оставаться у кассы между краем и кадром обслуживания
            i = edge
            while 0 <= i < len(tl) and abs(tl[i].pts_sec - t) <= CONTEXT_SEC:
                if service(tl[i]) == "no_client":
                    return Check("refuted", [tl[i].index], "client not at counter in context"), None
                if service(tl[i]) == "yes":
                    break
                i += step
            return _context(tl, edge, step, t, lambda f: "unknown" if service(f) == "unknown" else
                            ("yes" if service(f) == "yes" else "no"))

        pre_c, pre = ctx(a - 1, -1, run[0].pts_sec) if a > 0 else (Check("not_established", [], "starts at video start"), None)
        post_c, post = ctx(b + 1, 1, run[-1].pts_sec) if b < len(tl) - 1 else (Check("not_established", [], "ends at video end"), None)
        cut = a == 0 or b == len(tl) - 1
        checks.update(service_before=pre_c, service_after=post_c,
                      interval_complete=Check("not_established" if cut else "confirmed", []))
        ev = [x for x in (pre, run[len(run) // 2], post) if x is not None]
        if len(ev) < 2:
            ev = [run[0], run[len(run) // 2], run[-1]]
        out.append(Candidate(TYPE_CASH01, run, checks, ("cashier_absent", "client_present_during_absence"),
                             _dedup(ev)))
    return out


def _cash02(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["cashier_counting_cash"] == "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ACTION_FRAMES:
            continue
        checks = {"counting": Check("confirmed", [f.index for f in run]),
                  "client_absent_during_counting": _all(run, "client_at_counter", "no", "yes"),
                  "no_collector_during_counting": _all(run, "collector_present", "no", "yes")}
        if a > 0:
            c, pre = _context(tl, a - 1, -1, run[0].pts_sec, lambda f: f["client_at_counter"])
        else:
            c, pre = Check("not_established", [], "starts at video start"), None
        checks["client_before_counting"] = c
        ev = _dedup([x for x in (pre, run[0], run[-1]) if x is not None])
        out.append(Candidate(TYPE_CASH02, run, checks, ("counting", "client_absent_during_counting"), ev))
    return out


def _collect02(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["bag_opened"] == "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ACTION_FRAMES:
            continue
        checks = {"bag_opened": Check("confirmed", [f.index for f in run]),
                  "collector_absent_during_opening": _all(run, "collector_present", "no", "yes")}
        if a > 0:
            c, pre = _context(tl, a - 1, -1, run[0].pts_sec, lambda f: f["collector_present"])
        else:
            c, pre = Check("not_established", [], "starts at video start"), None
        checks["collector_before_opening"] = c
        ev = _dedup([x for x in (pre, run[0], run[-1]) if x is not None])
        out.append(Candidate(TYPE_COLLECT02, run, checks, ("bag_opened", "collector_absent_during_opening"), ev))
    return out


def _dedup(frames: list[Facts]) -> list[Facts]:
    seen, out = set(), []
    for f in sorted(frames, key=lambda f: f.pts_sec):
        if f.index not in seen:
            seen.add(f.index)
            out.append(f)
    return out[:3]


def evaluate(timeline: Sequence[Facts], types: Sequence[str] = SUPPORTED_TYPES) -> list[Candidate]:
    tl = sorted(timeline, key=lambda f: f.pts_sec)
    fns = {TYPE_CASH01: _cash01, TYPE_CASH02: _cash02, TYPE_COLLECT02: _collect02}
    return [c for t in types for c in fns[t](tl)]


def describe(c: Candidate) -> str:
    """Индивидуальное обоснование из фактов с временами реальных кадров."""
    a, b = c.interval[0].pts_sec, c.interval[-1].pts_sec
    ctx = {k: v for k, v in c.checks.items() if k not in c.core}
    tail = "" if c.context_complete else (
        " Контекст установлен не полностью: " + ", ".join(CONDITION_RU[k].lower() for k, v in ctx.items()
                                                          if v.status != "confirmed") + ".")
    if c.type_id == TYPE_CASH01:
        s = (f"{a:.1f}–{b:.1f} с — кассира нет на рабочем месте ({len(c.interval)} кадров подряд), клиент остаётся "
             "у кассы.")
    elif c.type_id == TYPE_CASH02:
        s = f"{a:.1f}–{b:.1f} с — кассир пересчитывает деньги, клиента у кассы нет."
        if c.checks["client_before_counting"].status == "confirmed":
            s += " Незадолго до этого клиент был у кассы."
    else:
        s = f"{a:.1f}–{b:.1f} с — инкассаторская сумка вскрыта или из неё достают содержимое, инкассатора нет."
        if c.checks["collector_before_opening"].status == "confirmed":
            s += " Незадолго до этого инкассатор был у кассы."
        s += " Повреждение сумки по видео не проверялось."
    return s + " Личность не устанавливалась." + tail


def trace(timeline: Sequence[Facts], cands: Sequence[Candidate]) -> dict:
    return {
        "timeline": [[f.index, round(f.pts_sec, 3), *[f[k] for k in FIELDS]]
                     for f in sorted(timeline, key=lambda f: f.pts_sec)],
        "fields": list(FIELDS),
        "candidates": [{
            "type_id": c.type_id, "decision": c.decision, "absent": [c.interval[0].pts_sec, c.interval[-1].pts_sec],
            "evidence": [f.index for f in c.evidence],
            "checks": {k: {"status": v.status, "frames": v.frames, "note": v.note} for k, v in c.checks.items()},
        } for c in cands],
    }
