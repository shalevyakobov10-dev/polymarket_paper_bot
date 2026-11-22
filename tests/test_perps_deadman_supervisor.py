from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

from perps_deadman_supervisor import PerpsDeadmanSwitchSupervisor


NOW = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)


class FakeDeadmanSession:
    def __init__(self):
        self.arm_calls = []
        self.fetch_calls = 0
        self.arm_error = None
        self.arm_hangs = False
        self.fetch_error = None
        self.deadline_override = None
        self.triggered = 0
        self.daily_limit = 5

    async def arm_auto_cancel(self, *, cancel_at):
        self.arm_calls.append(cancel_at)
        if self.arm_hangs:
            await asyncio.Event().wait()
        if self.arm_error:
            raise self.arm_error

    async def fetch_auto_cancel_status(self):
        self.fetch_calls += 1
        if self.fetch_error:
            raise self.fetch_error
        return {
            "deadline": self.deadline_override or self.arm_calls[-1],
            "triggered": self.triggered,
            "daily_limit": self.daily_limit,
        }


class PerpsDeadmanSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = FakeDeadmanSession()
        self.stop_event = asyncio.Event()
        self.halt_event = asyncio.Event()
        self.environment = {
            "DRY_RUN": "false",
            "PERPS_LIVE_EXECUTION_ENABLED": "true",
            "PERPS_LIVE_TRADING_ARMED": "true",
            "PERPS_LIVE_CONFIRMATION": "I_ACCEPT_REAL_MONEY_RISK",
        }

    def build(self, **kwargs):
        return PerpsDeadmanSwitchSupervisor(
            session=self.session,
            stop_event=self.stop_event,
            halt_event=self.halt_event,
            environment=self.environment,
            now_fn=lambda: NOW,
            **kwargs,
        )

    async def test_closed_source_release_gate_prevents_any_account_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.build(status_path=Path(directory) / "deadman.json")
            report = await supervisor.run()

            self.assertEqual(report["state"], "blocked")
            self.assertEqual(report["reason_code"], "perps_live_release_gate_closed")
            self.assertTrue(self.halt_event.is_set())
            self.assertEqual(self.session.arm_calls, [])
            self.assertEqual(self.session.fetch_calls, 0)
            self.assertFalse(report["order_submission_allowed"])
            self.assertFalse(report["disarm_on_shutdown"])
            saved = json.loads((Path(directory) / "deadman.json").read_text())
            self.assertEqual(saved["state"], "blocked")

    async def test_stopped_before_start_does_not_arm_auto_cancel(self):
        self.stop_event.set()
        with patch(
            "perps_deadman_supervisor.evaluate_perps_live_release_gate",
            return_value={"eligible_for_separate_gateway_review": True},
        ):
            report = await self.build().run()
        self.assertEqual(report["state"], "stopped")
        self.assertEqual(self.session.arm_calls, [])
        self.assertEqual(self.session.fetch_calls, 0)

    async def test_arm_and_verify_checks_deadline_and_daily_limit(self):
        supervisor = self.build(lease_seconds=60, minimum_remaining_seconds=10)
        report = await supervisor._arm_and_verify_once()
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(len(self.session.arm_calls), 1)
        self.assertEqual(self.session.arm_calls[0], NOW + timedelta(seconds=60))
        self.assertEqual(self.session.fetch_calls, 1)
        self.assertEqual(report["seconds_remaining"], 60.0)
        self.assertFalse(report["order_submission_allowed"])

    async def test_arm_timeout_or_api_failure_fails_closed(self):
        self.session.arm_hangs = True
        supervisor = self.build(command_timeout_seconds=0.05)
        with patch(
            "perps_deadman_supervisor.evaluate_perps_live_release_gate",
            return_value={"eligible_for_separate_gateway_review": True},
        ):
            report = await supervisor.run()
        self.assertEqual(report["state"], "degraded_deadman_left_armed")
        self.assertEqual(report["reason_code"], "deadman_refresh_failed")
        self.assertTrue(self.halt_event.is_set())
        self.assertEqual(self.session.fetch_calls, 0)

    async def test_near_expiry_or_exhausted_daily_limit_is_rejected(self):
        self.session.deadline_override = NOW + timedelta(seconds=4)
        with self.assertRaisesRegex(ValueError, "auto_cancel_deadline_too_close"):
            await self.build()._arm_and_verify_once()

        self.session.deadline_override = None
        self.session.triggered = self.session.daily_limit
        with self.assertRaisesRegex(ValueError, "auto_cancel_daily_limit"):
            await self.build()._arm_and_verify_once()

    async def test_shutdown_leaves_last_deadman_lease_armed(self):
        class StopAfterOneRenewal(asyncio.Event):
            async def wait(self):
                self.set()
                return True

        self.stop_event = StopAfterOneRenewal()
        supervisor = self.build()
        with patch(
            "perps_deadman_supervisor.evaluate_perps_live_release_gate",
            return_value={"eligible_for_separate_gateway_review": True},
        ):
            report = await supervisor.run()
        self.assertEqual(report["state"], "stopped_deadman_left_armed")
        self.assertEqual(report["reason_code"], "shutdown_requested")
        self.assertEqual(len(self.session.arm_calls), 1)
        self.assertEqual(self.session.fetch_calls, 1)
        self.assertEqual(report["seconds_remaining"], 60.0)
        self.assertTrue(self.halt_event.is_set())
        self.assertFalse(report["disarm_on_shutdown"])
        self.assertFalse(hasattr(self.session, "disarm_auto_cancel"))


if __name__ == "__main__":
    unittest.main()
