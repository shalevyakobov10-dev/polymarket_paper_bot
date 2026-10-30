import unittest
from unittest.mock import patch

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
        self.assertAlmostEqual(result["ask_depth_usd_top5"], 62.0)
        self.assertAlmostEqual(result["depth_imbalance_top5"], (71.5 - 62.0) / 133.5)
        self.assertTrue(result["book_usable"])
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

    def test_batch_fetch_is_bounded_and_deduplicated(self):
        rows = [{"token_id": "a"}, {"token_id": "a"}, {"token_id": "b"}, {"token_id": "c"}]
        with patch.dict("os.environ", {"ORDERBOOK_TOKENS_PER_POLL": "2"}):
            with patch.object(market_data, "fetch_order_book", side_effect=lambda token: {"token_id": token}) as fetch:
                with patch.object(market_data.time, "sleep"):
                    result = market_data.fetch_order_books(rows)
        self.assertEqual(list(result), ["a", "b"])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
