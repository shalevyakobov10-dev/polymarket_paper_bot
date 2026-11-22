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
        self.cancel_all_calls = 0
        self.open_orders_calls = 0
        self.open_orders = []
        self.positions = []
        self.cancel_error = False
        self.open_orders_error = False
        self.disarm_error = False
        self.keep_orders_open = False
        self.disarm_stays_armed = False
        self.disarmed = False

    async def arm_auto_cancel(self, *, cancel_at):
        self.arm_calls.append(cancel_at)
        self.disarmed = False
        if self.arm_error:
            raise RuntimeError("auto-cancel arm failed")

    async def fetch_auto_cancel_status(self):
        self.status_calls += 1
        if self.status_error:
            raise RuntimeError("auto-cancel status failed")
        if self.disarmed and not self.disarm_stays_armed:
            deadline = None
        else:
            reference = self.arm_calls[-1] if self.arm_calls else NOW
            deadline = reference + timedelta(seconds=self.deadline_delta - 60)
        return SimpleNamespace(
            deadline=deadline,
            triggered=self.triggered,
            daily_limit=self.daily_limit,
        )

    async def fetch_portfolio(self):
        return SimpleNamespace(positions=self.positions)

    async def cancel_all_orders(self):
        self.cancel_all_calls += 1
        if self.cancel_error:
            raise RuntimeError("cancel all failed")
        if not self.keep_orders_open:
            self.open_orders = []

    async def fetch_open_orders(self):
        self.open_orders_calls += 1
        if self.open_orders_error:
            raise RuntimeError("open order read failed")
        return list(self.open_orders)

    async def disarm_auto_cancel(self):
        self.disarm_calls += 1
        if self.disarm_error:
            raise RuntimeError("disarm failed")
        self.disarmed = True


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
        self.assertEqual(self.session.status_calls, 0)

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

    async def test_graceful_shutdown_cancels_open_orders_then_verifies_disarm(self):
        self.session.open_orders = [{"id": 11}, {"id": 12}]
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(
            result["state"],
            "stopped_after_orders_cleared_and_auto_cancel_disarmed",
        )
        self.assertEqual(result["open_positions_remaining"], 0)
        self.assertEqual(result["open_orders_remaining"], 0)
        self.assertTrue(result["server_auto_cancel_disarmed"])
        self.assertEqual(self.session.cancel_all_calls, 1)
        self.assertEqual(self.session.open_orders_calls, 1)
        self.assertEqual(self.session.disarm_calls, 1)
        self.assertEqual(len(self.session.arm_calls), 1)
        self.assertFalse(result["new_order_permission"])

    async def test_graceful_shutdown_keeps_deadman_armed_when_positions_are_open(self):
        self.session.positions = [SimpleNamespace(size="0.01")]
        self.session.open_orders = [{"id": 11}]
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "shutdown_open_positions_remain_deadman_kept_armed",
        )
        self.assertEqual(self.session.cancel_all_calls, 0)
        self.assertEqual(self.session.disarm_calls, 0)
        self.assertTrue(self.kill_file.exists())
        self.assertFalse(result["new_order_permission"])

    async def test_graceful_shutdown_keeps_deadman_armed_when_cancel_all_fails(self):
        self.session.cancel_error = True
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "shutdown_cancel_all_failed_deadman_kept_armed",
        )
        self.assertEqual(self.session.disarm_calls, 0)
        self.assertTrue(self.kill_file.exists())

    async def test_graceful_shutdown_keeps_deadman_armed_until_all_orders_disappear(self):
        self.session.open_orders = [{"id": 11}]
        self.session.keep_orders_open = True
        result = await self.guard(
            shutdown_cancel_timeout_seconds=0.05,
            shutdown_poll_interval_seconds=0.01,
        ).run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "shutdown_open_orders_remain_deadman_kept_armed",
        )
        self.assertGreater(self.session.open_orders_calls, 1)
        self.assertEqual(self.session.disarm_calls, 0)

    async def test_graceful_shutdown_does_not_claim_disarmed_until_server_confirms(self):
        self.session.disarm_stays_armed = True
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "shutdown_auto_cancel_still_armed_after_disarm",
        )
        self.assertEqual(self.session.disarm_calls, 1)
        self.assertTrue(self.kill_file.exists())

    async def test_graceful_shutdown_disarm_error_leaves_deadman_as_fallback(self):
        self.session.disarm_error = True
        result = await self.guard().run_forever(stop_event=StopImmediatelyEvent())
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(
            result["reason_code"],
            "shutdown_auto_cancel_disarm_or_verification_failed",
        )
        self.assertEqual(self.session.disarm_calls, 1)
        self.assertTrue(self.kill_file.exists())

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
