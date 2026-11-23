import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

import live_market_snapshot_provider as live


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def market():
    return {
        "conditionId": "condition-123",
        "question": "Will the example bill pass by December?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.51", "0.49"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "volume24hr": "12000",
        "liquidity": "85000",
        "category": "Politics",
        "slug": "example-bill-passes",
        "endDate": "2026-12-31T23:59:00Z",
        "active": True,
        "closed": False,
    }


def book(token, bid, ask, observed, exchange=None):
    return {
        "token_id": token,
        "best_bid": bid,
        "best_ask": ask,
        "observed_at": observed,
        "exchange_timestamp": exchange,
        "book_usable": True,
        "crossed_book": False,
        "source": "official_polymarket_python_sdk",
        "execution": "none",
    }


class LiveMarketSnapshotProviderTests(unittest.TestCase):
    def test_default_market_scan_is_bounded_to_30_markets_and_80_websocket_tokens(self):
        with patch.dict("os.environ", {
            "PAPER_LIVE_MARKETS_LIMIT": "30",
            "MARKET_WS_MAX_TOKENS": "80",
        }, clear=False):
            provider = live.LiveClobMarketSnapshotProvider()
            self.assertEqual(provider.market_limit, 30)
            self.assertLessEqual(provider.market_limit * 2, 80)

    def test_market_limit_rejects_unsafe_values(self):
        for value in (0, -1, 51, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    live.LiveClobMarketSnapshotProvider(market_limit=value)

    def test_maps_gamma_yes_no_tokens_to_fresh_clob_midpoints(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])) as request:
            with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                provider = live.LiveClobMarketSnapshotProvider(market_limit=1)
                rows = provider.load(now=now)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["market_id"], "condition-123")
        self.assertEqual(row["yes_token_id"], "yes-token")
        self.assertEqual(row["no_token_id"], "no-token")
        self.assertAlmostEqual(row["yes_price"], 0.51)
        self.assertAlmostEqual(row["no_price"], 0.49)
        self.assertEqual(row["market_source"], "public_gamma_api")
        self.assertEqual(row["book_source"], "official_polymarket_python_sdk")
        self.assertEqual(row["market_category"], "politics")
        self.assertEqual(row["market_slug"], "example-bill-passes")
        self.assertEqual(row["market_end_date"], "2026-12-31T23:59:00Z")
        self.assertEqual(row["liquidity"], 85000.0)
        self.assertTrue(row["research_only"])
        self.assertEqual(row["execution"], "none")
        request.assert_called_once()
        fetch.assert_called_once_with([{"token_id": "yes-token"}, {"token_id": "no-token"}])

    def test_withholds_market_when_either_book_is_stale(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        stale = "2026-11-07T09:50:00+00:00"
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, fresh),
            "no-token": book("no-token", 0.48, 0.50, stale),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_withholds_freshly_received_book_with_stale_exchange_timestamp(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        stale_exchange = "2026-11-07T09:59:00+00:00"
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, fresh, stale_exchange),
            "no-token": book("no-token", 0.48, 0.50, fresh, fresh),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_withholds_future_or_malformed_exchange_timestamp(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        for invalid in ("2026-11-07T10:00:10+00:00", "2026-11-07T10:00:00", "not-a-timestamp"):
            with self.subTest(exchange_timestamp=invalid):
                books = {
                    "yes-token": book("yes-token", 0.50, 0.52, fresh, invalid),
                    "no-token": book("no-token", 0.48, 0.50, fresh, fresh),
                }
                with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
                    with patch.object(live, "fetch_order_books", return_value=books):
                        rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
                self.assertEqual(rows, [])

    def test_accepts_fresh_exchange_timestamps_and_preserves_book_evidence(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        exchange = "2026-11-07T09:59:50+00:00"
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, fresh, exchange),
            "no-token": book("no-token", 0.48, 0.50, fresh, fresh),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["yes_book"]["exchange_timestamp"], exchange)

    def test_withholds_market_when_one_side_is_not_usable(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        yes = book("yes-token", 0.50, 0.52, stamp)
        no = book("no-token", 0.48, 0.50, stamp)
        no["book_usable"] = False
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value={"yes-token": yes, "no-token": no}):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_ignores_non_binary_or_ambiguous_outcome_markets(self):
        invalid = market()
        invalid["outcomes"] = '["Up", "Down"]'
        with patch.object(live, "urlopen", return_value=FakeResponse([invalid])):
            with patch.object(live, "fetch_order_books") as fetch:
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(
                    now=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
                )
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    def test_multi_page_gamma_discovery_diversifies_categories_and_deduplicates_ids(self):
        from urllib.parse import parse_qs, urlparse

        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        pages = []
        books = {}
        for page_index, category in enumerate(("politics", "sports", "economics")):
            page_rows = []
            for index in range(20):
                row = market()
                market_index = page_index * 20 + index
                row["conditionId"] = f"condition-{market_index}"
                row["question"] = f"Will example {category} event {index} happen?"
                row["category"] = category
                row["clobTokenIds"] = json.dumps([f"yes-{market_index}", f"no-{market_index}"])
                row["volume24hr"] = str(300_000 - market_index)
                page_rows.append(row)
                books[f"yes-{market_index}"] = book(f"yes-{market_index}", 0.50, 0.52, stamp)
                books[f"no-{market_index}"] = book(f"no-{market_index}", 0.48, 0.50, stamp)
            pages.append(page_rows)

        # The same market can appear at a page boundary; it must be de-duplicated.
        duplicate = dict(pages[0][0])
        pages[1][0] = duplicate
        requests = []

        def fake_gamma(request, timeout=15):
            requests.append(request.full_url)
            offset = int(parse_qs(urlparse(request.full_url).query)["offset"][0])
            page_index = offset // 20
            return FakeResponse(pages[page_index])

        with patch.dict("os.environ", {
            "PAPER_LIVE_MARKETS_LIMIT": "6",
            "PAPER_GAMMA_PAGE_SIZE": "20",
            "PAPER_GAMMA_DISCOVERY_PAGES": "3",
            "PAPER_MARKET_CATEGORY_MAX": "2",
        }, clear=False):
            with patch.object(live, "urlopen", side_effect=fake_gamma):
                with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                    provider = live.LiveClobMarketSnapshotProvider()
                    rows = provider.load(now=now)

        self.assertEqual(len(requests), 3)
        offsets = [
            int(parse_qs(urlparse(url).query)["offset"][0])
            for url in requests
        ]
        self.assertEqual(offsets, [0, 20, 40])
        self.assertEqual(provider.last_discovery_pages_attempted, 3)
        self.assertEqual(provider.last_discovery_pages_succeeded, 3)
        self.assertEqual(provider.last_discovery_pages_failed, 0)
        self.assertEqual(provider.last_discovered_market_count, 59)
        self.assertEqual(provider.last_selected_market_count, 6)
        self.assertEqual(provider.last_orderbook_token_requests, 12)
        self.assertEqual(provider.last_usable_market_count, 6)
        self.assertEqual(
            {row["market_category"] for row in rows},
            {"politics", "sports", "economics"},
        )
        self.assertEqual(
            [row["market_category"] for row in rows].count("politics"), 2
        )
        self.assertEqual(
            [row["market_category"] for row in rows].count("sports"), 2
        )
        self.assertEqual(
            [row["market_category"] for row in rows].count("economics"), 2
        )
        fetch.assert_called_once()
        self.assertEqual(len(fetch.call_args.args[0]), 12)

    def test_failed_gamma_page_does_not_discard_successful_pages(self):
        from urllib.parse import parse_qs, urlparse

        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        pages = []
        books = {}
        for page_index in (0, 2):
            page_rows = []
            for index in range(20):
                market_index = page_index * 20 + index
                row = market()
                row["conditionId"] = f"condition-{market_index}"
                row["clobTokenIds"] = json.dumps([f"yes-{market_index}", f"no-{market_index}"])
                row["volume24hr"] = str(100_000 - market_index)
                page_rows.append(row)
                books[f"yes-{market_index}"] = book(f"yes-{market_index}", 0.50, 0.52, stamp)
                books[f"no-{market_index}"] = book(f"no-{market_index}", 0.48, 0.50, stamp)
            pages.append(page_rows)

        def fake_gamma(request, timeout=15):
            offset = int(parse_qs(urlparse(request.full_url).query)["offset"][0])
            if offset == 20:
                raise TimeoutError("simulated page timeout")
            return FakeResponse(pages[0] if offset == 0 else pages[1])

        with patch.dict("os.environ", {
            "PAPER_LIVE_MARKETS_LIMIT": "10",
            "PAPER_GAMMA_PAGE_SIZE": "20",
            "PAPER_GAMMA_DISCOVERY_PAGES": "3",
            "PAPER_MARKET_CATEGORY_MAX": "3",
        }, clear=False):
            with patch.object(live, "urlopen", side_effect=fake_gamma):
                with patch.object(live, "fetch_order_books", return_value=books):
                    provider = live.LiveClobMarketSnapshotProvider()
                    rows = provider.load(now=now)

        self.assertEqual(provider.last_discovery_pages_attempted, 3)
        self.assertEqual(provider.last_discovery_pages_succeeded, 2)
        self.assertEqual(provider.last_discovery_pages_failed, 1)
        self.assertEqual(provider.last_discovered_market_count, 40)
        self.assertEqual(len(rows), 10)
        self.assertEqual(provider.last_usable_market_count, 10)

    def test_default_watchlist_and_orderbook_batch_cover_thirty_binary_markets(self):
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        markets = []
        books = {}
        for index in range(32):
            row = market()
            row["conditionId"] = f"condition-{index}"
            row["question"] = f"Will example event {index} happen?"
            row["clobTokenIds"] = json.dumps([f"yes-{index}", f"no-{index}"])
            row["volume24hr"] = str(100_000 - index)
            markets.append(row)
            books[f"yes-{index}"] = book(f"yes-{index}", 0.50, 0.52, stamp)
            books[f"no-{index}"] = book(f"no-{index}", 0.48, 0.50, stamp)

        with patch.dict("os.environ", {
            "PAPER_LIVE_MARKETS_LIMIT": "30",
            "PAPER_GAMMA_PAGE_SIZE": "200",
            "ORDERBOOK_TOKENS_PER_POLL": "60",
        }):
            with patch.object(live, "urlopen", return_value=FakeResponse(markets)) as request:
                with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                    provider = live.LiveClobMarketSnapshotProvider()
                    rows = provider.load(now=now)

        self.assertEqual(len(rows), 30)
        self.assertEqual(provider.last_discovered_market_count, 32)
        self.assertEqual(provider.last_selected_market_count, 30)
        self.assertEqual(provider.last_orderbook_token_requests, 60)
        self.assertEqual(provider.last_usable_market_count, 30)
        self.assertEqual(
            [row["market_id"] for row in rows],
            [f"condition-{index}" for index in range(30)],
        )
        fetch.assert_called_once()
        self.assertEqual(len(fetch.call_args.args[0]), 60)
        query = request.call_args.args[0].full_url
        self.assertIn("limit=200", query)

    def test_supports_gamma_json_string_fields_and_deduplicates_market_ids(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market(), market()])):
            with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                rows = live.LiveClobMarketSnapshotProvider(market_limit=5).load(now=now)
        self.assertEqual(len(rows), 1)
        fetch.assert_called_once_with([{"token_id": "yes-token"}, {"token_id": "no-token"}])


if __name__ == "__main__":
    unittest.main()
