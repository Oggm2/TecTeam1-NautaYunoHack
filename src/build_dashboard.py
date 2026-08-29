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
import diagnoser
import explainer
import incident_memory
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


def diagnose_alerts(alerts: list[dict[str, Any]], events: list[dict[str, Any]], history_events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    diag_args = argparse.Namespace(
        min_attempts=args.diag_min_attempts, min_history_attempts=args.min_history_attempts,
        min_drop_pp=args.min_drop_pp, z_threshold=args.z_threshold,
        min_contribution=args.min_contribution, max_depth=args.max_depth,
        recovery_horizon_hours=args.recovery_horizon_hours,
    )
    diagnoses = []
    for alert in alerts:
        diagnosis = diagnoser.diagnose(alert, events, history_events, diag_args)
        diagnosis["explanation"] = explainer.deterministic_explanation(diagnosis)
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
        severity, status = classify_severity(diagnosis, incident.priority_rank)
        recurrence = incident_memory.match(diagnosis, memory)
        entries.append({
            "incident_id": diagnosis.get("incident_id"),
            "alert_id": diagnosis.get("alert_id"),
            "incident_key": incident.incident_key,
            "priority_rank": incident.priority_rank,
            "priority_score": incident.priority_score,
            "cost_per_hour_usd": incident.cost_per_hour_usd,
            "gross_lost_amount_per_hour_usd": diagnosis.get("root_metrics", {}).get("gross_lost_amount_per_hour_usd", 0.0),
            "urgency": incident.urgency,
            "urgency_basis": incident.urgency_basis,
            "readings": incident.readings,
            "severity": severity,
            "status": status,
            "confidence_pct": confidence_percent(diagnosis),
            "root_cause_segment": segment,
            "root_cause_label": " × ".join(segment.values()) if segment else None,
            "duration_minutes": round((ended_at - started_at).total_seconds() / 60, 1) if started_at and ended_at else None,
            "recurrence": recurrence,
            "diagnosis": diagnosis,
        })
    return entries


def build_chart(events: list[dict[str, Any]], alerts: list[dict[str, Any]], history_events: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    buckets, totals = build_overall_hourly(history_events)
    per_minute: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
    for event in events:
        if event.get("status") not in CONVERSION_STATUSES:
            continue
        completed_at = parse_timestamp(event.get("completed_at"))
        if not completed_at:
            continue
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
    return {
        "period_seconds": 60, "window_seconds": args.window_seconds,
        "sustain_evaluations": args.persistence, "points": points,
    }


def build_kpis(
    entries: list[dict[str, Any]], chart: dict[str, Any], transactions_window: int, recovery_horizon_hours: float,
) -> dict[str, Any]:
    last_point = chart["points"][-1] if chart["points"] else None
    active = [e for e in entries if e["status"] == "active"]
    return {
        "current_conversion_pct": last_point["observed_pct"] if last_point else None,
        "expected_conversion_pct": last_point["expected_pct"] if last_point else None,
        "transactions_window": transactions_window,
        "active_incidents": len(active),
        "critical_count": sum(e["severity"] == "crit" for e in entries),
        "high_count": sum(e["severity"] == "high" for e in entries),
        "investigating_count": sum(e["status"] == "investigating" for e in entries),
        "expected_unrecovered_gmv_per_hour_usd": round(sum(e["cost_per_hour_usd"] for e in active), 2),
        "gross_lost_gmv_per_hour_usd": round(sum(e.get("gross_lost_amount_per_hour_usd", 0.0) for e in active), 2),
        "recovery_horizon_hours": recovery_horizon_hours,
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

    diagnoses = diagnose_alerts(alerts, events, history_events, args)
    Path(args.diagnoses_out).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.diagnoses_out).open("w", encoding="utf-8") as sink:
        for diagnosis in diagnoses:
            sink.write(json.dumps({k: v for k, v in diagnosis.items() if k != "explanation"}, separators=(",", ":")) + "\n")

    prioritized = prioritizer.prioritize(diagnoses)
    Path(args.priorities_out).write_text(json.dumps(prioritizer.to_json(prioritized), indent=2), encoding="utf-8")

    memory = incident_memory.load(args.memory)
    entries = build_incident_entries(prioritized, memory)
    chart = build_chart(events, alerts, history_events, args)
    kpis = build_kpis(entries, chart, transactions_window=len(events), recovery_horizon_hours=args.recovery_horizon_hours)

    dashboard = {
        "generated_at": datetime.now(UTC).isoformat(),
        "kpis": kpis,
        "chart": chart,
        "incidents": entries,
        "resolved": memory,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")
    print(f"wrote {output_path} with {len(entries)} incident(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
