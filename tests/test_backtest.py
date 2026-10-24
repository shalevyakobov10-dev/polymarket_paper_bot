import unittest

from backtest import list_field, run_backtest
from paper_trading import PaperConfig


class BacktestTests(unittest.TestCase):
    def test_list_field_accepts_json_and_rejects_bad_values(self):
        self.assertEqual(list_field('["a", "b"]'), ["a", "b"])
        self.assertEqual(list_field({"not": "a list"}), [])
        self.assertEqual(list_field("invalid"), [])

    def test_backtest_uses_chronological_train_test_split(self):
        prices = [0.40, 0.44, 0.50, 0.48, 0.52, 0.46, 0.50, 0.47, 0.51, 0.48, 0.53, 0.50]
        history = [
            {
                "timestamp": 1_800_000_000 + i * 3600,
                "token_id": "token-a",
                "market_id": "market-a",
                "question": "Synthetic test market",
                "outcome": "Yes",
                "observed_price": price,
            }
            for i, price in enumerate(prices)
        ]
        report = run_backtest(history, PaperConfig(), train_ratio=0.7)
        self.assertEqual(report["data"]["split_method"], "chronological")
        self.assertLess(
            report["in_sample"]["window_end_utc"],
            report["out_of_sample"]["window_start_utc"],
        )
        self.assertEqual(report["in_sample"]["metrics"]["cash"] >= 0, True)
        self.assertEqual(report["out_of_sample"]["metrics"]["cash"] >= 0, True)
        self.assertEqual(report["out_of_sample"]["open_positions_at_end"] <= 5, True)
        self.assertIn("does not guarantee future performance", " ".join(report["limitations"]).lower())

    def test_backtest_rejects_too_few_points(self):
        history = [
            {"timestamp": 1_800_000_000 + i, "token_id": "t", "observed_price": 0.5}
            for i in range(5)
        ]
        with self.assertRaises(ValueError):
            run_backtest(history, PaperConfig())

    def test_backtest_requires_valid_chronological_ratio(self):
        with self.assertRaises(ValueError):
            run_backtest([], PaperConfig(), train_ratio=0.2)


if __name__ == "__main__":
    unittest.main()
