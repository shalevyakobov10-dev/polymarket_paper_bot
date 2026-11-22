"""Resilient public Perps market-history collector; no account or execution access."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from perps_market_data import PerpsPublicMarketDataProvider
from perps_market_history_store import PerpsMarketHistoryStore
from perps_paper_engine import PerpsPaperEngine


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp = handle.name
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _status_count(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _sanitize_paper_summary(value: Any) -> dict[str, Any] | None:
    """Allowlist paper portfolio telemetry and never expose raw database rows."""
    if not isinstance(value, dict):
        return None
    summary: dict[str, Any] = {
        "mode": "POLYMARKET_PERPS_ISOLATED_MARGIN_PAPER",
        "research_only": True,
        "live_execution_enabled": False,
        "order_submission_allowed": False,
        "execution": "none",
    }
    amount_fields = (
        "cash", "equity", "peak_equity", "isolated_margin_reserved",
        "unrealized_pnl", "closed_net_pnl", "daily_start_equity",
        "max_leverage", "max_position_notional", "max_total_notional",
        "taker_fee_rate_assumption", "liquidation_fee_rate_assumption",
    )
    for field in amount_fields:
        raw = value.get(field)
        if raw is None:
            continue
        text_value = str(raw).strip()
        if len(text_value) > 80:
            continue
        try:
            from decimal import Decimal, InvalidOperation
            parsed = Decimal(text_value)
            if parsed.is_finite():
                summary[field] = text_value
        except (InvalidOperation, ValueError, TypeError):
            continue
    for field in ("open_positions", "closed_positions"):
        raw = value.get(field)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            summary[field] = raw
    if value.get("halted") in (True, False):
        summary["halted"] = value["halted"]
    if value.get("halt_reason") is None or isinstance(value.get("halt_reason"), str):
        summary["halt_reason"] = str(value.get("halt_reason"))[:120] if value.get("halt_reason") else None
    if isinstance(value.get("utc_day"), str):
        summary["utc_day"] = value["utc_day"][:10]
    if isinstance(value.get("event_counts"), dict):
        counts = {}
        for key, count in list(value["event_counts"].items())[:30]:
            if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                counts[key[:80]] = count
        summary["event_counts"] = counts
    return summary


def read_perps_worker_status(path: str | Path) -> dict[str, Any]:
    """Return a fixed allowlisted status view; discard unknown fields."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("schema_version") != 1
        ):
            raise ValueError("invalid Perps worker status schema")
        status = {
            "schema_version": 1,
            "enabled": raw.get("enabled") is True,
            "worker_state": raw.get("worker_state") if raw.get("worker_state") in {
                "starting", "healthy", "degraded", "stopped"
            } else "unknown",
            "last_heartbeat_at": raw.get("last_heartbeat_at"),
            "last_success_at": raw.get("last_success_at"),
            "last_error": str(raw.get("last_error"))[:240] if raw.get("last_error") else None,
            "consecutive_failures": _status_count(raw.get("consecutive_failures", 0), "consecutive_failures"),
            "markets_in_last_snapshot": _status_count(raw.get("markets_in_last_snapshot", 0), "markets_in_last_snapshot"),
            "snapshots_inserted_total": _status_count(raw.get("snapshots_inserted_total", 0), "snapshots_inserted_total"),
            "history_rows": _status_count(raw.get("history_rows", 0), "history_rows"),
            "poll_interval_seconds": _status_count(raw.get("poll_interval_seconds", 30), "poll_interval_seconds", minimum=1),
            "history_retention_days": _status_count(raw.get("history_retention_days", 14), "history_retention_days", minimum=1),
            "expired_rows_pruned_total": _status_count(raw.get("expired_rows_pruned_total", 0), "expired_rows_pruned_total"),
            "paper_engine_enabled": raw.get("paper_engine_enabled") is True,
            "paper_engine_state": raw.get("paper_engine_state") if raw.get("paper_engine_state") in {
                "disabled", "starting", "healthy", "halted", "degraded"
            } else "unknown",
            "paper_engine": _sanitize_paper_summary(raw.get("paper_engine")),
            "research_only": True,
            "live_execution_enabled": False,
            "execution": "none",
        }
        return status
    except FileNotFoundError:
        return {
            "schema_version": 1, "enabled": False, "worker_state": "unknown",
            "last_heartbeat_at": None, "last_success_at": None,
            "last_error": "status_file_missing", "consecutive_failures": 0,
            "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "paper_engine_enabled": False, "paper_engine_state": "unknown", "paper_engine": None,
            "research_only": True, "live_execution_enabled": False, "execution": "none",
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "schema_version": 1, "enabled": False, "worker_state": "unknown",
            "last_heartbeat_at": None, "last_success_at": None,
            "last_error": "status_file_invalid", "consecutive_failures": 0,
            "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
            "history_rows": 0, "poll_interval_seconds": None,
            "history_retention_days": None, "expired_rows_pruned_total": 0,
            "research_only": True, "live_execution_enabled": False, "execution": "none",
        }


def run_worker(
    *,
    provider: Any | None = None,
    store: Any | None = None,
    stop_event: Any | None = None,
    status_path: str | Path | None = None,
    poll_interval_seconds: int | None = None,
    history_retention_days: int | None = None,
    paper_engine: Any | None = None,
    paper_engine_enabled: bool | None = None,
    max_cycles: int | None = None,
) -> int:
    """Poll read-only Perps market data, archive it, and retry with bounded backoff."""
    stopping = stop_event or threading.Event()
    interval = poll_interval_seconds or _positive_int_env(
        "PERPS_POLL_INTERVAL_SECONDS", 30, 15, 300
    )
    if not 15 <= interval <= 300:
        raise ValueError("poll_interval_seconds must be between 15 and 300")
    retention_days = history_retention_days or _positive_int_env(
        "PERPS_HISTORY_RETENTION_DAYS", 14, 1, 365
    )
    if not 1 <= retention_days <= 365:
        raise ValueError("history_retention_days must be between 1 and 365")
    current_provider = provider or PerpsPublicMarketDataProvider(
        market_limit=_positive_int_env("PERPS_MARKET_LIMIT", 10, 1, 50),
        max_age_seconds=10.0,
        depth=10,
    )
    database = Path(os.getenv("PERPS_MARKET_HISTORY_DB", "data/perps_market_history.sqlite3"))
    current_store = store or PerpsMarketHistoryStore(database)
    status_file = Path(status_path or os.getenv(
        "PERPS_MARKET_WORKER_STATUS_FILE", "data/perps_market_worker_status.json"
    ))
    if paper_engine_enabled is None:
        paper_raw = str(os.getenv("ENABLE_PERPS_PAPER_ENGINE", "false")).strip().lower()
        if paper_raw not in {"true", "false"}:
            raise ValueError("ENABLE_PERPS_PAPER_ENGINE must be exactly true or false")
        engine_enabled = paper_raw == "true"
    else:
        engine_enabled = bool(paper_engine_enabled)
    current_engine = paper_engine
    engine_created = False
    if engine_enabled and current_engine is None:
        current_engine = PerpsPaperEngine(
            os.getenv("PERPS_PAPER_DB", str(Path(os.getenv("DATA_DIR", "data")) / "perps_paper.sqlite3")),
            initial_equity=os.getenv("PERPS_PAPER_INITIAL_EQUITY", "1000"),
            max_position_notional=os.getenv("PERPS_PAPER_MAX_POSITION_NOTIONAL", "25"),
            max_total_notional=os.getenv("PERPS_PAPER_MAX_TOTAL_NOTIONAL", "75"),
            max_open_positions=_positive_int_env("PERPS_PAPER_MAX_OPEN_POSITIONS", 3, 1, 10),
            max_leverage=os.getenv("PERPS_PAPER_MAX_LEVERAGE", "2"),
            risk_per_trade_pct=os.getenv("PERPS_PAPER_RISK_PER_TRADE_PCT", "0.0025"),
            stop_loss_pct=os.getenv("PERPS_PAPER_STOP_LOSS_PCT", "0.0075"),
            take_profit_pct=os.getenv("PERPS_PAPER_TAKE_PROFIT_PCT", "0.015"),
            taker_fee_rate=os.getenv("PERPS_PAPER_TAKER_FEE_RATE", "0.0005"),
            liquidation_fee_rate=os.getenv("PERPS_PAPER_LIQUIDATION_FEE_RATE", "0.01"),
            max_spread_bps=os.getenv("PERPS_PAPER_MAX_SPREAD_BPS", "20"),
            lookback_observations=_positive_int_env("PERPS_PAPER_LOOKBACK_OBSERVATIONS", 10, 3, 500),
            momentum_threshold_bps=os.getenv("PERPS_PAPER_MOMENTUM_THRESHOLD_BPS", "20"),
            minimum_history_span_seconds=_positive_int_env("PERPS_PAPER_MIN_HISTORY_SPAN_SECONDS", 270, 0, 86400),
            daily_drawdown_pct=os.getenv("PERPS_PAPER_DAILY_DRAWDOWN_PCT", "0.03"),
            maximum_drawdown_pct=os.getenv("PERPS_PAPER_MAXIMUM_DRAWDOWN_PCT", "0.10"),
            max_data_age_seconds=_positive_int_env("PERPS_PAPER_MAX_DATA_AGE_SECONDS", 15, 1, 60),
        )
        engine_created = True
    if current_engine is not None and not engine_enabled:
        raise ValueError("paper_engine was supplied while paper_engine_enabled is false")
    initial_paper_summary = current_engine.summary() if current_engine is not None else None
    state: dict[str, Any] = {
        "schema_version": 1, "enabled": True, "worker_state": "starting",
        "last_heartbeat_at": _utc_now(), "last_success_at": None,
        "last_error": None, "consecutive_failures": 0,
        "markets_in_last_snapshot": 0, "snapshots_inserted_total": 0,
        "history_rows": current_store.count(), "poll_interval_seconds": interval,
        "history_retention_days": retention_days, "expired_rows_pruned_total": 0,
        "paper_engine_enabled": engine_enabled,
        "paper_engine_state": (
            "halted" if initial_paper_summary and initial_paper_summary.get("halted")
            else "healthy" if current_engine is not None else "disabled"
        ),
        "paper_engine": initial_paper_summary,
        "research_only": True, "live_execution_enabled": False, "execution": "none",
    }
    cycles = 0
    last_prune_monotonic: float | None = None
    try:
        _write_status(status_file, state)
        while not stopping.is_set():
            state["last_heartbeat_at"] = _utc_now()
            try:
                snapshots = current_provider.load()
                if not snapshots:
                    raise RuntimeError("provider returned zero usable Perps snapshots")
                recorded = current_store.record_snapshots(snapshots)
                if int(recorded["inserted"]) < 0:
                    raise RuntimeError("history store returned an invalid insert count")
                if current_engine is not None:
                    for market_snapshot in snapshots:
                        current_engine.process_snapshot(market_snapshot, current_store)
                    paper_summary = current_engine.summary()
                    state["paper_engine"] = paper_summary
                    state["paper_engine_state"] = (
                        "halted" if paper_summary.get("halted") else "healthy"
                    )
                monotonic_now = time.monotonic()
                prune_before = getattr(current_store, "prune_before", None)
                if callable(prune_before) and (
                    last_prune_monotonic is None
                    or monotonic_now - last_prune_monotonic >= 21_600
                ):
                    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
                    pruned = prune_before(cutoff)
                    deleted = int(pruned.get("deleted", 0))
                    if deleted < 0:
                        raise RuntimeError("history retention returned an invalid delete count")
                    state["expired_rows_pruned_total"] += deleted
                    state["history_rows"] = int(pruned.get("remaining", current_store.count()))
                    last_prune_monotonic = monotonic_now
                state["worker_state"] = "healthy"
                state["last_success_at"] = _utc_now()
                state["last_error"] = None
                state["consecutive_failures"] = 0
                state["markets_in_last_snapshot"] = len(snapshots)
                state["snapshots_inserted_total"] += int(recorded["inserted"])
                state["history_rows"] = int(current_store.count())
                state["last_heartbeat_at"] = _utc_now()
                _write_status(status_file, state)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    return 0
                if stopping.wait(interval):
                    break
            except Exception as exc:
                failures = int(state["consecutive_failures"]) + 1
                delay = min(300.0, 2.0 * (2 ** min(failures - 1, 7)))
                state["worker_state"] = "degraded"
                state["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                state["consecutive_failures"] = failures
                state["last_heartbeat_at"] = _utc_now()
                if current_engine is not None:
                    state["paper_engine_state"] = "degraded"
                    try:
                        state["paper_engine"] = current_engine.summary()
                    except Exception:
                        state["paper_engine"] = None
                _write_status(status_file, state)
                if max_cycles is not None:
                    print(f"Perps worker cycle failed: {state['last_error']}", file=sys.stderr, flush=True)
                    return 1
                if stopping.wait(delay):
                    break
        state["worker_state"] = "stopped"
        state["last_heartbeat_at"] = _utc_now()
        _write_status(status_file, state)
        return 0
    finally:
        if engine_created and current_engine is not None:
            current_engine.close()
        if store is None:
            current_store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public Polymarket Perps market data")
    parser.add_argument("--once", action="store_true", help="collect one snapshot cycle and exit")
    args = parser.parse_args()
    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return run_worker(stop_event=stop_event, max_cycles=1 if args.once else None)


if __name__ == "__main__":
    raise SystemExit(main())
