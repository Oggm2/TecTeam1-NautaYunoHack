"""Persistent incident memory and ML-based recurrence matching.

Every stored incident is encoded as a sparse feature vector — one-hot for
each affected dimension value (plus a "this dimension is involved at all"
flag, so a partial match still counts), the dominant decline reason, a
cyclical encoding of time-of-day and weekday, and a severity term — and a
new diagnosis is matched against that memory with 1-nearest-neighbor search
under cosine similarity. This generalizes past exact segment-overlap: two
incidents that share a *pattern* (same failing dimension, similar time of
day, same decline reason) can match even when the exact merchant or bank
differs.

No numpy/scikit-learn dependency: with the handful of incidents this store
realistically holds for a hackathon-scale demo, a brute-force nearest-
neighbor scan in pure Python is both fast enough and one less thing to
install before a live run.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

DIMENSION_KEYS = ("merchant", "provider", "payment_method", "country", "issuing_bank")


def load(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        return []
    content = json.loads(file.read_text(encoding="utf-8"))
    return content.get("incidents", []) if isinstance(content, dict) else content


def save(path: str, incidents: list[dict[str, Any]]) -> None:
    Path(path).write_text(json.dumps({"incidents": incidents}, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _vectorize(record: dict[str, Any]) -> dict[str, float]:
    """Feature engineering shared by memory records and live diagnoses."""
    segment = record.get("root_cause_segment", {})
    vector: dict[str, float] = {}
    for key in DIMENSION_KEYS:
        if key in segment:
            vector[f"has:{key}"] = 1.0
            vector[f"{key}:{segment[key]}"] = 1.0
    decline = record.get("decline_reason")
    if decline:
        vector[f"decline:{decline}"] = 1.0
    at = _parse_time(record.get("resolved_at") or record.get("observed_at"))
    if at is not None:
        # Cyclical encoding: hour 23 and hour 0 should read as close, not maximally far apart.
        vector["hour_sin"] = math.sin(2 * math.pi * at.hour / 24)
        vector["hour_cos"] = math.cos(2 * math.pi * at.hour / 24)
        vector["weekday_sin"] = math.sin(2 * math.pi * at.weekday() / 7)
        vector["weekday_cos"] = math.cos(2 * math.pi * at.weekday() / 7)
    cost = record.get("cost_per_hour_usd") or 0.0
    vector["severity"] = math.log1p(max(0.0, cost)) / 12  # compresses six-figure USD/hr into roughly [0, 1]
    return vector


def _cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    shared_keys = a.keys() & b.keys()
    dot = sum(a[key] * b[key] for key in shared_keys)
    norm_a = math.sqrt(sum(v * v for v in a.values())) or 1.0
    norm_b = math.sqrt(sum(v * v for v in b.values())) or 1.0
    return dot / (norm_a * norm_b)


def match(current: dict[str, Any], records: list[dict[str, Any]], minimum_similarity: float = 0.55) -> dict[str, Any] | None:
    """1-nearest-neighbor recurrence search over engineered incident features."""
    if not records:
        return None
    query_input = {
        "root_cause_segment": current.get("root_cause_segment", {}),
        "decline_reason": (current.get("dominant_decline") or {}).get("decline_reason"),
        "observed_at": current.get("diagnosed_at"),
        "cost_per_hour_usd": (current.get("root_metrics") or {}).get("expected_unrecovered_amount_per_hour_usd")
        or (current.get("root_metrics") or {}).get("gross_lost_amount_per_hour_usd"),
    }
    query = _vectorize(query_input)
    scored = [(_cosine_similarity(query, _vectorize(record)), record) for record in records]
    scored = [(score, record) for score, record in scored if score >= minimum_similarity]
    if not scored:
        return None
    score, record = max(scored, key=lambda pair: pair[0])
    return {"similarity": round(score, 4), "previous_incident": record, "method": "knn_cosine_v1"}


def record_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Turn a diagnosed dashboard incident into a memory record for future recurrence matching."""
    diagnosis = entry["diagnosis"]
    return {
        "incident_id": diagnosis.get("incident_id"),
        "root_cause_segment": diagnosis.get("root_cause_segment", {}),
        "decline_reason": (diagnosis.get("dominant_decline") or {}).get("decline_reason"),
        "observed_at": diagnosis.get("diagnosed_at"),
        "duration_minutes": entry.get("duration_minutes"),
        "cost_per_hour_usd": entry.get("cost_per_hour_usd"),
        "resolution_note": None,
    }


def upsert(records: list[dict[str, Any]], new_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Add or refresh the memory record for this exact root-cause segment."""
    segment = new_record.get("root_cause_segment", {})
    kept = [record for record in records if record.get("root_cause_segment", {}) != segment]
    kept.append(new_record)
    return kept
