"""Промпт покадровых фактов для CASH-01. Модель не выносит вывод о нарушении — только факты по кадрам."""
from __future__ import annotations

from pathlib import Path

from .conditions import TRI

FACTS_PROMPT_VERSION = "cash01-facts-v1"
SUPPORTED_TYPE_ID = "cashier_leaves_during_service"
FIELDS = ("cashier_at_workplace", "client_at_counter", "service_in_progress")

INTRO = (
    "You see {n} frames in time order from a fixed CCTV camera at a bank cash desk, each preceded by its "
    "index and timestamp. For EACH frame answer three questions with yes, no or unknown. Answer unknown "
    "when the frame does not let you decide (occlusion, blur, person partly out of view). Do not guess and "
    "do not identify people.\n"
    "- cashier_at_workplace: is the cashier (the employee on the staff side of the cash desk) at their "
    "workplace? Standing up, turning or bending at the workplace still counts as yes. Answer no only if the "
    "cashier has clearly left the workplace area.\n"
    "- client_at_counter: is a client present at the client side of the counter?\n"
    "- service_in_progress: is service visibly in progress at the counter in this frame: cash, documents or "
    "a card being handed over, counted or processed for the client?\n"
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
    return {"type": "object", "properties": {"frames": {"type": "array", "minItems": n, "maxItems": n,
                                                        "items": item}},
            "required": ["frames"]}


def validate(obj, frame_indices: list[int]) -> bool:
    """Ровно по одной записи на каждый кадр окна, значения только yes/no/unknown."""
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
