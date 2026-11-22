import json
import tempfile
import unittest
from pathlib import Path

from perps_market_worker import read_perps_worker_status, run_worker


class FakeStopEvent:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        return True


class FakeProvider:
    def __init__(self, snapshots=None, error=None):
        self.snapshots = snapshots or [{
            "instrument": {
                "instrument_id": 1, "symbol": "BTC-PERP", "category": "crypto"
            },
            "ticker": {
                "instrument_id": 1, "timestamp_ms": 1795334400000,
                "index_price": "100", "mark_price": "100",
                "last_price": "100", "mid_price": "100",
                "open_interest": "10", "funding_rate": "0.0001",
            },
            "book": {
                "instrument_id": 1, "timestamp_ms": 1795334400000,
                "sequence": 1, "best_bid": "99", "best_ask": "101",
                "bids": [{"price": "99", "quantity": "1"}],
                "asks": [{"price": "101", "quantity": "1"}],
            },
            "received_at": "2026-11-22T08:00:00+00:00",
        }]
        self.error = error

    def load(self):
        if self.error:
            raise self.error
        return self.snapshots


class FakeStore:
    def __init__(self):
        self.rows = []
        self.closed = False

    def count(self):
        return len(self.rows)

    def record_snapshots(self, rows):
        self.rows.extend(rows)
        return {
            "inserted": len(rows), "total_snapshots": len(self.rows)
        }

    def close(self):
        self.closed = True


class FakePaperEngine:
    def __init__(self):
        self.processed = []

    def funding_requirements(self, snapshots):
        return []

    def process_snapshot(self, snapshot, history_store):
        self.processed.append(snapshot["instrument"]["instrument_id"])

    def summary(self):
        return {
            "cash": "975",
            "equity": "1000",
            "peak_equity": "1000",
            "isolated_margin_reserved": "25",
            "unrealized_pnl": "0",
            "open_positions": 1,
            "closed_positions": 0,
            "closed_net_pnl": "0",
            "halted": False,
            "halt_reason": None,
            "max_leverage": "2",
            "max_position_notional": "25",
            "max_total_notional": "75",
            "taker_fee_rate_assumption": "0.0005",
            "liquidation_fee_rate_assumption": "0.01",
            "event_counts": {"PAPER_POSITION_OPENED": 1},
            "research_only": True,
            "live_execution_enabled": False,
            "order_submission_allowed": False,
            "execution": "none",
            "POLYMARKET_PRIVATE_KEY": "must-not-leak",
        }


class PerpsMarketWorkerTests(unittest.TestCase):
    def test_worker_records_market_data_and_publishes_healthy_status(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = FakeStore()
            result = run_worker(
                provider=FakeProvider(),
                store=store,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "healthy")
            self.assertEqual(raw["markets_in_last_snapshot"], 1)
            self.assertEqual(raw["heartbeat_timeout_seconds"], 205)
            self.assertEqual(raw["markets_with_fee_schedule"], 0)
            self.assertEqual(raw["history_rows"], 1)
            self.assertFalse(raw["live_execution_enabled"])
            self.assertEqual(raw["execution"], "none")
            self.assertEqual(len(store.rows), 1)

    def test_worker_runs_optional_perps_paper_engine_after_archiving_public_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = FakeStore()
            engine = FakePaperEngine()
            result = run_worker(
                provider=FakeProvider(snapshots=[{
                    **FakeProvider().snapshots[0],
                    "fees": {
                        "taker_fee_rate": "0.0004",
                        "maker_fee_rate": "0.0001",
                        "source": "public_perps_fee_schedule",
                    },
                }]),
                store=store,
                paper_engine=engine,
                paper_engine_enabled=True,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            self.assertEqual(engine.processed, [1])
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(raw["paper_engine_enabled"])
            self.assertEqual(raw["paper_engine_state"], "healthy")
            self.assertEqual(raw["paper_engine"]["open_positions"], 1)
            self.assertFalse(raw["paper_engine"]["live_execution_enabled"])
            sanitized = read_perps_worker_status(target)
            self.assertEqual(sanitized["paper_engine_state"], "healthy")
            self.assertEqual(sanitized["paper_engine"]["equity"], "1000")
            self.assertFalse(sanitized["paper_engine"]["order_submission_allowed"])
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", sanitized["paper_engine"])

    def test_enabled_paper_engine_degrades_when_no_verified_fee_schedule_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            engine = FakePaperEngine()
            result = run_worker(
                provider=FakeProvider(),  # no public fee metadata
                store=FakeStore(),
                paper_engine=engine,
                paper_engine_enabled=True,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "healthy")
            self.assertEqual(raw["paper_engine_state"], "degraded")
            self.assertEqual(raw["paper_engine_degraded_reason"], "no_verified_fee_schedule")
            self.assertEqual(raw["markets_with_fee_schedule"], 0)
            safe = read_perps_worker_status(target)
            self.assertEqual(safe["paper_engine_state"], "degraded")
            self.assertEqual(safe["paper_engine_degraded_reason"], "no_verified_fee_schedule")
            self.assertFalse(safe["paper_engine"]["order_submission_allowed"])

    def test_worker_backups_perps_position_ledger_and_funding_history_together(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "status.json"
            paper_db = root / "perps_paper.sqlite3"
            market_db = root / "perps_market_history.sqlite3"
            paper_db.write_bytes(b"paper database placeholder")
            market_db.write_bytes(b"market history placeholder")
            store = FakeStore()
            engine = FakePaperEngine()
            calls = []

            def fake_backup(paths, backup_directory, *, retain):
                calls.append((list(paths), Path(backup_directory), retain))
                return {
                    "created_at": "2026-11-22T08:00:00+00:00",
                    "manifest_name": "perps-backup-manifest.json",
                    "files": [{"backup_name": "paper.sqlite3"}, {"backup_name": "history.sqlite3"}],
                    "skipped_missing_sources": [],
                }

            with patch.dict(os.environ, {
                "PERPS_PAPER_DB": str(paper_db),
                "PERPS_MARKET_HISTORY_DB": str(market_db),
                "PERPS_BACKUP_DIRECTORY": str(root / "backups"),
            }, clear=False):
                result = run_worker(
                    provider=FakeProvider(),
                    store=store,
                    paper_engine=engine,
                    paper_engine_enabled=True,
                    backup_function=fake_backup,
                    stop_event=FakeStopEvent(),
                    status_path=target,
                    poll_interval_seconds=15,
                    backup_interval_hours=6,
                    backup_retain=7,
                    max_cycles=1,
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], [paper_db, market_db])
            self.assertEqual(calls[0][1], root / "backups")
            self.assertEqual(calls[0][2], 7)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["backup_files_last_run"], 2)
            self.assertEqual(raw["last_backup_manifest_name"], "perps-backup-manifest.json")
            self.assertIsNone(raw["backup_error_type"])
            safe = read_perps_worker_status(target)
            self.assertEqual(safe["backup_files_last_run"], 2)
            self.assertEqual(safe["backup_consecutive_failures"], 0)

    def test_worker_fetches_and_persists_exact_funding_before_engine_processing(self):
        class OrderedProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.order = []

            def load_funding_history(self, instrument_id, *, start_timestamp_ms, end_timestamp_ms):
                self.order.append("fetch_funding")
                self.asserted_window = (instrument_id, start_timestamp_ms, end_timestamp_ms)
                return [{
                    "instrument_id": instrument_id,
                    "timestamp_ms": start_timestamp_ms,
                    "funding_rate": "0.00025",
                }]

        class OrderedStore(FakeStore):
            def __init__(self, order):
                super().__init__()
                self.order = order
                self.funding_rows = []

            def record_funding_history(self, instrument_id, entries, *, received_at=None):
                self.order.append("persist_funding")
                self.funding_rows.extend(entries)
                return {"received": len(entries), "inserted": len(entries), "duplicates": 0}

        class OrderedEngine(FakePaperEngine):
            def __init__(self, order):
                super().__init__()
                self.order = order

            def funding_requirements(self, snapshots):
                return [{
                    "instrument_id": 1,
                    "start_timestamp_ms": 1795334400000,
                    "end_timestamp_ms": 1795338000000,
                }]

            def process_snapshot(self, snapshot, history_store):
                self.order.append("process_snapshot")
                super().process_snapshot(snapshot, history_store)

        with tempfile.TemporaryDirectory() as directory:
            order = []
            provider = OrderedProvider()
            store = OrderedStore(order)
            engine = OrderedEngine(order)
            provider.order = order
            result = run_worker(
                provider=provider,
                store=store,
                paper_engine=engine,
                paper_engine_enabled=True,
                stop_event=FakeStopEvent(),
                status_path=Path(directory) / "status.json",
                poll_interval_seconds=15,
                max_cycles=1,
                backup_function=None,
            )
            self.assertEqual(result, 0)
            self.assertEqual(order, ["fetch_funding", "persist_funding", "process_snapshot"])
            self.assertEqual(store.funding_rows[0]["funding_rate"], "0.00025")

    def test_worker_deduplicates_exact_funding_requirements_per_cycle(self):
        class CountingProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.funding_calls = []

            def load_funding_history(self, instrument_id, *, start_timestamp_ms, end_timestamp_ms):
                self.funding_calls.append((instrument_id, start_timestamp_ms, end_timestamp_ms))
                return [{
                    "instrument_id": instrument_id,
                    "timestamp_ms": start_timestamp_ms,
                    "funding_rate": "0.00025",
                }]

        class DuplicateRequirementEngine(FakePaperEngine):
            def funding_requirements(self, snapshots):
                request = {
                    "instrument_id": 1,
                    "start_timestamp_ms": 1795334400000,
                    "end_timestamp_ms": 1795338000000,
                }
                return [request, dict(request)]

        with tempfile.TemporaryDirectory() as directory:
            provider = CountingProvider()
            store = FakeStore()

            def persist(instrument_id, entries, *, received_at=None):
                store.persisted_funding = list(entries)
                return {"received": len(entries), "inserted": len(entries), "duplicates": 0}

            store.record_funding_history = persist
            result = run_worker(
                provider=provider,
                store=store,
                paper_engine=DuplicateRequirementEngine(),
                paper_engine_enabled=True,
                backup_function=None,
                stop_event=FakeStopEvent(),
                status_path=Path(directory) / "status.json",
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(provider.funding_calls), 1)
            self.assertEqual(len(store.persisted_funding), 1)

    def test_perps_backup_failure_is_reported_with_bounded_retry(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper_db = root / "perps_paper.sqlite3"
            market_db = root / "perps_market_history.sqlite3"
            paper_db.write_bytes(b"paper")
            market_db.write_bytes(b"history")
            status_file = root / "status.json"

            def failed_backup(*args, **kwargs):
                raise OSError("simulated disk full")

            with patch.dict(os.environ, {
                "PERPS_PAPER_DB": str(paper_db),
                "PERPS_MARKET_HISTORY_DB": str(market_db),
            }, clear=False):
                result = run_worker(
                    provider=FakeProvider(),
                    store=FakeStore(),
                    paper_engine=FakePaperEngine(),
                    paper_engine_enabled=True,
                    backup_function=failed_backup,
                    stop_event=FakeStopEvent(),
                    status_path=status_file,
                    poll_interval_seconds=15,
                    max_cycles=1,
                )

            self.assertEqual(result, 0)
            raw = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(raw["backup_error_type"], "OSError")
            self.assertEqual(raw["backup_consecutive_failures"], 1)
            self.assertEqual(raw["backup_retry_delay_seconds"], 60)
            safe = read_perps_worker_status(status_file)
            self.assertEqual(safe["backup_error_type"], "OSError")
            self.assertEqual(safe["backup_retry_delay_seconds"], 60)

    def test_worker_exits_after_bounded_consecutive_failures_for_supervisor_restart(self):
        class ContinueStopEvent:
            def is_set(self):
                return False

            def wait(self, seconds):
                return False

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            result = run_worker(
                provider=FakeProvider(error=RuntimeError("persistent upstream outage")),
                store=FakeStore(),
                stop_event=ContinueStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                backup_function=None,
                max_consecutive_failures=2,
            )
            self.assertEqual(result, 1)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "degraded")
            self.assertEqual(raw["consecutive_failures"], 2)
            self.assertIn("persistent upstream outage", raw["last_error"])

    def test_worker_counts_only_snapshots_with_public_fee_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeProvider(snapshots=[
                {
                    **FakeProvider().snapshots[0],
                    "fees": {
                        "source": "public_perps_fee_schedule",
                        "taker_fee_rate": "0.0004",
                        "maker_fee_rate": "0.0001",
                    },
                },
                {**FakeProvider().snapshots[0], "instrument": {
                    "instrument_id": 2, "symbol": "ETH-PERP", "category": "crypto"
                }},
            ])
            result = run_worker(
                provider=provider,
                store=FakeStore(),
                stop_event=FakeStopEvent(),
                status_path=Path(directory) / "status.json",
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            status = read_perps_worker_status(Path(directory) / "status.json")
            self.assertEqual(status["markets_in_last_snapshot"], 2)
            self.assertEqual(status["markets_with_fee_schedule"], 1)

    def test_worker_marks_degraded_and_returns_failure_when_data_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = FakeStore()
            result = run_worker(
                provider=FakeProvider(error=RuntimeError("public feed unavailable")),
                store=store,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                max_cycles=1,
            )
            self.assertEqual(result, 1)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["worker_state"], "degraded")
            self.assertIn("public feed unavailable", raw["last_error"])
            self.assertEqual(raw["consecutive_failures"], 1)
            self.assertEqual(store.rows, [])

    def test_worker_records_retention_policy_and_prunes_expired_history(self):
        class RetentionStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.prune_calls = []

            def prune_before(self, cutoff):
                self.prune_calls.append(cutoff)
                return {"deleted": 2, "remaining": len(self.rows)}

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            store = RetentionStore()
            result = run_worker(
                provider=FakeProvider(),
                store=store,
                stop_event=FakeStopEvent(),
                status_path=target,
                poll_interval_seconds=15,
                history_retention_days=14,
                max_cycles=1,
            )
            self.assertEqual(result, 0)
            self.assertEqual(len(store.prune_calls), 1)
            raw = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(raw["history_retention_days"], 14)
            self.assertEqual(raw["expired_rows_pruned_total"], 2)
            self.assertEqual(raw["history_rows"], 1)

    def test_status_reader_rejects_boolean_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text(json.dumps({
                "schema_version": 1,
                "enabled": True,
                "worker_state": "healthy",
                "consecutive_failures": True,
            }), encoding="utf-8")
            result = read_perps_worker_status(target)
            self.assertEqual(result["worker_state"], "unknown")
            self.assertEqual(result["last_error"], "status_file_invalid")

    def test_status_reader_drops_unexpected_and_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text(json.dumps({
                "schema_version": 1,
                "enabled": True,
                "worker_state": "healthy",
                "last_heartbeat_at": "2026-11-22T08:00:00+00:00",
                "last_success_at": "2026-11-22T08:00:00+00:00",
                "consecutive_failures": 0,
                "markets_in_last_snapshot": 3,
                "snapshots_inserted_total": 4,
                "history_rows": 4,
                "poll_interval_seconds": 30,
                "POLYMARKET_PRIVATE_KEY": "never-expose",
                "unknown_field": "discard",
            }), encoding="utf-8")
            result = read_perps_worker_status(target)
            self.assertTrue(result["enabled"])
            self.assertEqual(result["markets_in_last_snapshot"], 3)
            self.assertEqual(result["heartbeat_timeout_seconds"], 180)
            self.assertFalse(result["live_execution_enabled"])
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", result)
            self.assertNotIn("unknown_field", result)

    def test_missing_or_invalid_status_is_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing.json"
            missing = read_perps_worker_status(target)
            self.assertEqual(missing["worker_state"], "unknown")
            target.write_text("{broken", encoding="utf-8")
            invalid = read_perps_worker_status(target)
            self.assertEqual(invalid["worker_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
