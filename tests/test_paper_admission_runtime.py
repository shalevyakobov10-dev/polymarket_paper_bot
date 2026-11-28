from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paper_admission_runtime import (
    PaperAdmissionRuntime,
    PaperAdmissionRuntimeError,
    build_admission_id,
)
from unified_paper_risk import UnifiedPaperRiskSnapshot


class PaperAdmissionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.poly = root / "poly.sqlite3"
        self.perps = root / "perps.sqlite3"
        self.history = root / "history.sqlite3"
        self.coordinator = root / "admission.sqlite3"
        for path in (self.poly, self.perps, self.history):
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
        self.now = datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)
        self.snapshot = UnifiedPaperRiskSnapshot(
            captured_at_utc=self.now.isoformat(),
            utc_day=self.now.date().isoformat(),
            polymarket_equity="100",
            perps_equity="1000",
            combined_equity="1100",
            combined_starting_capital=None,
            polymarket_peak_equity="100",
            perps_peak_equity="1000",
            combined_peak_equity=None,
            combined_drawdown_pct=None,
            polymarket_daily_pnl="-4",
            perps_daily_pnl="-20",
            combined_daily_pnl="-24",
            polymarket_realized_today="-2",
            perps_realized_today="-10",
            polymarket_unrealized_pnl="-2",
            perps_unrealized_pnl="-10",
            polymarket_open_exposure="10",
            perps_open_notional="30",
            perps_margin_used="15",
            combined_open_exposure="40",
            combined_open_positions=2,
            freshest_mark_age_seconds="2",
            stalest_mark_age_seconds="5",
            cross_venue_mark_skew_seconds="3",
            perps_halted=False,
            marks_fresh=True,
            snapshot_complete=True,
        )

    def runtime(self):
        return PaperAdmissionRuntime(
            polymarket_db_path=self.poly,
            perps_db_path=self.perps,
            perps_history_db_path=self.history,
            coordinator_db_path=self.coordinator,
            max_snapshot_age_seconds=120,
            max_cross_venue_skew_seconds=120,
            max_total_exposure_usd="95",
            max_total_positions=8,
            max_combined_daily_loss_usd="35",
        )

    def test_admission_id_is_deterministic_and_separated_by_venue_and_strategy(self):
        base = build_admission_id(
            venue="POLYMARKET",
            source_identity="market-1|snapshot-1",
            strategy_version="event-v1",
        )
        self.assertEqual(base, build_admission_id(
            venue="POLYMARKET",
            source_identity="market-1|snapshot-1",
            strategy_version="event-v1",
        ))
        self.assertNotEqual(base, build_admission_id(
            venue="PERPS",
            source_identity="market-1|snapshot-1",
            strategy_version="event-v1",
        ))
        self.assertNotEqual(base, build_admission_id(
            venue="POLYMARKET",
            source_identity="market-1|snapshot-1",
            strategy_version="event-v2",
        ))

    def test_reserve_uses_complete_fresh_combined_snapshot_and_shared_daily_loss(self):
        runtime = self.runtime()
        with patch(
            "paper_admission_runtime.build_unified_paper_risk_snapshot",
            return_value=self.snapshot,
        ) as mocked:
            ticket = runtime.reserve(
                venue="POLYMARKET",
                source_identity="market-1|2026-11-28T18:00:00+00:00",
                strategy_version="event-v1",
                exposure_usd="10.50",
                now=self.now,
            )
        self.assertTrue(ticket.result.approved)
        self.assertEqual(ticket.venue, "POLYMARKET")
        self.assertEqual(ticket.result.status, "PENDING")
        args = mocked.call_args.kwargs
        self.assertEqual(args["polymarket_db_path"], self.poly)
        self.assertEqual(args["perps_db_path"], self.perps)
        self.assertEqual(args["perps_history_db_path"], self.history)
        self.assertEqual(ticket.result.reserved_positions, 3)

    def test_incomplete_or_stale_combined_snapshot_never_reserves(self):
        runtime = self.runtime()
        invalid = self.snapshot.__class__(
            **{**self.snapshot.as_dict(), "snapshot_complete": False}
        )
        with patch(
            "paper_admission_runtime.build_unified_paper_risk_snapshot",
            return_value=invalid,
        ):
            with self.assertRaisesRegex(
                PaperAdmissionRuntimeError, "not_complete_and_fresh"
            ):
                runtime.reserve(
                    venue="PERPS",
                    source_identity="instrument-5|snapshot-1",
                    strategy_version="perps-v1",
                    exposure_usd="10",
                    now=self.now,
                )
        self.assertEqual(runtime.coordinator.list_unresolved(), [])

    def test_combined_daily_loss_limit_blocks_both_venues(self):
        runtime = self.runtime()
        losing = self.snapshot.__class__(
            **{**self.snapshot.as_dict(), "combined_daily_pnl": "-35"}
        )
        with patch(
            "paper_admission_runtime.build_unified_paper_risk_snapshot",
            return_value=losing,
        ):
            result = runtime.reserve(
                venue="PERPS",
                source_identity="instrument-5|snapshot-2",
                strategy_version="perps-v1",
                exposure_usd="10",
                now=self.now,
            )
        self.assertFalse(result.result.approved)
        self.assertEqual(result.result.reason, "combined_daily_loss_limit")
        self.assertEqual(runtime.coordinator.list_unresolved(), [])

    def test_recovery_reconciles_exact_ledger_entries_and_keeps_absent_reservations(self):
        runtime = self.runtime()
        ticket = runtime.coordinator.reserve(
            admission_id="polymarket:" + "a" * 64,
            venue="POLYMARKET",
            exposure_usd="10",
            positions=1,
            snapshot_exposure_usd="0",
            snapshot_positions=0,
            snapshot_daily_pnl_usd="0",
            max_combined_daily_loss_usd="35",
            snapshot_at=self.now,
            max_snapshot_age_seconds=120,
            max_total_exposure_usd="95",
            max_total_positions=8,
            now=self.now,
        )
        connection = sqlite3.connect(self.poly)
        connection.execute(
            "CREATE TABLE trades (trade_id INTEGER PRIMARY KEY, notional TEXT, status TEXT, admission_id TEXT UNIQUE)"
        )
        connection.execute(
            "INSERT INTO trades(trade_id,notional,status,admission_id) VALUES(1,'10','OPEN',?)",
            (ticket.admission_id,),
        )
        connection.commit()
        connection.close()

        absent = runtime.coordinator.reserve(
            admission_id="perps:" + "b" * 64,
            venue="PERPS",
            exposure_usd="10",
            positions=1,
            snapshot_exposure_usd="0",
            snapshot_positions=0,
            snapshot_daily_pnl_usd="0",
            max_combined_daily_loss_usd="35",
            snapshot_at=self.now,
            max_snapshot_age_seconds=120,
            max_total_exposure_usd="95",
            max_total_positions=8,
            now=self.now,
        )
        report = runtime.recover_unresolved(now=self.now)
        self.assertEqual(report.reconciled, (ticket.admission_id,))
        self.assertEqual(report.still_pending, (absent.admission_id,))
        self.assertEqual(runtime.coordinator.list_unresolved()[0]["admission_id"], absent.admission_id)

    def test_env_configuration_requires_distinct_database_paths_and_positive_limits(self):
        with patch.dict(os.environ, {
            "PAPER_COMBINED_DAILY_LOSS_LIMIT_USD": "0",
        }):
            with self.assertRaisesRegex(
                PaperAdmissionRuntimeError, "PAPER_COMBINED_DAILY_LOSS_LIMIT_USD"
            ):
                PaperAdmissionRuntime.from_env(
                    polymarket_db_path=self.poly,
                    perps_db_path=self.perps,
                    perps_history_db_path=self.history,
                )


if __name__ == "__main__":
    unittest.main()
