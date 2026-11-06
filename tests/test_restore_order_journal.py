import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from order_journal import OrderJournal
from order_lifecycle import create_order_intent
from restore_order_journal import restore_order_journal


def complete_risk_snapshot():
    return {
        "snapshot_complete": True,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "collateral_decimals": 6,
        "collateral_balance_base_units": "100000000",
        "collateral_balance_usd": "100",
        "open_buy_commitment_usd": "0",
        "conservative_available_collateral_usd": "100",
        "collateral_shortfall_usd": "0",
        "current_open_exposure_usd": "0",
        "open_position_value_usd": "0",
        "open_position_cost_usd": "0",
        "open_position_risk_exposure_usd": "0",
        "open_unrealized_pnl_usd": "0",
        "open_sell_remaining_shares": "0",
        "current_open_positions": 0,
        "current_open_orders": 0,
        "open_position_assets_by_condition": {},
        "open_order_assets_by_condition": {},
        "execution": "none",
    }


class RestoreOrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.live_path = self.root / "live.sqlite3"
        self.backup_path = self.root / "backup.sqlite3"
        self.journal = OrderJournal(self.live_path)

    def tearDown(self):
        self.temp.cleanup()

    def _make_backup_with_order(self):
        intent = create_order_intent(
            client_order_id="restore-order-001",
            token_id="token-restore",
            side="BUY",
            limit_price=0.42,
            size=10,
        )
        self.journal.create_intent(intent)
        self.journal.begin_submission("restore-order-001")
        self.journal.record_submission_response(
            "restore-order-001",
            outcome="accepted",
            exchange_order_id="venue-restore-001",
        )
        self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        return self.journal.backup_to(self.backup_path)

    def test_restore_preserves_order_events_venue_index_and_risk_snapshot(self):
        self._make_backup_with_order()
        destination = self.root / "restored" / "order-journal.sqlite3"

        result = restore_order_journal(self.backup_path, destination)

        self.assertEqual(result["integrity_check"], "ok")
        self.assertGreater(result["size_bytes"], 0)
        self.assertEqual(result["restored_table_counts"]["order_intents"], 1)
        self.assertEqual(result["restored_table_counts"]["order_events"], 3)
        self.assertEqual(result["restored_table_counts"]["account_risk_snapshots"], 1)

        restored = OrderJournal(destination)
        state = restored.get_intent("restore-order-001")
        self.assertEqual(state["status"], "open")
        self.assertEqual(
            restored.find_client_order_id_by_exchange_order_id("venue-restore-001"),
            "restore-order-001",
        )
        self.assertEqual(
            [event["event_type"] for event in restored.list_events("restore-order-001")],
            ["intent_created", "submission_started", "submission_response_accepted"],
        )
        self.assertTrue(restored.get_latest_account_risk_snapshot()["fresh"])

    def test_restore_never_overwrites_an_existing_destination(self):
        self._make_backup_with_order()
        destination = self.root / "already-exists.sqlite3"
        original = b"preserve this file"
        destination.write_bytes(original)

        with self.assertRaisesRegex(FileExistsError, "already exists"):
            restore_order_journal(self.backup_path, destination)

        self.assertEqual(destination.read_bytes(), original)

    def test_restore_rejects_incomplete_journal_schema(self):
        incomplete = self.root / "incomplete.sqlite3"
        connection = sqlite3.connect(incomplete)
        try:
            connection.execute(
                "CREATE TABLE order_intents (client_order_id TEXT, state_json TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "must-not-exist.sqlite3"

        with self.assertRaisesRegex(ValueError, "required journal tables"):
            restore_order_journal(incomplete, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_malformed_persisted_state_json(self):
        self.journal.create_intent(create_order_intent(
            client_order_id="restore-order-bad-json",
            token_id="token-restore",
            side="BUY",
            limit_price=0.42,
            size=10,
        ))
        source_connection = sqlite3.connect(self.live_path)
        target_connection = sqlite3.connect(self.backup_path)
        try:
            source_connection.execute(
                "UPDATE order_intents SET state_json = ? WHERE client_order_id = ?",
                ("not-json", "restore-order-bad-json"),
            )
            source_connection.commit()
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        destination = self.root / "bad-json-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "malformed"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_backup_with_sqlite_sidecars(self):
        self._make_backup_with_order()
        sidecar = Path(f"{self.backup_path}-wal")
        sidecar.write_bytes(b"unexpected WAL sidecar")
        destination = self.root / "sidecar-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "sidecar files"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_venue_order_index_pointing_to_wrong_intent(self):
        self._make_backup_with_order()
        second_intent = create_order_intent(
            client_order_id="restore-order-002",
            token_id="token-restore",
            side="BUY",
            limit_price=0.42,
            size=10,
        )
        self.journal.create_intent(second_intent)
        # Rebuild backup so both intents exist, then create a logically corrupt
        # index that still satisfies the database foreign-key constraint.
        self.journal.backup_to(self.backup_path)
        connection = sqlite3.connect(self.backup_path)
        try:
            connection.execute(
                "UPDATE order_exchange_index SET client_order_id = ?",
                ("restore-order-002",),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "index-corrupt-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "venue-order index"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_successful_sync_state_referencing_unknown_snapshot(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            connection.execute(
                "UPDATE account_risk_sync_state SET snapshot_id = ? WHERE singleton_id = 1",
                ("missing-snapshot-id",),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "sync-corrupt-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "unknown snapshot"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_same_source_and_destination(self):
        self._make_backup_with_order()

        with self.assertRaisesRegex(ValueError, "must differ"):
            restore_order_journal(self.backup_path, self.backup_path)


if __name__ == "__main__":
    unittest.main()
