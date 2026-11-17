"""Read-only Polymarket collateral, position, and open-order risk snapshot.

Uses only authenticated read endpoints from the official polymarket-client SDK.
No allowance updates, signing, order placement, cancellation, or wallet transfers
are performed. Collateral is returned by the CLOB in base units; pUSD has six
decimals, so USD conversion is explicit and exact with Decimal arithmetic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class AccountRiskSnapshotIncompleteError(RuntimeError):
    """Raised when an account paginator cannot prove a complete snapshot."""


COLLATERAL_DECIMALS = 6


def _decimal(value: Any, field: str, *, allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise ValueError(f"{field} must be a finite decimal")
    return parsed



def _decimal_text(value: Decimal) -> str:
    """Serialize Decimal values without insignificant trailing zeros."""
    return format(value.normalize(), "f") if value else "0"


async def _collect_positions(client: Any) -> list[Any]:
    unique: dict[tuple[str, str], Any] = {}
    for status in ("OPEN", "REDEEMABLE"):
        async for page in client.list_positions(status=status, full_history=True):
            if getattr(page, "limit_reached", False):
                raise AccountRiskSnapshotIncompleteError(
                    f"SDK {status} position paginator reached its depth limit"
                )
            for position in page.items:
                key = (
                    str(getattr(position, "condition_id", "") or ""),
                    str(getattr(position, "asset_id", "") or ""),
                )
                if not all(key):
                    raise AccountRiskSnapshotIncompleteError(
                        "Position snapshot contains a record without condition_id or asset_id"
                    )
                size = _decimal(getattr(position, "current_size", None), "position.current_size")
                if size <= 0:
                    continue
                previous = unique.get(key)
                if previous is not None and previous != position:
                    raise AccountRiskSnapshotIncompleteError(
                        "Position snapshot repeated a condition/asset pair with conflicting values"
                    )
                unique[key] = position
    return list(unique.values())


async def collect_account_risk_snapshot(
    client: Any,
    *,
    open_orders: list[Any] | tuple[Any, ...] | None = None,
    collateral_decimals: int = COLLATERAL_DECIMALS,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Collect a complete read-only account risk snapshot.

    open_orders may be supplied by the user-order monitor to reuse the exact
    paginated order snapshot. When omitted, this function fetches all open orders
    itself. Any truncated paginator or malformed balance/position/order record
    aborts the snapshot instead of returning partially trusted risk figures.

    Outstanding BUY order notional is conservatively subtracted from collateral
    and added to projected exposure. This may over-reserve if the API balance
    already reflects locked funds, but it cannot overstate available collateral.
    Estimated fees, daily PnL, and drawdown are intentionally not inferred here.
    """
    if isinstance(collateral_decimals, bool) or not isinstance(collateral_decimals, int):
        raise ValueError("collateral_decimals must be an integer")
    if not 0 <= collateral_decimals <= 18:
        raise ValueError("collateral_decimals must be between 0 and 18")
    timestamp = captured_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)

    balance = await client.get_balance_allowance(asset_type="COLLATERAL")
    raw_balance = getattr(balance, "balance", None)
    if isinstance(raw_balance, bool) or not isinstance(raw_balance, int) or raw_balance < 0:
        raise ValueError("CLOB collateral balance must be a nonnegative integer in base units")
    scale = Decimal(10) ** collateral_decimals
    collateral_usd = Decimal(raw_balance) / scale

    if open_orders is None:
        collected_orders: list[Any] = []
        async for page in client.list_open_orders():
            if getattr(page, "limit_reached", False):
                raise AccountRiskSnapshotIncompleteError(
                    "SDK open-order paginator reached its depth limit"
                )
            collected_orders.extend(page.items)
        orders = collected_orders
    else:
        orders = list(open_orders)

    unique_orders: dict[str, Any] = {}
    open_buy_commitment = Decimal("0")
    open_sell_remaining = Decimal("0")
    orders_by_condition: dict[str, set[str]] = {}
    for order in orders:
        order_id = str(getattr(order, "id", "") or "")
        condition_id = str(getattr(order, "market", "") or getattr(order, "condition_id", "") or "")
        asset_id = str(getattr(order, "asset_id", "") or "")
        side = str(getattr(order, "side", "") or "").upper()
        original = _decimal(getattr(order, "original_size", None), "order.original_size")
        matched = _decimal(getattr(order, "size_matched", None), "order.size_matched")
        price = _decimal(getattr(order, "price", None), "order.price")
        if not order_id or not condition_id or not asset_id:
            raise AccountRiskSnapshotIncompleteError(
                "Open-order snapshot contains a record without order, market, or asset ID"
            )
        if side not in {"BUY", "SELL"} or original <= 0 or matched > original:
            raise ValueError("Open-order snapshot contains invalid side or size fields")
        if not Decimal("0") < price < Decimal("1"):
            raise ValueError("Open-order snapshot contains an invalid limit price")
        prior = unique_orders.get(order_id)
        if prior is not None and prior != order:
            raise AccountRiskSnapshotIncompleteError(
                "Open-order snapshot repeated an order ID with conflicting values"
            )
        unique_orders[order_id] = order
        remaining = original - matched
        if side == "BUY":
            open_buy_commitment += remaining * price
        else:
            open_sell_remaining += remaining
        orders_by_condition.setdefault(condition_id, set()).add(asset_id)

    positions = await _collect_positions(client)
    position_value = Decimal("0")
    position_cost = Decimal("0")
    position_risk_exposure = Decimal("0")
    unrealized_pnl = Decimal("0")
    position_assets_by_condition: dict[str, set[str]] = {}
    position_count_by_status: dict[str, int] = {}
    for position in positions:
        condition_id = str(position.condition_id)
        asset_id = str(position.asset_id)
        current_value = _decimal(position.current_value, "position.current_value")
        total_cost = _decimal(position.total_cost_usdc, "position.total_cost_usdc")
        pnl = _decimal(position.unrealized_pnl, "position.unrealized_pnl", allow_negative=True)
        status = str(position.status)
        position_value += current_value
        position_cost += total_cost
        # Use the higher of marked value and deployed cost for each position.
        # Per-position max avoids one winning position masking another position's
        # drawdown when only aggregate totals are compared.
        position_risk_exposure += max(current_value, total_cost)
        unrealized_pnl += pnl
        position_assets_by_condition.setdefault(condition_id, set()).add(asset_id)
        position_count_by_status[status] = position_count_by_status.get(status, 0) + 1

    conservative_available = max(Decimal("0"), collateral_usd - open_buy_commitment)
    projected_exposure = position_risk_exposure + open_buy_commitment
    collateral_shortfall = max(Decimal("0"), open_buy_commitment - collateral_usd)

    return {
        "snapshot_complete": True,
        "captured_at_utc": timestamp.isoformat(),
        "collateral_decimals": collateral_decimals,
        "collateral_balance_base_units": str(raw_balance),
        "collateral_balance_usd": _decimal_text(collateral_usd),
        "open_buy_commitment_usd": _decimal_text(open_buy_commitment),
        "conservative_available_collateral_usd": _decimal_text(conservative_available),
        "collateral_shortfall_usd": _decimal_text(collateral_shortfall),
        "current_open_exposure_usd": _decimal_text(projected_exposure),
        "open_position_value_usd": _decimal_text(position_value),
        "open_position_cost_usd": _decimal_text(position_cost),
        "open_position_risk_exposure_usd": _decimal_text(position_risk_exposure),
        "open_unrealized_pnl_usd": _decimal_text(unrealized_pnl),
        "current_open_positions": len(positions),
        "current_open_orders": len(unique_orders),
        "open_buy_orders_commitment_usd": str(open_buy_commitment),
        "open_sell_remaining_shares": _decimal_text(open_sell_remaining),
        "open_position_assets_by_condition": {
            key: sorted(values) for key, values in sorted(position_assets_by_condition.items())
        },
        "open_order_assets_by_condition": {
            key: sorted(values) for key, values in sorted(orders_by_condition.items())
        },
        "position_count_by_status": dict(sorted(position_count_by_status.items())),
        "execution": "none",
    }
