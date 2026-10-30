import unittest

from paper_fills import simulate_orderbook_fill


class PaperFillSimulationTests(unittest.TestCase):
    def setUp(self):
        self.book = {
            "asks_top5": [
                {"price": 0.50, "size": 2},
                {"price": 0.52, "size": 10},
            ],
            "bids_top5": [
                {"price": 0.48, "size": 2},
                {"price": 0.46, "size": 10},
            ],
        }

    def test_buy_walks_ask_levels_and_calculates_vwap(self):
        result = simulate_orderbook_fill(
            self.book, side="buy", target_notional_usd=1.0
        )
        expected_shares = 1.0 / 0.50
        self.assertTrue(result["eligible"])
        self.assertEqual(result["levels_used"], 1)
        self.assertAlmostEqual(result["average_price"], 0.50)
        self.assertAlmostEqual(result["filled_notional_usd"], 1.0)
        self.assertAlmostEqual(result["filled_shares"], expected_shares)
        self.assertEqual(result["reason"], "filled_visible_ask_depth")
        self.assertEqual(result["execution"], "none")

    def test_buy_walks_multiple_ask_levels_when_best_level_is_shallow(self):
        result = simulate_orderbook_fill(
            self.book, side="buy", target_notional_usd=2.5
        )
        expected_shares = 2.0 + (1.5 / 0.52)
        expected_vwap = 2.5 / expected_shares
        self.assertTrue(result["eligible"])
        self.assertEqual(result["levels_used"], 2)
        self.assertAlmostEqual(result["filled_shares"], expected_shares)
        self.assertAlmostEqual(result["average_price"], expected_vwap)

    def test_sell_walks_bid_levels_and_calculates_vwap(self):
        result = simulate_orderbook_fill(
            self.book, side="sell", target_shares=3.0
        )
        expected_proceeds = (2.0 * 0.48) + (1.0 * 0.46)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["levels_used"], 2)
        self.assertAlmostEqual(result["filled_notional_usd"], expected_proceeds)
        self.assertAlmostEqual(result["average_price"], expected_proceeds / 3.0)
        self.assertEqual(result["reason"], "filled_visible_bid_depth")

    def test_partial_depth_is_reported_but_not_accepted_as_a_full_fill(self):
        result = simulate_orderbook_fill(
            {"asks_top5": [{"price": 0.50, "size": 1}], "bids_top5": []},
            side="buy",
            target_notional_usd=2.0,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "insufficient_visible_ask_depth")
        self.assertGreater(result["unfilled_notional_usd"], 0)
        self.assertAlmostEqual(result["filled_shares"], 1.0)
        self.assertAlmostEqual(result["average_price"], 0.50)

    def test_missing_levels_do_not_fabricate_a_fill(self):
        result = simulate_orderbook_fill({}, side="sell", target_shares=2.0)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "orderbook_levels_missing")
        self.assertIsNone(result["average_price"])

    def test_invalid_levels_are_ignored(self):
        result = simulate_orderbook_fill(
            {"asks_top5": [
                {"price": "nan", "size": 2},
                {"price": 0.50, "size": -1},
            ]},
            side="buy",
            target_notional_usd=1.0,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["filled_shares"], 0.0)

    def test_validates_side_size_and_level_limit(self):
        with self.assertRaises(ValueError):
            simulate_orderbook_fill(self.book, side="hold", target_shares=1)
        with self.assertRaises(ValueError):
            simulate_orderbook_fill(self.book, side="buy", target_notional_usd=0)
        with self.assertRaises(ValueError):
            simulate_orderbook_fill(self.book, side="sell", target_shares=1, max_levels=21)


if __name__ == "__main__":
    unittest.main()
