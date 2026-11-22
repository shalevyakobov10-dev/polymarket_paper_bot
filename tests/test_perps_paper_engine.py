import tempfile
import unittest
from datetime import datetime, timezone
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


class PerpsPaperEngineTests(unittest.TestCase):
    def make_engine(self, directory, **kwargs):
        return PerpsPaperEngine(
            Path(directory) / "perps-paper.sqlite3",
            lookback_observations=3,
            minimum_history_span_seconds=60,
            max_data_age_seconds=15,
            **kwargs,
        )

    def test_opens_isolated_long_after_sufficient_positive_momentum(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = engine.process_snapshot(make_snapshot(), FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                self.assertEqual(result["worker_state"], "healthy")
                self.assertLess(Decimal(result["cash"]), Decimal("1000"))
                self.assertGreater(Decimal(result["isolated_margin_reserved"]), 0)
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

    def test_opens_short_on_negative_momentum(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = engine.process_snapshot(
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
                result = engine.process_snapshot(
                    make_snapshot(), FakeHistory(["100.00", "100.40"])
                )
                self.assertEqual(result["open_positions"], 0)
                self.assertEqual(engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_events WHERE event_type='NO_SIGNAL'"
                ).fetchone()[0], 1)

    def test_spread_guard_blocks_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                result = engine.process_snapshot(
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
                result = engine.process_snapshot(
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
                first = engine.process_snapshot(snapshot, FakeHistory())
                event_count = engine.db.execute(
                    "SELECT COUNT(*) FROM perps_paper_events"
                ).fetchone()[0]
                second = engine.process_snapshot(snapshot, FakeHistory())
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
                engine.process_snapshot(make_snapshot(), FakeHistory())
                closed_snapshot = make_snapshot(
                    mark="102.00",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="101.99",
                    ask="102.01",
                )
                result = engine.process_snapshot(closed_snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                row = engine.db.execute(
                    "SELECT status,close_reason,net_pnl FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "CLOSED")
                self.assertEqual(row["close_reason"], "take_profit")
                self.assertGreater(Decimal(row["net_pnl"]), 0)

    def test_stop_loss_closes_position_when_visible_bid_depth_is_sufficient(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="99.50",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="99.49",
                    ask="99.51",
                )
                result = engine.process_snapshot(snapshot, FakeHistory())
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
                engine.process_snapshot(make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="99.50",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="99.49",
                    ask="99.51",
                    bid_quantity="0.0001",
                )
                result = engine.process_snapshot(snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                row = engine.db.execute(
                    "SELECT status FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "OPEN")
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='CLOSE_BLOCKED'"
                ).fetchone()
                self.assertIn("insufficient visible", event["details_json"])

    def test_funding_uses_exact_published_settlement_not_previous_ticker_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(
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
                engine.process_snapshot(snapshot, history)
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

    def test_funding_gap_halts_new_risk_instead_of_guessing_missing_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="100.50",
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 3_600_000,
                    bid="100.49",
                    ask="100.51",
                    next_funding_ms=FUNDING_MS + 7_200_000,
                )
                result = engine.process_snapshot(snapshot, FakeHistory())
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_reason"], "funding_history_gap")
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='FUNDING_RECONCILIATION_REQUIRED'"
                ).fetchone()
                self.assertIn("no estimate applied", event["details_json"])

    def test_missing_exact_funding_settlement_halts_without_guessing_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="100.50",
                    sequence=4,
                    timestamp_ms=FUNDING_MS + 1_000,
                    bid="100.49",
                    ask="100.51",
                    next_funding_ms=FUNDING_MS + 3_600_000,
                )
                result = engine.process_snapshot(snapshot, FakeHistory(settled_rate=None))
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
                result = engine.process_snapshot(missing_fee, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                event = engine.db.execute(
                    "SELECT details_json FROM perps_paper_events WHERE event_type='ENTRY_BLOCKED'"
                ).fetchone()
                self.assertIn("fee_schedule_missing_or_stale", event["details_json"])

        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
                row = engine.db.execute(
                    "SELECT taker_fee_rate,liquidation_fee_rate FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(Decimal(row["taker_fee_rate"]), Decimal("0.0004"))
                self.assertEqual(Decimal(row["liquidation_fee_rate"]), Decimal("0.01"))

    def test_exit_uses_more_conservative_current_published_fee(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
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
                engine.process_snapshot(closing, FakeHistory())
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
                engine.process_snapshot(snapshot, FakeHistory())
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
                result = engine.process_snapshot(next_snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 1)
                self.assertFalse(result["halted"])

    def test_funding_requirements_report_exact_open_position_settlement_window(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                engine.process_snapshot(make_snapshot(), FakeHistory())
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
                engine.process_snapshot(make_snapshot(), FakeHistory())
                snapshot = make_snapshot(
                    mark="50.00",
                    sequence=4,
                    timestamp_ms=NOW_MS + 90_000,
                    bid="49.99",
                    ask="50.01",
                )
                result = engine.process_snapshot(snapshot, FakeHistory())
                self.assertEqual(result["open_positions"], 0)
                row = engine.db.execute(
                    "SELECT status,close_reason,net_pnl FROM perps_paper_positions"
                ).fetchone()
                self.assertEqual(row["status"], "LIQUIDATED")
                self.assertEqual(row["close_reason"], "maintenance_margin_breached")
                self.assertLess(Decimal(row["net_pnl"]), 0)

    def test_stale_exchange_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_engine(directory) as engine:
                snapshot = make_snapshot()
                snapshot["received_at"] = iso_ms(NOW_MS + 120_000)
                with self.assertRaisesRegex(ValueError, "stale"):
                    engine.process_snapshot(snapshot, FakeHistory())

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
