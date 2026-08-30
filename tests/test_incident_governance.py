"""Regression tests for immutable identity, governed knowledge and exposure state."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import incident_lifecycle
import incident_loss_ledger
import incident_memory
from recovery_estimator import RecoveryEstimator


class IncidentGovernanceTests(unittest.TestCase):
    def _entry(self, incident_key: str = "alerts:one") -> dict:
        start = datetime(2026, 8, 30, 12, tzinfo=UTC)
        return {
            "incident_key": incident_key,
            "detection_signature": "provider=stripe|country=BR",
            "lifecycle_key": "provider=stripe|country=BR",
            "alert_id": "alert-1",
            "priority_rank": 1,
            "severity": "high",
            "cost_per_hour_usd": 3600.0,
            "current_expected_unrecovered_gmv_per_hour_usd": 3600.0,
            "root_cause_segment": {"provider": "stripe", "country": "BR"},
            "diagnosis": {
                "evidence_sufficient": True,
                "incident_window": {"started_at": start.isoformat(), "ended_at": (start + timedelta(minutes=5)).isoformat()},
                "root_metrics": {"lost_approvals": 1},
            },
        }

    def test_incident_id_and_financial_ledger_survive_alert_group_changes(self) -> None:
        at = datetime(2026, 8, 30, 12, tzinfo=UTC)
        state = incident_lifecycle.empty()
        first = incident_lifecycle.reconcile(state, [self._entry("alerts:a")], at, evaluated=True)[0]
        second = incident_lifecycle.reconcile(state, [self._entry("alerts:a|b|c")], at + timedelta(seconds=30), evaluated=True)[0]

        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(first["financial_exposure_id"], second["financial_exposure_id"])

        ledger = incident_loss_ledger.empty()
        event_one = {"transaction_id": "one", "provider": "stripe", "country": "BR", "status": "declined", "amount_usd": 100, "payment_method": "card", "completed_at": (at + timedelta(minutes=1)).isoformat()}
        ledger = incident_loss_ledger.checkpoint(ledger, [first], [event_one], RecoveryEstimator().fit([]), at + timedelta(minutes=2))
        first_total = first["accumulated_incident_loss_usd"]
        event_two = {**event_one, "transaction_id": "two", "completed_at": (at + timedelta(minutes=3)).isoformat()}
        ledger = incident_loss_ledger.checkpoint(ledger, [second], [event_one, event_two], RecoveryEstimator().fit([]), at + timedelta(minutes=4))

        self.assertEqual(list(ledger["active"]), [first["incident_id"]])
        self.assertGreater(second["accumulated_incident_loss_usd"], first_total)

    def test_recovered_incident_keeps_frozen_cost_and_recovery_time(self) -> None:
        at = datetime(2026, 8, 30, 12, tzinfo=UTC)
        state = incident_lifecycle.empty()
        active = incident_lifecycle.reconcile(state, [self._entry()], at, evaluated=True)[0]
        recovered = incident_lifecycle.reconcile(state, [], at + timedelta(seconds=30), evaluated=True)
        self.assertEqual(recovered, [])
        recovered = incident_lifecycle.reconcile(state, [], at + timedelta(seconds=60), evaluated=True)
        self.assertEqual(recovered, [])
        recovered = state["incidents"][active["incident_id"]]

        self.assertEqual(recovered["status"], "recovered_automatically")
        self.assertEqual(recovered["financial_exposure_status"], "ended")
        self.assertEqual(recovered["incident_id"], active["incident_id"])
        memory_record = incident_lifecycle.record_for_memory(recovered)
        self.assertEqual(memory_record["memory_status"], "statistically_recovered")
        self.assertEqual(memory_record["recovered_at"], (at + timedelta(seconds=60)).isoformat())
        self.assertEqual(memory_record["time_to_recovery_minutes"], 1.0)

    def test_recovered_incidents_are_recurrence_memory_with_verification_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.json"
            path.write_text(json.dumps({"incidents": [{"incident_id": "legacy", "resolution_note": None}]}), encoding="utf-8")
            self.assertEqual(incident_memory.load(str(path)), [])
            recovered = {"incident_id": "inc_recovered", "root_cause_segment": {"provider": "stripe"}, "memory_status": "statistically_recovered"}
            incident_memory.save(str(path), [recovered])
            self.assertEqual(incident_memory.load(str(path)), [recovered])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["version"], 3)
            self.assertEqual(stored["incident_memory"][0]["memory_status"], "statistically_recovered")
            self.assertEqual(stored["unverified_observed_incidents"][0]["incident_id"], "legacy")


if __name__ == "__main__":
    unittest.main()
