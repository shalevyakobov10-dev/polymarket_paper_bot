from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perps_paper_worker import read_perps_worker_status, run_worker


def sample_snapshot():
    now = datetime.now(timezone.utc)
    timestamp = int(now.timestamp() * 1000)
    return {
        "research_only": True,
        "execution": "none",
        "entry_eligible": True,
        "instrument_id": 101,
        "symbol": "BTC-PERP",
        "observed_at": now.isoformat(),
        "ticker_timestamp_ms": timestamp,
        "book_timestamp_ms": timestamp,
        "ticker_age_seconds": 1,
        "book_age_seconds": 1,
        "mark_price": "100",
        "index_price": "100",
        "next_funding_ms": timestamp + 3_600_000,
        "funding_rate": "0.0001",
        "funding_interval": "1h",
        "bids": [{"price": "99.95", "quantity": "1"}],
        "asks": [{"price": "100.05", "quantity": "1"}],
    }


class StopAfterWaits:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.calls = 0

    def is_set(self):
        return False

    def wait(self, _seconds):
        self.calls += 1
        return self.calls >= self.stop_after


class FakeProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def load_snapshots(self):
        value = self.outcomes.pop(0) if self.outcomes else [sample_snapshot()]
        if isinstance(value, Exception):
            raise value
        return value


class FakeEngine:
    def __init__(self, outcome=None):
        self.database_path = Path("unused.sqlite3")
        self.processed = []
        self.outcome = outcome or {"status": "PAPER_MARK_RECORDED", "open_position": False}

    def process_snapshot(self, snapshot):
        self.processed.append(snapshot["symbol"])
        return dict(self.outcome)

    def recent_snapshots(self, instrument_id, *, limit=12000):
        return [{"instrument_id": instrument_id, "symbol": "BTC-PERP"}]

    def claim_strategy_bar(self, instrument_id, bar_close_ms, *, now=None):
        self.claimed = (instrument_id, bar_close_ms)
        return True

    def open_position(self, snapshot, **kwargs):
        self.opened = kwargs
        return {"status": "PAPER_POSITION_OPEN", "trade_id": "paper-trade-1"}

    def record_strategy_decision(self, instrument_id, bar_close_ms, **kwargs):
        self.recorded = (instrument_id, bar_close_ms, kwargs)

    def account_metrics(self):
        return {
            "open_positions": 0,
            "cash_balance": "100",
            "equity": "100",
        }


class FakeSignalStrategy:
    def evaluate(self, history, *, current_snapshot, now):
        return {
            "signal": True,
            "side": "LONG",
            "reason": "bullish_ema_crossover_confirmed",
            "bar_close_ms": int(now.timestamp() * 1000) // 60_000 * 60_000,
            "entry_notional_usd": "5",
            "stop_loss_distance_pct": "0.005",
            "reward_risk_multiple": "2",
        }


class PerpsPaperWorkerTests(unittest.TestCase):
    def test_runs_public_market_cycle_and_writes_sanitized_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perps-worker.json"
            provider = FakeProvider([[sample_snapshot()]])
            engine = FakeEngine()
            result = run_worker(
                provider_factory=lambda: provider,
                engine_factory=lambda: engine,
                stop_event=StopAfterWaits(1),
                status_path=path,
                poll_seconds=1,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            self.assertEqual(engine.processed, ["BTC-PERP"])
            status = read_perps_worker_status(path)
            self.assertEqual(status["worker_state"], "stopped")
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["markets_loaded_last_cycle"], 1)
            self.assertTrue(status["research_only"])
            self.assertFalse(status["live_execution_enabled"])
            self.assertEqual(status["execution"], "none")
            raw = json.loads(path.read_text())
            self.assertEqual(raw["strategy_status"], "disabled")

    def test_opt_in_autonomous_strategy_routes_signal_through_paper_engine_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perps-worker.json"
            provider = FakeProvider([[sample_snapshot()]])
            engine = FakeEngine()
            with patch.dict("os.environ", {"PERPS_AUTO_ENTRY_ENABLED": "true"}):
                with patch("perps_paper_worker.strategy_from_env", return_value=FakeSignalStrategy()):
                    result = run_worker(
                        provider_factory=lambda: provider,
                        engine_factory=lambda: engine,
                        stop_event=StopAfterWaits(1),
                        status_path=path,
                        poll_seconds=1,
                        backup_function=None,
                    )
            self.assertEqual(result, 0)
            self.assertEqual(engine.opened["side"], "LONG")
            self.assertEqual(engine.opened["requested_notional"], "5")
            self.assertEqual(engine.opened["stop_loss_distance_pct"], "0.005")
            self.assertEqual(engine.recorded[2]["decision_status"], "entry_opened")
            status = read_perps_worker_status(path)
            self.assertEqual(status["strategy_status"], "active")
            self.assertEqual(status["signals_last_cycle"], 1)
            self.assertEqual(status["entries_opened_last_cycle"], 1)

    def test_blocked_protective_exit_marks_worker_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perps-worker.json"
            provider = FakeProvider([[sample_snapshot()]])
            engine = FakeEngine({"status": "PAPER_EXIT_BLOCKED", "reason": "insufficient_visible_depth"})
            result = run_worker(
                provider_factory=lambda: provider,
                engine_factory=lambda: engine,
                stop_event=StopAfterWaits(1),
                status_path=path,
                poll_seconds=1,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            raw = json.loads(path.read_text())
            self.assertEqual(raw["last_cycle_warning"], "protective_exit_blocked")
            self.assertEqual(raw["exit_blocks_last_cycle"], 1)
            self.assertEqual(raw["last_exit_block_reason"], "insufficient_visible_depth")
            self.assertEqual(read_perps_worker_status(path)["last_exit_block_reason"], "insufficient_visible_depth")

    def test_retries_a_transient_provider_error_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            provider = FakeProvider([
                RuntimeError("private response must not be recorded"),
                [sample_snapshot()],
            ])
            engine = FakeEngine()
            result = run_worker(
                provider_factory=lambda: provider,
                engine_factory=lambda: engine,
                stop_event=StopAfterWaits(2),
                status_path=path,
                poll_seconds=1,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            status = read_perps_worker_status(path)
            self.assertEqual(status["cycles"], 1)
            self.assertEqual(status["total_failures"], 1)
            self.assertEqual(status["consecutive_failures"], 0)
            self.assertEqual(status["worker_state"], "stopped")
            raw = json.loads(path.read_text())
            self.assertNotIn("private response", raw.get("last_error_type") or "")

    def test_exits_after_bounded_consecutive_failures_for_supervisor_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            provider = FakeProvider([
                RuntimeError("network unavailable"),
                RuntimeError("network unavailable"),
            ])
            result = run_worker(
                provider_factory=lambda: provider,
                engine_factory=FakeEngine,
                stop_event=StopAfterWaits(10),
                status_path=path,
                poll_seconds=1,
                max_consecutive_failures=2,
                backup_function=None,
            )
            self.assertEqual(result, 1)
            status = read_perps_worker_status(path)
            self.assertEqual(status["worker_state"], "failed")
            self.assertEqual(status["total_failures"], 2)
            self.assertEqual(status["last_error_type"], "RuntimeError")

    def test_status_reader_rejects_bad_heartbeat_and_drops_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            payload = {
                "schema_version": 1,
                "worker_state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cycles": 2,
                "private_key": "must-not-leak",
                "account_secret": "must-not-leak",
            }
            path.write_text(json.dumps(payload))
            status = read_perps_worker_status(path)
            self.assertTrue(status["heartbeat_fresh"])
            self.assertNotIn("private_key", status)
            self.assertNotIn("account_secret", status)

            payload["updated_at"] = "not-a-timestamp"
            path.write_text(json.dumps(payload))
            broken = read_perps_worker_status(path)
            self.assertEqual(broken["worker_state"], "unknown")
            self.assertFalse(broken["heartbeat_fresh"])


if __name__ == "__main__":
    unittest.main()
