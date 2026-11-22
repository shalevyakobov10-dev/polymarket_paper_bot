from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from perps_market_history_store import PerpsMarketHistoryStore
from perps_walk_forward_evaluation import evaluate_perps_walk_forward

BASE = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)


def market_snapshot(sequence, seconds, mark):
    received = BASE + timedelta(seconds=seconds)
    timestamp_ms = int(received.timestamp() * 1000)
    price = float(mark)
    bid, ask = f"{price - 0.01:.2f}", f"{price + 0.01:.2f}"
    return {
        "instrument": {
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "category": "crypto",
            "close_only": False,
            "isolated_only": True,
            "min_notional": "10",
            "max_market_notional": "100000",
            "max_leverage": "20",
            "quantity_decimals": 4,
            "price_decimals": 2,
            "price_bounds": "0.2",
            "liquidation_fee": "0.01",
            "funding_interval": "1h",
            "risk_tiers": [
                {"lower_bound": "0", "max_leverage": "20"},
                {"lower_bound": "100000", "max_leverage": "10"},
            ],
        },
        "ticker": {
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "index_price": str(mark),
            "mark_price": str(mark),
            "last_price": str(mark),
            "mid_price": str(mark),
            "open_interest": "100",
            "funding_rate": "0.0001",
            "next_funding_ms": timestamp_ms + 3_600_000,
            "timestamp_ms": timestamp_ms,
        },
        "book": {
            "instrument_id": 7,
            "timestamp_ms": timestamp_ms,
            "sequence": sequence,
            "best_bid": bid,
            "best_ask": ask,
            "bids": [{"price": bid, "quantity": "10"}],
            "asks": [{"price": ask, "quantity": "10"}],
        },
        "fees": {
            "taker_fee_rate": "0.0004",
            "maker_fee_rate": "0.0001",
            "tier_assumption": "highest_published_rate_no_volume_discount",
            "source": "public_perps_fee_schedule",
            "retrieved_at": received.isoformat(),
        },
        "received_at": received.isoformat(),
        "research_only": True,
        "execution": "none",
    }


class PerpsWalkForwardEvaluationTests(unittest.TestCase):
    def _history_database(self, directory: str, count: int = 24) -> Path:
        path = Path(directory) / "perps-history.sqlite3"
        snapshots = [
            market_snapshot(index + 1, index * 30, f"{100 + index * 0.1:.2f}")
            for index in range(count)
        ]
        with PerpsMarketHistoryStore(path) as store:
            store.record_snapshots(snapshots)
        return path

    @staticmethod
    def _profitable_fold_report(**kwargs):
        entry_start = kwargs.get("entry_start_received_at")
        end = kwargs.get("end_received_at")
        return {
            "paper_entry_events": 3,
            "snapshots_replayed": 20,
            "performance": {
                "closed_trades": 2,
                "net_closed_pnl_after_fees_and_funding": "2.00",
                "winning_net_pnl": "3.00",
                "losing_net_pnl_abs": "1.00",
                "profit_factor": "3.0000",
                "net_expectancy_per_closed_trade": "1.000000",
            },
            "portfolio": {
                "open_positions": 0,
                "pending_exit_positions": 0,
                "peak_drawdown_pct": "2.00",
            },
            "entry_start_received_at": entry_start.isoformat(),
            "last_received_at": end.isoformat(),
        }

    def test_uses_contiguous_time_folds_and_fresh_ledgers_but_never_marks_oos_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self._history_database(directory, count=24)
            with patch(
                "perps_walk_forward_evaluation.replay_perps_history",
                side_effect=self._profitable_fold_report,
            ) as replay:
                report = evaluate_perps_walk_forward(
                    database,
                    folds=3,
                    warmup_fraction=0.25,
                    minimum_closed_trades_per_fold=2,
                    minimum_total_closed_trades=6,
                )

            self.assertEqual(report["fold_count"], 3)
            self.assertTrue(report["walk_forward_consistency_passed"])
            self.assertEqual(report["validation_state"], "walk_forward_consistency_passed_exploratory")
            self.assertEqual(report["aggregate"]["closed_trades"], 6)
            self.assertTrue(report["entry_policy_frozen_across_folds"])
            self.assertTrue(report["folds_use_fresh_paper_ledgers"])
            self.assertFalse(report["out_of_sample_validated"])
            self.assertFalse(report["live_execution_eligible"])
            self.assertFalse(report["live_execution_enabled"])
            self.assertFalse(report["order_submission_allowed"])
            self.assertEqual(replay.call_count, 3)

            folds = report["fold_reports"]
            self.assertTrue(all(row["fold_consistency_passed"] for row in folds))
            self.assertEqual(
                [row["test_start"] for row in folds],
                sorted(row["test_start"] for row in folds),
            )
            self.assertEqual(
                [row["test_end"] for row in folds[:-1]],
                [row["test_end"] for row in folds[:-1]],
            )
            calls = replay.call_args_list
            self.assertEqual(
                [call.kwargs["entry_start_received_at"].isoformat() for call in calls],
                [row["test_start"] for row in folds],
            )
            self.assertTrue(all(call.kwargs["start_received_at"] == BASE for call in calls))
            self.assertTrue(all(call.kwargs["end_received_at"] <= report["last_capture_at"] for call in calls))

    def test_insufficient_closed_trade_sample_does_not_pass_consistency_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self._history_database(directory, count=24)
            thin = self._profitable_fold_report()
            thin["performance"]["closed_trades"] = 1
            with patch(
                "perps_walk_forward_evaluation.replay_perps_history",
                return_value=thin,
            ):
                report = evaluate_perps_walk_forward(
                    database,
                    folds=3,
                    minimum_closed_trades_per_fold=2,
                    minimum_total_closed_trades=6,
                )
            self.assertEqual(report["validation_state"], "insufficient_sample")
            self.assertFalse(report["walk_forward_consistency_passed"])
            self.assertFalse(report["out_of_sample_validated"])

    def test_unresolved_positions_at_fold_end_block_fold_consistency(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self._history_database(directory, count=24)
            unresolved = self._profitable_fold_report()
            unresolved["portfolio"]["open_positions"] = 1
            with patch(
                "perps_walk_forward_evaluation.replay_perps_history",
                return_value=unresolved,
            ):
                report = evaluate_perps_walk_forward(
                    database,
                    folds=3,
                    minimum_closed_trades_per_fold=2,
                    minimum_total_closed_trades=6,
                )
            self.assertFalse(report["walk_forward_consistency_passed"])
            self.assertTrue(all(
                "positions_unresolved_at_test_window_end" in row["blocked_reasons"]
                for row in report["fold_reports"]
            ))

    def test_empty_or_too_short_history_fails_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self._history_database(directory, count=4)
            with self.assertRaisesRegex(ValueError, "not enough distinct"):
                evaluate_perps_walk_forward(database, folds=3)

    def test_naive_capture_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = self._history_database(directory, count=24)
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                evaluate_perps_walk_forward(
                    database,
                    start_received_at="2026-11-22T08:00:00",
                )


if __name__ == "__main__":
    unittest.main()
