import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from order_journal import OrderJournal
from polymarket.streams import UserOrderEvent, UserSpec, UserTradeEvent

from user_order_stream import (
    OrderUserEventProcessor,
    UserEventGapError,
    consume_user_order_stream,
)


def order_event(
    *,
    order_id="venue-123",
    event_kind="UPDATE",
    status="MATCHED",
    token_id="token-456",
    side="BUY",
    original_size="12",
    size_matched="3",
    price="0.37",
):
    return UserOrderEvent.model_validate({
        "topic": "user",
        "type": "order",
        "payload": {
            "id": order_id,
            "owner": "account-owner",
            "market": "condition-456",
            "asset_id": token_id,
            "side": side,
            "original_size": original_size,
            "size_matched": size_matched,
            "price": price,
            "type": event_kind,
            "status": status,
        },
    })


def trade_event():
    return UserTradeEvent.model_validate({
        "topic": "user",
        "type": "trade",
        "payload": {
            "id": "trade-123",
            "taker_order_id": "venue-123",
            "market": "condition-456",
            "asset_id": "token-456",
            "side": "BUY",
            "size": "3",
            "price": "0.37",
            "status": "MATCHED",
            "owner": "account-owner",
            "bucket_index": 0,
        },
    })


class FakeHandle:
    def __init__(self, events=(), *, dropped=0, drop_after_first=False):
        self.events = list(events)
        self.dropped = dropped
        self.drop_after_first = drop_after_first
        self.closed = False
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        if self.drop_after_first and self.index == 1:
            self.dropped += 1
        return event

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, handle):
        self.handle = handle
        self.spec = None

    async def subscribe(self, spec):
        self.spec = spec
        return self.handle


class UserOrderStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.database)
        self.journal.create_intent({
            "schema_version": 1,
            "mode": "LOCAL_ORDER_LIFECYCLE_ONLY",
            "client_order_id": "local-123",
            "token_id": "token-456",
            "side": "BUY",
            "limit_price": "0.37",
            "size": "12",
            "status": "prepared",
            "submission_attempts": 0,
            "reconciliation_attempts": 0,
            "exchange_order_id": None,
            "last_reason_code": None,
            "filled_size": "0",
            "automatic_retry_permitted": False,
            "execution": "none",
        })
        self.journal.begin_submission("local-123")
        self.journal.record_submission_response(
            "local-123", outcome="accepted", exchange_order_id="venue-123"
        )
        self.processor = OrderUserEventProcessor(self.journal)

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_cumulative_update_is_persisted_and_duplicate_is_noop(self):
        first = self.processor.process(order_event(size_matched="3"))
        self.assertEqual(first["result"], "updated")
        self.assertEqual(first["status"], "partially_filled")
        self.assertEqual(first["filled_size"], "3")
        event_count = len(self.journal.list_events("local-123"))

        duplicate = self.processor.process(order_event(size_matched="3"))
        self.assertEqual(duplicate["result"], "duplicate_observation")
        self.assertEqual(len(self.journal.list_events("local-123")), event_count)

        later = self.processor.process(order_event(size_matched="7"))
        self.assertEqual(later["result"], "updated")
        self.assertEqual(later["filled_size"], "7")
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "7")

    def test_full_fill_is_terminal(self):
        result = self.processor.process(
            order_event(size_matched="12", status="MATCHED")
        )
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled_size"], "12")

    def test_cancellation_preserves_previous_partial_fill(self):
        self.processor.process(order_event(size_matched="3"))
        result = self.processor.process(
            order_event(event_kind="CANCELLATION", status="CANCELED", size_matched="3")
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["filled_size"], "3")

    def test_venue_order_terms_mismatch_locks_intent_for_manual_review(self):
        result = self.processor.process(order_event(token_id="different-token"))
        self.assertEqual(result["result"], "manual_review")
        state = self.journal.get_intent("local-123")
        self.assertEqual(state["status"], "manual_review")
        self.assertEqual(state["last_reason_code"], "order_terms_mismatch")
        self.assertEqual(state["last_mismatch_fields"], ["token_id"])
        self.assertEqual(state["filled_size"], "0")

    def test_unknown_status_and_untracked_orders_do_not_mutate_journal(self):
        unknown = self.processor.process(order_event(status="NEW_UNKNOWN_STATUS"))
        self.assertEqual(unknown["result"], "ignored_unknown_venue_status")
        untracked = self.processor.process(order_event(order_id="not-in-journal"))
        self.assertEqual(untracked["result"], "untracked_order_event")
        self.assertEqual(self.journal.get_intent("local-123")["status"], "open")

    def test_trade_events_are_not_added_to_order_fills(self):
        result = self.processor.process(trade_event())
        self.assertEqual(result["result"], "ignored_trade_event")
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "0")

    def test_malformed_order_observation_fails_closed(self):
        malformed = SimpleNamespace(
            type="order",
            payload=SimpleNamespace(
                id="venue-123",
                original_size="12",
                size_matched="NaN",
                price="0.37",
                order_event_type="UPDATE",
                status="MATCHED",
                asset_id="token-456",
                side="BUY",
            ),
        )
        result = self.processor.process(malformed)
        self.assertEqual(result["result"], "rejected_malformed_order_observation")
        self.assertEqual(self.journal.get_intent("local-123")["status"], "open")

    def test_consumer_uses_official_user_spec_and_closes_handle(self):
        handle = FakeHandle([trade_event(), order_event(size_matched="2")])
        client = FakeClient(handle)
        import asyncio

        result = asyncio.run(
            consume_user_order_stream(client, self.journal, max_events=2)
        )
        self.assertIsInstance(client.spec, UserSpec)
        self.assertEqual(client.spec.topic, "user")
        self.assertEqual(result["received"], 2)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertTrue(handle.closed)

    def test_consumer_stops_when_sdk_reports_dropped_events(self):
        import asyncio

        handle = FakeHandle([order_event()], drop_after_first=True)
        client = FakeClient(handle)
        with self.assertRaises(UserEventGapError):
            asyncio.run(consume_user_order_stream(client, self.journal))
        self.assertTrue(handle.closed)
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "0")

    def test_consumer_rejects_nonpositive_max_events(self):
        import asyncio

        with self.assertRaisesRegex(ValueError, "positive integer"):
            asyncio.run(
                consume_user_order_stream(
                    FakeClient(FakeHandle()), self.journal, max_events=0
                )
            )


if __name__ == "__main__":
    unittest.main()
