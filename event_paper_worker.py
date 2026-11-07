"""Supervised continuous runner for the event-aware paper research engine.

This process reads public market/news data and records paper-only decisions. It
has no wallet credentials, signing, order-submission, deposit, or withdrawal code.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from offline_news_paper_bot import OfflineNewsPaperBot

STATUS_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _poll_seconds(value: Any = None) -> int:
    raw = value if value is not None else os.getenv("PAPER_POLL_SECONDS", "300")
    try:
        parsed = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("PAPER_POLL_SECONDS must be an integer") from None
    if not 30 <= parsed <= 86_400:
        raise ValueError("PAPER_POLL_SECONDS must be between 30 and 86400")
    return parsed


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = handle.name
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def read_event_paper_worker_status(
    path: str | Path,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 900.0,
) -> dict[str, Any]:
    """Read a strictly allowlisted status and report heartbeat freshness."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != STATUS_SCHEMA_VERSION:
            raise ValueError("unsupported event paper worker status schema")
        state = raw.get("worker_state")
        if state not in {"starting", "running", "degraded", "failed", "stopped"}:
            raise ValueError("invalid event paper worker state")
        updated = datetime.fromisoformat(str(raw.get("updated_at") or ""))
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise ValueError("worker heartbeat must be timezone-aware")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        age = (current.astimezone(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
        allowed = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": state,
            "updated_at": updated.astimezone(timezone.utc).isoformat(),
            "started_at": raw.get("started_at"),
            "last_cycle_at": raw.get("last_cycle_at"),
            "last_success_at": raw.get("last_success_at"),
            "cycles": max(0, int(raw.get("cycles", 0))),
            "consecutive_failures": max(0, int(raw.get("consecutive_failures", 0))),
            "total_failures": max(0, int(raw.get("total_failures", 0))),
            "retry_delay_seconds": max(0.0, float(raw.get("retry_delay_seconds", 0.0))),
            "markets_loaded_last_cycle": max(0, int(raw.get("markets_loaded_last_cycle", 0))),
            "opened_last_cycle": max(0, int(raw.get("opened_last_cycle", 0))),
            "closed_last_cycle": max(0, int(raw.get("closed_last_cycle", 0))),
            "close_blocked_last_cycle": max(0, int(raw.get("close_blocked_last_cycle", 0))),
            "last_error_type": str(raw.get("last_error_type") or "")[:80] or None,
            "mode": "OFFLINE_NEWS_PAPER_ONLY",
            "research_only": True,
            "execution": "none",
        }
        allowed["heartbeat_age_seconds"] = round(max(0.0, age), 1) if age >= -5 else None
        allowed["heartbeat_fresh"] = -5 <= age <= stale_after_seconds
        return allowed
    except FileNotFoundError:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": "unknown",
            "heartbeat_age_seconds": None,
            "heartbeat_fresh": False,
            "execution": "none",
            "status_error": "status_file_missing",
        }
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "worker_state": "unknown",
            "heartbeat_age_seconds": None,
            "heartbeat_fresh": False,
            "execution": "none",
            "status_error": "status_file_invalid",
        }


def run_worker(
    *,
    bot_factory: Callable[[], Any] = OfflineNewsPaperBot,
    stop_event: Any | None = None,
    status_path: str | Path | None = None,
    poll_seconds: int | None = None,
    max_consecutive_failures: int = 5,
) -> int:
    """Run cycles continuously; retry transient faults and exit after bounded failures."""
    if isinstance(max_consecutive_failures, bool) or not isinstance(max_consecutive_failures, int) or max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be a positive integer")
    interval = _poll_seconds(poll_seconds)
    stopping = stop_event or threading.Event()
    target = Path(status_path or os.getenv(
        "EVENT_PAPER_WORKER_STATUS_FILE", "./data/event_paper_worker_status.json"
    )).expanduser()
    started = _utc_now()
    status: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "worker_state": "starting",
        "started_at": started,
        "updated_at": started,
        "last_cycle_at": None,
        "last_success_at": None,
        "cycles": 0,
        "consecutive_failures": 0,
        "total_failures": 0,
        "retry_delay_seconds": 0.0,
        "markets_loaded_last_cycle": 0,
        "opened_last_cycle": 0,
        "closed_last_cycle": 0,
        "close_blocked_last_cycle": 0,
        "last_error_type": None,
        "mode": "OFFLINE_NEWS_PAPER_ONLY",
        "research_only": True,
        "execution": "none",
    }
    _write_status(target, status)
    bot = None
    exit_code = 0
    try:
        bot = bot_factory()
        while not stopping.is_set():
            cycle_started = _utc_now()
            try:
                result = bot.run_once()
                status.update({
                    "worker_state": "running",
                    "last_cycle_at": cycle_started,
                    "last_success_at": _utc_now(),
                    "cycles": status["cycles"] + 1,
                    "consecutive_failures": 0,
                    "retry_delay_seconds": 0.0,
                    "markets_loaded_last_cycle": max(0, int(result.get("markets_loaded", 0))),
                    "opened_last_cycle": len(result.get("opened") or []),
                    "closed_last_cycle": len(result.get("closed") or []),
                    "close_blocked_last_cycle": len(result.get("close_blocked") or []),
                    "last_error_type": None,
                })
                _write_status(target, {**status, "updated_at": _utc_now()})
                status["updated_at"] = _utc_now()
                if stopping.wait(interval):
                    break
            except Exception as exc:
                failures = int(status["consecutive_failures"]) + 1
                total = int(status["total_failures"]) + 1
                delay = min(300.0, 5.0 * (2 ** min(failures - 1, 6)))
                status.update({
                    "worker_state": "degraded" if failures < max_consecutive_failures else "failed",
                    "last_cycle_at": cycle_started,
                    "consecutive_failures": failures,
                    "total_failures": total,
                    "retry_delay_seconds": delay,
                    # Store only the exception type, never URLs, response bodies, or secrets.
                    "last_error_type": type(exc).__name__[:80],
                    "updated_at": _utc_now(),
                })
                _write_status(target, status)
                if failures >= max_consecutive_failures:
                    exit_code = 1
                    break
                if stopping.wait(delay):
                    break
    except Exception as exc:
        exit_code = 1
        status.update({
            "worker_state": "failed",
            "last_error_type": type(exc).__name__[:80],
            "updated_at": _utc_now(),
        })
        _write_status(target, status)
    finally:
        if bot is not None:
            try:
                bot.close()
            except Exception:
                if exit_code == 0:
                    exit_code = 1
        if status["worker_state"] not in {"failed"}:
            status["worker_state"] = "stopped"
        status["updated_at"] = _utc_now()
        status["retry_delay_seconds"] = 0.0
        _write_status(target, status)
    return exit_code


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    stopping = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return run_worker(stop_event=stopping)


if __name__ == "__main__":
    raise SystemExit(main())
