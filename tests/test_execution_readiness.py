import os
import unittest
from unittest.mock import patch

from execution_readiness import execution_readiness_payload


class ExecutionReadinessTests(unittest.TestCase):
    def test_live_execution_is_blocked_by_design(self):
        report = execution_readiness_payload()
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["mode"], "paper_only")
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["live_order_submission_implemented"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertFalse(report["live_order_submission_enabled"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertFalse(report["order_signing_enabled"])
        self.assertEqual(report["kill_switch"], "engaged")
        self.assertTrue(report["research_only"])
        self.assertEqual(report["execution"], "none")

    def test_event_gateway_capabilities_are_reported_without_authorizing(self):
        event = execution_readiness_payload()["gateway_inventory"]["event_contracts"]
        self.assertTrue(event["adapter_code_present"])
        self.assertFalse(event["integrated_into_paper_worker"])
        self.assertFalse(event["source_controlled_release_gates_passed"])
        self.assertEqual(event["supported_side"], "BUY")
        self.assertEqual(event["supported_order_type"], "FAK")
        self.assertTrue(event["per_order_geoblock_check"])
        self.assertTrue(event["local_kill_switch_check"])
        self.assertTrue(event["kill_switch_required_twice"])
        self.assertFalse(event["supports_sell"])
        self.assertFalse(event["supports_resting_orders"])
        self.assertFalse(event["runtime_enabled"])
        self.assertFalse(event["order_submission_allowed"])

    def test_perps_gateway_capabilities_and_geoblock_gap_are_explicit(self):
        report = execution_readiness_payload()
        perps = report["gateway_inventory"]["perps"]
        self.assertTrue(perps["adapter_code_present"])
        self.assertFalse(perps["integrated_into_strategy_worker"])
        self.assertEqual(perps["supported_time_in_force"], ["IOC", "FOK"])
        self.assertTrue(perps["per_order_geoblock_check"])
        self.assertFalse(perps["source_controlled_release_gates_passed"])
        self.assertFalse(perps["runtime_enabled"])
        self.assertFalse(report["order_submission_allowed"])

    def test_event_order_management_is_present_but_not_operationally_enabled(self):
        operations = execution_readiness_payload()["event_order_management"]
        self.assertTrue(operations["adapter_code_present"])
        self.assertTrue(operations["supports_open_order_reads"])
        self.assertTrue(operations["supports_cancel_one"])
        self.assertTrue(operations["supports_cancel_all"])
        self.assertTrue(operations["readback_verification"])
        self.assertTrue(operations["shared_submission_cancel_lock"])
        self.assertTrue(operations["requires_explicit_confirmation"])
        self.assertFalse(operations["runtime_enabled"])
        self.assertFalse(operations["integrated_into_operator_service"])
        self.assertFalse(operations["new_order_submission_allowed"])

    def test_event_live_kill_switch_is_mandatory_and_fails_closed(self):
        report = execution_readiness_payload()
        switch = report["event_live_kill_switch"]
        self.assertEqual(switch["status_file_env"], "EVENT_LIVE_KILL_SWITCH_PATH")
        self.assertEqual(switch["freshness_seconds"], 60)
        self.assertTrue(switch["missing_or_invalid_means_engaged"])
        self.assertTrue(switch["checked_on_gateway_entry"])
        self.assertTrue(switch["checked_immediately_before_submission"])
        self.assertFalse(switch["operator_ui_disarm_available"])
        self.assertFalse(switch["order_submission_allowed"])
        codes = {item["code"] for item in report["blockers"]}
        self.assertIn("event_live_kill_switch_not_verified", codes)
        self.assertFalse(report["live_execution_enabled"])

    def test_shared_paper_admission_is_reported_as_not_integrated(self):
        risk = execution_readiness_payload()["shared_paper_risk"]
        self.assertTrue(risk["coordinator_code_present"])
        self.assertTrue(risk["ledger_entry_verification_present"])
        self.assertFalse(risk["wired_into_event_engine"])
        self.assertFalse(risk["wired_into_perps_engine"])
        self.assertFalse(risk["combined_daily_loss_enforced"])
        self.assertFalse(risk["combined_drawdown_enforced"])
        self.assertFalse(risk["order_admission_allowed"])

    def test_platform_policy_requires_independent_age_and_runtime_geoblock_checks(self):
        report = execution_readiness_payload()
        policy = report["platform_eligibility_policy"]
        self.assertEqual(policy["minimum_user_age_years"], 18)
        self.assertEqual(policy["terms_effective_date"], "2026-08-11")
        self.assertTrue(policy["independent_account_verification_required"])
        self.assertFalse(policy["runtime_geoblock_checked_by_this_endpoint"])
        self.assertIn("/api/geoblock", policy["geoblock_endpoint"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertNotIn("self-reported", repr(report))
        self.assertNotIn("age 41", repr(report))

    def test_environment_flags_and_secrets_cannot_enable_or_leak_execution(self):
        with patch.dict(os.environ, {
            "LIVE_TRADING_ENABLED": "true",
            "LIVE_TRADING_ARMED": "true",
            "LIVE_EXECUTION_ENABLED": "1",
            "PERPS_LIVE_EXECUTION_ENABLED": "true",
            "PERPS_LIVE_TRADING_ARMED": "true",
            "POLYMARKET_PRIVATE_KEY": "must-not-be-read-or-returned",
        }):
            report = execution_readiness_payload()
        self.assertFalse(report["live_execution_enabled"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertNotIn("must-not-be-read-or-returned", repr(report))

    def test_every_production_blocker_is_explicit(self):
        codes = {item["code"] for item in execution_readiness_payload()["blockers"]}
        expected = {
            "live_execution_disabled_by_default",
            "event_source_release_gates_closed",
            "event_order_management_not_integrated",
            "event_gateway_not_integrated",
            "perps_gateway_not_integrated",
            "perps_source_release_gates_closed",
            "shared_paper_admission_not_integrated",
            "account_eligibility_not_verified",
            "jurisdiction_and_runtime_geoblock_not_verified",
            "strategy_edge_not_established",
            "account_credential_isolation_not_verified",
            "production_operations_not_verified",
        }
        self.assertEqual(codes, expected)


if __name__ == "__main__":
    unittest.main()
