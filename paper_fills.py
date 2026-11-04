"""Depth-aware paper fill simulation against public CLOB order-book levels.

This module only estimates hypothetical fills. It never submits, signs, or
routes orders to Polymarket or any account.
"""
from __future__ import annotations

import math
from typing import Any


def _positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def simulate_orderbook_fill(
    orderbook: dict[str, Any] | None,
    *,
    side: str,
    target_notional_usd: float | None = None,
    target_shares: float | None = None,
    max_levels: int = 5,
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Walk visible asks for buys or bids for sells and return a VWAP estimate.

    A fill is eligible only when the complete requested size is visible in the
    selected top levels. Partial depth is reported but never treated as a full
    fill. Real queue position, hidden liquidity, latency and market impact beyond
    the displayed book are not modelled.
    """
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    if not isinstance(max_levels, int) or max_levels < 1 or max_levels > 20:
        raise ValueError("max_levels must be an integer between 1 and 20")

    requested_notional = _positive(target_notional_usd) if target_notional_usd is not None else None
    requested_shares = _positive(target_shares) if target_shares is not None else None
    parsed_limit_price = _positive(limit_price) if limit_price is not None else None
    if limit_price is not None and (
        parsed_limit_price is None or parsed_limit_price >= 1.0
    ):
        raise ValueError("limit_price must be finite and in the open interval (0, 1)")
    if normalized_side == "buy":
        if requested_notional is None or requested_shares is not None:
            raise ValueError("buy simulation requires only a positive target_notional_usd")
    else:
        if requested_shares is None or requested_notional is not None:
            raise ValueError("sell simulation requires only a positive target_shares")

    levels_key = "asks_top5" if normalized_side == "buy" else "bids_top5"
    raw_levels = orderbook.get(levels_key) if isinstance(orderbook, dict) else None
    if not isinstance(raw_levels, list) or not raw_levels:
        return {
            "eligible": False,
            "reason": "orderbook_levels_missing",
            "side": normalized_side,
            "requested_notional_usd": requested_notional,
            "requested_shares": requested_shares,
            "filled_notional_usd": 0.0,
            "filled_shares": 0.0,
            "average_price": None,
            "levels_used": 0,
            "limit_price": parsed_limit_price,
            "execution": "none",
            "research_only": True,
        }

    parsed_levels: list[tuple[float, float]] = []
    for item in raw_levels[:max_levels]:
        if not isinstance(item, dict):
            continue
        price = _positive(item.get("price"))
        size = _positive(item.get("size"))
        if price is None or size is None or price >= 1.0:
            continue
        parsed_levels.append((price, size))

    parsed_levels.sort(key=lambda item: item[0], reverse=normalized_side == "sell")

    # Reject internally inconsistent snapshots before simulating a fill. The
    # top level in the raw depth must agree with the book's declared best price.
    if isinstance(orderbook, dict) and parsed_levels:
        declared_key = "best_ask" if normalized_side == "buy" else "best_bid"
        if declared_key in orderbook:
            declared_best = _positive(orderbook.get(declared_key))
            actual_best = parsed_levels[0][0]
            if declared_best is None or abs(declared_best - actual_best) > 1e-8:
                return {
                    "eligible": False,
                    "reason": "orderbook_top_level_mismatch",
                    "side": normalized_side,
                    "requested_notional_usd": requested_notional,
                    "requested_shares": requested_shares,
                    "filled_notional_usd": 0.0,
                    "filled_shares": 0.0,
                    "average_price": None,
                    "levels_used": 0,
                    "limit_price": parsed_limit_price,
                    "execution": "none",
                    "research_only": True,
                }

    # A FOK-style limit simulation only consumes levels at or better than the
    # caller's hard price cap. Insufficient depth within the cap is not a fill.
    if parsed_limit_price is not None:
        if normalized_side == "buy":
            parsed_levels = [item for item in parsed_levels if item[0] <= parsed_limit_price + 1e-12]
        else:
            parsed_levels = [item for item in parsed_levels if item[0] >= parsed_limit_price - 1e-12]

    remaining = requested_notional if normalized_side == "buy" else requested_shares
    filled_shares = 0.0
    filled_notional = 0.0
    levels_used = 0
    worst_price = None

    for price, available_shares in parsed_levels:
        if remaining is None or remaining <= 1e-10:
            break
        if normalized_side == "buy":
            quantity = min(available_shares, remaining / price)
            notional = quantity * price
            remaining -= notional
        else:
            quantity = min(available_shares, remaining)
            notional = quantity * price
            remaining -= quantity
        if quantity <= 0:
            continue
        filled_shares += quantity
        filled_notional += notional
        levels_used += 1
        worst_price = price

    average_price = filled_notional / filled_shares if filled_shares > 0 else None
    complete = remaining is not None and remaining <= 1e-8
    if normalized_side == "buy":
        unfilled_notional = max(0.0, float(remaining or 0.0))
        unfilled_shares = None
        reason = "filled_visible_ask_depth" if complete else "insufficient_visible_ask_depth"
    else:
        unfilled_shares = max(0.0, float(remaining or 0.0))
        unfilled_notional = None
        reason = "filled_visible_bid_depth" if complete else "insufficient_visible_bid_depth"

    return {
        "eligible": complete,
        "reason": reason,
        "side": normalized_side,
        "requested_notional_usd": round(requested_notional, 8) if requested_notional is not None else None,
        "requested_shares": round(requested_shares, 12) if requested_shares is not None else None,
        "filled_notional_usd": round(filled_notional, 8),
        "filled_shares": round(filled_shares, 12),
        "average_price": round(average_price, 10) if average_price is not None else None,
        "worst_price": worst_price,
        "limit_price": round(parsed_limit_price, 10) if parsed_limit_price is not None else None,
        "levels_used": levels_used,
        "unfilled_notional_usd": round(unfilled_notional, 8) if unfilled_notional is not None else None,
        "unfilled_shares": round(unfilled_shares, 12) if unfilled_shares is not None else None,
        "method": "walk_visible_top_levels_vwap",
        "execution": "none",
        "research_only": True,
    }
