from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from perps_account_supervisor import (
    PerpsAccountSupervisor,
    perps_account_supervisor_freshness,
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
        self.submission_lock_path = Path(self.temp_dir.name) / "perps-submission.lock"
        self.journal = FakeJournal()

    async def test_clean_read_only_preflight_publishes_healthy_status(self):
        preflight = FakePreflight()
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
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

    async def test_account_preflight_runs_inside_the_gateway_exclusive_submission_lock(self):
        lock_observed = []

        class LockCheckingPreflight(FakePreflight):
            async def run(inner_self, *, now=None):
                lock_observed.append(self.submission_lock_path.exists())
                return await super(LockCheckingPreflight, inner_self).run(now=now)

        preflight = LockCheckingPreflight()
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
        )
        await supervisor.run(max_cycles=1)

        self.assertEqual(lock_observed, [True])
        self.assertFalse(self.submission_lock_path.exists())
        self.assertEqual(self.journal.calls, 1)
        self.assertEqual(preflight.calls, 1)

    async def test_account_supervisor_respects_the_gateway_lock_implementation(self):
        from perps_live_execution_gateway import _ExclusiveSubmissionLock

        gateway_lock = _ExclusiveSubmissionLock(self.submission_lock_path)
        self.assertTrue(gateway_lock.acquire())
        preflight = FakePreflight()
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
        )
        try:
            await supervisor.run(max_cycles=1)
            status = read_perps_account_supervisor_status(self.status_path)
            self.assertEqual(status["last_error_code"], "submission_lock_unavailable")
            self.assertFalse(status["recovery_completed"])
            self.assertEqual(self.journal.calls, 0)
            self.assertEqual(preflight.calls, 0)
            self.assertTrue(self.submission_lock_path.exists())
        finally:
            gateway_lock.release()
        self.assertFalse(self.submission_lock_path.exists())

    async def test_existing_or_stale_submission_lock_prevents_preflight_and_recovery(self):
        self.submission_lock_path.write_text(
            json.dumps({"token": "operator-inspection-required"}),
            encoding="utf-8",
        )
        preflight = FakePreflight()
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
        )
        await supervisor.run(max_cycles=1)

        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "degraded")
        self.assertEqual(status["last_error_code"], "submission_lock_unavailable")
        self.assertEqual(status["blocked_reasons"], ["submission_lock_unavailable"])
        self.assertFalse(status["recovery_completed"])
        self.assertEqual(self.journal.calls, 0)
        self.assertEqual(preflight.calls, 0)
        self.assertTrue(self.submission_lock_path.exists())

    async def test_report_cannot_claim_pass_when_critical_account_invariants_fail(self):
        invalid = report()
        invalid["observations"]["unknown_remote_open_orders"] = 1
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=FakePreflight(reports=[invalid]),
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
        )
        await supervisor.run(max_cycles=1)

        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "degraded")
        self.assertFalse(status["read_only_preflight_passed"])
        self.assertEqual(status["blocked_reasons"], ["preflight_pass_invariant_violation"])

    async def test_blocked_account_preflight_is_degraded_but_not_a_worker_crash(self):
        preflight = FakePreflight(reports=[
            report(passed=False, reasons=["unmanaged_or_mismatched_remote_open_orders"])
        ])
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=preflight,
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
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
            submission_lock_path=self.submission_lock_path,
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

    async def test_supervisor_rejects_a_preflight_report_with_execution_permission(self):
        malicious = report()
        malicious["order_placement_authorized"] = True
        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=FakePreflight(reports=[malicious]),
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
        )
        await supervisor.run(max_cycles=1)

        status = read_perps_account_supervisor_status(self.status_path)
        self.assertEqual(status["worker_state"], "degraded")
        self.assertFalse(status["read_only_preflight_passed"])
        self.assertEqual(status["last_error_code"], "account_supervisor_cycle_failed")
        self.assertEqual(status["blocked_reasons"], ["account_supervisor_cycle_unavailable"])

    async def test_preflight_timeout_is_reported_as_degraded(self):
        class HangingPreflight:
            async def run(self, *, now=None):
                await asyncio.Event().wait()

        supervisor = PerpsAccountSupervisor(
            journal=self.journal,
            account_preflight=HangingPreflight(),
            status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
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
            submission_lock_path=self.submission_lock_path,
            now_provider=lambda: NOW,
            max_consecutive_failures=2,
            sleep=fake_sleep,
        )
        result = await supervisor.run()

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

    async def test_stale_or_future_heartbeat_is_never_reported_healthy(self):
        base = {
            "enabled": True,
            "worker_state": "healthy",
            "poll_interval_seconds": 30,
            "last_heartbeat_at": NOW.isoformat(),
        }
        fresh = perps_account_supervisor_freshness(base, now=NOW)
        self.assertEqual(fresh["worker_state"], "healthy")
        self.assertTrue(fresh["heartbeat_fresh"])
        self.assertEqual(fresh["heartbeat_stale_after_seconds"], 90)

        stale = perps_account_supervisor_freshness({
            **base,
            "last_heartbeat_at": (NOW - timedelta(seconds=91)).isoformat(),
        }, now=NOW)
        self.assertEqual(stale["worker_state"], "stale")
        self.assertFalse(stale["heartbeat_fresh"])

        future = perps_account_supervisor_freshness({
            **base,
            "last_heartbeat_at": (NOW + timedelta(seconds=5)).isoformat(),
        }, now=NOW)
        self.assertEqual(future["worker_state"], "invalid")
        self.assertFalse(future["heartbeat_fresh"])

        missing = perps_account_supervisor_freshness({
            **base,
            "last_heartbeat_at": None,
        }, now=NOW)
        self.assertEqual(missing["worker_state"], "unknown")
        self.assertFalse(missing["heartbeat_fresh"])

    async def test_validation_rejects_unsafe_supervisor_configuration(self):
        with self.assertRaisesRegex(ValueError, "poll_interval_seconds"):
            PerpsAccountSupervisor(
                journal=self.journal,
                account_preflight=FakePreflight(),
                status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
                poll_interval_seconds=5,
            )
        with self.assertRaisesRegex(ValueError, "max_consecutive_failures"):
            PerpsAccountSupervisor(
                journal=self.journal,
                account_preflight=FakePreflight(),
                status_path=self.status_path,
            submission_lock_path=self.submission_lock_path,
                max_consecutive_failures=0,
            )


if __name__ == "__main__":
    unittest.main()
