from __future__ import annotations

import unittest

from perps_live_release_gate import (
    CONFIRMATION_PHRASE,
    RELEASE_GATES,
    evaluate_perps_live_release_gate,
)


class PerpsLiveReleaseGateTests(unittest.TestCase):
    def test_defaults_are_closed_and_report_every_unverified_release_gate(self):
        report = evaluate_perps_live_release_gate({})
        self.assertFalse(report["all_release_gates_passed"])
        self.assertFalse(report["runtime_flags_passed"])
        self.assertFalse(report["eligible_for_separate_gateway_review"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertEqual(report["execution"], "none")
        self.assertEqual(
            len(report["blocked_reasons"]),
            len(RELEASE_GATES) + 4,
        )

    def test_runtime_flags_and_confirmation_cannot_override_source_release_gates(self):
        report = evaluate_perps_live_release_gate({
            "DRY_RUN": "false",
            "PERPS_LIVE_EXECUTION_ENABLED": "true",
            "PERPS_LIVE_TRADING_ARMED": "true",
            "PERPS_LIVE_CONFIRMATION": CONFIRMATION_PHRASE,
        })
        self.assertTrue(report["runtime_flags_passed"])
        self.assertFalse(report["all_release_gates_passed"])
        self.assertFalse(report["eligible_for_separate_gateway_review"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertTrue(
            all(
                reason.startswith("release_gate:")
                for reason in report["blocked_reasons"]
            )
        )

    def test_invalid_runtime_booleans_fail_closed(self):
        report = evaluate_perps_live_release_gate({
            "DRY_RUN": "maybe",
            "PERPS_LIVE_EXECUTION_ENABLED": "enabled",
            "PERPS_LIVE_TRADING_ARMED": "1",
            "PERPS_LIVE_CONFIRMATION": CONFIRMATION_PHRASE,
        })
        self.assertFalse(report["runtime_flags_passed"])
        self.assertIn("invalid_runtime_flag:DRY_RUN", report["blocked_reasons"])
        self.assertIn(
            "invalid_runtime_flag:PERPS_LIVE_EXECUTION_ENABLED",
            report["blocked_reasons"],
        )
        self.assertFalse(report["order_submission_allowed"])

    def test_confirmation_is_exact_and_has_no_whitespace_normalization(self):
        values = {
            "DRY_RUN": "false",
            "PERPS_LIVE_EXECUTION_ENABLED": "true",
            "PERPS_LIVE_TRADING_ARMED": "true",
        }
        for confirmation in ("", f" {CONFIRMATION_PHRASE}", f"{CONFIRMATION_PHRASE} "):
            with self.subTest(confirmation=confirmation):
                report = evaluate_perps_live_release_gate({
                    **values,
                    "PERPS_LIVE_CONFIRMATION": confirmation,
                })
                self.assertIn(
                    "perps_live_confirmation_missing_or_invalid",
                    report["blocked_reasons"],
                )
                self.assertFalse(report["order_submission_allowed"])


if __name__ == "__main__":
    unittest.main()
