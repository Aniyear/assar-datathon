"""Режим cash-specialists-v6: типы по разбору пар «нарушение / эталон» в train/dev.

- CASH-03 (п. 20 абз. 2 Правил № 231 — ранее принятые деньги хранятся в устройствах на рабочем месте,
  закрывающихся на ключ): кассира нет в комнате ≥ 3 кадров подряд, а на столе в это время лежат деньги, монеты
  или мешок инкассации (≥ 2 кадров отсутствия). Контрпример: стол чистый (ценности убраны перед уходом).
- MULTI-01 (п. 20 абз. 2 — на рабочем месте находятся наличные деньги обслуживаемого клиента): в комнате
  одновременно ≥ 2 человек кроме кассира ≥ 2 кадров подряд при кассире на месте (обслуживание второго клиента до
  завершения операции с первым). Личность и роли не устанавливаются.
"""
from __future__ import annotations

from typing import Sequence

from .rules_v2 import Candidate, Check, Facts, _all, _dedup, _runs

TYPE_CASH03 = "valuables_left_unattended"
TYPE_MULTI01 = "two_clients_at_once"
SUPPORTED_TYPES = (TYPE_CASH03, TYPE_MULTI01)
FIELDS = ("cashier_at_workplace", "others_count", "desk_valuables")
MIN_ABSENCE_FRAMES = 3
MIN_DESK_FRAMES = 2
MIN_MULTI_FRAMES = 2

CONDITION_RU = {
    "cashier_absent": f"Кассира нет в комнате ≥ {MIN_ABSENCE_FRAMES} кадров подряд",
    "valuables_on_desk": f"На столе деньги, монеты или мешок инкассации на ≥ {MIN_DESK_FRAMES} кадрах отсутствия",
    "two_or_more_others": f"В комнате одновременно ≥ 2 человек кроме кассира ≥ {MIN_MULTI_FRAMES} кадров подряд",
    "cashier_present": "Кассир на месте (идёт обслуживание)",
    "multi_verified": "Повторная узкая проверка: двое кроме кассира внутри комнаты на всех трёх кадрах",
}


def _cash03(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["cashier_at_workplace"] != "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ABSENCE_FRAMES:
            continue
        hits = [f for f in run if f["desk_valuables"] == "yes"]
        if len(hits) >= MIN_DESK_FRAMES:
            desk = Check("confirmed", [f.index for f in hits])
        elif all(f["desk_valuables"] == "no" for f in run):
            desk = Check("refuted", [f.index for f in run], "desk clear")
        else:
            desk = Check("not_established", [f.index for f in run], "desk unknown or too few frames")
        checks = {"cashier_absent": _all(run, "cashier_at_workplace", "no", "yes"), "valuables_on_desk": desk}
        ev = _dedup(([tl[a - 1]] if a > 0 else []) + (hits[:1] or [run[0]]) + [hits[-1] if hits else run[-1]])
        out.append(Candidate(TYPE_CASH03, run, checks, ("cashier_absent", "valuables_on_desk"), ev))
    return out


def _multi01(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["others_count"] == "2+"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_MULTI_FRAMES:
            continue
        checks = {"two_or_more_others": Check("confirmed", [f.index for f in run]),
                  "cashier_present": _all(run, "cashier_at_workplace", "yes", "no")}
        out.append(Candidate(TYPE_MULTI01, run, checks, ("two_or_more_others", "cashier_present"),
                             _dedup([run[0], run[len(run) // 2], run[-1]])))
    return out


def evaluate(timeline: Sequence[Facts], types: Sequence[str] = SUPPORTED_TYPES) -> list[Candidate]:
    tl = sorted(timeline, key=lambda f: f.pts_sec)
    fns = {TYPE_CASH03: _cash03, TYPE_MULTI01: _multi01}
    return [c for t in types for c in fns[t](tl)]


def describe(c: Candidate) -> str:
    a, b = c.interval[0].pts_sec, c.interval[-1].pts_sec
    if c.type_id == TYPE_CASH03:
        fr = c.checks["valuables_on_desk"].frames
        return (f"{a:.1f}–{b:.1f} с — кассира нет на рабочем месте ({len(c.interval)} кадров подряд); на кадрах "
                f"{', '.join(str(i) for i in fr[:5])} на столе лежат деньги, монеты или мешок инкассации. "
                "Личность не устанавливалась.")
    return (f"{a:.1f}–{b:.1f} с — в комнате одновременно не менее двух человек кроме кассира "
            f"({len(c.interval)} кадров), кассир на месте. Роли и личности не устанавливались.")


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
