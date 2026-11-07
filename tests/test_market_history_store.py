import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_history_store import MarketHistoryStore


def sample_snapshot(**overrides):
    row = {
        "market_id": "condition-1",
        "question": "Will the example event happen?",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "observed_at": "2026-11-07T10:00:00+00:00",
        "yes_bid": 0.41,
        "yes_ask": 0.43,
        "no_bid": 0.57,
        "no_ask": 0.59,
        "volume_24h": 1200,
        "yes_book": {
            "observed_at": "2026-11-07T10:00:00+00:00",
            "bids_top5": [{"price": 0.41, "size": 20}],
            "asks_top5": [{"price": 0.43, "size": 25}],
            "source": "official_polymarket_python_sdk",
        },
        "no_book": {
            "observed_at": "2026-11-07T10:00:00+00:00",
            "bids_top5": [{"price": 0.57, "size": 20}],
            "asks_top5": [{"price": 0.59, "size": 25}],
            "source": "official_polymarket_python_sdk",
        },
        "market_source": "public_gamma_api",
        "book_source": "official_polymarket_python_sdk",
    }
    row.update(overrides)
    return row


class MarketHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = MarketHistoryStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_appends_bid_ask_and_book_levels_and_can_read_back(self):
        result = self.store.append_snapshots(
            [sample_snapshot()],
            captured_at=datetime(2026, 11, 7, 10, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["total_snapshots"], 1)
        rows = self.store.history("condition-1")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["yes_mid"], 0.42)
        self.assertAlmostEqual(rows[0]["no_mid"], 0.58)
        self.assertEqual(rows[0]["yes_book"]["bids_top5"][0]["size"], 20)
        self.assertEqual(rows[0]["book_source"], "official_polymarket_python_sdk")

    def test_exact_duplicate_snapshot_is_ignored(self):
        row = sample_snapshot()
        first = self.store.append_snapshots([row])
        second = self.store.append_snapshots([row])
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.store.count(), 1)

    def test_changed_book_at_same_observation_time_is_kept(self):
        self.store.append_snapshots([sample_snapshot()])
        changed = sample_snapshot(yes_bid=0.42, yes_ask=0.44)
        result = self.store.append_snapshots([changed])
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.store.count(), 2)

    def test_batch_is_validated_before_any_rows_are_written(self):
        bad = sample_snapshot(yes_bid=0.7, yes_ask=0.6)
        with self.assertRaisesRegex(ValueError, "Crossed"):
            self.store.append_snapshots([sample_snapshot(), bad])
        self.assertEqual(self.store.count(), 0)

    def test_rejects_naive_or_invalid_timestamps(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.store.append_snapshots([sample_snapshot(observed_at="2026-11-07T10:00:00")])
        self.assertEqual(self.store.count(), 0)

    def test_rejects_non_finite_and_out_of_range_prices(self):
        for value in (float("nan"), float("inf"), 0, 1, -0.1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.append_snapshots([sample_snapshot(yes_bid=value)])
        self.assertEqual(self.store.count(), 0)

    def test_history_filters_by_time_and_bounds_limit(self):
        self.store.append_snapshots([sample_snapshot()])
        later = sample_snapshot(
            observed_at="2026-11-07T11:00:00+00:00",
            yes_bid=0.44, yes_ask=0.46, no_bid=0.54, no_ask=0.56,
        )
        self.store.append_snapshots([later])
        self.assertEqual(len(self.store.history("condition-1", start_at="2026-11-07T10:30:00+00:00")), 1)
        self.assertEqual(len(self.store.history("condition-1", limit=1)), 1)

    def test_database_schema_is_versioned_and_future_versions_are_rejected(self):
        version = self.store.db.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        self.store.db.execute("PRAGMA user_version=999")
        self.store.db.commit()
        self.store.close()
        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            MarketHistoryStore(self.path)


if __name__ == "__main__":
    unittest.main()
