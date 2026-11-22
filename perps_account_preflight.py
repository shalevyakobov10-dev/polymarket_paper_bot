"""Read-only Perps account startup preflight; never authorizes order placement."""
from __future__ import annotations

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
        self.portfolio_max_age_seconds = portfolio_max_age_seconds
        self.auto_cancel_min_remaining_seconds = auto_cancel_min_remaining_seconds
        self.reconciler = PerpsSdkReconciler(journal=journal, session=session)

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
            "unknown_remote_open_orders": 0,
            "missing_remote_open_orders": 0,
            "execution": "none",
            "order_placement_authorized": False,
        }

        # Reconcile all previously submitted intents before sampling open orders.
        intents = self.journal.list_intents(limit=self.max_unresolved_orders_to_reconcile + 1)
        observations["local_order_count"] = len(intents)
        if len(intents) > self.max_unresolved_orders_to_reconcile:
            reasons.append("local_order_journal_exceeds_reconciliation_bound")
        else:
            for intent in intents:
                if intent["status"] in RECONCILABLE_STATUSES or intent["status"] == "submission_pending":
                    try:
                        await self.reconciler.reconcile(intent["client_order_id"])
                    except Exception:
                        reasons.append("local_order_reconciliation_failed")
                        break

        refreshed_intents = self.journal.list_intents(
            limit=self.max_unresolved_orders_to_reconcile + 1
        )
        unresolved = [
            item for item in refreshed_intents if item["status"] in UNRESOLVED_STATES
        ]
        observations["unresolved_order_count"] = len(unresolved)
        if unresolved:
            reasons.append("unresolved_or_unsubmitted_local_orders")

        try:
            portfolio = await self.session.fetch_portfolio()
            balances = await self.session.fetch_balances()
            open_orders = await self.session.fetch_open_orders()
            auto_cancel = await self.session.fetch_auto_cancel_status()
            account_configs = await self.session.fetch_account_config()
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
        available_margin = _field(margin, "available_order_margin")
        try:
            available = _finite_decimal(available_margin)
            observations["margin_available"] = str(available)
            if available <= 0:
                reasons.append("no_available_order_margin")
        except ValueError:
            reasons.append("available_order_margin_invalid")

        positions = _field(portfolio, "positions")
        if not isinstance(positions, (tuple, list)):
            reasons.append("portfolio_positions_invalid")
            positions = ()
        observations["position_count"] = len(positions)
        if len(positions) > self.max_open_positions:
            reasons.append("max_open_positions_exceeded")

        for position in positions:
            cross = _field(position, "cross")
            if cross is not False:
                observations["cross_margin_positions"] += 1
            leverage = _field(position, "leverage")
            if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage > self.max_leverage or leverage < 1:
                observations["over_leveraged_positions"] += 1
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
                if int(_field(order, "instrument_id")) != local["instrument_id"]:
                    raise ValueError("instrument")
                side_value = _field(order, "side")
                if not isinstance(side_value, str) or side_value.lower() != local["side"]:
                    raise ValueError("side")
                if _finite_decimal(_field(order, "quantity")) != Decimal(local["quantity"]):
                    raise ValueError("quantity")
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
