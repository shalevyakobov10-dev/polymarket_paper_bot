import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backup_paper_data import create_paper_data_backups


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
