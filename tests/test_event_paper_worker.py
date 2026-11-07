import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from pathlib import Path

from event_paper_worker import read_event_paper_worker_status, run_worker


class StopAfterWaits:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.wait_count = 0
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout=None):
        self.wait_count += 1
        if self.wait_count >= self.stop_after:
            self.stopped = True
            return True
        return False


class FakeBot:
    def __init__(self, results):
        self.results = list(results)
        self.closed = False
        self.calls = 0

    def run_once(self):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


class EventPaperWorkerTests(unittest.TestCase):
    def test_runs_paper_cycle_and_persists_sanitized_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([{
                "markets_loaded": 3, "opened": [{"market_id": "m1"}],
                "closed": [], "close_blocked": [],
                "portfolio": {"cash": 98.0, "equity": 100.0, "realized_pnl": 0.0,
                              "open_positions": 1, "closed_positions": 0, "win_rate_pct": None,
                              "active_event_evidence_threshold": 0.75,
                              "performance": {"evaluation_status": "insufficient_sample",
                                             "closed_trades": 2, "fees_paid": 0.4,
                                             "out_of_sample_validated": True,
                                             "live_execution_eligible": True,
                                             "warnings": ["sample too small"],
                                             "api_key": "must-not-persist"},
                              "last_tuning": {
                                  "ran_at": "2026-11-07T12:00:00+00:00",
                                  "result": {
                                      "status": "rejected", "applied": False,
                                      "input_dataset_sha256": "a" * 64,
                                      "evaluation_run_sha256": "b" * 64,
                                      "live_execution_eligible": True,
                                      "real_money_execution": "enabled",
                                      "fold_reports": [{"fold": 1, "status": "rejected",
                                                        "net_pnl_improvement": -1.0,
                                                        "api_key": "must-not-persist"}],
                                      "private_key": "must-not-persist",
                                  },
                              },
                              "POLYMARKET_PRIVATE_KEY": "must-not-persist"},
            }])
            stop = StopAfterWaits(1)
            result = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
            )
            self.assertEqual(result, 0)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["markets_loaded_last_cycle"], 3)
            self.assertEqual(status["opened_last_cycle"], 1)
            self.assertEqual(status["portfolio"]["cash"], 98.0)
            self.assertEqual(status["portfolio"]["open_positions"], 1)
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", status["portfolio"])
            self.assertEqual(status["portfolio"]["performance"]["closed_trades"], 2)
            self.assertFalse(status["portfolio"]["performance"]["out_of_sample_validated"])
            self.assertFalse(status["portfolio"]["performance"]["live_execution_eligible"])
            self.assertNotIn("api_key", status["portfolio"]["performance"])
            tuning = status["portfolio"]["last_tuning"]
            self.assertEqual(tuning["result"]["input_dataset_sha256"], "a" * 64)
            self.assertEqual(tuning["result"]["evaluation_run_sha256"], "b" * 64)
            self.assertFalse(tuning["result"]["live_execution_eligible"])
            self.assertEqual(tuning["result"]["real_money_execution"], "not_implemented")
            self.assertNotIn("private_key", tuning["result"])
            self.assertNotIn("api_key", tuning["result"]["fold_reports"][0])
            self.assertEqual(status["execution"], "none")
            self.assertTrue(status["research_only"])
            self.assertNotIn("environment", status)
            self.assertNotIn("command", status)

    def test_successful_paper_cycle_runs_scheduled_two_database_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            for path in (paper, history):
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.commit()
                connection.close()
            bot = FakeBot([{
                "markets_loaded": 1, "opened": [], "closed": [], "close_blocked": [],
                "portfolio": {"cash": 100, "equity": 100, "realized_pnl": 0,
                              "open_positions": 0, "closed_positions": 0},
            }])
            bot.config = SimpleNamespace(
                database_path=paper,
                market_history_database_path=history,
            )
            calls = []

            def backup_function(paths, directory, *, retain):
                calls.append((list(paths), Path(directory), retain))
                return {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "manifest_name": "manifest.json",
                    "files": [{"backup_name": "paper.sqlite3"}, {"backup_name": "history.sqlite3"}],
                    "skipped_missing_sources": [],
                }

            status_path = root / "worker.json"
            stop = StopAfterWaits(1)
            result = run_worker(
                bot_factory=lambda: bot,
                stop_event=stop,
                status_path=status_path,
                poll_seconds=30,
                backup_function=backup_function,
                backup_interval_hours=6,
                backup_directory=root / "backups",
                backup_retain=7,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], [paper, history])
            self.assertEqual(calls[0][1], root / "backups")
            self.assertEqual(calls[0][2], 7)
            status = read_event_paper_worker_status(status_path)
            self.assertEqual(status["last_backup_at"] is not None, True)
            self.assertEqual(status["backup_manifest_name"], "manifest.json")
            self.assertEqual(status["backup_files_last_run"], 2)
            self.assertIsNone(status["backup_error_type"])

    def test_backup_failure_marks_worker_degraded_without_faking_backup_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            for path in (paper, history):
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.commit()
                connection.close()
            bot = FakeBot([{
                "markets_loaded": 1, "opened": [], "closed": [], "close_blocked": [],
            }])
            bot.config = SimpleNamespace(
                database_path=paper,
                market_history_database_path=history,
            )

            def fail_backup(*_args, **_kwargs):
                raise OSError("private path details must not be recorded")

            status_path = root / "worker.json"
            result = run_worker(
                bot_factory=lambda: bot,
                stop_event=StopAfterWaits(1),
                status_path=status_path,
                poll_seconds=30,
                backup_function=fail_backup,
            )
            self.assertEqual(result, 0)
            status = read_event_paper_worker_status(status_path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertIsNone(status["last_backup_at"])
            self.assertEqual(status["backup_error_type"], "OSError")
            self.assertEqual(status["backup_consecutive_failures"], 1)
            self.assertGreaterEqual(status["backup_retry_delay_seconds"], 60)

    def test_zero_usable_market_snapshots_marks_worker_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([{
                "markets_loaded": 0, "opened": [], "closed": [], "close_blocked": [],
                "portfolio": {"cash": 100, "equity": 100, "realized_pnl": 0,
                              "open_positions": 0, "closed_positions": 0},
            }])
            result = run_worker(
                bot_factory=lambda: bot,
                stop_event=StopAfterWaits(1),
                status_path=path,
                poll_seconds=30,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["last_cycle_warning"], "no_usable_market_snapshots")
            self.assertEqual(status["markets_loaded_last_cycle"], 0)
            self.assertFalse(status["heartbeat_fresh"] is False)

    def test_transient_error_retries_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([
                RuntimeError("response content must not be stored"),
                {"markets_loaded": 2, "opened": [], "closed": [], "close_blocked": [1]},
            ])
            stop = StopAfterWaits(2)
            result = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
            )
            self.assertEqual(result, 0)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["total_failures"], 1)
            self.assertEqual(status["consecutive_failures"], 0)
            self.assertEqual(status["close_blocked_last_cycle"], 1)
            self.assertIsNone(status["last_error_type"])

    def test_repeated_failures_exit_for_supervisor_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([RuntimeError("failure 1"), RuntimeError("failure 2")])
            stop = StopAfterWaits(10)
            result = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
                max_consecutive_failures=2,
            )
            self.assertEqual(result, 1)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "failed")
            self.assertEqual(status["total_failures"], 2)
            self.assertEqual(status["last_error_type"], "RuntimeError")

    def test_invalid_poll_interval_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 30 and 86400"):
            run_worker(
                bot_factory=lambda: FakeBot([]),
                stop_event=StopAfterWaits(1),
                status_path="worker.json",
                poll_seconds=10,
            )

    def test_missing_status_is_not_reported_as_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            status = read_event_paper_worker_status(Path(directory) / "missing.json")
        self.assertEqual(status["worker_state"], "unknown")
        self.assertFalse(status["heartbeat_fresh"])
        self.assertEqual(status["status_error"], "status_file_missing")


if __name__ == "__main__":
    unittest.main()
