import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import live_market_snapshot_provider as provider


def market(market_id="m1", *, volume=100, outcomes=None, tokens=None):
    return {
        "conditionId": market_id,
        "id": market_id,
        "question": f"Will {market_id} event happen?",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "volume24hr": str(volume),
        "outcomes": outcomes if outcomes is not None else '["No","Yes"]',
        "clobTokenIds": tokens if tokens is not None else '["no-token","yes-token"]',
    }


def book(token, now, *, bid=0.45, ask=0.47, exchange_time=None, usable=True):
    return {
        "token_id": token,
        "observed_at": now.isoformat(),
        "exchange_timestamp": (exchange_time or now).isoformat() if exchange_time is not False else None,
        "best_bid": bid,
        "best_ask": ask,
        "midpoint": (bid + ask) / 2 if bid < ask else None,
        "book_usable": usable,
        "spread_bps": 200.0,
        "bid_depth_usd_top5": 100.0,
        "ask_depth_usd_top5": 120.0,
        "book_hash": f"hash-{token}",
        "source": "official_polymarket_python_sdk",
        "research_only": True,
        "execution": "none",
    }


class LiveMarketSnapshotProviderTests(unittest.TestCase):
    def test_resolves_yes_no_tokens_by_outcome_label_not_array_position(self):
        now = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
        raw = market(
            outcomes='["YES","NO"]',
            tokens='["yes-token","no-token"]',
        )
        books = {
            "yes-token": book("yes-token", now, bid=0.41, ask=0.43),
            "no-token": book("no-token", now, bid=0.57, ask=0.59),
        }
        result = provider.LiveClobMarketSnapshotProvider(
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        ).load(now=now)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["yes_token_id"], "yes-token")
        self.assertEqual(row["no_token_id"], "no-token")
        self.assertAlmostEqual(row["yes_price"], 0.42)
        self.assertAlmostEqual(row["no_price"], 0.58)
        self.assertEqual(row["market_data_mode"], "read_only_live_public")
        self.assertEqual(row["execution"], "none")

    def test_json_encoded_gamma_fields_are_supported_and_malformed_rows_are_skipped(self):
        parsed = provider._binary_yes_no_market(market())
        self.assertEqual(parsed["yes_token_id"], "yes-token")
        self.assertEqual(parsed["no_token_id"], "no-token")
        for bad in (
            market(outcomes='["Yes","Maybe"]'),
            market(tokens='["same-token","same-token"]'),
            market(tokens='["yes-token"]'),
            market(outcomes="not-json"),
            {**market(), "active": False},
            {**market(), "closed": True},
            {**market(), "acceptingOrders": False},
        ):
            self.assertIsNone(provider._binary_yes_no_market(bad))

    def test_only_complete_fresh_usable_yes_and_no_books_are_returned(self):
        now = datetime.now(timezone.utc)
        raw = market()
        stale = book("yes-token", now - timedelta(seconds=60))
        books = {
            "yes-token": stale,
            "no-token": book("no-token", now),
        }
        live = provider.LiveClobMarketSnapshotProvider(
            max_book_age_seconds=20,
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        )
        with self.assertRaisesRegex(RuntimeError, "No active binary markets"):
            live.load(now=now)
        self.assertEqual(live.last_diagnostics["snapshots_returned"], 0)
        self.assertEqual(live.last_diagnostics["markets_skipped"], 1)

    def test_one_failed_or_crossed_outcome_book_skips_whole_market(self):
        now = datetime.now(timezone.utc)
        raw = market()
        books = {
            "yes-token": book("yes-token", now, bid=0.60, ask=0.50),
            "no-token": book("no-token", now),
        }
        live = provider.LiveClobMarketSnapshotProvider(
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        )
        with self.assertRaises(RuntimeError):
            live.load(now=now)
        self.assertEqual(live.last_diagnostics["snapshots_returned"], 0)

    def test_exchange_timestamp_staleness_is_checked_when_sdk_provides_it(self):
        now = datetime.now(timezone.utc)
        raw = market()
        books = {
            "yes-token": book("yes-token", now, exchange_time=now - timedelta(seconds=60)),
            "no-token": book("no-token", now),
        }
        live = provider.LiveClobMarketSnapshotProvider(
            max_book_age_seconds=15,
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        )
        with self.assertRaises(RuntimeError):
            live.load(now=now)
        self.assertEqual(live.last_diagnostics["markets_skipped"], 1)

    def test_when_exchange_time_exists_for_only_one_outcome_provider_fails_closed(self):
        now = datetime.now(timezone.utc)
        raw = market()
        books = {
            "yes-token": book("yes-token", now, exchange_time=False),
            "no-token": book("no-token", now, exchange_time=False),
        }
        # Neither timestamp is available initially; receipt time is the fallback.
        books["yes-token"]["exchange_timestamp"] = None
        live = provider.LiveClobMarketSnapshotProvider(
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        )
        result = live.load(now=now)
        self.assertEqual(len(result), 1)  # no exchange timestamp from either book: receipt time is the fallback
        books["no-token"]["exchange_timestamp"] = now.isoformat()
        with self.assertRaises(RuntimeError):
            live.load(now=now)

    def test_unparseable_exchange_timestamp_does_not_fall_back_to_receipt_time(self):
        now = datetime.now(timezone.utc)
        raw = market()
        books = {
            "yes-token": book("yes-token", now),
            "no-token": book("no-token", now),
        }
        books["yes-token"]["exchange_timestamp"] = "not-a-timestamp"
        live = provider.LiveClobMarketSnapshotProvider(
            market_fetcher=lambda: [raw],
            orderbook_fetcher=lambda rows: books,
        )
        with self.assertRaises(RuntimeError):
            live.load(now=now)
        self.assertEqual(live.last_diagnostics["markets_skipped"], 1)

    def test_orders_markets_by_volume_and_caps_selected_market_count(self):
        now = datetime.now(timezone.utc)
        first = market("low", volume=10, tokens='["low-no","low-yes"]')
        second = market("high", volume=900, tokens='["high-no","high-yes"]')
        books = {}
        for token in ("low-no", "low-yes", "high-no", "high-yes"):
            books[token] = book(token, now)
        requested = []
        def fetch(rows):
            requested.extend(row["token_id"] for row in rows)
            return books
        live = provider.LiveClobMarketSnapshotProvider(
            max_markets=1,
            market_fetcher=lambda: [first, second],
            orderbook_fetcher=fetch,
        )
        result = live.load(now=now)
        self.assertEqual([row["market_id"] for row in result], ["high"])
        self.assertEqual(set(requested), {"high-no", "high-yes"})
        self.assertEqual(live.last_diagnostics["markets_selected"], 1)

    def test_duplicate_market_ids_and_non_binary_markets_are_not_traded(self):
        now = datetime.now(timezone.utc)
        yes_no = market("one")
        duplicate = {**yes_no, "question": "duplicate"}
        non_binary = market("multi", outcomes='["Yes","No","Draw"]', tokens='["a","b","c"]')
        books = {
            "yes-token": book("yes-token", now),
            "no-token": book("no-token", now),
        }
        live = provider.LiveClobMarketSnapshotProvider(
            market_fetcher=lambda: [yes_no, duplicate, non_binary],
            orderbook_fetcher=lambda rows: books,
        )
        result = live.load(now=now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["question"], yes_no["question"])

    def test_gamma_fetch_uses_bounded_public_endpoint_and_parses_catalog(self):
        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, limit):
                self.limit = limit
                return json.dumps([market()]).encode("utf-8")

        response = FakeResponse()
        with patch.object(provider, "urlopen", return_value=response) as request:
            result = provider.fetch_active_gamma_markets(page_size=20, max_pages=2)
        self.assertEqual(len(result), 1)
        args, kwargs = request.call_args
        parsed = urlparse(args[0].full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "gamma-api.polymarket.com")
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])
        self.assertEqual(response.limit, provider.MAX_GAMMA_RESPONSE_BYTES + 1)
        self.assertEqual(kwargs["timeout"], 15.0)

    def test_gamma_fetch_rejects_non_list_response(self):
        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, limit):
                return b'{"markets":[]}'
        with patch.object(provider, "urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(ValueError, "not a list"):
                provider.fetch_active_gamma_markets(page_size=20, max_pages=1)

    def test_provider_from_env_defaults_to_local_and_requires_explicit_public_mode(self):
        with patch.dict("os.environ", {}, clear=True):
            local = provider.provider_from_env(local_path="./data/markets.json", local_max_age_seconds=120)
            self.assertEqual(type(local).__name__, "JsonMarketSnapshotProvider")
        with patch.dict("os.environ", {"PAPER_MARKET_DATA_SOURCE": "polymarket_public"}, clear=True):
            live = provider.provider_from_env(local_path="./data/markets.json", local_max_age_seconds=120)
            self.assertIsInstance(live, provider.LiveClobMarketSnapshotProvider)
            self.assertEqual(live.max_markets, 10)

    def test_unknown_provider_mode_is_rejected(self):
        with patch.dict("os.environ", {"PAPER_MARKET_DATA_SOURCE": "automatic"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PAPER_MARKET_DATA_SOURCE"):
                provider.provider_from_env(local_path="./data/markets.json", local_max_age_seconds=120)


if __name__ == "__main__":
    unittest.main()
