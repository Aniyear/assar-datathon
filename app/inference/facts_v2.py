"""Промпт покадровых фактов cash-facts-v2 (CASH-01, CASH-02, COLLECT-02). Модель не выносит вывод о нарушении."""
from __future__ import annotations

from pathlib import Path

from .rules_v2 import FIELDS, SUPPORTED_TYPES, TRI

PROMPT_VERSION = "cash-facts-v2"
SUPPORTED_TYPE_IDS = SUPPORTED_TYPES

INTRO = (
    "You see {n} frames in time order from a fixed overhead CCTV camera at a bank cash desk, each preceded by "
    "its index and timestamp. For EACH frame answer six questions with yes, no or unknown. Answer unknown when "
    "the frame does not let you decide (occlusion, blur, person or object out of view). Do not guess and do not "
    "identify people. Roles are by position: the cashier works on the staff side of the desk, a client stands "
    "on the customer side, a cash collector brings or takes a cash-collection bag.\n"
    "- cashier_at_workplace: is the cashier at their workplace? Standing up, turning or bending there still "
    "counts as yes; no only if the cashier has clearly left the workplace area.\n"
    "- client_at_counter: is a client present at the customer side of the counter?\n"
    "- collector_present: is a cash collector present at the desk area?\n"
    "- service_in_progress: is a client being served right now (cash, documents or a card handed over, "
    "counted or processed for the client)?\n"
    "- cashier_counting_cash: is the cashier counting, sorting or checking banknotes (by hand or machine)?\n"
    "- bag_opened: is a cash-collection bag open, being opened, or are its contents being taken out?\n"
    "Return exactly one entry per frame, using the frame index."
)


def window_parts(frames) -> list[str | Path]:
    parts: list[str | Path] = [INTRO.format(n=len(frames))]
    for f in frames:
        parts.append(f"Frame {f.index}, t={f.pts_sec:.1f}s:")
        parts.append(f.model_path)
    parts.append("Answer in JSON.")
    return parts


def schema(frame_indices: list[int]) -> dict:
    item = {"type": "object",
            "properties": {"index": {"type": "integer", "enum": frame_indices},
                           **{k: {"type": "string", "enum": list(TRI)} for k in FIELDS}},
            "required": ["index", *FIELDS]}
    n = len(frame_indices)
    return {"type": "object",
            "properties": {"frames": {"type": "array", "minItems": n, "maxItems": n, "items": item}},
            "required": ["frames"]}


def validate(obj, frame_indices: list[int]) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("frames"), list):
        return False
    seen = []
    for e in obj["frames"]:
        if not isinstance(e, dict) or not isinstance(e.get("index"), int) or isinstance(e.get("index"), bool):
            return False
        if any(e.get(k) not in TRI for k in FIELDS):
            return False
        seen.append(e["index"])
    return sorted(seen) == sorted(frame_indices)


def to_rows(obj) -> list[dict]:
    return obj["frames"]
