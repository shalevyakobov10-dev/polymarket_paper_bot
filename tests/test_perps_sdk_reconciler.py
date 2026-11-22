from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace

from perps_order_journal import PerpsOrderJournal
from perps_sdk_reconciler import PerpsSdkReconciler


class FakePaginator:
    def __init__(self, rows):
        self.rows = list(rows)

    def __aiter__(self):
        async def iterate():
            for row in self.rows:
                yield row
        return iterate()


class FakePerpsSession:
    def __init__(self, orders=None, fills=None, *, fetch_error=False, fill_error=False):
        self.orders = orders or {}
        self.fills = list(fills or [])
        self.fetch_error = fetch_error
        self.fill_error = fill_error
        self.fetch_calls = []
        self.fill_calls = []

    async def fetch_orders(self, *, client_order_id):
        self.fetch_calls.append(client_order_id)
        if self.fetch_error:
            raise RuntimeError("private account API unavailable")
        return tuple(self.orders.get(client_order_id, ()))

    def list_fills(self, *, instrument_id=None, start=None, end=None, sort=None, cursor=None):
        self.fill_calls.append({
            "instrument_id": instrument_id,
            "start": start,
            "sort": sort,
        })
        if self.fill_error:
            raise RuntimeError("fill API unavailable")
        return FakePaginator(self.fills)


class PerpsSdkReconcilerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.journal = PerpsOrderJournal(Path(self.temp_dir.name) / "orders.sqlite3")
        self.client_id = "a" * 32
        self.reconciler_session = FakePerpsSession()
        self.reconciler = PerpsSdkReconciler(
            journal=self.journal,
            session=self.reconciler_session,
        )

    def prepare_pending(self, *, quantity="2"):
        self.journal.create_intent(
            client_order_id=self.client_id,
            instrument_id=17,
            side="buy",
            quantity=quantity,
            position_intent="open_long",
            time_in_force="gtc",
            limit_price="2500",
        )
        self.journal.begin_submission(self.client_id)

    def order(
        self,
        *,
        client_order_id=None,
        status="accepted",
        quantity="2",
        filled_quantity="0",
        side="BUY",
        instrument_id=17,
        order_id=123,
    ):
        return SimpleNamespace(
            id=order_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            filled_quantity=filled_quantity,
            status=status,
            client_order_id=client_order_id or self.client_id,
            created_at=datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 11, 22, 8, 1, tzinfo=timezone.utc),
        )

    def fill(self, *, order_id=123, quantity="1", price="2500", instrument_id=17):
        return SimpleNamespace(
            order_id=order_id,
            instrument_id=instrument_id,
            quantity=quantity,
            price=price,
        )

    async def test_pending_submission_is_marked_unknown_before_remote_lookup(self):
        self.prepare_pending()
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["last_reason_code"], "order_not_visible_yet")
        self.assertEqual(state["submission_attempts"], 1)
        self.assertEqual(self.reconciler_session.fetch_calls, [self.client_id])

    async def test_sdk_accepted_status_maps_to_open_only_after_exact_id_reconciliation(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [self.order(status="accepted")]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["venue_order_id"], "123")
        self.assertEqual(state["filled_quantity"], "0")

    async def test_sdk_partial_status_uses_exact_matching_fills_for_weighted_average(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="partial", filled_quantity="1.5")
        ]
        self.reconciler_session.fills = [
            self.fill(order_id=999, quantity="100", price="1"),
            self.fill(quantity="0.5", price="2500"),
            self.fill(quantity="1.0", price="2501"),
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "partially_filled")
        self.assertEqual(state["filled_quantity"], "1.5")
        self.assertEqual(state["average_fill_price"], str((Decimal("0.5")*Decimal("2500") + Decimal("1")*Decimal("2501"))/Decimal("1.5")))
        self.assertEqual(self.reconciler_session.fill_calls[0]["instrument_id"], 17)
        self.assertEqual(self.reconciler_session.fill_calls[0]["sort"], "asc")

    async def test_sdk_filled_status_requires_complete_fills_and_closes_order_state(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="filled", filled_quantity="2")
        ]
        self.reconciler_session.fills = [
            self.fill(quantity="0.75", price="2500"),
            self.fill(quantity="1.25", price="2502"),
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "filled")
        self.assertEqual(state["filled_quantity"], "2")

    async def test_auto_cancelled_sdk_order_maps_to_terminal_cancelled(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="auto_cancelled")
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "cancelled")

    async def test_ioc_no_fill_maps_to_rejected_not_a_fake_fill(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="ioc_no_fill")
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "rejected")
        self.assertEqual(state["filled_quantity"], "0")
        self.assertIsNone(state["average_fill_price"])

    async def test_fill_history_lag_keeps_order_unresolved_instead_of_using_limit_price(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="partial", filled_quantity="1")
        ]
        self.reconciler_session.fills = [self.fill(quantity="0.5")]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["last_reason_code"], "fill_history_not_yet_reconciled")
        self.assertIsNone(state["average_fill_price"])

    async def test_order_lookup_failure_keeps_ambiguous_submission_open(self):
        self.prepare_pending()
        self.reconciler_session.fetch_error = True
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["last_reason_code"], "venue_order_lookup_failed")

    async def test_duplicate_remote_results_keep_submission_ambiguous(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(order_id=123),
            self.order(order_id=124),
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["last_reason_code"], "duplicate_client_order_id_results")

    async def test_duplicate_and_lookup_error_statuses_require_manual_review(self):
        self.prepare_pending()
        for remote_status in ("duplicate_order", "order_not_found", "order_already_terminal"):
            with self.subTest(remote_status=remote_status):
                self.reconciler_session.orders[self.client_id] = [
                    self.order(status=remote_status)
                ]
                # Re-create the intent after the prior subtest's transition.
                if remote_status != "duplicate_order":
                    self.journal.close()
                    self.journal = PerpsOrderJournal(
                        Path(self.temp_dir.name) / f"{remote_status}.sqlite3"
                    )
                    self.reconciler.journal = self.journal
                    self.client_id = "b" * 32 if remote_status == "order_not_found" else "c" * 32
                    self.prepare_pending()
                    self.reconciler_session.orders[self.client_id] = [
                        self.order(
                            client_order_id=self.client_id,
                            status=remote_status,
                        )
                    ]
                state = await self.reconciler.reconcile(self.client_id)
                self.assertEqual(state["status"], "manual_review")

    async def test_unrecognized_sdk_status_moves_order_to_manual_review(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="new_future_status")
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "manual_review")
        self.assertEqual(state["last_reason_code"], "unrecognized_venue_order_status")

    async def test_mismatched_remote_order_terms_require_manual_review(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(side="SELL")
        ]
        state = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(state["status"], "manual_review")
        self.assertEqual(state["last_reason_code"], "side_mismatch")

    async def test_prepared_intent_is_not_reconciled_before_submission_starts(self):
        self.journal.create_intent(
            client_order_id=self.client_id,
            instrument_id=17,
            side="buy",
            quantity="2",
            position_intent="open_long",
            time_in_force="gtc",
            limit_price="2500",
        )
        with self.assertRaisesRegex(RuntimeError, "has not entered submission"):
            await self.reconciler.reconcile(self.client_id)
        self.assertEqual(self.reconciler_session.fetch_calls, [])

    async def test_terminal_intents_are_idempotent_and_do_not_call_the_sdk_again(self):
        self.prepare_pending()
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="accepted")
        ]
        opened = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(opened["status"], "open")
        cancelled = self.journal.record_cancel_requested(self.client_id)
        self.assertEqual(cancelled["status"], "cancel_pending")
        self.reconciler_session.orders[self.client_id] = [
            self.order(status="cancelled")
        ]
        closed = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(closed["status"], "cancelled")
        calls = len(self.reconciler_session.fetch_calls)
        again = await self.reconciler.reconcile(self.client_id)
        self.assertEqual(again["status"], "cancelled")
        self.assertEqual(len(self.reconciler_session.fetch_calls), calls)


if __name__ == "__main__":
    unittest.main()
