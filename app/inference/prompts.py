"""Промпт и JSON-схема наблюдений для окна кадров. Версия меняется при любой правке текста или схемы."""
from __future__ import annotations

from pathlib import Path

PROMPT_VERSION = "obs-v0.1"

# Наблюдаемые действия, а не юридические выводы. Связь с rule_id делает каталог правил.
CATEGORIES = [
    "cashier_leaves_during_service",   # кандидат CASH-01
    "cash_counted_after_client_left",  # кандидат CASH-02
    "cash_left_outside_storage",       # кандидат CASH-03
    "cash_bag_handover",               # кандидат COLLECT-01
    "cash_bag_opened",                 # кандидат COLLECT-02
    "other_notable_action",
]

INTRO = (
    "You are a visual inspector of bank cash-desk CCTV. You see {n} frames in time order from one camera, "
    "each preceded by its index and timestamp. Report only what is directly visible. Do not identify people, "
    "do not guess identities, documents content, intentions or anything not visible.\n"
    "Categories of observable actions:\n"
    "- cashier_leaves_during_service: the cashier walks away from the workstation while a client is still being served\n"
    "- cash_counted_after_client_left: cash received from a client is counted only after that client has left\n"
    "- cash_left_outside_storage: cash from a finished operation is left out on the desk instead of a lockable storage\n"
    "- cash_bag_handover: a sealed cash-collection bag is handed over\n"
    "- cash_bag_opened: a cash-collection bag is opened\n"
    "- other_notable_action: another clearly visible cash-handling action\n"
    "Set frames_assessable=false only if the scene cannot be judged at all (black, blurred, occluded). "
    "Return an empty observations list when none of the categories is visible. "
    "Each observation lists the frame indices that show it and a description of at most 15 words. "
    "Report each distinct action once."
)


def window_parts(frames) -> list[str | Path]:
    parts: list[str | Path] = [INTRO.format(n=len(frames))]
    for f in frames:
        parts.append(f"Frame {f.index}, t={f.pts_sec:.1f}s:")
        parts.append(f.model_path)
    parts.append("Answer in JSON.")
    return parts


def schema(frame_indices: list[int]) -> dict:
    return {
        "type": "object",
        "properties": {
            "frames_assessable": {"type": "boolean"},
            "observations": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": CATEGORIES},
                        "frame_indices": {"type": "array", "minItems": 1, "maxItems": 6,
                                          "items": {"type": "integer", "enum": frame_indices}},
                        # без maxLength: llama.cpp разворачивает ограниченные повторы в грамматику (bad_alloc)
                        "description": {"type": "string"},
                    },
                    "required": ["category", "frame_indices", "description"],
                },
            },
        },
        "required": ["frames_assessable", "observations"],
    }


def validate(obj, frame_indices: list[int]) -> bool:
    """Программная проверка поверх grammar-ограничения сервера."""
    if not isinstance(obj, dict) or not isinstance(obj.get("frames_assessable"), bool):
        return False
    obs = obj.get("observations")
    if not isinstance(obs, list):
        return False
    allowed = set(frame_indices)
    for o in obs:
        if not isinstance(o, dict) or o.get("category") not in CATEGORIES:
            return False
        fi = o.get("frame_indices")
        if not isinstance(fi, list) or not fi or not all(isinstance(i, int) and i in allowed for i in fi):
            return False
        # Индивидуальное наблюдение обязательно: пустой/пробельный текст — невалидный ответ.
        if not isinstance(o.get("description"), str) or not o["description"].strip():
            return False
    return True
