"""Cost estimation shared by the detection and diagnosis engines.

Cost is always: lost approvals x average ticket of the affected segment,
inside the incident window. Extrapolating to a per-hour figure just rescales
by the window length so incidents of different durations become comparable
for prioritization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CostEstimate:
    attempts: int
    approved: int
    lost_approvals: float
    average_ticket_usd: float
    lost_amount_usd: float
    lost_amount_per_hour_usd: float | None


def estimate(events: list[dict[str, Any]], expected_conversion: float, window_seconds: float | None = None) -> CostEstimate:
    attempts = len(events)
    approved = sum(event.get("status") == "approved" for event in events)
    lost_approvals = max(0.0, attempts * expected_conversion - approved)
    average_ticket_usd = sum(float(event.get("amount_usd", 0)) for event in events) / attempts if attempts else 0.0
    lost_amount_usd = lost_approvals * average_ticket_usd
    lost_amount_per_hour_usd = (lost_amount_usd * 3600 / window_seconds) if window_seconds else None
    return CostEstimate(
        attempts=attempts,
        approved=approved,
        lost_approvals=round(lost_approvals, 2),
        average_ticket_usd=round(average_ticket_usd, 2),
        lost_amount_usd=round(lost_amount_usd, 2),
        lost_amount_per_hour_usd=round(lost_amount_per_hour_usd, 2) if lost_amount_per_hour_usd is not None else None,
    )
