from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from perps_market_worker import read_perps_worker_status, run_worker
from perps_paper_engine import PerpsPaperEngine


class FakeProvider:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self):
        return [self.snapshot]


class FakeStore:
    def __init__(self):
        self.rows = []
        self.history_query = None

    def count(self):
        return len(self.rows)

    def record_snapshots(self, snapshots):
        self.rows.extend(snapshots)
        return {"inserted": len(snapshots), "total_snapshots": len(self.rows)}

    def history(self, instrument_id, limit=100, *, as_of_received_at=None):
        self.history_query = (instrument_id, limit, as_of_received_at)
        as_of = as_of_received_at
        rows = []
        for offset in range(60, 0, -1):
            observed = as_of - timedelta(minutes=offset)
            price = 100 + (60 - offset) * 0.02
            rows.append({
                "instrument_id": instrument_id,
                "received_at": observed.isoformat(),
                "ticker": {"mark_price": str(price)},
            })
        return rows

    def prune_before(self, _cutoff):
        return {"deleted": 0, "remaining": len(self.rows)}


class FakePaperEngine:
    def __init__(self):
        self.processed = []
        self.decisions = []
        self.summary_calls = 0

    def summary(self):
        self.summary_calls += 1
        return {
            "halted": False,
            "cash": "1000",
            "equity": "1000",
            "open_positions": 0,
            "closed_positions": 0,
            "ema_shadow": {
                "decisions_total": len(self.decisions),
                "signals": sum(1 for item in self.decisions if item["signal"]),
                "no_signal": sum(1 for item in self.decisions if not item["signal"]),
                "latest": None,
            },
        }

    def funding_requirements(self, _snapshots):
        return []

    def process_snapshot(self, snapshot, history_store):
        self.processed.append(snapshot["instrument"]["instrument_id"])
        return {"status": "snapshot_recorded"}

    def record_ema_shadow_decision(self, instrument_id, decision, *, evaluated_at):
        self.decisions.append({
            "instrument_id": instrument_id,
            "bar_close_ms": decision["bar_close_ms"],
            "signal": decision["signal"],
            "evaluated_at": evaluated_at,
        })
        return True


class FakeShadowStrategy:
    def __init__(self, *, raises=False, signal=True):
        self.config = SimpleNamespace(minimum_completed_bars=60)
        self.raises = raises
        self.signal = signal
        self.history_seen = None
        self.snapshot_seen = None
        self.now_seen = None

    def evaluate(self, history, *, current_snapshot, now):
        if self.raises:
            raise RuntimeError("shadow strategy test error")
        self.history_seen = history
        self.snapshot_seen = current_snapshot
        self.now_seen = now
        return {
            "strategy": "ema_8_21_momentum_volatility_v1",
            "research_only": True,
            "execution": "none",
            "signal": self.signal,
            "side": "LONG" if self.signal else None,
            "reason": "bullish_ema_crossover_confirmed" if self.signal else "no_confirmed_crossover",
            "bar_close_ms": int(now.timestamp() // 60 * 60_000),
            "completed_bars": 60,
            "fast_ema": "101.1",
            "slow_ema": "100.9",
        }


def sample_snapshot():
    now = datetime.now(timezone.utc)
    timestamp_ms = int(now.timestamp() * 1000)
    return {
        "instrument": {
            "instrument_id": 41,
            "instrument_type": "perpetual",
            "symbol": "BTC-PERP",
            "close_only": False,
            "settlement": None,
        },
        "ticker": {
            "instrument_id": 41,
            "mark_price": "101.5",
            "index_price": "101.4",
            "timestamp_ms": timestamp_ms,
        },
        "book": {
            "instrument_id": 41,
            "timestamp_ms": timestamp_ms,
            "sequence": timestamp_ms,
            "best_bid": "101.4",
            "best_ask": "101.6",
            "bids": [{"price": "101.4", "quantity": "2"}],
            "asks": [{"price": "101.6", "quantity": "2"}],
        },
        "fees": {"source": "public_perps_fee_schedule", "taker_fee_rate": "0.0005"},
        "received_at": now.isoformat(),
        "research_only": True,
        "execution": "none",
    }


class PerpsEmaShadowLedgerTests(unittest.TestCase):
    def test_shadow_ledger_is_idempotent_per_instrument_strategy_and_closed_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(Path(directory) / "perps.sqlite3")
            self.addCleanup(engine.close)
            now = datetime.now(timezone.utc)
            bar_close_ms = int(now.timestamp() // 60 * 60_000)
            decision = {
                "strategy": "ema_8_21_momentum_volatility_v1",
                "research_only": True,
                "execution": "none",
                "signal": True,
                "side": "LONG",
                "reason": "bullish_ema_crossover_confirmed",
                "bar_close_ms": bar_close_ms,
                "completed_bars": 60,
                "fast_ema": "101.1",
                "slow_ema": "100.9",
            }
            self.assertTrue(engine.record_ema_shadow_decision(41, decision, evaluated_at=now))
            self.assertFalse(engine.record_ema_shadow_decision(41, decision, evaluated_at=now))
            summary = engine.summary()["ema_shadow"]
            self.assertEqual(summary["decisions_total"], 1)
            self.assertEqual(summary["signals"], 1)
            self.assertEqual(summary["no_signal"], 0)
            self.assertEqual(summary["mode"], "shadow_only")
            self.assertFalse(summary["order_submission_allowed"])

    def test_ledger_rejects_live_or_unbounded_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PerpsPaperEngine(Path(directory) / "perps.sqlite3")
            self.addCleanup(engine.close)
            now = datetime.now(timezone.utc)
            decision = {
                "strategy": "ema_8_21_momentum_volatility_v1",
                "research_only": False,
                "execution": "submit_order",
                "signal": True,
                "side": "LONG",
                "reason": "bad",
                "bar_close_ms": 1_800_000_000_000,
                "completed_bars": 60,
            }
            with self.assertRaisesRegex(ValueError, "research-only"):
                engine.record_ema_shadow_decision(41, decision, evaluated_at=now)


class PerpsEmaShadowWorkerTests(unittest.TestCase):
    def _run(self, directory, *, strategy):
        snapshot = sample_snapshot()
        provider = FakeProvider(snapshot)
        store = FakeStore()
        engine = FakePaperEngine()
        status_path = Path(directory) / "worker.json"
        result = run_worker(
            provider=provider,
            store=store,
            paper_engine=engine,
            paper_engine_enabled=True,
            ema_shadow_strategy=strategy,
            status_path=status_path,
            poll_interval_seconds=15,
            history_retention_days=14,
            backup_function=None,
            max_cycles=1,
        )
        status = read_perps_worker_status(status_path)
        return result, status, store, engine, strategy

    def test_shadow_worker_uses_point_in_time_history_and_never_opens_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            result, status, store, engine, strategy = self._run(
                directory, strategy=FakeShadowStrategy(signal=True),
            )
            self.assertEqual(result, 0)
            self.assertEqual(store.history_query[0], 41)
            self.assertEqual(store.history_query[1], 5000)
            self.assertEqual(store.history_query[2].isoformat(), strategy.now_seen.isoformat())
            self.assertEqual(strategy.snapshot_seen["execution"], "none")
            self.assertTrue(strategy.snapshot_seen["research_only"])
            self.assertEqual(len(engine.decisions), 1)
            self.assertEqual(len(engine.processed), 1)
            self.assertEqual(status["ema_shadow_enabled"], True)
            self.assertEqual(status["ema_shadow_evaluations_last_cycle"], 1)
            self.assertEqual(status["ema_shadow_signals_last_cycle"], 1)
            self.assertEqual(status["ema_shadow_errors_last_cycle"], 0)
            self.assertFalse(status["live_execution_enabled"])
            self.assertEqual(status["execution"], "none")

    def test_shadow_strategy_failure_does_not_interrupt_existing_position_management(self):
        with tempfile.TemporaryDirectory() as directory:
            result, status, _store, engine, _strategy = self._run(
                directory, strategy=FakeShadowStrategy(raises=True),
            )
            self.assertEqual(result, 0)
            self.assertEqual(engine.processed, [41])
            self.assertEqual(engine.decisions, [])
            self.assertEqual(status["ema_shadow_errors_last_cycle"], 1)
            self.assertTrue(status["ema_shadow_enabled"])
            self.assertTrue(status["research_only"])
            self.assertFalse(status["live_execution_enabled"])


if __name__ == "__main__":
    unittest.main()
