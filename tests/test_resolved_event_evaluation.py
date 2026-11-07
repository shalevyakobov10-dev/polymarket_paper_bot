import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from resolved_event_evaluation import (
    chronological_resolution_walk_forward,
    evaluate_resolved_event_entries,
    load_definitive_resolutions,
)


class ResolvedEventEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paper_db = self.root / "paper.sqlite3"
        self.history_db = self.root / "market-history.sqlite3"
        self.resolution_log = self.root / "resolutions.jsonl"
        self._create_databases()

    def tearDown(self):
        self.temp.cleanup()

    def _create_databases(self):
        with sqlite3.connect(self.paper_db) as db:
            db.executescript(
                """
                CREATE TABLE decisions (
                    decision_id INTEGER PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    selected_direction TEXT,
                    decision TEXT NOT NULL
                );
                CREATE TABLE trades (
                    trade_id INTEGER PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    shares REAL NOT NULL,
                    notional REAL NOT NULL,
                    entry_fee REAL NOT NULL,
                    signal_strength REAL NOT NULL,
                    entry_threshold REAL NOT NULL,
                    status TEXT NOT NULL,
                    closed_at TEXT,
                    pnl REAL
                );
                """
            )
        with sqlite3.connect(self.history_db) as db:
            db.execute(
                """
                CREATE TABLE market_snapshots (
                    snapshot_id INTEGER PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    yes_ask REAL NOT NULL,
                    no_ask REAL NOT NULL,
                    yes_book_json TEXT NOT NULL,
                    no_book_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _book(ask, bid=None, size=100):
        bid = ask - 0.02 if bid is None else bid
        return {
            "bids_top5": [{"price": bid, "size": size}],
            "asks_top5": [{"price": ask, "size": size}],
        }

    def _add_market(self, market_id, decision_at, *, direction="YES", outcome=1,
                    entry_price=0.40, fee=0.004, signal=0.90, threshold=0.75,
                    snapshot_at=None, resolution_at=None, trade_status="CLOSED",
                    actual_pnl=0.10):
        decided = datetime.fromisoformat(decision_at.replace("Z", "+00:00"))
        snapshot_dt = snapshot_at or decided
        resolved_dt = resolution_at or (decided + timedelta(days=2))
        shares = 5.0 / entry_price
        entry_fee = fee
        with sqlite3.connect(self.paper_db) as db:
            db.execute(
                "INSERT INTO decisions VALUES(NULL,?,?,?,'ENTERED')",
                (decided.isoformat(), market_id, direction),
            )
            db.execute(
                """
                INSERT INTO trades(
                    market_id,question,direction,opened_at,entry_price,shares,
                    notional,entry_fee,signal_strength,entry_threshold,status,closed_at,pnl
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    market_id, f"Will {market_id} happen?", direction, decided.isoformat(),
                    entry_price, shares, 5.0, entry_fee, signal, threshold, trade_status,
                    (resolved_dt - timedelta(hours=1)).isoformat() if trade_status == "CLOSED" else None,
                    actual_pnl if trade_status == "CLOSED" else None,
                ),
            )
        yes_book = self._book(entry_price)
        no_book = self._book(0.60)
        with sqlite3.connect(self.history_db) as db:
            db.execute(
                "INSERT INTO market_snapshots VALUES(NULL,?,?,?,?,?,?,?)",
                (
                    market_id, snapshot_dt.isoformat(), snapshot_dt.isoformat(),
                    entry_price, 0.60, json.dumps(yes_book), json.dumps(no_book),
                ),
            )
        record = {
            "market_id": market_id,
            "resolved_yes": int(outcome),
            "resolved_at": resolved_dt.isoformat(),
            "resolution_status": "resolved",
            "resolution_source": "https://example.com/resolution",
            "source": "public_gamma_resolved_market",
        }
        with self.resolution_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    def _evaluate(self, **kwargs):
        return evaluate_resolved_event_entries(
            self.paper_db,
            self.history_db,
            self.resolution_log,
            minimum_train_markets=2,
            minimum_test_markets=2,
            **kwargs,
        )

    def test_correct_and_incorrect_outcomes_use_binary_settlement_payout(self):
        self._add_market(
            "market-win", "2026-10-01T10:00:00+00:00",
            direction="YES", outcome=1, entry_price=0.40, fee=0.05,
        )
        self._add_market(
            "market-loss", "2026-10-02T10:00:00+00:00",
            direction="YES", outcome=0, entry_price=0.40, fee=0.05,
        )
        result = self._evaluate()
        self.assertEqual(result["metrics"]["n"], 2)
        winner = next(row for row in result["trades"] if row["market_id"] == "market-win")
        loser = next(row for row in result["trades"] if row["market_id"] == "market-loss")
        self.assertTrue(winner["prediction_correct"])
        self.assertAlmostEqual(winner["gross_settlement_payout_usd"], 12.5)
        self.assertAlmostEqual(winner["counterfactual_net_pnl_usd"], 7.45)
        self.assertFalse(loser["prediction_correct"])
        self.assertEqual(loser["gross_settlement_payout_usd"], 0.0)
        self.assertAlmostEqual(loser["counterfactual_net_pnl_usd"], -5.05)
        self.assertTrue(result["research_only"])
        self.assertFalse(result["live_execution_eligible"])
        self.assertFalse(result["out_of_sample_validated"])

    def test_entry_snapshot_after_decision_is_never_used(self):
        self._add_market(
            "future-book", "2026-10-01T10:00:00+00:00",
            snapshot_at=datetime(2026, 10, 1, 10, 1, tzinfo=timezone.utc),
        )
        result = self._evaluate()
        self.assertEqual(result["metrics"]["n"], 0)
        self.assertEqual(result["excluded"][0]["reason"], "no_fresh_pre_decision_market_snapshot")

    def test_entry_snapshot_must_be_fresh_and_ledger_fill_must_match(self):
        self._add_market(
            "stale-book", "2026-10-01T10:00:00+00:00",
            snapshot_at=datetime(2026, 10, 1, 9, 50, tzinfo=timezone.utc),
        )
        result = self._evaluate(max_entry_snapshot_age_seconds=60)
        self.assertEqual(result["metrics"]["n"], 0)
        self.assertEqual(result["excluded"][0]["reason"], "no_fresh_pre_decision_market_snapshot")

    def test_conflicting_resolution_labels_are_withheld(self):
        record = self._add_market("conflict-market", "2026-10-01T10:00:00+00:00", outcome=1)
        with self.resolution_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**record, "resolved_yes": 0}) + "\n")
        resolutions, conflicts, diagnostics = load_definitive_resolutions(self.resolution_log)
        self.assertNotIn("conflict-market", resolutions)
        self.assertIn("conflict-market", conflicts)
        self.assertEqual(diagnostics["conflicting_resolution_markets"], 1)
        result = self._evaluate()
        self.assertEqual(result["metrics"]["n"], 0)
        self.assertEqual(
            result["excluded"][0]["reason"],
            "conflicting_resolution_labels_across_retained_records",
        )

    def test_market_resolved_before_entry_is_excluded(self):
        self._add_market(
            "resolved-before-entry", "2026-10-01T10:00:00+00:00",
            resolution_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        )
        result = self._evaluate()
        self.assertEqual(result["metrics"]["n"], 0)
        self.assertEqual(result["excluded"][0]["reason"], "market_resolved_before_or_at_entry_decision")

    def test_repeated_market_decisions_count_only_once(self):
        self._add_market("repeat-market", "2026-10-01T10:00:00+00:00")
        # A second decision on the same market uses a different, later entry and
        # still shares the exact same binary outcome label.
        decided = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
        with sqlite3.connect(self.paper_db) as db:
            db.execute("INSERT INTO decisions VALUES(NULL,?,?,?,'ENTERED')",
                       (decided.isoformat(), "repeat-market", "YES"))
            db.execute(
                """
                INSERT INTO trades(
                    market_id,question,direction,opened_at,entry_price,shares,
                    notional,entry_fee,signal_strength,entry_threshold,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("repeat-market", "Will repeat-market happen?", "YES", decided.isoformat(),
                 0.40, 12.5, 5.0, 0.05, 0.95, 0.75, "OPEN"),
            )
        with sqlite3.connect(self.history_db) as db:
            db.execute(
                "INSERT INTO market_snapshots VALUES(NULL,?,?,?,?,?,?,?)",
                ("repeat-market", decided.isoformat(), decided.isoformat(), 0.40, 0.60,
                 json.dumps(self._book(0.40)), json.dumps(self._book(0.60))),
            )
        result = self._evaluate()
        self.assertEqual(result["metrics"]["n"], 1)
        self.assertEqual(result["coverage"]["excluded_decision_rows"], 1)
        self.assertEqual(
            result["excluded"][0]["reason"],
            "duplicate_market_decision_after_first_eligible_entry",
        )

    def test_walk_forward_purges_training_labels_not_available_at_fold_start(self):
        samples = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(20):
            decided = start + timedelta(days=index)
            resolved = decided + timedelta(hours=2)
            if index == 0:
                # This early decision resolves after fold 1 begins, so it cannot train fold 1.
                resolved = start + timedelta(days=11)
            samples.append({
                "market_id": f"m-{index}",
                "decision_at": decided.isoformat(),
                "market_resolved_at": resolved.isoformat(),
                "signal_strength": 0.90,
                "entry_threshold": 0.70,
                "entry_notional_usd": 5.0,
                "entry_fee_usd": 0.05,
                "settlement_fee_usd": 0.0,
                "gross_settlement_payout_usd": 12.5 if index % 2 == 0 else 0.0,
                "net_settlement_payout_usd": 12.5 if index % 2 == 0 else 0.0,
                "counterfactual_net_pnl_usd": 7.45 if index % 2 == 0 else -5.05,
                "counterfactual_net_roi_pct": 149.0 if index % 2 == 0 else -101.0,
                "entry_price_impact_bps": 0.0,
            })
        for row in samples:
            row["_decision_dt"] = datetime.fromisoformat(row["decision_at"])
            row["_resolved_dt"] = datetime.fromisoformat(row["market_resolved_at"])
        result = chronological_resolution_walk_forward(
            samples, train_fraction=0.4, folds=3,
            minimum_train_markets=4, minimum_test_markets=3,
        )
        self.assertEqual(result["folds_scored"], 3)
        first = result["folds"][0]
        self.assertEqual(first["training_markets_resolved_before_test_start"], 3)
        self.assertLess(
            datetime.fromisoformat(first["latest_training_resolution_at"]),
            datetime.fromisoformat(first["test_decision_start"]),
        )
        self.assertFalse(result["out_of_sample_validated"])
        self.assertFalse(result["live_execution_eligible"])

    def test_timezone_naive_resolution_is_invalid(self):
        self._add_market("naive-resolution", "2026-10-01T10:00:00+00:00")
        with self.resolution_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "market_id": "bad", "resolved_yes": 1, "resolved_at": "2026-10-01T12:00:00",
                "resolution_status": "resolved", "source": "public_gamma_resolved_market",
            }) + "\n")
        resolutions, _, diagnostics = load_definitive_resolutions(self.resolution_log)
        self.assertNotIn("bad", resolutions)
        self.assertEqual(diagnostics["invalid_resolution_rows"], 1)

    def test_read_only_evaluation_does_not_change_databases(self):
        self._add_market("readonly-market", "2026-10-01T10:00:00+00:00")
        with sqlite3.connect(self.paper_db) as db:
            before_decisions = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            before_trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        with sqlite3.connect(self.history_db) as db:
            before_snapshots = db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        self._evaluate()
        with sqlite3.connect(self.paper_db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], before_decisions)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], before_trades)
        with sqlite3.connect(self.history_db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0], before_snapshots)

    def test_walk_forward_rejects_invalid_parameters(self):
        with self.assertRaises(ValueError):
            chronological_resolution_walk_forward([], train_fraction=0.9)
        with self.assertRaises(ValueError):
            chronological_resolution_walk_forward([], folds=True)
        with self.assertRaises(ValueError):
            chronological_resolution_walk_forward([], minimum_train_markets=1)


if __name__ == "__main__":
    unittest.main()
