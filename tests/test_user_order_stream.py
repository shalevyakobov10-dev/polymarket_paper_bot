import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from order_journal import OrderJournal
from polymarket.models import ClobTrade
from polymarket.streams import UserOrderEvent, UserSpec, UserTradeEvent

from user_order_stream import (
    OrderUserEventProcessor,
    OrderSnapshotIncompleteError,
    UserEventGapError,
    consume_user_order_stream,
    reconcile_open_orders_snapshot,
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



def clob_trade(
    *,
    trade_id="trade-001",
    taker_order_id="different-taker-order",
    size="3",
    status="MINED",
    asset_id="token-456",
    side="BUY",
    maker_orders=None,
):
    return ClobTrade.model_validate({
        "asset_id": asset_id,
        "bucket_index": 1,
        "fee_rate_bps": "10",
        "id": trade_id,
        "last_update": 1700000010000,
        "maker_address": "0xMAKER",
        "maker_orders": maker_orders or [],
        "market": "0xMARKET",
        "match_time": 1700000000000,
        "outcome": "Yes",
        "owner": "0xOWNER",
        "price": "0.37",
        "side": side,
        "size": size,
        "status": status,
        "taker_order_id": taker_order_id,
        "trader_side": "TAKER",
        "transaction_hash": "0xTX",
    })


def maker_order(order_id, *, amount="3", asset_id="token-456", side="BUY"):
    return {
        "asset_id": asset_id,
        "fee_rate_bps": "10",
        "maker_address": "0xMAKER",
        "matched_amount": amount,
        "order_id": order_id,
        "outcome": "Yes",
        "owner": "0xOWNER",
        "price": "0.37",
        "side": side,
    }


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


class DelayedFakeHandle(FakeHandle):
    def __init__(self, events=()):
        super().__init__(events)
        self.first_wait = True

    async def __anext__(self):
        if self.first_wait:
            self.first_wait = False
            import asyncio
            await asyncio.sleep(0.05)
        return await super().__anext__()


class CancellationSensitiveDelayedHandle(FakeHandle):
    """Fake websocket iterator that fails loudly if an in-flight read is cancelled."""

    def __init__(self, events=()):
        super().__init__(events)
        self.first_wait = True
        self.cancelled_reads = 0

    async def __anext__(self):
        if self.first_wait:
            self.first_wait = False
            import asyncio
            try:
                await asyncio.sleep(0.04)
            except asyncio.CancelledError:
                self.cancelled_reads += 1
                raise RuntimeError("websocket receive was cancelled")
        return await super().__anext__()


class BusyFakeHandle(FakeHandle):
    async def __anext__(self):
        import asyncio
        await asyncio.sleep(0.01)
        return await super().__anext__()


class FakeClient:
    def __init__(
        self, handle, pages=(), trade_pages_by_asset=None,
        positions_by_status=None, collateral_balance=100_000_000,
    ):
        self.handle = handle
        self.pages = list(pages)
        self.trade_pages_by_asset = trade_pages_by_asset or {}
        self.positions_by_status = positions_by_status or {}
        self.collateral_balance = collateral_balance
        self.spec = None

    async def subscribe(self, spec):
        self.spec = spec
        return self.handle

    def list_open_orders(self):
        return FakePaginator(self.pages)

    def list_account_trades(self, *, asset_id):
        return FakePaginator(self.trade_pages_by_asset.get(asset_id, ()))

    async def get_balance_allowance(self, *, asset_type):
        if asset_type != "COLLATERAL":
            raise AssertionError("risk snapshot must read collateral balance only")
        return SimpleNamespace(balance=self.collateral_balance)

    def list_positions(self, *, status, full_history):
        if full_history is not True:
            raise AssertionError("risk snapshot must request full position history")
        return FakePaginator(self.positions_by_status.get(status, ()))



class FakePaginator:
    def __init__(self, pages=()):
        self.pages = list(pages)

    def __aiter__(self):
        async def iterate():
            for page in self.pages:
                yield page
        return iterate()


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
        unknown = self.processor.process(order_event(status="NEW_UNKNOWN_STATUS", size_matched="0"))
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
            consume_user_order_stream(client, self.journal, max_events=2, initial_snapshot=False)
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
            asyncio.run(consume_user_order_stream(client, self.journal, initial_snapshot=False))
        self.assertTrue(handle.closed)
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "0")



    def test_complete_reconciliation_persists_account_risk_snapshot(self):
        open_order = SimpleNamespace(
            id="venue-123", market="condition-456", asset_id="token-456",
            side="BUY", original_size="12", size_matched="5", price="0.37",
        )
        page_obj = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(
                FakeClient(FakeHandle(), [page_obj]), self.journal
            )
        )
        self.assertTrue(result["account_risk_snapshot_complete"])
        self.assertIsNotNone(result["account_risk_snapshot_id"])
        self.assertEqual(result["account_risk_sync_state"]["status"], "success")
        self.assertEqual(
            result["account_risk_sync_state"]["snapshot_id"],
            result["account_risk_snapshot_id"],
        )
        self.assertTrue(result["account_risk_sync_state"]["fresh"])
        latest = self.journal.get_latest_account_risk_snapshot(max_age_seconds=60)
        self.assertEqual(latest["snapshot_id"], result["account_risk_snapshot_id"])
        self.assertEqual(latest["collateral_balance_usd"], "100")
        self.assertEqual(latest["current_open_orders"], 1)
        self.assertEqual(latest["open_buy_commitment_usd"], "2.59")
        self.assertTrue(latest["fresh"])

    def test_account_risk_endpoint_failure_does_not_invent_or_persist_snapshot(self):
        class BrokenRiskClient(FakeClient):
            async def get_balance_allowance(self, *, asset_type):
                raise RuntimeError("simulated balance endpoint outage")

        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(
                BrokenRiskClient(FakeHandle(), []), self.journal
            )
        )
        self.assertFalse(result["account_risk_snapshot_complete"])
        self.assertEqual(result["account_risk_snapshot_error"], "RuntimeError")
        self.assertIsNone(self.journal.get_latest_account_risk_snapshot())
        sync_state = self.journal.get_latest_account_risk_sync_state()
        self.assertEqual(sync_state["status"], "failed")
        self.assertEqual(sync_state["error_code"], "RuntimeError")
        self.assertFalse(sync_state["fresh"])

    def test_rest_open_order_snapshot_refreshes_cumulative_fills(self):
        open_order = SimpleNamespace(
            id="venue-123",
            asset_id="token-456",
            side="BUY",
            original_size="12",
            size_matched="5",
            price="0.37",
            status="LIVE",
        )
        page = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(FakeClient(FakeHandle(), [page]), self.journal)
        )
        self.assertTrue(result["snapshot_complete"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unresolved_missing_orders"], [])
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "5")

    def test_missing_order_in_open_snapshot_is_reported_but_not_inferred_cancelled(self):
        page = SimpleNamespace(items=(), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(FakeClient(FakeHandle(), [page]), self.journal)
        )
        self.assertEqual(result["status"], "snapshot_has_unresolved_absences")
        self.assertEqual(len(result["unresolved_missing_orders"]), 1)
        self.assertEqual(result["unresolved_missing_orders"][0]["status"], "open")
        self.assertEqual(
            result["unresolved_missing_orders"][0]["trade_history_result"],
            "terminal_status_unconfirmed",
        )
        self.assertTrue(result["account_trade_history_checked"])
        self.assertEqual(result["trade_history_unresolved_orders"], 1)
        self.assertEqual(self.journal.get_intent("local-123")["status"], "open")


    def test_missing_fully_filled_order_is_reconciled_from_taker_trade_history(self):
        trade = clob_trade(
            trade_id="trade-full-001",
            taker_order_id="venue-123",
            size="12",
            status="CONFIRMED",
        )
        page = SimpleNamespace(items=(trade,), limit_reached=False, has_more=False)
        result = __import__("asyncio").run(
            reconcile_open_orders_snapshot(
                FakeClient(FakeHandle(), [SimpleNamespace(items=(), limit_reached=False, has_more=False)],
                           {"token-456": [page]}),
                self.journal,
            )
        )
        state = self.journal.get_intent("local-123")
        self.assertEqual(state["status"], "filled")
        self.assertEqual(state["filled_size"], "12")
        self.assertEqual(state["last_reason_code"], "reconciled_from_account_trade_history")
        self.assertEqual(result["unresolved_missing_orders"], [])
        self.assertEqual(result["trade_records_inspected"], 1)

    def test_missing_partially_filled_order_stays_unresolved_after_maker_trade_history(self):
        trade = clob_trade(
            trade_id="trade-maker-001",
            size="3",
            maker_orders=[maker_order("venue-123", amount="3")],
        )
        page = SimpleNamespace(items=(trade,), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(
                FakeClient(FakeHandle(), [SimpleNamespace(items=(), limit_reached=False, has_more=False)],
                           {"token-456": [page]}),
                self.journal,
            )
        )
        state = self.journal.get_intent("local-123")
        self.assertEqual(state["status"], "partially_filled")
        self.assertEqual(state["filled_size"], "3")
        self.assertEqual(state["last_reason_code"], "partial_fill_from_account_trade_history")
        self.assertEqual(len(result["unresolved_missing_orders"]), 1)
        self.assertFalse(result["unresolved_missing_orders"][0]["terminal_status_confirmed"])
        self.assertEqual(
            result["unresolved_missing_orders"][0]["trade_history_result"],
            "partial_fill_confirmed_terminal_unknown",
        )

    def test_failed_trade_is_not_counted_as_a_fill(self):
        trade = clob_trade(
            trade_id="trade-failed-001",
            taker_order_id="venue-123",
            size="12",
            status="FAILED",
        )
        page = SimpleNamespace(items=(trade,), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(
                FakeClient(FakeHandle(), [SimpleNamespace(items=(), limit_reached=False, has_more=False)],
                           {"token-456": [page]}),
                self.journal,
            )
        )
        self.assertEqual(self.journal.get_intent("local-123")["status"], "open")
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "0")
        self.assertEqual(
            result["unresolved_missing_orders"][0]["trade_history_result"],
            "terminal_status_unconfirmed",
        )

    def test_trade_history_asset_or_side_conflict_requires_manual_review(self):
        trade = clob_trade(
            trade_id="trade-conflict-001",
            taker_order_id="venue-123",
            size="2",
            asset_id="different-token",
            side="SELL",
        )
        page = SimpleNamespace(items=(trade,), limit_reached=False, has_more=False)
        import asyncio

        result = asyncio.run(
            reconcile_open_orders_snapshot(
                FakeClient(FakeHandle(), [SimpleNamespace(items=(), limit_reached=False, has_more=False)],
                           {"token-456": [page]}),
                self.journal,
            )
        )
        state = self.journal.get_intent("local-123")
        self.assertEqual(state["status"], "manual_review")
        self.assertEqual(state["last_reason_code"], "order_terms_mismatch")
        self.assertEqual(state["last_mismatch_fields"], ["token_id", "side"])
        self.assertEqual(result["status"], "snapshot_requires_manual_review")
        self.assertEqual(result["trade_history_manual_reviews"], 1)
        self.assertEqual(result["unresolved_missing_orders"], [])

    def test_incomplete_account_trade_pagination_does_not_mutate_missing_order(self):
        trade = clob_trade(
            trade_id="trade-full-002", taker_order_id="venue-123", size="12"
        )
        page = SimpleNamespace(items=(trade,), limit_reached=True, has_more=True)
        import asyncio

        with self.assertRaises(OrderSnapshotIncompleteError):
            asyncio.run(
                reconcile_open_orders_snapshot(
                    FakeClient(FakeHandle(), [SimpleNamespace(items=(), limit_reached=False, has_more=False)],
                               {"token-456": [page]}),
                    self.journal,
                )
            )
        state = self.journal.get_intent("local-123")
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["filled_size"], "0")

    def test_truncated_rest_snapshot_is_not_reported_as_complete(self):
        page = SimpleNamespace(items=(), limit_reached=True, has_more=True)
        import asyncio

        with self.assertRaises(OrderSnapshotIncompleteError):
            asyncio.run(
                reconcile_open_orders_snapshot(FakeClient(FakeHandle(), [page]), self.journal)
            )

    def test_consumer_runs_rest_snapshot_after_subscribing(self):
        open_order = SimpleNamespace(
            id="venue-123",
            asset_id="token-456",
            side="BUY",
            original_size="12",
            size_matched="2",
            price="0.37",
            status="LIVE",
        )
        page = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        handle = FakeHandle([order_event(size_matched="2")])
        client = FakeClient(handle, [page])
        import asyncio

        result = asyncio.run(consume_user_order_stream(client, self.journal, max_events=1))
        self.assertEqual(result["initial_open_orders_snapshot"]["updated"], 1)
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "2")
        self.assertTrue(handle.closed)


    def test_periodic_rest_snapshot_refreshes_quiet_stream(self):
        open_order = SimpleNamespace(
            id="venue-123",
            asset_id="token-456",
            side="BUY",
            original_size="12",
            size_matched="4",
            price="0.37",
            status="LIVE",
        )
        page = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        handle = DelayedFakeHandle([order_event(size_matched="4")])
        client = FakeClient(handle, [page])
        import asyncio

        result = asyncio.run(
            consume_user_order_stream(
                client,
                self.journal,
                max_events=1,
                initial_snapshot=False,
                snapshot_interval_seconds=0.005,
            )
        )
        self.assertEqual(len(result["periodic_open_orders_snapshots"]), 1)
        self.assertEqual(result["periodic_open_orders_snapshots"][0]["updated"], 1)
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "4")
        self.assertTrue(handle.closed)

    def test_periodic_refresh_does_not_cancel_an_inflight_websocket_receive(self):
        open_order = SimpleNamespace(
            id="venue-123",
            asset_id="token-456",
            side="BUY",
            original_size="12",
            size_matched="4",
            price="0.37",
            status="LIVE",
        )
        page = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        handle = CancellationSensitiveDelayedHandle([order_event(size_matched="4")])
        client = FakeClient(handle, [page])
        import asyncio

        result = asyncio.run(
            consume_user_order_stream(
                client,
                self.journal,
                max_events=1,
                initial_snapshot=False,
                snapshot_interval_seconds=0.015,
            )
        )
        self.assertEqual(handle.cancelled_reads, 0)
        self.assertEqual(result["received"], 1)
        self.assertGreaterEqual(len(result["periodic_open_orders_snapshots"]), 1)
        self.assertEqual(self.journal.get_intent("local-123")["filled_size"], "4")
        self.assertTrue(handle.closed)

    def test_periodic_rest_snapshot_is_not_starved_by_continuous_user_events(self):
        open_order = SimpleNamespace(
            id="venue-123",
            asset_id="token-456",
            side="BUY",
            original_size="12",
            size_matched="4",
            price="0.37",
            status="LIVE",
        )
        page = SimpleNamespace(items=(open_order,), limit_reached=False, has_more=False)
        handle = BusyFakeHandle([
            order_event(size_matched="4"),
            order_event(size_matched="4"),
            order_event(size_matched="4"),
        ])
        client = FakeClient(handle, [page])
        import asyncio

        result = asyncio.run(
            consume_user_order_stream(
                client,
                self.journal,
                max_events=3,
                initial_snapshot=False,
                snapshot_interval_seconds=0.015,
            )
        )
        self.assertEqual(result["received"], 3)
        self.assertGreaterEqual(len(result["periodic_open_orders_snapshots"]), 1)
        self.assertTrue(handle.closed)

    def test_consumer_rejects_invalid_periodic_snapshot_interval(self):
        import asyncio

        with self.assertRaisesRegex(ValueError, "positive and finite"):
            asyncio.run(
                consume_user_order_stream(
                    FakeClient(FakeHandle()),
                    self.journal,
                    initial_snapshot=False,
                    snapshot_interval_seconds=0,
                )
            )

        with self.assertRaisesRegex(ValueError, "must not exceed 240"):
            asyncio.run(
                consume_user_order_stream(
                    FakeClient(FakeHandle()),
                    self.journal,
                    initial_snapshot=False,
                    snapshot_interval_seconds=241,
                )
            )

    def test_consumer_rejects_nonpositive_max_events(self):
        import asyncio

        with self.assertRaisesRegex(ValueError, "positive integer"):
            asyncio.run(
                consume_user_order_stream(
                    FakeClient(FakeHandle()), self.journal, max_events=0, initial_snapshot=False
                )
            )


if __name__ == "__main__":
    unittest.main()
