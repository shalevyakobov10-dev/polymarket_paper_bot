import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from order_journal import OrderJournal
from order_lifecycle import create_order_intent, submission_policy


def new_intent(**overrides):
    values = {
        "client_order_id": "journal-order-001",
        "token_id": "token-456",
        "side": "BUY",
        "limit_price": 0.37,
        "size": 12,
    }
    values.update(overrides)
    return create_order_intent(**values)



def complete_risk_snapshot(**overrides):
    values = {
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
    values.update(overrides)
    return values


class OrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_created_intent_persists_and_reopens(self):
        saved = self.journal.create_intent(new_intent())
        reopened = OrderJournal(self.database)
        loaded = reopened.get_intent("journal-order-001")
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["status"], "prepared")
        self.assertEqual(len(reopened.list_events("journal-order-001")), 1)

    def test_replaying_same_intent_is_idempotent_but_changed_terms_are_rejected(self):
        first = self.journal.create_intent(new_intent())
        replay = self.journal.create_intent(new_intent())
        self.assertEqual(first, replay)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), 1)
        with self.assertRaisesRegex(ValueError, "different order terms"):
            self.journal.create_intent(new_intent(size=13))

    def test_submission_pending_is_committed_and_duplicate_attempt_is_blocked(self):
        self.journal.create_intent(new_intent())
        pending = self.journal.begin_submission("journal-order-001")
        self.assertEqual(pending["status"], "submission_pending")
        reloaded = OrderJournal(self.database).get_intent("journal-order-001")
        self.assertEqual(reloaded["status"], "submission_pending")
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            self.journal.begin_submission("journal-order-001")

    def test_ambiguous_outcome_and_not_found_survive_restart(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        unknown = self.journal.record_submission_response(
            "journal-order-001", outcome="ambiguous", reason_code="transport_timeout"
        )
        self.assertEqual(unknown["status"], "submission_unknown")
        restarted = OrderJournal(self.database)
        still_unknown = restarted.reconcile_submission(
            "journal-order-001", outcome="not_found"
        )
        self.assertEqual(still_unknown["status"], "submission_unknown")
        self.assertFalse(submission_policy(still_unknown)["automatic_retry_permitted"])

    def test_crash_recovery_converts_pending_to_unknown_not_prepared(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        restarted = OrderJournal(self.database)
        self.assertEqual(restarted.recover_interrupted_submissions(), 1)
        recovered = restarted.get_intent("journal-order-001")
        self.assertEqual(recovered["status"], "submission_unknown")
        self.assertEqual(recovered["last_reason_code"], "process_restart_during_submission")
        self.assertEqual(recovered["submission_attempts"], 1)
        self.assertEqual(restarted.recover_interrupted_submissions(), 0)
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            restarted.begin_submission("journal-order-001")

    def test_recovery_is_idempotent_and_does_not_touch_resolved_orders(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="rejected", reason_code="venue_rejected"
        )
        self.assertEqual(self.journal.recover_interrupted_submissions(), 0)
        self.assertEqual(self.journal.get_intent("journal-order-001")["status"], "rejected")

    def test_event_journal_records_state_transitions_in_order(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-123"
        )
        events = self.journal.list_events("journal-order-001")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["intent_created", "submission_started", "submission_response_accepted"],
        )
        self.assertEqual(events[-1]["state"]["status"], "open")

    def test_two_concurrent_workers_cannot_begin_the_same_order_twice(self):
        self.journal.create_intent(new_intent())
        barrier = threading.Barrier(2)
        successes = []
        failures = []

        def worker():
            barrier.wait()
            try:
                successes.append(self.journal.begin_submission("journal-order-001"))
            except Exception as exc:  # Capture the losing concurrent transition.
                failures.append(exc)

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        final_state = self.journal.get_intent("journal-order-001")
        self.assertEqual(final_state["submission_attempts"], 1)
        self.assertEqual(final_state["status"], "submission_pending")


    def test_cumulative_observations_are_durable_and_duplicate_snapshots_are_noops(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-123"
        )
        first = self.journal.record_order_observation(
            "journal-order-001", outcome="partially_filled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=3,
        )
        self.assertEqual(first["filled_size"], "3")
        event_count = len(self.journal.list_events("journal-order-001"))
        duplicate = self.journal.record_order_observation(
            "journal-order-001", outcome="partially_filled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=3,
        )
        self.assertEqual(duplicate, first)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), event_count)
        cancelled = self.journal.record_order_observation(
            "journal-order-001", outcome="cancelled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=3,
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["filled_size"], "3")
        self.assertEqual(self.journal.list_events("journal-order-001")[-1]["event_type"],
                         "order_observation_cancelled")

    def test_duplicate_terminal_snapshot_does_not_append_second_event(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-123"
        )
        first = self.journal.record_order_observation(
            "journal-order-001", outcome="filled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=12,
        )
        before = len(self.journal.list_events("journal-order-001"))
        replay = self.journal.record_order_observation(
            "journal-order-001", outcome="filled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=12,
        )
        self.assertEqual(replay, first)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), before)

    def test_stale_fill_snapshot_cannot_reduce_persisted_cumulative_fill(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-123"
        )
        self.journal.record_order_observation(
            "journal-order-001", outcome="partially_filled",
            matched_client_order_id="journal-order-001",
            exchange_order_id="venue-123", cumulative_filled_size=5,
        )
        before = self.journal.get_intent("journal-order-001")
        with self.assertRaisesRegex(ValueError, "cannot decrease"):
            self.journal.record_order_observation(
                "journal-order-001", outcome="partially_filled",
                matched_client_order_id="journal-order-001",
                exchange_order_id="venue-123", cumulative_filled_size=2,
            )
        self.assertEqual(self.journal.get_intent("journal-order-001"), before)



    def test_create_intent_with_status_reports_atomic_created_flag(self):
        intent = new_intent()
        saved, created = self.journal.create_intent_with_status(intent)
        self.assertTrue(created)
        self.assertEqual(saved, intent)
        replay, replay_created = self.journal.create_intent_with_status(intent)
        self.assertFalse(replay_created)
        self.assertEqual(replay, saved)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), 1)

    def test_concurrent_identical_intent_creation_has_one_creator(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            barrier.wait()
            try:
                results.append(self.journal.create_intent_with_status(new_intent()))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=10)

        self.assertTrue(all(not worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for _state, created in results if created), 1)
        self.assertEqual(sum(1 for _state, created in results if not created), 1)
        self.assertEqual(len(self.journal.list_events("journal-order-001")), 1)



    def test_account_risk_sync_state_tracks_success_failure_and_recovery(self):
        self.assertIsNone(self.journal.get_latest_account_risk_sync_state())
        first_id = self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        first = self.journal.get_latest_account_risk_sync_state(max_age_seconds=60)
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["snapshot_id"], first_id)
        self.assertTrue(first["fresh"])

        self.journal.record_account_risk_snapshot_failure(error_code="TimeoutError")
        failed = self.journal.get_latest_account_risk_sync_state(max_age_seconds=60)
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["snapshot_id"])
        self.assertEqual(failed["error_code"], "TimeoutError")
        self.assertFalse(failed["fresh"])

        second_id = self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        recovered = self.journal.get_latest_account_risk_sync_state(max_age_seconds=60)
        self.assertEqual(recovered["status"], "success")
        self.assertEqual(recovered["snapshot_id"], second_id)
        self.assertTrue(recovered["fresh"])

    def test_successful_sync_state_expires_independently_of_snapshot(self):
        attempted = datetime.now(timezone.utc)
        snapshot_id = self.journal.save_account_risk_snapshot(
            complete_risk_snapshot(captured_at_utc=attempted.isoformat())
        )
        stale = self.journal.get_latest_account_risk_sync_state(
            max_age_seconds=5,
            now=attempted + __import__("datetime").timedelta(seconds=6),
        )
        self.assertEqual(stale["status"], "success")
        self.assertEqual(stale["snapshot_id"], snapshot_id)
        self.assertFalse(stale["fresh"])

    def test_delayed_older_success_cannot_overwrite_a_newer_failed_sync(self):
        failed_at = datetime.now(timezone.utc)
        newer_success_id = self.journal.save_account_risk_snapshot(
            complete_risk_snapshot(
                captured_at_utc=(failed_at - timedelta(milliseconds=500)).isoformat()
            )
        )
        self.journal.record_account_risk_snapshot_failure(
            error_code="ConnectionError",
            attempted_at=failed_at,
        )
        delayed_snapshot = complete_risk_snapshot(
            captured_at_utc=(failed_at - timedelta(seconds=1)).isoformat()
        )
        self.journal.save_account_risk_snapshot(delayed_snapshot)
        self.assertEqual(
            self.journal.get_latest_account_risk_snapshot()["snapshot_id"],
            newer_success_id,
        )
        sync_state = self.journal.get_latest_account_risk_sync_state(max_age_seconds=60)
        self.assertEqual(sync_state["status"], "failed")
        self.assertEqual(sync_state["error_code"], "ConnectionError")
        self.assertFalse(sync_state["fresh"])

    def test_failed_sync_wins_exact_timestamp_tie_against_delayed_success(self):
        attempted_at = datetime.now(timezone.utc)
        self.journal.save_account_risk_snapshot(
            complete_risk_snapshot(captured_at_utc=attempted_at.isoformat())
        )
        self.journal.record_account_risk_snapshot_failure(
            error_code="TimeoutError",
            attempted_at=attempted_at,
        )
        # Simulate a successful fetch that began at the same instant but was
        # written after the failure. Equal timestamps must not clear the failure.
        self.journal.save_account_risk_snapshot(
            complete_risk_snapshot(captured_at_utc=attempted_at.isoformat())
        )
        state = self.journal.get_latest_account_risk_sync_state(max_age_seconds=60)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_code"], "TimeoutError")
        self.assertFalse(state["fresh"])

    def test_failed_latest_sync_blocks_atomic_intent_commit(self):
        snapshot_id = self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        intent = new_intent()
        intent["market_condition_id"] = "condition-1"
        intent["account_risk_snapshot_id"] = snapshot_id
        self.journal.record_account_risk_snapshot_failure(error_code="TimeoutError")
        with self.assertRaisesRegex(ValueError, "sync is not the latest successful attempt"):
            self.journal.create_intent_with_status(intent)
        self.assertIsNone(self.journal.get_intent(intent["client_order_id"]))

    def test_failed_sync_rejects_unsafe_error_code(self):
        with self.assertRaisesRegex(ValueError, "sanitized identifier"):
            self.journal.record_account_risk_snapshot_failure(
                error_code="timeout with credential=secret"
            )

    def test_account_risk_snapshot_rejects_inconsistent_derived_values(self):
        cases = [
            (
                {"collateral_balance_usd": "NaN"},
                "field collateral_balance_usd is outside valid bounds",
            ),
            (
                {"current_open_positions": True},
                "current_open_positions must be a nonnegative integer",
            ),
            (
                {"current_open_exposure_usd": "1"},
                "current open exposure is inconsistent",
            ),
            (
                {"open_position_assets_by_condition": {"condition-1": "token-1"}},
                "values must be lists",
            ),
            (
                {"conservative_available_collateral_usd": "99"},
                "available collateral is inconsistent",
            ),
            (
                {"collateral_balance_base_units": "not-an-integer"},
                "base_units must be a nonnegative integer string",
            ),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, expected):
                    self.journal.save_account_risk_snapshot(
                        complete_risk_snapshot(**overrides)
                    )

    def test_account_risk_snapshot_rejects_exposure_below_cost_or_market_value(self):
        snapshot = complete_risk_snapshot(
            open_position_value_usd="5",
            open_position_cost_usd="8",
            open_position_risk_exposure_usd="7",
            current_open_exposure_usd="7",
        )
        with self.assertRaisesRegex(ValueError, "cannot be below market value or cost"):
            self.journal.save_account_risk_snapshot(snapshot)

    def test_account_risk_snapshot_rejects_duplicate_assets_and_future_timestamps(self):
        duplicate_assets = complete_risk_snapshot(
            open_position_assets_by_condition={"condition-1": ["token-a", "token-a"]}
        )
        with self.assertRaisesRegex(ValueError, "duplicate asset IDs"):
            self.journal.save_account_risk_snapshot(duplicate_assets)

        future = complete_risk_snapshot(
            captured_at_utc=(datetime.now(timezone.utc) + __import__("datetime").timedelta(minutes=2)).isoformat()
        )
        with self.assertRaisesRegex(ValueError, "more than 60 seconds in the future"):
            self.journal.save_account_risk_snapshot(future)

    def test_staged_intent_must_reference_latest_snapshot_inside_commit(self):
        first_snapshot_id = self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        intent = new_intent()
        intent["market_condition_id"] = "condition-1"
        intent["account_risk_snapshot_id"] = first_snapshot_id
        saved, created = self.journal.create_intent_with_status(intent)
        self.assertTrue(created)
        self.assertEqual(saved["account_risk_snapshot_id"], first_snapshot_id)

        self.journal.save_account_risk_snapshot(complete_risk_snapshot())
        stale_intent = new_intent(client_order_id="journal-order-stale")
        stale_intent["market_condition_id"] = "condition-1"
        stale_intent["account_risk_snapshot_id"] = first_snapshot_id
        with self.assertRaisesRegex(ValueError, "snapshot changed before intent commit"):
            self.journal.create_intent_with_status(stale_intent)
        self.assertIsNone(self.journal.get_intent("journal-order-stale"))

    def test_exchange_order_id_index_survives_restart(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-index-001"
        )
        self.assertEqual(
            self.journal.find_client_order_id_by_exchange_order_id("venue-index-001"),
            "journal-order-001",
        )
        reopened = OrderJournal(self.database)
        self.assertEqual(
            reopened.find_client_order_id_by_exchange_order_id("venue-index-001"),
            "journal-order-001",
        )
        self.assertIsNone(
            reopened.find_client_order_id_by_exchange_order_id("unknown-venue-id")
        )

    def test_exchange_order_id_cannot_be_linked_to_two_local_intents(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-shared-001"
        )
        second = new_intent(client_order_id="journal-order-002")
        self.journal.create_intent(second)
        self.journal.begin_submission("journal-order-002")
        with self.assertRaisesRegex(ValueError, "already linked"):
            self.journal.record_submission_response(
                "journal-order-002", outcome="accepted", exchange_order_id="venue-shared-001"
            )
        self.assertEqual(
            self.journal.get_intent("journal-order-002")["status"],
            "submission_pending",
        )
        self.assertEqual(
            self.journal.find_client_order_id_by_exchange_order_id("venue-shared-001"),
            "journal-order-001",
        )

    def test_exchange_order_index_is_rebuilt_from_existing_journal_state(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="accepted", exchange_order_id="venue-backfill-001"
        )
        connection = self.journal._connect()
        try:
            connection.execute("DROP TABLE order_exchange_index")
        finally:
            connection.close()
        reopened = OrderJournal(self.database)
        self.assertEqual(
            reopened.find_client_order_id_by_exchange_order_id("venue-backfill-001"),
            "journal-order-001",
        )

    def test_unknown_order_can_only_reconcile_when_client_id_matches(self):
        self.journal.create_intent(new_intent())
        self.journal.begin_submission("journal-order-001")
        self.journal.record_submission_response(
            "journal-order-001", outcome="ambiguous"
        )
        result = self.journal.reconcile_submission(
            "journal-order-001",
            outcome="found_open",
            matched_client_order_id="someone-else",
            exchange_order_id="venue-987",
        )
        self.assertEqual(result["status"], "manual_review")


if __name__ == "__main__":
    unittest.main()
