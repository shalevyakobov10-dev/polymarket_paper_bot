from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from perps_market_worker import read_perps_worker_status, run_worker
from perps_paper_engine import PerpsPaperEngine


def live_nested_snapshot():
    now = datetime.now(timezone.utc)
    stamp = int(now.timestamp() * 1000)
    return {
        "instrument": {
            "instrument_id": 7,
            "instrument_type": "perpetual",
            "symbol": "BTC-PERP",
            "display_symbol": "BTC",
            "category": "crypto",
            "base_asset": "BTC",
            "quote_asset": "USD",
            "funding_interval": "1h",
            "close_only": False,
            "isolated_only": False,
            "min_notional": "10",
            "max_market_notional": "100000",
            "max_limit_notional": "1000000",
            "max_leverage": "20",
            "quantity_decimals": 4,
            "price_decimals": 2,
            "risk_tiers": [{"lower_bound": "0", "max_leverage": "20"}],
        },
        "ticker": {
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "index_price": "65000",
            "mark_price": "65000",
            "last_price": "65000",
            "mid_price": "65000",
            "open_interest": "50",
            "funding_rate": "0.0001",
            "next_funding_ms": stamp + 3_600_000,
            "timestamp_ms": stamp,
        },
        "book": {
            "instrument_id": 7,
            "timestamp_ms": stamp,
            "sequence": 100,
            "best_bid": "64999",
            "best_ask": "65001",
            "bids": [{"price": "64999", "quantity": "2"}],
            "asks": [{"price": "65001", "quantity": "2"}],
        },
        "received_at": now.isoformat(),
        "research_only": True,
        "execution": "none",
    }


class FakeStop:
    def wait(self, _seconds):
        return True

    def is_set(self):
        return False


class FakeProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self):
        return [self.snapshot]


class FakeStore:
    def __init__(self, path=None):
        self.rows = []
        self.path = path

    def count(self):
        return len(self.rows)

    def record_snapshots(self, snapshots):
        self.rows.extend(snapshots)
        return {"inserted": len(snapshots), "total_snapshots": len(self.rows)}

    def prune_before(self, _cutoff):
        return {"deleted": 0, "remaining": len(self.rows)}

    def close(self):
        pass


class PerpsPaperWorkerIntegrationTests(unittest.TestCase):
    def test_existing_public_market_worker_updates_paper_account_and_allowlisted_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "worker-status.json"
            engine = PerpsPaperEngine(Path(directory) / "paper.sqlite3")
            result = run_worker(
                provider=FakeProvider(live_nested_snapshot()),
                store=FakeStore(),
                stop_event=FakeStop(),
                status_path=target,
                poll_interval_seconds=15,
                history_retention_days=14,
                max_cycles=1,
                paper_engine=engine,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            status = read_perps_worker_status(target)
            self.assertEqual(status["worker_state"], "healthy")
            self.assertTrue(status["paper_engine_enabled"])
            self.assertEqual(status["paper_worker_state"], "running")
            self.assertEqual(status["paper_open_positions"], 0)
            self.assertEqual(status["paper_cash_balance"], "100")
            self.assertEqual(status["paper_equity"], "100")
            self.assertEqual(status["execution"], "none")
            self.assertFalse(status["live_execution_enabled"])

    def test_worker_backs_up_both_market_history_and_paper_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.sqlite3"
            with sqlite3.connect(history_path) as database:
                database.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
            calls = []

            def backup(paths, target, *, retain):
                calls.append((list(paths), Path(target), retain))
                return {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "files": [{"backup_name": Path(path).name} for path in paths],
                }

            engine = PerpsPaperEngine(root / "paper.sqlite3")
            target = root / "status.json"
            result = run_worker(
                provider=FakeProvider(live_nested_snapshot()),
                store=FakeStore(history_path),
                stop_event=FakeStop(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
                paper_engine=engine,
                backup_function=backup,
                backup_directory=root / "backups",
                backup_interval_hours=6,
                backup_retain=12,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
            paths, backup_dir, retain = calls[0]
            self.assertEqual({Path(path).name for path in paths}, {"history.sqlite3", "paper.sqlite3"})
            self.assertEqual(backup_dir, root / "backups")
            self.assertEqual(retain, 12)
            status = read_perps_worker_status(target)
            self.assertIsNotNone(status["backup_at"])
            self.assertIsNone(status["backup_error_type"])

    def test_backup_failure_degrades_worker_without_exposing_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.sqlite3"
            with sqlite3.connect(history_path) as database:
                database.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
            engine = PerpsPaperEngine(root / "paper.sqlite3")

            def fail_backup(*_args, **_kwargs):
                raise OSError("synthetic path includes private marker DO_NOT_LEAK")

            result = run_worker(
                provider=FakeProvider(live_nested_snapshot()),
                store=FakeStore(history_path),
                stop_event=FakeStop(),
                status_path=root / "status.json",
                poll_interval_seconds=15,
                max_cycles=1,
                paper_engine=engine,
                backup_function=fail_backup,
                backup_directory=root / "backups",
            )
            self.assertEqual(result, 0)
            status = read_perps_worker_status(root / "status.json")
            self.assertEqual(status["worker_state"], "degraded")
            self.assertEqual(status["backup_error_type"], "OSError")
            self.assertNotIn("DO_NOT_LEAK", str(status))


if __name__ == "__main__":
    unittest.main()
