"""Режим cash-events-v4: CASH-01, CASH-02 + SAFE-01 и ACCESS-01 (по разведке типов в train/dev).

Нормы (Правила № 231, текущая редакция; проверено по открытому тексту):
- SAFE-01: п. 20 абз. 2 (ранее принятые деньги хранятся в устройствах, закрывающихся на ключ) и п. 80 (порядок
  хранения ключей от устройств на рабочем месте исключает их несанкционированное использование).
  Наблюдаемо: кассира нет ≥ 3 кадров подряд, а сейф при этом открыт или ключ оставлен в замке/на сейфе (на
  кадрах отсутствия или на последних кадрах перед уходом). Уход в конце ролика допускается.
- ACCESS-01: п. 75 (вход в кладовую/сейфовую комнату — только ответственным лицам или в их сопровождении) и
  п. 76 (ключи и коды замков хранятся у ответственных лиц). Наблюдаемо: человек, не являющийся кассиром,
  находится на стороне сотрудника / у сейфа или аппарата ≥ 2 кадров подряд. Полномочия по видео не
  устанавливаются — это ограничение в описании.
CASH-01/CASH-02 — как в rules_v2 (те же поля фактов).
"""
from __future__ import annotations

from typing import Sequence

from . import rules_v2
from .rules_v2 import (MIN_ABSENCE_FRAMES, MIN_ACTION_FRAMES, TRI, TYPE_CASH01, TYPE_CASH02, Candidate, Check,
                       Facts, _all, _dedup, _runs)

TYPE_SAFE01 = "safe_left_unsecured"
TYPE_ACCESS01 = "outsider_in_staff_zone"
SUPPORTED_TYPES = (TYPE_CASH01, TYPE_CASH02, TYPE_SAFE01, TYPE_ACCESS01)
FIELDS = ("cashier_at_workplace", "client_at_counter", "outsider_staff_side", "safe_open", "keys_left",
          "collector_present", "service_in_progress", "cashier_counting_cash", "bag_opened")
PRE_FRAMES = 2  # кадры перед уходом, на которых смотрим состояние сейфа

CONDITION_RU = {
    **rules_v2.CONDITION_RU,
    "safe_unsecured": "Сейф открыт или ключ оставлен в замке/на сейфе во время или прямо перед уходом",
    "outsider_present": f"Посторонний (не кассир) на стороне сотрудника / у сейфа ≥ {MIN_ACTION_FRAMES} кадров подряд",
}
_ = TRI


def _safe01(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["cashier_at_workplace"] != "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ABSENCE_FRAMES:
            continue
        window = list(tl[max(0, a - PRE_FRAMES):a]) + run
        idx = [f.index for f in window]
        open_hits = [f for f in window if "yes" in (f["safe_open"], f["keys_left"])]
        if open_hits:
            unsecured = Check("confirmed", [f.index for f in open_hits])
        elif all(f["safe_open"] == "no" and f["keys_left"] == "no" for f in window):
            unsecured = Check("refuted", idx, "safe closed, no key left")
        else:
            unsecured = Check("not_established", idx, "safe state unknown")
        checks = {"cashier_absent": _all(run, "cashier_at_workplace", "no", "yes"), "safe_unsecured": unsecured,
                  "interval_complete": Check("confirmed" if a > 0 else "not_established", [])}
        ev = _dedup(([tl[a - 1]] if a > 0 else []) + (open_hits[:1] or [run[0]]) + [run[-1]])
        out.append(Candidate(TYPE_SAFE01, run, checks, ("cashier_absent", "safe_unsecured"), ev))
    return out


def _access01(tl: Sequence[Facts]) -> list[Candidate]:
    out = []
    for a, b in _runs(tl, lambda f: f["outsider_staff_side"] == "yes"):
        run = list(tl[a:b + 1])
        if len(run) < MIN_ACTION_FRAMES:
            continue
        checks = {"outsider_present": Check("confirmed", [f.index for f in run])}
        out.append(Candidate(TYPE_ACCESS01, run, checks, ("outsider_present",),
                             _dedup([run[0], run[len(run) // 2], run[-1]])))
    return out


def evaluate(timeline: Sequence[Facts], types: Sequence[str] = SUPPORTED_TYPES) -> list[Candidate]:
    tl = sorted(timeline, key=lambda f: f.pts_sec)
    fns = {TYPE_CASH01: rules_v2._cash01, TYPE_CASH02: rules_v2._cash02, TYPE_SAFE01: _safe01,
           TYPE_ACCESS01: _access01}
    return [c for t in types for c in fns[t](tl)]


def describe(c: Candidate) -> str:
    a, b = c.interval[0].pts_sec, c.interval[-1].pts_sec
    if c.type_id == TYPE_SAFE01:
        s = (f"{a:.1f}–{b:.1f} с — кассира нет на рабочем месте ({len(c.interval)} кадров подряд); на кадрах "
             f"{', '.join(str(i) for i in c.checks['safe_unsecured'].frames[:4])} сейф открыт или в нём оставлен ключ.")
        if c.checks["interval_complete"].status != "confirmed":
            s += " Начало ухода не попало в запись."
        return s + " Личность не устанавливалась."
    if c.type_id == TYPE_ACCESS01:
        return (f"{a:.1f}–{b:.1f} с — человек, не являющийся кассиром, находится на стороне сотрудника / у сейфа "
                f"или аппарата ({len(c.interval)} кадров). Полномочия и сопровождение по видео не устанавливаются. "
                "Личность не устанавливалась.")
    return rules_v2.describe(c)


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
