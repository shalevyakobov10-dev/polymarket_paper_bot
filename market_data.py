"""Read-only Polymarket CLOB order-book snapshots for research and simulation."""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


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
        "crossed_book": crossed,
        "book_usable": bool(midpoint is not None and not crossed),
        "source": "public_clob_orderbook",
        "research_only": True,
        "execution": "none",
    }


def fetch_order_book(token_id: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Fetch one public order book; no credentials or account access are used."""
    token = str(token_id or "").strip()
    if not token:
        raise ValueError("token_id is required")
    request = Request(
        f"{CLOB_BOOK_URL}?{urlencode({'token_id': token})}",
        headers={"User-Agent": "PolymarketPaperResearchBot/1.0", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"CLOB book API returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    result = summarize_order_book(payload, token_id=token)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def fetch_order_books(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fetch a bounded number of unique token books and isolate per-token failures."""
    try:
        limit = int(os.getenv("ORDERBOOK_TOKENS_PER_POLL", "20"))
    except (TypeError, ValueError):
        limit = 20
    limit = min(100, max(0, limit))
    if limit == 0:
        return {}
    tokens = []
    seen = set()
    for row in rows:
        token = str(row.get("token_id") or "").strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
        if len(tokens) >= limit:
            break
    results: dict[str, dict[str, Any]] = {}
    for token in tokens:
        try:
            results[token] = fetch_order_book(token)
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            results[token] = {
                "token_id": token,
                "observed_at": None,
                "book_usable": False,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "source": "public_clob_orderbook",
                "research_only": True,
                "execution": "none",
            }
        except Exception as exc:
            results[token] = {
                "token_id": token,
                "observed_at": None,
                "book_usable": False,
                "error": f"Unexpected {type(exc).__name__}: {str(exc)[:240]}",
                "source": "public_clob_orderbook",
                "research_only": True,
                "execution": "none",
            }
        time.sleep(0.05)
    return results
