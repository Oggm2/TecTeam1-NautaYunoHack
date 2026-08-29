"""Explain a detector alert by finding where its estimated approval loss concentrates.

The program is intentionally deterministic: it calculates the diagnosis from
transactions and historical baselines. An LLM can later turn this JSON result
into an operations or executive explanation without changing the evidence.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baseline import Baseline, BaselineStore
from detector import CONVERSION_STATUSES, parse_timestamp


DIMENSIONS = ("merchant", "provider", "payment_method", "country", "issuing_bank")
DRILL_DOWN_DIMENSIONS = ("merchant", "provider", "payment_method", "issuing_bank")
DIAGNOSIS_SEGMENTS = tuple(
    combination
    for length in range(1, len(DIMENSIONS) + 1)
    for combination in itertools.combinations(DIMENSIONS, length)
    if "country" in combination
)


def canonical_dimensions(dimensions: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    requested = set(dimensions)
    return tuple(dimension for dimension in DIMENSIONS if dimension in requested)


def matches(event: dict[str, Any], segment: dict[str, str]) -> bool:
    return all(str(event.get(dimension)) == str(value) for dimension, value in segment.items())


def load_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                records.append(json.loads(line))
    return records


def load_alert(path: str) -> dict[str, Any]:
    """Accept an alert JSON object or a JSONL file and use its last alert."""
    content = Path(path).read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("The alert file is empty.")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return json.loads(content.splitlines()[-1])


def build_baseline(history: list[dict[str, Any]]) -> BaselineStore:
    store = BaselineStore(DIAGNOSIS_SEGMENTS)
    for event in history:
        completed_at = parse_timestamp(event.get("completed_at"))
        if completed_at:
            store.add(event, completed_at)
    return store


def loss_metrics(events: list[dict[str, Any]], baseline: Baseline) -> dict[str, float | int]:
    attempts = len(events)
    approved = sum(event["status"] == "approved" for event in events)
    observed = approved / attempts
    standard_error = math.sqrt(baseline.expected_conversion * (1 - baseline.expected_conversion) / attempts)
    z_score = (observed - baseline.expected_conversion) / standard_error if standard_error else 0.0
    lost_approvals = max(0.0, attempts * baseline.expected_conversion - approved)
    average_amount_usd = sum(float(event.get("amount_usd", 0)) for event in events) / attempts
    return {
        "attempts": attempts,
        "approved": approved,
        "observed_conversion": observed,
        "expected_conversion": baseline.expected_conversion,
        "conversion_drop_pp": (baseline.expected_conversion - observed) * 100,
        "z_score": z_score,
        "lost_approvals": lost_approvals,
        "lost_amount_usd": lost_approvals * average_amount_usd,
    }


def expected_for_segment(store: BaselineStore, segment: dict[str, str], at: datetime, minimum_history_attempts: int) -> Baseline | None:
    dimensions = canonical_dimensions(tuple(segment))
    return store.expected_for(
        {"detection_dimensions": dimensions, "segment": segment}, at, minimum_history_attempts
    )


def candidate_breakdown(
    events: list[dict[str, Any]], parent_segment: dict[str, str], dimension: str,
    store: BaselineStore, at: datetime, args: argparse.Namespace,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        buckets[str(event.get(dimension, "unknown"))].append(event)
    rows: list[dict[str, Any]] = []
    for value, child_events in buckets.items():
        if len(child_events) < args.min_attempts:
            continue
        child_segment = parent_segment | {dimension: value}
        baseline = expected_for_segment(store, child_segment, at, args.min_history_attempts)
        if baseline is None:
            continue
        metrics = loss_metrics(child_events, baseline)
        rows.append({
            "value": value, "segment": child_segment, "baseline_attempts": baseline.attempts,
            "baseline_source": baseline.source, **metrics,
        })
    return rows


def dominant_decline_reason(
    current_events: list[dict[str, Any]], history: list[dict[str, Any]], segment: dict[str, str], min_attempts: int,
) -> dict[str, Any] | None:
    historical_events = [event for event in history if event.get("status") in CONVERSION_STATUSES and matches(event, segment)]
    if len(historical_events) < min_attempts:
        return None
    current_counts: dict[str, int] = defaultdict(int)
    history_counts: dict[str, int] = defaultdict(int)
    for event in current_events:
        if event.get("status") != "approved":
            current_counts[str(event.get("decline_reason") or "unknown")] += 1
    for event in historical_events:
        if event.get("status") != "approved":
            history_counts[str(event.get("decline_reason") or "unknown")] += 1
    excess = {
        reason: max(0.0, count - len(current_events) * history_counts[reason] / len(historical_events))
        for reason, count in current_counts.items()
    }
    total_excess = sum(excess.values())
    if not total_excess:
        return None
    reason, excess_count = max(excess.items(), key=lambda item: item[1])
    return {
        "decline_reason": reason,
        "excess_declines": round(excess_count, 1),
        "share_of_excess_declines": round(excess_count / total_excess, 4),
    }


def recommendation(segment: dict[str, str], decline: dict[str, Any] | None) -> str:
    if "provider" in segment:
        return f"Contactar a {segment['provider']} y evaluar el enrutamiento temporal de pagos afectados hacia otro provider."
    if "issuing_bank" in segment:
        detail = "; el código dominante indica indisponibilidad" if decline and decline["decline_reason"] == "issuer_unavailable" else ""
        return f"Validar el estado de {segment['issuing_bank']} con el merchant afectado{detail}."
    if "payment_method" in segment:
        return f"Verificar la disponibilidad de {segment['payment_method']} y comunicar el impacto a los merchants afectados."
    return "Investigar el incidente con el equipo de operaciones antes de ejecutar cambios de enrutamiento."


def diagnose(alert: dict[str, Any], current: list[dict[str, Any]], history: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    evidence = alert.get("evidence", {})
    initial_segment = {key: str(value) for key, value in evidence.get("segment", {}).items()}
    if not initial_segment or "country" not in initial_segment:
        raise ValueError("The alert must include an evidence.segment with country.")
    ended_at = parse_timestamp(evidence.get("window_ended_at")) or datetime.now(UTC)
    started_at = parse_timestamp(evidence.get("window_started_at"))
    current = [
        event for event in current
        if event.get("status") in CONVERSION_STATUSES
        and matches(event, initial_segment)
        and (not started_at or (completed_at := parse_timestamp(event.get("completed_at"))) and started_at <= completed_at <= ended_at)
    ]
    store = build_baseline(history)
    parent_baseline = expected_for_segment(store, initial_segment, ended_at, args.min_history_attempts)
    if parent_baseline is None or len(current) < args.min_attempts:
        return {
            "incident_id": str(uuid.uuid4()), "alert_id": alert.get("alert_id"),
            "evidence_sufficient": False, "confidence": "low",
            "reason": "No hay suficiente volumen actual o histórico para diagnosticar el incidente.",
            "root_cause_segment": initial_segment,
        }

    parent_metrics = loss_metrics(current, parent_baseline)
    path: list[dict[str, Any]] = []
    root_segment = initial_segment.copy()
    root_events = current
    remaining = [dimension for dimension in DRILL_DOWN_DIMENSIONS if dimension not in root_segment]
    for _ in range(args.max_depth):
        best: dict[str, Any] | None = None
        for dimension in remaining:
            rows = candidate_breakdown(root_events, root_segment, dimension, store, ended_at, args)
            for row in rows:
                contribution = row["lost_approvals"] / parent_metrics["lost_approvals"] if parent_metrics["lost_approvals"] else 0.0
                row["contribution_to_parent_loss"] = contribution
                row["dimension"] = dimension
                is_anomalous = row["conversion_drop_pp"] >= args.min_drop_pp and row["z_score"] <= -args.z_threshold
                if is_anomalous and (best is None or contribution > best["contribution_to_parent_loss"]):
                    best = row
        if best is None or best["contribution_to_parent_loss"] < args.min_contribution:
            break
        path.append(best)
        root_segment = best["segment"]
        root_events = [event for event in root_events if matches(event, {best["dimension"]: best["value"]})]
        parent_metrics = loss_metrics(root_events, expected_for_segment(store, root_segment, ended_at, args.min_history_attempts))
        remaining.remove(best["dimension"])

    decline = dominant_decline_reason(root_events, history, root_segment, args.min_history_attempts)
    sufficient = bool(path) or parent_metrics["conversion_drop_pp"] >= args.min_drop_pp
    confidence = "high" if path and path[-1]["contribution_to_parent_loss"] >= 0.8 else "medium" if sufficient else "low"
    result = {
        "incident_id": str(uuid.uuid4()), "alert_id": alert.get("alert_id"), "diagnosed_at": datetime.now(UTC).isoformat(),
        "evidence_sufficient": sufficient, "confidence": confidence,
        "root_cause_segment": root_segment, "incident_window": {"started_at": evidence.get("window_started_at"), "ended_at": evidence.get("window_ended_at")},
        "root_metrics": {key: round(value, 4) if isinstance(value, float) else value for key, value in parent_metrics.items()},
        "drill_down_path": path, "dominant_decline": decline,
        "recommended_action": recommendation(root_segment, decline),
    }
    if not path:
        result["reason"] = "La caída está confirmada, pero no hay un subsegmento que concentre suficiente pérdida para atribuir una causa más específica."
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose a conversion-drop alert using current and historical JSONL data.")
    parser.add_argument("--alert", required=True, help="Alert JSON or JSONL file emitted by detector.py.")
    parser.add_argument("--transactions", required=True, help="Captured live transaction JSONL file.")
    parser.add_argument("--history", required=True, help="Normal historical transaction JSONL file.")
    parser.add_argument("--min-attempts", type=int, default=20)
    parser.add_argument("--min-history-attempts", type=int, default=200)
    parser.add_argument("--min-drop-pp", type=float, default=5.0)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--min-contribution", type=float, default=0.60)
    parser.add_argument("--max-depth", type=int, default=3)
    args = parser.parse_args()
    if args.min_attempts <= 0 or args.min_history_attempts <= 0 or args.max_depth <= 0:
        parser.error("Minimum attempts and max depth must be positive.")
    if not 0 < args.min_contribution <= 1:
        parser.error("--min-contribution must be in (0, 1].")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    output = diagnose(load_alert(arguments.alert), load_jsonl(arguments.transactions), load_jsonl(arguments.history), arguments)
    print(json.dumps(output, indent=2))
