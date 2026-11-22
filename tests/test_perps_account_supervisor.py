from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from perps_account_supervisor import (
    PerpsAccountSupervisor,
    read_perps_account_supervisor_status,
)


NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)


def report(*, passed=True, reasons=None):
    return {
        "mode": "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT",
        "checked_at": NOW.isoformat(),
        "read_only_preflight_passed": passed,
        "blocked_reasons": list(reasons or []),
        "observations": {
            "portfolio_fresh": True,
            "margin_available": "100",
            "position_count": 1,
            "open_order_count": 1,
            "local_order_count": 1,
            "unresolved_order_count": 0,
            "auto_cancel_armed": True,
            "auto_cancel_seconds_remaining": "45",
            "cross_margin_positions": 0,
            "over_leveraged_positions": 0,
            "position_integrity_errors": 0,
            "high_adl_risk_positions": 0,
            "account_maintenance_margin_ratio": "0.1",
            "unknown_remote_open_orders": 0,
            "missing_remote_open_orders": 0,
            "fee_schedule_fresh": True,
            "account_fee_tier": 0,
            "effective_taker_fee_rate": "0.0005",
            "effective_maker_fee_rate": "0.0002",
            "account_wallet": "must-not-leak",
            "private_key": "must-not-leak",
        },
        "order_placement_authorized": False,
        "live_execution_enabled": False,
        "execution": "none",
        "unexpected": "must-not-publish",
    }


class FakeJournal:
    def __init__(self, recovered=2, error=None):
        self.recovered = recovered
        self.error = error
        self.calls = 0

    def recover_interrupted_submissions(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.recovered


class FakePreflight:
    def __init__(self, reports=None, error=None):
        self.reports = list(reports or [report()])
        self.error = error
        self.calls = 0

    async def run(self, *, now=None):
        self.calls += 1
        if self.error:
            raise self.error
        if len(self.reports) > 1:
            return self.reports.pop(0)
        return self.reports[0]


class ContinueStopEvent:
    def is_set(self):
        return False

    async def wait(self):
        return False


class PerpsAccountSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.status_path = Path(self.temp_dir.name) / "account-supervisor.json"
        self.journal = FakeJournal()

    async def test_clean_read_only_preflight_publishes_healthy_status(self):
        preflight = FakePreflight()
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            now_provider=lambda: NOW,
        )
        result = await supervisor.run(max_cycles=1)

        self.assertEqual(result, 0)
        self.assertEqual(self.journal.calls, 1)
        self.assertEqual(preflight.calls, 1)
        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "healthy")
        self.assertTrue(status["recovery_completed"])
        self.assertEqual(status["recovered_interrupted_submissions"], 2)
        self.assertTrue(status["read_only_preflight_passed"])
        self.assertEqual(status["observations"]["position_count"], 1)
        self.assertEqual(status["observations"]["effective_taker_fee_rate"], "0.0005")
        self.assertNotIn("account_wallet", status["observations"])
        self.assertNotIn("private_key", status["observations"])
        self.assertFalse(status["live_execution_enabled"])
        self.assertFalse(status["order_submission_allowed"])
        self.assertEqual(status["execution"], "none")

    async def test_blocked_account_preflight_is_degraded_but_not_a_worker_crash(self):
        preflight = FakePreflight(reports=[
            report(passed=False, reasons=["unmanaged_or_mismatched_remote_open_orders"])
        ])
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            now_provider=lambda: NOW,
        )
        await supervisor.run(max_cycles=1)

        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "degraded")
        self.assertFalse(status["read_only_preflight_passed"])
        self.assertEqual(
            status["blocked_reasons"],
            ["unmanaged_or_mismatched_remote_open_orders"],
        )
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIsNone(status["last_error_code"])

    async def test_invalid_preflight_contract_fails_closed(self):
        preflight = FakePreflight(reports=[{
            "mode": "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT",
            "read_only_preflight_passed": True,
            "blocked_reasons": [],
            "observations": {"position_count": True},
        }])
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            now_provider=lambda: NOW,
            max_consecutive_failures=1,
        )
        result = await supervisor.run()

        self.assertEqual(result, 1)
        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "degraded")
        self.assertEqual(status["last_error_code"], "account_supervisor_cycle_failed")
        self.assertFalse(status["read_only_preflight_passed"])
        self.assertEqual(status["blocked_reasons"], ["account_supervisor_cycle_unavailable"])

    async def test_preflight_timeout_is_reported_as_degraded(self):
        class HangingPreflight:
            async def run(self, *, now=None):
                await asyncio.Event().wait()

        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=HangingPreflight(),
            status_path=self.status_path,
            now_provider=lambda: NOW,
            cycle_timeout_seconds=0.05,
            max_consecutive_failures=1,
        )
        result = await supervisor.run()

        self.assertEqual(result, 1)
        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["last_error_code"], "account_supervisor_cycle_timeout")
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["worker_state"], "degraded")

    async def test_repeated_preflight_errors_use_bounded_retry_and_startup_recovery_once(self):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        preflight = FakePreflight(error=RuntimeError("secret-bearing upstream error"))
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            now_provider=lambda: NOW,
            max_consecutive_failures=2,
            sleep=fake_sleep,
        )
        result = await supervisor.run(stop_event=ContinueStopEvent())

        self.assertEqual(result, 1)
        self.assertEqual(self.journal.calls, 1)
        self.assertEqual(preflight.calls, 2)
        self.assertEqual(sleeps, [2.0])
        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["consecutive_failures"], 2)
        self.assertEqual(status["last_error_code"], "account_supervisor_cycle_failed")
        serialized = self.status_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-bearing", serialized)

    async def test_malformed_status_file_is_unknown_not_healthy(self):
        self.status_path.write_text(json.dumps({
            "schema_version": 1,
            "mode": "PERPS_ACCOUNT_READ_ONLY_SUPERVISOR",
            "enabled": True,
            "worker_state": "healthy",
            "read_only_preflight_passed": True,
            "blocked_reasons": [],
            "observations": {"position_count": True},
            "poll_interval_seconds": 30,
            "cycle_timeout_seconds": 60,
        }), encoding="utf-8")

        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "unknown")
        self.assertFalse(status["read_only_preflight_passed"])
        self.assertEqual(status["last_error_code"], "status_file_invalid")

    async def test_validation_rejects_unsafe_supervisor_configuration(self):
        with self.assertRaisesRegex(ValueError, "poll_interval_seconds"):
            PerpsAccountSupervisor(
                journal=self.journal,
                account_preflight=FakePreflight(),
                status_path=self.status_path,
                poll_interval_seconds=5,
            )
        with self.assertRaisesRegex(ValueError, "max_consecutive_failures"):
            PerpsAccountSupervisor(
                journal=self.journal,
                account_preflight=FakePreflight(),
                status_path=self.status_path,
                max_consecutive_failures=0,
            )


if __name__ == "__main__":
    unittest.main()
