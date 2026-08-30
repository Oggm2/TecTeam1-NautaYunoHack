"""Tests for the always-visible payment-method impact breakdown."""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import diagnoser
from recovery_estimator import RecoveryEstimator


class PaymentMethodImpactTests(unittest.TestCase):
    def test_breakdown_compares_each_method_and_ranks_incident_impact(self) -> None:
        at = datetime(2026, 8, 29, 12, tzinfo=UTC)

        def event(method: str, status: str) -> dict[str, object]:
            return {
                "provider": "stripe", "country": "MX", "payment_method": method,
                "status": status, "amount_usd": 100.0,
            }

        history = [event("card", "approved") for _ in range(95)] + [event("card", "declined") for _ in range(5)]
        history += [event("wallet", "approved") for _ in range(98)] + [event("wallet", "declined") for _ in range(2)]
        current = [event("card", "approved")] + [event("card", "declined") for _ in range(9)]
        current += [event("wallet", "approved") for _ in range(9)] + [event("wallet", "declined")]

        store = diagnoser.build_baseline([])
        for row in history:
            store.add(row, at)
        args = argparse.Namespace(min_attempts=5, min_history_attempts=20, min_drop_pp=5.0, z_threshold=3.0)
        parent_segment = {"provider": "stripe", "country": "MX"}
        parent_baseline = diagnoser.expected_for_segment(store, parent_segment, at, args.min_history_attempts)
        parent_metrics = diagnoser.loss_metrics(current, parent_baseline, 300, RecoveryEstimator().fit([]))

        impact = diagnoser.payment_method_impact(
            current, parent_segment, parent_metrics, store, at, args, 300, RecoveryEstimator().fit([]),
        )

        self.assertEqual([row["value"] for row in impact], ["card", "wallet"])
        self.assertTrue(impact[0]["is_anomalous"])
        self.assertGreater(impact[0]["impact_share_of_lost_approvals"], impact[1]["impact_share_of_lost_approvals"])


if __name__ == "__main__":
    unittest.main()
