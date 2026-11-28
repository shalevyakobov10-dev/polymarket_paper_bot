"""Supervised loop around an injected paper cycle.

The worker records an allowlisted heartbeat only. It has no order client and
cannot enable live execution.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "worker_state": status["worker_state"],
        "cycles": int(status["cycles"]),
        "total_failures": int(status["total_failures"]),
        "consecutive_failures": int(status["consecutive_failures"]),
        "markets_loaded_last_cycle": int(status["markets_loaded_last_cycle"]),
        "opened_last_cycle": int(status["opened_last_cycle"]),
        "closed_last_cycle": int(status["closed_last_cycle"]),
        "close_blocked_last_cycle": int(status["close_blocked_last_cycle"]),
        "last_error_type": status["last_error_type"],
        "updated_at": status["updated_at"],
        "execution": "none",
        "research_only": True,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def read_event_paper_worker_status(path: str | Path) -> dict[str, Any]:
    """Return an allowlisted heartbeat. Missing or foreign keys are not forwarded."""
    status_path = Path(path)
    empty = {
        "worker_state": "unknown",
        "heartbeat_fresh": False,
        "status_error": "status_file_missing",
        "cycles": 0,
        "total_failures": 0,
        "consecutive_failures": 0,
        "markets_loaded_last_cycle": 0,
        "opened_last_cycle": 0,
        "closed_last_cycle": 0,
        "close_blocked_last_cycle": 0,
        "last_error_type": None,
        "execution": "none",
        "research_only": True,
    }
    if not status_path.exists():
        return empty
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        empty["status_error"] = "status_file_unreadable"
        return empty
    if not isinstance(payload, dict):
        empty["status_error"] = "status_file_unreadable"
        return empty
    updated = payload.get("updated_at")
    fresh = False
    if isinstance(updated, str):
        try:
            stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            fresh = (_utc_now() - stamp.astimezone(timezone.utc)).total_seconds() <= 180
        except ValueError:
            fresh = False
    def count(name: str) -> int:
        value = payload.get(name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    error_type = payload.get("last_error_type")
    return {
        "worker_state": str(payload.get("worker_state") or "unknown"),
        "heartbeat_fresh": fresh,
        "cycles": count("cycles"),
        "total_failures": count("total_failures"),
        "consecutive_failures": count("consecutive_failures"),
        "markets_loaded_last_cycle": count("markets_loaded_last_cycle"),
        "opened_last_cycle": count("opened_last_cycle"),
        "closed_last_cycle": count("closed_last_cycle"),
        "close_blocked_last_cycle": count("close_blocked_last_cycle"),
        "last_error_type": error_type if isinstance(error_type, str) and error_type else None,
        "updated_at": updated if isinstance(updated, str) else None,
        "execution": "none",
        "research_only": True,
    }


def _count_list(result: dict[str, Any], name: str) -> int:
    value = result.get(name) or []
    return len(value) if isinstance(value, list) else 0


def run_worker(
    *,
    bot_factory: Callable[[], Any],
    stop_event: Any,
    status_path: str | Path,
    poll_seconds: int,
    max_consecutive_failures: int = 5,
) -> int:
    """Run paper cycles until stop or the consecutive-failure limit."""
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int) or not 30 <= poll_seconds <= 86400:
        raise ValueError("poll_seconds must be an integer between 30 and 86400")
    if isinstance(max_consecutive_failures, bool) or not isinstance(max_consecutive_failures, int) or max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be a positive integer")
    path = Path(status_path)
    bot = bot_factory()
    cycles = 0
    total_failures = 0
    consecutive_failures = 0
    markets_loaded = 0
    opened = 0
    closed = 0
    close_blocked = 0
    last_error_type = None

    def publish(state: str) -> None:
        _write_status(path, {
            "worker_state": state,
            "cycles": cycles,
            "total_failures": total_failures,
            "consecutive_failures": consecutive_failures,
            "markets_loaded_last_cycle": markets_loaded,
            "opened_last_cycle": opened,
            "closed_last_cycle": closed,
            "close_blocked_last_cycle": close_blocked,
            "last_error_type": last_error_type,
            "updated_at": _utc_now().isoformat(),
        })

    try:
        while not stop_event.is_set():
            try:
                result = bot.run_once()
            except Exception as exc:
                total_failures += 1
                consecutive_failures += 1
                last_error_type = type(exc).__name__
                if consecutive_failures >= max_consecutive_failures:
                    publish("failed")
                    return 1
                publish("degraded")
                stop_event.wait(poll_seconds)
                continue
            if not isinstance(result, dict):
                total_failures += 1
                consecutive_failures += 1
                last_error_type = "invalid_cycle_result"
                if consecutive_failures >= max_consecutive_failures:
                    publish("failed")
                    return 1
                publish("degraded")
                stop_event.wait(poll_seconds)
                continue
            cycles += 1
            consecutive_failures = 0
            last_error_type = None
            loaded = result.get("markets_loaded", 0)
            markets_loaded = loaded if isinstance(loaded, int) and not isinstance(loaded, bool) else 0
            opened = _count_list(result, "opened")
            closed = _count_list(result, "closed")
            close_blocked = _count_list(result, "close_blocked")
            publish("running")
            stop_event.wait(poll_seconds)
        publish("stopped")
        return 0
    finally:
        bot.close()
