import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from historical_replay import replay_recorded_decisions


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paper_db = self.root / "paper.sqlite3"
        self.history_db = self.root / "history.sqlite3"
        self._create_paper_db()
        self._create_history_db()

    def tearDown(self):
        self.temp.cleanup()

    def _create_paper_db(self):
        with sqlite3.connect(self.paper_db) as db:
            db.execute(
                """
                CREATE TABLE decisions (
                    decision_id INTEGER PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    selected_direction TEXT,
                    decision TEXT NOT NULL
                )
                """
            )
            db.execute(
                "INSERT INTO decisions VALUES(1, ?, 'market-1', 'YES', 'ENTERED')",
                ("2026-11-07T10:00:00+00:00",),
            )
            # Skipped signals must never be replayed as executed entries.
            db.execute(
                "INSERT INTO decisions VALUES(2, ?, 'market-1', 'NO', 'SKIPPED')",
                ("2026-11-07T10:05:00+00:00",),
            )

    def _create_history_db(self):
        with sqlite3.connect(self.history_db) as db:
            db.execute(
                """
                CREATE TABLE market_snapshots (
                    snapshot_id INTEGER PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    yes_bid REAL NOT NULL,
                    yes_ask REAL NOT NULL,
                    no_bid REAL NOT NULL,
                    no_ask REAL NOT NULL,
                    yes_book_json TEXT NOT NULL,
                    no_book_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _snapshot(snapshot_id, observed_at, captured_at, *, yes_bid, yes_ask, bid_size=20, ask_size=20):
        yes_book = {
            "bids_top5": [{"price": yes_bid, "size": bid_size}],
            "asks_top5": [{"price": yes_ask, "size": ask_size}],
        }
        no_book = {
            "bids_top5": [{"price": 0.57, "size": 20}],
            "asks_top5": [{"price": 0.59, "size": 20}],
        }
        return (
            snapshot_id, "market-1", observed_at, captured_at,
            yes_bid, yes_ask, 0.57, 0.59,
            json.dumps(yes_book), json.dumps(no_book),
        )

    def _insert_snapshots(self, rows):
        with sqlite3.connect(self.history_db) as db:
            db.executemany(
                "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def _replay(self, **overrides):
        options = {
            "notional_usd": 4.2,
            "holding_period_hours": 1.0,
            "max_entry_snapshot_age_seconds": 300,
            "max_exit_delay_seconds": 300,
            "fee_rate": 0.01,
        }
        options.update(overrides)
        return replay_recorded_decisions(self.paper_db, self.history_db, **options)

    def test_entry_uses_latest_book_at_or_before_signal_and_never_future_book(self):
        self._insert_snapshots([
            self._snapshot(
                1, "2026-11-07T09:59:00+00:00", "2026-11-07T09:59:00+00:00",
                yes_bid=0.40, yes_ask=0.42,
            ),
            # This attractive later ask is after the decision and must not be used.
            self._snapshot(
                2, "2026-11-07T10:01:00+00:00", "2026-11-07T10:01:00+00:00",
                yes_bid=0.89, yes_ask=0.90,
            ),
            self._snapshot(
                3, "2026-11-07T11:01:00+00:00", "2026-11-07T11:01:00+00:00",
                yes_bid=0.50, yes_ask=0.52,
            ),
        ])

        report = self._replay()
        self.assertEqual(report["metrics"]["entered_decisions"], 1)
        self.assertEqual(report["metrics"]["closed_replays"], 1)
        trade = report["trades"][0]
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(trade["entry_average_price"], 0.42)
        self.assertEqual(trade["entry_observed_at"], "2026-11-07T09:59:00+00:00")
        self.assertEqual(trade["exit_average_price"], 0.50)
        self.assertEqual(trade["entry_snapshot_age_seconds"], 60.0)
        self.assertEqual(trade["exit_delay_seconds"], 60.0)
        # $4.20 buys 10 shares at $0.42; exit returns $5.00.
        # Apply 1% proportional fee to both legs: 5.00 - .05 - 4.20 - .042.
        self.assertAlmostEqual(trade["net_pnl_usd"], 0.708, places=8)
        self.assertAlmostEqual(report["metrics"]["total_fees_usd"], 0.092, places=8)
        self.assertIs(report["live_execution_eligible"], False)
        self.assertIs(report["out_of_sample_validated"], False)

    def test_stale_or_future_only_entry_books_are_excluded(self):
        self._insert_snapshots([
            self._snapshot(
                1, "2026-11-07T10:01:00+00:00", "2026-11-07T10:01:00+00:00",
                yes_bid=0.40, yes_ask=0.42,
            ),
        ])
        report = self._replay()
        self.assertEqual(report["metrics"]["closed_replays"], 0)
        self.assertEqual(report["trades"][0]["status"], "excluded")
        self.assertEqual(
            report["trades"][0]["reason"],
            "no_fresh_market_snapshot_at_or_before_signal",
        )

    def test_missing_exit_book_is_reported_unresolved_not_as_a_loss_or_win(self):
        self._insert_snapshots([
            self._snapshot(
                1, "2026-11-07T09:59:00+00:00", "2026-11-07T09:59:00+00:00",
                yes_bid=0.40, yes_ask=0.42,
            ),
        ])
        report = self._replay()
        self.assertEqual(report["metrics"]["closed_replays"], 0)
        self.assertEqual(report["metrics"]["unresolved_replays"], 1)
        self.assertIsNone(report["metrics"]["closed_win_rate_pct"])
        self.assertEqual(report["trades"][0]["reason"], "no_market_snapshot_within_exit_window")

    def test_insufficient_visible_exit_depth_remains_unresolved(self):
        self._insert_snapshots([
            self._snapshot(
                1, "2026-11-07T09:59:00+00:00", "2026-11-07T09:59:00+00:00",
                yes_bid=0.40, yes_ask=0.42,
            ),
            self._snapshot(
                2, "2026-11-07T11:01:00+00:00", "2026-11-07T11:01:00+00:00",
                yes_bid=0.50, yes_ask=0.52, bid_size=1,
            ),
        ])
        report = self._replay()
        self.assertEqual(report["metrics"]["closed_replays"], 0)
        self.assertEqual(report["metrics"]["unresolved_replays"], 1)
        self.assertEqual(
            report["trades"][0]["reason"],
            "insufficient_visible_exit_bid_depth",
        )

    def test_timezone_naive_decisions_are_excluded(self):
        with sqlite3.connect(self.paper_db) as db:
            # Keep this test focused on the malformed decision; the valid row is
            # intentionally changed to SKIPPED so it does not add a missing-book exclusion.
            db.execute("UPDATE decisions SET decision='SKIPPED' WHERE decision_id=1")
            db.execute(
                "INSERT INTO decisions VALUES(3, ?, 'market-1', 'YES', 'ENTERED')",
                ("2026-11-07T10:00:00",),
            )
        report = self._replay()
        self.assertEqual(report["metrics"]["entered_decisions"], 1)
        self.assertEqual(report["metrics"]["excluded_replays"], 1)
        naive = next(row for row in report["trades"] if row.get("decision_id") == 3)
        self.assertEqual(naive["reason"], "invalid_or_timezone_naive_decision_timestamp")

    def test_read_only_replay_does_not_change_source_database_rows(self):
        with sqlite3.connect(self.paper_db) as db:
            before = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self._insert_snapshots([
            self._snapshot(
                1, "2026-11-07T09:59:00+00:00", "2026-11-07T09:59:00+00:00",
                yes_bid=0.40, yes_ask=0.42,
            ),
            self._snapshot(
                2, "2026-11-07T11:01:00+00:00", "2026-11-07T11:01:00+00:00",
                yes_bid=0.50, yes_ask=0.52,
            ),
        ])
        self._replay()
        with sqlite3.connect(self.paper_db) as db:
            after = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
