"""Conservative top-five-level paper fills from observed CLOB books.

These helpers simulate consuming displayed liquidity only. They do not model queue
priority, hidden liquidity, market impact beyond the captured levels, or live fills.
"""
from __future__ import annotations

import math
from typing import Any


def _normalized_levels(raw: Any, *, side: str, max_levels: int) -> list[tuple[float, float]]:
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not isinstance(raw, list) or not raw:
        raise ValueError("visible order-book levels are required")
    if not 1 <= int(max_levels) <= 5:
        raise ValueError("max_levels must be between 1 and 5")

    parsed: list[tuple[float, float]] = []
    for level in raw[:int(max_levels)]:
        if not isinstance(level, dict):
            continue
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(price) or not math.isfinite(size):
            continue
        if not 0.0 < price < 1.0 or size <= 0:
            continue
        parsed.append((price, size))

    parsed.sort(key=lambda item: item[0], reverse=(side == "SELL"))
    if not parsed:
        raise ValueError("no valid visible order-book levels")
    return parsed


def simulate_buy(
    asks: Any,
    *,
    quote_budget: float,
    max_levels: int = 5,
) -> dict[str, Any]:
    """Simulate a marketable BUY using a quote-currency budget across ask levels."""
    try:
        budget = float(quote_budget)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("quote_budget must be finite and positive") from None
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("quote_budget must be finite and positive")

    levels = _normalized_levels(asks, side="BUY", max_levels=max_levels)
    remaining = budget
    shares = 0.0
    spent = 0.0
    used = 0
    best_price = levels[0][0]
    for price, available_shares in levels:
        capacity = price * available_shares
        quote = min(remaining, capacity)
        if quote <= 0:
            break
        shares += quote / price
        spent += quote
        remaining -= quote
        used += 1
        if remaining <= max(1e-10, budget * 1e-10):
            remaining = 0.0
            break

    complete = remaining == 0.0
    average_price = spent / shares if shares > 0 else None
    return {
        "side": "BUY",
        "complete": complete,
        "quote_budget": budget,
        "quote_filled": spent,
        "shares_filled": shares,
        "average_price": average_price,
        "best_price": best_price,
        "price_impact_bps": (
            (average_price - best_price) / best_price * 10_000.0
            if average_price is not None else None
        ),
        "levels_used": used,
        "unfilled_quote": remaining,
        "research_only": True,
        "execution": "simulated_only",
    }


def simulate_sell(
    bids: Any,
    *,
    shares: float,
    max_levels: int = 5,
) -> dict[str, Any]:
    """Simulate a marketable SELL by consuming bid levels for a share quantity."""
    try:
        quantity = float(shares)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("shares must be finite and positive") from None
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("shares must be finite and positive")

    levels = _normalized_levels(bids, side="SELL", max_levels=max_levels)
    remaining = quantity
    filled = 0.0
    proceeds = 0.0
    used = 0
    best_price = levels[0][0]
    for price, available_shares in levels:
        amount = min(remaining, available_shares)
        if amount <= 0:
            break
        filled += amount
        proceeds += amount * price
        remaining -= amount
        used += 1
        if remaining <= max(1e-10, quantity * 1e-10):
            remaining = 0.0
            break

    complete = remaining == 0.0
    average_price = proceeds / filled if filled > 0 else None
    return {
        "side": "SELL",
        "complete": complete,
        "shares_requested": quantity,
        "shares_filled": filled,
        "quote_proceeds": proceeds,
        "average_price": average_price,
        "best_price": best_price,
        "price_impact_bps": (
            (best_price - average_price) / best_price * 10_000.0
            if average_price is not None else None
        ),
        "levels_used": used,
        "unfilled_shares": remaining,
        "research_only": True,
        "execution": "simulated_only",
    }
