from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from perps_account_preflight import PerpsAccountPreflight
from perps_order_journal import PerpsOrderJournal


NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)


class FakePreflightSession:
    def __init__(self):
        self.portfolio = SimpleNamespace(
            timestamp=NOW - timedelta(seconds=1),
            in_liquidation=False,
            positions=(),
            margin=SimpleNamespace(available_order_margin="100"),
        )
        self.balances = (SimpleNamespace(asset="USDC", balance="100", value="100"),)
        self.open_orders = ()
        self.auto_cancel = SimpleNamespace(
            deadline=NOW + timedelta(seconds=60),
            triggered=0,
            daily_limit=5,
        )
        self.account_configs = ()
        self.orders_by_client = {}
        self.read_error = False
        self.fetch_orders_calls = []

    async def fetch_portfolio(self):
        if self.read_error:
            raise RuntimeError("account API unavailable")
        return self.portfolio

    async def fetch_balances(self):
        return self.balances

    async def fetch_open_orders(self):
        return self.open_orders

    async def fetch_auto_cancel_status(self):
        return self.auto_cancel

    async def fetch_account_config(self):
        return self.account_configs

    async def fetch_orders(self, *, client_order_id):
        self.fetch_orders_calls.append(client_order_id)
        return tuple(self.orders_by_client.get(client_order_id, ()))

    def list_fills(self, **kwargs):
        class EmptyAsyncPaginator:
            def __aiter__(self):
                async def iterator():
                    if False:
                        yield None
                return iterator()
        return EmptyAsyncPaginator()


class PerpsAccountPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.journal = PerpsOrderJournal(Path(self.temp_dir.name) / "orders.sqlite3")
        self.session = FakePreflightSession()
        self.preflight = PerpsAccountPreflight(
            journal=self.journal,
            session=self.session,
        )

    async def test_clean_account_passes_read_only_preflight_but_never_authorizes_orders(self):
        report = await self.preflight.run(now=NOW)
        self.assertTrue(report["read_only_preflight_passed"])
        self.assertEqual(report["blocked_reasons"], [])
        self.assertFalse(report["order_placement_authorized"])
        self.assertFalse(report["live_execution_enabled"])
        self.assertEqual(report["execution"], "none")
        self.assertTrue(report["observations"]["portfolio_fresh"])
        self.assertTrue(report["observations"]["auto_cancel_armed"])

    async def test_stale_or_future_portfolio_is_blocked(self):
        self.session.portfolio.timestamp = NOW - timedelta(minutes=5)
        report = await self.preflight.run(now=NOW)
        self.assertIn("portfolio_snapshot_stale_or_future_dated", report["blocked_reasons"])
        self.assertFalse(report["read_only_preflight_passed"])

    async def test_liquidation_and_nonpositive_margin_are_blocked(self):
        self.session.portfolio.in_liquidation = True
        self.session.portfolio.margin.available_order_margin = "0"
        report = await self.preflight.run(now=NOW)
        self.assertIn("account_in_liquidation_or_state_unknown", report["blocked_reasons"])
        self.assertIn("no_available_order_margin", report["blocked_reasons"])

    async def test_cross_margin_or_leverage_above_policy_is_blocked(self):
        self.session.portfolio.positions = (
            SimpleNamespace(cross=True, leverage=5),
        )
        self.session.account_configs = (
            SimpleNamespace(cross=True, leverage=5, instrument_id=17),
        )
        report = await self.preflight.run(now=NOW)
        self.assertIn("cross_margin_position_detected", report["blocked_reasons"])
        self.assertIn("position_leverage_exceeds_policy", report["blocked_reasons"])
        self.assertIn("cross_margin_account_config_detected", report["blocked_reasons"])

    async def test_open_position_and_order_count_limits_are_enforced(self):
        self.session.portfolio.positions = tuple(
            SimpleNamespace(cross=False, leverage=2) for _ in range(4)
        )
        self.session.open_orders = tuple(
            SimpleNamespace(
                client_order_id=f"{i:032x}",
                instrument_id=17,
                side="BUY",
                quantity="1",
            )
            for i in range(11)
        )
        report = await self.preflight.run(now=NOW)
        self.assertIn("max_open_positions_exceeded", report["blocked_reasons"])
        self.assertIn("max_open_orders_exceeded", report["blocked_reasons"])
        self.assertEqual(report["observations"]["unknown_remote_open_orders"], 11)

    async def test_unarmed_or_nearly_expired_deadman_switch_is_blocked(self):
        self.session.auto_cancel.deadline = NOW + timedelta(seconds=3)
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "auto_cancel_deadman_switch_not_armed_or_near_expiry",
            report["blocked_reasons"],
        )

    async def test_exhausted_auto_cancel_daily_limit_is_blocked(self):
        self.session.auto_cancel.triggered = 5
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "auto_cancel_daily_trigger_limit_unavailable_or_exhausted",
            report["blocked_reasons"],
        )

    async def test_unknown_remote_open_order_blocks_account_readiness(self):
        self.session.open_orders = (
            SimpleNamespace(
                client_order_id="b" * 32,
                instrument_id=17,
                side="BUY",
                quantity="1",
            ),
        )
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "unmanaged_or_mismatched_remote_open_orders",
            report["blocked_reasons"],
        )

    async def test_missing_remote_open_order_blocks_even_if_local_journal_says_open(self):
        client_id = "a" * 32
        self.journal.create_intent(
            client_order_id=client_id,
            instrument_id=17,
            side="buy",
            quantity="2",
            position_intent="open_long",
            time_in_force="gtc",
            limit_price="2500",
        )
        self.journal.begin_submission(client_id)
        self.journal.record_submission_acknowledgement(
            client_id,
            venue_order_id="123",
        )
        self.journal.record_reconciliation(
            client_id,
            outcome="found",
            matched_client_order_id=client_id,
            venue_order_id="123",
            instrument_id=17,
            side="buy",
            quantity="2",
            remote_status="open",
            filled_quantity="0",
        )
        self.session.orders_by_client[client_id] = (
            SimpleNamespace(
                id=123,
                client_order_id=client_id,
                instrument_id=17,
                side="BUY",
                quantity="2",
                filled_quantity="0",
                status="open",
            ),
        )
        report = await self.preflight.run(now=NOW)
        self.assertEqual(self.session.fetch_orders_calls, [client_id])
        self.assertIn(
            "local_open_orders_missing_from_venue_open_order_list",
            report["blocked_reasons"],
        )

    async def test_unknown_local_submission_is_reconciled_but_still_blocks_if_not_found(self):
        client_id = "c" * 32
        self.journal.create_intent(
            client_order_id=client_id,
            instrument_id=17,
            side="buy",
            quantity="2",
            position_intent="open_long",
            time_in_force="gtc",
            limit_price="2500",
        )
        self.journal.begin_submission(client_id)
        report = await self.preflight.run(now=NOW)
        self.assertIn("unresolved_or_unsubmitted_local_orders", report["blocked_reasons"])
        self.assertEqual(
            self.journal.get_intent(client_id)["status"],
            "submission_unknown",
        )

    async def test_account_read_failure_fails_closed(self):
        self.session.read_error = True
        report = await self.preflight.run(now=NOW)
        self.assertEqual(report["blocked_reasons"], ["account_read_api_unavailable"])
        self.assertFalse(report["read_only_preflight_passed"])
        self.assertFalse(report["order_placement_authorized"])

    def test_invalid_preflight_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_leverage"):
            PerpsAccountPreflight(
                journal=self.journal,
                session=self.session,
                max_leverage=0,
            )


if __name__ == "__main__":
    unittest.main()
