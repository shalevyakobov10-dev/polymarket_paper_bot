from __future__ import annotations

import asyncio
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
            margin=SimpleNamespace(
                total_account_value="100",
                available_order_margin="100",
                total_initial_margin="0",
                total_maintenance_margin="0",
                total_position_value="0",
            ),
            withdrawable="100",
            fee_tier=0,
        )
        self.balances = (SimpleNamespace(asset="USDC", balance="100", value="100"),)
        self.open_orders = ()
        self.auto_cancel = SimpleNamespace(
            deadline=NOW + timedelta(seconds=60),
            triggered=0,
            daily_limit=5,
        )
        self.account_configs = ()
        self.fee_schedule_snapshot = {
            "retrieved_at": NOW - timedelta(seconds=1),
            "fee_schedule": [
                {
                    "instrument_type": "perpetual",
                    "category": "legacy_crypto",
                    "tiers": [
                        {
                            "min_volume_30d": "0",
                            "taker_fee_rate": "0.0005",
                            "maker_fee_rate": "0.0002",
                        },
                        {
                            "min_volume_30d": "1000000",
                            "taker_fee_rate": "0.0004",
                            "maker_fee_rate": "0.0002",
                        },
                    ],
                },
            ],
        }
        self.orders_by_client = {}
        self.read_error = False
        self.hang_account_read = False
        self.fetch_orders_calls = []

    async def fetch_portfolio(self):
        if self.hang_account_read:
            await asyncio.Event().wait()
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

    async def fetch_public_fee_schedule(self):
        return self.fee_schedule_snapshot

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
            fee_schedule_provider=self.session.fetch_public_fee_schedule,
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
        self.assertTrue(report["observations"]["fee_schedule_fresh"])
        self.assertEqual(report["observations"]["account_fee_tier"], 0)
        self.assertEqual(report["observations"]["effective_taker_fee_rate"], "0.0005")
        self.assertEqual(report["observations"]["effective_maker_fee_rate"], "0.0002")

    async def test_account_fee_tier_is_resolved_from_current_published_schedule(self):
        self.session.portfolio.fee_tier = 1
        report = await self.preflight.run(now=NOW)
        self.assertTrue(report["read_only_preflight_passed"])
        self.assertEqual(report["observations"]["effective_taker_fee_rate"], "0.0004")
        self.assertEqual(report["observations"]["effective_maker_fee_rate"], "0.0002")

    async def test_stale_public_fee_schedule_blocks_account_preflight(self):
        self.session.fee_schedule_snapshot["retrieved_at"] = NOW - timedelta(minutes=30)
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "public_perps_fee_schedule_missing_stale_or_ambiguous",
            report["blocked_reasons"],
        )
        self.assertFalse(report["observations"]["fee_schedule_fresh"])

    async def test_account_fee_tier_outside_published_schedule_fails_closed(self):
        self.session.portfolio.fee_tier = 99
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "public_perps_fee_schedule_missing_stale_or_ambiguous",
            report["blocked_reasons"],
        )

    async def test_conflicting_legacy_category_fee_schedules_are_ambiguous(self):
        self.session.fee_schedule_snapshot["fee_schedule"].append({
            "instrument_type": "perpetual",
            "category": "legacy_equity",
            "tiers": [
                {"min_volume_30d": "0", "taker_fee_rate": "0.0008", "maker_fee_rate": "0.0002"},
                {"min_volume_30d": "1000000", "taker_fee_rate": "0.0007", "maker_fee_rate": "0.0002"},
            ],
        })
        report = await self.preflight.run(now=NOW)
        self.assertIn(
            "public_perps_fee_schedule_missing_stale_or_ambiguous",
            report["blocked_reasons"],
        )

    async def test_missing_fee_schedule_provider_fails_closed(self):
        preflight = PerpsAccountPreflight(journal=self.journal, session=self.session)
        report = await preflight.run(now=NOW)
        self.assertIn("public_perps_fee_schedule_provider_missing", report["blocked_reasons"])
        self.assertFalse(report["read_only_preflight_passed"])

    async def test_synchronous_public_fee_provider_is_called_off_event_loop(self):
        preflight = PerpsAccountPreflight(
            journal=self.journal,
            session=self.session,
            fee_schedule_provider=lambda: self.session.fee_schedule_snapshot,
        )
        report = await preflight.run(now=NOW)
        self.assertTrue(report["read_only_preflight_passed"])
        self.assertTrue(report["observations"]["fee_schedule_fresh"])
        self.assertEqual(report["observations"]["effective_taker_fee_rate"], "0.0005")

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
            SimpleNamespace(
                instrument_id=17, symbol="BTC", size="1", entry_price="50000",
                cross=True, leverage=5, initial_margin="100", maintenance_margin="50",
                position_value="50000", liquidation_price="25000",
                adl_index=0,
            ),
        )
        self.session.account_configs = (
            SimpleNamespace(cross=True, leverage=5, instrument_id=17),
        )
        report = await self.preflight.run(now=NOW)
        self.assertIn("cross_margin_position_detected", report["blocked_reasons"])
        self.assertIn("position_leverage_exceeds_policy", report["blocked_reasons"])
        self.assertIn("cross_margin_account_config_detected", report["blocked_reasons"])

    async def test_invalid_portfolio_margin_totals_block_readiness(self):
        self.session.portfolio.margin.total_account_value = "100"
        self.session.portfolio.margin.total_maintenance_margin = "101"
        report = await self.preflight.run(now=NOW)
        self.assertIn("account_maintenance_margin_exceeds_equity", report["blocked_reasons"])

    async def test_duplicate_or_invalid_position_data_blocks_readiness(self):
        position = SimpleNamespace(
            instrument_id=17, symbol="BTC", size="1", entry_price="50000",
            cross=False, leverage=2, initial_margin="100", maintenance_margin="50",
            position_value="50000", liquidation_price="25000", adl_index=3,
        )
        self.session.portfolio.positions = (position, position)
        report = await self.preflight.run(now=NOW)
        self.assertIn("account_position_data_invalid", report["blocked_reasons"])
        self.assertIn("position_adl_risk_elevated", report["blocked_reasons"])
        self.assertEqual(report["observations"]["position_integrity_errors"], 1)
        self.assertEqual(report["observations"]["high_adl_risk_positions"], 2)

    async def test_open_position_and_order_count_limits_are_enforced(self):
        self.session.portfolio.positions = tuple(
            SimpleNamespace(
                instrument_id=i + 1, symbol=f"COIN{i}", size="1", entry_price="100",
                cross=False, leverage=2, initial_margin="50", maintenance_margin="20",
                position_value="100", liquidation_price="50", adl_index=0,
            )
            for i in range(4)
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

    async def test_large_terminal_journal_history_does_not_exhaust_active_reconciliation_limit(self):
        for index in range(105):
            client_id = f"{index + 1:032x}"
            self.journal.create_intent(
                client_order_id=client_id,
                instrument_id=17,
                side="buy",
                quantity="1",
                position_intent="open_long",
                time_in_force="ioc",
            )
            self.journal.begin_submission(client_id)
            self.journal.record_submission_rejection(client_id, reason_code="test_terminal")

        report = await self.preflight.run(now=NOW)

        self.assertTrue(report["read_only_preflight_passed"])
        self.assertEqual(report["observations"]["local_order_count"], 0)
        self.assertEqual(report["observations"]["unresolved_order_count"], 0)

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

    async def test_open_order_must_match_exact_venue_id_and_immutable_terms(self):
        client_id = "d" * 32
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
        self.journal.record_submission_acknowledgement(client_id, venue_order_id="123")
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
        self.session.open_orders = (
            SimpleNamespace(
                id=124,
                client_order_id=client_id,
                instrument_id=17,
                side="BUY",
                quantity="2",
                price="2500",
                filled_quantity="0",
                reduce_only=False,
                status="open",
            ),
        )

        report = await self.preflight.run(now=NOW)

        self.assertIn(
            "unmanaged_or_mismatched_remote_open_orders",
            report["blocked_reasons"],
        )
        self.assertIn(
            "local_open_orders_missing_from_venue_open_order_list",
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

    async def test_account_read_timeout_fails_closed_without_waiting_indefinitely(self):
        self.session.hang_account_read = True
        bounded = PerpsAccountPreflight(
            journal=self.journal,
            session=self.session,
            account_read_timeout_seconds=0.05,
        )
        report = await bounded.run(now=NOW)
        self.assertEqual(report["blocked_reasons"], ["account_read_api_timeout"])
        self.assertFalse(report["read_only_preflight_passed"])
        self.assertFalse(report["order_placement_authorized"])

    def test_invalid_account_read_timeout_is_rejected(self):
        for timeout in (True, float("nan"), 0, 61):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "account_read_timeout_seconds"):
                    PerpsAccountPreflight(
                        journal=self.journal,
                        session=self.session,
                        account_read_timeout_seconds=timeout,
                    )

    def test_invalid_preflight_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_leverage"):
            PerpsAccountPreflight(
                journal=self.journal,
                session=self.session,
                max_leverage=0,
            )


if __name__ == "__main__":
    unittest.main()
