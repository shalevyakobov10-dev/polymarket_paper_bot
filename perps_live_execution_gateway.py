"""Gated single-order entry point for a future explicitly authorized Perps session.

This module is disabled by default. It requires independent account preflight,
server-side Auto-Cancel verification, an explicit operator acknowledgement,
a local kill-switch absence, a bounded risk approval, and per-instrument
isolated-margin/leverage verification before it can call the SDK.

It deliberately supports IOC/FOK limit orders only; it has no market-order,
GTC, batch-order, leverage-change, margin-transfer, or withdrawal path.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from perps_order_journal import PerpsOrderJournal
from perps_sdk_reconciler import PerpsSdkReconciler
from perps_live_release_gate import evaluate_perps_live_release_gate


LIVE_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_PERPS_LIVE_TRADING_RISK"
ALLOWED_TIME_IN_FORCE = frozenset({"ioc", "fok"})
POSITION_SIDES = {
    "open_long": "BUY",
    "open_short": "SELL",
    "close_long": "SELL",
    "close_short": "BUY",
}


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
        raise ValueError(f"{name} is outside valid bounds")
    return result


class _ExclusiveSubmissionLock:
    """Crash-conservative per-account lock; stale files require manual review."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "token": self.token,
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
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


class PerpsLiveExecutionGateway:
    """Single-order gateway with layered operator, account and risk gates.

    No SDK client or account session is created here. The caller must inject a
    ready authenticated Perps session. This class does not run automatically.
    """

    def __init__(
        self,
        *,
        session: Any,
        journal: PerpsOrderJournal,
        reconciler: PerpsSdkReconciler,
        account_preflight: Any,
        auto_cancel_guard: Any,
        enabled: bool = False,
        manually_armed: bool = False,
        dry_run: bool = True,
        acknowledgement: str | None = None,
        kill_switch_path: str | Path,
        submission_lock_path: str | Path,
        max_order_notional_usd: Any = "25",
        max_leverage: int = 2,
        account_read_timeout_seconds: float = 5.0,
        submission_timeout_seconds: float = 10.0,
    ) -> None:
        if not callable(getattr(session, "place_order", None)):
            raise ValueError("session must expose place_order")
        if not callable(getattr(account_preflight, "run", None)):
            raise ValueError("account_preflight must expose run")
        if not callable(getattr(auto_cancel_guard, "arm_once", None)):
            raise ValueError("auto_cancel_guard must expose arm_once")
        if not isinstance(journal, PerpsOrderJournal):
            raise ValueError("a durable PerpsOrderJournal is required")
        if not isinstance(reconciler, PerpsSdkReconciler):
            raise ValueError("a PerpsSdkReconciler is required")
        if not all(isinstance(flag, bool) for flag in (enabled, manually_armed, dry_run)):
            raise ValueError("gateway flags must be booleans")
        if isinstance(max_leverage, bool) or not isinstance(max_leverage, int) or not 1 <= max_leverage <= 20:
            raise ValueError("max_leverage must be between 1 and 20")
        max_notional = _decimal(max_order_notional_usd, "max_order_notional_usd", positive=True)
        if max_notional > Decimal("100"):
            raise ValueError("initial Perps order notional cap cannot exceed 100 USD")
        if enabled and manually_armed:
            if dry_run:
                raise ValueError("live Perps gateway cannot run while DRY_RUN is enabled")
            if acknowledgement != LIVE_ACKNOWLEDGEMENT:
                raise ValueError("explicit live Perps risk acknowledgement is required")
        self.session = session
        self.journal = journal
        self.reconciler = reconciler
        self.account_preflight = account_preflight
        self.auto_cancel_guard = auto_cancel_guard
        self.enabled = enabled
        self.manually_armed = manually_armed
        self.dry_run = dry_run
        self.acknowledgement = acknowledgement
        self.kill_switch_path = Path(kill_switch_path).expanduser()
        self.submission_lock_path = Path(submission_lock_path).expanduser()
        for name, value in (
            ("account_read_timeout_seconds", account_read_timeout_seconds),
            ("submission_timeout_seconds", submission_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.05 <= float(value) <= 60.0
            ):
                raise ValueError(f"{name} must be between 0.05 and 60")
        self.max_order_notional_usd = max_notional
        self.max_leverage = max_leverage
        self.account_read_timeout_seconds = float(account_read_timeout_seconds)
        self.submission_timeout_seconds = float(submission_timeout_seconds)

    def _blocked(self, reason: str, *, client_order_id: str | None = None) -> dict[str, Any]:
        return {
            "mode": "PERPS_LIVE_EXECUTION_GATEWAY",
            "state": "blocked",
            "reason_code": reason,
            "client_order_id": client_order_id,
            "order_placement_attempted": False,
            "order_placement_authorized": False,
            "execution": "none",
        }

    async def submit_limit_order(
        self,
        *,
        instrument_id: int,
        quantity: Any,
        position_intent: str,
        limit_price: Any,
        risk_decision: dict[str, Any],
        time_in_force: str = "ioc",
        client_order_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        if not self.enabled or not self.manually_armed:
            return self._blocked("live_execution_not_explicitly_enabled_and_armed")
        if self.dry_run or self.acknowledgement != LIVE_ACKNOWLEDGEMENT:
            return self._blocked("dry_run_or_live_acknowledgement_gate")
        release_report = evaluate_perps_live_release_gate()
        if release_report.get("eligible_for_separate_gateway_review") is not True:
            return self._blocked("source_controlled_perps_release_gate_closed")
        if self.kill_switch_path.exists():
            return self._blocked("local_perps_kill_switch_present")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
            return self._blocked("instrument_id_invalid")

        intent = position_intent.strip().lower() if isinstance(position_intent, str) else ""
        side = POSITION_SIDES.get(intent)
        if side is None:
            return self._blocked("position_intent_invalid")
        tif = time_in_force.strip().lower() if isinstance(time_in_force, str) else ""
        if tif not in ALLOWED_TIME_IN_FORCE:
            return self._blocked("time_in_force_must_be_ioc_or_fok")
        try:
            qty = _decimal(quantity, "quantity", positive=True)
            price = _decimal(limit_price, "limit_price", positive=True)
        except ValueError:
            return self._blocked("order_quantity_or_limit_price_invalid")
        order_notional = qty * price
        if order_notional > self.max_order_notional_usd:
            return self._blocked("hard_max_order_notional_exceeded")

        if not isinstance(risk_decision, dict) or risk_decision.get("approved") is not True:
            return self._blocked("independent_risk_decision_not_approved")
        try:
            risk_cap = _decimal(risk_decision.get("max_notional_usd"), "risk_max_notional", positive=True)
            required_margin = _decimal(
                risk_decision.get("required_initial_margin_usd"),
                "required_initial_margin_usd",
            )
        except ValueError:
            return self._blocked("risk_snapshot_missing_or_invalid")
        if order_notional > min(self.max_order_notional_usd, risk_cap):
            return self._blocked("risk_snapshot_notional_cap_exceeded")
        if not isinstance(risk_decision.get("risk_snapshot_id"), str) or not risk_decision["risk_snapshot_id"].strip():
            return self._blocked("risk_snapshot_id_missing")
        signal_id = risk_decision.get("strategy_signal_id")
        if not isinstance(signal_id, str) or not signal_id.strip():
            return self._blocked("strategy_signal_id_missing")

        identifier = client_order_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or len(identifier) != 32 or any(ch not in "0123456789abcdef" for ch in identifier):
            return self._blocked("client_order_id_invalid")

        lock = _ExclusiveSubmissionLock(self.submission_lock_path)
        if not lock.acquire():
            return self._blocked("another_perps_submission_or_stale_lock_exists", client_order_id=identifier)
        try:
            if self.kill_switch_path.exists():
                return self._blocked("local_perps_kill_switch_present", client_order_id=identifier)

            guard_report = await self.auto_cancel_guard.arm_once(now=current)
            if not isinstance(guard_report, dict) or guard_report.get("state") != "armed":
                return self._blocked("server_auto_cancel_guard_not_verified", client_order_id=identifier)

            preflight_report = await self.account_preflight.run(now=current)
            if not isinstance(preflight_report, dict) or preflight_report.get("read_only_preflight_passed") is not True:
                return self._blocked("account_preflight_failed", client_order_id=identifier)
            observations = preflight_report.get("observations")
            if not isinstance(observations, dict):
                return self._blocked("account_preflight_observations_missing", client_order_id=identifier)
            if observations.get("fee_schedule_fresh") is not True:
                return self._blocked("account_fee_schedule_not_fresh_or_verified", client_order_id=identifier)
            fee_tier = observations.get("account_fee_tier")
            if isinstance(fee_tier, bool) or not isinstance(fee_tier, int) or fee_tier < 0:
                return self._blocked("account_fee_tier_not_verified", client_order_id=identifier)
            try:
                margin_available = _decimal(observations.get("margin_available"), "available_order_margin")
                account_taker_fee_rate = _decimal(
                    observations.get("effective_taker_fee_rate"),
                    "account_effective_taker_fee_rate",
                )
                max_round_trip_fee_budget = _decimal(
                    risk_decision.get("max_round_trip_fee_usd"),
                    "max_round_trip_fee_usd",
                )
            except ValueError:
                return self._blocked("account_fee_or_margin_observation_invalid", client_order_id=identifier)
            if account_taker_fee_rate >= Decimal("1"):
                return self._blocked("account_taker_fee_rate_out_of_bounds", client_order_id=identifier)
            estimated_entry_fee = order_notional * account_taker_fee_rate
            estimated_round_trip_fee = estimated_entry_fee * Decimal("2")
            if max_round_trip_fee_budget < estimated_round_trip_fee:
                return self._blocked("risk_fee_budget_below_current_taker_fee_estimate", client_order_id=identifier)
            # Reserve the estimated close fee as well as the entry fee so
            # the account has room for the planned round trip, not only entry.
            if required_margin + estimated_round_trip_fee > margin_available:
                return self._blocked(
                    "insufficient_available_initial_margin_and_round_trip_fees",
                    client_order_id=identifier,
                )

            # Confirm the target instrument's own margin mode and leverage.
            try:
                configs = await asyncio.wait_for(
                    self.session.fetch_account_config(instrument_id=instrument_id),
                    timeout=self.account_read_timeout_seconds,
                )
            except Exception:
                return self._blocked("target_instrument_config_unavailable", client_order_id=identifier)
            if not isinstance(configs, (tuple, list)) or len(configs) != 1:
                return self._blocked("target_instrument_config_missing_or_ambiguous", client_order_id=identifier)
            config = configs[0]
            if config is None:
                return self._blocked("target_instrument_config_missing_or_ambiguous", client_order_id=identifier)
            configured_instrument_id = _field(config, "instrument_id")
            if (
                isinstance(configured_instrument_id, bool)
                or not isinstance(configured_instrument_id, int)
                or configured_instrument_id != instrument_id
            ):
                return self._blocked("target_instrument_config_instrument_mismatch", client_order_id=identifier)
            leverage = _field(config, "leverage")
            cross_margin = _field(config, "cross")
            if cross_margin is not False:
                return self._blocked("target_instrument_not_confirmed_isolated", client_order_id=identifier)
            if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= self.max_leverage:
                return self._blocked("target_instrument_leverage_exceeds_policy", client_order_id=identifier)

            if self.kill_switch_path.exists():
                return self._blocked("local_perps_kill_switch_present", client_order_id=identifier)

            try:
                prepared = self.journal.create_intent(
                    client_order_id=identifier,
                    instrument_id=instrument_id,
                    side=side.lower(),
                    quantity=qty,
                    position_intent=intent,
                    time_in_force=tif,
                    limit_price=price,
                    strategy_signal_id=signal_id,
                    risk_snapshot_id=risk_decision["risk_snapshot_id"],
                )
                if prepared["status"] != "prepared":
                    return self._blocked("client_order_id_already_used_or_not_prepared", client_order_id=identifier)
                pending = self.journal.begin_submission(identifier)
                if pending["status"] != "submission_pending":
                    return self._blocked("submission_journal_transition_failed", client_order_id=identifier)
            except Exception:
                return self._blocked("durable_pre_submission_journal_failed", client_order_id=identifier)

            try:
                placement = await asyncio.wait_for(
                    self.session.place_order(
                        instrument_id=instrument_id,
                        side=side,
                        quantity=qty,
                        time_in_force=tif,
                        price=price,
                        reduce_only=intent.startswith("close_"),
                        client_order_id=identifier,
                    ),
                    timeout=self.submission_timeout_seconds,
                )
            except asyncio.CancelledError:
                self.journal.record_submission_unknown(
                    identifier,
                    reason_code="sdk_submission_task_cancelled_outcome_unknown",
                )
                raise
            except asyncio.TimeoutError:
                state = self.journal.record_submission_unknown(
                    identifier,
                    reason_code="sdk_submission_timeout_outcome_unknown",
                )
                return {
                    "mode": "PERPS_LIVE_EXECUTION_GATEWAY",
                    "state": state["status"],
                    "reason_code": state["last_reason_code"],
                    "client_order_id": identifier,
                    "order_placement_attempted": True,
                    "automatic_retry_permitted": False,
                    "execution": "submission_outcome_unknown",
                }
            except Exception:
                state = self.journal.record_submission_unknown(
                    identifier,
                    reason_code="sdk_submission_outcome_unknown",
                )
                return {
                    "mode": "PERPS_LIVE_EXECUTION_GATEWAY",
                    "state": state["status"],
                    "reason_code": state["last_reason_code"],
                    "client_order_id": identifier,
                    "order_placement_attempted": True,
                    "automatic_retry_permitted": False,
                    "execution": "submission_outcome_unknown",
                }

            order = _field(placement, "order")
            venue_id = _field(order, "id")
            returned_client_id = _field(order, "client_order_id")
            if isinstance(venue_id, bool) or not isinstance(venue_id, (int, str)) or not str(venue_id).strip():
                state = self.journal.record_submission_unknown(
                    identifier,
                    reason_code="sdk_acknowledgement_missing_venue_order_id",
                )
                return {
                    "mode": "PERPS_LIVE_EXECUTION_GATEWAY",
                    "state": state["status"],
                    "reason_code": state["last_reason_code"],
                    "client_order_id": identifier,
                    "order_placement_attempted": True,
                    "automatic_retry_permitted": False,
                    "execution": "acknowledgement_unresolved",
                }

            self.journal.record_submission_acknowledgement(
                identifier,
                venue_order_id=str(venue_id),
            )
            if returned_client_id != identifier:
                state = self.journal.record_reconciliation(
                    identifier,
                    outcome="found",
                    matched_client_order_id=returned_client_id,
                    venue_order_id=str(venue_id),
                )
            else:
                try:
                    state = await self.reconciler.reconcile(identifier)
                except Exception:
                    state = self.journal.get_intent(identifier)
            return {
                "mode": "PERPS_LIVE_EXECUTION_GATEWAY",
                "state": state["status"] if state else "manual_review",
                "reason_code": state.get("last_reason_code") if state else "journal_state_missing",
                "client_order_id": identifier,
                "venue_order_id": str(venue_id),
                "filled_quantity": state.get("filled_quantity") if state else None,
                "average_fill_price": state.get("average_fill_price") if state else None,
                "order_placement_attempted": True,
                "automatic_retry_permitted": False,
                "execution": "perps_order_lifecycle_started",
            }
        finally:
            lock.release()
