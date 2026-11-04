import json
import tempfile
import unittest
from pathlib import Path

from order_intent_ledger import OrderIntentLedger


class OrderIntentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "order_intents.json"
        self.ledger = OrderIntentLedger(self.path)
        self.order = {
            "market_id": "market-1",
            "token_id": "token-yes",
            "side": "BUY",
            "price": "0.50",
            "size": "10.000",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_is_idempotent_for_same_logical_intent(self):
        first = self.ledger.prepare("signal-123", self.order)
        second = self.ledger.prepare("signal-123", {**self.order, "price": 0.5})
        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "prepared")
        self.assertEqual(len(json.loads(self.path.read_text())["intents"]), 1)

    def test_reusing_intent_id_with_changed_terms_is_rejected(self):
        self.ledger.prepare("signal-123", self.order)
        with self.assertRaisesRegex(ValueError, "different order terms"):
            self.ledger.prepare("signal-123", {**self.order, "price": 0.51})

    def test_submission_can_be_claimed_only_once_even_after_restart(self):
        self.ledger.prepare("signal-123", self.order)
        self.assertTrue(self.ledger.begin_submission("signal-123"))
        restarted = OrderIntentLedger(self.path)
        self.assertFalse(restarted.begin_submission("signal-123"))
        self.assertEqual(restarted.get("signal-123")["submission_attempts"], 1)

    def test_ambiguous_result_requires_reconciliation_and_never_retries(self):
        self.ledger.prepare("signal-123", self.order)
        self.assertTrue(self.ledger.begin_submission("signal-123"))
        self.ledger.record_submission_result("signal-123", "unknown")
        restarted = OrderIntentLedger(self.path)
        self.assertFalse(restarted.begin_submission("signal-123"))
        reconciled = restarted.reconcile(
            "signal-123", "not_found", "CLOB order lookup at 2026-11-04T12:00Z"
        )
        self.assertEqual(reconciled["status"], "reconciled_not_found")
        self.assertFalse(restarted.begin_submission("signal-123"))

    def test_reconciliation_is_required_for_in_flight_uncertain_submission(self):
        self.ledger.prepare("signal-123", self.order)
        self.ledger.begin_submission("signal-123")
        with self.assertRaisesRegex(ValueError, "only ambiguous"):
            self.ledger.reconcile("signal-123", "filled", "lookup")

    def test_open_order_can_be_reconciled_again_without_resubmission(self):
        self.ledger.prepare("signal-123", self.order)
        self.ledger.begin_submission("signal-123")
        opened = self.ledger.reconcile("signal-123", "open", "order lookup")
        self.assertEqual(opened["status"], "reconciled_open")
        filled = self.ledger.reconcile("signal-123", "filled", "later order lookup")
        self.assertEqual(filled["status"], "reconciled_filled")
        self.assertFalse(self.ledger.begin_submission("signal-123"))

    def test_invalid_order_payloads_fail_closed(self):
        invalid = [
            {**self.order, "price": 1.0},
            {**self.order, "price": float("nan")},
            {**self.order, "size": 0},
            {**self.order, "side": "HOLD"},
            {**self.order, "token_id": ""},
        ]
        for index, order in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.ledger.prepare(f"invalid-{index}", order)

    def test_corrupt_ledger_is_not_silently_reset(self):
        self.path.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "refusing to reset"):
            OrderIntentLedger(self.path)

    def test_missing_intent_and_invalid_transitions_are_rejected(self):
        with self.assertRaises(KeyError):
            self.ledger.begin_submission("missing")
        self.ledger.prepare("signal-123", self.order)
        with self.assertRaises(ValueError):
            self.ledger.record_submission_result("signal-123", "accepted")
        with self.assertRaises(ValueError):
            self.ledger.record_submission_result("signal-123", "maybe")


if __name__ == "__main__":
    unittest.main()
