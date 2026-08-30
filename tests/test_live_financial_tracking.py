"""Tests for the rolling-window incidence-cost estimate."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_dashboard
import incident_loss_ledger
from recovery_estimator import RecoveryEstimator


class LiveFinancialTrackingTests(unittest.TestCase):
    def test_window_cost_uses_the_diagnosis_window_not_elapsed_runtime(self) -> None:
        started_at = datetime(2026, 8, 30, 12, tzinfo=UTC)
        diagnosis = {
            "incident_id": "incident-1", "alert_id": "alert-1", "root_cause_segment": {"provider": "stripe"},
            "incident_window": {"started_at": started_at.isoformat(), "ended_at": (started_at + timedelta(minutes=5)).isoformat()},
            "evidence_sufficient": True, "confidence": "high", "root_metrics": {},
        }
        prioritized = [build_dashboard.prioritizer.PrioritizedIncident(
            incident_key="alerts:alert-1", latest_diagnosis=diagnosis, readings=1,
            cost_per_hour_usd=3600.0, urgency=0.0, urgency_basis="test", priority_score=3600.0, priority_factors={},
        )]

        entry = build_dashboard.build_incident_entries(prioritized, [])[0]

        # USD 3,600/hour × 5 minutes = USD 300 for this rolling window.
        self.assertEqual(entry["window_expected_unrecovered_gmv_usd"], 300.0)
        self.assertEqual(entry["duration_minutes"], 5.0)

    def test_processing_time_uses_a_closed_three_minute_bucket(self) -> None:
        base = datetime(2026, 8, 30, 12, 3, tzinfo=UTC)
        events = [
            {"status": "approved", "completed_at": (base - timedelta(minutes=2)).isoformat(), "processing_time_ms": 100},
            {"status": "approved", "completed_at": (base - timedelta(minutes=1)).isoformat(), "processing_time_ms": 300},
            # This event belongs to the new bucket and must not affect the
            # stable value until the next three-minute update.
            {"status": "approved", "completed_at": (base + timedelta(seconds=10)).isoformat(), "processing_time_ms": 900},
        ]

        stats = build_dashboard.build_dataset_stats(events, processing_window_seconds=180)

        self.assertEqual(stats["average_processing_time_ms"], 200.0)
        self.assertEqual(stats["processing_time_samples"], 2)
        self.assertEqual(stats["processing_time_window_seconds"], 180)

    def test_loss_ledger_counts_a_decline_once_and_freezes_on_resolution(self) -> None:
        started_at = datetime(2026, 8, 30, 12, tzinfo=UTC)
        entry = {
            "incident_key": "alerts:one", "priority_rank": 1, "status": "active",
            "root_cause_segment": {"provider": "stripe", "country": "BR"},
            "diagnosis": {
                "evidence_sufficient": True,
                "incident_window": {"started_at": started_at.isoformat(), "ended_at": (started_at + timedelta(minutes=5)).isoformat()},
                "root_metrics": {"lost_approvals": 1},
            },
        }
        event = {
            "transaction_id": "tx-1", "provider": "stripe", "country": "BR", "status": "declined",
            "amount_usd": 100, "payment_method": "card", "completed_at": (started_at + timedelta(minutes=1)).isoformat(),
        }
        ledger = incident_loss_ledger.checkpoint(incident_loss_ledger.empty(), [entry], [event], RecoveryEstimator().fit([]), started_at + timedelta(minutes=2))
        first_total = entry["accumulated_incident_loss_usd"]
        ledger = incident_loss_ledger.checkpoint(ledger, [entry], [event], RecoveryEstimator().fit([]), started_at + timedelta(minutes=3))

        self.assertGreater(first_total, 0)
        self.assertEqual(entry["accumulated_incident_loss_usd"], first_total)
        ledger = incident_loss_ledger.checkpoint(ledger, [], [event], RecoveryEstimator().fit([]), started_at + timedelta(minutes=4))
        self.assertEqual(ledger["resolved"]["alerts:one"]["accumulated_loss_usd"], first_total)


if __name__ == "__main__":
    unittest.main()
