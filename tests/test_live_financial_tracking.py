"""Tests for live rolling-rate and accumulated-loss tracking."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_dashboard


class LiveFinancialTrackingTests(unittest.TestCase):
    def test_accumulated_loss_integrates_between_live_rates(self) -> None:
        tracker: dict[str, dict[str, object]] = {}
        started_at = datetime(2026, 8, 30, 12, tzinfo=UTC)
        first = [{"incident_key": "provider=stripe ∧ country=BR", "cost_per_hour_usd": 7200.0}]
        build_dashboard.update_accumulated_unrecovered_gmv(first, tracker, started_at)
        self.assertEqual(first[0]["accumulated_expected_unrecovered_gmv_usd"], 0.0)

        later = [{"incident_key": "provider=stripe ∧ country=BR", "cost_per_hour_usd": 3600.0}]
        build_dashboard.update_accumulated_unrecovered_gmv(later, tracker, started_at + timedelta(minutes=10))

        # Trapezoid: average(7200, 3600) USD/hour × 1/6 hour = USD 900.
        self.assertEqual(later[0]["current_expected_unrecovered_gmv_per_hour_usd"], 3600.0)
        self.assertEqual(later[0]["accumulated_expected_unrecovered_gmv_usd"], 900.0)


if __name__ == "__main__":
    unittest.main()
