import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from service_supervisor import (
    _perps_account_worker_heartbeat_age,
    _perps_worker_heartbeat_age,
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
        self.assertIsNone(config.perps_worker_command)
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
            self.assertEqual(failed_worker.returncode, 7)
            self.assertTrue(restarted_worker.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertTrue(status["event_paper_worker_enabled"])
            self.assertEqual(status["event_paper_worker_state"], "stopped")
            self.assertEqual(status["event_paper_worker_restart_count"], 1)
            self.assertEqual(status["event_paper_worker_last_exit_code"], 7)
            self.assertIsNone(status["event_paper_worker_next_restart_at"])

    def test_perps_market_worker_isolated_and_forced_to_public_research_mode(self):
        config = build_supervisor_config(
            {
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "ENABLE_PERPS_PAPER_ENGINE": "true",
                "DATA_DIR": "/persist/data",
                "DRY_RUN": "false",
                "LIVE_EXECUTION_ENABLED": "true",
                "LIVE_TRADING_ENABLED": "true",
                "POLYMARKET_PRIVATE_KEY": "synthetic-private-key",
                "PERPS_POLL_INTERVAL_SECONDS": "45",
                "PERPS_MARKET_LIMIT": "8",
            },
            python_executable="python-test",
        )
        self.assertEqual(config.perps_worker_command, ("python-test", "perps_market_worker.py"))
        self.assertEqual(config.perps_worker_env["DRY_RUN"], "true")
        self.assertEqual(config.perps_worker_env["LIVE_EXECUTION_ENABLED"], "false")
        self.assertNotIn("LIVE_TRADING_ENABLED", config.perps_worker_env)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", config.perps_worker_env)
        self.assertEqual(
            config.perps_worker_env["PERPS_MARKET_HISTORY_DB"],
            "/persist/data/perps_market_history.sqlite3",
        )
        self.assertEqual(
            config.perps_worker_env["PERPS_ORDER_JOURNAL_DB"],
            "/persist/data/perps_order_journal.sqlite3",
        )
        self.assertEqual(
            config.perps_worker_env["PERPS_MARKET_WORKER_STATUS_FILE"],
            "/persist/data/perps_market_worker_status.json",
        )
        self.assertEqual(config.perps_worker_env["PERPS_POLL_INTERVAL_SECONDS"], "45")
        self.assertEqual(config.perps_worker_env["PERPS_MARKET_LIMIT"], "8")
        self.assertEqual(config.perps_worker_env["ENABLE_PERPS_PAPER_ENGINE"], "true")
        self.assertEqual(
            config.perps_worker_env["PERPS_PAPER_DB"],
            "/persist/data/perps_paper.sqlite3",
        )
        self.assertEqual(config.perps_worker_env["PERPS_WORKER_HEARTBEAT_TIMEOUT_SECONDS"], "359")
        self.assertEqual(config.perps_worker_status_path, Path(
            "/persist/data/perps_market_worker_status.json"
        ))
        self.assertEqual(config.perps_worker_heartbeat_timeout_seconds, 359.0)
        self.assertNotIn("LIVE_TRADING_ENABLED", config.perps_worker_env)
        self.assertNotIn("LIVE_TRADING_ARMED", config.perps_worker_env)

    def test_perps_heartbeat_timeout_cannot_be_shorter_than_market_poll_budget(self):
        with self.assertRaisesRegex(ValueError, "between 359 and 7200"):
            build_supervisor_config({
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "PERPS_POLL_INTERVAL_SECONDS": "45",
                "PERPS_MARKET_LIMIT": "8",
                "PERPS_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "358",
            })

    def test_perps_heartbeat_age_ignores_old_process_files_and_rejects_future_stamps(self):
        now = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
        started = now - timedelta(seconds=10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perps-status.json"
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T07:59:58+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(
                _perps_worker_heartbeat_age(path, worker_started_at=started, now=now),
                2.0,
            )
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T07:59:47+00:00"}',
                encoding="utf-8",
            )
            # A timestamp older than this process launch is not inherited as fresh.
            self.assertEqual(
                _perps_worker_heartbeat_age(path, worker_started_at=started, now=now),
                10.0,
            )
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T08:00:10+00:00"}',
                encoding="utf-8",
            )
            # Future timestamps are treated as no new heartbeat; restart timing
            # remains anchored to this process launch rather than looping immediately.
            self.assertEqual(
                _perps_worker_heartbeat_age(path, worker_started_at=started, now=now),
                10.0,
            )

    def test_perps_heartbeat_watchdog_restarts_a_hung_but_running_process(self):
        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config({
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "DATA_DIR": directory,
                "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
            }, python_executable="python-test")
            config = replace(config, perps_worker_heartbeat_timeout_seconds=0.05)
            app = FakeProcess()
            hung_worker = FakeProcess()
            restarted_worker = FakeProcess()
            processes = [app, hung_worker, restarted_worker]
            commands = []

            def fake_popen(command, *, env):
                commands.append((command, env))
                return processes.pop(0)

            event = FakeStopEvent(stop_after_waits=2)
            with patch(
                "service_supervisor._perps_worker_heartbeat_age",
                side_effect=[0.2, 0.0],
            ):
                result = run_supervisor(
                    config, popen_factory=fake_popen, stop_event=event
                )
            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(commands[1][0], ["python-test", "perps_market_worker.py"])
            self.assertEqual(commands[2][0], ["python-test", "perps_market_worker.py"])
            self.assertEqual(hung_worker.returncode, -15)
            self.assertTrue(restarted_worker.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertEqual(status["perps_market_worker_restart_count"], 1)
            self.assertEqual(status["perps_market_worker_last_exit_code"], -15)

    def test_perps_paper_engine_requires_public_market_worker_and_valid_flag(self):
        with self.assertRaisesRegex(ValueError, "requires ENABLE_PERPS_MARKET_DATA_WORKER"):
            build_supervisor_config({"ENABLE_PERPS_PAPER_ENGINE": "true"})
        with self.assertRaisesRegex(ValueError, "ENABLE_PERPS_PAPER_ENGINE"):
            build_supervisor_config({
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "ENABLE_PERPS_PAPER_ENGINE": "sometimes",
            })

    def test_invalid_perps_worker_flag_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "ENABLE_PERPS_MARKET_DATA_WORKER"):
            build_supervisor_config({"ENABLE_PERPS_MARKET_DATA_WORKER": "sometimes"})

    def test_perps_worker_restarts_after_failure_and_persists_status(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config = build_supervisor_config(
                {
                    "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                    "DATA_DIR": directory,
                    "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
                },
                python_executable="python-test",
            )
            app = FakeProcess()
            failed_worker = FakeProcess(poll_results=[9])
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
            self.assertEqual(commands[1][0], ["python-test", "perps_market_worker.py"])
            self.assertEqual(commands[2][0], ["python-test", "perps_market_worker.py"])
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", commands[1][1])
            self.assertTrue(restarted_worker.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertTrue(status["perps_market_worker_enabled"])
            self.assertEqual(status["perps_market_worker_state"], "stopped")
            self.assertEqual(status["perps_market_worker_restart_count"], 1)
            self.assertEqual(status["perps_market_worker_last_exit_code"], 9)
            self.assertIsNone(status["perps_market_worker_next_restart_at"])

    def _account_supervisor_secret_file(self, directory, *, mode=0o600):
        path = Path(directory) / "perps-account-supervisor-secrets.json"
        path.write_text("{}", encoding="utf-8")
        path.chmod(mode)
        return path

    def test_perps_account_supervisor_is_disabled_by_default(self):
        config = build_supervisor_config(
            {"DATA_DIR": "/persist/data"},
            python_executable="python-test",
        )
        self.assertIsNone(config.perps_account_supervisor_command)
        self.assertIsNone(config.perps_account_supervisor_env)
        self.assertIsNone(config.perps_account_supervisor_status_path)

    def test_perps_account_supervisor_requires_public_perps_worker_and_secret_file(self):
        with self.assertRaisesRegex(ValueError, "requires ENABLE_PERPS_MARKET_DATA_WORKER"):
            build_supervisor_config({
                "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": "/secrets/perps.json",
            })
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "secret file"):
                build_supervisor_config({
                    "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                    "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                    "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(
                        Path(directory) / "missing.json"
                    ),
                })

    def test_perps_account_supervisor_uses_strict_child_environment_and_shared_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = self._account_supervisor_secret_file(directory)
            config = build_supervisor_config(
                {
                    "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                    "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                    "DATA_DIR": directory,
                    "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(secret_path),
                    "POLYMARKET_PRIVATE_KEY": "must-not-enter-account-env",
                    "POLYMARKET_API_KEY": "must-not-enter-account-env",
                    "POLYMARKET_API_SECRET": "must-not-enter-account-env",
                    "DRY_RUN": "false",
                    "LIVE_EXECUTION_ENABLED": "true",
                    "LIVE_TRADING_ENABLED": "true",
                    "PERPS_ACCOUNT_SUPERVISOR_POLL_SECONDS": "45",
                    "PERPS_ACCOUNT_SUPERVISOR_CYCLE_TIMEOUT_SECONDS": "75",
                    "PERPS_ACCOUNT_SUPERVISOR_MAX_FAILURES": "4",
                },
                python_executable="python-test",
            )
        self.assertEqual(
            config.perps_account_supervisor_command,
            ("python-test", "perps_account_supervisor_worker.py"),
        )
        env = config.perps_account_supervisor_env
        self.assertEqual(env["PERPS_ACCOUNT_SUPERVISOR_POLL_SECONDS"], "45")
        self.assertEqual(env["PERPS_ACCOUNT_SUPERVISOR_CYCLE_TIMEOUT_SECONDS"], "75.0")
        self.assertEqual(env["PERPS_ACCOUNT_SUPERVISOR_MAX_FAILURES"], "4")
        self.assertEqual(env["DRY_RUN"], "true")
        self.assertEqual(env["LIVE_EXECUTION_ENABLED"], "false")
        self.assertEqual(
            env["PERPS_SUBMISSION_LOCK_PATH"],
            str(Path(directory) / "perps_submission.lock"),
        )
        self.assertEqual(
            env["PERPS_ORDER_JOURNAL_DB"],
            str(Path(directory) / "perps_order_journal.sqlite3"),
        )
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", env)
        self.assertNotIn("POLYMARKET_API_KEY", env)
        self.assertNotIn("POLYMARKET_API_SECRET", env)
        self.assertNotIn("LIVE_TRADING_ENABLED", env)
        self.assertEqual(
            config.perps_account_supervisor_status_path,
            Path(directory) / "perps_account_supervisor_status.json",
        )
        self.assertEqual(config.perps_account_supervisor_heartbeat_timeout_seconds, 240.0)
        self.assertNotIn("PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE", config.app_env)
        self.assertNotIn("PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE", config.perps_worker_env)

    def test_perps_account_supervisor_secret_file_permissions_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = self._account_supervisor_secret_file(directory, mode=0o644)
            with self.assertRaisesRegex(ValueError, "permissions are unsafe"):
                build_supervisor_config({
                    "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                    "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                    "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(secret_path),
                })

    def test_perps_account_supervisor_heartbeat_timeout_cannot_be_too_short(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = self._account_supervisor_secret_file(directory)
            with self.assertRaisesRegex(ValueError, "between 180 and 7200"):
                build_supervisor_config({
                    "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                    "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                    "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(secret_path),
                    "PERPS_ACCOUNT_SUPERVISOR_HEARTBEAT_TIMEOUT_SECONDS": "179",
                })

    def test_perps_account_supervisor_heartbeat_age_ignores_old_and_future_status(self):
        now = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
        started = now - timedelta(seconds=10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-status.json"
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T07:59:58+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(
                _perps_account_worker_heartbeat_age(path, worker_started_at=started, now=now),
                2.0,
            )
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T07:59:47+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(
                _perps_account_worker_heartbeat_age(path, worker_started_at=started, now=now),
                10.0,
            )
            path.write_text(
                '{"last_heartbeat_at":"2026-11-22T08:00:10+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(
                _perps_account_worker_heartbeat_age(path, worker_started_at=started, now=now),
                10.0,
            )

    def test_perps_account_supervisor_restarts_after_exit_and_persists_status(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = self._account_supervisor_secret_file(directory)
            config = build_supervisor_config({
                "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "DATA_DIR": directory,
                "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(secret_path),
                "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
            }, python_executable="python-test")
            app = FakeProcess()
            perps_market = FakeProcess()
            failed_account = FakeProcess(poll_results=[11])
            restarted_account = FakeProcess()
            processes = [app, perps_market, failed_account, restarted_account]
            commands = []

            def fake_popen(command, *, env):
                commands.append((command, env))
                return processes.pop(0)

            event = FakeStopEvent(stop_after_waits=2)
            result = run_supervisor(config, popen_factory=fake_popen, stop_event=event)

            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 4)
            self.assertEqual(
                commands[2][0],
                ["python-test", "perps_account_supervisor_worker.py"],
            )
            self.assertEqual(
                commands[3][0],
                ["python-test", "perps_account_supervisor_worker.py"],
            )
            self.assertEqual(
                commands[2][1]["PERPS_SUBMISSION_LOCK_PATH"],
                commands[3][1]["PERPS_SUBMISSION_LOCK_PATH"],
            )
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", commands[2][1])
            self.assertNotIn("LIVE_EXECUTION_ENABLED", commands[2][1])
            self.assertTrue(restarted_account.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertTrue(status["perps_account_supervisor_enabled"])
            self.assertEqual(status["perps_account_supervisor_state"], "stopped")
            self.assertEqual(status["perps_account_supervisor_restart_count"], 1)
            self.assertEqual(status["perps_account_supervisor_last_exit_code"], 11)
            self.assertIsNone(status["perps_account_supervisor_next_restart_at"])

    def test_perps_account_supervisor_heartbeat_watchdog_restarts_hung_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = self._account_supervisor_secret_file(directory)
            config = build_supervisor_config({
                "ENABLE_PERPS_ACCOUNT_SUPERVISOR": "true",
                "ENABLE_PERPS_MARKET_DATA_WORKER": "true",
                "DATA_DIR": directory,
                "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE": str(secret_path),
                "SERVICE_SUPERVISOR_POLL_SECONDS": "0.25",
            }, python_executable="python-test")
            config = replace(config, perps_account_supervisor_heartbeat_timeout_seconds=0.05)
            app = FakeProcess()
            perps_market = FakeProcess()
            hung_account = FakeProcess()
            restarted_account = FakeProcess()
            processes = [app, perps_market, hung_account, restarted_account]
            commands = []

            def fake_popen(command, *, env):
                commands.append((command, env))
                return processes.pop(0)

            event = FakeStopEvent(stop_after_waits=2)
            with patch(
                "service_supervisor._perps_account_worker_heartbeat_age",
                side_effect=[0.2, 0.0],
            ):
                result = run_supervisor(
                    config, popen_factory=fake_popen, stop_event=event
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 4)
            self.assertEqual(hung_account.returncode, -15)
            self.assertTrue(restarted_account.terminated)
            status = read_supervisor_status(config.status_path)
            self.assertEqual(status["perps_account_supervisor_restart_count"], 1)
            self.assertEqual(status["perps_account_supervisor_last_exit_code"], -15)

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
