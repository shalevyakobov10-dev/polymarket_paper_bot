"""Pure fail-closed preflight checks for a possible future live order path.

This module never creates clients, reads secrets, signs, or submits orders.
A passing preflight is not launch authorization.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any


LIVE_ORDER_SUBMISSION_IMPLEMENTED = False
REQUIRED_RELEASE_GATES = (
    "account_eligibility_verified",
    "jurisdiction_and_legal_review_approved",
    "strategy_validation_passed",
    "independent_security_review_passed",
    "production_operations_verified",
    "explicit_release_approved",
)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def evaluate_live_order_preflight(
    *,
    token_id: str,
    side: str,
    limit_price: float,
    size: float,
    best_ask: float,
    tick_size: float,
    minimum_order_size: float,
    book_age_seconds: float,
    max_book_age_seconds: float,
    max_price_impact_bps: float,
    max_order_notional_usd: float,
    available_collateral_usd: float,
    estimated_fees_usd: float,
    current_open_positions: int,
    max_open_positions: int,
    opposing_market_outcome_open: bool,
    current_open_exposure_usd: float,
    max_open_exposure_usd: float,
    daily_realized_pnl_usd: float,
    open_unrealized_pnl_usd: float,
    daily_loss_limit_usd: float,
    current_drawdown_pct: float,
    max_drawdown_pct: float,
    market_active: bool,
    kill_switch_engaged: bool,
    release_gates: dict[str, bool] | None,
) -> dict[str, Any]:
    """Validate a proposed BUY limit order; never authorize order submission."""
    blockers: list[str] = []

    if not isinstance(token_id, str) or not token_id.strip():
        blockers.append("missing_token_id")
    if side != "BUY":
        blockers.append("unsupported_order_side")

    price = _decimal(limit_price)
    size_value = _decimal(size)
    ask = _decimal(best_ask)
    tick = _decimal(tick_size)
    minimum = _decimal(minimum_order_size)
    book_age = _decimal(book_age_seconds)
    max_book_age = _decimal(max_book_age_seconds)
    impact_bps = _decimal(max_price_impact_bps)
    max_order = _decimal(max_order_notional_usd)
    collateral = _decimal(available_collateral_usd)
    estimated_fees = _decimal(estimated_fees_usd)
    exposure = _decimal(current_open_exposure_usd)
    max_exposure = _decimal(max_open_exposure_usd)
    realized = _decimal(daily_realized_pnl_usd)
    unrealized = _decimal(open_unrealized_pnl_usd)
    daily_loss_limit = _decimal(daily_loss_limit_usd)
    drawdown = _decimal(current_drawdown_pct)
    max_drawdown = _decimal(max_drawdown_pct)

    values = {
        "limit_price": price, "size": size_value, "best_ask": ask,
        "tick_size": tick, "minimum_order_size": minimum,
        "book_age_seconds": book_age, "max_book_age_seconds": max_book_age,
        "max_price_impact_bps": impact_bps, "max_order_notional_usd": max_order,
        "available_collateral_usd": collateral, "estimated_fees_usd": estimated_fees,
        "current_open_exposure_usd": exposure, "max_open_exposure_usd": max_exposure,
        "daily_realized_pnl_usd": realized, "open_unrealized_pnl_usd": unrealized,
        "daily_loss_limit_usd": daily_loss_limit, "current_drawdown_pct": drawdown,
        "max_drawdown_pct": max_drawdown,
    }
    for name, value in values.items():
        if value is None:
            blockers.append(f"invalid_{name}")

    if price is not None and not Decimal("0") < price < Decimal("1"):
        blockers.append("limit_price_out_of_range")
    if size_value is not None and size_value <= 0:
        blockers.append("invalid_order_size")
    if tick is not None and tick <= 0:
        blockers.append("invalid_tick_size")
    if price is not None and tick is not None and tick > 0:
        if price % tick != 0:
            blockers.append("limit_price_not_on_tick")
    if size_value is not None and minimum is not None and minimum > 0:
        if size_value < minimum:
            blockers.append("below_minimum_order_size")
    if ask is not None and not Decimal("0") < ask < Decimal("1"):
        blockers.append("invalid_best_ask")
    if book_age is not None and max_book_age is not None:
        if book_age < 0 or max_book_age <= 0 or book_age > max_book_age:
            blockers.append("stale_or_invalid_order_book")
    if impact_bps is not None and impact_bps < 0:
        blockers.append("invalid_price_impact_limit")
    if price is not None and ask is not None and impact_bps is not None and ask > 0 and impact_bps >= 0:
        maximum_price = ask * (Decimal("1") + impact_bps / Decimal("10000"))
        if price > maximum_price:
            blockers.append("price_impact_limit_exceeded")

    if max_order is not None and max_order <= 0:
        blockers.append("invalid_max_order_notional")
    if collateral is not None and collateral < 0:
        blockers.append("invalid_available_collateral")
    if estimated_fees is not None and estimated_fees < 0:
        blockers.append("invalid_estimated_fees")
    if (
        price is not None and size_value is not None
        and collateral is not None and estimated_fees is not None
        and collateral >= 0 and estimated_fees >= 0
        and price * size_value + estimated_fees > collateral
    ):
        blockers.append("insufficient_available_collateral_after_fees")
    if (
        isinstance(current_open_positions, bool)
        or not isinstance(current_open_positions, int)
        or isinstance(max_open_positions, bool)
        or not isinstance(max_open_positions, int)
        or current_open_positions < 0
        or max_open_positions < 1
    ):
        blockers.append("invalid_open_position_count")
    elif current_open_positions >= max_open_positions:
        blockers.append("max_open_positions_reached")
    if opposing_market_outcome_open is True:
        blockers.append("opposing_market_outcome_already_open")
    elif opposing_market_outcome_open is not False:
        blockers.append("invalid_opposing_market_outcome_flag")
    if price is not None and size_value is not None and max_order is not None and max_order > 0:
        if price * size_value > max_order:
            blockers.append("max_order_notional_exceeded")
    if exposure is not None and max_exposure is not None:
        if exposure < 0 or max_exposure <= 0 or exposure > max_exposure:
            blockers.append("invalid_or_exceeded_open_exposure")
        elif price is not None and size_value is not None and exposure + price * size_value > max_exposure:
            blockers.append("max_open_exposure_exceeded")
    if daily_loss_limit is not None and daily_loss_limit <= 0:
        blockers.append("invalid_daily_loss_limit")
    if realized is not None and unrealized is not None and daily_loss_limit is not None and daily_loss_limit > 0:
        if realized + unrealized <= -daily_loss_limit:
            blockers.append("daily_loss_limit_reached")
    if drawdown is not None and max_drawdown is not None:
        if drawdown < 0 or max_drawdown <= 0 or max_drawdown > 100:
            blockers.append("invalid_drawdown_limit")
        elif drawdown >= max_drawdown:
            blockers.append("max_drawdown_reached")

    if market_active is not True:
        blockers.append("market_not_confirmed_active")
    if kill_switch_engaged is not False:
        blockers.append("kill_switch_engaged")

    gates = release_gates if isinstance(release_gates, dict) else {}
    for gate in REQUIRED_RELEASE_GATES:
        if gates.get(gate) is not True:
            blockers.append(f"release_gate_not_verified:{gate}")

    # De-duplicate while preserving deterministic blocker order.
    blockers = list(dict.fromkeys(blockers))
    checks_passed = not blockers
    return {
        "mode": "preflight_only",
        "status": "checks_passed_not_authorized" if checks_passed else "blocked",
        "preflight_passed": checks_passed,
        "blockers": blockers,
        "order_submission_implemented": LIVE_ORDER_SUBMISSION_IMPLEMENTED,
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "execution": "none",
        "order_notional_usd": (
            float(price * size_value)
            if price is not None and size_value is not None
            and price > 0 and size_value > 0 else None
        ),
        "release_gates_required": list(REQUIRED_RELEASE_GATES),
    }
