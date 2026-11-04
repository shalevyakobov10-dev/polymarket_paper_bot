import subprocess
import unittest

from service_supervisor import (
    build_supervisor_config,
    monitor_restart_delay,
    run_supervisor,
)


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

    def is_set(self):
        return False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return len(self.wait_calls) >= self.stop_after_waits


class SupervisorTests(unittest.TestCase):
    def test_monitor_is_disabled_by_default_and_only_http_worker_starts(self):
        config = build_supervisor_config(
            {"DATA_DIR": "/tmp/paper-data"},
            python_executable="python-test",
        )
        self.assertIsNone(config.monitor_command)
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
