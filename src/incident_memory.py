"""Persistent incident memory and deterministic recurrence matching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        return []
    content = json.loads(file.read_text(encoding="utf-8"))
    return content.get("incidents", []) if isinstance(content, dict) else content


def similarity(current: dict[str, Any], previous: dict[str, Any]) -> float:
    current_segment = current.get("root_cause_segment", {})
    previous_segment = previous.get("root_cause_segment", {})
    keys = set(current_segment) | set(previous_segment)
    if not keys:
        return 0.0
    matches = sum(current_segment.get(key) == previous_segment.get(key) for key in keys)
    score = matches / len(keys)
    current_reason = (current.get("dominant_decline") or {}).get("decline_reason")
    if current_reason and current_reason == previous.get("decline_reason"):
        score = min(1.0, score + 0.15)
    return score


def match(current: dict[str, Any], records: list[dict[str, Any]], minimum_similarity: float = 0.6) -> dict[str, Any] | None:
    candidates = [(similarity(current, record), record) for record in records]
    candidates = [(score, record) for score, record in candidates if score >= minimum_similarity]
    if not candidates:
        return None
    score, record = max(candidates, key=lambda pair: pair[0])
    return {"similarity": round(score, 4), "previous_incident": record}
