import unittest
from datetime import datetime, timedelta, timezone

from paper_parameter_tuner import postmortem_losers, tune_sentiment_threshold


def trade(index, strength, roi, pnl, reason="dynamic_stop_loss", *, trade_id=None, market_id=None):
    closed = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    row = {
        "status": "CLOSED",
        "closed_at": closed.isoformat(),
        "signal_strength": strength,
        "net_roi": roi,
        "pnl": pnl,
        "exit_reason": reason,
    }
    if trade_id is not None:
        row["trade_id"] = trade_id
    if market_id is not None:
        row["market_id"] = market_id
    return row


class PaperParameterTunerTests(unittest.TestCase):
    def test_stricter_threshold_is_applied_only_after_all_holdout_folds_improve(self):
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
        self.assertEqual(result["folds_scored"], 3)
        self.assertEqual([row["status"] for row in result["fold_reports"]], ["passed"] * 3)
        self.assertGreaterEqual(result["candidate_holdout_net_pnl"], result["baseline_holdout_net_pnl"])
        self.assertTrue(all(row["net_pnl_improvement"] >= 0 for row in result["fold_reports"]))

    def test_candidate_is_rejected_when_one_chronological_holdout_fold_is_bad(self):
        rows = []
        for index in range(90):
            if index % 3 == 0:
                rows.append(trade(index, 0.31, -0.02, -0.20))
            else:
                bad_middle_fold = 72 <= index <= 80
                roi = -0.03 if bad_middle_fold else 0.02
                pnl = -0.30 if bad_middle_fold else 0.20
                rows.append(trade(index, 0.50, roi, pnl, "dynamic_take_profit"))
        result = tune_sentiment_threshold(
            rows, current_threshold=0.30, minimum_closed_trades=60,
            minimum_train_trades=30, minimum_holdout_trades=15,
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["new_threshold"], 0.30)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("fold", result["reason"])
        self.assertEqual(result["fold_reports"][1]["status"], "rejected")

    def test_candidate_is_rejected_when_mean_roi_improves_but_total_net_pnl_falls(self):
        rows = []
        for index in range(90):
            if index >= 63:
                if index % 3 == 0:
                    rows.append(trade(index, 0.31, 0.01, 1.00))
                else:
                    rows.append(trade(index, 0.50, 0.02, 0.10, "dynamic_take_profit"))
            elif index % 3 == 0:
                rows.append(trade(index, 0.31, -0.02, -0.20))
            else:
                rows.append(trade(index, 0.50, 0.02, 0.20, "dynamic_take_profit"))
        result = tune_sentiment_threshold(
            rows, current_threshold=0.30, minimum_closed_trades=60,
            minimum_train_trades=30, minimum_holdout_trades=15,
        )
        self.assertGreater(result["candidate_holdout_mean_roi"], result["baseline_holdout_mean_roi"])
        self.assertLess(result["candidate_holdout_net_pnl"], result["baseline_holdout_net_pnl"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["new_threshold"], 0.30)
        self.assertEqual(result["status"], "rejected")

    def test_insufficient_data_never_changes_threshold(self):
        rows = [trade(i, 0.5, 0.1, 1.0) for i in range(20)]
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertFalse(result["applied"])
        self.assertEqual(result["new_threshold"], 0.3)
        self.assertEqual(result["status"], "insufficient_data")

    def test_timezone_naive_or_invalid_close_timestamps_are_excluded(self):
        rows = [trade(i, 0.5, 0.1, 1.0) for i in range(60)]
        rows[0]["closed_at"] = "2026-10-01T12:00:00"
        rows[1]["closed_at"] = "not-a-timestamp"
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertEqual(result["invalid_timestamp_count"], 2)
        self.assertEqual(result["closed_trade_count"], 58)
        self.assertFalse(result["applied"])

    def test_duplicate_trade_ids_are_excluded_instead_of_double_counted(self):
        rows = [trade(i, 0.5, 0.1, 1.0, trade_id=str(i)) for i in range(60)]
        rows.append(trade(61, 0.5, 0.1, 1.0, trade_id="1"))
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertEqual(result["duplicate_trade_ids_excluded"], 1)
        self.assertEqual(result["closed_trade_count"], 59)
        self.assertFalse(result["applied"])

    def test_duplicate_market_ids_are_excluded_to_avoid_reusing_one_resolution(self):
        rows = [trade(i, 0.5, 0.1, 1.0, market_id=f"market-{i}") for i in range(60)]
        rows.append(trade(61, 0.5, 0.1, 1.0, market_id="market-1"))
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertEqual(result["duplicate_market_ids_excluded"], 1)
        self.assertEqual(result["closed_trade_count"], 59)
        self.assertFalse(result["applied"])

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
