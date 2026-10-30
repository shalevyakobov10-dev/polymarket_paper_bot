"""Conservative public CLOB book-quality checks for the paper simulator."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _finite_nonnegative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_order_book_quality(
    orderbook: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 180.0,
    max_spread_bps: float = 1000.0,
    min_side_depth_usd: float = 5.0,
    require_orderbook: bool = True,
) -> dict[str, Any]:
    """Check freshness, two-sided integrity, spread and visible depth.

    The function only evaluates a public snapshot. It does not estimate a fill,
    reconstruct hidden liquidity, or send an order.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    if orderbook is None:
        reason = "missing_orderbook" if require_orderbook else "orderbook_not_required"
        return {
            "eligible": not require_orderbook,
            "reason": reason,
            "book_present": False,
            "age_seconds": None,
            "research_only": True,
            "execution": "none",
        }
    if not isinstance(orderbook, dict):
        return {
            "eligible": False,
            "reason": "invalid_orderbook_payload",
            "book_present": True,
            "age_seconds": None,
            "research_only": True,
            "execution": "none",
        }

    base = {
        "book_present": True,
        "token_id": str(orderbook.get("token_id") or ""),
        "spread_bps": _finite_nonnegative(orderbook.get("spread_bps")),
        "bid_depth_usd_top5": _finite_nonnegative(orderbook.get("bid_depth_usd_top5")),
        "ask_depth_usd_top5": _finite_nonnegative(orderbook.get("ask_depth_usd_top5")),
        "research_only": True,
        "execution": "none",
    }

    def reject(reason: str, age_seconds: float | None = None) -> dict[str, Any]:
        return {
            **base,
            "eligible": False,
            "reason": reason,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        }

    if orderbook.get("error"):
        return reject("orderbook_fetch_failed")
    if orderbook.get("crossed_book"):
        return reject("crossed_orderbook")

    observed_at = _parse_timestamp(orderbook.get("observed_at"))
    if observed_at is None:
        return reject("invalid_orderbook_timestamp")
    age_seconds = (current - observed_at).total_seconds()
    if age_seconds < -5.0:
        return reject("future_orderbook_timestamp", age_seconds)
    if age_seconds > max_age_seconds:
        return reject("stale_orderbook", age_seconds)

    best_bid = _finite_nonnegative(orderbook.get("best_bid"))
    best_ask = _finite_nonnegative(orderbook.get("best_ask"))
    if (
        orderbook.get("book_usable") is not True
        or best_bid is None
        or best_ask is None
        or not 0.0 < best_bid < best_ask < 1.0
    ):
        return reject("unusable_two_sided_orderbook", age_seconds)

    spread_bps = base["spread_bps"]
    if spread_bps is None:
        midpoint = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / midpoint) * 10000.0
        base["spread_bps"] = round(spread_bps, 2)
    if spread_bps > max_spread_bps:
        return reject("spread_too_wide", age_seconds)

    bid_depth = base["bid_depth_usd_top5"]
    ask_depth = base["ask_depth_usd_top5"]
    if bid_depth is None or bid_depth < min_side_depth_usd:
        return reject("insufficient_bid_depth", age_seconds)
    if ask_depth is None or ask_depth < min_side_depth_usd:
        return reject("insufficient_ask_depth", age_seconds)

    return {
        **base,
        "eligible": True,
        "reason": "approved_market_quality",
        "age_seconds": round(max(0.0, age_seconds), 3),
        "max_age_seconds": float(max_age_seconds),
        "max_spread_bps": float(max_spread_bps),
        "min_side_depth_usd": float(min_side_depth_usd),
    }
