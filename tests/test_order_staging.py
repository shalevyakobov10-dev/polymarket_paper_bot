import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_order_preflight import REQUIRED_RELEASE_GATES
from order_journal import OrderJournal
from order_staging import stage_buy_order_intent


def preflight_context():
    return {
        "best_ask": 0.50,
        "tick_size": 0.01,
        "minimum_order_size": 5,
        "book_age_seconds": 1,
        "max_book_age_seconds": 5,
        "max_price_impact_bps": 100,
        "max_order_notional_usd": 20,
        "available_collateral_usd": 100,
        "estimated_fees_usd": 0.10,
        "current_open_positions": 0,
        "max_open_positions": 5,
        "opposing_market_outcome_open": False,
        "current_open_exposure_usd": 0,
        "max_open_exposure_usd": 100,
        "daily_realized_pnl_usd": 0,
        "open_unrealized_pnl_usd": 0,
        "daily_loss_limit_usd": 25,
        "current_drawdown_pct": 0,
        "max_drawdown_pct": 20,
        "market_active": True,
        "kill_switch_engaged": False,
        "release_gates": {gate: True for gate in REQUIRED_RELEASE_GATES},
    }


class OrderStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.database)
        self.snapshot_id = self.save_snapshot()

    def save_snapshot(
        self, *, age_seconds=0, position_assets=None, order_assets=None,
        available="100", exposure="0", positions=0, open_orders=0, unrealized="0",
        include_unrealized=True, position_assets_raw=None
    ):
        captured = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return self.journal.save_account_risk_snapshot({
            "snapshot_complete": True,
            "captured_at_utc": captured.isoformat(),
            "collateral_balance_usd": "100",
            "conservative_available_collateral_usd": available,
            "current_open_exposure_usd": exposure,
            "current_open_positions": positions,
            "current_open_orders": open_orders,
            "open_position_assets_by_condition": (
                position_assets_raw if position_assets_raw is not None else (position_assets or {})
            ),
            "open_order_assets_by_condition": order_assets or {},
            **({"open_unrealized_pnl_usd": unrealized} if include_unrealized else {}),
            "execution": "none",
        })

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, **overrides):
        values = {
            "client_order_id": "staged-order-001",
            "token_id": "token-123",
            "side": "BUY",
            "limit_price": 0.50,
            "size": 10,
            "market_condition_id": "condition-123",
            "preflight_context": preflight_context(),
        }
        values.update(overrides)
        return stage_buy_order_intent(self.journal, **values)

    def test_passed_preflight_creates_only_a_prepared_local_intent(self):
        result = self.stage()
        self.assertEqual(result["status"], "prepared_not_authorized")
        self.assertTrue(result["intent_created"])
        self.assertEqual(result["intent_status"], "prepared")
        self.assertEqual(result["submission_attempts"], 0)
        self.assertFalse(result["order_submission_allowed"])
        self.assertFalse(result["order_signing_allowed"])
        self.assertEqual(result["execution"], "none")
        saved = self.journal.get_intent("staged-order-001")
        self.assertEqual(saved["status"], "prepared")
        self.assertTrue(saved["preflight_report"]["preflight_passed"])
        self.assertEqual(saved["account_risk_snapshot_id"], self.snapshot_id)
        self.assertEqual(saved["market_condition_id"], "condition-123")
        self.assertEqual(result["account_risk_snapshot_id"], self.snapshot_id)
        events = self.journal.list_events("staged-order-001")
        self.assertEqual(len(events), 1)
        self.assertIn("preflight_report", events[0]["state"])

    def test_missing_release_gate_blocks_without_creating_intent(self):
        context = preflight_context()
        context["release_gates"]["strategy_validation_passed"] = False
        result = self.stage(preflight_context=context)
        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "release_gate_not_verified:strategy_validation_passed",
            result["blockers"],
        )
        self.assertFalse(result["intent_created"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_sell_order_is_not_supported_by_buy_only_preflight(self):
        result = self.stage(side="SELL")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unsupported_order_side", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_mismatched_terms_between_signal_and_preflight_context_are_blocked(self):
        context = preflight_context()
        context["limit_price"] = 0.51
        result = self.stage(preflight_context=context)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"], ["preflight_order_term_mismatch:limit_price"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_incomplete_preflight_context_fails_closed(self):
        result = self.stage(preflight_context={"best_ask": 0.5})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("invalid_preflight_context", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))


    def test_missing_or_stale_account_snapshot_blocks_staging(self):
        self.save_snapshot(age_seconds=600)
        result = self.stage()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("account_risk_snapshot_missing_or_stale", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_account_metrics_are_derived_from_snapshot_not_caller_values(self):
        context = preflight_context()
        context.update({
            "available_collateral_usd": 0,
            "current_open_exposure_usd": 999,
            "current_open_positions": 5,
            "open_unrealized_pnl_usd": -999,
            "opposing_market_outcome_open": True,
        })
        result = self.stage(preflight_context=context)
        self.assertEqual(result["status"], "prepared_not_authorized")
        self.assertTrue(result["preflight_report"]["preflight_passed"])

    def test_opposing_outcome_in_account_snapshot_blocks_staging(self):
        self.save_snapshot(position_assets={"condition-123": ["opposing-token"]})
        result = self.stage()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("opposing_market_outcome_already_open", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_open_orders_count_toward_conservative_position_limit(self):
        self.save_snapshot(open_orders=5)
        result = self.stage()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("max_open_positions_reached", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))


    def test_malformed_snapshot_schema_blocks_staging(self):
        self.save_snapshot(include_unrealized=False)
        result = self.stage()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("invalid_preflight_context", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_malformed_condition_asset_list_blocks_staging(self):
        self.save_snapshot(position_assets_raw={"condition-123": "not-a-list"})
        result = self.stage()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("invalid_preflight_context", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_missing_market_condition_id_blocks_staging(self):
        result = self.stage(market_condition_id="")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("missing_market_condition_id", result["blockers"])
        self.assertIsNone(self.journal.get_intent("staged-order-001"))


    def test_reusing_client_id_with_new_snapshot_is_blocked_without_mutation(self):
        first = self.stage()
        self.assertEqual(first["status"], "prepared_not_authorized")
        original = self.journal.get_intent("staged-order-001")
        self.save_snapshot(available="90")
        second = self.stage()
        self.assertEqual(second["status"], "blocked")
        self.assertIn("existing_intent_conflict", second["blockers"])
        self.assertEqual(self.journal.get_intent("staged-order-001"), original)

    def test_replaying_same_staged_intent_is_idempotent(self):
        first = self.stage()
        events_before = len(self.journal.list_events("staged-order-001"))
        second = self.stage()
        self.assertEqual(first["client_order_id"], second["client_order_id"])
        self.assertEqual(second["intent_status"], "prepared")
        self.assertFalse(second["intent_created"])
        self.assertTrue(second["intent_reused"])
        self.assertEqual(len(self.journal.list_events("staged-order-001")), events_before)


if __name__ == "__main__":
    unittest.main()
