"""Explicit, auditable lifecycle for payment incidents.

Detection and financial recovery are statistical observations. Operational
closure is a human decision. Every system that refers to an incident uses the
immutable ``incident_id`` minted at first detection, not a mutable alert set.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from detector import parse_timestamp
from incident_identity import DEFAULT_TENANT, canonical_signature, make_incident_id


OPEN_STATUSES = {"detected", "investigating", "monitoring"}
RECOVERED_STATUS = "recovered_automatically"
RESOLVED_STATUS = "resolved_by_operator"
DEFAULT_OWNER = "Payments Operations"


def empty() -> dict[str, Any]:
    return {"version": 2, "tenant_id": DEFAULT_TENANT, "incidents": {}, "archived_unverified_observed": []}


def _detection_signature(entry: dict[str, Any]) -> str:
    return canonical_signature(
        entry.get("detection_signature")
        or entry.get("lifecycle_key")
        or entry.get("alert_signature")
        or entry.get("root_cause_segment")
        or entry.get("incident_key")
    )


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade v1 records keyed by alert signature without losing history."""
    tenant_id = str(raw.get("tenant_id") or DEFAULT_TENANT)
    migrated: dict[str, dict[str, Any]] = {}
    archived = list(raw.get("archived_unverified_observed", [])) if isinstance(raw.get("archived_unverified_observed", []), list) else []
    source = raw.get("incidents") if isinstance(raw, dict) else {}
    for legacy_key, value in (source.items() if isinstance(source, dict) else []):
        if not isinstance(value, dict):
            continue
        # v1 lifecycle records were created before immutable identity and
        # governed closure existed. Preserve them for audit but do not show
        # them as current operational incidents or let them affect a demo.
        if value.get("identity_schema_version") != 2:
            archived.append({"legacy_key": legacy_key, "record": value})
            continue
        record = copy.deepcopy(value)
        signature = canonical_signature(record.get("detection_signature") or record.get("lifecycle_key") or legacy_key)
        created = str(record.get("created_at") or record.get("updated_at") or datetime.now(UTC).isoformat())
        incident_id = str(record.get("incident_id") or make_incident_id(created, signature, tenant_id))
        base_id, suffix = incident_id, 2
        while incident_id in migrated:
            incident_id = f"{base_id}_{suffix}"
            suffix += 1
        record["incident_id"] = incident_id
        record["detection_signature"] = signature
        record.pop("lifecycle_key", None)
        record.setdefault("financial_exposure_status", "ended" if record.get("status") in {RECOVERED_STATUS, RESOLVED_STATUS} else "active")
        if record.get("status") in {RECOVERED_STATUS, RESOLVED_STATUS}:
            record.setdefault("financial_exposure_ended_at", record.get("updated_at"))
        migrated[incident_id] = record
    return {"version": 2, "tenant_id": tenant_id, "incidents": migrated, "archived_unverified_observed": archived}


def load(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return empty()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty()
    return _migrate(raw if isinstance(raw, dict) else {})


def save(path: str | Path, state: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, destination)


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _event(record: dict[str, Any], status: str, reason: str, actor: str, at: datetime | None = None) -> None:
    record["status"] = status
    record["updated_at"] = _now(at)
    record.setdefault("status_history", []).append({
        "status": status, "at": record["updated_at"], "reason": reason, "actor": actor,
    })


def _new_record(entry: dict[str, Any], at: datetime, tenant_id: str) -> dict[str, Any]:
    observed_at = _now(at)
    signature = _detection_signature(entry)
    record: dict[str, Any] = {
        "incident_id": make_incident_id(observed_at, signature, tenant_id),
        "identity_schema_version": 2,
        "detection_signature": signature,
        "created_at": observed_at,
        "updated_at": observed_at,
        "status": "detected",
        "owner": DEFAULT_OWNER,
        "severity": entry.get("severity", "warn"),
        "active_evaluations": 1,
        "healthy_evaluations": 0,
        "financial_exposure_status": "active",
        "financial_exposure_started_at": observed_at,
        "status_history": [],
        "operator": {},
        "last_entry": copy.deepcopy(entry),
    }
    _event(record, "detected", "Sustained conversion anomaly met the configured statistical and persistence criteria.", "system", at)
    return record


def _minutes_between(start: str | None, end: str | None) -> float | None:
    left, right = parse_timestamp(start), parse_timestamp(end)
    if not left or not right:
        return None
    return round(max(0.0, (right - left).total_seconds() / 60), 1)


def _decorate(entry: dict[str, Any], record: dict[str, Any], observed_at: datetime | None = None) -> dict[str, Any]:
    decorated = copy.deepcopy(entry)
    incident_id = record["incident_id"]
    financial_status = record.get("financial_exposure_status", "active")
    ended_at = record.get("financial_exposure_ended_at")
    lifecycle_end = record.get("operational_resolved_at") or ended_at or _now(observed_at)
    last_rate = float(entry.get("current_expected_unrecovered_gmv_per_hour_usd") or entry.get("cost_per_hour_usd") or 0.0)
    decorated.update({
        "incident_id": incident_id,
        "lifecycle_key": incident_id,
        "financial_exposure_id": incident_id,
        "detection_signature": record.get("detection_signature"),
        "status": record["status"],
        "owner": record.get("owner", DEFAULT_OWNER),
        "status_reason": (record.get("status_history") or [{}])[-1].get("reason"),
        "status_history": record.get("status_history", []),
        "operator": record.get("operator", {}),
        "recovery_evaluations": record.get("healthy_evaluations", 0),
        "financial_exposure_status": financial_status,
        "financial_exposure_started_at": record.get("financial_exposure_started_at", record.get("created_at")),
        "financial_exposure_ended_at": ended_at,
        "incident_started_at": record.get("created_at"),
        "recovered_at": ended_at,
        "operational_resolved_at": record.get("operational_resolved_at"),
        "time_to_recovery_minutes": _minutes_between(record.get("created_at"), ended_at),
        "time_to_resolution_minutes": _minutes_between(record.get("created_at"), record.get("operational_resolved_at")),
        "incident_duration_minutes": _minutes_between(record.get("created_at"), lifecycle_end),
        "last_observed_loss_rate_per_hour_usd": float(record.get("last_observed_loss_rate_per_hour_usd", last_rate)),
    })
    if financial_status == "ended":
        decorated["current_expected_unrecovered_gmv_per_hour_usd"] = 0.0
    return decorated


def _find_current_record(records: dict[str, dict[str, Any]], signature: str) -> dict[str, Any] | None:
    candidates = [record for record in records.values() if record.get("detection_signature") == signature]
    return max(candidates, key=lambda record: str(record.get("created_at", ""))) if candidates else None


def _record_active(entry: dict[str, Any], record: dict[str, Any], at: datetime, evaluated: bool) -> None:
    record["severity"] = entry.get("severity", record.get("severity", "warn"))
    record["last_entry"] = copy.deepcopy(entry)
    record["last_observed_loss_rate_per_hour_usd"] = float(entry.get("current_expected_unrecovered_gmv_per_hour_usd") or entry.get("cost_per_hour_usd") or 0.0)
    record["healthy_evaluations"] = 0
    record["financial_exposure_status"] = "active"
    record.pop("financial_exposure_ended_at", None)
    if record.get("status") == RECOVERED_STATUS:
        _event(record, "investigating", "The anomaly returned after statistical recovery; investigation reopened.", "system", at)
    elif evaluated and record.get("status") == "detected":
        record["active_evaluations"] = int(record.get("active_evaluations", 1)) + 1
        _event(record, "investigating", "A new evaluation confirms the anomaly; evidence collection and diagnosis are underway.", "system", at)


def reconcile(state: dict[str, Any], active_entries: list[dict[str, Any]], observed_at: datetime, evaluated: bool, healthy_evaluations_required: int = 2) -> list[dict[str, Any]]:
    """Synchronize detector evidence into incident records.

    An absent alert ends *financial exposure* only after healthy windows. The
    observed incident remains visible until a person documents the outcome.
    """
    records: dict[str, dict[str, Any]] = state.setdefault("incidents", {})
    tenant_id = str(state.get("tenant_id") or DEFAULT_TENANT)
    active_ids: set[str] = set()
    materialized: list[dict[str, Any]] = []

    for entry in active_entries:
        signature = _detection_signature(entry)
        record = _find_current_record(records, signature)
        if record is None or record.get("status") == RESOLVED_STATUS:
            record = _new_record(entry, observed_at, tenant_id)
            base_id, suffix = record["incident_id"], 2
            while record["incident_id"] in records:
                record["incident_id"] = f"{base_id}_{suffix}"
                suffix += 1
            records[record["incident_id"]] = record
        else:
            _record_active(entry, record, observed_at, evaluated)
        active_ids.add(record["incident_id"])
        materialized.append(_decorate(entry, record, observed_at))

    if evaluated:
        for incident_id, record in records.items():
            if incident_id in active_ids or record.get("status") not in OPEN_STATUSES:
                continue
            record["healthy_evaluations"] = int(record.get("healthy_evaluations", 0)) + 1
            if record["healthy_evaluations"] >= healthy_evaluations_required:
                record["financial_exposure_status"] = "ended"
                record["financial_exposure_ended_at"] = _now(observed_at)
                _event(record, RECOVERED_STATUS, f"Conversion returned within the expected range for {healthy_evaluations_required} consecutive evaluation windows. Financial exposure is frozen; no mitigation was executed by the system.", "system", observed_at)

    for record in records.values():
        if record.get("status") == RECOVERED_STATUS and (snapshot := record.get("last_entry")):
            materialized.append(_decorate(snapshot, record, observed_at))
    return materialized


def _lookup(state: dict[str, Any], incident_id: str) -> dict[str, Any] | None:
    records = state.get("incidents", {})
    return records.get(incident_id) or _find_current_record(records, canonical_signature(incident_id))


def transition_to_monitoring(state: dict[str, Any], incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = _lookup(state, incident_id)
    if not record:
        raise ValueError("incident not found")
    if record.get("status") not in {"detected", "investigating", "monitoring"}:
        raise ValueError("only an open incident can enter monitoring")
    owner = str(payload.get("owner") or record.get("owner") or DEFAULT_OWNER).strip()
    proposed_action = str(payload.get("proposed_action") or "Continue monitoring the recommendation; no system change was executed.").strip()
    record["owner"] = owner[:120] or DEFAULT_OWNER
    record.setdefault("operator", {}).update({"proposed_action": proposed_action[:1000], "recent_changes_checked": payload.get("recent_changes_checked", []), "provider_ticket": str(payload.get("provider_ticket") or "").strip()[:160] or None})
    _event(record, "monitoring", "Operator reviewed evidence and started a monitored response. " + proposed_action[:500], owner or "operator")
    return record


def resolve_by_operator(state: dict[str, Any], incident_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = _lookup(state, incident_id)
    if not record:
        raise ValueError("incident not found")
    if record.get("status") != RECOVERED_STATUS:
        raise ValueError("wait for verified statistical recovery before resolving and storing the incident")
    required = ("owner", "category", "confirmed_root_cause", "action_taken", "validation_result")
    missing = [field for field in required if not str(payload.get(field) or "").strip()]
    if missing:
        raise ValueError("missing required closure fields: " + ", ".join(missing))
    record["owner"] = str(payload["owner"]).strip()[:120]
    record["operator"] = {**record.get("operator", {}), "category": str(payload["category"]).strip()[:80], "confirmed_root_cause": str(payload["confirmed_root_cause"]).strip()[:1500], "action_taken": str(payload["action_taken"]).strip()[:1500], "validation_result": str(payload["validation_result"]).strip()[:1500], "resolution_note": str(payload.get("resolution_note") or "").strip()[:1500] or None}
    record["operational_resolved_at"] = _now()
    _event(record, RESOLVED_STATUS, "Operator documented the confirmed cause, human decision, and validation result; incident stored in governed knowledge.", record["owner"])
    return record


def record_for_memory(record: dict[str, Any]) -> dict[str, Any]:
    entry = record.get("last_entry", {})
    diagnosis = entry.get("diagnosis", {})
    operator = record.get("operator", {})
    return {
        "incident_id": record.get("incident_id"),
        "root_cause_segment": entry.get("root_cause_segment", diagnosis.get("root_cause_segment", {})),
        "decline_reason": (diagnosis.get("dominant_decline") or {}).get("decline_reason"),
        "observed_at": record.get("created_at"), "financial_exposure_ended_at": record.get("financial_exposure_ended_at"),
        "resolved_at": record.get("operational_resolved_at") or record.get("updated_at"),
        "time_to_recovery_minutes": _minutes_between(record.get("created_at"), record.get("financial_exposure_ended_at")),
        "time_to_resolution_minutes": _minutes_between(record.get("created_at"), record.get("operational_resolved_at")),
        "accumulated_incident_loss_usd": entry.get("accumulated_incident_loss_usd"),
        "cost_per_hour_usd": entry.get("last_observed_loss_rate_per_hour_usd") or entry.get("cost_per_hour_usd"),
        "severity": record.get("severity"), "confidence_pct": entry.get("confidence_pct"), "owner": record.get("owner"),
        "category": operator.get("category"), "confirmed_root_cause": operator.get("confirmed_root_cause"),
        "action_taken": operator.get("action_taken"), "validation_result": operator.get("validation_result"),
        "resolution_note": operator.get("resolution_note") or operator.get("action_taken"),
        "evidence": {"diagnosis": diagnosis, "status_history": record.get("status_history", [])},
    }
