"""Fail-closed, local event-market live kill-switch status reader.

The switch is read-only from the execution gateway. A missing, malformed,
symlinked, oversized, stale, or future-dated status file is treated as engaged.
This module never creates a disarmed state and never changes an order.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_MAX_BYTES = 4096
_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _blocked(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "verified": False,
        "state": "engaged",
        "engaged": True,
        "reason_code": reason_code,
        "execution": "none",
    }


def read_live_kill_switch(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 60.0,
) -> dict[str, Any]:
    """Read an operator-owned status file; every uncertainty means engaged."""
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not 1 <= float(max_age_seconds) <= 3600
    ):
        return _blocked("kill_switch_freshness_policy_invalid")
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        return _blocked("kill_switch_clock_invalid")
    current = current.astimezone(timezone.utc)

    candidate = Path(path).expanduser()
    descriptor: int | None = None
    try:
        if candidate.is_symlink():
            return _blocked("kill_switch_symlink_rejected")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(candidate, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return _blocked("kill_switch_not_regular_file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_BYTES:
            return _blocked("kill_switch_file_size_invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return _blocked("kill_switch_file_size_invalid")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_object(pairs),
        )
    except FileNotFoundError:
        return _blocked("kill_switch_file_missing")
    except Exception:
        return _blocked("kill_switch_status_unreadable_or_invalid")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "state", "updated_at_utc", "reason_code"
    }:
        return _blocked("kill_switch_schema_invalid")
    version = payload.get("schema_version")
    if isinstance(version, bool) or version != _SCHEMA_VERSION:
        return _blocked("kill_switch_schema_version_unsupported")
    state = payload.get("state")
    if state not in {"ENGAGED", "DISARMED"}:
        return _blocked("kill_switch_state_invalid")
    reason = payload.get("reason_code")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        return _blocked("kill_switch_reason_invalid")

    raw_timestamp = payload.get("updated_at_utc")
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        return _blocked("kill_switch_timestamp_missing")
    try:
        updated = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return _blocked("kill_switch_timestamp_invalid")
    if updated.tzinfo is None or updated.utcoffset() is None:
        return _blocked("kill_switch_timestamp_invalid")
    age = (current - updated.astimezone(timezone.utc)).total_seconds()
    if age < -2 or age > float(max_age_seconds):
        return _blocked("kill_switch_status_stale_or_future_dated")

    engaged = state == "ENGAGED"
    return {
        "schema_version": _SCHEMA_VERSION,
        "verified": True,
        "state": state.lower(),
        "engaged": engaged,
        "reason_code": reason,
        "updated_at_utc": updated.astimezone(timezone.utc).isoformat(),
        "age_seconds": max(0.0, age),
        "execution": "none",
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate kill-switch field")
        result[key] = value
    return result


def check_live_kill_switch() -> dict[str, Any]:
    """Read the configured status file. No default disarmed state exists."""
    default_path = Path(os.getenv("DATA_DIR", "data")) / "event_live_kill_switch.json"
    configured = os.getenv("EVENT_LIVE_KILL_SWITCH_PATH")
    return read_live_kill_switch(configured or default_path)
