import os
import unittest
from unittest.mock import patch

from execution_readiness import execution_readiness_payload


class ExecutionReadinessTests(unittest.TestCase):
    def test_live_execution_is_blocked_by_design(self):
        report = execution_readiness_payload()
        self.assertEqual(report["mode"], "paper_only")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["live_order_submission_implemented"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertFalse(report["order_signing_enabled"])
        self.assertEqual(report["kill_switch"], "engaged")
        self.assertTrue(report["research_only"])
        self.assertEqual(report["execution"], "none")

    def test_environment_flags_cannot_enable_live_execution(self):
        with patch.dict(os.environ, {
            "LIVE_TRADING_ENABLED": "true",
            "LIVE_EXECUTION_ENABLED": "1",
            "POLYMARKET_PRIVATE_KEY": "must-not-be-read-or-returned",
        }):
            report = execution_readiness_payload()
        self.assertFalse(report["live_execution_enabled"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertNotIn("must-not-be-read-or-returned", repr(report))

    def test_blockers_include_nontechnical_requirements(self):
        codes = {item["code"] for item in execution_readiness_payload()["blockers"]}
        self.assertIn("live_order_adapter_not_implemented", codes)
        self.assertIn("account_eligibility_not_verified", codes)
        self.assertIn("jurisdiction_and_legal_review_not_verified", codes)
        self.assertIn("strategy_edge_not_established", codes)
        self.assertIn("production_operations_not_verified", codes)


if __name__ == "__main__":
    unittest.main()
