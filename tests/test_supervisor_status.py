import json
import tempfile
import unittest
from pathlib import Path

from supervisor_status import (
    build_supervisor_status,
    read_supervisor_status,
    write_supervisor_status,
)


class SupervisorStatusTests(unittest.TestCase):
    def test_write_and_read_running_monitor_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "supervisor.json"
            payload = build_supervisor_status(
                service_state="running",
                monitor_enabled=True,
                monitor_state="running",
                restart_count=2,
                last_exit_code=7,
                updated_at="2026-11-04T20:00:00+00:00",
            )
            write_supervisor_status(target, payload)
            result = read_supervisor_status(target)
            self.assertTrue(result["supervised"])
            self.assertEqual(result["service_state"], "running")
            self.assertTrue(result["monitor_enabled"])
            self.assertEqual(result["monitor_state"], "running")
            self.assertEqual(result["monitor_restart_count"], 2)
            self.assertEqual(result["monitor_last_exit_code"], 7)
            self.assertEqual(result["execution"], "none")

    def test_restarting_status_exposes_restart_time_without_environment(self):
        payload = build_supervisor_status(
            service_state="running",
            monitor_enabled=True,
            monitor_state="restarting",
            restart_count=3,
            last_exit_code=9,
            next_restart_at="2026-11-04T20:00:05+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            write_supervisor_status(target, payload)
            loaded = read_supervisor_status(target)
        self.assertEqual(loaded["monitor_state"], "restarting")
        self.assertEqual(loaded["monitor_restart_count"], 3)
        self.assertEqual(loaded["monitor_last_exit_code"], 9)
        self.assertEqual(loaded["monitor_next_restart_at"], "2026-11-04T20:00:05+00:00")
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", loaded)
        self.assertNotIn("env", loaded)
        self.assertNotIn("command", loaded)

    def test_missing_or_corrupt_status_returns_safe_unknown_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing.json"
            missing = read_supervisor_status(target)
            self.assertFalse(missing["supervised"])
            self.assertEqual(missing["status_error"], "status_file_missing")

            target.write_text("{broken", encoding="utf-8")
            corrupt = read_supervisor_status(target)
            self.assertFalse(corrupt["supervised"])
            self.assertEqual(corrupt["status_error"], "status_file_invalid")

    def test_status_keeps_main_worker_exit_code_separate_from_monitor_exit_code(self):
        payload = build_supervisor_status(
            service_state="app_failed",
            monitor_enabled=True,
            monitor_state="stopped",
            last_exit_code=7,
            app_exit_code=17,
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            write_supervisor_status(target, payload)
            loaded = read_supervisor_status(target)
        self.assertEqual(loaded["monitor_last_exit_code"], 7)
        self.assertEqual(loaded["app_exit_code"], 17)

    def test_status_builder_rejects_invalid_or_inconsistent_state(self):
        common = {
            "service_state": "running",
            "monitor_enabled": True,
            "monitor_state": "running",
        }
        bad = [
            ({"service_state": "unknown"}, "service_state"),
            ({"monitor_enabled": False}, "monitor_state"),
            ({"restart_count": -1}, "restart_count"),
            ({"last_exit_code": True}, "last_exit_code"),
            ({"next_restart_at": "tomorrow"}, "valid ISO timestamp"),
        ]
        for changes, expected in bad:
            args = {**common, **changes}
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, expected):
                    build_supervisor_status(**args)

    def test_reader_drops_unrecognized_fields_from_status_document(self):
        payload = build_supervisor_status(
            service_state="running",
            monitor_enabled=False,
            monitor_state="disabled",
        )
        payload["POLYMARKET_PRIVATE_KEY"] = "must-not-be-returned"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            loaded = read_supervisor_status(target)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", loaded)
        self.assertEqual(loaded["monitor_state"], "disabled")


if __name__ == "__main__":
    unittest.main()
