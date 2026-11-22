"""Supervise the Polymarket Perps one-shot dead-man's auto-cancel switch.

Disabled by default. This component is intended to run only inside a future,
explicitly authorized live Perps account supervisor with an injected session.
It never places, replaces, or cancels an individual order.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_DEADLINE_SECONDS = 60
DEFAULT_REFRESH_SECONDS = 20
MIN_SERVER_REMAINING_SECONDS = 25


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def write_perps_kill_switch(path: str | Path, reason_code: str) -> None:
    if not isinstance(reason_code, str) or not reason_code.replace("_", "").isalnum():
        raise ValueError("reason_code must be a lowercase machine-readable code")
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    payload = {
        "mode": "PERPS_LIVE_KILL_SWITCH",
        "enabled": True,
        "reason_code": reason_code,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution": "blocked",
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class PerpsAutoCancelGuard:
    """Re-arm and verify the server-side 60-second Perps dead-man's switch."""

    def __init__(
        self,
        *,
        session: Any,
        enabled: bool = False,
        manually_armed: bool = False,
        kill_switch_path: str | Path | None = None,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        refresh_interval_seconds: int = DEFAULT_REFRESH_SECONDS,
        min_server_remaining_seconds: int = MIN_SERVER_REMAINING_SECONDS,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not callable(getattr(session, "arm_auto_cancel", None)):
            raise ValueError("session must expose arm_auto_cancel")
        if not callable(getattr(session, "fetch_auto_cancel_status", None)):
            raise ValueError("session must expose fetch_auto_cancel_status")
        for name, value, minimum, maximum in (
            ("deadline_seconds", deadline_seconds, 15, 300),
            ("refresh_interval_seconds", refresh_interval_seconds, 5, 120),
            ("min_server_remaining_seconds", min_server_remaining_seconds, 5, 120),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if refresh_interval_seconds >= deadline_seconds - min_server_remaining_seconds:
            raise ValueError("refresh interval leaves insufficient dead-man's-switch safety margin")
        if enabled and manually_armed and kill_switch_path is None:
            raise ValueError("an enabled Perps Auto-Cancel guard requires a kill-switch path")
        if not isinstance(enabled, bool) or not isinstance(manually_armed, bool):
            raise ValueError("enabled and manually_armed must be booleans")
        self.session = session
        self.enabled = enabled
        self.manually_armed = manually_armed
        self.kill_switch_path = Path(kill_switch_path).expanduser() if kill_switch_path is not None else None
        self.deadline_seconds = deadline_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self.min_server_remaining_seconds = min_server_remaining_seconds
        self.sleep = sleep

    def _blocked(self, reason: str, *, now: datetime) -> dict[str, Any]:
        if self.kill_switch_path is not None:
            write_perps_kill_switch(self.kill_switch_path, reason)
        return {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "blocked",
            "reason_code": reason,
            "checked_at": now.isoformat(),
            "server_deadline": None,
            "seconds_remaining": None,
            "new_order_permission": False,
            "execution": "none",
        }

    async def arm_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        if not self.enabled or not self.manually_armed:
            return {
                "mode": "PERPS_AUTO_CANCEL_GUARD",
                "state": "disabled",
                "reason_code": "guard_not_explicitly_enabled_and_armed",
                "checked_at": current.isoformat(),
                "server_deadline": None,
                "seconds_remaining": None,
                "new_order_permission": False,
                "execution": "none",
            }

        requested_deadline = current + timedelta(seconds=self.deadline_seconds)
        try:
            await self.session.arm_auto_cancel(cancel_at=requested_deadline)
            status = await self.session.fetch_auto_cancel_status()
        except Exception:
            return self._blocked("auto_cancel_arm_or_status_read_failed", now=current)

        server_deadline = _utc_datetime(_field(status, "deadline"))
        triggered = _field(status, "triggered")
        daily_limit = _field(status, "daily_limit")
        if server_deadline is None:
            return self._blocked("auto_cancel_server_deadline_invalid", now=current)
        remaining = (server_deadline - current).total_seconds()
        if remaining < self.min_server_remaining_seconds:
            return self._blocked("auto_cancel_server_deadline_too_close", now=current)
        if server_deadline < requested_deadline - timedelta(seconds=2):
            return self._blocked("auto_cancel_server_deadline_mismatch", now=current)
        if (
            isinstance(triggered, bool) or not isinstance(triggered, int)
            or isinstance(daily_limit, bool) or not isinstance(daily_limit, int)
            or daily_limit <= 0 or triggered >= daily_limit
        ):
            return self._blocked("auto_cancel_daily_trigger_limit_exhausted_or_unknown", now=current)

        return {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "armed",
            "reason_code": "server_deadman_switch_verified",
            "checked_at": current.isoformat(),
            "server_deadline": server_deadline.isoformat(),
            "seconds_remaining": round(remaining, 3),
            "daily_triggers_used": triggered,
            "daily_trigger_limit": daily_limit,
            "new_order_permission": False,
            "execution": "none",
        }

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> dict[str, Any]:
        """Keep the switch alive; on failure block new entries and stop refreshing."""
        if not self.enabled or not self.manually_armed:
            return await self.arm_once()
        last_report: dict[str, Any] | None = None
        while stop_event is None or not stop_event.is_set():
            last_report = await self.arm_once()
            if last_report["state"] != "armed":
                return last_report
            if stop_event is None:
                await self.sleep(self.refresh_interval_seconds)
            else:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.refresh_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    continue
                break
        # Do not disarm on shutdown. Let the verified server-side deadline
        # expire so remaining resting orders are cancelled automatically.
        return {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "stopped_with_server_deadman_armed",
            "reason_code": "guard_stopped_without_disarming_server_switch",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "last_verified_deadline": (last_report or {}).get("server_deadline"),
            "new_order_permission": False,
            "execution": "none",
        }
