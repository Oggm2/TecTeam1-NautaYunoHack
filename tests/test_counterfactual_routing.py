"""Safety tests for observational, non-executing route recommendations."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import counterfactual_routing


class CounterfactualRoutingTests(unittest.TestCase):
    def _events(self, provider: str, approved: int, attempts: int, issuer: str = "Itau") -> list[dict]:
        end = datetime(2026, 8, 30, 12, tzinfo=UTC)
        return [
            {
                "transaction_id": f"{provider}-{issuer}-{index}",
                "merchant": "PagoModa", "country": "BR", "payment_method": "pix",
                "provider": provider, "issuing_bank": issuer,
                "status": "approved" if index < approved else "declined",
                "completed_at": (end - timedelta(minutes=10) + timedelta(seconds=index)).isoformat(),
            }
            for index in range(attempts)
        ]

    @staticmethod
    def _diagnosis() -> dict:
        return {
            "evidence_sufficient": True,
            "root_cause_segment": {
                "merchant": "PagoModa", "provider": "stripe", "country": "BR", "payment_method": "pix",
            },
            "incident_window": {"ended_at": datetime(2026, 8, 30, 12, tzinfo=UTC).isoformat()},
        }

    def test_recommends_only_a_capped_human_approved_experiment(self) -> None:
        evidence = self._events("stripe", approved=65, attempts=100) + self._events("adyen", approved=85, attempts=100)
        recommendation = counterfactual_routing.recommend(self._diagnosis(), evidence)

        self.assertEqual(recommendation["decision"], "controlled_experiment_recommended")
        self.assertEqual(recommendation["candidate_provider"], "adyen")
        self.assertEqual(recommendation["comparison"]["uplift_pp"], 20.0)
        self.assertIn("10% controlled routing experiment", recommendation["action"])
        self.assertTrue(recommendation["requires_human_approval"])
        self.assertEqual(recommendation["guardrail_status"], "pending_external_fraud_cost_and_compliance_review")
        self.assertIn("Fraud and chargeback risk review", recommendation["policy"]["required_guardrails"][0])

    def test_refuses_unmatched_issuer_traffic_as_counterfactual_evidence(self) -> None:
        evidence = self._events("stripe", approved=65, attempts=100, issuer="Itau") + self._events("adyen", approved=95, attempts=100, issuer="Nubank")
        recommendation = counterfactual_routing.recommend(self._diagnosis(), evidence)

        self.assertEqual(recommendation["decision"], "not_recommended")
        self.assertNotIn("controlled routing experiment from", recommendation["action"])
        self.assertIn("No alternative route exceeded", recommendation["reason"])

    def test_refuses_when_incident_is_not_specific_to_a_routable_cohort(self) -> None:
        diagnosis = self._diagnosis()
        diagnosis["root_cause_segment"].pop("payment_method")
        recommendation = counterfactual_routing.recommend(diagnosis, self._events("stripe", 65, 100))

        self.assertEqual(recommendation["decision"], "not_recommended")
        self.assertIn("not yet localized", recommendation["reason"])


if __name__ == "__main__":
    unittest.main()
