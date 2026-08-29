"""Detect sustained, statistically significant conversion drops from JSONL input.

Example:
  py src/generator.py --events-per-second 50 --injections examples/injections.json |
  py src/detector.py --history data/history.jsonl --window-seconds 120 --evaluation-seconds 30
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cost_estimator
from baseline import BaselineStore
from recovery_estimator import RecoveryEstimator


SEGMENT_DEFINITIONS = (
    ("provider", "country"),
    ("merchant", "country"),
    ("issuing_bank", "country"),
    ("payment_method", "country"),
)
CONVERSION_STATUSES = {"approved", "declined", "failed", "expired"}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def load_history(path: str, segments: tuple[tuple[str, ...], ...]) -> BaselineStore:
    store = BaselineStore(segments)
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            completed_at = parse_timestamp(event.get("completed_at"))
            if completed_at is None:
                continue
            store.add(event, completed_at)
    return store


def load_recovery_estimator(path: str, horizon_hours: float = 24) -> RecoveryEstimator:
    events: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                events.append(json.loads(line))
    return RecoveryEstimator(horizon_hours=horizon_hours).fit(events)


class DetectionEngine:
    def __init__(self, baseline: BaselineStore, args: argparse.Namespace, recovery_estimator: RecoveryEstimator | None = None) -> None:
        self.baseline = baseline
        self.args = args
        self.recovery_estimator = recovery_estimator
        self.events: deque[tuple[datetime, dict[str, Any]]] = deque()
        self.seen_ids: set[str] = set()
        self.flags: dict[str, deque[bool]] = defaultdict(lambda: deque(maxlen=args.persistence))
        self.active: set[str] = set()

    def add(self, event: dict[str, Any]) -> None:
        transaction_id = event.get("transaction_id")
        if transaction_id and transaction_id in self.seen_ids:
            return
        if event.get("status") not in CONVERSION_STATUSES:
            return
        completed_at = parse_timestamp(event.get("completed_at"))
        if completed_at is None:
            return
        if transaction_id:
            self.seen_ids.add(transaction_id)
        self.events.append((completed_at, event))

    def evaluate(self, now: datetime) -> list[dict[str, Any]]:
        cutoff = now - timedelta(seconds=self.args.window_seconds)
        while self.events and self.events[0][0] < cutoff:
            _, expired = self.events.popleft()
            transaction_id = expired.get("transaction_id")
            if transaction_id:
                self.seen_ids.discard(transaction_id)

        grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
        for _, event in self.events:
            for dimensions in SEGMENT_DEFINITIONS:
                values = tuple(str(event.get(dimension, "unknown")) for dimension in dimensions)
                grouped[(dimensions, values)].append(event)

        alerts: list[dict[str, Any]] = []
        evaluated_signatures: set[str] = set()
        for (dimensions, values), events in grouped.items():
            segment = dict(zip(dimensions, values, strict=True))
            signature = "|".join(f"{key}={value}" for key, value in segment.items())
            evaluated_signatures.add(signature)
            attempts = len(events)
            approved = sum(event["status"] == "approved" for event in events)
            observed = approved / attempts
            query = {"detection_dimensions": dimensions, "segment": segment}
            baseline = self.baseline.expected_for(query, now, self.args.min_history_attempts)
            anomalous = False
            evidence: dict[str, Any] | None = None
            if baseline and attempts >= self.args.min_attempts:
                standard_error = math.sqrt(baseline.expected_conversion * (1 - baseline.expected_conversion) / attempts)
                z_score = (observed - baseline.expected_conversion) / standard_error if standard_error else 0.0
                drop_pp = (baseline.expected_conversion - observed) * 100
                anomalous = drop_pp >= self.args.min_drop_pp and z_score <= -self.args.z_threshold
                cost = cost_estimator.estimate(events, baseline.expected_conversion, self.args.window_seconds, self.recovery_estimator)
                evidence = {
                    "detection_dimensions": list(dimensions), "segment": segment,
                    "window_started_at": cutoff.isoformat(), "window_ended_at": now.isoformat(),
                    "attempts": attempts, "approved": approved,
                    "observed_conversion": round(observed, 4),
                    "expected_conversion": round(baseline.expected_conversion, 4),
                    "conversion_drop_pp": round(drop_pp, 2), "z_score": round(z_score, 2),
                    "baseline_attempts": baseline.attempts, "baseline_source": baseline.source,
                    "estimated_lost_approvals": cost.lost_approvals,
                    "gross_lost_amount_usd": cost.gross_lost_amount_usd,
                    "gross_lost_amount_per_hour_usd": cost.gross_lost_amount_per_hour_usd,
                    "expected_unrecovered_amount_usd": cost.expected_unrecovered_amount_usd,
                    "expected_unrecovered_amount_per_hour_usd": cost.expected_unrecovered_amount_per_hour_usd,
                    "expected_recovery_rate": cost.expected_recovery_rate,
                    "recovery_model_source": cost.recovery_model_source,
                    "recovery_model_sample_size": cost.recovery_model_sample_size,
                }
            self.flags[signature].append(anomalous)
            if anomalous and len(self.flags[signature]) == self.args.persistence and all(self.flags[signature]) and signature not in self.active:
                self.active.add(signature)
                alerts.append({
                    "alert_id": str(uuid.uuid4()), "type": "conversion_drop",
                    "detected_at": now.isoformat(), "persistence_evaluations": self.args.persistence,
                    "confidence": "high" if evidence and evidence["z_score"] <= -4 else "medium",
                    "evidence": evidence,
                })
            elif not anomalous:
                self.active.discard(signature)

        # Segments no longer present in the window lose their alert state.
        for signature in set(self.flags) - evaluated_signatures:
            self.flags[signature].append(False)
            self.active.discard(signature)
        return alerts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect conversion drops from transaction JSON Lines on stdin.")
    parser.add_argument("--history", required=True, help="Path to normal historical JSONL data.")
    parser.add_argument("--window-seconds", type=int, default=300, help="Rolling conversion window (default: 300).")
    parser.add_argument("--evaluation-seconds", type=int, default=60, help="How often to evaluate (default: 60).")
    parser.add_argument("--persistence", type=int, default=3, help="Consecutive anomalous evaluations required (default: 3).")
    parser.add_argument("--min-attempts", type=int, default=30)
    parser.add_argument("--min-history-attempts", type=int, default=200)
    parser.add_argument("--min-drop-pp", type=float, default=5.0)
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--recovery-horizon-hours", type=float, default=24.0)
    parser.add_argument("--verbose", action="store_true", help="Print evaluation progress to stderr.")
    args = parser.parse_args()
    if min(args.window_seconds, args.evaluation_seconds, args.persistence, args.min_attempts, args.min_history_attempts, args.recovery_horizon_hours) <= 0:
        parser.error("All integer thresholds must be positive.")
    return args


def main() -> None:
    args = parse_args()
    engine = DetectionEngine(
        load_history(args.history, SEGMENT_DEFINITIONS), args,
        load_recovery_estimator(args.history, args.recovery_horizon_hours),
    )
    last_evaluation = time.monotonic()
    for line in sys.stdin:
        if not line.strip():
            continue
        engine.add(json.loads(line))
        if time.monotonic() - last_evaluation >= args.evaluation_seconds:
            now = datetime.now(UTC)
            alerts = engine.evaluate(now)
            if args.verbose:
                print(
                    f"[{now.isoformat()}] evaluated {len(engine.events)} terminal transactions; "
                    f"new alerts: {len(alerts)}",
                    file=sys.stderr,
                    flush=True,
                )
            for alert in alerts:
                print(json.dumps(alert, separators=(",", ":")), flush=True)
            last_evaluation = time.monotonic()


if __name__ == "__main__":
    main()
