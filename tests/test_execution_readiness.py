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

    def test_platform_age_policy_is_explicit_and_cannot_authorize_execution(self):
        report = execution_readiness_payload()
        policy = report["platform_eligibility_policy"]
        self.assertEqual(policy["minimum_user_age_years"], 18)
        self.assertEqual(policy["terms_effective_date"], "2026-08-11")
        self.assertTrue(policy["independent_verification_required"])
        self.assertIn("polymarket.com", policy["source_url"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertFalse(report["order_submission_allowed"])
        eligibility = next(
            item for item in report["blockers"]
            if item["code"] == "account_eligibility_not_verified"
        )
        self.assertIn("18 or older", eligibility["detail"])
        self.assertTrue(any(
            "at least 18 years old" in item
            for item in report["requirements_before_any_separate_live_launch_review"]
        ))

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
        self.assertIn("venue_order_watchdog_not_verified", codes)
        watchdog_blocker = next(
            item for item in execution_readiness_payload()["blockers"]
            if item["code"] == "venue_order_watchdog_not_verified"
        )
        self.assertIn("heartbeat/dead-man-switch", watchdog_blocker["detail"])
        adapter_blocker = next(
            item for item in execution_readiness_payload()["blockers"]
            if item["code"] == "live_order_adapter_not_implemented"
        )
        self.assertIn("read open orders and user events", adapter_blocker["detail"])
        self.assertIn("no reviewed order-placement adapter", adapter_blocker["detail"])
        self.assertIn("account_eligibility_not_verified", codes)
        self.assertIn("jurisdiction_and_legal_review_not_verified", codes)
        self.assertIn("strategy_edge_not_established", codes)
        self.assertIn("production_operations_not_verified", codes)


if __name__ == "__main__":
    unittest.main()
