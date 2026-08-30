"""End-to-end orchestrator: replay captured live transactions, detect, diagnose, cost,
prioritize and explain every incident, then write a single JSON file the
frontend renders directly.

It uses data/live_transactions.jsonl produced by generator.py, not a separate
compressed simulation. The frontend reads the snapshot it writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import detector
import counterfactual_routing
import diagnoser
import explainer
import incident_memory
import incident_loss_ledger
import prioritizer
from recovery_estimator import RecoveryEstimator
from baseline import BaselineStore
from detector import CONVERSION_STATUSES, parse_timestamp


def load_jsonl(path: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    raw = Path(path).read_bytes()
    # Windows PowerShell's Tee-Object writes UTF-16 by default; Python tools
    # and the generator itself write UTF-8. Support both captured formats.
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    for line in raw.decode(encoding).splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def build_store(events: list[dict[str, Any]], dimensions: tuple[tuple[str, ...], ...]) -> BaselineStore:
    store = BaselineStore(dimensions)
    for event in events:
        completed_at = parse_timestamp(event.get("completed_at"))
        if completed_at:
            store.add(event, completed_at)
    return store


def build_overall_hourly(history: list[dict[str, Any]]) -> tuple[dict[tuple[int, int], list[int]], list[int]]:
    """Weekday/hour approval buckets used only for the chart's expected line."""
    buckets: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    totals = [0, 0]
    for event in history:
        if event.get("status") not in CONVERSION_STATUSES:
            continue
        completed_at = parse_timestamp(event.get("completed_at"))
        if not completed_at:
            continue
        approved = int(event["status"] == "approved")
        bucket = buckets[(completed_at.weekday(), completed_at.hour)]
        bucket[0] += 1
        bucket[1] += approved
        totals[0] += 1
        totals[1] += approved
    return buckets, totals


def expected_conversion_at(buckets: dict[tuple[int, int], list[int]], totals: list[int], at: datetime, min_attempts: int = 200) -> float:
    bucket = buckets.get((at.weekday(), at.hour))
    if bucket and bucket[0] >= min_attempts:
        return (bucket[1] + 1) / (bucket[0] + 2)
    return (totals[1] + 1) / (totals[0] + 2)


def replay_transactions(history_events: list[dict[str, Any]], events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    detector_store = build_store(history_events, detector.SEGMENT_DEFINITIONS)
    detector_args = argparse.Namespace(
        window_seconds=args.window_seconds, evaluation_seconds=args.evaluation_seconds,
        persistence=args.persistence, min_attempts=args.min_attempts,
        min_history_attempts=args.min_history_attempts, min_drop_pp=args.min_drop_pp,
        z_threshold=args.z_threshold,
    )
    recovery_estimator = RecoveryEstimator(horizon_hours=args.recovery_horizon_hours).fit(history_events)
    engine = detector.DetectionEngine(detector_store, detector_args, recovery_estimator)
    alerts: list[dict[str, Any]] = []
    ordered = sorted(events, key=lambda event: parse_timestamp(event.get("completed_at")) or datetime.min.replace(tzinfo=UTC))
    first_time = next((parse_timestamp(event.get("completed_at")) for event in ordered if parse_timestamp(event.get("completed_at"))), None)
    if first_time is None:
        return alerts
    next_evaluation = first_time + timedelta(seconds=args.evaluation_seconds)
    last_time = first_time
    for event in ordered:
        timestamp = parse_timestamp(event.get("completed_at"))
        if timestamp is None:
            continue
        while timestamp >= next_evaluation:
            alerts.extend(engine.evaluate(next_evaluation))
            next_evaluation += timedelta(seconds=args.evaluation_seconds)
        engine.add(event)
        last_time = timestamp
    alerts.extend(engine.evaluate(last_time))
    return alerts


def diagnose_alerts(
    alerts: list[dict[str, Any]], events: list[dict[str, Any]], history_events: list[dict[str, Any]],
    args: argparse.Namespace, memory: list[dict[str, Any]], routing_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    diag_args = argparse.Namespace(
        min_attempts=args.diag_min_attempts, min_history_attempts=args.min_history_attempts,
        min_drop_pp=args.min_drop_pp, z_threshold=args.z_threshold,
        min_contribution=args.min_contribution, max_depth=args.max_depth,
        recovery_horizon_hours=args.recovery_horizon_hours,
    )
    routing_policy = routing_policy or counterfactual_routing.load_policy(getattr(args, "routing_policy", None))
    diagnoses = []
    for alert in alerts:
        diagnosis = diagnoser.diagnose(alert, events, history_events, diag_args)
        # This is the stable detector identity used by the live incident
        # lifecycle. The diagnosis UUID changes on each drill-down refresh.
        diagnosis["alert_signature"] = alert.get("alert_signature")
        # Payment data can support an observational route comparison, but it
        # cannot authorize a switch.  Attach a conditional experiment only
        # when the exact merchant-country-method cohort is comparable.
        routing_recommendation = counterfactual_routing.recommend(diagnosis, events, routing_policy)
        diagnosis["counterfactual_recommendation"] = routing_recommendation
        diagnosis["recommended_action"] = routing_recommendation["action"]
        recurrence = incident_memory.match(diagnosis, memory)
        diagnosis["recurrence"] = recurrence
        diagnosis["explanation"] = explainer.deterministic_explanation(diagnosis, recurrence)
        diagnoses.append(diagnosis)
    return diagnoses


def classify_severity(diagnosis: dict[str, Any], priority_rank: int) -> tuple[str, str]:
    if not diagnosis.get("evidence_sufficient"):
        return "warn", "investigating"
    confidence = diagnosis.get("confidence", "low")
    if confidence == "high" and priority_rank == 1:
        return "crit", "active"
    if confidence in ("high", "medium"):
        return "high", "active"
    return "warn", "active"


def confidence_percent(diagnosis: dict[str, Any]) -> int:
    path = diagnosis.get("drill_down_path") or []
    if path:
        return round(max(40, min(99, path[-1]["contribution_to_parent_loss"] * 100)))
    return {"high": 90, "medium": 70, "low": 40}.get(diagnosis.get("confidence", "low"), 40)


def build_incident_entries(prioritized: list[prioritizer.PrioritizedIncident], memory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for incident in prioritized:
        diagnosis = incident.latest_diagnosis
        segment = diagnosis.get("root_cause_segment", {})
        window = diagnosis.get("incident_window", {})
        started_at, ended_at = parse_timestamp(window.get("started_at")), parse_timestamp(window.get("ended_at"))
        window_minutes = max(0.0, (ended_at - started_at).total_seconds() / 60) if started_at and ended_at else 0.0
        window_cost = float(incident.cost_per_hour_usd or 0.0) * window_minutes / 60
        severity, status = classify_severity(diagnosis, incident.priority_rank)
        recurrence = diagnosis.get("recurrence")
        if recurrence is None:
            recurrence = incident_memory.match(diagnosis, memory)
        entries.append({
            "incident_id": diagnosis.get("incident_id"),
            "alert_id": diagnosis.get("alert_id"),
            "lifecycle_key": diagnosis.get("alert_signature") or diagnosis.get("alert_id"),
            "detection_signature": diagnosis.get("alert_signature") or diagnosis.get("alert_id"),
            "incident_key": incident.incident_key,
            "priority_rank": incident.priority_rank,
            "priority_score": incident.priority_score,
            "cost_per_hour_usd": incident.cost_per_hour_usd,
            "current_expected_unrecovered_gmv_per_hour_usd": incident.cost_per_hour_usd,
            "window_expected_unrecovered_gmv_usd": round(window_cost, 2),
            "accumulated_incident_loss_usd": 0.0,
            "gross_lost_amount_per_hour_usd": diagnosis.get("root_metrics", {}).get("gross_lost_amount_per_hour_usd", 0.0),
            "urgency": incident.urgency,
            "urgency_basis": incident.urgency_basis,
            "priority_factors": incident.priority_factors,
            "readings": incident.readings,
            "severity": severity,
            "status": status,
            "confidence_pct": confidence_percent(diagnosis),
            "root_cause_segment": segment,
            "root_cause_label": " × ".join(segment.values()) if segment else None,
            "duration_minutes": round(window_minutes, 1) if started_at and ended_at else None,
            "recurrence": recurrence,
            "diagnosis": diagnosis,
        })
    return entries


def build_chart(events: list[dict[str, Any]], alerts: list[dict[str, Any]], history_events: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    buckets, totals = build_overall_hourly(history_events)
    per_minute: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
    completed_events: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if event.get("status") not in CONVERSION_STATUSES:
            continue
        completed_at = parse_timestamp(event.get("completed_at"))
        if not completed_at:
            continue
        completed_events.append((completed_at, event))
        minute = completed_at.replace(second=0, microsecond=0)
        bucket = per_minute[minute]
        bucket[0] += 1
        bucket[1] += int(event["status"] == "approved")

    windows = [
        (parse_timestamp(alert["evidence"]["window_started_at"]), parse_timestamp(alert["evidence"]["window_ended_at"]))
        for alert in alerts if alert.get("evidence")
    ]

    points = []
    for minute in sorted(per_minute)[-args.chart_minutes:]:
        attempts, approved = per_minute[minute]
        if attempts == 0:
            continue
        observed = approved / attempts
        expected = expected_conversion_at(buckets, totals, minute)
        standard_error = (expected * (1 - expected) / attempts) ** 0.5
        threshold = expected - args.z_threshold * standard_error
        in_incident = any(start and end and start <= minute <= end for start, end in windows)
        state = "incident" if in_incident else ("breach" if observed < threshold else "ok")
        points.append({
            "t": minute.isoformat(), "observed_pct": round(observed * 100, 2),
            "expected_pct": round(expected * 100, 2), "threshold_pct": round(threshold * 100, 2), "state": state,
        })
    latest_at = max((timestamp for timestamp, _ in completed_events), default=None)
    current_window: dict[str, Any] | None = None
    if latest_at:
        window_start = latest_at - timedelta(seconds=args.window_seconds)
        window_events = [event for timestamp, event in completed_events if timestamp >= window_start]
        attempts = len(window_events)
        approved = sum(event["status"] == "approved" for event in window_events)
        expected = expected_conversion_at(buckets, totals, latest_at)
        current_window = {
            "started_at": window_start.isoformat(), "ended_at": latest_at.isoformat(), "attempts": attempts,
            "approved": approved, "observed_pct": round(100 * approved / attempts, 2) if attempts else None,
            "expected_pct": round(100 * expected, 2),
        }
    return {
        "period_seconds": 60, "window_seconds": args.window_seconds,
        "sustain_evaluations": args.persistence,
        "stream_started_at": points[0]["t"] if points else None,
        "current_window": current_window,
        "points": points,
    }


def build_kpis(
    entries: list[dict[str, Any]], chart: dict[str, Any], transactions_window: int, recovery_horizon_hours: float,
    loss_ledger: dict[str, Any] | None = None, observed_at: datetime | None = None,
) -> dict[str, Any]:
    current_window = chart.get("current_window") or {}
    active = [e for e in entries if e["status"] in {"active", "detected", "investigating", "monitoring"}]
    return {
        "current_conversion_pct": current_window.get("observed_pct"),
        "expected_conversion_pct": current_window.get("expected_pct"),
        "conversion_window_seconds": chart.get("window_seconds"),
        "conversion_window_attempts": current_window.get("attempts", 0),
        "transactions_window": transactions_window,
        "active_incidents": len(active),
        "critical_count": sum(e["severity"] == "crit" for e in entries),
        "high_count": sum(e["severity"] == "high" for e in entries),
        "investigating_count": sum(e["status"] in {"detected", "investigating", "monitoring"} for e in entries),
        "incidence_cost_current_window_usd": round(sum(e.get("window_expected_unrecovered_gmv_usd", 0.0) for e in active), 2),
        "accumulated_incident_loss_usd": round(sum(e.get("accumulated_incident_loss_usd", 0.0) for e in active), 2),
        "current_loss_rate_per_hour_usd": round(sum(e.get("current_expected_unrecovered_gmv_per_hour_usd", e["cost_per_hour_usd"]) for e in active), 2),
        "current_incidence_cost_per_hour_usd": round(sum(e.get("current_expected_unrecovered_gmv_per_hour_usd", e["cost_per_hour_usd"]) for e in active), 2),
        "expected_unrecovered_gmv_per_hour_usd": round(sum(e["cost_per_hour_usd"] for e in active), 2),
        "gross_lost_gmv_per_hour_usd": round(sum(e.get("gross_lost_amount_per_hour_usd", 0.0) for e in active), 2),
        "recovery_horizon_hours": recovery_horizon_hours,
        "incident_loss_periods_usd": (loss_ledger or {}).get("period_totals_usd", {}),
    }


PROCESSING_TIME_UPDATE_SECONDS = 180


def build_dataset_stats(
    events: list[dict[str, Any]], processing_window_seconds: int | None = None,
) -> dict[str, Any]:
    """Compact historical/live aggregates for the dashboard analytics view."""
    terminal = [event for event in events if event.get("status") in CONVERSION_STATUSES]
    attempts = len(terminal)
    approved = sum(event.get("status") == "approved" for event in terminal)

    processing_events = [event for event in terminal if event.get("processing_time_ms") is not None]
    processing_updated_at: datetime | None = None
    if processing_window_seconds and processing_events:
        completed = [(parse_timestamp(event.get("completed_at")), event) for event in processing_events]
        completed = [(timestamp, event) for timestamp, event in completed if timestamp]
        if completed:
            latest = max(timestamp for timestamp, _ in completed)
            # A closed three-minute bucket is stable between updates.  This
            # prevents the UI from changing every dashboard refresh while
            # still providing a fresh operational latency signal.
            elapsed_seconds = int(latest.timestamp())
            bucket_end = datetime.fromtimestamp(
                elapsed_seconds - (elapsed_seconds % processing_window_seconds), tz=UTC,
            )
            bucket_start = bucket_end - timedelta(seconds=processing_window_seconds)
            processing_events = [
                event for timestamp, event in completed
                if bucket_start <= timestamp < bucket_end
            ]
            processing_updated_at = bucket_end

    processing_values = [float(event["processing_time_ms"]) for event in processing_events]

    def grouped(field: str) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in terminal:
            buckets[str(event.get(field, "unknown"))].append(event)
        return sorted([
            {"name": name, "attempts": len(rows), "conversion_pct": round(100 * sum(row.get("status") == "approved" for row in rows) / len(rows), 2)}
            for name, rows in buckets.items()
        ], key=lambda row: row["attempts"], reverse=True)

    return {
        "attempts": attempts,
        "approved": approved,
        "conversion_pct": round(100 * approved / attempts, 2) if attempts else None,
        "average_ticket_usd": round(sum(float(event.get("amount_usd", 0)) for event in terminal) / attempts, 2) if attempts else 0,
        "average_processing_time_ms": round(sum(processing_values) / len(processing_values), 1) if processing_values else None,
        "processing_time_samples": len(processing_values),
        "processing_time_window_seconds": processing_window_seconds,
        "processing_time_updated_at": processing_updated_at.isoformat() if processing_updated_at else None,
        "by_country": grouped("country"), "by_provider": grouped("provider"), "by_payment_method": grouped("payment_method"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build dashboard data from captured live transaction JSONL.")
    parser.add_argument("--history", default="data/history.jsonl")
    parser.add_argument("--transactions", default="data/live_transactions.jsonl")
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--evaluation-seconds", type=int, default=30)
    parser.add_argument("--persistence", type=int, default=3)
    parser.add_argument("--min-attempts", type=int, default=30)
    parser.add_argument("--min-history-attempts", type=int, default=200)
    parser.add_argument("--min-drop-pp", type=float, default=5.0)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--diag-min-attempts", type=int, default=20)
    parser.add_argument("--min-contribution", type=float, default=0.60)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--recovery-horizon-hours", type=float, default=24.0)
    parser.add_argument("--chart-minutes", type=int, default=30)
    parser.add_argument("--diagnoses-out", default="data/diagnoses.jsonl")
    parser.add_argument("--priorities-out", default="data/priorities.json")
    parser.add_argument("--alerts-out", default="data/alerts.jsonl")
    parser.add_argument("--memory", default="data/incident_memory.json")
    parser.add_argument("--routing-policy", default="data/routing_guardrails.json", help="Guardrails for read-only counterfactual routing experiments.")
    parser.add_argument("--runtime-config", default="data/runtime_config.json", help="Runtime priority settings shared with the live dashboard.")
    parser.add_argument("--output", default="frontend/dashboard_data.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    history_events = load_jsonl(args.history)
    events = load_jsonl(args.transactions)
    alerts = replay_transactions(history_events, events, args)
    print(f"replayed {len(events)} captured transactions, {len(alerts)} alert(s)", file=sys.stderr)

    Path(args.alerts_out).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.alerts_out).open("w", encoding="utf-8") as sink:
        for alert in alerts:
            sink.write(json.dumps(alert, separators=(",", ":")) + "\n")

    memory = incident_memory.load(args.memory)
    routing_policy = counterfactual_routing.load_policy(args.routing_policy)
    diagnoses = diagnose_alerts(alerts, events, history_events, args, memory, routing_policy)
    Path(args.diagnoses_out).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.diagnoses_out).open("w", encoding="utf-8") as sink:
        for diagnosis in diagnoses:
            sink.write(json.dumps({k: v for k, v in diagnosis.items() if k != "explanation"}, separators=(",", ":")) + "\n")

    try:
        priority_config = json.loads(Path(args.runtime_config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        priority_config = {}
    prioritized = prioritizer.prioritize(diagnoses, priority_config)
    Path(args.priorities_out).write_text(json.dumps(prioritizer.to_json(prioritized), indent=2), encoding="utf-8")

    entries = build_incident_entries(prioritized, memory)
    observed_at = datetime.now(UTC)
    offline_ledger = incident_loss_ledger.checkpoint(
        incident_loss_ledger.empty(), entries, events,
        RecoveryEstimator(horizon_hours=args.recovery_horizon_hours).fit(history_events), observed_at,
    )
    chart = build_chart(events, alerts, history_events, args)
    kpis = build_kpis(
        entries, chart, transactions_window=len(events), recovery_horizon_hours=args.recovery_horizon_hours,
        loss_ledger={**offline_ledger, "period_totals_usd": incident_loss_ledger.period_totals(offline_ledger, observed_at)}, observed_at=observed_at,
    )

    dashboard = {
        "generated_at": observed_at.isoformat(),
        "kpis": kpis,
        "chart": chart,
        "incidents": entries,
        "resolved": memory,
        "analytics": {
            "historical": build_dataset_stats(history_events),
            "live": build_dataset_stats(events, processing_window_seconds=PROCESSING_TIME_UPDATE_SECONDS),
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
    print(f"wrote {output_path} with {len(entries)} incident(s)", file=sys.stderr)

    # A statistical diagnosis is not operational closure. Memory is written
    # by the live lifecycle after statistical recovery is verified.


if __name__ == "__main__":
    main()
