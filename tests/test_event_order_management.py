from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from event_order_management import (
    CANCEL_ALL_ACKNOWLEDGEMENT,
    CANCEL_ONE_ACKNOWLEDGEMENT,
    EventOrderOperationLock,
    EventOrderOperations,
)
from order_journal import OrderJournal
from order_lifecycle import create_order_intent


class FakePage:
    def __init__(self, items, *, limit_reached=False):
        self.items = tuple(items)
        self.has_more = False
        self.limit_reached = limit_reached


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def __iter__(self):
        return iter(self.pages)


def fake_order(
    order_id, *, status="LIVE", size="10", matched="0",
    token_id="token-1", side="BUY", price="0.5", condition_id="condition-1",
):
    return SimpleNamespace(
        id=order_id,
        asset_id=token_id,
        condition_id=condition_id,
        side=side,
        price=price,
        original_size=size,
        size_matched=matched,
        status=status,
        order_type="GTC",
    )


class FakeSession:
    def __init__(self, orders):
        self.orders = {order.id: order for order in orders}
        self.cancel_calls = []
        self.cancel_all_calls = 0
        self.cancel_result_override = None
        self.leave_open_on_cancel = False
        self.raise_after_cancel = False

    def list_open_orders(self):
        return FakePaginator([FakePage([
            order for order in self.orders.values() if str(order.status).upper() == "LIVE"
        ])])

    def get_order(self, *, order_id):
        return self.orders[order_id]

    def cancel_order(self, *, order_id):
        self.cancel_calls.append(order_id)
        if not self.leave_open_on_cancel:
            self.orders[order_id].status = "CANCELLED"
        if self.raise_after_cancel:
            raise RuntimeError("simulated response loss after cancellation")
        return self.cancel_result_override or SimpleNamespace(
            canceled=(order_id,), not_canceled={}
        )

    def cancel_all(self):
        self.cancel_all_calls += 1
        if not self.leave_open_on_cancel:
            for order in self.orders.values():
                if str(order.status).upper() == "LIVE":
                    order.status = "CANCELLED"
        return SimpleNamespace(
            canceled=tuple(self.orders), not_canceled={}
        )


class EventOrderOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.db_path)
        self.session = FakeSession([
            fake_order("venue-order-1", matched="2"),
        ])
        self.now = datetime.now(timezone.utc)
        self.geo = lambda: {
            "verified": True,
            "blocked": False,
            "country": "IL",
            "region": "TA",
            "checked_at_utc": self.now.isoformat(),
            "ip_redacted": True,
        }
        self.manager = EventOrderOperations(
            session=self.session,
            journal=self.journal,
            enabled=True,
            manually_armed=True,
            dry_run=False,
            geoblock_checker=self.geo,
        )
        self._seed_intent("venue-order-1")

    def _seed_intent(self, exchange_order_id, *, token_id="token-1", side="BUY", price="0.5", size="10"):
        client_order_id = "client-" + exchange_order_id
        intent = create_order_intent(
            client_order_id=client_order_id,
            token_id=token_id,
            side=side,
            limit_price=price,
            size=size,
        )
        self.journal.create_intent(intent)
        self.journal.begin_submission(client_order_id)
        self.journal.record_submission_response(
            client_order_id,
            outcome="accepted",
            exchange_order_id=exchange_order_id,
        )
        return client_order_id

    def test_cancel_one_verifies_terminal_state_and_preserves_partial_fill(self):
        result = self.manager.cancel_order(
            "venue-order-1", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT
        )
        state = self.journal.get_intent("client-venue-order-1")
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["filled_size"], "2")
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["filled_size"], "2")
        self.assertEqual(self.session.cancel_calls, ["venue-order-1"])
        self.assertFalse(result["automatic_retry_permitted"])

    def test_cancel_requires_explicit_confirmation_and_is_disabled_by_default(self):
        result = self.manager.cancel_order("venue-order-1")
        self.assertEqual(result["reason_code"], "explicit_single_cancel_confirmation_required")
        self.assertEqual(self.session.cancel_calls, [])
        disabled = EventOrderOperations(
            session=self.session,
            journal=self.journal,
            geoblock_checker=self.geo,
        ).cancel_order("venue-order-1", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT)
        self.assertEqual(
            disabled["reason_code"],
            "event_order_management_not_explicitly_enabled_and_armed",
        )
        self.assertEqual(self.session.cancel_calls, [])

    def test_cancel_does_not_retry_after_response_loss_but_reconciles_by_read(self):
        self.session.raise_after_cancel = True
        result = self.manager.cancel_order(
            "venue-order-1", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT
        )
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(
            result["reason_code"],
            "cancel_response_lost_but_terminal_state_verified",
        )
        self.assertEqual(self.session.cancel_calls, ["venue-order-1"])
        self.assertEqual(self.journal.get_intent("client-venue-order-1")["status"], "cancelled")

    def test_cancel_refuses_order_not_owned_by_local_journal(self):
        self.session.orders["manual-order"] = fake_order("manual-order")
        result = self.manager.cancel_order(
            "manual-order", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT
        )
        self.assertEqual(result["reason_code"], "ValueError")
        self.assertEqual(self.session.cancel_calls, [])

    def test_order_identity_mismatch_blocks_cancel_before_api_call(self):
        self.session.orders["venue-order-1"].asset_id = "different-token"
        result = self.manager.cancel_order(
            "venue-order-1", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT
        )
        self.assertEqual(result["reason_code"], "ValueError")
        self.assertEqual(self.session.cancel_calls, [])

    def test_cancel_all_verifies_empty_open_order_book_and_reconciles_local_orders(self):
        self.session.orders["venue-order-2"] = fake_order("venue-order-2", matched="1")
        self._seed_intent("venue-order-2")
        result = self.manager.cancel_all(confirmation=CANCEL_ALL_ACKNOWLEDGEMENT)
        self.assertEqual(result["state"], "cancelled_all_verified")
        self.assertEqual(result["open_order_ids_after_cancel"], [])
        self.assertEqual(self.session.cancel_all_calls, 1)
        self.assertEqual(self.journal.get_intent("client-venue-order-2")["status"], "cancelled")

    def test_cancel_all_reports_unknown_orders_for_manual_review(self):
        self.session.orders["manual-order"] = fake_order("manual-order")
        result = self.manager.cancel_all(confirmation=CANCEL_ALL_ACKNOWLEDGEMENT)
        self.assertEqual(result["state"], "manual_review")
        self.assertEqual(result["untracked_order_ids"], ["manual-order"])
        self.assertEqual(result["open_order_ids_after_cancel"], [])

    def test_cancel_all_does_not_claim_success_when_orders_remain_open(self):
        self.session.leave_open_on_cancel = True
        result = self.manager.cancel_all(confirmation=CANCEL_ALL_ACKNOWLEDGEMENT)
        self.assertEqual(result["state"], "cancel_unresolved")
        self.assertEqual(result["open_order_ids_after_cancel"], ["venue-order-1"])
        self.assertEqual(self.session.cancel_all_calls, 1)

    def test_incomplete_open_order_pagination_blocks_cancel_all(self):
        class IncompleteSession(FakeSession):
            def list_open_orders(self):
                return FakePaginator([FakePage([], limit_reached=True)])
        manager = EventOrderOperations(
            session=IncompleteSession([fake_order("x")]),
            journal=self.journal,
            enabled=True,
            manually_armed=True,
            dry_run=False,
            geoblock_checker=self.geo,
        )
        result = manager.cancel_all(confirmation=CANCEL_ALL_ACKNOWLEDGEMENT)
        self.assertEqual(result["state"], "manual_review")
        self.assertEqual(result["reason_code"], "RuntimeError")

    def test_event_order_operation_lock_is_shared_and_stale_lock_is_not_removed(self):
        held = EventOrderOperationLock(self.db_path)
        self.assertTrue(held.acquire())
        blocked = self.manager.cancel_order(
            "venue-order-1", confirmation=CANCEL_ONE_ACKNOWLEDGEMENT
        )
        self.assertEqual(blocked["reason_code"], "event_order_operation_lock_busy_or_stale")
        self.assertEqual(self.session.cancel_calls, [])
        held.release()
        self.assertFalse(held.path.exists())


if __name__ == "__main__":
    unittest.main()
