"""Supervise the Polymarket Perps one-shot dead-man's auto-cancel switch.

Disabled by default. This component is intended to run only inside a future,
explicitly authorized live Perps account supervisor with an injected session.
It never places or replaces orders. Graceful shutdown requires a verified flat
portfolio, cancellation of every resting order, and server-confirmed disarming.
Unexpected process loss still leaves the server-side deadline armed.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


DEFAULT_DEADLINE_SECONDS = 60
DEFAULT_REFRESH_SECONDS = 20
MIN_SERVER_REMAINING_SECONDS = 25
DEFAULT_SHUTDOWN_CANCEL_TIMEOUT_SECONDS = 15.0
DEFAULT_SHUTDOWN_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_ACCOUNT_READ_TIMEOUT_SECONDS = 5.0


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _finite_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


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
    """Re-arm and verify Perps dead-man's switch; disable only after safe cleanup."""

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
        shutdown_cancel_timeout_seconds: float = DEFAULT_SHUTDOWN_CANCEL_TIMEOUT_SECONDS,
        shutdown_poll_interval_seconds: float = DEFAULT_SHUTDOWN_POLL_INTERVAL_SECONDS,
        account_read_timeout_seconds: float = DEFAULT_ACCOUNT_READ_TIMEOUT_SECONDS,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not callable(getattr(session, "arm_auto_cancel", None)):
            raise ValueError("session must expose arm_auto_cancel")
        if not callable(getattr(session, "fetch_auto_cancel_status", None)):
            raise ValueError("session must expose fetch_auto_cancel_status")
        if not isinstance(enabled, bool) or not isinstance(manually_armed, bool):
            raise ValueError("enabled and manually_armed must be booleans")
        if enabled and manually_armed:
            required_shutdown_apis = (
                "fetch_portfolio",
                "cancel_all_orders",
                "fetch_open_orders",
                "disarm_auto_cancel",
            )
            missing = [
                name for name in required_shutdown_apis
                if not callable(getattr(session, name, None))
            ]
            if missing:
                raise ValueError(
                    "enabled Perps Auto-Cancel guard is missing graceful-shutdown APIs: "
                    + ",".join(missing)
                )
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
        for name, value, minimum, maximum in (
            ("shutdown_cancel_timeout_seconds", shutdown_cancel_timeout_seconds, 0.05, 120.0),
            ("shutdown_poll_interval_seconds", shutdown_poll_interval_seconds, 0.01, 5.0),
            ("account_read_timeout_seconds", account_read_timeout_seconds, 0.05, 60.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < float(value) <= maximum
                or float(value) < minimum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        self.session = session
        self.enabled = enabled
        self.manually_armed = manually_armed
        self.kill_switch_path = Path(kill_switch_path).expanduser() if kill_switch_path is not None else None
        self.deadline_seconds = deadline_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self.min_server_remaining_seconds = min_server_remaining_seconds
        self.shutdown_cancel_timeout_seconds = float(shutdown_cancel_timeout_seconds)
        self.shutdown_poll_interval_seconds = float(shutdown_poll_interval_seconds)
        self.account_read_timeout_seconds = float(account_read_timeout_seconds)
        self.sleep = sleep

    def _blocked(
        self,
        reason: str,
        *,
        now: datetime,
        last_verified_deadline: str | None = None,
    ) -> dict[str, Any]:
        if self.kill_switch_path is not None:
            write_perps_kill_switch(self.kill_switch_path, reason)
        return {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "blocked",
            "reason_code": reason,
            "checked_at": now.isoformat(),
            "last_verified_deadline": last_verified_deadline,
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
            await asyncio.wait_for(
                self.session.arm_auto_cancel(cancel_at=requested_deadline),
                timeout=self.account_read_timeout_seconds,
            )
            status = await asyncio.wait_for(
                self.session.fetch_auto_cancel_status(),
                timeout=self.account_read_timeout_seconds,
            )
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

    async def _graceful_shutdown(
        self,
        *,
        last_report: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cancel resting orders only after proving positions are flat; then disarm.

        If portfolio state is unknown/non-flat, cancellation cannot be verified,
        or disarming cannot be confirmed, leave the server dead-man's switch armed.
        This preserves the server-side expiry as the fallback on an unsafe exit.
        """
        current = now or datetime.now(timezone.utc)
        current = current.astimezone(timezone.utc)
        last_deadline = last_report.get("server_deadline")
        try:
            portfolio = await asyncio.wait_for(
                self.session.fetch_portfolio(),
                timeout=self.account_read_timeout_seconds,
            )
        except Exception:
            return self._blocked(
                "shutdown_portfolio_read_failed_deadman_kept_armed",
                now=current,
                last_verified_deadline=last_deadline,
            )

        positions = _field(portfolio, "positions")
        if not isinstance(positions, (tuple, list)):
            return self._blocked(
                "shutdown_position_state_unknown_deadman_kept_armed",
                now=current,
                last_verified_deadline=last_deadline,
            )
        for position in positions:
            try:
                size = _finite_decimal(_field(position, "size"), "position.size")
            except ValueError:
                return self._blocked(
                    "shutdown_position_state_invalid_deadman_kept_armed",
                    now=current,
                    last_verified_deadline=last_deadline,
                )
            if size != 0:
                return self._blocked(
                    "shutdown_open_positions_remain_deadman_kept_armed",
                    now=current,
                    last_verified_deadline=last_deadline,
                )

        try:
            # A successful cancel-all acknowledgement is not proof every order
            # has disappeared; the subsequent open-order reads establish that.
            await asyncio.wait_for(
                self.session.cancel_all_orders(),
                timeout=self.account_read_timeout_seconds,
            )
        except Exception:
            return self._blocked(
                "shutdown_cancel_all_failed_deadman_kept_armed",
                now=current,
                last_verified_deadline=last_deadline,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_cancel_timeout_seconds
        while True:
            try:
                open_orders = await asyncio.wait_for(
                    self.session.fetch_open_orders(),
                    timeout=self.account_read_timeout_seconds,
                )
            except Exception:
                return self._blocked(
                    "shutdown_open_order_verification_failed_deadman_kept_armed",
                    now=current,
                    last_verified_deadline=last_deadline,
                )
            if not isinstance(open_orders, (tuple, list)):
                return self._blocked(
                    "shutdown_open_order_state_invalid_deadman_kept_armed",
                    now=current,
                    last_verified_deadline=last_deadline,
                )
            if len(open_orders) == 0:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._blocked(
                    "shutdown_open_orders_remain_deadman_kept_armed",
                    now=current,
                    last_verified_deadline=last_deadline,
                )
            await self.sleep(min(self.shutdown_poll_interval_seconds, remaining))

        try:
            await asyncio.wait_for(
                self.session.disarm_auto_cancel(),
                timeout=self.account_read_timeout_seconds,
            )
            status = await asyncio.wait_for(
                self.session.fetch_auto_cancel_status(),
                timeout=self.account_read_timeout_seconds,
            )
        except Exception:
            return self._blocked(
                "shutdown_auto_cancel_disarm_or_verification_failed",
                now=current,
                last_verified_deadline=last_deadline,
            )

        cleared_deadline = _field(status, "deadline")
        deadline_cleared = (
            cleared_deadline is None
            or (
                isinstance(cleared_deadline, int)
                and not isinstance(cleared_deadline, bool)
                and cleared_deadline == 0
            )
        )
        if not deadline_cleared:
            return self._blocked(
                "shutdown_auto_cancel_still_armed_after_disarm",
                now=current,
                last_verified_deadline=last_deadline,
            )
        return {
            "mode": "PERPS_AUTO_CANCEL_GUARD",
            "state": "stopped_after_orders_cleared_and_auto_cancel_disarmed",
            "reason_code": "flat_portfolio_open_orders_cleared_and_disarm_verified",
            "checked_at": current.isoformat(),
            "last_verified_deadline": last_deadline,
            "open_positions_remaining": 0,
            "open_orders_remaining": 0,
            "server_auto_cancel_disarmed": True,
            "new_order_permission": False,
            "execution": "none",
        }

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> dict[str, Any]:
        """Maintain the switch; use a verified cancel-and-disarm sequence on clean shutdown."""
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
        if last_report is None:
            return self._blocked(
                "shutdown_before_deadman_armed",
                now=datetime.now(timezone.utc),
            )
        return await self._graceful_shutdown(last_report=last_report)
