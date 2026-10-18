"""Режим cash-specialists-v6: два специалиста с узкими вопросами (люди; стол) → rules_v6."""
from __future__ import annotations

from pathlib import Path

from .facts_v5 import _HEAD, _TRI_HINT, _PerFrame
from .rules_v6 import SUPPORTED_TYPES

PROMPT_VERSION = "cash-specialists-v6"
SUPPORTED_TYPE_IDS = SUPPORTED_TYPES
COUNTS = ["0", "1", "2+", "unknown"]


class _People:
    name = "people"

    def window_parts(self, frames) -> list[str | Path]:
        q = ("For EACH frame answer:\n"
             "- cashier: yes, no or unknown — is the cashier inside the room at their workplace (sitting, standing or "
             "bending there counts)? no if the cashier has left the room.\n"
             "- others: how many people OTHER than the cashier are INSIDE the room (clients, visitors, collectors)? "
             "Answer 0, 1, 2+ or unknown. People outside behind a glass door or wall do NOT count.")
        parts: list[str | Path] = [_HEAD.format(n=len(frames)) + q]
        for f in frames:
            parts += [f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path]
        return parts + ["Answer in JSON."]

    def schema(self, idx: list[int]) -> dict:
        item = {"type": "object", "properties": {"index": {"type": "integer", "enum": idx},
                                                 "cashier": {"type": "string", "enum": ["yes", "no", "unknown"]},
                                                 "others": {"type": "string", "enum": COUNTS}},
                "required": ["index", "cashier", "others"]}
        return {"type": "object", "properties": {"frames": {"type": "array", "minItems": len(idx),
                                                            "maxItems": len(idx), "items": item}},
                "required": ["frames"]}

    def validate(self, obj, idx: list[int]) -> bool:
        if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
            return False
        ok = [e.get("index") for e in obj["frames"] if isinstance(e, dict)
              and e.get("cashier") in ("yes", "no", "unknown") and e.get("others") in COUNTS]
        return sorted(ok) == sorted(idx)

    def rows(self, obj) -> dict[int, dict]:
        return {e["index"]: e for e in obj["frames"]}


PEOPLE = _People()
DESK = _PerFrame("desk", {
    "desk_valuables": "are banknotes, a stack of cash, coin rolls or a cash-collection bag lying on the desk or table "
                      "(not inside the safe, a drawer or the cash machine)?"})
PASSES = (PEOPLE, DESK)
_ = (_TRI_HINT,)


def merge(objs: list[dict], idx: list[int]) -> list[dict]:
    people, desk = PEOPLE.rows(objs[0]), DESK.rows(objs[1])
    return [{"index": i, "cashier_at_workplace": people[i]["cashier"], "others_count": people[i]["others"],
             "desk_valuables": desk[i]["desk_valuables"]} for i in idx]


# Второй шаг для MULTI-01: по трём кадрам кандидата (все три «yes» — подтверждено).
VERIFY_MULTI = (
    "Each frame shows a bank cash office from a fixed overhead camera. The cashier is the employee at the staff side "
    "of the desk. For EACH frame answer yes, no or unknown: are there at least TWO people OTHER than the cashier "
    "INSIDE the room at the same time (for example two clients at the desk)? People outside behind a glass door, a "
    "window or a wall do NOT count. The cashier does NOT count.")


def verify_parts(frames) -> list[str | Path]:
    parts: list[str | Path] = [VERIFY_MULTI]
    for f in frames:
        parts += [f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path]
    return parts + ["Answer in JSON."]


def verify_schema(frame_indices: list[int]) -> dict:
    n = len(frame_indices)
    item = {"type": "object", "properties": {"index": {"type": "integer", "enum": frame_indices},
                                             "two_others_inside": {"type": "string", "enum": ["yes", "no", "unknown"]}},
            "required": ["index", "two_others_inside"]}
    return {"type": "object", "properties": {"frames": {"type": "array", "minItems": n, "maxItems": n,
                                                        "items": item}}, "required": ["frames"]}


def verify_validate(obj, frame_indices: list[int]) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
        return False
    seen = [e.get("index") for e in obj["frames"]
            if isinstance(e, dict) and e.get("two_others_inside") in ("yes", "no", "unknown")]
    return sorted(seen) == sorted(frame_indices)
