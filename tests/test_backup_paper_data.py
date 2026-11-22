import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backup_paper_data import (
    create_paper_data_backups,
    restore_paper_data_backup,
    verify_paper_data_backup,
)
from perps_order_journal import PerpsOrderJournal


class PaperDataBackupTests(unittest.TestCase):
    def _database(self, path, value):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample(value) VALUES(?)", (value,))
            connection.commit()
        finally:
            connection.close()

    def test_backs_up_both_databases_and_writes_verified_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            self._database(paper, "trade")
            self._database(history, "market-snapshot")
            result = create_paper_data_backups(
                [paper, history], root / "backups",
                now=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(len(result["files"]), 2)
            self.assertTrue(all(item["integrity_check"] == "ok" for item in result["files"]))
            self.assertTrue(all(len(item["sha256"]) == 64 for item in result["files"]))
            manifest_path = root / "backups" / result["manifest_name"]
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(len(manifest["files"]), 2)
            self.assertTrue(verify_paper_data_backup(manifest_path)["complete"])
            for item in result["files"]:
                db = sqlite3.connect(root / "backups" / item["backup_name"])
                try:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 1)
                finally:
                    db.close()

    def test_missing_source_is_reported_but_existing_source_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            self._database(paper, "trade")
            result = create_paper_data_backups(
                [paper, root / "missing.sqlite3"], root / "backups"
            )
            self.assertEqual(result["skipped_missing_sources"], ["missing.sqlite3"])
            self.assertEqual(len(result["files"]), 1)

    def test_retention_prunes_old_backups_per_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            self._database(paper, "trade")
            for day in range(1, 4):
                create_paper_data_backups(
                    [paper], root / "backups", retain=1,
                    now=datetime(2026, 11, day, tzinfo=timezone.utc),
                )
            backup_dir = root / "backups"
            backups = list(backup_dir.glob("paper-*.sqlite3"))
            manifests = list(backup_dir.glob("paper-backup-manifest-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(len(manifests), 1)
            self.assertTrue(verify_paper_data_backup(manifests[0])["complete"])

    def test_perps_order_journal_backup_can_be_reopened_and_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            perps_path = root / "perps_order_journal.sqlite3"
            self._database(paper, "paper")
            self._database(history, "market")
            journal = PerpsOrderJournal(perps_path)
            journal.create_intent(
                client_order_id="a" * 32,
                instrument_id=17,
                side="buy",
                quantity="0.5",
                position_intent="open_long",
                time_in_force="ioc",
            )
            journal.begin_submission("a" * 32)

            result = create_paper_data_backups(
                [paper, history, perps_path],
                root / "backups",
                now=datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc),
            )
            perps_item = next(
                item for item in result["files"]
                if item["source_name"] == perps_path.name
            )
            backup_path = root / "backups" / perps_item["backup_name"]
            restored = PerpsOrderJournal(backup_path)
            state = restored.get_intent("a" * 32)
            self.assertEqual(state["status"], "submission_pending")
            self.assertEqual(state["instrument_id"], 17)
            self.assertFalse(state["automatic_retry_permitted"])
            self.assertEqual(
                [event["event_type"] for event in restored.list_events("a" * 32)],
                ["INTENT_PREPARED", "SUBMISSION_STARTED"],
            )
            self.assertTrue(restored.integrity_check()["integrity_ok"])
            self.assertEqual(perps_item["integrity_check"], "ok")
            self.assertEqual(len(perps_item["sha256"]), 64)

    def test_manifest_verification_and_restore_to_new_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            self._database(paper, "trade")
            self._database(history, "snapshot")
            result = create_paper_data_backups(
                [paper, history],
                root / "backups",
                now=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
            )
            manifest = root / "backups" / result["manifest_name"]

            verified = verify_paper_data_backup(manifest)
            self.assertTrue(verified["complete"])
            self.assertEqual(len(verified["files"]), 2)

            restored_path = root / "restore-target" / "paper-restored.sqlite3"
            restored = restore_paper_data_backup(
                manifest, "paper.sqlite3", restored_path
            )
            self.assertEqual(restored["integrity_check"], "ok")
            db = sqlite3.connect(restored_path)
            try:
                self.assertEqual(
                    db.execute("SELECT value FROM sample").fetchone()[0],
                    "trade",
                )
            finally:
                db.close()

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                restore_paper_data_backup(
                    manifest, "paper.sqlite3", restored_path
                )

    def test_manifest_verification_detects_tampered_backup_before_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            self._database(paper, "trade")
            result = create_paper_data_backups(
                [paper],
                root / "backups",
                now=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
            )
            backup_dir = root / "backups"
            manifest = backup_dir / result["manifest_name"]
            backup = backup_dir / result["files"][0]["backup_name"]
            with backup.open("ab") as handle:
                handle.write(b"tamper")

            with self.assertRaisesRegex(ValueError, "size mismatch|SHA-256 mismatch"):
                verify_paper_data_backup(manifest)
            with self.assertRaisesRegex(ValueError, "size mismatch|SHA-256 mismatch"):
                restore_paper_data_backup(
                    manifest, "paper.sqlite3", root / "restored.sqlite3"
                )
            self.assertFalse((root / "restored.sqlite3").exists())

    def test_manifest_rejects_path_traversal_backup_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            self._database(paper, "trade")
            result = create_paper_data_backups(
                [paper],
                root / "backups",
                now=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
            )
            manifest_path = root / "backups" / result["manifest_name"]
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][0]["backup_name"] = "../outside.sqlite3"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe or duplicate filename"):
                verify_paper_data_backup(manifest_path)

    def test_failed_multi_database_backup_preserves_previous_verified_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            corrupt = root / "corrupt.sqlite3"
            self._database(paper, "old-trade")
            self._database(history, "old-history")
            backup_dir = root / "backups"
            first = create_paper_data_backups(
                [paper, history],
                backup_dir,
                retain=1,
                now=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
            )
            first_manifest = backup_dir / first["manifest_name"]
            before_paper = sorted(p.name for p in backup_dir.glob("paper-*.sqlite3"))
            before_history = sorted(p.name for p in backup_dir.glob("history-*.sqlite3"))
            self._database(paper, "new-trade")
            corrupt.write_bytes(b"not sqlite")

            with self.assertRaises(sqlite3.DatabaseError):
                create_paper_data_backups(
                    [paper, corrupt],
                    backup_dir,
                    retain=1,
                    now=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
                )

            self.assertEqual(
                sorted(p.name for p in backup_dir.glob("paper-*.sqlite3")),
                before_paper,
            )
            self.assertEqual(
                sorted(p.name for p in backup_dir.glob("history-*.sqlite3")),
                before_history,
            )
            self.assertTrue(first_manifest.is_file())
            self.assertTrue(verify_paper_data_backup(first_manifest)["complete"])
            self.assertEqual(list(backup_dir.glob("*.tmp")), [])
            self.assertEqual(list(backup_dir.glob(".*.tmp")), [])

    def test_corrupt_database_is_not_published_as_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a sqlite database")
            with self.assertRaises(sqlite3.DatabaseError):
                create_paper_data_backups([corrupt], root / "backups")
            self.assertEqual(list((root / "backups").glob("corrupt-*.sqlite3")), [])

    def test_retention_and_timestamp_validation_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            self._database(paper, "trade")
            with self.assertRaises(ValueError):
                create_paper_data_backups([paper], root / "backups", retain=0)
            with self.assertRaises(ValueError):
                create_paper_data_backups(
                    [paper], root / "backups",
                    now=datetime(2026, 11, 7, 12, 0),
                )


if __name__ == "__main__":
    unittest.main()
