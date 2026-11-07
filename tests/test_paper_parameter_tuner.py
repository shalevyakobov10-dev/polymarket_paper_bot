import unittest
from datetime import datetime, timedelta, timezone

from paper_parameter_tuner import postmortem_losers, tune_sentiment_threshold


def trade(index, strength, roi, pnl, reason="dynamic_stop_loss", *, trade_id="auto", market_id="auto"):
    closed = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    row = {
        "status": "CLOSED",
        "closed_at": closed.isoformat(),
        "signal_strength": strength,
        "net_roi": roi,
        "pnl": pnl,
        "exit_reason": reason,
    }
    if trade_id == "auto":
        trade_id = f"trade-{index}"
    if market_id == "auto":
        market_id = f"market-{index}"
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
        self.assertFalse(result["live_execution_eligible"])
        self.assertEqual(result["real_money_execution"], "not_implemented")
        self.assertGreater(result["new_threshold"], 0.30)
        self.assertEqual(result["status"], "validated_for_paper_only")
        self.assertEqual(result["folds_scored"], 3)
        self.assertEqual([row["status"] for row in result["fold_reports"]], ["passed"] * 3)
        self.assertGreaterEqual(result["candidate_holdout_net_pnl"], result["baseline_holdout_net_pnl"])
        self.assertTrue(all(row["net_pnl_improvement"] >= 0 for row in result["fold_reports"]))
        self.assertLess(result["train_time_end"], result["holdout_time_start"])
        self.assertEqual(len(result["training_candidate_scores"]), 4)
        self.assertTrue(all(row["training_trade_count"] >= 30 for row in result["training_candidate_scores"]))
        self.assertEqual(len(result["input_dataset_sha256"]), 64)
        self.assertEqual(len(result["evaluation_run_sha256"]), 64)

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
        self.assertEqual(result["duplicate_trade_rows_excluded"], 2)
        self.assertEqual(result["closed_trade_count"], 59)
        self.assertFalse(result["applied"])

    def test_duplicate_market_ids_are_excluded_to_avoid_reusing_one_resolution(self):
        rows = [trade(i, 0.5, 0.1, 1.0, market_id=f"market-{i}") for i in range(60)]
        rows.append(trade(61, 0.5, 0.1, 1.0, market_id="market-1"))
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertEqual(result["duplicate_market_ids_excluded"], 1)
        self.assertEqual(result["duplicate_market_rows_excluded"], 2)
        self.assertEqual(result["closed_trade_count"], 59)
        self.assertFalse(result["applied"])

    def test_missing_trade_or_market_identity_is_excluded_fail_closed(self):
        rows = [trade(i, 0.5, 0.1, 1.0) for i in range(60)]
        rows[0].pop("trade_id")
        rows[1].pop("market_id")
        result = tune_sentiment_threshold(rows, current_threshold=0.3)
        self.assertEqual(result["missing_trade_id_rows_excluded"], 1)
        self.assertEqual(result["missing_market_id_rows_excluded"], 1)
        self.assertEqual(result["closed_trade_count"], 58)
        self.assertFalse(result["applied"])

    def test_dataset_fingerprint_is_order_independent_and_changes_with_economic_data(self):
        rows = [trade(i, 0.5, 0.01, 0.1) for i in range(60)]
        forward = tune_sentiment_threshold(rows, current_threshold=0.3)
        reversed_result = tune_sentiment_threshold(list(reversed(rows)), current_threshold=0.3)
        changed_rows = [dict(row) for row in rows]
        changed_rows[10]["pnl"] = 0.11
        changed = tune_sentiment_threshold(changed_rows, current_threshold=0.3)
        self.assertEqual(forward["input_dataset_sha256"], reversed_result["input_dataset_sha256"])
        self.assertNotEqual(forward["input_dataset_sha256"], changed["input_dataset_sha256"])
        self.assertNotEqual(forward["evaluation_run_sha256"], changed["evaluation_run_sha256"])
        different_config = tune_sentiment_threshold(rows, current_threshold=0.31)
        self.assertNotEqual(forward["evaluation_run_sha256"], different_config["evaluation_run_sha256"])
        self.assertEqual(forward["input_dataset_sha256"], forward["input_dataset_sha256"].lower())
        self.assertEqual(len(forward["evaluation_run_sha256"]), 64)
        self.assertEqual(forward["evaluation_run_sha256"], reversed_result["evaluation_run_sha256"])

    def test_dataset_fingerprint_normalizes_equivalent_timezone_offsets(self):
        rows = [trade(i, 0.5, 0.01, 0.1) for i in range(60)]
        utc_result = tune_sentiment_threshold(rows, current_threshold=0.3)
        offset_rows = [dict(row) for row in rows]
        utc_stamp = datetime.fromisoformat(offset_rows[10]["closed_at"])
        offset_rows[10]["closed_at"] = utc_stamp.astimezone(
            timezone(timedelta(hours=2))
        ).isoformat()
        offset_result = tune_sentiment_threshold(offset_rows, current_threshold=0.3)
        self.assertEqual(utc_result["input_dataset_sha256"], offset_result["input_dataset_sha256"])
        self.assertEqual(utc_result["evaluation_run_sha256"], offset_result["evaluation_run_sha256"])

    def test_sample_configuration_rejects_booleans_floats_and_strings(self):
        for value in (True, 2.5, "60"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "minimum_closed_trades must be an integer"):
                    tune_sentiment_threshold(
                        [], current_threshold=0.3, minimum_closed_trades=value
                    )

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
