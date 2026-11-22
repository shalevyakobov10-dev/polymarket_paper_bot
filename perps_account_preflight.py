"""Read-only Perps account startup preflight; never authorizes order placement."""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from perps_order_journal import PerpsOrderJournal, RECONCILABLE_STATUSES
from perps_sdk_reconciler import PerpsSdkReconciler


ACTIVE_LOCAL_STATES = frozenset({"open", "partially_filled", "cancel_pending"})
UNRESOLVED_STATES = frozenset({
    "prepared", "submission_pending", "submission_unknown",
    "acknowledged_unreconciled", "manual_review",
})
# Do not let old terminal trade history consume the bounded reconciliation budget.
# Only unresolved intents and locally active orders are relevant to readiness.
READINESS_TRACKED_STATES = tuple(sorted(UNRESOLVED_STATES | ACTIVE_LOCAL_STATES))
REMOTE_OPEN_STATES = frozenset({"accepted", "open", "partial"})


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _finite_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid decimal") from None
    if not result.is_finite():
        raise ValueError("invalid decimal")
    return result


class PerpsAccountPreflight:
    """Check that an injected SDK account session is internally reconciled.

    The check is strictly read-only. A successful report means only that the
    sampled account state passed this preflight; it never permits trading.
    """

    def __init__(
        self,
        *,
        journal: PerpsOrderJournal,
        session: Any,
        max_leverage: int = 2,
        max_open_positions: int = 3,
        max_open_orders: int = 10,
        max_unresolved_orders_to_reconcile: int = 100,
        portfolio_max_age_seconds: int = 120,
        auto_cancel_min_remaining_seconds: int = 10,
        account_read_timeout_seconds: float = 10.0,
        fee_schedule_provider: Any | None = None,
        fee_schedule_max_age_seconds: int = 900,
    ) -> None:
        required = (
            "fetch_portfolio", "fetch_balances", "fetch_open_orders",
            "fetch_auto_cancel_status", "fetch_account_config",
        )
        missing = [name for name in required if not callable(getattr(session, name, None))]
        if missing:
            raise ValueError("Perps account session is missing required read APIs")
        for name, value, maximum in (
            ("max_leverage", max_leverage, 20),
            ("max_open_positions", max_open_positions, 100),
            ("max_open_orders", max_open_orders, 1000),
            ("max_unresolved_orders_to_reconcile", max_unresolved_orders_to_reconcile, 1000),
            ("portfolio_max_age_seconds", portfolio_max_age_seconds, 3600),
            ("auto_cancel_min_remaining_seconds", auto_cancel_min_remaining_seconds, 300),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        self.journal = journal
        self.session = session
        self.max_leverage = max_leverage
        self.max_open_positions = max_open_positions
        self.max_open_orders = max_open_orders
        self.max_unresolved_orders_to_reconcile = max_unresolved_orders_to_reconcile
        if (
            isinstance(account_read_timeout_seconds, bool)
            or not isinstance(account_read_timeout_seconds, (int, float))
            or not math.isfinite(float(account_read_timeout_seconds))
            or not 0.05 <= float(account_read_timeout_seconds) <= 60.0
        ):
            raise ValueError("account_read_timeout_seconds must be between 0.05 and 60")
        self.portfolio_max_age_seconds = portfolio_max_age_seconds
        self.auto_cancel_min_remaining_seconds = auto_cancel_min_remaining_seconds
        if fee_schedule_provider is not None and not callable(fee_schedule_provider):
            raise ValueError("fee_schedule_provider must be an async callable")
        if (
            isinstance(fee_schedule_max_age_seconds, bool)
            or not isinstance(fee_schedule_max_age_seconds, int)
            or not 30 <= fee_schedule_max_age_seconds <= 3600
        ):
            raise ValueError("fee_schedule_max_age_seconds must be between 30 and 3600")
        self.account_read_timeout_seconds = float(account_read_timeout_seconds)
        self.fee_schedule_provider = fee_schedule_provider
        self.fee_schedule_max_age_seconds = fee_schedule_max_age_seconds
        self.reconciler = PerpsSdkReconciler(journal=journal, session=session)

    def _resolve_account_fee_tier(
        self,
        fee_snapshot: Any,
        fee_tier: int,
        now: datetime,
    ) -> tuple[Decimal, Decimal]:
        """Resolve the authenticated account tier against a fresh public Perps schedule.

        Polymarket documents one Perps tier ladder; legacy category labels must
        not be used to select different fee ladders for different instruments.
        If a response contains several perpetual entries, their complete tier
        ladders must agree before the account's fee_tier index is applied.
        """
        if not isinstance(fee_snapshot, dict):
            raise ValueError("fee schedule response must be an object")
        retrieved_raw = fee_snapshot.get("retrieved_at")
        if isinstance(retrieved_raw, datetime):
            retrieved = retrieved_raw
        elif isinstance(retrieved_raw, str):
            try:
                retrieved = datetime.fromisoformat(retrieved_raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("fee schedule retrieval timestamp is invalid") from None
        else:
            raise ValueError("fee schedule retrieval timestamp is missing")
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            raise ValueError("fee schedule retrieval timestamp must be timezone-aware")
        age = (now - retrieved.astimezone(timezone.utc)).total_seconds()
        if age < -2 or age > self.fee_schedule_max_age_seconds:
            raise ValueError("fee schedule snapshot is stale or future-dated")

        rows = fee_snapshot.get("fee_schedule")
        if not isinstance(rows, (tuple, list)):
            raise ValueError("fee schedule entries are missing")
        perps_rows = [
            row for row in rows
            if isinstance(row, dict)
            and str(row.get("instrument_type") or "").strip().lower() == "perpetual"
        ]
        if not perps_rows:
            raise ValueError("no perpetual fee schedule entry is available")

        normalized_ladders: list[tuple[tuple[str, str, str], ...]] = []
        for entry in perps_rows:
            tiers = entry.get("tiers")
            if not isinstance(tiers, list) or not tiers:
                raise ValueError("perpetual fee tiers are missing")
            normalized: list[tuple[str, str, str]] = []
            previous_volume = Decimal("-1")
            for tier in tiers:
                if not isinstance(tier, dict):
                    raise ValueError("perpetual fee tier has an invalid shape")
                volume = _finite_decimal(tier.get("min_volume_30d"))
                taker = _finite_decimal(tier.get("taker_fee_rate"))
                maker = _finite_decimal(tier.get("maker_fee_rate"))
                if volume < 0 or volume <= previous_volume:
                    raise ValueError("perpetual fee tier thresholds are not strictly increasing")
                if not Decimal("0") <= taker < Decimal("1"):
                    raise ValueError("perpetual taker fee rate is outside valid bounds")
                if not Decimal("-1") < maker < Decimal("1"):
                    raise ValueError("perpetual maker fee rate is outside valid bounds")
                previous_volume = volume
                normalized.append((str(volume), str(taker), str(maker)))
            normalized_ladders.append(tuple(normalized))

        if any(ladder != normalized_ladders[0] for ladder in normalized_ladders[1:]):
            raise ValueError("perpetual fee schedules differ across legacy categories")
        if isinstance(fee_tier, bool) or not isinstance(fee_tier, int) or not 0 <= fee_tier < len(normalized_ladders[0]):
            raise ValueError("account fee tier is outside the published perpetual fee schedule")
        selected = normalized_ladders[0][fee_tier]
        return Decimal(selected[1]), Decimal(selected[2])

    async def run(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        reasons: list[str] = []
        observations: dict[str, Any] = {
            "portfolio_fresh": False,
            "margin_available": None,
            "position_count": 0,
            "open_order_count": 0,
            "local_order_count": 0,
            "unresolved_order_count": 0,
            "auto_cancel_armed": False,
            "auto_cancel_seconds_remaining": None,
            "cross_margin_positions": 0,
            "over_leveraged_positions": 0,
            "position_integrity_errors": 0,
            "high_adl_risk_positions": 0,
            "account_maintenance_margin_ratio": None,
            "unknown_remote_open_orders": 0,
            "missing_remote_open_orders": 0,
            "fee_schedule_fresh": False,
            "account_fee_tier": None,
            "effective_taker_fee_rate": None,
            "effective_maker_fee_rate": None,
            "execution": "none",
            "order_placement_authorized": False,
        }

        # Reconcile all previously submitted intents before sampling open orders.
        intents = self.journal.list_intents(
            statuses=READINESS_TRACKED_STATES,
            limit=self.max_unresolved_orders_to_reconcile + 1,
        )
        observations["local_order_count"] = len(intents)
        if len(intents) > self.max_unresolved_orders_to_reconcile:
            reasons.append("local_order_journal_exceeds_reconciliation_bound")
        else:
            for intent in intents:
                if intent["status"] in RECONCILABLE_STATUSES or intent["status"] == "submission_pending":
                    try:
                        await asyncio.wait_for(
                            self.reconciler.reconcile(intent["client_order_id"]),
                            timeout=self.account_read_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        reasons.append("local_order_reconciliation_timeout")
                        break
                    except Exception:
                        reasons.append("local_order_reconciliation_failed")
                        break

        refreshed_intents = self.journal.list_intents(
            statuses=READINESS_TRACKED_STATES,
            limit=self.max_unresolved_orders_to_reconcile + 1,
        )
        unresolved = [
            item for item in refreshed_intents if item["status"] in UNRESOLVED_STATES
        ]
        observations["unresolved_order_count"] = len(unresolved)
        if unresolved:
            reasons.append("unresolved_or_unsubmitted_local_orders")

        try:
            (
                portfolio,
                balances,
                open_orders,
                auto_cancel,
                account_configs,
            ) = await asyncio.wait_for(
                asyncio.gather(
                    self.session.fetch_portfolio(),
                    self.session.fetch_balances(),
                    self.session.fetch_open_orders(),
                    self.session.fetch_auto_cancel_status(),
                    self.session.fetch_account_config(),
                ),
                timeout=self.account_read_timeout_seconds,
            )
        except asyncio.TimeoutError:
            reasons.append("account_read_api_timeout")
            return self._report(reasons, observations, current)
        except Exception:
            reasons.append("account_read_api_unavailable")
            return self._report(reasons, observations, current)

        timestamp = _field(portfolio, "timestamp")
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            reasons.append("portfolio_timestamp_invalid")
        else:
            age = (current - timestamp.astimezone(timezone.utc)).total_seconds()
            if age < -2 or age > self.portfolio_max_age_seconds:
                reasons.append("portfolio_snapshot_stale_or_future_dated")
            else:
                observations["portfolio_fresh"] = True

        if _field(portfolio, "in_liquidation") is not False:
            reasons.append("account_in_liquidation_or_state_unknown")

        margin = _field(portfolio, "margin")
        margin_fields = (
            "total_account_value",
            "available_order_margin",
            "total_initial_margin",
            "total_maintenance_margin",
            "total_position_value",
        )
        parsed_margin: dict[str, Decimal] = {}
        for name in margin_fields:
            try:
                parsed_margin[name] = _finite_decimal(_field(margin, name))
                if parsed_margin[name] < 0:
                    raise ValueError("negative")
            except ValueError:
                reasons.append("account_margin_summary_invalid")
                break
        if len(parsed_margin) == len(margin_fields):
            account_value = parsed_margin["total_account_value"]
            maintenance = parsed_margin["total_maintenance_margin"]
            available = parsed_margin["available_order_margin"]
            observations["margin_available"] = str(available)
            if account_value <= 0:
                reasons.append("account_equity_nonpositive")
            elif maintenance >= account_value:
                reasons.append("account_maintenance_margin_exceeds_equity")
            else:
                observations["account_maintenance_margin_ratio"] = str(
                    (maintenance / account_value).quantize(Decimal("0.0001"))
                )
            if available <= 0:
                reasons.append("no_available_order_margin")
        else:
            reasons.append("account_margin_summary_invalid")
        try:
            withdrawable = _finite_decimal(_field(portfolio, "withdrawable"))
            if withdrawable < 0:
                raise ValueError("negative withdrawable")
        except ValueError:
            reasons.append("account_withdrawable_invalid")
        fee_tier = _field(portfolio, "fee_tier")
        if isinstance(fee_tier, bool) or not isinstance(fee_tier, int) or fee_tier < 0:
            reasons.append("account_fee_tier_invalid")
        else:
            observations["account_fee_tier"] = fee_tier
            if self.fee_schedule_provider is None:
                reasons.append("public_perps_fee_schedule_provider_missing")
            else:
                try:
                    pending_fee_snapshot = self.fee_schedule_provider()
                    if not hasattr(pending_fee_snapshot, "__await__"):
                        raise TypeError("fee schedule provider must return an awaitable")
                    fee_snapshot = await asyncio.wait_for(
                        pending_fee_snapshot,
                        timeout=self.account_read_timeout_seconds,
                    )
                    taker_rate, maker_rate = self._resolve_account_fee_tier(
                        fee_snapshot, fee_tier, current
                    )
                    observations["fee_schedule_fresh"] = True
                    observations["effective_taker_fee_rate"] = str(taker_rate)
                    observations["effective_maker_fee_rate"] = str(maker_rate)
                except asyncio.TimeoutError:
                    reasons.append("public_perps_fee_schedule_timeout")
                except Exception:
                    reasons.append("public_perps_fee_schedule_missing_stale_or_ambiguous")

        positions = _field(portfolio, "positions")
        if not isinstance(positions, (tuple, list)):
            reasons.append("portfolio_positions_invalid")
            positions = ()
        observations["position_count"] = len(positions)
        if len(positions) > self.max_open_positions:
            reasons.append("max_open_positions_exceeded")

        seen_position_instruments: set[int] = set()
        for position in positions:
            instrument_id = _field(position, "instrument_id")
            if (
                isinstance(instrument_id, bool)
                or not isinstance(instrument_id, int)
                or instrument_id <= 0
                or instrument_id in seen_position_instruments
            ):
                observations["position_integrity_errors"] += 1
            elif isinstance(instrument_id, int):
                seen_position_instruments.add(instrument_id)
            symbol = _field(position, "symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                observations["position_integrity_errors"] += 1
            for field_name, allow_zero in (
                ("size", False),
                ("entry_price", False),
                ("initial_margin", True),
                ("maintenance_margin", True),
                ("position_value", False),
                ("liquidation_price", False),
            ):
                try:
                    amount = _finite_decimal(_field(position, field_name))
                    if (allow_zero and amount < 0) or (not allow_zero and amount == 0):
                        raise ValueError("outside position bounds")
                except ValueError:
                    observations["position_integrity_errors"] += 1
            adl_index = _field(position, "adl_index")
            if isinstance(adl_index, bool) or not isinstance(adl_index, int) or adl_index not in {0, 1, 2, 3}:
                observations["position_integrity_errors"] += 1
            elif adl_index >= 2:
                observations["high_adl_risk_positions"] += 1
            cross = _field(position, "cross")
            if cross is not False:
                observations["cross_margin_positions"] += 1
            leverage = _field(position, "leverage")
            if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage > self.max_leverage or leverage < 1:
                observations["over_leveraged_positions"] += 1
        if observations["position_integrity_errors"]:
            reasons.append("account_position_data_invalid")
        if observations["high_adl_risk_positions"]:
            reasons.append("position_adl_risk_elevated")
        if observations["cross_margin_positions"]:
            reasons.append("cross_margin_position_detected")
        if observations["over_leveraged_positions"]:
            reasons.append("position_leverage_exceeds_policy")

        # Verify account-level instrument settings as well as the reported positions.
        if not isinstance(account_configs, (tuple, list)):
            reasons.append("account_leverage_config_invalid")
        else:
            for config in account_configs:
                leverage = _field(config, "leverage")
                cross = _field(config, "cross")
                if cross is not False:
                    reasons.append("cross_margin_account_config_detected")
                    break
                if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= self.max_leverage:
                    reasons.append("account_leverage_config_exceeds_policy")
                    break

        # Validate collateral values are nonnegative and finite.
        if not isinstance(balances, (tuple, list)):
            reasons.append("account_balances_invalid")
        else:
            for balance in balances:
                try:
                    amount = _finite_decimal(_field(balance, "balance"))
                    value = _finite_decimal(_field(balance, "value"))
                    if amount < 0 or value < 0:
                        raise ValueError("negative")
                except ValueError:
                    reasons.append("account_balance_invalid")
                    break

        if not isinstance(open_orders, (tuple, list)):
            reasons.append("open_order_response_invalid")
            open_orders = ()
        observations["open_order_count"] = len(open_orders)
        if len(open_orders) > self.max_open_orders:
            reasons.append("max_open_orders_exceeded")

        local_active = {
            item["client_order_id"]: item
            for item in refreshed_intents
            if item["status"] in ACTIVE_LOCAL_STATES
        }
        remote_by_client: dict[str, Any] = {}
        for order in open_orders:
            client_id = _field(order, "client_order_id")
            if not isinstance(client_id, str) or client_id not in local_active:
                observations["unknown_remote_open_orders"] += 1
                continue
            if client_id in remote_by_client:
                observations["unknown_remote_open_orders"] += 1
                continue
            remote_by_client[client_id] = order
            local = local_active[client_id]
            try:
                venue_order_id = _field(order, "id")
                if str(venue_order_id) != local["venue_order_id"]:
                    raise ValueError("venue_order_id")
                remote_instrument_id = _field(order, "instrument_id")
                if (
                    isinstance(remote_instrument_id, bool)
                    or not isinstance(remote_instrument_id, int)
                    or remote_instrument_id != local["instrument_id"]
                ):
                    raise ValueError("instrument")
                side_value = _field(order, "side")
                if not isinstance(side_value, str) or side_value.lower() != local["side"]:
                    raise ValueError("side")
                remote_status = _field(order, "status")
                if not isinstance(remote_status, str) or remote_status.lower() not in REMOTE_OPEN_STATES:
                    raise ValueError("remote_status")
                if _finite_decimal(_field(order, "quantity")) != Decimal(local["quantity"]):
                    raise ValueError("quantity")
                if _finite_decimal(_field(order, "filled_quantity")) != Decimal(local["filled_quantity"]):
                    raise ValueError("filled_quantity")
                remote_reduce_only = _field(order, "reduce_only")
                if remote_reduce_only is not local["reduce_only"]:
                    raise ValueError("reduce_only")
                local_price = local["limit_price"]
                if local_price is not None and _finite_decimal(_field(order, "price")) != Decimal(local_price):
                    raise ValueError("price")
            except (ValueError, TypeError, InvalidOperation):
                observations["unknown_remote_open_orders"] += 1
                remote_by_client.pop(client_id, None)
        observations["missing_remote_open_orders"] = len(
            set(local_active) - set(remote_by_client)
        )
        if observations["unknown_remote_open_orders"]:
            reasons.append("unmanaged_or_mismatched_remote_open_orders")
        if observations["missing_remote_open_orders"]:
            reasons.append("local_open_orders_missing_from_venue_open_order_list")

        deadline = _field(auto_cancel, "deadline")
        triggered = _field(auto_cancel, "triggered")
        daily_limit = _field(auto_cancel, "daily_limit")
        if isinstance(deadline, datetime) and deadline.tzinfo is not None and deadline.utcoffset() is not None:
            remaining = (deadline.astimezone(timezone.utc) - current).total_seconds()
            observations["auto_cancel_seconds_remaining"] = round(remaining, 3)
            observations["auto_cancel_armed"] = remaining >= self.auto_cancel_min_remaining_seconds
        if not observations["auto_cancel_armed"]:
            reasons.append("auto_cancel_deadman_switch_not_armed_or_near_expiry")
        if (
            isinstance(triggered, bool) or not isinstance(triggered, int)
            or isinstance(daily_limit, bool) or not isinstance(daily_limit, int)
            or daily_limit <= 0 or triggered >= daily_limit
        ):
            reasons.append("auto_cancel_daily_trigger_limit_unavailable_or_exhausted")

        return self._report(reasons, observations, current)

    @staticmethod
    def _report(
        reasons: list[str],
        observations: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        unique_reasons = list(dict.fromkeys(reasons))
        return {
            "mode": "PERPS_ACCOUNT_READ_ONLY_PREFLIGHT",
            "checked_at": now.isoformat(),
            "read_only_preflight_passed": not unique_reasons,
            "blocked_reasons": unique_reasons,
            "observations": observations,
            "order_placement_authorized": False,
            "live_execution_enabled": False,
            "execution": "none",
        }
