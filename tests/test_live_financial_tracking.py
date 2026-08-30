"""Tests for the rolling-window incidence-cost estimate."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_dashboard


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


if __name__ == "__main__":
    unittest.main()
