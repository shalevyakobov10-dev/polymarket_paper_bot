from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from perps_live_execution_gateway import (
    LIVE_ACKNOWLEDGEMENT,
    PerpsLiveExecutionGateway,
)
from perps_order_journal import PerpsOrderJournal
from perps_sdk_reconciler import PerpsSdkReconciler


NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)


class FakePaginator:
    def __aiter__(self):
        async def iterator():
            if False:
                yield None
        return iterator()


class FakeSession:
    def __init__(self):
        self.place_calls = []
        self.fetch_order_calls = []
        self.config = SimpleNamespace(instrument_id=17, leverage=2, cross=False)
        self.placed_client_id = None
        self.returned_client_id = None
        self.raise_on_place = False
        self.omit_order_id = False
        self.remote_status = "accepted"
        self.orders = {}

    async def fetch_account_config(self, *, instrument_id=None):
        if instrument_id != 17:
            return ()
        return (self.config,)

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        if self.raise_on_place:
            raise TimeoutError("simulated ambiguous transport outcome")
        client_id = kwargs["client_order_id"]
        self.placed_client_id = client_id
        returned_id = (
            self.returned_client_id
            if self.returned_client_id is not None
            else client_id
        )
        order = SimpleNamespace(
            id=None if self.omit_order_id else 123,
            client_order_id=returned_id,
        )
        # Reconciliation uses the account-order read, never this placement ACK.
        remote = SimpleNamespace(
            id=123,
            instrument_id=kwargs["instrument_id"],
            side=kwargs["side"].lower(),
            quantity=str(kwargs["quantity"]),
            filled_quantity="0",
            resting_quantity=str(kwargs["quantity"]),
            status=self.remote_status,
            client_order_id=client_id,
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=1),
        )
        self.orders[client_id] = (remote,)
        return SimpleNamespace(order=order)

    async def fetch_orders(self, *, client_order_id):
        self.fetch_order_calls.append(client_order_id)
        return self.orders.get(client_order_id, ())

    def list_fills(self, **kwargs):
        return FakePaginator()


class FakePreflight:
    def __init__(self):
        self.report = {
            "mode": "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT",
            "read_only_preflight_passed": True,
            "checked_at": NOW.isoformat(),
            "observations": {
                "margin_available": "100",
                "portfolio_fresh": True,
            },
            "order_placement_authorized": False,
            "execution": "none",
        }
        self.calls = 0

    async def run(self, *, now=None):
        self.calls += 1
        return self.report


class FakeGuard:
    def __init__(self):
        self.report = {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "armed",
            "reason_code": "server_deadman_switch_verified",
            "seconds_remaining": 60,
            "new_order_permission": False,
            "execution": "none",
        }
        self.calls = 0

    async def arm_once(self, *, now=None):
        self.calls += 1
        return self.report


class PerpsLiveExecutionGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.kill_file = root / "kill-switch.json"
        self.lock_file = root / "submission.lock"
        self.database = root / "perps.sqlite3"
        self.journal = PerpsOrderJournal(self.database)
        self.session = FakeSession()
        self.reconciler = PerpsSdkReconciler(
            journal=self.journal,
            session=self.session,
        )
        self.preflight = FakePreflight()
        self.guard = FakeGuard()

    def gateway(self, **overrides):
        options = {
            "session": self.session,
            "journal": self.journal,
            "reconciler": self.reconciler,
            "account_preflight": self.preflight,
            "auto_cancel_guard": self.guard,
            "enabled": True,
            "manually_armed": True,
            "dry_run": False,
            "acknowledgement": LIVE_ACKNOWLEDGEMENT,
            "kill_switch_path": self.kill_file,
            "submission_lock_path": self.lock_file,
            "max_order_notional_usd": "25",
            "max_leverage": 2,
        }
        options.update(overrides)
        return PerpsLiveExecutionGateway(**options)

    def risk_decision(self, **overrides):
        result = {
            "approved": True,
            "max_notional_usd": "25",
            "required_initial_margin_usd": "12.5",
            "risk_snapshot_id": "risk-snapshot-1",
            "strategy_signal_id": "signal-1",
        }
        result.update(overrides)
        return result

    async def submit(self, gateway=None, **overrides):
        args = {
            "instrument_id": 17,
            "quantity": "0.01",
            "position_intent": "open_long",
            "limit_price": "2000",
            "risk_decision": self.risk_decision(),
            "time_in_force": "ioc",
            "client_order_id": "a" * 32,
            "now": NOW,
        }
        args.update(overrides)
        return await (gateway or self.gateway()).submit_limit_order(**args)

    async def test_disabled_by_default_and_never_calls_order_api(self):
        gateway = self.gateway(enabled=False, manually_armed=False, dry_run=True, acknowledgement=None)
        result = await self.submit(gateway)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason_code"], "live_execution_not_explicitly_enabled_and_armed")
        self.assertFalse(result["order_placement_attempted"])
        self.assertEqual(self.session.place_calls, [])

    async def test_enabled_gateway_requires_explicit_acknowledgement_and_dry_run_off(self):
        with self.assertRaisesRegex(ValueError, "DRY_RUN"):
            self.gateway(dry_run=True)
        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            self.gateway(acknowledgement="yes")

    async def test_local_kill_switch_blocks_before_auto_cancel_or_preflight(self):
        self.kill_file.write_text("{}", encoding="utf-8")
        result = await self.submit()
        self.assertEqual(result["reason_code"], "local_perps_kill_switch_present")
        self.assertEqual(self.guard.calls, 0)
        self.assertEqual(self.session.place_calls, [])

    async def test_existing_submission_lock_blocks_concurrent_or_uncertain_writer(self):
        self.lock_file.write_text('{"token":"other"}', encoding="utf-8")
        result = await self.submit()
        self.assertEqual(
            result["reason_code"],
            "another_perps_submission_or_stale_lock_exists",
        )
        self.assertEqual(self.session.place_calls, [])

    async def test_hard_notional_cap_blocks_before_any_account_or_order_call(self):
        result = await self.submit(quantity="0.02", limit_price="2000")
        self.assertEqual(result["reason_code"], "hard_max_order_notional_exceeded")
        self.assertEqual(self.guard.calls, 0)
        self.assertEqual(self.session.place_calls, [])

    async def test_risk_decision_must_be_approved_and_have_required_margin(self):
        denied = await self.submit(risk_decision=self.risk_decision(approved=False))
        self.assertEqual(denied["reason_code"], "independent_risk_decision_not_approved")
        invalid = await self.submit(risk_decision=self.risk_decision(required_initial_margin_usd=None))
        self.assertEqual(invalid["reason_code"], "risk_snapshot_missing_or_invalid")

    async def test_auto_cancel_and_account_preflight_must_both_pass(self):
        self.guard.report["state"] = "blocked"
        blocked_guard = await self.submit()
        self.assertEqual(blocked_guard["reason_code"], "server_auto_cancel_guard_not_verified")
        self.guard.report["state"] = "armed"
        self.preflight.report["read_only_preflight_passed"] = False
        blocked_account = await self.submit(client_order_id="b" * 32)
        self.assertEqual(blocked_account["reason_code"], "account_preflight_failed")
        self.assertEqual(self.session.place_calls, [])

    async def test_required_initial_margin_must_fit_account_available_margin(self):
        self.preflight.report["observations"]["margin_available"] = "5"
        result = await self.submit()
        self.assertEqual(result["reason_code"], "insufficient_available_initial_margin")
        self.assertEqual(self.session.place_calls, [])

    async def test_target_instrument_must_be_isolated_and_at_or_below_two_x(self):
        self.session.config = SimpleNamespace(instrument_id=17, leverage=5, cross=False)
        result = await self.submit()
        self.assertEqual(result["reason_code"], "target_instrument_leverage_exceeds_policy")
        self.session.config = SimpleNamespace(instrument_id=17, leverage=2, cross=True)
        result = await self.submit(client_order_id="b" * 32)
        self.assertEqual(result["reason_code"], "target_instrument_not_confirmed_isolated")
        self.assertEqual(self.session.place_calls, [])

    async def test_missing_or_ambiguous_target_instrument_config_blocks(self):
        self.session.config = None
        result = await self.submit()
        self.assertEqual(result["reason_code"], "target_instrument_config_missing_or_ambiguous")
        self.assertEqual(self.session.place_calls, [])

    async def test_only_ioc_and_fok_are_supported_and_limit_price_is_required(self):
        result = await self.submit(time_in_force="gtc")
        self.assertEqual(result["reason_code"], "time_in_force_must_be_ioc_or_fok")
        invalid = await self.submit(limit_price="0", client_order_id="b" * 32)
        self.assertEqual(invalid["reason_code"], "order_quantity_or_limit_price_invalid")

    async def test_success_persists_before_submit_and_reconciles_actual_venue_order(self):
        result = await self.submit()
        self.assertEqual(result["state"], "open")
        self.assertTrue(result["order_placement_attempted"])
        self.assertFalse(result["automatic_retry_permitted"])
        self.assertEqual(result["venue_order_id"], "123")
        self.assertEqual(len(self.session.place_calls), 1)
        self.assertEqual(len(self.session.fetch_order_calls), 1)
        self.assertEqual(
            self.session.place_calls[0]["client_order_id"],
            "a" * 32,
        )
        self.assertEqual(self.session.place_calls[0]["time_in_force"], "ioc")
        self.assertEqual(self.session.place_calls[0]["reduce_only"], False)
        state = self.journal.get_intent("a" * 32)
        self.assertEqual(state["status"], "open")
        events = self.journal.list_events("a" * 32)
        self.assertEqual(events[0]["event_type"], "INTENT_PREPARED")
        self.assertEqual(events[1]["event_type"], "SUBMISSION_STARTED")

    async def test_close_position_uses_reduce_only_and_correct_sell_side(self):
        result = await self.submit(
            position_intent="close_long",
            client_order_id="b" * 32,
        )
        self.assertEqual(result["state"], "open")
        self.assertEqual(self.session.place_calls[0]["side"], "SELL")
        self.assertTrue(self.session.place_calls[0]["reduce_only"])

    async def test_transport_timeout_is_ambiguous_and_is_never_retried(self):
        self.session.raise_on_place = True
        first = await self.submit()
        self.assertEqual(first["state"], "submission_unknown")
        self.assertFalse(first["automatic_retry_permitted"])
        self.session.raise_on_place = False
        second = await self.submit()
        self.assertEqual(second["reason_code"], "client_order_id_already_used_or_not_prepared")
        self.assertEqual(len(self.session.place_calls), 1)
        self.assertEqual(self.journal.get_intent("a" * 32)["submission_attempts"], 1)

    async def test_acknowledged_client_id_mismatch_is_sent_to_manual_review(self):
        self.session.returned_client_id = "b" * 32
        result = await self.submit()
        self.assertEqual(result["state"], "manual_review")
        self.assertEqual(result["reason_code"], "client_order_id_mismatch")
        self.assertEqual(self.journal.get_intent("a" * 32)["status"], "manual_review")

    async def test_missing_venue_order_id_remains_ambiguous_and_not_retried(self):
        self.session.omit_order_id = True
        result = await self.submit()
        self.assertEqual(result["state"], "submission_unknown")
        self.assertEqual(
            result["reason_code"],
            "sdk_acknowledgement_missing_venue_order_id",
        )
        self.assertEqual(self.journal.get_intent("a" * 32)["submission_attempts"], 1)

    async def test_limit_cap_cannot_be_configured_above_initial_hard_ceiling(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed 100 USD"):
            self.gateway(max_order_notional_usd="101")

    async def test_gateway_lock_is_released_after_attempt(self):
        result = await self.submit()
        self.assertEqual(result["state"], "open")
        self.assertFalse(self.lock_file.exists())


if __name__ == "__main__":
    unittest.main()
