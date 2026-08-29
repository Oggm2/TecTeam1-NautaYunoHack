"""Estimate whether a declined checkout will recover within a fixed horizon.

The first implementation deliberately uses smoothed historical rates rather
than a black-box model.  It can score a transaction immediately and naturally
falls back to broader segments when an exact combination is sparse.  A future
logistic-regression scorer can implement the same ``probability`` interface.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


RECOVERABLE_FAILURE_STATUSES = {"declined", "failed", "expired"}
DEFAULT_RECOVERY_BY_METHOD = {
    "card": 0.48, "wallet": 0.55, "pix": 0.50, "pse": 0.36, "cash_in_store": 0.12,
}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class RecoveryEstimate:
    probability: float
    source: str
    sample_size: int


class RecoveryEstimator:
    """Hierarchical empirical recovery rates for initial failed checkouts."""

    def __init__(self, horizon_hours: float = 24, prior_strength: float = 12.0) -> None:
        self.horizon = timedelta(hours=horizon_hours)
        self.prior_strength = prior_strength
        self.levels: list[tuple[str, tuple[str, ...]]] = [
            ("method_reason_hour_country", ("payment_method", "decline_reason", "hour", "country")),
            ("method_reason_country", ("payment_method", "decline_reason", "country")),
            ("method_reason", ("payment_method", "decline_reason")),
            ("method", ("payment_method",)),
        ]
        self.counts: dict[str, dict[tuple[str, ...], list[int]]] = {
            name: defaultdict(lambda: [0, 0]) for name, _ in self.levels
        }
        self.global_counts = [0, 0]

    @staticmethod
    def _key(event: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
        completed = parse_timestamp(event.get("completed_at")) or parse_timestamp(event.get("created_at"))
        values: list[str] = []
        for field in fields:
            if field == "hour":
                values.append(str(completed.hour if completed else 0))
            else:
                values.append(str(event.get(field) or "unknown"))
        return tuple(values)

    def fit(self, events: list[dict[str, Any]]) -> "RecoveryEstimator":
        """Learn labels from an initial failure followed by an approval per checkout."""
        checkouts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            checkout_id = event.get("checkout_id")
            if checkout_id:
                checkouts[str(checkout_id)].append(event)

        for attempts in checkouts.values():
            attempts.sort(
                key=lambda event: parse_timestamp(event.get("completed_at"))
                or parse_timestamp(event.get("created_at"))
                or datetime.min.replace(tzinfo=UTC)
            )
            initial = next((event for event in attempts if not event.get("is_retry") and int(event.get("attempt_number", 1)) == 1), None)
            if not initial or initial.get("status") not in RECOVERABLE_FAILURE_STATUSES:
                continue
            initial_at = parse_timestamp(initial.get("completed_at")) or parse_timestamp(initial.get("created_at"))
            if initial_at is None:
                continue
            recovered = any(
                event.get("status") == "approved"
                and (event_at := parse_timestamp(event.get("completed_at")) or parse_timestamp(event.get("created_at")))
                and initial_at < event_at <= initial_at + self.horizon
                for event in attempts
            )
            self.global_counts[0] += 1
            self.global_counts[1] += int(recovered)
            for name, fields in self.levels:
                bucket = self.counts[name][self._key(initial, fields)]
                bucket[0] += 1
                bucket[1] += int(recovered)
        return self

    @staticmethod
    def _smoothed(successes: int, total: int, prior: float, strength: float) -> float:
        return (successes + strength * prior) / (total + strength)

    def probability(self, event: dict[str, Any]) -> RecoveryEstimate:
        method = str(event.get("payment_method") or "unknown")
        default = DEFAULT_RECOVERY_BY_METHOD.get(method, 0.35)
        if not self.global_counts[0]:
            return RecoveryEstimate(default, "default_by_payment_method", 0)

        probability = self._smoothed(self.global_counts[1], self.global_counts[0], default, self.prior_strength)
        source = "global"
        sample_size = self.global_counts[0]
        for name, fields in reversed(self.levels):
            total, successes = self.counts[name].get(self._key(event, fields), [0, 0])
            if total:
                probability = self._smoothed(successes, total, probability, self.prior_strength)
                source = name
                sample_size = total
        return RecoveryEstimate(round(min(0.99, max(0.01, probability)), 4), source, sample_size)
