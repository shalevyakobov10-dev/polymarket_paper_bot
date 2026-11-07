"""Read-only live market snapshots from public Gamma metadata and CLOB books.

This provider discovers active binary YES/NO markets, fetches public order books,
and emits only fresh, two-sided snapshots suitable for offline paper research.
It has no credentials, signing, or order-submission code.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market_data import fetch_order_books

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _outcome_indices(market: dict[str, Any]) -> tuple[int, int] | None:
    outcomes = [str(value).strip().casefold() for value in _list_field(market.get("outcomes"))]
    yes = [index for index, value in enumerate(outcomes) if value in {"yes", "true"}]
    no = [index for index, value in enumerate(outcomes) if value in {"no", "false"}]
    if len(yes) != 1 or len(no) != 1:
        return None
    return yes[0], no[0]


class LiveClobMarketSnapshotProvider:
    """Fetch bounded public Gamma markets and current CLOB books for paper use."""

    def __init__(
        self,
        *,
        max_age_seconds: float = 30.0,
        market_limit: int | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.max_age_seconds = float(max_age_seconds)
        self.market_limit = market_limit or _bounded_int_env("PAPER_LIVE_MARKETS_LIMIT", 10, 1, 50)
        self.timeout_seconds = max(2.0, min(30.0, float(timeout_seconds)))

    def _discover_markets(self) -> list[dict[str, Any]]:
        limit = _bounded_int_env("PAPER_GAMMA_PAGE_SIZE", 100, 20, 200)
        query = urlencode({
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": 0,
            "order": "volume24hr",
            "ascending": "false",
        })
        request = Request(
            f"{GAMMA_MARKETS_URL}?{query}",
            headers={"User-Agent": "PolymarketPaperResearchBot/1.0"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError("Gamma API returned a non-200 response")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gamma market response must be a list")
        return [row for row in payload if isinstance(row, dict)]

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for market in self._discover_markets():
            market_id = str(market.get("conditionId") or market.get("id") or "").strip()
            question = str(market.get("question") or "").strip()
            tokens = _list_field(market.get("clobTokenIds"))
            prices = _list_field(market.get("outcomePrices"))
            indices = _outcome_indices(market)
            if not market_id or not question or indices is None:
                continue
            yes_index, no_index = indices
            if max(yes_index, no_index) >= len(tokens) or max(yes_index, no_index) >= len(prices):
                continue
            yes_token = str(tokens[yes_index] or "").strip()
            no_token = str(tokens[no_index] or "").strip()
            if not yes_token or not no_token or yes_token == no_token:
                continue
            if market_id in seen:
                continue
            seen.add(market_id)
            candidates.append({
                "market_id": market_id,
                "question": question[:500],
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "volume_24h": _number(market.get("volume24hr")),
                "gamma_yes_price": _number(prices[yes_index]),
                "gamma_no_price": _number(prices[no_index]),
            })
            if len(candidates) >= self.market_limit:
                break

        token_rows = [
            {"token_id": token}
            for row in candidates
            for token in (row["yes_token_id"], row["no_token_id"])
        ]
        books = fetch_order_books(token_rows) if token_rows else {}
        snapshots: list[dict[str, Any]] = []
        for market in candidates:
            yes_book = books.get(market["yes_token_id"])
            no_book = books.get(market["no_token_id"])
            if not isinstance(yes_book, dict) or not isinstance(no_book, dict):
                continue
            if yes_book.get("book_usable") is not True or no_book.get("book_usable") is not True:
                continue
            yes_bid, yes_ask = _number(yes_book.get("best_bid")), _number(yes_book.get("best_ask"))
            no_bid, no_ask = _number(no_book.get("best_bid")), _number(no_book.get("best_ask"))
            if any(value is None for value in (yes_bid, yes_ask, no_bid, no_ask)):
                continue
            yes_price = (yes_bid + yes_ask) / 2.0
            no_price = (no_bid + no_ask) / 2.0
            observed_values = [yes_book.get("observed_at"), no_book.get("observed_at")]
            try:
                observed = [
                    datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
                    for value in observed_values
                ]
            except (TypeError, ValueError, OverflowError):
                continue
            ages = [(current - stamp).total_seconds() for stamp in observed]
            if any(age < -5.0 or age > self.max_age_seconds for age in ages):
                continue
            snapshots.append({
                "market_id": market["market_id"],
                "question": market["question"],
                "yes_token_id": market["yes_token_id"],
                "no_token_id": market["no_token_id"],
                "observed_at": min(observed).isoformat(),
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "volume_24h": market["volume_24h"],
                "yes_book": yes_book,
                "no_book": no_book,
                "book_source": "official_polymarket_python_sdk",
                "market_source": "public_gamma_api",
                "research_only": True,
                "execution": "none",
            })
        return snapshots
