import tempfile
import unittest
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
    def test_runs_paper_cycles_and_writes_allowlisted_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([{
                "markets_loaded": 3, "opened": [{"market_id": "m1"}],
                "closed": [], "close_blocked": [],
            }])
            stop = StopAfterWaits(1)
            exit_code = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["markets_loaded_last_cycle"], 3)
            self.assertEqual(status["opened_last_cycle"], 1)
            self.assertEqual(status["execution"], "none")
            self.assertTrue(status["research_only"])
            self.assertNotIn("environment", status)
            self.assertNotIn("command", status)

    def test_transient_cycle_failure_uses_backoff_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([
                RuntimeError("do not persist this response body"),
                {"markets_loaded": 2, "opened": [], "closed": [], "close_blocked": [1]},
            ])
            stop = StopAfterWaits(2)
            exit_code = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["total_failures"], 1)
            self.assertEqual(status["consecutive_failures"], 0)
            self.assertEqual(status["close_blocked_last_cycle"], 1)
            self.assertIsNone(status["last_error_type"])

    def test_five_consecutive_failures_exit_for_supervisor_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            bot = FakeBot([RuntimeError("failure 1"), RuntimeError("failure 2")])
            stop = StopAfterWaits(10)
            exit_code = run_worker(
                bot_factory=lambda: bot, stop_event=stop,
                status_path=path, poll_seconds=30,
                max_consecutive_failures=2,
            )
            self.assertEqual(exit_code, 1)
            self.assertTrue(bot.closed)
            status = read_event_paper_worker_status(path)
            self.assertEqual(status["worker_state"], "failed")
            self.assertEqual(status["total_failures"], 2)
            self.assertEqual(status["last_error_type"], "RuntimeError")

    def test_rejects_invalid_poll_interval(self):
        with self.assertRaisesRegex(ValueError, "between 30 and 86400"):
            run_worker(
                bot_factory=lambda: FakeBot([]),
                stop_event=StopAfterWaits(1),
                status_path="worker.json",
                poll_seconds=10,
            )

    def test_missing_worker_status_is_not_reported_as_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            status = read_event_paper_worker_status(Path(directory) / "missing.json")
        self.assertEqual(status["worker_state"], "unknown")
        self.assertFalse(status["heartbeat_fresh"])
        self.assertEqual(status["status_error"], "status_file_missing")


if __name__ == "__main__":
    unittest.main()
