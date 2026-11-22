from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from perps_paper_engine import PerpsPaperEngine, sweep_perps_book


BASE_MS = 1_800_000_000_000
BASE_TIME = datetime.fromtimestamp(BASE_MS / 1000, timezone.utc)


def market_snapshot(
    *,
    now=BASE_TIME,
    instrument_id=1,
    symbol="BTC-PERP",
    mark="100",
    bid="99.95",
    ask="100.05",
    bid_depth="10",
    ask_depth="10",
    next_funding_offset_ms=3_600_000,
    funding_rate="0.0001",
    close_only=False,
    entry_eligible=True,
    ticker_age=1,
    book_age=1,
    risk_tiers=None,
):
    timestamp_ms = int(now.timestamp() * 1000)
    return {
        "schema_version": 1,
        "source": "polymarket_perps_public_rest",
        "research_only": True,
        "execution": "none",
        "observed_at": now.isoformat(),
        "instrument_id": instrument_id,
        "symbol": symbol,
        "category": "crypto",
        "base_asset": "BTC",
        "quote_asset": "USD",
        "close_only": close_only,
        "settlement": False,
        "funding_interval": "1h",
        "quantity_decimals": 4,
        "price_decimals": 2,
        "min_notional": "1",
        "max_market_notional": "100000",
        "max_leverage": 10,
        "risk_tiers": risk_tiers or [{"lower_bound": "0", "max_leverage": 10}],
        "mark_price": mark,
        "index_price": mark,
        "last_price": mark,
        "mid_price": mark,
        "open_interest": "100",
        "funding_rate": funding_rate,
        "next_funding_ms": timestamp_ms + next_funding_offset_ms,
        "ticker_timestamp_ms": timestamp_ms,
        "book_timestamp_ms": timestamp_ms,
        "ticker_age_seconds": ticker_age,
        "book_age_seconds": book_age,
        "best_bid": bid,
        "best_ask": ask,
        "midpoint_from_book": str((float(bid) + float(ask)) / 2),
        "spread_bps": "10",
        "bids": [
            {"price": bid, "quantity": bid_depth},
            {"price": str(float(bid) - 0.1), "quantity": bid_depth},
        ],
        "asks": [
            {"price": ask, "quantity": ask_depth},
            {"price": str(float(ask) + 0.1), "quantity": ask_depth},
        ],
        "entry_eligible": entry_eligible,
    }


class PerpsBookFillTests(unittest.TestCase):
    def test_buy_fill_uses_visible_ask_depth_and_reports_consumed_levels(self):
        snap = market_snapshot(ask_depth="0.02")
        result = sweep_perps_book(snap, order_side="BUY", quantity="0.03")
        self.assertTrue(result["filled"])
        self.assertEqual(result["levels_consumed"], 2)
        self.assertEqual(result["execution"], "paper_simulation_only")

    def test_incomplete_depth_and_excessive_impact_are_rejected(self):
        thin = market_snapshot(ask_depth="0.01")
        insufficient = sweep_perps_book(thin, order_side="BUY", quantity="0.03")
        self.assertEqual(insufficient["reason"], "insufficient_visible_depth")

        wide_impact = market_snapshot(
            ask="100",
        )
        wide_impact["asks"] = [
            {"price": "100", "quantity": "0.01"},
            {"price": "105", "quantity": "1"},
        ]
        impact = sweep_perps_book(
            wide_impact, order_side="BUY", quantity="0.02", max_impact_bps="100",
        )
        self.assertEqual(impact["reason"], "paper_market_impact_above_limit")


class PerpsPaperEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "perps.sqlite3"
        self.engine = PerpsPaperEngine(self.db, starting_cash="100")
        self.snap = market_snapshot()

    def test_open_position_reserves_margin_and_fees_without_live_execution(self):
        result = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        self.assertEqual(result["status"], "PAPER_POSITION_OPEN")
        self.assertEqual(result["leverage"], "2")
        self.assertGreater(float(result["initial_margin"]), 0)
        self.assertGreater(float(result["entry_fee"]), 0)
        self.assertEqual(result["execution"], "paper_simulation_only")
        self.assertFalse(result["live_execution_eligible"])
        metrics = self.engine.account_metrics(now=BASE_TIME)
        self.assertEqual(metrics["open_positions"], 1)
        self.assertLess(float(metrics["cash_balance"]), 100)
        self.assertGreater(float(metrics["margin_reserved"]), 0)

    def test_second_position_for_same_instrument_is_blocked(self):
        first = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        self.assertEqual(first["status"], "PAPER_POSITION_OPEN")
        second = self.engine.open_position(
            self.snap, side="SHORT", requested_notional="5", now=BASE_TIME,
        )
        self.assertEqual(second["reason"], "position_already_open_for_instrument")

    def test_stale_snapshot_and_insufficient_visible_depth_block_entry(self):
        stale = market_snapshot(ticker_age=11)
        rejected = self.engine.open_position(
            stale, side="LONG", requested_notional="5", now=BASE_TIME,
        )
        self.assertEqual(rejected["status"], "PAPER_ENTRY_BLOCKED")

        thin = market_snapshot(ask_depth="0.01")
        rejected = self.engine.open_position(
            thin, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        self.assertEqual(rejected["reason"], "insufficient_visible_depth")

    def test_close_only_market_can_reduce_existing_position(self):
        opened = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        close_only = market_snapshot(
            now=BASE_TIME + timedelta(minutes=1),
            close_only=True,
            entry_eligible=False,
        )
        closed = self.engine.close_position(
            opened["trade_id"], close_only, now=BASE_TIME + timedelta(minutes=1),
        )
        self.assertEqual(closed["status"], "PAPER_POSITION_CLOSED")
        self.assertEqual(closed["close_reason"], "manual_paper_close")

    def test_close_records_realised_gross_and_net_pnl_after_both_fees(self):
        opened = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        close_time = BASE_TIME + timedelta(minutes=1)
        close_market = market_snapshot(
            now=close_time, mark="101", bid="100.95", ask="101.05",
        )
        closed = self.engine.close_position(
            opened["trade_id"], close_market, now=close_time,
        )
        self.assertEqual(closed["status"], "PAPER_POSITION_CLOSED")
        self.assertGreater(float(closed["gross_pnl"]), 0)
        self.assertGreater(float(closed["entry_fee"]), 0)
        self.assertGreater(float(closed["exit_fee"]), 0)
        self.assertLess(float(closed["net_pnl"]), float(closed["gross_pnl"]))
        trades = self.engine.list_trades()
        self.assertEqual(trades[0]["status"], "CLOSED")
        metrics = self.engine.account_metrics(now=close_time)
        self.assertEqual(metrics["open_positions"], 0)

    def test_stop_loss_is_managed_automatically_on_new_market_snapshot(self):
        opened = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        later = BASE_TIME + timedelta(minutes=1)
        downside = market_snapshot(
            now=later, mark="98.5", bid="98.45", ask="98.55",
            next_funding_offset_ms=3_540_000,
        )
        result = self.engine.process_snapshot(downside, now=later)
        self.assertEqual(result["status"], "PAPER_POSITION_CLOSED")
        self.assertEqual(result["close_reason"], "stop_loss")
        self.assertEqual(result["trade_id"], opened["trade_id"])

    def test_hourly_positive_funding_is_charged_to_long_once(self):
        opened = self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        later = BASE_TIME + timedelta(hours=1)
        funding_snapshot = market_snapshot(
            now=later,
            mark="100",
            next_funding_offset_ms=3_600_000,
            funding_rate="0.0002",
        )
        result = self.engine.process_snapshot(funding_snapshot, now=later)
        self.assertEqual(result["status"], "PAPER_MARK_RECORDED")
        trade = self.engine.list_trades()[0]
        self.assertGreater(float(trade["funding_paid"]), 0)
        daily = self.engine.account_metrics(now=later)
        self.assertGreater(float(daily["daily_loss"]), 0)
        self.assertEqual(trade["funding_status"], "tracking")
        self.assertEqual(trade["trade_id"], opened["trade_id"])

        # Re-processing the same funding timestamp must not charge it again.
        self.engine.process_snapshot(funding_snapshot, now=later + timedelta(seconds=1))
        repeated = self.engine.list_trades()[0]
        self.assertEqual(repeated["funding_paid"], trade["funding_paid"])

    def test_funding_gap_does_not_disable_a_protective_stop_exit(self):
        self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        later = BASE_TIME + timedelta(hours=2)
        gap_and_stop = market_snapshot(
            now=later,
            mark="98.5",
            bid="98.45",
            ask="98.55",
            next_funding_offset_ms=3_600_000,
            funding_rate="0.0002",
        )
        result = self.engine.process_snapshot(gap_and_stop, now=later)
        self.assertEqual(result["status"], "PAPER_POSITION_CLOSED")
        self.assertEqual(result["close_reason"], "stop_loss")
        self.assertEqual(result["funding_reconciliation"], "incomplete")
        self.assertEqual(result["warning"], "position_closed_despite_funding_gap")

    def test_missed_funding_interval_is_flagged_and_blocks_new_entries(self):
        self.engine.open_position(
            self.snap, side="LONG", requested_notional="9.5", now=BASE_TIME,
        )
        later = BASE_TIME + timedelta(hours=2)
        gap_snapshot = market_snapshot(
            now=later,
            next_funding_offset_ms=3_600_000,
            funding_rate="0.0002",
        )
        result = self.engine.process_snapshot(gap_snapshot, now=later)
        self.assertEqual(result["status"], "PAPER_POSITION_FUNDING_GAP")
        self.assertEqual(self.engine.list_trades()[0]["funding_status"], "gap")

        another = market_snapshot(
            instrument_id=2, symbol="ETH-PERP", mark="2000",
            bid="1999", ask="2001",
        )
        blocked = self.engine.open_position(
            another, side="LONG", requested_notional="5", now=later,
        )
        self.assertEqual(blocked["reason"], "existing_position_funding_gap")

    def test_volatility_adjusted_stop_and_reward_multiple_are_persisted(self):
        result = self.engine.open_position(
            self.snap,
            side="LONG",
            requested_notional="9.5",
            now=BASE_TIME,
            stop_loss_distance_pct="0.006",
            reward_risk_multiple="2.5",
        )
        self.assertEqual(result["status"], "PAPER_POSITION_OPEN")
        self.assertEqual(result["stop_loss_distance_pct"], "0.006")
        self.assertEqual(result["reward_risk_multiple"], "2.5")
        entry = Decimal(result["entry_price"])
        self.assertEqual(Decimal(result["stop_price"]), entry * Decimal("0.994"))
        self.assertEqual(Decimal(result["take_profit_price"]), entry * Decimal("1.015"))

    def test_strategy_bar_claim_is_idempotent_and_auditable(self):
        self.engine.process_snapshot(self.snap, now=BASE_TIME)
        history = self.engine.recent_snapshots(1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["symbol"], "BTC-PERP")
        bar_close_ms = BASE_MS + 60_000
        self.assertTrue(self.engine.claim_strategy_bar(1, bar_close_ms, now=BASE_TIME))
        self.assertFalse(self.engine.claim_strategy_bar(1, bar_close_ms, now=BASE_TIME))
        self.engine.record_strategy_decision(
            1,
            bar_close_ms,
            decision_status="no_signal",
            reason="no_confirmed_ema_crossover",
            metrics={"completed_bars": 60, "signal": False},
            now=BASE_TIME,
        )
        summary = self.engine.strategy_decision_summary()
        self.assertEqual(summary["decisions_total"], 1)
        self.assertEqual(summary["signals_opened"], 0)
        self.assertEqual(summary["latest"]["decision_status"], "no_signal")

    def test_snapshot_history_and_status_are_research_only(self):
        self.engine.process_snapshot(self.snap, now=BASE_TIME)
        status = self.engine.status(now=BASE_TIME)
        self.assertEqual(status["mode"], "perps_paper_only")
        self.assertTrue(status["research_only"])
        self.assertFalse(status["live_execution_enabled"])
        self.assertEqual(status["execution"], "none")
        self.assertEqual(status["market_snapshots"], 1)


if __name__ == "__main__":
    unittest.main()
