import tempfile
import unittest
from decimal import Decimal
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
        # Intentionally optimistic caller values: staging must override them
        # with the values from the durable account snapshot.
        "available_collateral_usd": 100000,
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


def account_snapshot(
    *,
    captured_at=None,
    collateral="100",
    exposure="0",
    positions=0,
    unrealized="0",
    position_assets=None,
    order_assets=None,
):
    return {
        "snapshot_complete": True,
        "captured_at_utc": (captured_at or datetime.now(timezone.utc)).isoformat(),
        "collateral_decimals": 6,
        "collateral_balance_base_units": str(int(Decimal(collateral) * Decimal("1000000"))),
        "collateral_balance_usd": collateral,
        "open_buy_commitment_usd": "0",
        "conservative_available_collateral_usd": collateral,
        "collateral_shortfall_usd": "0",
        "current_open_exposure_usd": exposure,
        "open_position_value_usd": exposure,
        "open_position_cost_usd": "0",
        "open_position_risk_exposure_usd": exposure,
        "open_unrealized_pnl_usd": unrealized,
        "current_open_positions": positions,
        "current_open_orders": 0,
        "open_buy_orders_commitment_usd": "0",
        "open_sell_remaining_shares": "0",
        "open_position_assets_by_condition": position_assets or {},
        "open_order_assets_by_condition": order_assets or {},
        "execution": "none",
    }


class OrderStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.database)
        self.snapshot_id = self.journal.save_account_risk_snapshot(account_snapshot())

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, **overrides):
        values = {
            "client_order_id": "staged-order-001",
            "token_id": "token-123",
            "market_condition_id": "condition-123",
            "side": "BUY",
            "limit_price": 0.50,
            "size": 10,
            "preflight_context": preflight_context(),
        }
        values.update(overrides)
        return stage_buy_order_intent(self.journal, **values)

    def test_passed_preflight_uses_fresh_account_snapshot_and_creates_prepared_intent(self):
        result = self.stage()
        self.assertEqual(result["status"], "prepared_not_authorized")
        self.assertTrue(result["intent_created"])
        self.assertEqual(result["intent_status"], "prepared")
        self.assertEqual(result["submission_attempts"], 0)
        self.assertEqual(result["account_risk_snapshot_id"], self.snapshot_id)
        self.assertFalse(result["order_submission_allowed"])
        self.assertFalse(result["order_signing_allowed"])
        self.assertEqual(result["execution"], "none")
        self.assertEqual(
            result["preflight_report"]["account_risk_snapshot"]["snapshot_id"],
            self.snapshot_id,
        )
        saved = self.journal.get_intent("staged-order-001")
        self.assertEqual(saved["status"], "prepared")
        self.assertEqual(saved["account_risk_snapshot_id"], self.snapshot_id)
        self.assertTrue(saved["preflight_report"]["preflight_passed"])
        self.assertEqual(saved["preflight_report"]["order_notional_usd"], 5.0)
        events = self.journal.list_events("staged-order-001")
        self.assertEqual(len(events), 1)
        self.assertIn("preflight_report", events[0]["state"])

    def test_missing_account_risk_snapshot_blocks_without_creating_intent(self):
        other = OrderJournal(Path(self.temp.name) / "empty.sqlite3")
        result = stage_buy_order_intent(
            other, client_order_id="no-snapshot", token_id="token-123",
            market_condition_id="condition-123", side="BUY", limit_price=0.5,
            size=10, preflight_context=preflight_context(),
        )
        self.assertEqual(result["blockers"], ["account_risk_snapshot_missing"])
        self.assertIsNone(other.get_intent("no-snapshot"))

    def test_stale_account_risk_snapshot_blocks_without_creating_intent(self):
        other = OrderJournal(Path(self.temp.name) / "stale.sqlite3")
        other.save_account_risk_snapshot(account_snapshot(
            captured_at=datetime.now(timezone.utc) - timedelta(minutes=10)
        ))
        result = stage_buy_order_intent(
            other, client_order_id="stale-snapshot", token_id="token-123",
            market_condition_id="condition-123", side="BUY", limit_price=0.5,
            size=10, preflight_context=preflight_context(),
            max_account_snapshot_age_seconds=30,
        )
        self.assertEqual(result["blockers"], ["account_risk_snapshot_stale_or_incomplete"])
        self.assertIsNone(other.get_intent("stale-snapshot"))

    def test_caller_cannot_override_collateral_or_exposure_from_snapshot(self):
        other = OrderJournal(Path(self.temp.name) / "low-collateral.sqlite3")
        other.save_account_risk_snapshot(account_snapshot(collateral="1"))
        context = preflight_context()
        context["available_collateral_usd"] = 1000000
        result = stage_buy_order_intent(
            other, client_order_id="low-collateral", token_id="token-123",
            market_condition_id="condition-123", side="BUY", limit_price=0.5,
            size=10, preflight_context=context,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("insufficient_available_collateral_after_fees", result["blockers"])
        self.assertIsNone(other.get_intent("low-collateral"))

    def test_opposing_outcome_detected_from_position_and_open_order_snapshot(self):
        for field in ("open_position_assets_by_condition", "open_order_assets_by_condition"):
            with self.subTest(field=field):
                other = OrderJournal(Path(self.temp.name) / f"{field}.sqlite3")
                other.save_account_risk_snapshot(account_snapshot(**{
                    "position_assets" if field == "open_position_assets_by_condition" else "order_assets":
                        {"condition-123": ["token-no"]}
                }))
                result = stage_buy_order_intent(
                    other, client_order_id=f"opposite-{field}", token_id="token-yes",
                    market_condition_id="condition-123", side="BUY", limit_price=0.5,
                    size=10, preflight_context=preflight_context(),
                )
                self.assertIn("opposing_market_outcome_already_open", result["blockers"])
                self.assertIsNone(other.get_intent(f"opposite-{field}"))

    def test_snapshot_position_count_and_exposure_override_caller_values(self):
        other = OrderJournal(Path(self.temp.name) / "exposure.sqlite3")
        other.save_account_risk_snapshot(account_snapshot(exposure="96", positions=5))
        context = preflight_context()
        context["current_open_exposure_usd"] = 0
        context["current_open_positions"] = 0
        result = stage_buy_order_intent(
            other, client_order_id="exposure-order", token_id="token-123",
            market_condition_id="condition-123", side="BUY", limit_price=0.5,
            size=10, preflight_context=context,
        )
        self.assertIn("max_open_positions_reached", result["blockers"])
        self.assertIn("max_open_exposure_exceeded", result["blockers"])
        self.assertIsNone(other.get_intent("exposure-order"))


    def test_snapshot_freshness_window_cannot_be_weakened_by_caller(self):
        result = self.stage(max_account_snapshot_age_seconds=86400)
        self.assertEqual(
            result["blockers"],
            ["account_risk_snapshot_freshness_window_out_of_policy"],
        )
        self.assertIsNone(self.journal.get_intent("staged-order-001"))

    def test_missing_market_condition_id_is_blocked(self):
        result = self.stage(market_condition_id="")
        self.assertIn("missing_market_condition_id", result["blockers"])

    def test_missing_release_gate_blocks_without_creating_intent(self):
        context = preflight_context()
        context["release_gates"]["strategy_validation_passed"] = False
        result = self.stage(preflight_context=context)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("release_gate_not_verified:strategy_validation_passed", result["blockers"])
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


    def test_same_client_id_cannot_be_reused_with_a_different_account_snapshot(self):
        first = self.stage()
        self.assertTrue(first["intent_created"])
        self.journal.save_account_risk_snapshot(account_snapshot(collateral="90"))
        replay = self.stage()
        self.assertEqual(replay["status"], "blocked")
        self.assertIn("order_intent_conflict_or_corrupt_journal", replay["blockers"])
        self.assertEqual(self.journal.get_intent("staged-order-001")["account_risk_snapshot_id"],
                         self.snapshot_id)
        self.assertEqual(len(self.journal.list_events("staged-order-001")), 1)

    def test_same_client_id_cannot_reuse_changed_market_preflight_evidence(self):
        first = self.stage()
        self.assertTrue(first["intent_created"])
        context = preflight_context()
        context["book_age_seconds"] = 2
        replay = self.stage(preflight_context=context)
        self.assertEqual(replay["status"], "blocked")
        self.assertIn("order_intent_conflict_or_corrupt_journal", replay["blockers"])
        saved = self.journal.get_intent("staged-order-001")
        self.assertEqual(saved["preflight_report"]["preflight_input_evidence"]["book_age_seconds"], 1)
        self.assertEqual(len(self.journal.list_events("staged-order-001")), 1)

    def test_replaying_same_staged_intent_is_idempotent(self):
        first = self.stage()
        events_before = len(self.journal.list_events("staged-order-001"))
        second = self.stage()
        self.assertEqual(first["client_order_id"], second["client_order_id"])
        self.assertEqual(second["intent_status"], "prepared")
        self.assertFalse(second["intent_created"])
        self.assertTrue(second["intent_reused"])
        saved = self.journal.get_intent("staged-order-001")
        self.assertEqual(second["preflight_report"], saved["preflight_report"])
        self.assertEqual(len(self.journal.list_events("staged-order-001")), events_before)


if __name__ == "__main__":
    unittest.main()