import tempfile
import unittest
from pathlib import Path

from perps_market_history_store import PerpsMarketHistoryStore


def sample_snapshot(instrument_id=11, received_at="2026-11-22T08:00:00+00:00"):
    symbol = f"ASSET{instrument_id}-PERP"
    return {
        "instrument": {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "category": "crypto",
            "max_leverage": "10",
            "risk_tiers": [{"lower_bound": "0", "max_leverage": "10"}],
        },
        "ticker": {
            "instrument_id": instrument_id,
            "timestamp_ms": 1795334400000,
            "index_price": "100.0",
            "mark_price": "100.1",
            "last_price": "100.0",
            "mid_price": "100.05",
            "open_interest": "12",
            "funding_rate": "0.0001",
        },
        "book": {
            "instrument_id": instrument_id,
            "timestamp_ms": 1795334400000,
            "sequence": 5,
            "best_bid": "100.0",
            "best_ask": "100.1",
            "bids": [{"price": "100.0", "quantity": "3"}],
            "asks": [{"price": "100.1", "quantity": "4"}],
        },
        "received_at": received_at,
        "research_only": True,
        "execution": "none",
    }


class PerpsMarketHistoryStoreTests(unittest.TestCase):
    def test_persists_snapshots_and_returns_latest_instrument_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perps.sqlite3"
            with PerpsMarketHistoryStore(path) as store:
                result = store.record_snapshots([
                    sample_snapshot(11),
                    sample_snapshot(12),
                ])
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(result["total_snapshots"], 2)
                latest = store.latest()
                self.assertEqual(len(latest), 2)
                self.assertEqual({row["symbol"] for row in latest}, {
                    "ASSET11-PERP", "ASSET12-PERP"
                })
                self.assertEqual(latest[0]["instrument"]["instrument_id"], 11)
                self.assertEqual(latest[0]["bids"][0]["price"], "100.0")
            self.assertTrue(path.exists())

    def test_deduplicates_same_instrument_book_timestamp_and_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                snapshot = sample_snapshot()
                first = store.record_snapshots([snapshot])
                duplicate = store.record_snapshots([snapshot])
                self.assertEqual(first["inserted"], 1)
                self.assertEqual(duplicate["inserted"], 0)
                self.assertEqual(duplicate["duplicates"], 1)
                self.assertEqual(store.count(), 1)

    def test_keeps_distinct_exchange_book_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                first = sample_snapshot()
                second = sample_snapshot()
                second["book"] = {**second["book"], "sequence": 6}
                result = store.record_snapshots([first, second])
                self.assertEqual(result["inserted"], 2)

    def test_rejects_mismatched_market_ids_and_missing_book_side(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                mismatch = sample_snapshot()
                mismatch["book"] = {**mismatch["book"], "instrument_id": 99}
                with self.assertRaisesRegex(ValueError, "identifiers do not match"):
                    store.record_snapshots([mismatch])
                empty = sample_snapshot()
                empty["book"] = {**empty["book"], "asks": []}
                with self.assertRaisesRegex(ValueError, "both sides"):
                    store.record_snapshots([empty])

    def test_rejects_naive_capture_time_and_malformed_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                naive = sample_snapshot(received_at="2026-11-22T08:00:00")
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    store.record_snapshots([naive])
                with self.assertRaisesRegex(ValueError, "snapshot"):
                    store.record_snapshots(["not-a-snapshot"])

    def test_rejects_json_nan_in_book_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                invalid = sample_snapshot()
                invalid["book"]["bids"] = [{"price": float("nan"), "quantity": "1"}]
                with self.assertRaises(ValueError):
                    store.record_snapshots([invalid])


if __name__ == "__main__":
    unittest.main()
