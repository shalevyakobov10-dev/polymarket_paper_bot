from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from order_journal import OrderJournal
from order_lifecycle import create_order_intent
from order_journal_status import OrderJournalStatusError, read_local_order_journal_status


class LocalOrderJournalStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "order-journal.sqlite3"
        self.journal = OrderJournal(self.database)

    def make_intent(self, client_id, token_id="token-public-1"):
        return create_order_intent(
            client_order_id=client_id,
            token_id=token_id,
            side="BUY",
            limit_price="0.42",
            size="7",
        )

    def test_empty_journal_is_reported_as_local_only_not_venue_verified(self):
        result = read_local_order_journal_status(self.database)
        self.assertTrue(result["available"])
        self.assertTrue(result["snapshot_complete"])
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["active_order_count"], 0)
        self.assertEqual(result["unresolved_order_count"], 0)
        self.assertFalse(result["venue_open_orders_checked"])
        self.assertFalse(result["venue_status_verified"])
        self.assertFalse(result["order_submission_allowed"])
        self.assertFalse(result["order_cancellation_allowed"])

    def test_active_and_ambiguous_intents_are_separated_and_sanitized(self):
        self.journal.create_intent(self.make_intent("active-1"))
        self.journal.begin_submission("active-1")
        self.journal.record_submission_response(
            "active-1", outcome="accepted", exchange_order_id="venue-101"
        )

        self.journal.create_intent(self.make_intent("ambiguous-1", "token-public-2"))
        self.journal.begin_submission("ambiguous-1")
        self.journal.record_submission_response(
            "ambiguous-1", outcome="ambiguous", reason_code="transport_timeout"
        )

        result = read_local_order_journal_status(self.database)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["active_order_count"], 1)
        self.assertEqual(result["unresolved_order_count"], 1)
        self.assertEqual(result["active_orders"][0]["client_order_id"], "active-1")
        self.assertEqual(result["active_orders"][0]["exchange_order_id"], "venue-101")
        self.assertEqual(result["unresolved_orders"][0]["client_order_id"], "ambiguous-1")
        self.assertTrue(result["unresolved_orders"][0]["reconciliation_required"])
        self.assertFalse(result["automatic_retry_permitted"])
        self.assertEqual(result["execution"], "none")
        self.assertNotIn("token-public-1", repr(result))
        self.assertNotIn("token-public-2", repr(result))
        self.assertNotIn("venue_response", repr(result))

    def test_read_only_status_does_not_mutate_journal(self):
        self.journal.create_intent(self.make_intent("prepared-1"))
        connection = sqlite3.connect(self.database)
        try:
            before = connection.execute(
                "SELECT client_order_id,state_json,created_at,updated_at FROM order_intents"
            ).fetchall()
            before_events = connection.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
        finally:
            connection.close()

        read_local_order_journal_status(self.database)

        connection = sqlite3.connect(self.database)
        try:
            after = connection.execute(
                "SELECT client_order_id,state_json,created_at,updated_at FROM order_intents"
            ).fetchall()
            after_events = connection.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(after, before)
        self.assertEqual(after_events, before_events)

    def test_missing_database_is_not_treated_as_empty(self):
        missing = Path(self.temp.name) / "does-not-exist.sqlite3"
        with self.assertRaisesRegex(OrderJournalStatusError, "missing_or_unsafe"):
            read_local_order_journal_status(missing)

    def test_corrupt_intent_fails_closed(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE order_intents SET state_json=? WHERE client_order_id=?",
                ("not-json", "missing-id"),
            )
            connection.execute(
                "INSERT INTO order_intents(client_order_id,state_json) VALUES(?,?)",
                ("broken", "not-json"),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(OrderJournalStatusError, "intent_invalid"):
            read_local_order_journal_status(self.database)

    def test_intent_limit_is_bounded_and_rejects_invalid_values(self):
        for limit in (0, -1, True, 2001, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    read_local_order_journal_status(
                        self.database, maximum_intents=limit
                    )


if __name__ == "__main__":
    unittest.main()
