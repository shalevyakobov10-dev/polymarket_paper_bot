import unittest
from unittest.mock import patch

import market_stream


class MarketStreamTests(unittest.TestCase):
    def test_normalizes_full_book_and_rejects_crossed_midpoint(self):
        rows = market_stream.normalize_market_message({
            "event_type": "book",
            "asset_id": "token-1",
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "12"}],
            "timestamp": "123",
        }, "2026-10-30T12:00:00+00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["midpoint"], 0.5)
        self.assertEqual(rows[0]["spread"], 0.04)
        self.assertEqual(rows[0]["execution"], "none")

        crossed = market_stream.normalize_market_message({
            "event_type": "book",
            "asset_id": "token-2",
            "bids": [{"price": "0.55", "size": "10"}],
            "asks": [{"price": "0.50", "size": "10"}],
        })
        self.assertTrue(crossed[0]["crossed_book"])
        self.assertIsNone(crossed[0]["midpoint"])

    def test_normalizes_incremental_price_changes_as_independent_events(self):
        rows = market_stream.normalize_market_message({
            "event_type": "price_change",
            "timestamp": "456",
            "price_changes": [
                {"asset_id": "token-a", "price": "0.4", "size": "25", "side": "BUY", "best_bid": "0.4", "best_ask": "0.42"},
                {"asset_id": "token-b", "price": "0.6", "size": "0", "side": "SELL", "best_bid": "0.58", "best_ask": "0.6"},
            ],
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["spread"], 0.02)
        self.assertEqual(rows[1]["token_id"], "token-b")

    def test_ignores_malformed_and_unknown_messages(self):
        self.assertEqual(market_stream.normalize_market_message("PONG"), [])
        self.assertEqual(market_stream.normalize_market_message({"event_type": "unknown"}), [])
        self.assertEqual(market_stream.normalize_market_message({"event_type": "price_change", "price_changes": "bad"}), [])

    def test_token_selection_is_bounded_and_deduplicated(self):
        rows = [{"token_id": "a"}, {"token_id": "a"}, {"token_id": "b"}, {"token_id": "c"}]
        with patch.dict("os.environ", {"MARKET_WS_MAX_TOKENS": "2"}):
            self.assertEqual(market_stream.MarketStreamCollector.select_tokens(rows), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
