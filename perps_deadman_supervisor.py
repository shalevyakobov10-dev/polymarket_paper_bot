"""Perps dead-man's-switch supervisor for a future, separately released gateway.

This component only accepts an already-open SDK session by dependency
injection. It never creates credentials or a client. Its run loop is hard-gated
by perps_live_release_gate.py; the current source-controlled release gates are
closed, so it cannot arm or modify an account in this build.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from perps_live_release_gate import evaluate_perps_live_release_gate


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


class PerpsDeadmanSwitchSupervisor:
    """Maintain and verify an account auto-cancel lease while a live gateway runs.

    The SDK's auto-cancel action is a signed account modification. The loop
    therefore checks the source-controlled release gate before its first SDK
    call. It leaves the last armed deadline in place when stopping or failing;
    it never disarms the switch automatically.
    """

    def __init__(
        self,
        *,
        session: Any,
        stop_event: asyncio.Event,
        halt_event: asyncio.Event,
        status_path: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        lease_seconds: int = 60,
        renew_interval_seconds: int = 20,
        command_timeout_seconds: float = 5.0,
        minimum_remaining_seconds: int = 10,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(session, "arm_auto_cancel", None)):
            raise ValueError("session is missing arm_auto_cancel")
        if not callable(getattr(session, "fetch_auto_cancel_status", None)):
            raise ValueError("session is missing fetch_auto_cancel_status")
        if not isinstance(stop_event, asyncio.Event) or not isinstance(halt_event, asyncio.Event):
            raise ValueError("stop_event and halt_event must be asyncio.Event instances")
        for name, value, low, high in (
            ("lease_seconds", lease_seconds, 15, 300),
            ("renew_interval_seconds", renew_interval_seconds, 5, 120),
            ("minimum_remaining_seconds", minimum_remaining_seconds, 5, 60),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if renew_interval_seconds >= lease_seconds:
            raise ValueError("renew_interval_seconds must be shorter than the lease")
        if (
            isinstance(command_timeout_seconds, bool)
            or not isinstance(command_timeout_seconds, (int, float))
            or not math.isfinite(float(command_timeout_seconds))
            or not 0.05 <= float(command_timeout_seconds) <= 30.0
        ):
            raise ValueError("command_timeout_seconds must be between 0.05 and 30")
        if minimum_remaining_seconds >= lease_seconds:
            raise ValueError("minimum_remaining_seconds must be shorter than the lease")

        self.session = session
        self.stop_event = stop_event
        self.halt_event = halt_event
        self.status_path = Path(status_path) if status_path is not None else None
        self.environment = environment
        self.lease_seconds = lease_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.minimum_remaining_seconds = minimum_remaining_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_verified_deadline: datetime | None = None

    def _now(self) -> datetime:
        now = self.now_fn()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def _write_status(self, payload: dict[str, Any]) -> None:
        if self.status_path is None:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_name(self.status_path.name + ".tmp")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.status_path)

    def _status(
        self,
        *,
        state: str,
        reason_code: str | None = None,
        deadline: datetime | None = None,
        triggered: int | None = None,
        daily_limit: int | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        remaining = (
            (deadline.astimezone(timezone.utc) - now).total_seconds()
            if deadline is not None else None
        )
        return {
            "schema_version": 1,
            "component": "perps_deadman_switch_supervisor",
            "state": state,
            "reason_code": reason_code,
            "checked_at": now.isoformat(),
            "last_verified_deadline": (
                deadline.astimezone(timezone.utc).isoformat()
                if deadline is not None else None
            ),
            "seconds_remaining": round(remaining, 3) if remaining is not None else None,
            "triggered_today": triggered,
            "daily_trigger_limit": daily_limit,
            "halt_event_set": self.halt_event.is_set(),
            "disarm_on_shutdown": False,
            "order_submission_allowed": False,
            "execution": "none",
        }

    async def _arm_and_verify_once(self) -> dict[str, Any]:
        now = self._now()
        requested_deadline = now + timedelta(seconds=self.lease_seconds)
        await asyncio.wait_for(
            self.session.arm_auto_cancel(cancel_at=requested_deadline),
            timeout=self.command_timeout_seconds,
        )
        status = await asyncio.wait_for(
            self.session.fetch_auto_cancel_status(),
            timeout=self.command_timeout_seconds,
        )
        deadline = _field(status, "deadline")
        triggered = _field(status, "triggered")
        daily_limit = _field(status, "daily_limit")
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("auto_cancel_deadline_invalid")
        deadline = deadline.astimezone(timezone.utc)
        remaining = (deadline - self._now()).total_seconds()
        if remaining < self.minimum_remaining_seconds:
            raise ValueError("auto_cancel_deadline_too_close")
        if (
            isinstance(triggered, bool) or not isinstance(triggered, int) or triggered < 0
            or isinstance(daily_limit, bool) or not isinstance(daily_limit, int)
            or daily_limit <= 0 or triggered >= daily_limit
        ):
            raise ValueError("auto_cancel_daily_limit_exhausted_or_invalid")
        self.last_verified_deadline = deadline
        return self._status(
            state="healthy",
            deadline=deadline,
            triggered=triggered,
            daily_limit=daily_limit,
        )

    async def run(self) -> dict[str, Any]:
        gate = evaluate_perps_live_release_gate(self.environment)
        if gate.get("eligible_for_separate_gateway_review") is not True:
            self.halt_event.set()
            report = self._status(
                state="blocked",
                reason_code="perps_live_release_gate_closed",
            )
            report["release_gate_blocked_reasons"] = gate.get("blocked_reasons", [])
            self._write_status(report)
            return report

        if self.stop_event.is_set():
            report = self._status(
                state="stopped",
                reason_code="stopped_before_first_deadman_arm",
            )
            self.halt_event.set()
            self._write_status(report)
            return report

        while True:
            try:
                report = await self._arm_and_verify_once()
                self._write_status(report)
            except asyncio.CancelledError:
                self.halt_event.set()
                report = self._status(
                    state="stopped_deadman_left_armed",
                    reason_code="supervisor_task_cancelled",
                    deadline=self.last_verified_deadline,
                )
                self._write_status(report)
                raise
            except Exception as error:
                self.halt_event.set()
                report = self._status(
                    state="degraded_deadman_left_armed",
                    reason_code=(
                        str(error) if isinstance(error, ValueError)
                        and str(error).startswith("auto_cancel_")
                        else "deadman_refresh_failed"
                    ),
                    deadline=self.last_verified_deadline,
                )
                self._write_status(report)
                return report

            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.renew_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

            # Do not disarm during shutdown. The last verified auto-cancel
            # deadline remains active and cancels resting orders if the process
            # does not restart and obtain a fresh, verified lease.
            report = self._status(
                state="stopped_deadman_left_armed",
                reason_code="shutdown_requested",
                deadline=self.last_verified_deadline,
            )
            self.halt_event.set()
            self._write_status(report)
            return report
