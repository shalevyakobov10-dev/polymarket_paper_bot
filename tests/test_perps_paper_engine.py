import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from perps_paper_engine import PerpsPaperEngine


NOW_MS = 1795334400000
FUNDING_MS = NOW_MS + 3_600_000


def iso_ms(value):
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def make_snapshot(
    mark="100.40",
    *,
    sequence=3,
    timestamp_ms=NOW_MS + 60_000,
    bid=None,
    ask=None,
    bid_quantity="10",
    ask_quantity="10",
    close_only=False,
    next_funding_ms=FUNDING_MS,
    funding_rate="0.0001",
    funding_interval="1h",
    fees=None,
    liquidation_fee="0.01",
):
    mark_value = Decimal(mark)
    bid_value = Decimal(bid) if bid is not None else mark_value - Decimal("0.01")
    ask_value = Decimal(ask) if ask is not None else mark_value + Decimal("0.01")
    return {
        "instrument": {
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "category": "crypto",
            "close_only": close_only,
            "isolated_only": True,
            "min_notional": "10",
            "max_market_notional": "100000",
            "max_limit_notional": "100000",
            "max_leverage": "20",
            "quantity_decimals": 4,
            "price_decimals": 2,
            "price_bounds": "0.1",
            "liquidation_fee": liquidation_fee,
            "funding_interval": funding_interval,
            "risk_tiers": [
                {"lower_bound": "0", "max_leverage": "20"},
                {"lower_bound": "100000", "max_leverage": "10"},
            ],
        },
        "ticker": {
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "index_price": str(mark_value),
            "mark_price": str(mark_value),
            "last_price": str(mark_value),
            "mid_price": str(mark_value),
            "open_interest": "100",
            "funding_rate": str(funding_rate),
            "next_funding_ms": next_funding_ms,
            "timestamp_ms": timestamp_ms,
        },
        "book": {
            "instrument_id": 7,
            "timestamp_ms": timestamp_ms,
            "sequence": sequence,
            "best_bid": str(bid_value),
            "best_ask": str(ask_value),
            "bids": [{"price": str(bid_value), "quantity": bid_quantity}],
            "asks": [{"price": str(ask_value), "quantity": ask_quantity}],
        },
        "received_at": iso_ms(timestamp_ms),
        "fees": fees if fees is not None else {
            "taker_fee_rate": "0.0004",
            "maker_fee_rate": "0.0001",
            "tier_assumption": "highest_published_rate_no_volume_discount",
            "source": "public_perps_fee_schedule",
            "retrieved_at": iso_ms(timestamp_ms),
        },
        "research_only": True,
        "execution": "none",
    }


class FakeHistory:
    def __init__(self, marks=None, *, first_ms=NOW_MS, settled_rate="0.00025"):
        values = marks or ["100.00", "100.20", "100.40"]
        self.settled_rate = settled_rate
        self.rows = [
            {
                "book_timestamp_ms": first_ms + index * 30_000,
                "ticker": {"mark_price": value},
            }
            for index, value in enumerate(values)
        ]

    def history(self, instrument_id, limit=100):
        return self.rows[-limit:]

    def funding_rate_at(self, instrument_id, timestamp_ms, *, tolerance_ms=1000):
        if self.settled_rate is None:
            return None
        return {
            "instrument_id": instrument_id,
            "timestamp_ms": timestamp_ms,
            "funding_rate": self.settled_rate,
        }


def process_snapshot_at_capture(engine, snapshot, history, *, now=None):
    if now is None:
        timestamp_ms = int(snapshot["book"]["timestamp_ms"])
        now = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return engine.process_snapshot(snapshot, history, now=now)


class PerpsPaperEngineTests(unittest.TestCase):
    def make_engine(self, directory, **kwargs):
        return PerpsPaperEngine(
            Path(directory) / "perps-paper.sqlite3",
            lookback_observations=3,
            minimum_history_span_seconds=60,
            max_data_age_seconds=15,
            **kwargs,
        )

    def test_positions_table_has_unique_admission_id_column_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                columns = {
                    row[1] for row in engine.db.execute(
                        "PRAGMA table_info(perps_paper_positions)"
                    )
                }
                self.assertIn("admission_id", columns)
                indexes = {
                    row[1] for row in engine.db.execute(
                        "PRAGMA index_list(perps_paper_positions)"
                    )
                }
                self.assertIn("idx_perps_paper_admission_id", indexes)

    def test_opens_isolated_long_after_sufficient_positive_momentum(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                self.assertEqual(engine.open_instrument_ids(), [7])
                self.assertEqual(result["worker_state"], "healthy")
                self.assertLess(Decimal(result["cash"]), Decimal("1000"))
                self.assertGreater(Decimal(result["isolated_margin_reserved"]), 0)
                self.assertGreater(Decimal(result["open_notional"]), 0)
                self.assertGreater(Decimal(result["effective_leverage"]), 0)
                self.assertGreaterEqual(Decimal(result["peak_drawdown_pct"]), 0)
                self.assertTrue(Decimal(result["daily_pnl_pct"]).is_finite())
                self.assertEqual(result["research_only"], True)
                self.assertFalse(result["live_execution_enabled"])
                self.assertFalse(result["order_submission_allowed"])
                self.assertEqual(result["execution"], "none")
                row = engine.db.execute(
                    "SELECT * FROM perps_paper_positions WHERE status='OPEN'"
                ).fetchone()
                self.assertEqual(row["side"], "LONG")
                self.assertEqual(Decimal(row["entry_price"]), Decimal("100.41"))
                self.assertEqual(Decimal(row["stop_price"]), Decimal(row["entry_price"]) * Decimal("0.9925"))
                self.assertEqual(Decimal(row["take_profit_price"]), Decimal(row["entry_price"]) * Decimal("1.015"))
                self.assertLessEqual(
                    Decimal(row["quantity"]) * Decimal(row["entry_price"]),
                    Decimal("25"),
                )

    def test_daily_equity_drawdown_halts_perps_paper_account(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory, daily_drawdown_pct="0.000001") as engine:
                result = process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "daily_drawdown_limit")
                self.assertGreater(Decimal(result["peak_drawdown_pct"]), 0)
                self.assertLess(Decimal(result["daily_pnl_pct"]), 0)

    def test_snapshot_cannot_open_new_risk_when_open_position_market_data_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                snapshot = make_snapshot()
                timestamp_ms = int(snapshot["book"]["timestamp_ms"])
                captured = datetime.fromtimestamp(
                    timestamp_ms / 1000, tz=timezone.utc
                )
                result = engine.process_snapshot(
                    snapshot,
                    FakeHistory(),
                    now=captured,
                    allow_new_entries=False,
                )
                self.assertEqual(result["open_positions"], 0)
                self.assertEqual(engine.open_instrument_ids(), [])
                event = engine.db.execute(
                    "SELECT event_type, details_json FROM perps_paper_events "
                    "ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(event["event_type"], "ENTRY_BLOCKED")
                self.assertIn(
                    "open_position_market_data_incomplete",
                    event["details_json"],
                )

    def test_opens_short_on_negative_momentum(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = process_snapshot_at_capture(engine, 
                    make_snapshot(mark="100.00", bid="99.99", ask="100.01"),
                    FakeHistory(["100.40", "100.20", "100.00"]),
                )
                self.assertEqual(result["open_positions"], 1)
                row = engine.db.execute(
                    "SELECT side FROM perps_paper_positions WHERE status='OPEN'"
                ).fetchone()
                self.assertEqual(row["side"], "SHORT")

    def test_requires_history_and_does_not_trade_on_insufficient_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = process_snapshot_at_capture(engine, 
                    make_snapshot(), FakeHistory(["100.00", "100.40"])
                )
                self.assertEqual(result["open_positions"], 0)
                self.assertEqual(engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_events WHERE event_type='NO_SIGNAL'"
                ).fetchone()[0], 1)

    def test_spread_guard_blocks_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = process_snapshot_at_capture(engine, 
                    make_snapshot(mark="100", bid="99.50", ask="100.50"),
                    FakeHistory(),
                )
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("spread_too_wide", event["details_json"])

    def test_close_only_blocks_new_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = process_snapshot_at_capture(engine, 
                    make_snapshot(close_only=True), FakeHistory()
                )
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("instrument_close_only", event["details_json"])

    def test_snapshot_idempotency_prevents_duplicate_position_or_event(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                snapshot = make_snapshot()
                first = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                event_count = engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_events"
                ).fetchone()[0]
                second = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(first["open_positions"], 1)
                self.assertEqual(second["open_positions"], 1)
                self.assertEqual(
                    engine.db.execute("SELECT COUNT(*) FROM perps_paper_positions").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    engine.db.execute("SELECT COUNT(*) FROM perps_paper_events").fetchone()[0],
                    event_count,
                )

    def test_take_profit_closes_and_same_snapshot_does_not_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                closed_snapshot = make_snapshot(
                    mark="102.00",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="101.99",
                    ask="102.01",
                )
                result = process_snapshot_at_capture(engine, closed_snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                row = engine.db.execute(
                    "SELECT status,close_reason,net_pnl FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "CLOSED")
                self.assertEqual(row["close_reason"], "take_profit")
                self.assertGreater(Decimal(row["net_pnl"]), 0)
                performance = engine.summary()["performance"]
                self.assertEqual(performance["closed_trades"], 1)
                self.assertEqual(performance["winning_trades"], 1)
                self.assertEqual(performance["losing_trades"], 0)
                self.assertEqual(performance["breakeven_trades"], 0)
                self.assertEqual(performance["win_rate_pct"], "100.00")
                self.assertEqual(performance["profit_factor"], None)
                self.assertEqual(
                    performance["profit_factor_status"],
                    "undefined_without_closed_losses",
                )
                self.assertGreater(Decimal(performance["gross_closed_pnl"]), 0)
                self.assertGreater(
                    Decimal(performance["net_closed_pnl_after_fees_and_funding"]), 0
                )
                self.assertGreater(Decimal(performance["entry_fees_paid"]), 0)
                self.assertGreater(Decimal(performance["exit_fees_paid"]), 0)
                self.assertEqual(performance["funding_paid"], "0")
                self.assertFalse(performance["out_of_sample_validated"])
                self.assertFalse(performance["live_execution_eligible"])

    def test_stop_loss_closes_position_when_visible_bid_depth_is_sufficient(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="99.50",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="99.49",
                    ask="99.51",
                )
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                row = engine.db.execute(
                    "SELECT status,close_reason,net_pnl FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "CLOSED")
                self.assertEqual(row["close_reason"], "stop_loss")
                self.assertLess(Decimal(row["net_pnl"]), 0)

    def test_insufficient_exit_depth_keeps_position_open_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="99.50",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="99.49",
                    ask="99.51",
                    bid_quantity="0.0001",
                )
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                row = engine.db.execute(
                    "SELECT status FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "OPEN")
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='CLOSE_BLOCKED'"
                ).fetchone()
                self.assertIn("insufficient visible", event["details_json"])
                position = engine.db.execute(
                    "SELECT pending_exit_reason,pending_exit_liquidated "
                    "FROM perps_paper_positions WHERE status='OPEN'"
                ).fetchone()
                self.assertEqual(position["pending_exit_reason"], "stop_loss")
                self.assertFalse(position["pending_exit_liquidated"])

    def test_pending_stop_exit_is_retried_after_mark_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                blocked = make_snapshot(
                    mark="99.50", sequence=4, timestamp_ms=NOW_MS + 90_000,
                    bid="99.49", ask="99.51", bid_quantity="0.0001",
                )
                process_snapshot_at_capture(engine, blocked, FakeHistory())
                recovering = make_snapshot(
                    mark="100.20", sequence=5, timestamp_ms=NOW_MS + 120_000,
                    bid="100.19", ask="100.21",
                )
                result = process_snapshot_at_capture(engine, recovering, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                position = engine.db.execute(
                    "SELECT status,close_reason,pending_exit_reason "
                    "FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(position["status"], "CLOSED")
                self.assertEqual(position["close_reason"], "stop_loss")
                self.assertIsNone(position["pending_exit_reason"])

    def test_funding_uses_exact_published_settlement_not_previous_ticker_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, 
                    make_snapshot(funding_rate="0.001"),
                    FakeHistory(),
                )
                before = engine.db.execute(
                    "SELECT margin_balance,quantity FROM perps_paper_positions"
                ).fetchone()
                snapshot = make_snapshot(
                    mark="100.50",
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 1_000,
                    bid="100.49",
                    ask="100.51",
                    funding_rate="0.0002",
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                history = FakeHistory(settled_rate="0.00025")
                process_snapshot_at_capture(engine, snapshot, history)
                after = engine.db.execute(
                    "SELECT margin_balance,funding_paid FROM perps_paper_positions"
                ).fetchone()
                expected = Decimal(before["quantity"]) * Decimal("100.50") * Decimal("0.00025")
                self.assertAlmostEqual(
                    float(Decimal(before["margin_balance"]) - Decimal(after["margin_balance"])),
                    float(expected),
                    places=8,
                )
                self.assertEqual(Decimal(after["funding_paid"]), expected)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='FUNDING_SETTLED'"
                ).fetchone()
                self.assertIn("0.00025", event["details_json"])
                self.assertEqual(
                    engine.db.execute(
                        "SELECT COUNT(*) FROM perps_paper_events WHERE event_type='FUNDING_SETTLED'"
                    ).fetchone()[0],
                    1,
                )

    def test_funding_schedule_grace_matches_thirty_second_worker_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                grace_snapshot = make_snapshot(
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 30_000,
                    next_funding_ms=FUNDING_MS,
                )
                within_grace = process_snapshot_at_capture(
                    engine, grace_snapshot, FakeHistory()
                )
                self.assertFalse(within_grace["halted"])
                self.assertEqual(within_grace["open_positions"], 1)

                settled_snapshot = make_snapshot(
                    sequence=5,
                    timestamp_ms=FUNDING_MS + 40_000,
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                settled = process_snapshot_at_capture(
                    engine, settled_snapshot, FakeHistory(settled_rate="0.00025")
                )
                self.assertFalse(settled["halted"])
                self.assertEqual(
                    engine.db.execute(
                        "SELECT COUNT(*) FROM perps_paper_events WHERE event_type='FUNDING_SETTLED'"
                    ).fetchone()[0],
                    1,
                )

    def test_funding_schedule_update_after_sixty_one_seconds_fits_default_mark_window(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                delayed_update = make_snapshot(
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 61_000,
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                result = process_snapshot_at_capture(
                    engine,
                    delayed_update,
                    FakeHistory(settled_rate="0.00025"),
                )
                self.assertFalse(result["halted"])
                self.assertEqual(result["open_positions"], 1)
                self.assertEqual(
                    engine.db.execute(
                        "SELECT COUNT(*) FROM perps_paper_events WHERE event_type='FUNDING_SETTLED'"
                    ).fetchone()[0],
                    1,
                )

    def test_funding_schedule_that_never_advances_halts_after_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                late = make_snapshot(
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 61_000,
                    next_funding_ms=FUNDING_MS,
                )
                result = process_snapshot_at_capture(engine, late, FakeHistory())
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "funding_schedule_not_advanced")

    def test_funding_mark_observation_beyond_configured_window_halts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(
                directory, max_post_funding_mark_delay_seconds=45
            ) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                too_late = make_snapshot(
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 46_000,
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                result = process_snapshot_at_capture(engine, too_late, FakeHistory())
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "funding_mark_observation_missing")

    def test_funding_gap_halts_new_risk_instead_of_guessing_missing_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="100.50",
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 3_600_000,
                    bid="100.49",
                    ask="100.51",
                    next_funding_ms=FUNDING_MS + 7_200_000,
                )
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "funding_history_gap")
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='FUNDING_RECONCILIATION_REQUIRED'"
                ).fetchone()
                self.assertIn("no estimate applied", event["details_json"])

    def test_missing_exact_funding_settlement_halts_without_guessing_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="100.50",
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 1_000,
                    bid="100.49",
                    ask="100.51",
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory(settled_rate=None))
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "published_funding_settlement_missing")
                position = engine.db.execute(
                    "SELECT funding_paid FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(Decimal(position["funding_paid"]), Decimal("0"))

    def test_fee_schedule_is_required_for_new_entries_and_stored_on_position(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                missing_fee = make_snapshot(fees={})
                result = process_snapshot_at_capture(engine, missing_fee, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("fee_schedule_missing_or_stale", event["details_json"])

        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                row = engine.db.execute(
                    "SELECT taker_fee_rate,liquidation_fee_rate FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(Decimal(row["taker_fee_rate"]), Decimal("0.0004"))
                self.assertEqual(Decimal(row["liquidation_fee_rate"]), Decimal("0.01"))

    def test_exit_uses_more_conservative_current_published_fee(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                closing = make_snapshot(
                    mark="102.00", sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="101.99", ask="102.01",
                    fees={
                        "taker_fee_rate": "0.0007",
                        "maker_fee_rate": "0.0001",
                        "source": "public_perps_fee_schedule",
                        "retrieved_at": iso_ms(NOW_MS + 90_000),
                    },
                )
                process_snapshot_at_capture(engine, closing, FakeHistory())
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='CLOSED'"
                ).fetchone()
                self.assertIn('"exit_taker_fee_rate":"0.0007"', event["details_json"])

    def test_maintenance_margin_uses_instrument_max_leverage_not_risk_tier_leverage(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                snapshot = make_snapshot()
                snapshot["instrument"]["risk_tiers"] = [
                    {"lower_bound": "0", "max_leverage": "10"},
                    {"lower_bound": "100000", "max_leverage": "5"},
                ]
                process_snapshot_at_capture(engine, snapshot, FakeHistory())
                row = engine.db.execute(
                    "SELECT maintenance_leverage,margin_balance FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(Decimal(row["maintenance_leverage"]), Decimal("20"))
                # Between the correct 2.5% maintenance requirement and the
                # incorrect 5% tier-based requirement: should stay open.
                engine.db.execute(
                    "UPDATE perps_paper_positions SET margin_balance='1.0'"
                )
                next_snapshot = make_snapshot(
                    sequence=4, timestamp_ms=NOW_MS + 90_000,
                    mark="100.40", bid="100.39", ask="100.41",
                )
                result = process_snapshot_at_capture(engine, next_snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                self.assertFalse(result["halted"])

    def test_funding_requirements_report_exact_open_position_settlement_window(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                result = engine.funding_requirements([make_snapshot(
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 1_000,
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )])
                self.assertEqual(result, [{
                    "instrument_id": 7,
                    "start_timestamp_ms": FUNDING_MS,
                    "end_timestamp_ms": FUNDING_MS + 3_600_000,
                }])

    def test_mark_price_liquidation_closes_with_separate_liquidation_fee(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="50.00",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="49.99",
                    ask="50.01",
                )
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                row = engine.db.execute(
                    "SELECT status,close_reason,net_pnl FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "LIQUIDATED")
                self.assertEqual(row["close_reason"], "maintenance_margin_breached")
                self.assertLess(Decimal(row["net_pnl"]), 0)

    def test_entry_impact_guard_blocks_depth_sweep_beyond_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory, max_entry_impact_bps="5") as engine:
                snapshot = make_snapshot()
                snapshot["book"]["asks"] = [
                    {"price": "100.41", "quantity": "0.01"},
                    {"price": "100.51", "quantity": "5"},
                ]
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("entry_impact_too_high", event["details_json"])

    def test_post_fill_notional_cannot_exceed_position_or_risk_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory, max_entry_impact_bps="1000") as engine:
                snapshot = make_snapshot()
                snapshot["book"]["asks"] = [
                    {"price": "100.41", "quantity": "0.01"},
                    {"price": "110.00", "quantity": "5"},
                ]
                result = process_snapshot_at_capture(engine, snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("instrument_or_position_notional_constraint", event["details_json"])

    def test_isolated_liquidation_cannot_spend_free_collateral(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                process_snapshot_at_capture(engine, make_snapshot(), FakeHistory())
                cash_after_entry = Decimal(engine.summary()["cash"])
                liquidation_snapshot = make_snapshot(
                    mark="1.00", sequence=4, timestamp_ms=NOW_MS + 90_000,
                    bid="0.99", ask="1.01",
                )
                result = process_snapshot_at_capture(engine, liquidation_snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "isolated_margin_exhausted")
                self.assertEqual(Decimal(result["cash"]), cash_after_entry)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='LIQUIDATED'"
                ).fetchone()
                details = __import__("json").loads(event["details_json"])
                self.assertFalse(details["free_collateral_used"])
                self.assertGreater(Decimal(details["isolated_margin_shortfall"]), 0)

    def test_old_archived_snapshot_cannot_certify_itself_as_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                stale_ms = int((datetime.now(timezone.utc) - timedelta(seconds=120)).timestamp() * 1000)
                snapshot = make_snapshot(timestamp_ms=stale_ms)
                with self.assertRaisesRegex(ValueError, "stale"):
                    engine.process_snapshot(snapshot, FakeHistory(), now=datetime.now(timezone.utc))

    def test_stale_exchange_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                snapshot = make_snapshot()
                snapshot["received_at"] = iso_ms(NOW_MS + 120_000)
                captured_at = datetime.fromtimestamp(
                    snapshot["book"]["timestamp_ms"] / 1000, tz=timezone.utc
                )
                with self.assertRaisesRegex(ValueError, "future-dated"):
                    engine.process_snapshot(snapshot, FakeHistory(), now=captured_at)

    def test_invalid_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "max_leverage"):
                PerpsPaperEngine(Path(directory) / "x.sqlite3", max_leverage="0")
            with self.assertRaisesRegex(ValueError, "lookback"):
                PerpsPaperEngine(
                    Path(directory) / "x.sqlite3",
                    lookback_observations=2,
                )


if __name__ == "__main__":
    unittest.main()
