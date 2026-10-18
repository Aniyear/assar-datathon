"""Промпт cash-events-v3: присутствие людей — по кадрам, действия — списком кадров на окно.

Тот же набор фактов, что cash-facts-v2 (rules_v2), но короче: 3 вопроса на кадр вместо 6 и три списка кадров
для действий. Кадр, не названный в списке действия, считается «действие не видно» (no); неизвестное
присутствие людей допускается. Модель не выносит вывод о нарушении.
"""
from __future__ import annotations

from pathlib import Path

from .rules_v2 import SUPPORTED_TYPES, TRI

PROMPT_VERSION = "cash-events-v3"
SUPPORTED_TYPE_IDS = SUPPORTED_TYPES
PRESENCE = ("cashier", "client", "collector")
ACTIONS = {"counting_frames": "cashier_counting_cash", "bag_open_frames": "bag_opened",
           "service_frames": "service_in_progress"}

INTRO = (
    "You see {n} frames in time order from a fixed overhead CCTV camera at a bank cash desk, each preceded by "
    "its index and timestamp. Roles are by position: the cashier works on the staff side of the desk, a client "
    "stands on the customer side, a cash collector brings or takes a cash-collection bag. Do not identify people.\n"
    "1) presence: for EACH frame say yes, no or unknown for: cashier (at their workplace; standing or bending "
    "there still counts), client (at the customer side of the counter), collector (at the desk area). Use "
    "unknown only if the frame does not let you decide.\n"
    "2) List the frame indices where each action is clearly visible (empty list if none):\n"
    "- counting_frames: the cashier counts, sorts or checks banknotes (by hand or machine)\n"
    "- bag_open_frames: a cash-collection bag is open, being opened, or its contents are taken out\n"
    "- service_frames: a client is being served (cash, documents or a card handed over or processed)\n"
    "Look carefully at the hands and the desk. Do not judge whether anything is a violation."
)


def window_parts(frames) -> list[str | Path]:
    parts: list[str | Path] = [INTRO.format(n=len(frames))]
    for f in frames:
        parts.append(f"Frame {f.index}, t={f.pts_sec:.1f}s:")
        parts.append(f.model_path)
    parts.append("Answer in JSON.")
    return parts


def schema(frame_indices: list[int]) -> dict:
    n = len(frame_indices)
    item = {"type": "object",
            "properties": {"index": {"type": "integer", "enum": frame_indices},
                           **{k: {"type": "string", "enum": list(TRI)} for k in PRESENCE}},
            "required": ["index", *PRESENCE]}
    idx_list = {"type": "array", "maxItems": n, "items": {"type": "integer", "enum": frame_indices}}
    return {"type": "object",
            "properties": {"presence": {"type": "array", "minItems": n, "maxItems": n, "items": item},
                           **{k: idx_list for k in ACTIONS}},
            "required": ["presence", *ACTIONS]}


def validate(obj, frame_indices: list[int]) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("presence"), list):
        return False
    allowed, seen = set(frame_indices), []
    for e in obj["presence"]:
        if not isinstance(e, dict) or not isinstance(e.get("index"), int) or isinstance(e.get("index"), bool):
            return False
        if any(e.get(k) not in TRI for k in PRESENCE):
            return False
        seen.append(e["index"])
    if sorted(seen) != sorted(frame_indices):
        return False
    for k in ACTIONS:
        v = obj.get(k)
        if not isinstance(v, list) or not all(isinstance(i, int) and not isinstance(i, bool) and i in allowed
                                              for i in v):
            return False
    return True


def to_rows(obj) -> list[dict]:
    """Строки в формате cash-facts-v2: по кадру все шесть полей rules_v2.FIELDS."""
    acts = {field: set(obj[k]) for k, field in ACTIONS.items()}
    return [{"index": e["index"], "cashier_at_workplace": e["cashier"], "client_at_counter": e["client"],
             "collector_present": e["collector"],
             **{field: "yes" if e["index"] in s else "no" for field, s in acts.items()}}
            for e in obj["presence"]]
