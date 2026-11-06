import unittest

from paper_parameter_tuner import postmortem_losers, tune_sentiment_threshold


def trade(index, strength, roi, pnl, reason="dynamic_stop_loss"):
    return {
        "status": "CLOSED",
        "closed_at": f"2026-10-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
        "signal_strength": strength,
        "net_roi": roi,
        "pnl": pnl,
        "exit_reason": reason,
    }


class PaperParameterTunerTests(unittest.TestCase):
    def test_stricter_threshold_is_applied_only_after_holdout_improves(self):
        rows = []
        for index in range(90):
            if index % 3 == 0:
                rows.append(trade(index, 0.31, -0.02, -0.20))
            else:
                rows.append(trade(index, 0.50, 0.02, 0.20, "dynamic_take_profit"))
        result = tune_sentiment_threshold(
            rows, current_threshold=0.30, minimum_closed_trades=60,
            minimum_train_trades=30, minimum_holdout_trades=15,
        )
        self.assertTrue(result["applied"])
        self.assertGreater(result["new_threshold"], 0.30)
        self.assertEqual(result["status"], "validated_for_paper_only")

    def test_insufficient_data_never_changes_threshold(self):
        rows = [trade(i, 0.5, 0.1, 1.0) for i in range(20)]
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertFalse(result["applied"])
        self.assertEqual(result["new_threshold"], 0.3)
        self.assertEqual(result["status"], "insufficient_data")

    def test_postmortem_groups_losing_trades_by_exit_reason(self):
        rows = [trade(1, 0.4, -0.1, -1, "dynamic_stop_loss"),
                trade(2, 0.5, -0.2, -2, "time_limit"),
                trade(3, 0.6, 0.1, 1, "dynamic_take_profit")]
        result = postmortem_losers(rows)
        self.assertEqual(result["losing_trade_count"], 2)
        self.assertEqual(result["losses_by_exit_reason"]["dynamic_stop_loss"], 1)
        self.assertAlmostEqual(result["mean_loser_pnl"], -1.5)


if __name__ == "__main__":
    unittest.main()
