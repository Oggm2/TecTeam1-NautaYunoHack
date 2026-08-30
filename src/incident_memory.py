"""Recovered incident memory and recurrence matching.

Every stored incident is encoded as a sparse feature vector — one-hot for
each affected dimension value (plus a "this dimension is involved at all"
flag, so a partial match still counts), the dominant decline reason, a
cyclical encoding of time-of-day and weekday, and a severity term — and a
new diagnosis is matched against that memory with 1-nearest-neighbor search
under cosine similarity. This generalizes past exact segment-overlap: two
incidents that share a *pattern* (same failing dimension, similar time of
day, same decline reason) can match even when the exact merchant or bank
differs.

An incident enters this memory when the detector verifies that conversion has
recovered for the required number of healthy evaluation windows.  This lets a
later incident benefit from the observed pattern immediately, without waiting
for an operator to complete its post-mortem.  Each record explicitly states
whether it is ``statistically_recovered`` or ``operator_confirmed`` so a
similarity match never implies that an unconfirmed root cause was verified by
a person.  Synthetic scenarios remain outside this memory.

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


def _store(content: Any) -> dict[str, Any]:
    """Migrate earlier memory formats without losing their audit trail."""
    if isinstance(content, dict) and content.get("version") == 3:
        return {
            "version": 3,
            "incident_memory": list(content.get("incident_memory", [])),
            "unverified_observed_incidents": list(content.get("unverified_observed_incidents", [])),
        }
    if isinstance(content, dict) and content.get("version") == 2:
        recovered = []
        for record in content.get("resolved_knowledge", []):
            migrated = dict(record)
            migrated.setdefault("memory_status", "historical_resolved")
            recovered.append(migrated)
        return {
            "version": 3,
            "incident_memory": recovered,
            "unverified_observed_incidents": list(content.get("unverified_observed_incidents", [])),
        }
    legacy = content.get("incidents", []) if isinstance(content, dict) else (content if isinstance(content, list) else [])
    # Old records were written at diagnosis time, before verified recovery.
    # Keep them for audit, never for recurrence recommendations.
    return {"version": 3, "incident_memory": [], "unverified_observed_incidents": list(legacy)}


def load(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        return []
    try:
        content = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _store(content)["incident_memory"]


def save(path: str, incidents: list[dict[str, Any]]) -> None:
    file = Path(path)
    try:
        existing = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    store = _store(existing)
    store["incident_memory"] = incidents
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(file)


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
    """Legacy helper; callers should use lifecycle.record_for_memory after closure."""
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
    """Add or refresh one recovered incident without erasing history."""
    incident_id = new_record.get("incident_id")
    kept = [record for record in records if record.get("incident_id") != incident_id]
    kept.append(new_record)
    return kept
