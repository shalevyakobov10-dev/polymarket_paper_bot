from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from perps_order_journal import PerpsOrderJournal


class PerpsOrderJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "perps-orders.sqlite3"
        self.journal = PerpsOrderJournal(self.database)

    def create_intent(
        self,
        client_order_id: str = "a" * 32,
        *,
        quantity: str = "2",
        position_intent: str = "open_long",
    ) -> dict:
        side = "buy" if position_intent in {"open_long", "close_short"} else "sell"
        return self.journal.create_intent(
            client_order_id=client_order_id,
            instrument_id=17,
            side=side,
            quantity=quantity,
            position_intent=position_intent,
            time_in_force="gtc",
            limit_price="2500.50",
        )

    def reconcile(
        self,
        client_order_id: str,
        *,
        remote_status: str = "open",
        matched_client_order_id: str | None = None,
        venue_order_id: str | None = None,
        instrument_id: int = 17,
        side: str = "buy",
        quantity: str = "2",
        filled_quantity: str = "0",
        average_fill_price: str | None = None,
    ) -> dict:
        current = self.journal.get_intent(client_order_id)
        if current is None:
            raise AssertionError("order intent must exist before reconciliation")
        if current["status"] == "prepared":
            self.journal.begin_submission(client_order_id)
            current = self.journal.get_intent(client_order_id)
        actual_venue_id = venue_order_id or current.get("venue_order_id") or "venue-123"
        if current["status"] == "submission_pending":
            self.journal.record_submission_acknowledgement(
                client_order_id,
                venue_order_id=actual_venue_id,
            )
        return self.journal.record_reconciliation(
            client_order_id,
            outcome="found",
            matched_client_order_id=(
                matched_client_order_id or client_order_id
            ),
            venue_order_id=actual_venue_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            remote_status=remote_status,
            filled_quantity=filled_quantity,
            average_fill_price=average_fill_price,
        )

    def test_journal_requires_durable_file_backed_database(self):
        with self.assertRaisesRegex(ValueError, "file-backed"):
            PerpsOrderJournal(":memory:")

    def test_client_order_id_must_match_official_sdk_format(self):
        with self.assertRaisesRegex(ValueError, "32 lowercase hexadecimal"):
            self.journal.create_intent(
                client_order_id="not-a-valid-sdk-id",
                instrument_id=17,
                side="buy",
                quantity="1",
                position_intent="open_long",
                time_in_force="ioc",
            )

    def test_prepare_is_idempotent_only_for_identical_immutable_terms(self):
        first = self.create_intent()
        second = self.create_intent()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(first["execution"], "none")
        self.assertFalse(first["automatic_retry_permitted"])
        with self.assertRaisesRegex(ValueError, "idempotency conflict"):
            self.create_intent(quantity="3")
        self.assertEqual(len(self.journal.list_events("a" * 32)), 1)

    def test_open_and_close_intents_enforce_side_and_reduce_only_semantics(self):
        opening = self.create_intent(position_intent="open_short", client_order_id="b" * 32)
        self.assertEqual(opening["side"], "sell")
        self.assertFalse(opening["reduce_only"])
        closing = self.journal.create_intent(
            client_order_id="c" * 32,
            instrument_id=17,
            side="sell",
            quantity="1",
            position_intent="close_long",
            time_in_force="ioc",
        )
        self.assertTrue(closing["reduce_only"])
        with self.assertRaisesRegex(ValueError, "side does not match"):
            self.journal.create_intent(
                client_order_id="d" * 32,
                instrument_id=17,
                side="sell",
                quantity="1",
                position_intent="open_long",
                time_in_force="ioc",
            )

    def test_gtc_requires_limit_price_and_gtd_requires_future_timezone_aware_expiry(self):
        with self.assertRaisesRegex(ValueError, "require limit_price"):
            self.journal.create_intent(
                client_order_id="e" * 32,
                instrument_id=17,
                side="buy",
                quantity="1",
                position_intent="open_long",
                time_in_force="gtc",
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.journal.create_intent(
                client_order_id="f" * 32,
                instrument_id=17,
                side="buy",
                quantity="1",
                position_intent="open_long",
                time_in_force="gtd",
                limit_price="2500",
                gtd_expiry_utc="2026-11-23T10:00:00",
            )

    def test_submission_is_marked_pending_before_any_adapter_can_send(self):
        self.create_intent()
        pending = self.journal.begin_submission("a" * 32)
        self.assertEqual(pending["status"], "submission_pending")
        self.assertEqual(pending["submission_attempts"], 1)
        self.assertEqual(pending["execution"], "none")
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            self.journal.begin_submission("a" * 32)

    def test_acknowledgement_is_not_mistaken_for_a_fill(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        acknowledged = self.journal.record_submission_acknowledgement(
            "a" * 32, venue_order_id="venue-1"
        )
        self.assertEqual(acknowledged["status"], "acknowledged_unreconciled")
        self.assertEqual(acknowledged["filled_quantity"], "0")
        self.assertIsNone(acknowledged["average_fill_price"])
        self.assertEqual(acknowledged["venue_order_id"], "venue-1")

    def test_ambiguous_submission_is_never_resent_when_order_is_not_yet_visible(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        unknown = self.journal.record_submission_unknown("a" * 32)
        self.assertEqual(unknown["status"], "submission_unknown")
        not_found = self.journal.record_reconciliation(
            "a" * 32, outcome="not_found"
        )
        self.assertEqual(not_found["status"], "submission_unknown")
        self.assertEqual(not_found["reconciliation_attempts"], 1)
        self.assertEqual(not_found["last_reason_code"], "order_not_visible_yet")
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            self.journal.begin_submission("a" * 32)

    def test_interrupted_pending_submission_is_explicitly_recovered_as_unknown(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        reopened = PerpsOrderJournal(self.database)
        self.assertEqual(reopened.get_intent("a" * 32)["status"], "submission_pending")
        self.assertEqual(reopened.recover_interrupted_submissions(), 1)
        recovered = reopened.get_intent("a" * 32)
        self.assertEqual(recovered["status"], "submission_unknown")
        self.assertEqual(recovered["last_reason_code"], "process_restart_during_submission")
        self.assertEqual(reopened.recover_interrupted_submissions(), 0)
        self.assertEqual(
            reopened.list_events("a" * 32)[-1]["event_type"],
            "RECOVERED_INTERRUPTED_SUBMISSION",
        )

    def test_not_found_after_acknowledgement_does_not_erase_venue_order_id(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        self.journal.record_submission_acknowledgement("a" * 32, venue_order_id="venue-2")
        state = self.journal.record_reconciliation("a" * 32, outcome="not_found")
        self.assertEqual(state["status"], "acknowledged_unreconciled")
        self.assertEqual(state["venue_order_id"], "venue-2")

    def test_reconciliation_confirms_open_order_only_after_exact_term_match(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        state = self.reconcile("a" * 32)
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["venue_order_id"], "venue-123")
        self.assertEqual(state["filled_quantity"], "0")

    def test_partial_fill_then_full_fill_is_monotonic_and_reconciled(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        partial = self.reconcile(
            "a" * 32,
            remote_status="partially_filled",
            filled_quantity="0.5",
            average_fill_price="2500.25",
        )
        self.assertEqual(partial["status"], "partially_filled")
        self.assertEqual(partial["filled_quantity"], "0.5")
        filled = self.reconcile(
            "a" * 32,
            remote_status="filled",
            filled_quantity="2",
            average_fill_price="2500.75",
        )
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["filled_quantity"], "2")
        self.assertEqual(filled["average_fill_price"], "2500.75")
        self.assertEqual(
            [event["event_type"] for event in self.journal.list_events("a" * 32)],
            ["INTENT_PREPARED", "SUBMISSION_STARTED", "SUBMISSION_ACKNOWLEDGED",
             "RECONCILIATION_FOUND", "RECONCILIATION_FOUND"],
        )

    def test_fill_regression_moves_order_to_manual_review(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        self.reconcile(
            "a" * 32,
            remote_status="partially_filled",
            filled_quantity="1",
            average_fill_price="2500",
        )
        regressed = self.reconcile(
            "a" * 32,
            remote_status="partially_filled",
            filled_quantity="0.5",
            average_fill_price="2500",
        )
        self.assertEqual(regressed["status"], "manual_review")
        self.assertEqual(regressed["last_reason_code"], "venue_filled_quantity_regressed")

    def test_client_id_instrument_side_or_quantity_mismatch_requires_manual_review(self):
        cases = [
            {"matched_client_order_id": "0" * 32, "expected": "client_order_id_mismatch"},
            {"instrument_id": 99, "expected": "instrument_id_mismatch"},
            {"side": "sell", "expected": "side_mismatch"},
            {"quantity": "3", "expected": "order_quantity_mismatch"},
        ]
        for index, case in enumerate(cases):
            client_id = hex(index + 1)[2:].rjust(32, "0")
            self.create_intent(client_order_id=client_id)
            self.journal.begin_submission(client_id)
            kwargs = {key: value for key, value in case.items() if key != "expected"}
            state = self.reconcile(client_id, **kwargs)
            self.assertEqual(state["status"], "manual_review")
            self.assertEqual(state["last_reason_code"], case["expected"])

    def test_cancel_ack_does_not_close_order_until_account_reconciliation(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        self.journal.record_submission_acknowledgement("a" * 32, venue_order_id="venue-3")
        self.journal.record_reconciliation(
            "a" * 32,
            outcome="found",
            matched_client_order_id="a" * 32,
            venue_order_id="venue-3",
            instrument_id=17,
            side="buy",
            quantity="2",
            remote_status="open",
        )
        pending = self.journal.record_cancel_requested("a" * 32)
        self.assertEqual(pending["status"], "cancel_pending")
        still_open = self.reconcile("a" * 32, remote_status="open")
        self.assertEqual(still_open["status"], "cancel_pending")
        cancelled = self.reconcile(
            "a" * 32,
            remote_status="cancelled",
            filled_quantity="0.25",
            average_fill_price="2500",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["filled_quantity"], "0.25")

    def test_venue_status_fill_inconsistencies_require_manual_review(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        bad = self.reconcile(
            "a" * 32,
            remote_status="filled",
            filled_quantity="1",
            average_fill_price="2500",
        )
        self.assertEqual(bad["status"], "manual_review")
        self.assertEqual(bad["last_reason_code"], "filled_status_quantity_mismatch")

    def test_explicit_rejection_is_terminal_and_never_retryable(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        rejected = self.journal.record_submission_rejection(
            "a" * 32, reason_code="risk_check_rejected"
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["last_reason_code"], "risk_check_rejected")
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            self.journal.begin_submission("a" * 32)

    def test_event_history_is_durable_and_sqlite_integrity_is_clean(self):
        self.create_intent()
        self.journal.begin_submission("a" * 32)
        reopened = PerpsOrderJournal(self.database)
        self.assertEqual(reopened.get_intent("a" * 32)["status"], "submission_pending")
        events = reopened.list_events("a" * 32)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["INTENT_PREPARED", "SUBMISSION_STARTED"],
        )
        report = reopened.integrity_check()
        self.assertTrue(report["integrity_ok"])
        self.assertEqual(report["foreign_key_errors"], 0)
        self.assertEqual(report["open_or_ambiguous_orders"], 1)
        self.assertEqual(report["execution"], "none")


if __name__ == "__main__":
    unittest.main()
