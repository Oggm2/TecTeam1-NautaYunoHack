"""Historical conversion baselines with time-aware fallbacks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Baseline:
    expected_conversion: float
    attempts: int
    source: str


class BaselineStore:
    """Stores approval rates for a segment at the hour/day and all-time levels."""

    def __init__(self, segment_definitions: tuple[tuple[str, ...], ...]) -> None:
        self.segment_definitions = segment_definitions
        self._by_time: dict[tuple[tuple[str, ...], int, int, tuple[str, ...]], list[int]] = defaultdict(lambda: [0, 0])
        self._by_segment: dict[tuple[tuple[str, ...], tuple[str, ...]], list[int]] = defaultdict(lambda: [0, 0])
        self._by_country_time: dict[tuple[int, int, str], list[int]] = defaultdict(lambda: [0, 0])
        self._by_country: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    @staticmethod
    def is_conversion_status(event: dict[str, Any]) -> bool:
        return event.get("status") in {"approved", "declined", "failed", "expired"}

    @staticmethod
    def _add(bucket: list[int], approved: bool) -> None:
        bucket[0] += 1
        bucket[1] += int(approved)

    @staticmethod
    def _segment_values(event: dict[str, Any], dimensions: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(event.get(dimension, "unknown")) for dimension in dimensions)

    def add(self, event: dict[str, Any], completed_at: datetime) -> None:
        if not self.is_conversion_status(event):
            return
        approved = event["status"] == "approved"
        weekday, hour = completed_at.weekday(), completed_at.hour
        country = str(event.get("country", "unknown"))
        self._add(self._by_country_time[(weekday, hour, country)], approved)
        self._add(self._by_country[country], approved)
        for dimensions in self.segment_definitions:
            values = self._segment_values(event, dimensions)
            self._add(self._by_time[(dimensions, weekday, hour, values)], approved)
            self._add(self._by_segment[(dimensions, values)], approved)

    @staticmethod
    def _baseline(bucket: list[int] | None, source: str, minimum_history_attempts: int) -> Baseline | None:
        if bucket is None or bucket[0] < minimum_history_attempts:
            return None
        attempts, approved = bucket
        # Laplace smoothing prevents a historical 0% or 100% rate from being absolute.
        return Baseline(expected_conversion=(approved + 1) / (attempts + 2), attempts=attempts, source=source)

    def expected_for(self, event: dict[str, Any], completed_at: datetime, minimum_history_attempts: int) -> Baseline | None:
        """Find the most specific baseline with enough comparable history."""
        dimensions = tuple(event["detection_dimensions"])
        values = tuple(event["segment"][dimension] for dimension in dimensions)
        weekday, hour = completed_at.weekday(), completed_at.hour
        country = str(event["segment"].get("country", "unknown"))
        candidates = (
            (self._by_time.get((dimensions, weekday, hour, values)), "same_weekday_hour_segment"),
            (self._by_segment.get((dimensions, values)), "all_time_segment"),
            (self._by_country_time.get((weekday, hour, country)), "same_weekday_hour_country"),
            (self._by_country.get(country), "all_time_country"),
        )
        for bucket, source in candidates:
            result = self._baseline(bucket, source, minimum_history_attempts)
            if result:
                return result
        return None
