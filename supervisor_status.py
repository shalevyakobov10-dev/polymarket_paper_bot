"""Sanitized, atomic process-supervisor status for read-only operations monitoring.

The status file intentionally contains no environment values, keys, wallet
addresses, command arguments, or child process IDs.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVICE_STATES = frozenset({
    "starting", "running", "app_failed", "startup_failed", "stopped",
})
MONITOR_STATES = frozenset({
    "disabled", "starting", "running", "restarting", "stopped",
})
STATUS_SCHEMA_VERSION = 1


def build_supervisor_status(
    *,
    service_state: str,
    monitor_enabled: bool,
    monitor_state: str,
    restart_count: int = 0,
    last_exit_code: int | None = None,
    next_restart_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    if service_state not in SERVICE_STATES:
        raise ValueError("invalid supervisor service_state")
    if not isinstance(monitor_enabled, bool):
        raise ValueError("monitor_enabled must be boolean")
    if monitor_state not in MONITOR_STATES:
        raise ValueError("invalid supervisor monitor_state")
    if not monitor_enabled and monitor_state != "disabled":
        raise ValueError("disabled monitor must use disabled monitor_state")
    if monitor_enabled and monitor_state == "disabled":
        raise ValueError("enabled monitor cannot use disabled monitor_state")
    if isinstance(restart_count, bool) or not isinstance(restart_count, int) or restart_count < 0:
        raise ValueError("restart_count must be a nonnegative integer")
    if last_exit_code is not None and (
        isinstance(last_exit_code, bool) or not isinstance(last_exit_code, int)
    ):
        raise ValueError("last_exit_code must be an integer or null")

    timestamp = updated_at or datetime.now(timezone.utc).isoformat()
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("updated_at must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        raise ValueError("updated_at must be a valid ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("updated_at must be timezone-aware")
    if next_restart_at is not None:
        if not isinstance(next_restart_at, str) or not next_restart_at.strip():
            raise ValueError("next_restart_at must be a non-empty ISO timestamp or null")
        try:
            restart_time = datetime.fromisoformat(next_restart_at)
        except ValueError:
            raise ValueError("next_restart_at must be a valid ISO timestamp") from None
        if restart_time.tzinfo is None or restart_time.utcoffset() is None:
            raise ValueError("next_restart_at must be timezone-aware")
    if monitor_state != "restarting" and next_restart_at is not None:
        raise ValueError("next_restart_at is valid only while monitor is restarting")

    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "supervised": True,
        "service_state": service_state,
        "monitor_enabled": monitor_enabled,
        "monitor_state": monitor_state,
        "monitor_restart_count": restart_count,
        "monitor_last_exit_code": last_exit_code,
        "monitor_next_restart_at": next_restart_at,
        "updated_at": parsed.astimezone(timezone.utc).isoformat(),
        "execution": "none",
    }


def write_supervisor_status(path: str | Path, payload: dict[str, Any]) -> None:
    """Atomically replace a status document after validating its fixed schema."""
    if not isinstance(payload, dict):
        raise ValueError("supervisor status payload must be a dictionary")
    normalized = build_supervisor_status(
        service_state=payload.get("service_state"),
        monitor_enabled=payload.get("monitor_enabled"),
        monitor_state=payload.get("monitor_state"),
        restart_count=payload.get("monitor_restart_count", 0),
        last_exit_code=payload.get("monitor_last_exit_code"),
        next_restart_at=payload.get("monitor_next_restart_at"),
        updated_at=payload.get("updated_at"),
    )
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def read_supervisor_status(path: str | Path) -> dict[str, Any]:
    """Return an allowlisted status view; never reflect arbitrary file keys."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != STATUS_SCHEMA_VERSION:
            raise ValueError("unsupported supervisor status schema")
        return build_supervisor_status(
            service_state=raw.get("service_state"),
            monitor_enabled=raw.get("monitor_enabled"),
            monitor_state=raw.get("monitor_state"),
            restart_count=raw.get("monitor_restart_count", 0),
            last_exit_code=raw.get("monitor_last_exit_code"),
            next_restart_at=raw.get("monitor_next_restart_at"),
            updated_at=raw.get("updated_at"),
        )
    except FileNotFoundError:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "supervised": False,
            "service_state": "unknown",
            "monitor_enabled": False,
            "monitor_state": "unknown",
            "monitor_restart_count": 0,
            "monitor_last_exit_code": None,
            "monitor_next_restart_at": None,
            "updated_at": None,
            "execution": "none",
            "status_error": "status_file_missing",
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "supervised": False,
            "service_state": "unknown",
            "monitor_enabled": False,
            "monitor_state": "unknown",
            "monitor_restart_count": 0,
            "monitor_last_exit_code": None,
            "monitor_next_restart_at": None,
            "updated_at": None,
            "execution": "none",
            "status_error": "status_file_invalid",
        }
