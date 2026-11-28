from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_kill_switch import read_live_kill_switch


class LiveKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "event-live-kill-switch.json"
        self.now = datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)

    def write_status(self, *, state="DISARMED", updated=None, extra=None):
        payload = {
            "schema_version": 1,
            "state": state,
            "updated_at_utc": (updated or self.now).isoformat(),
            "reason_code": "operator_review",
        }
        if extra:
            payload.update(extra)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return self.path

    def read(self, *, now=None):
        return read_live_kill_switch(self.path, now=now or self.now)

    def test_missing_file_is_engaged_and_unverified(self):
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_missing")

    def test_fresh_disarmed_status_is_verified(self):
        self.write_status()
        report = self.read()
        self.assertTrue(report["verified"])
        self.assertFalse(report["engaged"])
        self.assertEqual(report["state"], "disarmed")
        self.assertEqual(report["execution"], "none")

    def test_explicit_engaged_state_remains_engaged(self):
        self.write_status(state="ENGAGED")
        report = self.read()
        self.assertTrue(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["state"], "engaged")

    def test_stale_or_future_status_fails_closed(self):
        cases = (
            self.now - timedelta(seconds=61),
            self.now + timedelta(seconds=3),
        )
        for updated in cases:
            with self.subTest(updated=updated):
                self.write_status(updated=updated)
                report = self.read()
                self.assertFalse(report["verified"])
                self.assertTrue(report["engaged"])
                self.assertEqual(
                    report["reason_code"],
                    "kill_switch_status_stale_or_future_dated",
                )

    def test_naive_timestamp_fails_closed(self):
        self.write_status(updated=datetime(2026, 11, 28, 18, 0))
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])

    def test_duplicate_json_keys_fail_closed(self):
        self.path.write_text(
            '{"schema_version":1,"state":"DISARMED","state":"ENGAGED",'
            '"updated_at_utc":"2026-11-28T18:00:00+00:00",'
            '"reason_code":"operator_review"}',
            encoding="utf-8",
        )
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])

    def test_unexpected_fields_and_invalid_state_fail_closed(self):
        for overrides in (
            {"unexpected": "value"},
            {"state": "DISABLED"},
            {"reason_code": "not safe!"},
            {"schema_version": True},
        ):
            with self.subTest(overrides=overrides):
                self.write_status(extra=overrides)
                report = self.read()
                self.assertFalse(report["verified"])
                self.assertTrue(report["engaged"])

    def test_symlink_is_rejected(self):
        target = Path(self.temp.name) / "target.json"
        target.write_text(json.dumps({
            "schema_version": 1,
            "state": "DISARMED",
            "updated_at_utc": self.now.isoformat(),
            "reason_code": "operator_review",
        }), encoding="utf-8")
        self.path.symlink_to(target)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_symlink_rejected")

    def test_non_regular_or_oversized_file_fails_closed(self):
        self.path.mkdir()
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.path.rmdir()

        self.path.write_bytes(b"x" * 4097)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_size_invalid")


if __name__ == "__main__":
    unittest.main()
