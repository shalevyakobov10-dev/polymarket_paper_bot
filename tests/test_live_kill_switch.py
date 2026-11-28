from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live_kill_switch import engage_live_kill_switch, read_live_kill_switch


class LiveKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "event-live-kill-switch.json"
        self.owner_uid = self.path.parent.stat().st_uid
        self.gateway_uid = self.owner_uid + 10000
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
        if self.path.exists() and not self.path.is_symlink():
            self.path.chmod(0o600)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.path.chmod(0o444)
        return self.path

    def read(self, *, now=None, expected_owner_uid=None, gateway_uid=None):
        with patch("live_kill_switch.os.geteuid", return_value=(
            self.gateway_uid if gateway_uid is None else gateway_uid
        )):
            return read_live_kill_switch(
                self.path,
                now=now or self.now,
                expected_owner_uid=self.owner_uid if expected_owner_uid is None else expected_owner_uid,
            )

    def test_missing_file_is_engaged_and_unverified(self):
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_missing")

    def test_fresh_disarmed_status_is_verified_from_separate_operator_uid(self):
        self.write_status()
        report = self.read()
        self.assertTrue(report["verified"])
        self.assertFalse(report["engaged"])
        self.assertEqual(report["state"], "disarmed")
        self.assertEqual(report["owner_uid"], self.owner_uid)
        self.assertEqual(report["execution"], "none")

    def test_same_uid_cannot_self_authorize_disarmed_state(self):
        self.write_status()
        report = self.read(gateway_uid=self.owner_uid)
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_owner_must_be_separate_from_gateway")

    def test_expected_owner_must_match_parent_directory(self):
        self.write_status()
        report = self.read(expected_owner_uid=self.owner_uid + 1)
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_directory_owner_mismatch")

    def test_writable_file_or_directory_fails_closed(self):
        self.write_status()
        self.path.chmod(0o644)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_permissions_invalid")

        self.path.chmod(0o444)
        self.path.parent.chmod(0o770)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_directory_group_or_world_writable")

    def test_stale_or_future_status_fails_closed(self):
        self.path.parent.chmod(0o700)
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
                self.assertEqual(report["reason_code"], "kill_switch_status_stale_or_future_dated")

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
        self.path.chmod(0o444)
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
        target.chmod(0o444)
        self.path.symlink_to(target)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_symlink_rejected")

    def test_group_or_other_writable_status_file_fails_closed(self):
        self.write_status()
        self.path.chmod(0o666)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_permissions_invalid")

    def test_hard_link_status_file_fails_closed(self):
        self.write_status()
        alias = Path(self.temp.name) / "event-live-kill-switch-alias.json"
        try:
            alias.hardlink_to(self.path)
        except (NotImplementedError, OSError):
            self.skipTest("hard links are unavailable on this filesystem")
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_hardlink_rejected")

    def test_engage_command_atomically_engages_missing_switch(self):
        with patch.dict(os.environ, {"EVENT_LIVE_KILL_SWITCH_OWNER_UID": str(self.owner_uid)}), patch(
            "live_kill_switch.os.geteuid", return_value=self.owner_uid
        ):
            report = engage_live_kill_switch(
                self.path, reason_code="operator_request", now=self.now
            )
        self.assertTrue(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["state"], "engaged")
        self.assertEqual(report["reason_code"], "operator_request")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o444)

    def test_engage_command_overwrites_disarmed_state_with_engaged(self):
        self.write_status(state="DISARMED")
        with patch.dict(os.environ, {"EVENT_LIVE_KILL_SWITCH_OWNER_UID": str(self.owner_uid)}), patch(
            "live_kill_switch.os.geteuid", return_value=self.owner_uid
        ):
            report = engage_live_kill_switch(
                self.path, reason_code="incident_detected", now=self.now
            )
        self.assertTrue(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["state"], "engaged")
        self.assertEqual(report["reason_code"], "incident_detected")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o444)

    def test_engage_requires_operator_uid_and_owner_configuration(self):
        with patch.dict(os.environ, {"EVENT_LIVE_KILL_SWITCH_OWNER_UID": str(self.owner_uid)}), patch(
            "live_kill_switch.os.geteuid", return_value=self.gateway_uid
        ):
            with self.assertRaisesRegex(PermissionError, "operator UID"):
                engage_live_kill_switch(self.path, reason_code="operator_request", now=self.now)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PermissionError, "OWNER_UID"):
                engage_live_kill_switch(self.path, reason_code="operator_request", now=self.now)
        self.assertFalse(self.path.exists())

    def test_engage_command_rejects_unsafe_reason_code(self):
        with patch.dict(os.environ, {"EVENT_LIVE_KILL_SWITCH_OWNER_UID": str(self.owner_uid)}), patch(
            "live_kill_switch.os.geteuid", return_value=self.owner_uid
        ):
            for reason in ("", "operator request", "../switch", "a" * 65):
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, "reason_code"):
                        engage_live_kill_switch(self.path, reason_code=reason, now=self.now)
        self.assertFalse(self.path.exists())

    def test_non_regular_or_oversized_file_fails_closed(self):
        self.path.mkdir()
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.path.rmdir()
        self.path.write_bytes(b"x" * 4097)
        self.path.chmod(0o444)
        report = self.read()
        self.assertFalse(report["verified"])
        self.assertTrue(report["engaged"])
        self.assertEqual(report["reason_code"], "kill_switch_file_size_invalid")


if __name__ == "__main__":
    unittest.main()
