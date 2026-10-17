"""Промпт cash-events-v4: люди и сейф — по кадрам (5 вопросов), действия — списками кадров на окно."""
from __future__ import annotations

from pathlib import Path

from .rules_v4 import SUPPORTED_TYPES, TRI

PROMPT_VERSION = "cash-events-v4"
SUPPORTED_TYPE_IDS = SUPPORTED_TYPES
PRESENCE = ("cashier", "client", "outsider", "safe_open", "keys_left")
ACTIONS = {"counting_frames": "cashier_counting_cash", "service_frames": "service_in_progress"}

INTRO = (
    "You see {n} frames in time order from a fixed overhead CCTV camera in a bank cash office, each preceded by "
    "its index and timestamp. Roles are by position: the cashier is the employee who works at the staff side of "
    "the desk; a client sits or stands at the customer side of the desk. Do not identify people.\n"
    "1) For EACH frame answer yes, no or unknown:\n"
    "- cashier: is the cashier in the room at their workplace (sitting, standing or bending there counts)? no if "
    "the cashier has left the room or the workplace area.\n"
    "- client: is a client at the customer side of the desk?\n"
    "- outsider: is there a person other than the cashier on the staff side of the desk, behind or next to the "
    "cashier, or at the safe or cash machine (not a client sitting at the customer side)?\n"
    "- safe_open: is the door of the safe (metal cabinet) visibly open?\n"
    "- keys_left: is a key visibly left in the safe lock or lying on the safe?\n"
    "Use unknown only if the frame does not let you decide.\n"
    "2) List the frame indices where each action is clearly visible (empty list if none):\n"
    "- counting_frames: the cashier counts, sorts or checks banknotes (by hand or machine)\n"
    "- service_frames: a client is being served (cash, documents or a card handed over or processed)\n"
    "Do not judge whether anything is a violation."
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
    return all(isinstance(obj.get(k), list) and all(isinstance(i, int) and not isinstance(i, bool) and i in allowed
                                                    for i in obj[k]) for k in ACTIONS)


def to_rows(obj) -> list[dict]:
    acts = {field: set(obj[k]) for k, field in ACTIONS.items()}
    return [{"index": e["index"], "cashier_at_workplace": e["cashier"], "client_at_counter": e["client"],
             "outsider_staff_side": e["outsider"], "safe_open": e["safe_open"], "keys_left": e["keys_left"],
             # инкассатор отдельно не спрашивается в v4: для контекстной проверки CASH-02 — «неизвестно»
             "collector_present": "unknown", "bag_opened": "no",
             **{field: "yes" if e["index"] in s else "no" for field, s in acts.items()}}
            for e in obj["presence"]]
