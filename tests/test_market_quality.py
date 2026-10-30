import unittest
from datetime import datetime, timedelta, timezone

from market_quality import evaluate_order_book_quality


class OrderBookQualityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        self.book = {
            "token_id": "token-1",
            "observed_at": self.now.isoformat(),
            "best_bid": 0.48,
            "best_ask": 0.50,
            "spread_bps": 408.16,
            "bid_depth_usd_top5": 24.0,
            "ask_depth_usd_top5": 30.0,
            "book_usable": True,
            "crossed_book": False,
        }

    def evaluate(self, book="default", **overrides):
        settings = {
            "now": self.now,
            "max_age_seconds": 180,
            "max_spread_bps": 500,
            "min_side_depth_usd": 5,
            "require_orderbook": True,
        }
        settings.update(overrides)
        payload = self.book if book == "default" else book
        return evaluate_order_book_quality(payload, **settings)

    def test_accepts_fresh_two_sided_book_with_sufficient_depth(self):
        result = self.evaluate()
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "approved_market_quality")
        self.assertAlmostEqual(result["age_seconds"], 0)

    def test_rejects_missing_book_when_required(self):
        result = self.evaluate(book=None)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "missing_orderbook")

    def test_missing_book_can_be_allowed_only_when_not_required(self):
        result = self.evaluate(book=None, require_orderbook=False)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "orderbook_not_required")

    def test_rejects_stale_book(self):
        book = {**self.book, "observed_at": (self.now - timedelta(seconds=181)).isoformat()}
        self.assertEqual(self.evaluate(book)["reason"], "stale_orderbook")

    def test_rejects_future_timestamp(self):
        book = {**self.book, "observed_at": (self.now + timedelta(seconds=30)).isoformat()}
        self.assertEqual(self.evaluate(book)["reason"], "future_orderbook_timestamp")

    def test_rejects_crossed_or_unusable_book(self):
        self.assertEqual(self.evaluate({**self.book, "crossed_book": True})["reason"], "crossed_orderbook")
        self.assertEqual(self.evaluate({**self.book, "book_usable": False})["reason"], "unusable_two_sided_orderbook")

    def test_rejects_excessive_spread_and_low_depth(self):
        wide = {**self.book, "spread_bps": 900}
        self.assertEqual(self.evaluate(wide)["reason"], "spread_too_wide")
        shallow = {**self.book, "ask_depth_usd_top5": 2}
        self.assertEqual(self.evaluate(shallow)["reason"], "insufficient_ask_depth")

    def test_rejects_missing_or_invalid_timestamp(self):
        self.assertEqual(self.evaluate({**self.book, "observed_at": None})["reason"], "invalid_orderbook_timestamp")
        self.assertEqual(self.evaluate({**self.book, "observed_at": "not-a-date"})["reason"], "invalid_orderbook_timestamp")

    def test_fetch_error_is_not_eligible(self):
        self.assertEqual(self.evaluate({**self.book, "error": "timeout"})["reason"], "orderbook_fetch_failed")


if __name__ == "__main__":
    unittest.main()
