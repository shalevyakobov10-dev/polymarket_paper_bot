import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from perps_historical_replay import PointInTimePerpsHistory, replay_perps_history
from perps_market_history_store import PerpsMarketHistoryStore


BASE = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)


def market_snapshot(sequence, seconds, mark):
    received = BASE + timedelta(seconds=seconds)
    timestamp_ms = int(received.timestamp() * 1000)
    price = float(mark)
    bid = f"{price - 0.01:.2f}"
    ask = f"{price + 0.01:.2f}"
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


class PerpsHistoricalReplayTests(unittest.TestCase):
    def test_replay_does_not_use_future_snapshots_for_earlier_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                store.record_snapshots([
                    market_snapshot(1, 0, "100.00"),
                    market_snapshot(2, 30, "100.20"),
                    market_snapshot(3, 60, "100.40"),
                ])

            result = replay_perps_history(
                history_path,
                engine_options={
                    "initial_equity": "1000",
                    "max_position_notional": "25",
                    "max_total_notional": "75",
                    "max_leverage": "2",
                    "lookback_observations": 3,
                    "minimum_history_span_seconds": 60,
                    "momentum_threshold_bps": "20",
                },
            )

            self.assertEqual(result["snapshots_replayed"], 3)
            self.assertEqual(result["paper_entry_events"], 1)
            self.assertEqual(
                result["first_entry_at"],
                (BASE + timedelta(seconds=60)).isoformat(),
            )
            self.assertEqual(result["portfolio"]["open_positions"], 1)
            self.assertTrue(result["lookahead_protection"]["market_history_filtered_by_received_at"])
            self.assertTrue(result["lookahead_protection"]["query_filters_applied_before_limit"])
            self.assertFalse(result["live_execution_enabled"])
            self.assertFalse(result["live_execution_eligible"])

    def test_entry_start_cutoff_uses_earlier_snapshots_only_to_warm_up_history(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                store.record_snapshots([
                    market_snapshot(1, 0, "100.00"),
                    market_snapshot(2, 30, "100.20"),
                    market_snapshot(3, 60, "100.40"),
                ])

            result = replay_perps_history(
                history_path,
                entry_start_received_at=BASE + timedelta(seconds=90),
                engine_options={
                    "initial_equity": "1000",
                    "max_position_notional": "25",
                    "max_total_notional": "75",
                    "max_leverage": "2",
                    "lookback_observations": 3,
                    "minimum_history_span_seconds": 60,
                    "momentum_threshold_bps": "20",
                },
            )

            self.assertEqual(result["snapshots_replayed"], 3)
            self.assertEqual(result["paper_entry_events"], 0)
            self.assertEqual(result["entry_start_received_at"], (BASE + timedelta(seconds=90)).isoformat())
            self.assertTrue(result["lookahead_protection"]["pre_entry_snapshots_are_warmup_only"])
            self.assertEqual(result["portfolio"]["open_positions"], 0)
            self.assertFalse(result["out_of_sample_validated"])
            self.assertFalse(result["live_execution_eligible"])

    def test_future_exchange_timestamp_is_excluded_even_when_received_early(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                future = market_snapshot(1, 0, "100.00")
                future_ms = int((BASE + timedelta(seconds=5)).timestamp() * 1000)
                future["ticker"]["timestamp_ms"] = future_ms
                future["book"]["timestamp_ms"] = future_ms
                store.record_snapshots([future])

                history = PointInTimePerpsHistory(store, BASE).history(7)

                self.assertEqual(history, [])

    def test_late_funding_settlement_is_hidden_at_earlier_replay_time(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                settlement_ms = int((BASE + timedelta(hours=1)).timestamp() * 1000)
                store.record_funding_history(
                    7,
                    [{
                        "instrument_id": 7,
                        "timestamp_ms": settlement_ms,
                        "funding_rate": "0.0002",
                    }],
                    received_at=BASE + timedelta(hours=1, seconds=15),
                )
                early = PointInTimePerpsHistory(store, BASE + timedelta(hours=1, seconds=10))
                late = PointInTimePerpsHistory(store, BASE + timedelta(hours=1, seconds=15))

                self.assertIsNone(early.funding_rate_at(7, settlement_ms))
                self.assertEqual(
                    late.funding_rate_at(7, settlement_ms)["funding_rate"],
                    "0.0002",
                )

    def test_point_in_time_replay_exposes_funding_conflict_only_after_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                settlement_ms = int((BASE + timedelta(hours=1)).timestamp() * 1000)
                store.record_funding_history(
                    7,
                    [{
                        "instrument_id": 7,
                        "timestamp_ms": settlement_ms,
                        "funding_rate": "0.0002",
                    }],
                    received_at=BASE + timedelta(hours=1, seconds=10),
                )
                store.record_funding_history(
                    7,
                    [{
                        "instrument_id": 7,
                        "timestamp_ms": settlement_ms,
                        "funding_rate": "0.0003",
                    }],
                    received_at=BASE + timedelta(hours=1, seconds=30),
                )

                before_correction = PointInTimePerpsHistory(
                    store, BASE + timedelta(hours=1, seconds=20)
                )
                after_correction = PointInTimePerpsHistory(
                    store, BASE + timedelta(hours=1, seconds=30)
                )

                self.assertIsNone(
                    before_correction.funding_conflict_at(7, settlement_ms)
                )
                self.assertEqual(
                    before_correction.funding_rate_at(7, settlement_ms)["funding_rate"],
                    "0.0002",
                )
                conflict = after_correction.funding_conflict_at(7, settlement_ms)
                self.assertIsNotNone(conflict)
                self.assertEqual(conflict["observed_rates"], ["0.0002", "0.0003"])
                self.assertIsNone(after_correction.funding_rate_at(7, settlement_ms))

    def test_future_funding_event_is_hidden_even_when_received_early(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path) as store:
                future_ms = int((BASE + timedelta(hours=1)).timestamp() * 1000)
                store.record_funding_history(
                    7,
                    [{
                        "instrument_id": 7,
                        "timestamp_ms": future_ms,
                        "funding_rate": "0.0002",
                    }],
                    received_at=BASE,
                )
                history = PointInTimePerpsHistory(store, BASE)
                self.assertIsNone(history.funding_rate_at(7, future_ms))

    def test_replay_rejects_naive_window_and_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "perps-history.sqlite3"
            with PerpsMarketHistoryStore(history_path):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    replay_perps_history(history_path, start_received_at="2026-11-22T08:00:00")
                with self.assertRaisesRegex(ValueError, "no archived"):
                    replay_perps_history(history_path)


if __name__ == "__main__":
    unittest.main()
