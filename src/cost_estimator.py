"""Transaction-level gross-loss and expected-recovery cost estimation.

Costs are calculated from the actual values of excess declined transactions;
they never use an average ticket.  The expected unrecovered amount discounts
each attributed decline by its own probability of successful retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from recovery_estimator import RECOVERABLE_FAILURE_STATUSES, RecoveryEstimator


@dataclass(frozen=True)
class CostEstimate:
    attempts: int
    approved: int
    lost_approvals: float
    gross_lost_amount_usd: float
    gross_lost_amount_per_hour_usd: float | None
    expected_recovered_amount_usd: float
    expected_unrecovered_amount_usd: float
    expected_unrecovered_amount_per_hour_usd: float | None
    expected_recovery_rate: float
    recovery_model_source: str
    recovery_model_sample_size: int

    @property
    def lost_amount_usd(self) -> float:
        """Backward-compatible alias for gross lost GMV."""
        return self.gross_lost_amount_usd

    @property
    def lost_amount_per_hour_usd(self) -> float | None:
        """Backward-compatible alias for gross lost GMV per hour."""
        return self.gross_lost_amount_per_hour_usd


def estimate(
    events: list[dict[str, Any]], expected_conversion: float, window_seconds: float | None = None,
    recovery_estimator: RecoveryEstimator | None = None,
) -> CostEstimate:
    attempts = len(events)
    approved = sum(event.get("status") == "approved" for event in events)
    lost_approvals = max(0.0, attempts * expected_conversion - approved)
    failures = [event for event in events if event.get("status") in RECOVERABLE_FAILURE_STATUSES]
    # The detector observes excess declines at segment level.  Weight each
    # declined transaction by the excess-decline share, preserving its actual
    # amount instead of estimating loss from an average ticket.
    attribution = min(1.0, lost_approvals / len(failures)) if failures else 0.0
    gross_lost_amount = 0.0
    expected_unrecovered_amount = 0.0
    weighted_recovery = 0.0
    model_sources: list[str] = []
    model_samples: list[int] = []
    for event in failures:
        amount = max(0.0, float(event.get("amount_usd", 0)))
        estimate_for_event = recovery_estimator.probability(event) if recovery_estimator else None
        recovery_probability = estimate_for_event.probability if estimate_for_event else 0.35
        attributed_amount = amount * attribution
        gross_lost_amount += attributed_amount
        expected_unrecovered_amount += attributed_amount * (1 - recovery_probability)
        weighted_recovery += attributed_amount * recovery_probability
        if estimate_for_event:
            model_sources.append(estimate_for_event.source)
            model_samples.append(estimate_for_event.sample_size)
    expected_recovered_amount = gross_lost_amount - expected_unrecovered_amount
    recovery_rate = weighted_recovery / gross_lost_amount if gross_lost_amount else 0.0
    gross_per_hour = (gross_lost_amount * 3600 / window_seconds) if window_seconds else None
    unrecovered_per_hour = (expected_unrecovered_amount * 3600 / window_seconds) if window_seconds else None
    return CostEstimate(
        attempts=attempts,
        approved=approved,
        lost_approvals=round(lost_approvals, 2),
        gross_lost_amount_usd=round(gross_lost_amount, 2),
        gross_lost_amount_per_hour_usd=round(gross_per_hour, 2) if gross_per_hour is not None else None,
        expected_recovered_amount_usd=round(expected_recovered_amount, 2),
        expected_unrecovered_amount_usd=round(expected_unrecovered_amount, 2),
        expected_unrecovered_amount_per_hour_usd=round(unrecovered_per_hour, 2) if unrecovered_per_hour is not None else None,
        expected_recovery_rate=round(recovery_rate, 4),
        recovery_model_source=max(set(model_sources), key=model_sources.count) if model_sources else "default_by_payment_method",
        recovery_model_sample_size=max(model_samples, default=0),
    )
