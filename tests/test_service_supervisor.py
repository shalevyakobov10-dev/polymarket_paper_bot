import subprocess
import unittest

from service_supervisor import (
    build_supervisor_config,
    monitor_restart_delay,
    run_supervisor,
)
from supervisor_status import read_supervisor_status


class FakeProcess:
    def __init__(self, poll_results=(), *, wait_raises_timeout=False):
        self.poll_results = list(poll_results)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.wait_raises_timeout = wait_raises_timeout

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self.poll_results:
            result = self.poll_results.pop(0)
            if result is not None:
                self.returncode = result
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.wait_raises_timeout:
            self.returncode = -15

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_raises_timeout and not self.killed:
            raise subprocess.TimeoutExpired("fake-process", timeout)
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeStopEvent:
    def __init__(self, stop_after_waits):
        self.stop_after_waits = stop_after_waits
        self.wait_calls = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if len(self.wait_calls) >= self.stop_after_waits:
            self.stopped = True
            return True
        return False


class SupervisorTests(unittest.TestCase):
    def test_monitor_is_disabled_by_default_and_only_http_worker_starts(self):
        config = build_supervisor_config(
            {"DATA_DIR": "/tmp/paper-data"},
            python_executable="python-test",
        )
        self.assertIsNone(config.monitor_command)
        self.assertIsNone(config.paper_worker_command)
        self.assertEqual(config.app_command, ("python-test", "app.py"))
        app = FakeProcess()
        commands = []

        def fake_popen(command, *, env):
            commands.append((command, env))
            return app

        event = FakeStopEvent(stop_after_waits=1)
        result = run_supervisor(
            config, popen_factory=fake_popen, stop_event=event
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], ["python-test", "app.py"])
        self.assertTrue(app.terminated)

    def test_event_paper_worker_is_sanitized_and_forced_to_paper_mode(self):
        config = build_supervisor_config(
            {
                "ENABLE_EVENT_PAPER_WORKER": "true",
                "DATA_DIR": "/persist/data",
                "DRY_RUN": "false",
                "LIVE_EXECUTION_ENABLED": "true",
                "LIVE_TRADING_ENABLED": "true",
                "LIVE_TRADING_CONFIRMATION": "synthetic-live-secret",
                "LIVE_EXPECTED_WALLET": "synthetic-wallet",
                "POLYMARKET_PRIVATE_KEY": "synthetic-private-key",
            },
            python_executable="python-test",
        )
        self.assertEqual(config.paper_worker_command, ("python-test", "event_paper_worker.py"))
        self.assertEqual(config.paper_worker_env["DRY_RUN"], "true")
        self.assertEqual(config.paper_worker_env["LIVE_EXECUTION_ENABLED"], "false")
        self.assertNotIn("LIVE_TRADING_ENABLED", config.paper_worker_env)
        self.assertNotIn("LIVE_TRADING_CONFIRMATION", config.paper_worker_env)
        self.assertNotIn("LIVE_EXPECTED_WALLET", config.paper_worker_env)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", config.paper_worker_env)
        self.assertEqual(
            config.paper_worker_env["PAPER_DATABASE_PATH"],
            "/persist/data/offline_news_paper.sqlite3",
        )
        self.assertEqual(
            config.paper_worker_env["MARKET_HISTORY_DATABASE_PATH"],
            "/persist/data/market_history.sqlite3",
        )
        self.assertEqual(
            config.paper_worker_env["EVENT_PAPER_WORKER_STATUS_FILE"],
            "/persist/data/event_paper_worker_status.json",
        )

    def test_invalid_event_paper_worker_flag_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "ENABLE_EVENT_PAPER_WORKER"):
            build_supervisor_config({"ENABLE_EVENT_PAPER_WORKER": "sometimes"})

    def test_event_paper_worker_restarts_after_failure_with_bounded_backoff(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config(
                {
                    "ENABLE_EVENT_PAPER_WORKER": "true",
                    "DATA_DIR": directory,
                    "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
                },
                python_executable="python-test",
            )
            app = FakeProcess()
            failed_worker = FakeProcess(poll_results=[7])
            restarted_worker = FakeProcess()
            processes = [app, failed_worker, restarted_worker]
            commands = []

            def fake_popen(command, *, env):
                commands.append((command, env))
                return processes.pop(0)

            event = FakeStopEvent(stop_after_waits=2)
            result = run_supervisor(config, popen_factory=fake_popen, stop_event=event)
            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(commands[1][0], ["python-test", "event_paper_worker.py"])
            self.assertEqual(commands[2][0], ["python-test", "event_paper_worker.py"])
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", commands[1][1])
            self.assertTrue(failed_worker.terminated)
            self.assertTrue(restarted_worker.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertTrue(status["event_paper_worker_enabled"])
            self.assertEqual(status["event_paper_worker_state"], "stopped")
            self.assertEqual(status["event_paper_worker_restart_count"], 1)
            self.assertEqual(status["event_paper_worker_last_exit_code"], 7)
            self.assertIsNone(status["event_paper_worker_next_restart_at"])

    def test_opt_in_monitor_requires_runtime_secret_and_never_puts_it_in_argv(self):
        with self.assertRaisesRegex(ValueError, "requires POLYMARKET_PRIVATE_KEY"):
            build_supervisor_config({"ENABLE_USER_ORDER_MONITOR": "true"})

        config = build_supervisor_config(
            {
                "ENABLE_USER_ORDER_MONITOR": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                "DATA_DIR": "/persist/data",
                "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS": "120",
            },
            python_executable="python-test",
        )
        self.assertIsNotNone(config.monitor_command)
        self.assertEqual(config.monitor_command[:2], ("python-test", "user_order_stream.py"))
        self.assertNotIn("synthetic-test-secret", config.monitor_command)
        self.assertEqual(config.app_env["ORDER_JOURNAL_DB"],
                         "/persist/data/order_journal.sqlite3")
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", config.app_env)
        self.assertEqual(config.monitor_env["POLYMARKET_PRIVATE_KEY"],
                         "synthetic-test-secret")
        self.assertEqual(config.monitor_command[-1], "120.0")

    def test_invalid_monitor_flag_and_interval_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "exactly true or false"):
            build_supervisor_config({"ENABLE_USER_ORDER_MONITOR": "sometimes"})
        with self.assertRaisesRegex(ValueError, "positive finite"):
            build_supervisor_config({
                "ENABLE_USER_ORDER_MONITOR": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS": "NaN",
            })

        with self.assertRaisesRegex(ValueError, "must not exceed 240"):
            build_supervisor_config({
                "ENABLE_USER_ORDER_MONITOR": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS": "241",
            })

    def test_monitor_restart_uses_capped_exponential_backoff_and_shared_env(self):
        config = build_supervisor_config(
            {
                "ENABLE_USER_ORDER_MONITOR": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                "DATA_DIR": "/persist/data",
                "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
            },
            python_executable="python-test",
        )
        app = FakeProcess()
        failed_monitor = FakeProcess(poll_results=[7])
        restarted_monitor = FakeProcess()
        processes = [app, failed_monitor, restarted_monitor]
        commands = []

        def fake_popen(command, *, env):
            commands.append((command, env))
            return processes.pop(0)

        event = FakeStopEvent(stop_after_waits=2)
        result = run_supervisor(
            config, popen_factory=fake_popen, stop_event=event
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][0], ["python-test", "app.py"])
        self.assertEqual(commands[1][0], list(config.monitor_command))
        self.assertEqual(commands[2][0], list(config.monitor_command))
        self.assertEqual(event.wait_calls, [2.0, 0.25])
        self.assertTrue(app.terminated)
        self.assertTrue(restarted_monitor.terminated)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", commands[0][1])
        self.assertEqual(commands[1][1]["POLYMARKET_PRIVATE_KEY"],
                         "synthetic-test-secret")

    def test_main_worker_exit_propagates_and_stops_account_monitor(self):
        config = build_supervisor_config(
            {
                "ENABLE_USER_ORDER_MONITOR": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
            },
            python_executable="python-test",
        )
        app = FakeProcess(poll_results=[17])
        monitor = FakeProcess()
        processes = [app, monitor]

        def fake_popen(_command, *, env):
            return processes.pop(0)

        result = run_supervisor(config, popen_factory=fake_popen)
        self.assertEqual(result, 17)
        self.assertTrue(monitor.terminated)
        self.assertFalse(app.terminated)


    def test_supervisor_persists_disabled_and_stopped_status_without_secrets(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config(
                {"DATA_DIR": directory, "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret"},
                python_executable="python-test",
            )
            app = FakeProcess()

            def fake_popen(_command, *, env):
                self.assertNotIn("POLYMARKET_PRIVATE_KEY", env)
                return app

            event = FakeStopEvent(stop_after_waits=1)
            result = run_supervisor(config, popen_factory=fake_popen, stop_event=event)
            self.assertEqual(result, 0)
            status = read_supervisor_status(config.status_path)
            self.assertTrue(status["supervised"])
            self.assertEqual(status["service_state"], "stopped")
            self.assertFalse(status["monitor_enabled"])
            self.assertEqual(status["monitor_state"], "disabled")
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", status)
            self.assertTrue(Path(config.status_path).exists())

    def test_supervisor_status_records_monitor_restart_and_final_exit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config(
                {
                    "ENABLE_USER_ORDER_MONITOR": "true",
                    "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                    "DATA_DIR": directory,
                    "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
                },
                python_executable="python-test",
            )
            app = FakeProcess()
            failed_monitor = FakeProcess(poll_results=[7])
            restarted_monitor = FakeProcess()
            processes = [app, failed_monitor, restarted_monitor]

            def fake_popen(_command, *, env):
                return processes.pop(0)

            event = FakeStopEvent(stop_after_waits=2)
            result = run_supervisor(
                config, popen_factory=fake_popen, stop_event=event
            )
            self.assertEqual(result, 0)
            status = read_supervisor_status(config.status_path)
            self.assertEqual(status["service_state"], "stopped")
            self.assertTrue(status["monitor_enabled"])
            self.assertEqual(status["monitor_state"], "stopped")
            self.assertEqual(status["monitor_restart_count"], 1)
            self.assertEqual(status["monitor_last_exit_code"], 7)
            self.assertIsNone(status["monitor_next_restart_at"])

    def test_app_failure_is_reported_without_overwriting_monitor_exit_code(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config(
                {
                    "ENABLE_USER_ORDER_MONITOR": "true",
                    "POLYMARKET_PRIVATE_KEY": "synthetic-test-secret",
                    "DATA_DIR": directory,
                },
                python_executable="python-test",
            )
            app = FakeProcess(poll_results=[17])
            monitor = FakeProcess()
            processes = [app, monitor]

            def fake_popen(_command, *, env):
                return processes.pop(0)

            result = run_supervisor(config, popen_factory=fake_popen)
            self.assertEqual(result, 17)
            status = read_supervisor_status(config.status_path)
            self.assertEqual(status["service_state"], "app_failed")
            self.assertEqual(status["app_exit_code"], 17)
            self.assertIsNone(status["monitor_last_exit_code"])
            self.assertEqual(status["monitor_state"], "stopped")
            self.assertTrue(monitor.terminated)

    def test_stop_child_escalates_to_kill_after_timeout(self):
        from service_supervisor import _stop_child

        process = FakeProcess(wait_raises_timeout=True)
        _stop_child(process, timeout_seconds=0.01)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2)

    def test_monitor_backoff_is_bounded_and_rejects_invalid_attempts(self):
        self.assertEqual(monitor_restart_delay(1), 2)
        self.assertEqual(monitor_restart_delay(2), 4)
        self.assertEqual(monitor_restart_delay(20), 60)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            monitor_restart_delay(0)
        with self.assertRaisesRegex(ValueError, "cannot be less"):
            monitor_restart_delay(1, base_seconds=5, max_seconds=2)


if __name__ == "__main__":
    unittest.main()
