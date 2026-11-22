import json
import tempfile
import unittest
from pathlib import Path

from perps_market_worker import read_perps_worker_status, run_worker


class FakeStopEvent:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        return True


class FakeProvider:
    def __init__(self, snapshots=None, error=None):
        self.snapshots = snapshots or [{
            "instrument": {
                "instrument_id": 1, "symbol": "BTC-PERP", "category": "crypto"
            },
            "ticker": {
                "instrument_id": 1, "timestamp_ms": 1795334400000,
                "index_price": "100", "mark_price": "100",
                "last_price": "100", "mid_price": "100",
                "open_interest": "10", "funding_rate": "0.0001",
            },
            "book": {
                "instrument_id": 1, "timestamp_ms": 1795334400000,
                "sequence": 1, "best_bid": "99", "best_ask": "101",
                "bids": [{"price": "99", "quantity": "1"}],
                "asks": [{"price": "101", "quantity": "1"}],
            },
            "received_at": "2026-11-22T08:00:00+00:00",
        }]
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.snapshots


class FakeStore:
    def __init__(self):
        self.rows = []
        self.closed = False

    def count(self):
        return len(self.rows)

    def record_snapshots(self, rows):
        self.rows.extend(rows)
        return {
            "inserted": len(rows), "total_snapshots": len(self.rows)
        }

    def close(self):
        self.closed = True


class PerpsMarketWorkerTests(unittest.TestCase):
    def test_worker_records_market_data_and_publishes_healthy_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = FakeStore()
            result = run_worker(
                provider=FakeProvider(),
                store=store,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "healthy")
            self.assertEqual(raw["markets_in_last_snapshot"], 1)
            self.assertEqual(raw["history_rows"], 1)
            self.assertFalse(raw["live_execution_enabled"])
            self.assertEqual(raw["execution"], "none")
            self.assertEqual(len(store.rows), 1)

    def test_worker_marks_degraded_and_returns_failure_when_data_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = FakeStore()
            result = run_worker(
                provider=FakeProvider(error=RuntimeError("public feed unavailable")),
                store=store,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 1)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "degraded")
            self.assertIn("public feed unavailable", raw["last_error"])
            self.assertEqual(raw["consecutive_failures"], 1)
            self.assertEqual(store.rows, [])

    def test_status_reader_drops_unexpected_and_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text(json.dumps({
                "schema_version": 1,
                "enabled": True,
                "worker_state": "healthy",
                "last_heartbeat_at": "2026-11-22T08:00:00+00:00",
                "last_success_at": "2026-11-22T08:00:00+00:00",
                "consecutive_failures": 0,
                "markets_in_last_snapshot": 3,
                "snapshots_inserted_total": 4,
                "history_rows": 4,
                "poll_interval_seconds": 30,
                "POLYMARKET_PRIVATE_KEY": "never-expose",
                "unknown_field": "discard",
            }), encoding="utf-8")
            result = read_perps_worker_status(target)
            self.assertTrue(result["enabled"])
            self.assertEqual(result["markets_in_last_snapshot"], 3)
            self.assertFalse(result["live_execution_enabled"])
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", result)
            self.assertNotIn("unknown_field", result)

    def test_missing_or_invalid_status_is_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing.json"
            missing = read_perps_worker_status(target)
            self.assertEqual(missing["worker_state"], "unknown")
            target.write_text("{broken", encoding="utf-8")
            invalid = read_perps_worker_status(target)
            self.assertEqual(invalid["worker_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
