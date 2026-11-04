import tempfile
import unittest
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

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, **overrides):
        values = {
            "client_order_id": "staged-order-001",
            "token_id": "token-123",
            "side": "BUY",
            "limit_price": 0.50,
            "size": 10,
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
