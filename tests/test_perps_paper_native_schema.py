from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from perps_paper_engine import PerpsPaperEngine, flatten_perps_market_snapshot


BASE = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)


def nested_snapshot(
    *,
    now=BASE,
    mark="100",
    bid="99.95",
    ask="100.05",
    close_only=False,
    funding=True,
    instrument_id=7,
):
    stamp_ms = int(now.timestamp() * 1000)
    instrument = {
        "instrument_id": instrument_id,
        "instrument_type": "perpetual",
        "symbol": "BTC-PERP",
        "display_symbol": "BTC",
        "category": "crypto",
        "base_asset": "BTC",
        "quote_asset": "USD",
        "funding_interval": "1h" if funding else "",
        "close_only": close_only,
        "isolated_only": False,
        "min_notional": "10",
        "max_market_notional": "100000",
        "max_limit_notional": "1000000",
        "max_leverage": "20",
        "quantity_decimals": 4,
        "price_decimals": 2,
        "risk_tiers": [
            {"lower_bound": "0", "max_leverage": "20"},
            {"lower_bound": "100000", "max_leverage": "10"},
        ],
    }
    ticker = {
        "instrument_id": instrument_id,
        "symbol": "BTC-PERP",
        "index_price": mark,
        "mark_price": mark,
        "last_price": mark,
        "mid_price": mark,
        "open_interest": "100",
        "funding_rate": "0.0001",
        "next_funding_ms": stamp_ms + 3_600_000 if funding else None,
        "timestamp_ms": stamp_ms,
    }
    book = {
        "instrument_id": instrument_id,
        "timestamp_ms": stamp_ms,
        "sequence": 123,
        "best_bid": bid,
        "best_ask": ask,
        "bids": [
            {"price": bid, "quantity": "2"},
            {"price": str(float(bid) - 0.1), "quantity": "2"},
        ],
        "asks": [
            {"price": ask, "quantity": "2"},
            {"price": str(float(ask) + 0.1), "quantity": "2"},
        ],
    }
    return {
        "instrument": instrument,
        "ticker": ticker,
        "book": book,
        "received_at": now.isoformat(),
        "research_only": True,
        "execution": "none",
    }


class NativePerpsSnapshotTests(unittest.TestCase):
    def test_flattens_official_collector_schema_and_preserves_risk_tiers(self):
        result = flatten_perps_market_snapshot(nested_snapshot(), now=BASE)
        self.assertEqual(result["symbol"], "BTC-PERP")
        self.assertEqual(result["instrument_id"], 7)
        self.assertEqual(result["max_leverage"], "20")
        self.assertEqual(result["risk_tiers"][1]["max_leverage"], "10")
        self.assertTrue(result["funding_metadata_available"])
        self.assertEqual(result["ticker_age_seconds"], 0)
        self.assertGreater(float(result["spread_bps"]), 0)
        self.assertEqual(result["execution"], "none")

    def test_rejects_mismatched_instrument_ids_and_future_exchange_time(self):
        mismatch = nested_snapshot()
        mismatch["book"]["instrument_id"] = 8
        with self.assertRaisesRegex(ValueError, "instrument_mismatch"):
            flatten_perps_market_snapshot(mismatch, now=BASE)

        future = nested_snapshot(now=BASE + timedelta(seconds=5))
        with self.assertRaisesRegex(ValueError, "future_perps_exchange_timestamp"):
            flatten_perps_market_snapshot(future, now=BASE)

    def test_engine_opens_paper_position_using_native_collector_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(Path(directory) / "perps.sqlite3")
            result = engine.open_position(
                nested_snapshot(), side="LONG",
                requested_notional="10.5", now=BASE,
            )
            self.assertEqual(result["status"], "PAPER_POSITION_OPEN")
            self.assertGreaterEqual(float(result["notional"]), 10)
            self.assertEqual(result["execution"], "paper_simulation_only")
            self.assertFalse(result["live_execution_eligible"])
            self.assertEqual(engine.account_metrics(now=BASE)["open_positions"], 1)

    def test_entry_is_blocked_when_funding_metadata_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(Path(directory) / "perps.sqlite3")
            result = engine.open_position(
                nested_snapshot(funding=False), side="LONG",
                requested_notional="10.5", now=BASE,
            )
            self.assertEqual(result["status"], "PAPER_ENTRY_BLOCKED")
            self.assertEqual(result["reason"], "funding_metadata_unavailable")

    def test_missing_funding_data_never_blocks_a_protective_stop_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(Path(directory) / "perps.sqlite3")
            opened = engine.open_position(
                nested_snapshot(), side="LONG",
                requested_notional="10.5", now=BASE,
            )
            self.assertEqual(opened["status"], "PAPER_POSITION_OPEN")
            later = BASE + timedelta(minutes=2)
            degraded = nested_snapshot(
                now=later, mark="98", bid="97.95", ask="98.05", funding=False,
            )
            closed = engine.process_snapshot(degraded, now=later)
            self.assertEqual(closed["status"], "PAPER_POSITION_CLOSED")
            self.assertEqual(closed["close_reason"], "stop_loss")

    def test_retention_prunes_snapshots_older_than_configured_window(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(
                Path(directory) / "perps.sqlite3",
                snapshot_retention_days=1,
            )
            engine.process_snapshot(nested_snapshot(), now=BASE)
            later = BASE + timedelta(days=2)
            engine.process_snapshot(nested_snapshot(now=later), now=later)
            status = engine.status(now=later)
            self.assertEqual(status["market_snapshots"], 1)


if __name__ == "__main__":
    unittest.main()
