import unittest

from paper_orderbook_execution import simulate_buy, simulate_sell


class PaperOrderbookExecutionTests(unittest.TestCase):
    def test_buy_sweeps_asks_and_computes_weighted_average_price(self):
        result = simulate_buy(
            [{"price": 0.40, "size": 2}, {"price": 0.42, "size": 20}],
            quote_budget=5.0,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["levels_used"], 2)
        self.assertAlmostEqual(result["quote_filled"], 5.0)
        self.assertAlmostEqual(result["average_price"], 5.0 / (2 + 4.2 / 0.42), places=8)
        self.assertGreater(result["average_price"], 0.40)
        self.assertGreater(result["price_impact_bps"], 0)

    def test_buy_rejects_insufficient_visible_ask_depth_without_faking_full_fill(self):
        result = simulate_buy([{"price": 0.40, "size": 1}], quote_budget=2.0)
        self.assertFalse(result["complete"])
        self.assertAlmostEqual(result["quote_filled"], 0.40)
        self.assertAlmostEqual(result["unfilled_quote"], 1.60)

    def test_sell_sweeps_bids_and_reports_price_impact(self):
        result = simulate_sell(
            [{"price": 0.50, "size": 2}, {"price": 0.48, "size": 20}],
            shares=5,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["levels_used"], 2)
        self.assertAlmostEqual(result["quote_proceeds"], 2 * 0.50 + 3 * 0.48)
        self.assertAlmostEqual(result["average_price"], 0.488)
        self.assertGreater(result["price_impact_bps"], 0)

    def test_sell_rejects_insufficient_visible_bid_depth(self):
        result = simulate_sell([{"price": 0.50, "size": 2}], shares=3)
        self.assertFalse(result["complete"])
        self.assertAlmostEqual(result["shares_filled"], 2)
        self.assertAlmostEqual(result["unfilled_shares"], 1)

    def test_rejects_missing_or_invalid_book_levels(self):
        for raw in (None, [], [{"price": float("nan"), "size": 1}], [{"price": 0.5, "size": 0}]):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    simulate_buy(raw, quote_budget=1)

    def test_results_are_explicitly_simulated_only(self):
        buy = simulate_buy([{"price": 0.4, "size": 10}], quote_budget=1)
        sell = simulate_sell([{"price": 0.4, "size": 10}], shares=1)
        self.assertEqual(buy["execution"], "simulated_only")
        self.assertEqual(sell["execution"], "simulated_only")
        self.assertTrue(buy["research_only"] and sell["research_only"])


if __name__ == "__main__":
    unittest.main()
