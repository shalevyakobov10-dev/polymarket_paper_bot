import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from unified_paper_risk import (
    UnifiedPaperRiskError,
    build_unified_paper_risk_snapshot,
)


NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


class UnifiedPaperRiskSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.poly = root / "poly.sqlite3"
        self.perps = root / "perps.sqlite3"
        self.history = root / "history.sqlite3"
        self._create_poly()
        self._create_perps()
        self._create_history()

    def tearDown(self):
        self.temp.cleanup()

    def _create_poly(self):
        db = sqlite3.connect(self.poly)
        db.executescript("""
            CREATE TABLE account_state (
                singleton_id INTEGER PRIMARY KEY, cash REAL, starting_cash REAL,
                equity_peak REAL, day_key TEXT, day_start_equity REAL
            );
            CREATE TABLE trades (
                trade_id INTEGER PRIMARY KEY, token_id TEXT, opened_at TEXT,
                shares REAL, notional REAL, entry_fee REAL, last_mark_price REAL,
                status TEXT, closed_at TEXT, pnl REAL
            );
            CREATE TABLE observations (
                observation_id INTEGER PRIMARY KEY, token_id TEXT, observed_at TEXT
            );
            INSERT INTO account_state VALUES (1,90,100,105,'2026-11-28',100);
            INSERT INTO trades VALUES (1,'token-1','2026-11-28T11:00:00+00:00',
                10,10,0.1,1.2,'OPEN',NULL,NULL);
            INSERT INTO observations VALUES (1,'token-1','2026-11-28T11:59:30+00:00');
        """)
        db.commit()
        db.close()

    def _create_perps(self):
        db = sqlite3.connect(self.perps)
        db.executescript("""
            CREATE TABLE perps_paper_account (
                account_id INTEGER PRIMARY KEY, cash TEXT, peak_equity TEXT,
                utc_day TEXT, day_start_equity TEXT, halted INTEGER
            );
            CREATE TABLE perps_paper_positions (
                position_id INTEGER PRIMARY KEY, instrument_id INTEGER, symbol TEXT,
                side TEXT, quantity TEXT, entry_price TEXT, margin_balance TEXT,
                entry_fee TEXT, funding_paid TEXT, last_mark TEXT, status TEXT,
                closed_at TEXT, net_pnl TEXT
            );
            INSERT INTO perps_paper_account VALUES (1,'90','105','2026-11-28','100',0);
            INSERT INTO perps_paper_positions VALUES (1,7,'BTC','LONG','0.1','100','10',
                '0.01','0.02','110','OPEN',NULL,'0');
        """)
        db.commit()
        db.close()

    def _create_history(self):
        db = sqlite3.connect(self.history)
        db.executescript("""
            CREATE TABLE perps_market_snapshots (
                snapshot_id INTEGER PRIMARY KEY, instrument_id INTEGER,
                ticker_timestamp_ms INTEGER, mark_price TEXT
            );
        """)
        stamp = int(datetime(2026, 11, 28, 11, 59, 40, tzinfo=timezone.utc).timestamp() * 1000)
        db.execute("INSERT INTO perps_market_snapshots VALUES (1,7,?,'110')", (stamp,))
        db.commit()
        db.close()

    def snapshot(self, **kwargs):
        return build_unified_paper_risk_snapshot(
            polymarket_db_path=self.poly,
            perps_db_path=self.perps,
            perps_history_db_path=self.history,
            now=NOW,
            **kwargs,
        )

    def test_combines_two_paper_ledgers_and_marks(self):
        result = self.snapshot()
        self.assertEqual(result.combined_open_positions, 2)
        self.assertEqual(result.polymarket_equity, "102")
        self.assertEqual(result.perps_equity, "101")
        self.assertEqual(result.combined_equity, "203")
        self.assertEqual(result.combined_daily_pnl, "3")
        self.assertEqual(result.polymarket_unrealized_pnl, "1.9")
        self.assertEqual(result.perps_unrealized_pnl, "1")
        self.assertEqual(result.combined_open_exposure, "23")
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.marks_fresh)
        self.assertEqual(result.execution, "none")

    def test_missing_ledger_fails_closed(self):
        self.perps.unlink()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "missing"):
            self.snapshot()

    def test_stale_polymarket_mark_fails_closed(self):
        db = sqlite3.connect(self.poly)
        db.execute("UPDATE observations SET observed_at='2026-11-28T11:00:00+00:00'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "stale"):
            self.snapshot()

    def test_stale_perps_mark_fails_closed(self):
        db = sqlite3.connect(self.history)
        db.execute("UPDATE perps_market_snapshots SET ticker_timestamp_ms=?",
                   (int(datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc).timestamp() * 1000),))
        db.commit()
        db.close()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "stale"):
            self.snapshot()

    def test_perps_mark_mismatch_fails_closed(self):
        db = sqlite3.connect(self.history)
        db.execute("UPDATE perps_market_snapshots SET mark_price='109'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "does not match"):
            self.snapshot()

    def test_cross_venue_timestamp_skew_fails_closed(self):
        db = sqlite3.connect(self.history)
        stamp = int(datetime(2026, 11, 28, 11, 57, 0, tzinfo=timezone.utc).timestamp() * 1000)
        db.execute("UPDATE perps_market_snapshots SET ticker_timestamp_ms=?", (stamp,))
        db.commit()
        db.close()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "skew"):
            self.snapshot()

    def test_noncurrent_utc_baseline_fails_closed(self):
        db = sqlite3.connect(self.perps)
        db.execute("UPDATE perps_paper_account SET utc_day='2026-11-27'")
        db.commit()
        db.close()
        with self.assertRaisesRegex(UnifiedPaperRiskError, "baseline"):
            self.snapshot()

    def test_read_only_does_not_mutate_databases(self):
        before = sqlite3.connect(self.poly).execute("PRAGMA user_version").fetchone()[0]
        self.snapshot()
        after = sqlite3.connect(self.poly).execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(before, after)

    def test_rejects_database_path_aliasing(self):
        with self.assertRaisesRegex(UnifiedPaperRiskError, "distinct"):
            build_unified_paper_risk_snapshot(
                polymarket_db_path=self.poly,
                perps_db_path=self.poly,
                perps_history_db_path=self.history,
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
