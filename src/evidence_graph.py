"""Temporal evidence graph for diagnosis confidence and causal language.

The graph distinguishes what the payment data proves (localized impact) from
what it merely suggests (likely source) and what an operator or external
system has confirmed (confirmed cause).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from detector import parse_timestamp


def load_operational_events(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return []
    try:
        content = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return content if isinstance(content, list) else []


def _matches(segment: dict[str, Any], event: dict[str, Any]) -> bool:
    filters = event.get("filters", {})
    return isinstance(filters, dict) and all(str(segment.get(key)) == str(value) for key, value in filters.items())


def _likely_source(diagnosis: dict[str, Any]) -> dict[str, str] | None:
    segment = diagnosis.get("root_cause_segment", {})
    decline = (diagnosis.get("dominant_decline") or {}).get("decline_reason")
    if decline == "issuer_unavailable":
        return {"title": "Likely issuer-side availability issue", "detail": "Issuer-unavailable declines are concentrated in the affected segment. This remains a hypothesis until bank status or support confirms it."}
    if segment.get("provider"):
        return {"title": f"Likely provider or route issue: {segment['provider']}", "detail": "The impact is concentrated on one provider path. Compare provider status, routing changes, latency, and an alternative route before attributing cause."}
    if decline in {"transaction_not_permitted", "do_not_honor"}:
        return {"title": "Likely authorization-policy or risk signal", "detail": "The decline pattern can be caused by issuer policy, 3DS, fraud controls, or routing. Payment data alone cannot confirm which."}
    return None


def build(diagnosis: dict[str, Any], lifecycle: dict[str, Any], observed_at: datetime, operational_events: list[dict[str, Any]]) -> dict[str, Any]:
    segment = diagnosis.get("root_cause_segment", {})
    window = diagnosis.get("incident_window", {})
    started = parse_timestamp(window.get("started_at")) or observed_at
    detected = parse_timestamp(lifecycle.get("created_at")) or observed_at
    nodes: list[dict[str, Any]] = []
    lower, upper = started - timedelta(minutes=30), detected + timedelta(minutes=10)
    for event in operational_events:
        timestamp = parse_timestamp(event.get("at"))
        if not timestamp or not lower <= timestamp <= upper or not _matches(segment, event):
            continue
        nodes.append({
            "at": timestamp.isoformat(), "kind": "external_context", "certainty": event.get("verification", "observed"),
            "title": str(event.get("title") or event.get("source") or "Operational signal"),
            "detail": str(event.get("detail") or "Operational context attached to this segment."),
            "source": str(event.get("source") or "operational event"),
        })
    decline = diagnosis.get("dominant_decline") or {}
    if decline:
        nodes.append({
            "at": started.isoformat(), "kind": "payment_evidence", "certainty": "observed",
            "title": f"Decline signal rises: {decline.get('decline_reason', 'unknown')}",
            "detail": f"{round(float(decline.get('share_of_excess_declines', 0)) * 100)}% of excess declines in the localized segment.",
            "source": "payment stream",
        })
    nodes.append({
        "at": started.isoformat(), "kind": "impact_localized", "certainty": "observed",
        "title": "Impact localized", "detail": "Approval loss concentrates in " + (" × ".join(f"{key}={value}" for key, value in segment.items()) or "a broad segment") + ".",
        "source": "statistical diagnosis",
    })
    nodes.append({
        "at": detected.isoformat(), "kind": "detected", "certainty": "observed",
        "title": "Sustained anomaly detected", "detail": "Configured volume, statistical significance, and persistence criteria were met.",
        "source": "detector",
    })
    likely = _likely_source(diagnosis)
    if likely:
        nodes.append({"at": detected.isoformat(), "kind": "likely_source", "certainty": "hypothesis", "source": "diagnostic inference", **likely})
    operator = lifecycle.get("operator", {})
    if operator.get("confirmed_root_cause"):
        nodes.append({
            "at": lifecycle.get("operational_resolved_at") or lifecycle.get("updated_at"), "kind": "cause_confirmed", "certainty": "confirmed",
            "title": "Cause confirmed by operator", "detail": str(operator["confirmed_root_cause"]), "source": "operator evidence",
        })
    if lifecycle.get("financial_exposure_ended_at"):
        nodes.append({
            "at": lifecycle["financial_exposure_ended_at"], "kind": "financial_exposure_ended", "certainty": "observed",
            "title": "Financial exposure ended", "detail": "Conversion recovered for the configured healthy evaluation windows; operational closure is still separate.", "source": "detector",
        })
    nodes.sort(key=lambda node: node.get("at") or "")
    level = "cause_confirmed" if operator.get("confirmed_root_cause") else ("likely_source" if likely else "impact_localized")
    return {"conclusion_level": level, "nodes": nodes, "external_context_found": any(node["kind"] == "external_context" for node in nodes)}
