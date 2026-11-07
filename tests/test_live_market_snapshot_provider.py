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

    def read(self, limit=None):
        self.last_read_limit = limit
        raw = json.dumps(self.payload).encode("utf-8")
        return raw if limit is None else raw[:limit]


def market():
    return {
        "conditionId": "condition-123",
        "question": "Will the example bill pass by December?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.51", "0.49"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "volume24hr": "12000",
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


    def test_exchange_timestamps_must_be_present_for_both_books_or_neither(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp, None),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_rejects_timezone_naive_local_receipt_timestamps(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, "2026-11-07T10:00:00"),
            "no-token": book("no-token", 0.48, 0.50, now.isoformat()),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_rejects_locked_crossed_and_out_of_range_quotes(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        invalid_quotes = (
            (0.52, 0.52, 0.48, 0.50),
            (0.53, 0.52, 0.48, 0.50),
            (0.50, 0.52, 0.48, 1.0),
            (0.50, 0.52, -0.01, 0.50),
        )
        for quotes in invalid_quotes:
            with self.subTest(quotes=quotes):
                yes_bid, yes_ask, no_bid, no_ask = quotes
                books = {
                    "yes-token": book("yes-token", yes_bid, yes_ask, stamp),
                    "no-token": book("no-token", no_bid, no_ask, stamp),
                }
                with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
                    with patch.object(live, "fetch_order_books", return_value=books):
                        rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
                self.assertEqual(rows, [])

    def test_rejects_timezone_naive_now_argument(self):
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                live.LiveClobMarketSnapshotProvider(market_limit=1).load(
                    now=datetime(2026, 11, 7, 10, 0)
                )

    def test_sorts_binary_markets_by_volume_before_applying_market_limit(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        low = market()
        low.update({
            "conditionId": "low-volume",
            "clobTokenIds": '["low-yes", "low-no"]',
            "volume24hr": "10",
        })
        high = market()
        high.update({
            "conditionId": "high-volume",
            "clobTokenIds": '["high-yes", "high-no"]',
            "volume24hr": "9000",
        })
        books = {
            "high-yes": book("high-yes", 0.50, 0.52, stamp),
            "high-no": book("high-no", 0.48, 0.50, stamp),
        }
        provider = live.LiveClobMarketSnapshotProvider(market_limit=1)
        with patch.object(provider, "_discover_markets", return_value=[low, high]):
            with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                rows = provider.load(now=now)
        self.assertEqual([row["market_id"] for row in rows], ["high-volume"])
        fetch.assert_called_once_with([
            {"token_id": "high-yes"}, {"token_id": "high-no"}
        ])
        self.assertEqual(provider.last_diagnostics["markets_selected"], 1)

    def test_gamma_discovery_uses_bounded_pagination_and_response_reads(self):
        first_page = [
            {
                "conditionId": f"market-{index}",
                "question": f"Question {index}",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": [f"yes-{index}", f"no-{index}"],
            }
            for index in range(20)
        ]
        second_page = [{"conditionId": "last", "question": "Last market"}]
        first = FakeResponse(first_page)
        second = FakeResponse(second_page)
        with patch.object(live, "urlopen", side_effect=[first, second]) as request:
            results = live.fetch_active_gamma_markets(page_size=20, max_pages=5)
        self.assertEqual(len(results), 21)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(first.last_read_limit, live.MAX_GAMMA_RESPONSE_BYTES + 1)
        self.assertEqual(second.last_read_limit, live.MAX_GAMMA_RESPONSE_BYTES + 1)
        first_url = request.call_args_list[0].args[0].full_url
        second_url = request.call_args_list[1].args[0].full_url
        self.assertIn("offset=0", first_url)
        self.assertIn("offset=20", second_url)

    def test_gamma_discovery_rejects_oversized_and_non_list_responses(self):
        class OversizedResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, limit):
                return b"x" * limit

        with patch.object(live, "urlopen", return_value=OversizedResponse()):
            with self.assertRaisesRegex(ValueError, "3 MB"):
                live.fetch_active_gamma_markets(page_size=20, max_pages=1)

        with patch.object(live, "urlopen", return_value=FakeResponse({"markets": []})):
            with self.assertRaisesRegex(ValueError, "must be a list"):
                live.fetch_active_gamma_markets(page_size=20, max_pages=1)

    def test_binary_market_parser_rejects_extra_outcomes_and_disabled_orderbooks(self):
        invalid_markets = []
        extra = market()
        extra["outcomes"] = '["Yes", "No", "Draw"]'
        extra["clobTokenIds"] = '["yes-token", "no-token", "draw-token"]'
        invalid_markets.append(extra)
        disabled = market()
        disabled["enableOrderBook"] = False
        invalid_markets.append(disabled)
        not_accepting = market()
        not_accepting["acceptingOrders"] = False
        invalid_markets.append(not_accepting)

        provider = live.LiveClobMarketSnapshotProvider(market_limit=5)
        with patch.object(provider, "_discover_markets", return_value=invalid_markets):
            with patch.object(live, "fetch_order_books") as fetch:
                rows = provider.load(now=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    def test_snapshot_keeps_legacy_source_contract_and_adds_diagnostics(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp),
        }
        provider = live.LiveClobMarketSnapshotProvider(market_limit=1)
        with patch.object(provider, "_discover_markets", return_value=[market()]):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = provider.load(now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_source"], "public_gamma_api")
        self.assertEqual(rows[0]["book_source"], "official_polymarket_python_sdk")
        self.assertEqual(rows[0]["source"], "gamma_plus_public_clob_orderbook")
        self.assertEqual(rows[0]["market_data_mode"], "read_only_live_public")
        self.assertEqual(provider.last_diagnostics["snapshots_returned"], 1)
        self.assertEqual(provider.last_diagnostics["orderbooks_usable"], 2)
        self.assertTrue(rows[0]["research_only"])
        self.assertEqual(rows[0]["execution"], "none")

    def test_gamma_environment_bounds_are_validated_not_silently_clamped(self):
        with patch.dict("os.environ", {"PAPER_GAMMA_PAGE_SIZE": "201"}):
            with self.assertRaisesRegex(ValueError, "PAPER_GAMMA_PAGE_SIZE"):
                live.LiveClobMarketSnapshotProvider()


if __name__ == "__main__":
    unittest.main()
