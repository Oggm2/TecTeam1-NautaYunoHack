"""Persistent, non-duplicating loss ledger for confirmed payment incidents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from detector import parse_timestamp
from recovery_estimator import RECOVERABLE_FAILURE_STATUSES, RecoveryEstimator


def empty() -> dict[str, Any]:
    return {"active": {}, "resolved": {}, "claimed_transactions": {}}


def normalize(raw: Any) -> dict[str, Any]:
    base = empty()
    if not isinstance(raw, dict):
        return base
    for key in base:
        if isinstance(raw.get(key), dict):
            base[key] = raw[key]
    return base


def matches(event: dict[str, Any], segment: dict[str, Any]) -> bool:
    return all(str(event.get(key, "")).casefold() == str(value).casefold() for key, value in segment.items())


def completed_at(event: dict[str, Any]) -> datetime | None:
    return parse_timestamp(event.get("completed_at"))


def attribution(entry: dict[str, Any], events: list[dict[str, Any]]) -> float:
    """Estimate the share of each decline attributable to the incident."""
    diagnosis = entry.get("diagnosis", {})
    metrics = diagnosis.get("root_metrics", {})
    segment = entry.get("root_cause_segment", {})
    window = diagnosis.get("incident_window", {})
    started, ended = parse_timestamp(window.get("started_at")), parse_timestamp(window.get("ended_at"))
    failures = [
        event for event in events
        if event.get("status") in RECOVERABLE_FAILURE_STATUSES
        and matches(event, segment)
        and (timestamp := completed_at(event))
        and (not started or timestamp >= started)
        and (not ended or timestamp <= ended)
    ]
    return min(1.0, float(metrics.get("lost_approvals") or 0.0) / len(failures)) if failures else 0.0


def _period_start(now: datetime, period: str) -> datetime:
    now = now.astimezone(UTC)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported period: {period}")


def period_totals(ledger: dict[str, Any], now: datetime) -> dict[str, float]:
    records = [*ledger["active"].values(), *ledger["resolved"].values()]
    totals: dict[str, float] = {}
    for period in ("today", "week", "month"):
        start = _period_start(now, period).date().isoformat()
        totals[period] = round(sum(
            sum(float(amount) for date, amount in record.get("loss_by_date_usd", {}).items() if date >= start)
            for record in records
        ), 2)
    return totals


def attach(entries: list[dict[str, Any]], ledger: dict[str, Any]) -> None:
    for entry in entries:
        record = ledger["active"].get(str(entry.get("incident_key")))
        entry["accumulated_incident_loss_usd"] = round(float(record.get("accumulated_loss_usd", 0.0)), 2) if record else 0.0
        entry["incident_started_at"] = record.get("started_at") if record else None
        entry["ledger_checkpoint_at"] = record.get("last_checkpoint_at") if record else None


def checkpoint(
    ledger: dict[str, Any], entries: list[dict[str, Any]], events: list[dict[str, Any]],
    recovery_estimator: RecoveryEstimator, observed_at: datetime,
) -> dict[str, Any]:
    """Add only newly completed, attributable declines and freeze resolved incidents."""
    ledger = normalize(ledger)
    active_entries = [entry for entry in entries if entry.get("status") == "active" and entry.get("diagnosis", {}).get("evidence_sufficient")]
    active_keys = {str(entry.get("incident_key")) for entry in active_entries}

    for key in set(ledger["active"]) - active_keys:
        record = ledger["active"].pop(key)
        record["resolved_at"] = observed_at.isoformat()
        ledger["resolved"][key] = record

    # Specific segments claim transactions before broad segments. Priority then
    # provides a deterministic tie-breaker, preventing company-wide double count.
    ordered = sorted(active_entries, key=lambda entry: (-len(entry.get("root_cause_segment", {})), entry.get("priority_rank", 999)))
    for entry in ordered:
        key = str(entry.get("incident_key"))
        diagnosis = entry.get("diagnosis", {})
        segment = entry.get("root_cause_segment", {})
        window_start = parse_timestamp(diagnosis.get("incident_window", {}).get("started_at")) or observed_at
        record = ledger["active"].setdefault(key, {
            "incident_key": key, "started_at": window_start.isoformat(), "last_checkpoint_at": window_start.isoformat(),
            "root_cause_segment": segment, "accumulated_loss_usd": 0.0, "loss_by_date_usd": {}, "attributed_transactions": 0,
        })
        checkpoint_at = parse_timestamp(record.get("last_checkpoint_at")) or window_start
        share = attribution(entry, events)
        for event in events:
            timestamp = completed_at(event)
            transaction_id = str(event.get("transaction_id", ""))
            if not timestamp or timestamp <= checkpoint_at or timestamp > observed_at or not transaction_id:
                continue
            if event.get("status") not in RECOVERABLE_FAILURE_STATUSES or not matches(event, segment):
                continue
            if transaction_id in ledger["claimed_transactions"]:
                continue
            probability = recovery_estimator.probability(event).probability
            loss = max(0.0, float(event.get("amount_usd", 0) or 0)) * share * (1 - probability)
            if loss <= 0:
                continue
            ledger["claimed_transactions"][transaction_id] = key
            record["accumulated_loss_usd"] += loss
            date = timestamp.astimezone(UTC).date().isoformat()
            record["loss_by_date_usd"][date] = record["loss_by_date_usd"].get(date, 0.0) + loss
            record["attributed_transactions"] += 1
        record["last_checkpoint_at"] = observed_at.isoformat()
        record["root_cause_segment"] = segment

    attach(entries, ledger)
    # Transaction identifiers only protect active/recent incidents; retaining a
    # bounded horizon keeps the ledger compact during long-lived deployments.
    cutoff = observed_at - timedelta(days=35)
    active_transaction_ids = {
        str(event.get("transaction_id")) for event in events
        if (timestamp := completed_at(event)) and timestamp >= cutoff
    }
    ledger["claimed_transactions"] = {
        transaction_id: key for transaction_id, key in ledger["claimed_transactions"].items()
        if transaction_id in active_transaction_ids
    }
    return ledger

