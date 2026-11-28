import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import market_data


class OrderBookTests(unittest.TestCase):
    def test_calculates_midpoint_spread_depth_and_imbalance(self):
        result = market_data.summarize_order_book({
            "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "50"}],
            "asks": [{"price": "0.52", "size": "80"}, {"price": "0.53", "size": "40"}],
        }, token_id="yes-token", observed_at="2026-10-30T12:00:00+00:00")
        self.assertAlmostEqual(result["midpoint"], 0.5)
        self.assertAlmostEqual(result["spread"], 0.04)
        self.assertAlmostEqual(result["bid_depth_usd_top5"], 71.5)
        self.assertAlmostEqual(result["ask_depth_usd_top5"], 62.8)
        self.assertAlmostEqual(result["depth_imbalance_top5"], (71.5 - 62.8) / 134.3, places=5)
        self.assertTrue(result["book_usable"])
        self.assertEqual(result["bids_top5"], [{"price": 0.48, "size": 100.0}, {"price": 0.47, "size": 50.0}])
        self.assertEqual(result["asks_top5"], [{"price": 0.52, "size": 80.0}, {"price": 0.53, "size": 40.0}])
        self.assertEqual(result["execution"], "none")

    def test_rejects_crossed_book_for_midpoint(self):
        result = market_data.summarize_order_book({
            "bids": [{"price": "0.55", "size": "10"}],
            "asks": [{"price": "0.50", "size": "10"}],
        }, token_id="t")
        self.assertTrue(result["crossed_book"])
        self.assertIsNone(result["midpoint"])
        self.assertFalse(result["book_usable"])

    def test_ignores_invalid_levels_and_handles_empty_sides(self):
        result = market_data.summarize_order_book({
            "bids": [{"price": "nan", "size": "3"}, {"price": "0.4", "size": "-1"}],
            "asks": [],
        }, token_id="t")
        self.assertIsNone(result["best_bid"])
        self.assertIsNone(result["spread"])
        self.assertFalse(result["book_usable"])

    def test_fetch_order_book_uses_official_public_sdk_without_credentials(self):
        levels = lambda rows: tuple(
            SimpleNamespace(price=price, size=size) for price, size in rows
        )
        sdk_book = SimpleNamespace(
            asset_id="yes-token",
            timestamp=datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc),
            bids=levels([("0.47", "50"), ("0.48", "100")]),
            asks=levels([("0.53", "40"), ("0.52", "80")]),
            hash="book-hash",
            min_order_size="5",
            tick_size="0.01",
            neg_risk=False,
        )
        sdk_client = MagicMock()
        sdk_client.__enter__.return_value = sdk_client
        sdk_client.get_order_book.return_value = sdk_book
        with patch.object(market_data, "PublicClient", return_value=sdk_client) as factory:
            result = market_data.fetch_order_book("yes-token")

        factory.assert_called_once_with()
        sdk_client.get_order_book.assert_called_once_with(token_id="yes-token")
        sdk_client.__exit__.assert_called_once()
        self.assertEqual(result["source"], "official_polymarket_python_sdk")
        self.assertEqual(result["sdk_version"], "0.16.0")
        self.assertEqual(result["best_bid"], 0.48)
        self.assertEqual(result["best_ask"], 0.52)
        self.assertEqual(result["exchange_timestamp"], "2026-11-04T08:00:00+00:00")
        self.assertEqual(result["book_hash"], "book-hash")
        self.assertTrue(result["book_usable"])
        self.assertEqual(result["execution"], "none")

    def test_fetch_order_book_preserves_string_exchange_timestamp(self):
        sdk_book = SimpleNamespace(
            asset_id="yes-token",
            timestamp="2026-11-04T08:00:00+00:00",
            bids=(SimpleNamespace(price="0.48", size="10"),),
            asks=(SimpleNamespace(price="0.52", size="10"),),
            hash="book-hash",
            min_order_size="5",
            tick_size="0.01",
            neg_risk=False,
        )
        sdk_client = MagicMock()
        sdk_client.__enter__.return_value = sdk_client
        sdk_client.get_order_book.return_value = sdk_book
        with patch.object(market_data, "PublicClient", return_value=sdk_client):
            result = market_data.fetch_order_book("yes-token")
        self.assertEqual(result["exchange_timestamp"], "2026-11-04T08:00:00+00:00")

    def test_fetch_order_book_rejects_blank_token_before_network(self):
        with patch.object(market_data, "PublicClient") as factory:
            with self.assertRaisesRegex(ValueError, "token_id is required"):
                market_data.fetch_order_book("  ")
        factory.assert_not_called()

    def test_batch_fetch_can_be_disabled(self):
        with patch.dict("os.environ", {"ORDERBOOK_TOKENS_PER_POLL": "0"}):
            with patch.object(market_data, "fetch_order_book") as fetch:
                self.assertEqual(market_data.fetch_order_books([{"token_id": "a"}]), {})
                fetch.assert_not_called()

    def test_batch_fetch_is_bounded_and_deduplicated(self):
        rows = [{"token_id": "a"}, {"token_id": "a"}, {"token_id": "b"}, {"token_id": "c"}]
        with patch.dict("os.environ", {
            "ORDERBOOK_TOKENS_PER_POLL": "2",
            "ORDERBOOK_FETCH_WORKERS": "2",
        }):
            with patch.object(market_data, "fetch_order_book", side_effect=lambda token: {"token_id": token}) as fetch:
                result = market_data.fetch_order_books(rows)
        self.assertEqual(list(result), ["a", "b"])
        self.assertEqual(fetch.call_count, 2)

    def test_batch_fetch_isolates_failures_without_dropping_other_books(self):
        def fetch(token):
            if token == "b":
                raise TimeoutError("slow CLOB response")
            return {"token_id": token, "book_usable": True}

        rows = [{"token_id": "a"}, {"token_id": "b"}, {"token_id": "c"}]
        with patch.dict("os.environ", {
            "ORDERBOOK_TOKENS_PER_POLL": "3",
            "ORDERBOOK_FETCH_WORKERS": "2",
        }):
            with patch.object(market_data, "fetch_order_book", side_effect=fetch):
                result = market_data.fetch_order_books(rows)
        self.assertEqual(list(result), ["a", "b", "c"])
        self.assertTrue(result["a"]["book_usable"])
        self.assertFalse(result["b"]["book_usable"])
        self.assertIn("TimeoutError", result["b"]["error"])
        self.assertTrue(result["c"]["book_usable"])

    def test_batch_fetch_caps_worker_setting(self):
        rows = [{"token_id": str(index)} for index in range(10)]
        captured = {}
        original_executor = market_data.ThreadPoolExecutor

        def executor_spy(*, max_workers, thread_name_prefix):
            captured["max_workers"] = max_workers
            return original_executor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

        with patch.dict("os.environ", {
            "ORDERBOOK_TOKENS_PER_POLL": "10",
            "ORDERBOOK_FETCH_WORKERS": "999",
        }):
            with patch.object(market_data, "ThreadPoolExecutor", side_effect=executor_spy):
                with patch.object(market_data, "fetch_order_book", side_effect=lambda token: {"token_id": token}):
                    result = market_data.fetch_order_books(rows)
        self.assertEqual(captured["max_workers"], 8)
        self.assertEqual(len(result), 10)


if __name__ == "__main__":
    unittest.main()
