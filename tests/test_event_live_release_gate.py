from __future__ import annotations

import unittest

from event_live_release_gate import RELEASE_GATES, evaluate_event_live_release_gate


class EventLiveReleaseGateTests(unittest.TestCase):
    def test_all_source_controlled_event_release_gates_start_closed(self):
        report = evaluate_event_live_release_gate()
        self.assertFalse(report["all_release_gates_passed"])
        self.assertFalse(report["eligible_for_separate_gateway_review"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertFalse(report["order_signing_allowed"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertEqual(report["execution"], "none")
        self.assertEqual(len(report["blocked_reasons"]), len(RELEASE_GATES))
        self.assertTrue(all(
            gate["source_controlled"]
            for gate in report["release_gates"].values()
        ))
        self.assertTrue(all(
            not gate["passed"] for gate in report["release_gates"].values()
        ))

    def test_portfolio_admission_and_account_review_are_explicit_blockers(self):
        report = evaluate_event_live_release_gate()
        self.assertIn(
            "release_gate:shared_portfolio_admission_and_kill_switch_verified",
            report["blocked_reasons"],
        )
        self.assertIn(
            "release_gate:account_eligibility_and_kyc_verified",
            report["blocked_reasons"],
        )
        self.assertFalse(report["order_submission_allowed"])


if __name__ == "__main__":
    unittest.main()
