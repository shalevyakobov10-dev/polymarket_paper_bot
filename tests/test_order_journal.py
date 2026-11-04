import tempfile
import threading
import unittest
from pathlib import Path

from order_journal import OrderJournal
from order_lifecycle import create_order_intent, submission_policy


def new_intent(**overrides):
    values = {
        "client_order_id": "journal-order-001",
        "token_id": "token-456",
        "side": "BUY",
        "limit_price": 0.37,
        "size": 12,
    }
    values.update(overrides)
    return create_order_intent(**values)


class OrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_created_intent_persists_and_reopens(self):
        saved = self.journal.create_intent(new_intent())
        reopened = OrderJournal(self.database)
        loaded = reopened.get_intent("journal-order-001")
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["status"], "prepared")
        self.assertEqual(len(reopened.list_events("journal-order-001")), 1)

    def test_replaying_same_intent_is_idempotent_but_changed_terms_are_rejected(self):
        first = self.journal.create_intent(new_intent())
        replay = self.journal.create_intent(new_intent())
        self.assertEqual(first, replay)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), 1)
        with self.assertRaisesRegex(ValueError, "different order terms"):
            self.journal.create_intent(new_intent(size=13))

    def test_submission_pending_is_committed_and_duplicate_attempt_is_blocked(self):
        self.journal.create_intent(new_intent())
        pending = self.journal.begin_submission("journal-order-001")
        self.assertEqual(pending["status"], "submission_pending")
        reloaded = OrderJournal(self.database).get_intent("journal-order-001")
        self.assertEqual(reloaded["status"], "submission_pending")
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            self.journal.begin_submission("journal-order-001")

    def test_ambiguous_outcome_and_not_found_survive_restart(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        unknown = self.journal.record_submission_response(
            "journal-order-001", outcome="ambiguous", reason_code="transport_timeout"
        )
        self.assertEqual(unknown["status"], "submission_unknown")
        restarted = OrderJournal(self.database)
        still_unknown = restarted.reconcile_submission(
            "journal-order-001", outcome="not_found"
        )
        self.assertEqual(still_unknown["status"], "submission_unknown")
        self.assertFalse(submission_policy(still_unknown)["automatic_retry_permitted"])

    def test_crash_recovery_converts_pending_to_unknown_not_prepared(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        restarted = OrderJournal(self.database)
        self.assertEqual(restarted.recover_interrupted_submissions(), 1)
        recovered = restarted.get_intent("journal-order-001")
        self.assertEqual(recovered["status"], "submission_unknown")
        self.assertEqual(recovered["last_reason_code"], "process_restart_during_submission")
        self.assertEqual(recovered["submission_attempts"], 1)
        self.assertEqual(restarted.recover_interrupted_submissions(), 0)
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            restarted.begin_submission("journal-order-001")

    def test_recovery_is_idempotent_and_does_not_touch_resolved_orders(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="rejected", reason_code="venue_rejected"
        )
        self.assertEqual(self.journal.recover_interrupted_submissions(), 0)
        self.assertEqual(self.journal.get_intent("journal-order-001")["status"], "rejected")

    def test_event_journal_records_state_transitions_in_order(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-123"
        )
        events = self.journal.list_events("journal-order-001")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["intent_created", "submission_started", "submission_response_accepted"],
        )
        self.assertEqual(events[-1]["state"]["status"], "open")

    def test_two_concurrent_workers_cannot_begin_the_same_order_twice(self):
        self.journal.create_intent(new_intent())
        barrier = threading.Barrier(2)
        successes = []
        failures = []

        def worker():
            barrier.wait()
            try:
                successes.append(self.journal.begin_submission("journal-order-001"))
            except Exception as exc:  # Capture the losing concurrent transition.
                failures.append(exc)

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        final_state = self.journal.get_intent("journal-order-001")
        self.assertEqual(final_state["submission_attempts"], 1)
        self.assertEqual(final_state["status"], "submission_pending")

    def test_unknown_order_can_only_reconcile_when_client_id_matches(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="ambiguous"
        )
        result = self.journal.reconcile_submission(
            "journal-order-001",
            outcome="found_open",
            matched_client_order_id="someone-else",
            exchange_order_id="venue-987",
        )
        self.assertEqual(result["status"], "manual_review")


if __name__ == "__main__":
    unittest.main()
