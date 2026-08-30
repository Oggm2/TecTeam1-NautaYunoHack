"""Guarded, read-only routing recommendations from comparable live traffic.

This module deliberately recommends an *experiment*, never a routing change.
It compares the affected provider with alternatives inside the same merchant,
country and payment-method cohort, stratifying by issuing bank where possible.
Transactional data cannot establish fraud risk, provider cost, capacity or
compliance suitability, so those controls always remain pending human and
external-system approval.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from detector import CONVERSION_STATUSES, parse_timestamp


DEFAULT_POLICY: dict[str, Any] = {
    "comparison_window_minutes": 30,
    "minimum_attempts_per_route": 30,
    "minimum_attempts_per_issuer_stratum": 8,
    "minimum_uplift_pp": 2.0,
    "minimum_z_score": 1.96,
    "max_experiment_traffic_pct": 10,
    "required_guardrails": [
        "Fraud and chargeback risk review for the target route",
        "Provider fee, FX and commercial-cost review",
        "Provider capacity, latency and availability check",
        "Merchant, compliance and payment-method eligibility approval",
    ],
    "stop_conditions": [
        "Stop if fraud or chargeback risk breaches the approved threshold",
        "Stop if incremental provider cost exceeds the approved cap",
        "Stop if approval uplift is not sustained across the agreed evaluation windows",
        "Stop if latency, provider errors or customer-impact signals deteriorate",
    ],
}

COHORT_KEYS = ("merchant", "country", "payment_method")


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_policy(path: str | None = None) -> dict[str, Any]:
    """Load a bounded local policy; malformed values safely fall back."""
    policy = {key: value[:] if isinstance(value, list) else value for key, value in DEFAULT_POLICY.items()}
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                policy.update({key: value for key, value in raw.items() if key in DEFAULT_POLICY})
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    policy["comparison_window_minutes"] = max(1, min(120, int(_number(policy.get("comparison_window_minutes"), 30))))
    policy["minimum_attempts_per_route"] = max(10, int(_number(policy.get("minimum_attempts_per_route"), 30)))
    policy["minimum_attempts_per_issuer_stratum"] = max(3, int(_number(policy.get("minimum_attempts_per_issuer_stratum"), 8)))
    policy["minimum_uplift_pp"] = max(0.1, min(50.0, _number(policy.get("minimum_uplift_pp"), 2.0)))
    policy["minimum_z_score"] = max(0.1, min(6.0, _number(policy.get("minimum_z_score"), 1.96)))
    policy["max_experiment_traffic_pct"] = max(1, min(25, int(_number(policy.get("max_experiment_traffic_pct"), 10))))
    for key in ("required_guardrails", "stop_conditions"):
        if not isinstance(policy.get(key), list) or not all(isinstance(item, str) and item.strip() for item in policy[key]):
            policy[key] = DEFAULT_POLICY[key][:]
    return policy


def _rate(rows: list[dict[str, Any]]) -> float:
    return sum(row.get("status") == "approved" for row in rows) / len(rows) if rows else 0.0


def _provider_name(value: str) -> str:
    return {"stripe": "Stripe", "adyen": "Adyen", "dlocal": "dLocal", "mercadopago": "MercadoPago"}.get(value.casefold(), value)


def _not_recommended(
    *, reason: str, cohort: dict[str, str], source_provider: str | None, policy: dict[str, Any],
    observed_window_minutes: float | None = None, alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "decision": "not_recommended",
        "recommendation_type": "controlled_routing_experiment",
        "action": f"Do not change routing automatically. {reason}",
        "reason": reason,
        "reasons": [reason, "No routing action is executed by SentiPay."],
        "cohort": cohort,
        "source_provider": source_provider,
        "comparison_window_minutes": policy["comparison_window_minutes"],
        "observed_window_minutes": observed_window_minutes,
        "alternatives": alternatives or [],
        "guardrail_status": "not_applicable_until_comparable_evidence_exists",
        "requires_human_approval": True,
        "policy": _policy_summary(policy),
    }


def _policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "minimum_attempts_per_route": policy["minimum_attempts_per_route"],
        "minimum_uplift_pp": policy["minimum_uplift_pp"],
        "minimum_z_score": policy["minimum_z_score"],
        "max_experiment_traffic_pct": policy["max_experiment_traffic_pct"],
        "required_guardrails": policy["required_guardrails"],
        "stop_conditions": policy["stop_conditions"],
    }


def _comparison(
    source: str, candidate: str, cohort_events: list[dict[str, Any]], root_segment: dict[str, str], policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Issuer-stratified route comparison, weighted to affected-route traffic."""
    by_provider_issuer: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in cohort_events:
        provider = str(event.get("provider") or "unknown")
        issuer = str(event.get("issuing_bank") or "unknown")
        by_provider_issuer[(provider, issuer)].append(event)

    issuers = [str(root_segment["issuing_bank"])] if root_segment.get("issuing_bank") else sorted({issuer for provider, issuer in by_provider_issuer if provider == source} & {issuer for provider, issuer in by_provider_issuer if provider == candidate})
    strata: list[dict[str, Any]] = []
    for issuer in issuers:
        source_rows = by_provider_issuer[(source, issuer)]
        candidate_rows = by_provider_issuer[(candidate, issuer)]
        if min(len(source_rows), len(candidate_rows)) < policy["minimum_attempts_per_issuer_stratum"]:
            continue
        strata.append({
            "issuing_bank": issuer,
            "source_attempts": len(source_rows), "candidate_attempts": len(candidate_rows),
            "source_conversion": _rate(source_rows), "candidate_conversion": _rate(candidate_rows),
        })
    if not strata:
        return None

    source_attempts = sum(int(row["source_attempts"]) for row in strata)
    candidate_attempts = sum(int(row["candidate_attempts"]) for row in strata)
    if min(source_attempts, candidate_attempts) < policy["minimum_attempts_per_route"]:
        return None

    source_conversion = sum(float(row["source_conversion"]) * int(row["source_attempts"]) for row in strata) / source_attempts
    # Standardize the candidate to the issuer distribution seen on the affected route.
    candidate_conversion = sum(float(row["candidate_conversion"]) * int(row["source_attempts"]) for row in strata) / source_attempts
    variance = sum(
        (int(row["source_attempts"]) / source_attempts) ** 2
        * (
            float(row["source_conversion"]) * (1 - float(row["source_conversion"])) / int(row["source_attempts"])
            + float(row["candidate_conversion"]) * (1 - float(row["candidate_conversion"])) / int(row["candidate_attempts"])
        )
        for row in strata
    )
    uplift_pp = (candidate_conversion - source_conversion) * 100
    z_score = (candidate_conversion - source_conversion) / math.sqrt(variance) if variance > 0 else 0.0
    return {
        "provider": candidate,
        "provider_display_name": _provider_name(candidate),
        "source_provider": source,
        "source_provider_display_name": _provider_name(source),
        "source_attempts": source_attempts,
        "candidate_attempts": candidate_attempts,
        "source_conversion_pct": round(source_conversion * 100, 2),
        "candidate_conversion_pct": round(candidate_conversion * 100, 2),
        "uplift_pp": round(uplift_pp, 2),
        "z_score": round(z_score, 2),
        "issuer_strata_used": len(strata),
        "comparison_method": "exact_issuer_match" if root_segment.get("issuing_bank") else "issuer_stratified_to_affected_route_mix",
    }


def recommend(
    diagnosis: dict[str, Any], events: list[dict[str, Any]], policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a non-executing recommendation backed by recent route evidence."""
    policy = load_policy() if policy is None else policy
    root_segment = {key: str(value) for key, value in (diagnosis.get("root_cause_segment") or {}).items() if value not in (None, "")}
    source = root_segment.get("provider")
    cohort = {key: root_segment[key] for key in COHORT_KEYS if key in root_segment}
    if not diagnosis.get("evidence_sufficient"):
        return _not_recommended(
            reason="The incident has not reached sufficient diagnostic evidence for a routing experiment.", cohort=cohort,
            source_provider=source, policy=policy,
        )
    missing = [key for key in COHORT_KEYS if key not in cohort]
    if not source or missing:
        return _not_recommended(
            reason="The impact is not yet localized to one provider and an exact merchant × country × payment-method cohort; a route comparison would be confounded.",
            cohort=cohort, source_provider=source, policy=policy,
        )

    ended_at = parse_timestamp((diagnosis.get("incident_window") or {}).get("ended_at"))
    latest_event_at = max(
        (timestamp for event in events if (timestamp := parse_timestamp(event.get("completed_at")))),
        default=None,
    )
    ended_at = ended_at or latest_event_at or datetime.now(UTC)
    cutoff = ended_at - timedelta(minutes=policy["comparison_window_minutes"])
    cohort_events: list[dict[str, Any]] = []
    timestamps: list[datetime] = []
    for event in events:
        completed_at = parse_timestamp(event.get("completed_at"))
        if completed_at is None or not cutoff <= completed_at <= ended_at:
            continue
        if event.get("status") not in CONVERSION_STATUSES:
            continue
        if all(str(event.get(key)) == value for key, value in cohort.items()):
            cohort_events.append(event)
            timestamps.append(completed_at)
    observed_window_minutes = round((ended_at - min(timestamps)).total_seconds() / 60, 1) if timestamps else 0.0
    providers = sorted({str(event.get("provider")) for event in cohort_events if event.get("provider") and str(event.get("provider")) != source})
    if not providers:
        return _not_recommended(
            reason="No alternative provider has comparable completed traffic in the recent cohort.", cohort=cohort,
            source_provider=source, policy=policy, observed_window_minutes=observed_window_minutes,
        )

    alternatives = [comparison for provider in providers if (comparison := _comparison(source, provider, cohort_events, root_segment, policy))]
    alternatives.sort(key=lambda row: (row["uplift_pp"], row["candidate_attempts"]), reverse=True)
    eligible = [
        row for row in alternatives
        if row["uplift_pp"] >= policy["minimum_uplift_pp"] and row["z_score"] >= policy["minimum_z_score"]
    ]
    if not eligible:
        return _not_recommended(
            reason=(
                f"No alternative route exceeded the policy threshold of {policy['minimum_uplift_pp']:.1f} pp "
                f"with z-score {policy['minimum_z_score']:.2f} in issuer-matched traffic."
            ),
            cohort=cohort, source_provider=source, policy=policy, observed_window_minutes=observed_window_minutes,
            alternatives=alternatives,
        )

    best = eligible[0]
    traffic_pct = policy["max_experiment_traffic_pct"]
    cohort_label = " × ".join(f"{key}={value}" for key, value in cohort.items())
    action = (
        f"Recommend a {traffic_pct}% controlled routing experiment from {best['source_provider_display_name']} "
        f"to {best['provider_display_name']} for {cohort_label}, only after fraud, cost, capacity and compliance guardrails are approved."
    )
    comparison_window_label = f"the last {observed_window_minutes:g} minutes of available traffic" if observed_window_minutes < policy["comparison_window_minutes"] else f"the last {policy['comparison_window_minutes']} minutes"
    reasons = [
        f"{best['provider_display_name']} approved {best['uplift_pp']:.1f} pp more issuer-matched comparable traffic than {best['source_provider_display_name']} in {comparison_window_label}.",
        f"Sufficient matched sample: {best['source_attempts']} affected-route attempts and {best['candidate_attempts']} alternative-route attempts across {best['issuer_strata_used']} issuer stratum/strata (z={best['z_score']:.2f}).",
        "This is observational counterfactual evidence, not confirmation that the alternative route will perform identically after traffic is moved.",
        "The system cannot approve or execute routing; a human must approve all guardrails before any experiment.",
    ]
    return {
        "decision": "controlled_experiment_recommended",
        "recommendation_type": "controlled_routing_experiment",
        "action": action,
        "reasons": reasons,
        "cohort": cohort,
        "source_provider": source,
        "candidate_provider": best["provider"],
        "comparison_window_minutes": policy["comparison_window_minutes"],
        "observed_window_minutes": observed_window_minutes,
        "comparison": best,
        "alternatives": alternatives,
        "guardrail_status": "pending_external_fraud_cost_and_compliance_review",
        "requires_human_approval": True,
        "policy": _policy_summary(policy),
    }
