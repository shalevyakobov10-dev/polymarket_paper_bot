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

    def test_restore_preserves_interrupted_submission_until_exclusive_recovery(self):
        intent = create_order_intent(
            client_order_id="restore-pending-001",
            token_id="token-restore",
            side="BUY",
            limit_price=0.42,
            size=10,
        )
        self.journal.create_intent(intent)
        self.journal.begin_submission("restore-pending-001")
        self.journal.backup_to(self.backup_path)
        destination = self.root / "pending-restored.sqlite3"

        restore_order_journal(self.backup_path, destination)
        restored = OrderJournal(destination)

        self.assertEqual(
            restored.get_intent("restore-pending-001")["status"],
            "submission_pending",
        )
        self.assertEqual(restored.recover_interrupted_submissions(), 1)
        state = restored.get_intent("restore-pending-001")
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["submission_attempts"], 1)
        self.assertEqual(
            state["last_reason_code"],
            "process_restart_during_submission",
        )

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

    def test_restore_rejects_json_valid_but_invalid_order_lifecycle_state(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT state_json FROM order_intents WHERE client_order_id = ?",
                ("restore-order-001",),
            ).fetchone()[0]
            state = json.loads(raw)
            state["status"] = "not_a_real_order_state"
            connection.execute(
                "UPDATE order_intents SET state_json = ? WHERE client_order_id = ?",
                (json.dumps(state), "restore-order-001"),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "invalid-lifecycle-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "invalid order lifecycle state"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_valid_event_state_with_illegal_predecessor(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            rows = connection.execute(
                "SELECT event_id, state_json FROM order_events "
                "WHERE client_order_id = ? ORDER BY event_id",
                ("restore-order-001",),
            ).fetchall()
            event_id, raw = rows[1]
            state = json.loads(raw)
            state["status"] = "submission_unknown"
            state["last_reason_code"] = "order_not_visible_yet"
            state["reconciliation_attempts"] = 1
            connection.execute(
                "UPDATE order_events SET event_type = ?, state_json = ? WHERE event_id = ?",
                ("reconciliation_not_found", json.dumps(state), event_id),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "illegal-predecessor-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "illegal state transition"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_event_type_that_disagrees_with_its_target_state(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            connection.execute(
                "UPDATE order_events SET event_type = ? "
                "WHERE event_id = (SELECT MAX(event_id) FROM order_events WHERE client_order_id = ?)",
                ("submission_started", "restore-order-001"),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "event-type-mismatch-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "event type does not match"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_json_valid_but_invalid_audit_lifecycle_state(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT state_json FROM order_events "
                "WHERE client_order_id = ? ORDER BY event_id LIMIT 1",
                ("restore-order-001",),
            ).fetchone()[0]
            state = json.loads(raw)
            state["automatic_retry_permitted"] = True
            connection.execute(
                "UPDATE order_events SET state_json = ? "
                "WHERE event_id = (SELECT MIN(event_id) FROM order_events WHERE client_order_id = ?)",
                (json.dumps(state), "restore-order-001"),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "invalid-audit-state-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "invalid order audit lifecycle state"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_latest_audit_state_that_differs_from_current_intent(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT state_json FROM order_events "
                "WHERE client_order_id = ? ORDER BY event_id DESC LIMIT 1",
                ("restore-order-001",),
            ).fetchone()[0]
            state = json.loads(raw)
            # Keep a valid lifecycle state and the same local/venue identifiers,
            # but change an immutable order term so it no longer matches current.
            state["token_id"] = "different-token-id"
            connection.execute(
                "UPDATE order_events SET state_json = ? "
                "WHERE event_id = (SELECT MAX(event_id) FROM order_events WHERE client_order_id = ?)",
                (json.dumps(state), "restore-order-001"),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "audit-mismatch-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "immutable order terms|latest audit event"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_asset_ids_that_duplicate_after_normalization(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT snapshot_json FROM account_risk_snapshots LIMIT 1"
            ).fetchone()[0]
            snapshot = json.loads(raw)
            snapshot["open_order_assets_by_condition"] = {
                "condition-1": ["token-1", " token-1 "]
            }
            connection.execute(
                "UPDATE account_risk_snapshots SET snapshot_json = ?",
                (json.dumps(snapshot),),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "duplicate-assets-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "duplicate asset IDs"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_audit_attempt_counter_jump_without_transition(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT state_json FROM order_events "
                "WHERE client_order_id = ? ORDER BY event_id DESC LIMIT 1",
                ("restore-order-001",),
            ).fetchone()[0]
            state = json.loads(raw)
            state["reconciliation_attempts"] = 1
            connection.execute(
                "UPDATE order_events SET state_json = ? "
                "WHERE event_id = (SELECT MAX(event_id) FROM order_events WHERE client_order_id = ?)",
                (json.dumps(state), "restore-order-001"),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "counter-jump-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "reconciliation counter has an invalid transition"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_json_valid_but_inconsistent_account_risk_arithmetic(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            raw = connection.execute(
                "SELECT snapshot_json FROM account_risk_snapshots LIMIT 1"
            ).fetchone()[0]
            snapshot = json.loads(raw)
            snapshot["open_buy_commitment_usd"] = "10"
            connection.execute(
                "UPDATE account_risk_snapshots SET snapshot_json = ?",
                (json.dumps(snapshot),),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "risk-arithmetic-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "available collateral is inconsistent"):
            restore_order_journal(self.backup_path, destination)

        self.assertFalse(destination.exists())

    def test_restore_rejects_success_sync_timestamp_not_matching_snapshot(self):
        self._make_backup_with_order()
        connection = sqlite3.connect(self.backup_path)
        try:
            connection.execute(
                "UPDATE account_risk_sync_state "
                "SET attempted_at_utc = ? WHERE singleton_id = 1",
                ("2020-01-01T00:00:00+00:00",),
            )
            connection.commit()
        finally:
            connection.close()
        destination = self.root / "risk-sync-time-restored.sqlite3"

        with self.assertRaisesRegex(ValueError, "timestamp differs"):
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
