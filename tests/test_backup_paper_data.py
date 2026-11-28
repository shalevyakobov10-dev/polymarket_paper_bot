import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backup_paper_data import create_paper_data_backups
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
            manifest = json.loads((root / "backups" / result["manifest_name"]).read_text())
            self.assertEqual(len(manifest["files"]), 2)
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
            backups = list((root / "backups").glob("paper-*.sqlite3"))
            self.assertEqual(len(backups), 1)

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

    def test_event_order_journal_backup_can_be_reopened_and_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.sqlite3"
            history = root / "history.sqlite3"
            event_path = root / "order_journal.sqlite3"
            self._database(paper, "paper")
            self._database(history, "market")
            from order_journal import OrderJournal, create_order_intent
            journal = OrderJournal(event_path)
            journal.create_intent(create_order_intent(
                client_order_id="event-backup-order",
                token_id="token-public",
                side="BUY",
                limit_price="0.41",
                size="4",
            ))
            journal.begin_submission("event-backup-order")
            journal.record_submission_response(
                "event-backup-order", outcome="ambiguous",
                reason_code="transport_timeout",
            )
            result = create_paper_data_backups(
                [paper, history, event_path],
                root / "backups",
                now=datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc),
            )
            event_item = next(
                item for item in result["files"]
                if item["source_name"] == event_path.name
            )
            restored = OrderJournal(root / "backups" / event_item["backup_name"])
            state = restored.get_intent("event-backup-order")
            self.assertEqual(state["status"], "submission_unknown")
            self.assertFalse(state["automatic_retry_permitted"])
            self.assertEqual(
                [event["event_type"] for event in restored.list_events("event-backup-order")],
                ["INTENT_PREPARED", "SUBMISSION_STARTED", "SUBMISSION_UNKNOWN"],
            )
            self.assertEqual(event_item["integrity_check"], "ok")
            self.assertEqual(len(event_item["sha256"]), 64)

    def test_symlink_database_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "real.sqlite3"
            alias = root / "alias.sqlite3"
            self._database(source, "paper")
            try:
                alias.symlink_to(source)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable on this filesystem")
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                create_paper_data_backups([alias], root / "backups")
            self.assertFalse((root / "backups").exists())

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
