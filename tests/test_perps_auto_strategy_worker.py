from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from perps_market_history_store import PerpsMarketHistoryStore
from perps_market_worker import read_perps_worker_status, run_worker
from perps_paper_engine import PerpsPaperEngine


def market():
    now = datetime.now(timezone.utc)
    stamp = int(now.timestamp() * 1000)
    return {
        "instrument": {
            "instrument_id": 55, "instrument_type": "perpetual",
            "symbol": "BTC-PERP", "display_symbol": "BTC",
            "category": "crypto", "base_asset": "BTC", "quote_asset": "USD",
            "funding_interval": "1h", "close_only": False, "isolated_only": False,
            "min_notional": "10", "max_market_notional": "100000",
            "max_limit_notional": "1000000", "max_leverage": "20",
            "quantity_decimals": 4, "price_decimals": 2,
            "risk_tiers": [{"lower_bound": "0", "max_leverage": "20"}],
        },
        "ticker": {
            "instrument_id": 55, "symbol": "BTC-PERP",
            "index_price": "100", "mark_price": "100",
            "last_price": "100", "mid_price": "100",
            "open_interest": "100", "funding_rate": "0.0001",
            "timestamp_ms": stamp, "next_funding_ms": stamp + 3_600_000,
        },
        "book": {
            "instrument_id": 55, "timestamp_ms": stamp, "sequence": stamp,
            "best_bid": "99.95", "best_ask": "100.05",
            "bids": [{"price": "99.95", "quantity": "2"}],
            "asks": [{"price": "100.05", "quantity": "2"}],
        },
        "received_at": now.isoformat(),
        "research_only": True, "execution": "none",
    }


class OnceProvider:
    def __init__(self, row):
        self.row = row

    def load(self):
        return [self.row]


class SignalStrategy:
    def __init__(self):
        self.calls = 0
        self.decision_bar = None

    def evaluate(self, history, *, current_snapshot, now):
        self.calls += 1
        if self.decision_bar is None:
            self.decision_bar = int(now.timestamp() // 60 * 60_000)
        return {
            "strategy": "test_signal",
            "research_only": True,
            "execution": "none",
            "live_execution_eligible": False,
            "signal": True,
            "side": "LONG",
            "reason": "bullish_ema_crossover_confirmed",
            "bar_close_ms": self.decision_bar,
            "completed_bars": len(history),
            "entry_notional_usd": "10",
            "stop_loss_distance_pct": "0.005",
            "reward_risk_multiple": "2",
        }


class PerpsAutoStrategyWorkerTests(unittest.TestCase):
    def _run(self, root, *, engine, store, strategy, auto_entry=False):
        row = market()
        with patch.dict(
            "os.environ",
            {
                "ENABLE_PERPS_PAPER_ENGINE": "true",
                "PERPS_SIGNAL_EVALUATION_ENABLED": "true",
                "PERPS_AUTO_ENTRY_ENABLED": "true" if auto_entry else "false",
            },
        ):
            return run_worker(
                provider=OnceProvider(row),
                store=store,
                status_path=root / "worker-status.json",
                poll_interval_seconds=15,
                max_cycles=1,
                paper_engine=engine,
                strategy_engine=strategy,
                backup_function=None,
            )

    def test_signal_only_mode_persists_one_decision_without_opening_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = PerpsPaperEngine(root / "paper.sqlite3")
            store = PerpsMarketHistoryStore(root / "history.sqlite3")
            self.addCleanup(store.close)
            strategy = SignalStrategy()
            result = self._run(
                root, engine=engine, store=store, strategy=strategy, auto_entry=False,
            )
            self.assertEqual(result, 0)
            summary = engine.strategy_decision_summary()
            self.assertEqual(summary["signals_logged"], 1)
            self.assertEqual(summary["entries_opened"], 0)
            self.assertEqual(engine.account_metrics()["open_positions"], 0)
            status = read_perps_worker_status(root / "worker-status.json")
            self.assertEqual(status["paper_strategy_status"], "active")
            self.assertEqual(status["signals_logged"], 1)
            self.assertEqual(status["entries_opened_total"], 0)
            self.assertEqual(status["strategy_last_reason"], "bullish_ema_crossover_confirmed")

            # A worker restart on the same candle must not create another decision.
            self._run(root, engine=engine, store=store, strategy=strategy, auto_entry=False)
            self.assertEqual(engine.strategy_decision_summary()["decisions_total"], 1)
            self.assertEqual(engine.account_metrics()["open_positions"], 0)

    def test_explicit_paper_auto_entry_opens_only_one_risk_gated_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = PerpsPaperEngine(root / "paper.sqlite3")
            store = PerpsMarketHistoryStore(root / "history.sqlite3")
            self.addCleanup(store.close)
            strategy = SignalStrategy()
            result = self._run(
                root, engine=engine, store=store, strategy=strategy, auto_entry=True,
            )
            self.assertEqual(result, 0)
            self.assertEqual(engine.account_metrics()["open_positions"], 1)
            summary = engine.strategy_decision_summary()
            self.assertEqual(summary["entries_opened"], 1)
            self.assertEqual(summary["signals_logged"], 1)
            trade = engine.list_trades()[0]
            self.assertEqual(trade["side"], "LONG")
            self.assertEqual(trade["status"], "OPEN")


if __name__ == "__main__":
    unittest.main()
