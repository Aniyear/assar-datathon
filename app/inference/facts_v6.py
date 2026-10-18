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
ESSENTIAL_PASSES = (PEOPLE,)  # при нехватке времени «стол» пропускается: CASH-03 там не оценивается
_ = (_TRI_HINT,)


def merge(objs: list[dict], idx: list[int]) -> list[dict]:
    """objs — ответы PASSES по порядку; без ответа «стола» desk_valuables = unknown."""
    people = PEOPLE.rows(objs[0])
    desk = DESK.rows(objs[1]) if len(objs) > 1 else {i: {"desk_valuables": "unknown"} for i in idx}
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


# Рамки для доказательного кадра MULTI-01: кого модель посчитала (только силуэт, без лица и личности).
# Qwen3-VL возвращает координаты в относительной сетке 0–1000 по ширине и высоте кадра.
BOX_GRID = 1000
MAX_BOXES = 6
BOX_PROMPT = (
    "The frame shows a bank cash office from a fixed overhead camera. The cashier is the employee at the staff side "
    "of the desk. Give a bounding box for EACH person OTHER than the cashier who is INSIDE the room (not behind a "
    "glass door or wall). bbox_2d is [x1, y1, x2, y2] in relative coordinates from 0 to 1000. Do not identify people.")


def box_parts(frame) -> list[str | Path]:
    return [BOX_PROMPT, f"Frame {frame.index}, t={frame.pts_sec:.1f}s:", frame.model_path, "Answer in JSON."]


def box_schema() -> dict:
    box = {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
    item = {"type": "object", "properties": {"bbox_2d": box}, "required": ["bbox_2d"]}
    return {"type": "object", "properties": {"people": {"type": "array", "maxItems": MAX_BOXES, "items": item}},
            "required": ["people"]}


def boxes(obj) -> list[tuple[int, int, int, int]] | None:
    """Проверенные рамки (x1 < x2, y1 < y2, в пределах сетки) или None, если ответ не по схеме."""
    if not isinstance(obj, dict) or not isinstance(obj.get("people"), list):
        return None
    out = []
    for e in obj["people"]:
        b = e.get("bbox_2d") if isinstance(e, dict) else None
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, int) and 0 <= v <= BOX_GRID for v in b)):
            return None
        if b[0] < b[2] and b[1] < b[3]:
            out.append(tuple(b))
    return out

