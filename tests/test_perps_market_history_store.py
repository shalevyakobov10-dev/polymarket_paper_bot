import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_history_returns_latest_bounded_rows_in_chronological_order(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                snapshots = []
                base_time = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)
                base_timestamp_ms = int(base_time.timestamp() * 1000)
                for sequence, second in enumerate((0, 30, 60, 90), start=1):
                    snapshot_time = base_time + timedelta(seconds=second)
                    snapshot = sample_snapshot(
                        11, received_at=snapshot_time.isoformat()
                    )
                    timestamp_ms = base_timestamp_ms + second * 1000
                    snapshot["ticker"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["sequence"] = sequence
                    snapshots.append(snapshot)
                store.record_snapshots(snapshots)

                rows = store.history(11, limit=2)

                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    [row["book_sequence"] for row in rows],
                    [3, 4],
                )
                self.assertEqual(
                    [row["book_timestamp_ms"] for row in rows],
                    sorted(row["book_timestamp_ms"] for row in rows),
                )
                self.assertIn("ticker", rows[0])
                self.assertIn("bids", rows[0])

    def test_history_as_of_filters_before_limit_to_prevent_lookahead(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                base = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)
                snapshots = []
                for sequence, seconds in enumerate((0, 30, 60, 90), start=1):
                    timestamp = base + timedelta(seconds=seconds)
                    snapshot = sample_snapshot(11, received_at=timestamp.isoformat())
                    timestamp_ms = int(timestamp.timestamp() * 1000)
                    snapshot["ticker"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["sequence"] = sequence
                    snapshots.append(snapshot)
                store.record_snapshots(snapshots)

                rows = store.history(
                    11,
                    limit=1,
                    as_of_received_at=base + timedelta(seconds=30),
                )

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["book_sequence"], 2)

    def test_snapshots_returns_bounded_chronological_replay_window(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                base = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)
                rows = []
                for seconds in (60, 0, 30):
                    timestamp = base + timedelta(seconds=seconds)
                    snapshot = sample_snapshot(11, received_at=timestamp.isoformat())
                    timestamp_ms = int(timestamp.timestamp() * 1000)
                    snapshot["ticker"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["timestamp_ms"] = timestamp_ms
                    snapshot["book"]["sequence"] = seconds // 30 + 1
                    rows.append(snapshot)
                store.record_snapshots(rows)

                replay_rows = store.snapshots(
                    start_received_at=base,
                    end_received_at=base + timedelta(seconds=30),
                    limit=2,
                )

                self.assertEqual([row["book_sequence"] for row in replay_rows], [1, 2])
                with self.assertRaisesRegex(ValueError, "safety limit"):
                    store.snapshots(limit=1)

    def test_funding_as_of_excludes_settlements_received_after_decision_time(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                settlement_ms = 1795334400000
                store.record_funding_history(
                    11,
                    [{
                        "instrument_id": 11,
                        "timestamp_ms": settlement_ms,
                        "funding_rate": "0.000125",
                    }],
                    received_at="2026-11-22T08:00:10+00:00",
                )
                self.assertIsNone(store.funding_rate_at(
                    11,
                    settlement_ms,
                    as_of_received_at="2026-11-22T08:00:09+00:00",
                ))
                visible = store.funding_rate_at(
                    11,
                    settlement_ms,
                    as_of_received_at="2026-11-22T08:00:10+00:00",
                )
                self.assertEqual(visible["funding_rate"], "0.000125")

    def test_history_rejects_invalid_instrument_id_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                with self.assertRaisesRegex(ValueError, "instrument_id"):
                    store.history(-1)
                with self.assertRaisesRegex(ValueError, "limit"):
                    store.history(11, limit="not-an-integer")

    def test_rejects_json_nan_in_book_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                invalid = sample_snapshot()
                invalid["book"]["bids"] = [{"price": float("nan"), "quantity": "1"}]
                with self.assertRaises(ValueError):
                    store.record_snapshots([invalid])

    def test_persists_and_restores_public_fee_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                snapshot = sample_snapshot()
                snapshot["fees"] = {
                    "taker_fee_rate": "0.0004",
                    "maker_fee_rate": "0.0001",
                    "source": "public_perps_fee_schedule",
                    "retrieved_at": "2026-11-22T08:00:00+00:00",
                }
                store.record_snapshots([snapshot])
                latest = store.latest()[0]
                history = store.history(11)[0]
                self.assertEqual(latest["fees"]["taker_fee_rate"], "0.0004")
                self.assertEqual(history["fees"]["source"], "public_perps_fee_schedule")

    def test_allows_missing_fee_schedule_for_exit_only_market_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                snapshot = sample_snapshot()
                snapshot["fees"] = {}
                result = store.record_snapshots([snapshot])
                self.assertEqual(result["inserted"], 1)
                self.assertEqual(store.latest()[0]["fees"], {})

    def test_records_idempotent_published_funding_settlements(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                rows = [
                    {"instrument_id": 11, "timestamp_ms": 1795334400000, "funding_rate": "0.000125"},
                    {"instrument_id": 11, "timestamp_ms": 1795338000000, "funding_rate": "-0.00005"},
                ]
                first = store.record_funding_history(11, rows)
                repeated = store.record_funding_history(11, rows)
                self.assertEqual(first, {"received": 2, "inserted": 2, "duplicates": 0})
                self.assertEqual(repeated, {"received": 2, "inserted": 0, "duplicates": 2})
                self.assertEqual(
                    store.funding_rate_at(11, 1795334400000)["funding_rate"],
                    "0.000125",
                )
                self.assertIsNone(store.funding_rate_at(11, 1795335000000, tolerance_ms=1000))

    def test_conflicting_funding_revision_is_quarantined_from_engine_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                timestamp_ms = 1795334400000
                initial = store.record_funding_history(
                    11,
                    [{
                        "instrument_id": 11,
                        "timestamp_ms": timestamp_ms,
                        "funding_rate": "0.000125",
                    }],
                    received_at="2026-11-22T08:00:00+00:00",
                )
                self.assertEqual(initial["inserted"], 1)
                self.assertEqual(
                    store.funding_rate_at(11, timestamp_ms)["funding_rate"],
                    "0.000125",
                )

                revised = store.record_funding_history(
                    11,
                    [{
                        "instrument_id": 11,
                        "timestamp_ms": timestamp_ms,
                        "funding_rate": "0.0002",
                    }],
                    received_at="2026-11-22T08:00:30+00:00",
                )
                self.assertEqual(revised, {
                    "received": 1, "inserted": 0, "duplicates": 0, "conflicts": 1
                })
                # Never continue with the old value once the same settlement
                # timestamp has conflicting public observations.
                self.assertIsNone(store.funding_rate_at(11, timestamp_ms))
                conflict = store.db.execute(
                    "SELECT observed_rates_json FROM perps_funding_conflicts "
                    "WHERE instrument_id=? AND funding_timestamp_ms=?",
                    (11, timestamp_ms),
                ).fetchone()
                self.assertEqual(
                    __import__("json").loads(conflict["observed_rates_json"]),
                    ["0.000125", "0.0002"],
                )

                repeated = store.record_funding_history(
                    11,
                    [{
                        "instrument_id": 11,
                        "timestamp_ms": timestamp_ms,
                        "funding_rate": "0.0002",
                    }],
                    received_at="2026-11-22T08:01:00+00:00",
                )
                self.assertEqual(repeated["conflicts"], 1)
                self.assertIsNone(store.funding_rate_at(11, timestamp_ms))

    def test_rejects_mismatched_or_duplicate_funding_settlement_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                with self.assertRaisesRegex(ValueError, "ID mismatch"):
                    store.record_funding_history(11, [{
                        "instrument_id": 12, "timestamp_ms": 1795334400000,
                        "funding_rate": "0.0001",
                    }])
                duplicate = [
                    {"instrument_id": 11, "timestamp_ms": 1795334400000, "funding_rate": "0.0001"},
                    {"instrument_id": 11, "timestamp_ms": 1795334400000, "funding_rate": "0.0002"},
                ]
                with self.assertRaisesRegex(ValueError, "duplicate funding timestamp"):
                    store.record_funding_history(11, duplicate)

    def test_prunes_only_observations_older_than_retention_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                old = sample_snapshot(11, "2026-10-01T00:00:00+00:00")
                new = sample_snapshot(12, "2026-11-22T08:00:00+00:00")
                store.record_snapshots([old, new])
                store.record_funding_history(11, [{
                    "instrument_id": 11,
                    "timestamp_ms": int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
                    "funding_rate": "0.0001",
                }], received_at="2026-10-01T00:00:00+00:00")
                store.record_funding_history(12, [{
                    "instrument_id": 12,
                    "timestamp_ms": int(datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp() * 1000),
                    "funding_rate": "0.0002",
                }], received_at="2026-11-22T00:00:00+00:00")
                result = store.prune_before("2026-11-01T00:00:00+00:00")
                self.assertEqual(result, {
                    "deleted": 2,
                    "snapshot_rows_deleted": 1,
                    "funding_rows_deleted": 1,
                    "funding_conflicts_deleted": 0,
                    "remaining": 1,
                })
                self.assertIsNone(store.funding_rate_at(
                    11, int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
                ))
                self.assertEqual(store.count(), 1)
                self.assertEqual(store.latest()[0]["symbol"], "ASSET12-PERP")

    def test_retention_cutoff_must_be_timezone_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            with PerpsMarketHistoryStore(Path(directory) / "perps.sqlite3") as store:
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    store.prune_before("2026-11-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
