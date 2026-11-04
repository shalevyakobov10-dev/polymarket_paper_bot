"""Read-only Polymarket CLOB order-book snapshots via the official Python SDK."""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError

from polymarket import PublicClient


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def summarize_order_book(payload: dict[str, Any], *, token_id: str, observed_at: str | None = None) -> dict[str, Any]:
    """Extract best bid/ask, spread, top-five depth and imbalance from a CLOB book."""
    if not isinstance(payload, dict):
        raise ValueError("Order-book payload must be an object")

    def levels(name: str, reverse: bool) -> list[tuple[float, float]]:
        raw = payload.get(name)
        if not isinstance(raw, list):
            raise ValueError(f"Order-book {name} must be a list")
        parsed = []
        for level in raw:
            if not isinstance(level, dict):
                continue
            price, size = _number(level.get("price")), _number(level.get("size"))
            if price is None or size is None or not 0.0 < price < 1.0 or size <= 0:
                continue
            parsed.append((price, size))
        return sorted(parsed, key=lambda item: item[0], reverse=reverse)

    bids = levels("bids", True)
    asks = levels("asks", False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    crossed = best_bid is not None and best_ask is not None and best_bid >= best_ask
    midpoint = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None and not crossed else None
    spread = best_ask - best_bid if midpoint is not None else None
    bid_depth = sum(price * size for price, size in bids[:5])
    ask_depth = sum(price * size for price, size in asks[:5])
    total_depth = bid_depth + ask_depth
    # Persist only the five best visible levels needed for conservative paper fills.
    bids_top5 = [{"price": price, "size": size} for price, size in bids[:5]]
    asks_top5 = [{"price": price, "size": size} for price, size in asks[:5]]
    return {
        "token_id": str(token_id),
        "observed_at": observed_at,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": midpoint,
        "spread": spread,
        "spread_bps": round(spread / midpoint * 10000, 2) if spread is not None and midpoint else None,
        "bid_depth_usd_top5": round(bid_depth, 6),
        "ask_depth_usd_top5": round(ask_depth, 6),
        "depth_imbalance_top5": round((bid_depth - ask_depth) / total_depth, 6) if total_depth > 0 else None,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "bids_top5": bids_top5,
        "asks_top5": asks_top5,
        "crossed_book": crossed,
        "book_usable": bool(midpoint is not None and not crossed),
        "source": "public_clob_orderbook",
        "research_only": True,
        "execution": "none",
    }


def fetch_order_book(token_id: str) -> dict[str, Any]:
    """Fetch one public CLOB book through the official unauthenticated SDK client."""
    token = str(token_id or "").strip()
    if not token:
        raise ValueError("token_id is required")

    # PublicClient never reads wallet credentials and exposes no signing or
    # order-submission capability. The context manager closes its HTTP transports.
    with PublicClient() as client:
        book = client.get_order_book(token_id=token)

    payload = {
        "bids": [
            {"price": str(level.price), "size": str(level.size)}
            for level in book.bids
        ],
        "asks": [
            {"price": str(level.price), "size": str(level.size)}
            for level in book.asks
        ],
    }
    observed_at = datetime.now(timezone.utc).isoformat()
    result = summarize_order_book(payload, token_id=token, observed_at=observed_at)
    exchange_timestamp = getattr(book, "timestamp", None)
    result.update({
        "source": "official_polymarket_python_sdk",
        "sdk_version": "0.16.0",
        "exchange_timestamp": (
            exchange_timestamp.isoformat()
            if hasattr(exchange_timestamp, "isoformat") else None
        ),
        "book_hash": str(getattr(book, "hash", "") or ""),
        "min_order_size": str(getattr(book, "min_order_size", "") or ""),
        "tick_size": str(getattr(book, "tick_size", "") or ""),
        "neg_risk": bool(getattr(book, "neg_risk", False)),
    })
    return result


def fetch_order_books(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fetch a bounded batch concurrently while isolating per-token failures.

    Concurrency is deliberately capped to avoid turning a polling cycle into an
    unbounded burst against the public CLOB API. Result keys retain input token
    order even when requests complete out of order.
    """
    def bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return min(maximum, max(minimum, value))

    limit = bounded_env("ORDERBOOK_TOKENS_PER_POLL", 20, 0, 100)
    if limit == 0:
        return {}
    workers = bounded_env("ORDERBOOK_FETCH_WORKERS", 4, 1, 8)
    tokens = []
    seen = set()
    for row in rows:
        token = str(row.get("token_id") or "").strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
        if len(tokens) >= limit:
            break

    def fetch_one(token: str) -> tuple[str, dict[str, Any]]:
        try:
            result = fetch_order_book(token)
            if not isinstance(result, dict):
                raise ValueError("CLOB order-book fetch returned a non-object result")
            return token, result
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            return token, {
                "token_id": token,
                "observed_at": None,
                "book_usable": False,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "source": "public_clob_orderbook",
                "research_only": True,
                "execution": "none",
            }
        except Exception as exc:
            return token, {
                "token_id": token,
                "observed_at": None,
                "book_usable": False,
                "error": f"Unexpected {type(exc).__name__}: {str(exc)[:240]}",
                "source": "public_clob_orderbook",
                "research_only": True,
                "execution": "none",
            }

    completed: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(tokens)), thread_name_prefix="clob-book") as pool:
        futures = [pool.submit(fetch_one, token) for token in tokens]
        for future in futures:
            token, result = future.result()
            completed[token] = result
    return {token: completed[token] for token in tokens}
