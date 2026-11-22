"""Continuous, read-only Perps account supervision around account preflight.

The supervisor recovers crash-interrupted local submissions into an ambiguous
no-retry state, then periodically runs the injected read-only account preflight.
It never creates an SDK session, loads credentials, places/cancels orders, arms
Auto-Cancel, changes leverage/margin, or authorizes execution.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_CYCLE_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10
DEFAULT_ERROR_BACKOFF_CAP_SECONDS = 300.0

_COUNT_FIELDS = frozenset({
    "position_count",
    "open_order_count",
    "local_order_count",
    "unresolved_order_count",
    "cross_margin_positions",
    "over_leveraged_positions",
    "position_integrity_errors",
    "high_adl_risk_positions",
    "unknown_remote_open_orders",
    "missing_remote_open_orders",
})
_BOOLEAN_FIELDS = frozenset({
    "portfolio_fresh",
    "auto_cancel_armed",
    "fee_schedule_fresh",
})
_DECIMAL_FIELDS = frozenset({
    "margin_available",
    "auto_cancel_seconds_remaining",
    "account_maintenance_margin_ratio",
    "effective_taker_fee_rate",
    "effective_maker_fee_rate",
})
_REASON_RE = re.compile(r"^[a-z0-9_]{1,80}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonnegative_int(value: Any, field: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _finite_decimal_text(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return str(parsed)


def _sanitize_preflight_report(report: Any) -> dict[str, Any]:
    """Accept only the documented preflight contract and safe scalar telemetry."""
    if not isinstance(report, dict) or report.get("mode") != "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT":
        raise ValueError("invalid_read_only_preflight_report")
    if (
        report.get("order_placement_authorized") is not False
        or report.get("live_execution_enabled") is not False
        or report.get("execution") != "none"
    ):
        raise ValueError("preflight_report_contains_execution_permission")
    passed = report.get("read_only_preflight_passed")
    if not isinstance(passed, bool):
        raise ValueError("invalid_read_only_preflight_result")
    raw_reasons = report.get("blocked_reasons")
    if not isinstance(raw_reasons, list) or len(raw_reasons) > 50:
        raise ValueError("invalid_read_only_preflight_reasons")
    reasons: list[str] = []
    for reason in raw_reasons:
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise ValueError("invalid_read_only_preflight_reason_code")
        if reason not in reasons:
            reasons.append(reason)
    observations = report.get("observations")
    if not isinstance(observations, dict):
        raise ValueError("invalid_read_only_preflight_observations")

    safe_observations: dict[str, Any] = {}
    for name in _COUNT_FIELDS:
        value = observations.get(name, 0)
        safe_observations[name] = _nonnegative_int(value, name)
    for name in _BOOLEAN_FIELDS:
        value = observations.get(name, False)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be boolean")
        safe_observations[name] = value
    for name in _DECIMAL_FIELDS:
        value = observations.get(name)
        safe_observations[name] = (
            _finite_decimal_text(value, name) if value is not None else None
        )
    fee_tier = observations.get("account_fee_tier")
    if fee_tier is not None:
        safe_observations["account_fee_tier"] = _nonnegative_int(
            fee_tier, "account_fee_tier", maximum=10_000
        )

    if passed:
        invariant_failed = (
            safe_observations["portfolio_fresh"] is not True
            or safe_observations["auto_cancel_armed"] is not True
            or safe_observations["fee_schedule_fresh"] is not True
            or safe_observations.get("account_fee_tier") is None
            or safe_observations.get("margin_available") is None
            or Decimal(safe_observations["margin_available"] or "0") <= 0
            or any(safe_observations[name] != 0 for name in (
                "unresolved_order_count",
                "cross_margin_positions",
                "over_leveraged_positions",
                "position_integrity_errors",
                "high_adl_risk_positions",
                "unknown_remote_open_orders",
                "missing_remote_open_orders",
            ))
        )
        if invariant_failed:
            reasons.append("preflight_pass_invariant_violation")

    return {
        "passed": passed and not reasons,
        "blocked_reasons": reasons,
        "observations": safe_observations,
    }


class _ExclusiveAccountInspectionLock:
    """Use the same O_EXCL lock path as the single-order gateway.

    An existing lock, including a stale one, is never removed automatically.
    Operators must inspect the account and journal before clearing a stale lock.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "token": self.token,
            "pid": os.getpid(),
            "created_at": _utc_now().isoformat(),
            "mode": "PERPS_SINGLE_SUBMISSION_LOCK",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return True
        except BaseException:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                raise

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return
            if payload.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False


def _default_status() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "PERPS_ACCOUNT_READ_ONLY_SUPERVISOR",
        "enabled": False,
        "worker_state": "unknown",
        "last_heartbeat_at": None,
        "last_preflight_at": None,
        "last_error_code": "status_file_missing",
        "consecutive_failures": 0,
        "recovery_completed": False,
        "recovered_interrupted_submissions": 0,
        "read_only_preflight_passed": False,
        "blocked_reasons": [],
        "observations": {},
        "poll_interval_seconds": None,
        "cycle_timeout_seconds": None,
        "research_only": True,
        "live_execution_enabled": False,
        "order_submission_allowed": False,
        "execution": "none",
    }


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def read_perps_account_supervisor_status(path: str | Path) -> dict[str, Any]:
    """Read a strict allowlist view; malformed files never appear healthy."""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("mode") != "PERPS_ACCOUNT_READ_ONLY_SUPERVISOR"
        ):
            raise ValueError("invalid account supervisor status schema")
        state = raw.get("worker_state")
        if state not in {"starting", "healthy", "degraded", "stopped"}:
            raise ValueError("invalid account supervisor worker state")
        heartbeat = raw.get("last_heartbeat_at")
        if heartbeat is not None:
            stamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
            _aware_utc(stamp, "last_heartbeat_at")
        preflight_at = raw.get("last_preflight_at")
        if preflight_at is not None:
            stamp = datetime.fromisoformat(str(preflight_at).replace("Z", "+00:00"))
            _aware_utc(stamp, "last_preflight_at")
        reasons = raw.get("blocked_reasons")
        if not isinstance(reasons, list) or len(reasons) > 50 or any(
            not isinstance(reason, str) or not _REASON_RE.fullmatch(reason)
            for reason in reasons
        ):
            raise ValueError("invalid account supervisor reason codes")
        observations = raw.get("observations")
        if not isinstance(observations, dict):
            raise ValueError("invalid account supervisor observations")
        safe_report = _sanitize_preflight_report({
            "mode": "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT",
            "read_only_preflight_passed": raw.get("read_only_preflight_passed"),
            "blocked_reasons": reasons,
            "observations": observations,
            "order_placement_authorized": False,
            "live_execution_enabled": False,
            "execution": "none",
        })
        if state == "healthy" and (
            raw.get("enabled") is not True
            or raw.get("recovery_completed") is not True
            or not safe_report["passed"]
            or raw.get("consecutive_failures", 0) != 0
            or preflight_at is None
        ):
            raise ValueError("healthy status conflicts with preflight result")
        recovery_count = _nonnegative_int(
            raw.get("recovered_interrupted_submissions", 0),
            "recovered_interrupted_submissions",
        )
        failures = _nonnegative_int(raw.get("consecutive_failures", 0), "consecutive_failures")
        poll = _nonnegative_int(raw.get("poll_interval_seconds"), "poll_interval_seconds", minimum=15, maximum=300)
        timeout = raw.get("cycle_timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.05 <= float(timeout) <= 3600:
            raise ValueError("invalid cycle timeout")
        enabled = raw.get("enabled") is True
        return {
            **_default_status(),
            "enabled": enabled,
            "worker_state": state,
            "last_heartbeat_at": heartbeat,
            "last_preflight_at": preflight_at,
            "last_error_code": (
                raw.get("last_error_code")
                if isinstance(raw.get("last_error_code"), str)
                and _REASON_RE.fullmatch(raw["last_error_code"])
                else None
            ),
            "consecutive_failures": failures,
            "recovery_completed": raw.get("recovery_completed") is True,
            "recovered_interrupted_submissions": recovery_count,
            "read_only_preflight_passed": safe_report["passed"],
            "blocked_reasons": safe_report["blocked_reasons"],
            "observations": safe_report["observations"],
            "poll_interval_seconds": poll,
            "cycle_timeout_seconds": float(timeout),
        }
    except FileNotFoundError:
        return _default_status()
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        result = _default_status()
        result["last_error_code"] = "status_file_invalid"
        return result


def perps_account_supervisor_freshness(
    snapshot: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Downgrade stale/future heartbeats so an old status file cannot look healthy."""
    current = _aware_utc(now or _utc_now(), "now")
    result = dict(snapshot) if isinstance(snapshot, dict) else _default_status()
    raw_poll = result.get("poll_interval_seconds")
    poll = (
        raw_poll
        if isinstance(raw_poll, int) and not isinstance(raw_poll, bool) and 15 <= raw_poll <= 300
        else DEFAULT_POLL_INTERVAL_SECONDS
    )
    stale_after = max(60, poll * 3)
    heartbeat_age: float | None = None
    try:
        stamp = datetime.fromisoformat(
            str(result.get("last_heartbeat_at") or "").replace("Z", "+00:00")
        )
        heartbeat_age = (current - _aware_utc(stamp, "last_heartbeat_at")).total_seconds()
    except (TypeError, ValueError, OverflowError):
        heartbeat_age = None

    state = result.get("worker_state")
    fresh = heartbeat_age is not None and -2 <= heartbeat_age <= stale_after
    if result.get("enabled") is not True and state != "stopped":
        state = "disabled"
        fresh = False
    elif heartbeat_age is None:
        state = "starting" if state == "starting" else "unknown"
    elif heartbeat_age < -2:
        state = "invalid"
    elif heartbeat_age > stale_after:
        state = "stale"
    result.update({
        "worker_state": state,
        "heartbeat_fresh": fresh,
        "heartbeat_age_seconds": (
            round(max(0.0, heartbeat_age), 1) if heartbeat_age is not None else None
        ),
        "heartbeat_stale_after_seconds": stale_after,
    })
    return result


class PerpsAccountSupervisor:
    """Periodically inspect an injected account; it has no execution methods."""

    def __init__(
        self,
        *,
        journal: Any,
        account_preflight: Any,
        status_path: str | Path,
        submission_lock_path: str | Path,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        cycle_timeout_seconds: float = DEFAULT_CYCLE_TIMEOUT_SECONDS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        now_provider: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not callable(getattr(journal, "recover_interrupted_submissions", None)):
            raise ValueError("journal must expose recover_interrupted_submissions")
        if not callable(getattr(account_preflight, "run", None)):
            raise ValueError("account_preflight must expose run")
        if isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, int) or not 15 <= poll_interval_seconds <= 300:
            raise ValueError("poll_interval_seconds must be between 15 and 300")
        if (
            isinstance(cycle_timeout_seconds, bool)
            or not isinstance(cycle_timeout_seconds, (int, float))
            or not 0.05 <= float(cycle_timeout_seconds) <= 3600
        ):
            raise ValueError("cycle_timeout_seconds must be between 0.05 and 3600")
        if (
            isinstance(max_consecutive_failures, bool)
            or not isinstance(max_consecutive_failures, int)
            or not 1 <= max_consecutive_failures <= 50
        ):
            raise ValueError("max_consecutive_failures must be between 1 and 50")
        if now_provider is not None and not callable(now_provider):
            raise ValueError("now_provider must be callable")
        if not callable(sleep):
            raise ValueError("sleep must be callable")
        if not str(submission_lock_path).strip():
            raise ValueError("submission_lock_path must be a non-empty path")
        self.journal = journal
        self.account_preflight = account_preflight
        self.status_path = Path(status_path).expanduser()
        self.submission_lock_path = Path(submission_lock_path).expanduser()
        self.poll_interval_seconds = poll_interval_seconds
        self.cycle_timeout_seconds = float(cycle_timeout_seconds)
        self.max_consecutive_failures = max_consecutive_failures
        self.now_provider = now_provider or _utc_now
        self.sleep = sleep

    async def run(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        max_cycles: int | None = None,
    ) -> int:
        if max_cycles is not None and (
            isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1
        ):
            raise ValueError("max_cycles must be a positive integer")
        state = _default_status()
        state.update({
            "enabled": True,
            "worker_state": "starting",
            "last_error_code": None,
            "poll_interval_seconds": self.poll_interval_seconds,
            "cycle_timeout_seconds": self.cycle_timeout_seconds,
        })
        recovery_completed = False
        cycles = 0
        failures = 0
        _write_status(self.status_path, state)

        while stop_event is None or not stop_event.is_set():
            current = _aware_utc(self.now_provider(), "now_provider")
            state["last_heartbeat_at"] = current.isoformat()
            lock = _ExclusiveAccountInspectionLock(self.submission_lock_path)
            try:
                if not lock.acquire():
                    # Do not inspect or recover the journal while the gateway may
                    # be inside its single-order submission critical section.
                    state.update({
                        "worker_state": "degraded",
                        "last_error_code": "submission_lock_unavailable",
                        "consecutive_failures": 0,
                        "read_only_preflight_passed": False,
                        "blocked_reasons": ["submission_lock_unavailable"],
                        "observations": {},
                    })
                    failures = 0
                else:
                    try:
                        if not recovery_completed:
                            recovered = self.journal.recover_interrupted_submissions()
                            state["recovered_interrupted_submissions"] = _nonnegative_int(
                                recovered, "recovered_interrupted_submissions"
                            )
                            state["recovery_completed"] = True
                            recovery_completed = True

                        report = await asyncio.wait_for(
                            self.account_preflight.run(now=current),
                            timeout=self.cycle_timeout_seconds,
                        )
                        safe_report = _sanitize_preflight_report(report)
                        completed_at = _aware_utc(self.now_provider(), "now_provider")
                        state.update({
                            "worker_state": "healthy" if safe_report["passed"] else "degraded",
                            "last_preflight_at": completed_at.isoformat(),
                            "last_error_code": None,
                            "consecutive_failures": 0,
                            "read_only_preflight_passed": safe_report["passed"],
                            "blocked_reasons": safe_report["blocked_reasons"],
                            "observations": safe_report["observations"],
                        })
                        failures = 0
                    finally:
                        lock.release()
            except asyncio.CancelledError:
                lock.release()
                raise
            except Exception as exc:
                lock.release()
                failures += 1
                state.update({
                    "worker_state": "degraded",
                    "last_error_code": (
                        "account_supervisor_cycle_timeout"
                        if isinstance(exc, asyncio.TimeoutError)
                        else "account_supervisor_cycle_failed"
                    ),
                    "consecutive_failures": failures,
                    "read_only_preflight_passed": False,
                    "blocked_reasons": ["account_supervisor_cycle_unavailable"],
                })
                state["observations"] = {}
            state["last_heartbeat_at"] = _aware_utc(self.now_provider(), "now_provider").isoformat()
            _write_status(self.status_path, state)
            cycles += 1
            if failures >= self.max_consecutive_failures:
                return 1
            if max_cycles is not None and cycles >= max_cycles:
                return 0

            if failures:
                delay = min(
                    DEFAULT_ERROR_BACKOFF_CAP_SECONDS,
                    2.0 * (2 ** min(failures - 1, 7)),
                )
            else:
                delay = self.poll_interval_seconds
            if stop_event is None:
                await self.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    continue
                break

        state["worker_state"] = "stopped"
        state["enabled"] = False
        state["last_heartbeat_at"] = _aware_utc(self.now_provider(), "now_provider").isoformat()
        _write_status(self.status_path, state)
        return 0


__all__ = [
    "PerpsAccountSupervisor",
    "read_perps_account_supervisor_status",
    "perps_account_supervisor_freshness",
]
