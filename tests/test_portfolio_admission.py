from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from portfolio_admission import PortfolioAdmissionCoordinator, PortfolioAdmissionError


class PortfolioAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admissions.sqlite3"
        self.coordinator = PortfolioAdmissionCoordinator(self.path)
        self.now = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)

    def reserve(self, admission_id, *, venue="POLYMARKET", exposure="40", snapshot="0",
                positions=1, snapshot_positions=0, maximum="100", max_positions=3,
                snapshot_at=None, now=None):
        return self.coordinator.reserve(
            admission_id=admission_id, venue=venue, exposure_usd=exposure,
            positions=positions, snapshot_exposure_usd=snapshot,
            snapshot_positions=snapshot_positions,
            snapshot_at=snapshot_at or self.now,
            max_snapshot_age_seconds=120, max_total_exposure_usd=maximum,
            max_total_positions=max_positions, now=now or self.now,
        )

    def test_reservation_is_durable_and_idempotent(self):
        first = self.reserve("poly-1")
        again = self.reserve("poly-1")
        self.assertTrue(first.approved)
        self.assertTrue(again.approved)
        self.assertEqual(again.reason, "idempotent_existing_reservation")
        self.assertEqual(first.status, "PENDING")

    def test_idempotency_key_cannot_change_payload(self):
        self.reserve("poly-1")
        with self.assertRaisesRegex(PortfolioAdmissionError, "payload_conflict"):
            self.reserve("poly-1", exposure="41")

    def test_cross_venue_pending_reservation_consumes_shared_capacity(self):
        self.assertTrue(self.reserve("poly-1", exposure="60").approved)
        result = self.reserve("perps-1", venue="PERPS", exposure="41")
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "combined_exposure_limit")

    def test_pending_reservation_consumes_position_slot(self):
        self.assertTrue(self.reserve("poly-1", positions=2, exposure="10").approved)
        result = self.reserve("perps-1", venue="PERPS", positions=2, exposure="10",
                              max_positions=3)
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "combined_position_limit")

    def test_reconciled_reservation_is_counted_for_older_snapshot(self):
        self.reserve("poly-1", exposure="60")
        self.coordinator.mark_committed("poly-1", now=self.now)
        self.coordinator.reconcile("poly-1", ledger_entry_id="trade-77",
                                   now=self.now + timedelta(seconds=1))
        old_snapshot = self.reserve("perps-old", venue="PERPS", exposure="41",
                                    snapshot_at=self.now, now=self.now + timedelta(seconds=2))
        self.assertFalse(old_snapshot.approved)

    def test_reconciled_reservation_is_not_double_counted_by_newer_snapshot(self):
        self.reserve("poly-1", exposure="60")
        self.coordinator.mark_committed("poly-1", now=self.now)
        reconciled_at = self.now + timedelta(seconds=1)
        self.coordinator.reconcile("poly-1", ledger_entry_id="trade-77", now=reconciled_at)
        newer_snapshot = self.reserve("perps-new", venue="PERPS", exposure="41",
                                       snapshot="60", snapshot_at=reconciled_at,
                                       now=reconciled_at + timedelta(seconds=1))
        self.assertFalse(newer_snapshot.approved)
        within_capacity = self.reserve("perps-small", venue="PERPS", exposure="40",
                                       snapshot="60", snapshot_at=reconciled_at,
                                       now=reconciled_at + timedelta(seconds=1))
        self.assertTrue(within_capacity.approved)

    def test_uncertain_reservation_is_not_auto_expired(self):
        self.reserve("poly-1", exposure="60")
        later = self.now + timedelta(days=2)
        result = self.reserve("perps-1", venue="PERPS", exposure="41",
                              snapshot_at=later, now=later)
        self.assertFalse(result.approved)

    def test_release_requires_verified_ledger_absence(self):
        self.reserve("poly-1")
        with self.assertRaisesRegex(PortfolioAdmissionError, "verified_ledger_absence"):
            self.coordinator.release("poly-1", confirmed_absent_from_ledger=False)
        self.coordinator.release("poly-1", confirmed_absent_from_ledger=True, now=self.now)
        result = self.reserve("perps-1", venue="PERPS", exposure="100")
        self.assertTrue(result.approved)

    def test_reconcile_is_idempotent_but_entry_identity_is_immutable(self):
        self.reserve("poly-1")
        self.coordinator.mark_committed("poly-1", now=self.now)
        self.coordinator.reconcile("poly-1", ledger_entry_id="trade-77", now=self.now)
        self.coordinator.reconcile("poly-1", ledger_entry_id="trade-77", now=self.now)
        with self.assertRaisesRegex(PortfolioAdmissionError, "entry_conflict"):
            self.coordinator.reconcile("poly-1", ledger_entry_id="trade-78", now=self.now)

    def test_stale_and_future_snapshots_fail_closed(self):
        with self.assertRaisesRegex(PortfolioAdmissionError, "stale_or_future"):
            self.reserve("stale", snapshot_at=self.now - timedelta(seconds=121),
                         now=self.now)
        with self.assertRaisesRegex(PortfolioAdmissionError, "stale_or_future"):
            self.reserve("future", snapshot_at=self.now + timedelta(seconds=3),
                         now=self.now)

    def test_naive_timestamp_fails_closed(self):
        with self.assertRaisesRegex(PortfolioAdmissionError, "timezone_aware"):
            self.reserve("naive", snapshot_at=datetime(2026, 11, 28, 12))

    def test_concurrent_cross_venue_reservations_cannot_overallocate(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(admission_id, venue):
            try:
                barrier.wait()
                results.append(self.reserve(admission_id, venue=venue, exposure="60"))
            except Exception as exc:  # surfaced below, not swallowed
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("poly-race", "POLYMARKET")),
            threading.Thread(target=worker, args=("perps-race", "PERPS")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.approved for result in results), 1)

    def test_storage_reopen_preserves_pending_reservation(self):
        self.reserve("poly-1", exposure="60")
        reopened = PortfolioAdmissionCoordinator(self.path)
        result = reopened.reserve("perps-1", venue="PERPS", exposure="41")
        self.assertFalse(result.approved)


if __name__ == "__main__":
    unittest.main()
