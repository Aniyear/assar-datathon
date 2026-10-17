"""Режим cash-specialists-v5: те же факты, что cash-events-v4 (rules_v4), но каждый «специалист» — отдельный
короткий запрос с 1–2 узкими вопросами вместо одного запроса со всеми вопросами сразу.

Специалисты: люди (кассир, клиент), посторонний, сейф (дверца, ключ), действия (кадры пересчёта и обслуживания).
Итог собирает код (rules_v4), не модель.
"""
from __future__ import annotations

from pathlib import Path

from .rules_v4 import SUPPORTED_TYPES, TRI

PROMPT_VERSION = "cash-specialists-v5"
SUPPORTED_TYPE_IDS = SUPPORTED_TYPES

_HEAD = ("You see {n} frames in time order from a fixed overhead CCTV camera in a bank cash office, each preceded "
         "by its index and timestamp. The cashier is the employee who works at the staff side of the desk; a client "
         "sits or stands at the customer side. Do not identify people. ")
_TRI_HINT = " Answer yes, no or unknown for EACH frame; unknown only if the frame does not let you decide."


class _PerFrame:
    """Специалист с вопросами yes/no/unknown по каждому кадру."""

    def __init__(self, name: str, questions: dict[str, str]):
        self.name, self.questions = name, questions

    def window_parts(self, frames) -> list[str | Path]:
        q = "\n".join(f"- {k}: {v}" for k, v in self.questions.items())
        parts: list[str | Path] = [_HEAD.format(n=len(frames)) + "Look ONLY at the following." + _TRI_HINT + "\n" + q]
        for f in frames:
            parts += [f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path]
        return parts + ["Answer in JSON."]

    def schema(self, idx: list[int]) -> dict:
        item = {"type": "object", "properties": {"index": {"type": "integer", "enum": idx},
                                                 **{k: {"type": "string", "enum": list(TRI)} for k in self.questions}},
                "required": ["index", *self.questions]}
        return {"type": "object", "properties": {"frames": {"type": "array", "minItems": len(idx),
                                                            "maxItems": len(idx), "items": item}},
                "required": ["frames"]}

    def validate(self, obj, idx: list[int]) -> bool:
        if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
            return False
        seen = []
        for e in obj["frames"]:
            if not isinstance(e, dict) or any(e.get(k) not in TRI for k in self.questions):
                return False
            seen.append(e.get("index"))
        return sorted(seen) == sorted(idx)

    def rows(self, obj) -> dict[int, dict]:
        return {e["index"]: {k: e[k] for k in self.questions} for e in obj["frames"]}


class _Lists:
    """Специалист по действиям: списки кадров."""

    name = "actions"
    questions = {"counting_frames": "the cashier counts, sorts or checks banknotes (by hand or machine)",
                 "service_frames": "a client is being served (cash, documents or a card handed over or processed)"}

    def window_parts(self, frames) -> list[str | Path]:
        q = "\n".join(f"- {k}: {v}" for k, v in self.questions.items())
        parts: list[str | Path] = [_HEAD.format(n=len(frames)) + "Look ONLY at the hands and the desk. List the frame "
                                   "indices where each action is clearly visible (empty list if none):\n" + q]
        for f in frames:
            parts += [f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path]
        return parts + ["Answer in JSON."]

    def schema(self, idx: list[int]) -> dict:
        lst = {"type": "array", "maxItems": len(idx), "items": {"type": "integer", "enum": idx}}
        return {"type": "object", "properties": {k: lst for k in self.questions}, "required": list(self.questions)}

    def validate(self, obj, idx: list[int]) -> bool:
        allowed = set(idx)
        return isinstance(obj, dict) and all(
            isinstance(obj.get(k), list) and all(isinstance(i, int) and not isinstance(i, bool) and i in allowed
                                                 for i in obj[k]) for k in self.questions)


PEOPLE = _PerFrame("people", {
    "cashier": "is the cashier in the room at their workplace (sitting, standing or bending there counts)? no if the "
               "cashier has left the room or the workplace area",
    "client": "is a client at the customer side of the desk?"})
OUTSIDER = _PerFrame("outsider", {
    "outsider": "is there a person other than the cashier on the staff side of the desk, behind or next to the "
                "cashier, or at the safe or cash machine (a client at the customer side does NOT count)?"})
SAFE = _PerFrame("safe", {
    "safe_open": "is the door of the safe (metal cabinet) visibly open?",
    "keys_left": "is a key visibly left in the safe lock or lying on the safe?"})
ACTIONS = _Lists()
PASSES = (PEOPLE, OUTSIDER, SAFE, ACTIONS)


def merge(objs: list[dict], idx: list[int]) -> list[dict]:
    """Ответы специалистов → строки в формате rules_v4.FIELDS."""
    people, outsider, safe = PEOPLE.rows(objs[0]), OUTSIDER.rows(objs[1]), SAFE.rows(objs[2])
    acts = {k: set(objs[3][k]) for k in ACTIONS.questions}
    return [{"index": i, "cashier_at_workplace": people[i]["cashier"], "client_at_counter": people[i]["client"],
             "outsider_staff_side": outsider[i]["outsider"], "safe_open": safe[i]["safe_open"],
             "keys_left": safe[i]["keys_left"], "collector_present": "unknown", "bag_opened": "no",
             "cashier_counting_cash": "yes" if i in acts["counting_frames"] else "no",
             "service_in_progress": "yes" if i in acts["service_frames"] else "no"} for i in idx]
