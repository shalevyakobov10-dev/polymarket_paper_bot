from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from perps_auto_cancel_guard import PerpsAutoCancelGuard


NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)


class FakeAutoCancelSession:
    def __init__(self, *, deadline_delta=60, triggered=0, daily_limit=5):
        self.deadline_delta = deadline_delta
        self.triggered = triggered
        self.daily_limit = daily_limit
        self.arm_calls = []
        self.status_calls = 0
        self.arm_error = False
        self.status_error = False
        self.disarm_calls = 0

    async def arm_auto_cancel(self, *, cancel_at):
        self.arm_calls.append(cancel_at)
        if self.arm_error:
            raise RuntimeError("auto-cancel arm failed")

    async def fetch_auto_cancel_status(self):
        self.status_calls += 1
        if self.status_error:
            raise RuntimeError("auto-cancel status failed")
        reference = self.arm_calls[-1] if self.arm_calls else NOW
        return SimpleNamespace(
            deadline=reference + timedelta(seconds=self.deadline_delta - 60),
            triggered=self.triggered,
            daily_limit=self.daily_limit,
        )

    async def disarm_auto_cancel(self):
        self.disarm_calls += 1


class StopImmediatelyEvent:
    def is_set(self):
        return False

    async def wait(self):
        return True


class PerpsAutoCancelGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.kill_file = Path(self.temp_dir.name) / "perps-kill-switch.json"
        self.session = FakeAutoCancelSession()

    def guard(self, **overrides):
        options = {
            "session": self.session,
            "enabled": True,
            "manually_armed": True,
            "kill_switch_path": self.kill_file,
        }
        options.update(overrides)
        return PerpsAutoCancelGuard(**options)

    async def test_default_configuration_is_disabled_and_never_calls_sdk(self):
        guard = PerpsAutoCancelGuard(session=self.session)
        result = await guard.arm_once(now=NOW)
        self.assertEqual(result["state"], "disabled")
        self.assertFalse(result["new_order_permission"])
        self.assertEqual(result["execution"], "none")
        self.assertEqual(self.session.arm_calls, [])
        self.assertEqual(self.session.status_calls, [])

    async def test_enabled_and_manually_armed_switch_is_verified_against_server(self):
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "armed")
        self.assertEqual(result["reason_code"], "server_deadman_switch_verified")
        self.assertEqual(result["seconds_remaining"], 60.0)
        self.assertEqual(self.session.arm_calls, [NOW + timedelta(seconds=60)])
        self.assertEqual(self.session.status_calls, 1)
        self.assertFalse(result["new_order_permission"])
        self.assertFalse(self.kill_file.exists())

    async def test_server_deadline_too_close_writes_atomic_kill_switch(self):
        self.session.deadline_delta = 20
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "auto_cancel_server_deadline_too_close")
        payload = json.loads(self.kill_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["reason_code"], result["reason_code"])
        self.assertEqual(payload["execution"], "blocked")
        self.assertFalse(result["new_order_permission"])

    async def test_server_deadline_mismatch_fails_closed(self):
        self.session.deadline_delta = 40
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "auto_cancel_server_deadline_mismatch")
        self.assertTrue(self.kill_file.exists())

    async def test_daily_trigger_limit_exhaustion_fails_closed(self):
        self.session.triggered = 5
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "auto_cancel_daily_trigger_limit_exhausted_or_unknown",
        )
        self.assertTrue(self.kill_file.exists())

    async def test_unknown_daily_trigger_fields_fail_closed(self):
        self.session.daily_limit = None
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(self.kill_file.exists())

    async def test_arm_failure_writes_kill_switch(self):
        self.session.arm_error = True
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "auto_cancel_arm_or_status_read_failed")
        self.assertTrue(self.kill_file.exists())

    async def test_status_read_failure_writes_kill_switch(self):
        self.session.status_error = True
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "auto_cancel_arm_or_status_read_failed")
        self.assertTrue(self.kill_file.exists())

    async def test_naive_or_invalid_server_deadline_fails_closed(self):
        class InvalidDeadlineSession(FakeAutoCancelSession):
            async def fetch_auto_cancel_status(self):
                return SimpleNamespace(deadline="2026-11-22T08:01:00Z", triggered=0, daily_limit=5)
        session = InvalidDeadlineSession()
        result = await self.guard(session=session).arm_once(now=NOW)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "auto_cancel_server_deadline_invalid")

    async def test_guard_refuses_enabled_configuration_without_manual_arm_and_kill_path(self):
        result = await PerpsAutoCancelGuard(
            session=self.session,
            enabled=True,
            manually_armed=False,
        ).arm_once(now=NOW)
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(self.session.arm_calls, [])

    async def test_guard_rejects_refresh_schedule_without_safety_margin(self):
        with self.assertRaisesRegex(ValueError, "insufficient dead-man"):
            PerpsAutoCancelGuard(
                session=self.session,
                deadline_seconds=60,
                refresh_interval_seconds=35,
                min_server_remaining_seconds=25,
            )

    async def test_shutdown_leaves_server_deadman_armed_and_does_not_disarm(self):
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "stopped_with_server_deadman_armed")
        self.assertIsNotNone(result["last_verified_deadline"])
        self.assertEqual(self.session.disarm_calls, 0)
        self.assertEqual(len(self.session.arm_calls), 1)
        self.assertFalse(result["new_order_permission"])

    async def test_kill_switch_file_is_replaced_without_temporary_file_left_behind(self):
        result = await self.guard().arm_once(now=NOW)
        self.assertEqual(result["state"], "armed")
        self.session.triggered = self.session.daily_limit
        blocked = await self.guard().arm_once(now=NOW)
        self.assertEqual(blocked["state"], "blocked")
        files = list(Path(self.temp_dir.name).iterdir())
        self.assertEqual(files, [self.kill_file])


if __name__ == "__main__":
    unittest.main()
