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


def _utc_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


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
        self.market_limit = (
            market_limit
            if market_limit is not None
            else _bounded_int_env("PAPER_LIVE_MARKETS_LIMIT", 30, 1, 50)
        )
        if isinstance(self.market_limit, bool) or not isinstance(self.market_limit, int) or not 1 <= self.market_limit <= 50:
            raise ValueError("market_limit must be an integer between 1 and 50")
        self.gamma_page_size = _bounded_int_env("PAPER_GAMMA_PAGE_SIZE", 200, 20, 200)
        self.gamma_discovery_pages = _bounded_int_env("PAPER_GAMMA_DISCOVERY_PAGES", 3, 1, 5)
        self.max_markets_per_category = _bounded_int_env("PAPER_MARKET_CATEGORY_MAX", 8, 1, 50)
        self.timeout_seconds = max(2.0, min(30.0, float(timeout_seconds)))
        self.last_discovered_market_count = 0
        self.last_selected_market_count = 0
        self.last_orderbook_token_requests = 0
        self.last_usable_market_count = 0
        self.last_selected_categories: list[str] = []
        self.last_discovery_pages_attempted = 0
        self.last_discovery_pages_succeeded = 0
        self.last_discovery_pages_failed = 0

    def _discover_markets(self) -> list[dict[str, Any]]:
        """Read a bounded multi-page Gamma sample; isolate page failures.

        Gamma is sorted by reported 24-hour volume. The bounded pages improve
        category breadth but are not the full active market universe.
        """
        limit = self.gamma_page_size
        pages = self.gamma_discovery_pages
        combined: dict[str, dict[str, Any]] = {}
        failures = 0
        succeeded = 0
        attempted = 0
        for page in range(pages):
            offset = page * limit
            query = urlencode({
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            })
            request = Request(
                f"{GAMMA_MARKETS_URL}?{query}",
                headers={"User-Agent": "PolymarketPaperResearchBot/1.0"},
            )
            attempted += 1
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    if getattr(response, "status", 200) != 200:
                        raise RuntimeError("Gamma API returned a non-200 response")
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("Gamma market response must be a list")
                page_rows = [row for row in payload if isinstance(row, dict)]
                succeeded += 1
                for row in page_rows:
                    identity = str(row.get("conditionId") or row.get("id") or "").strip()
                    if identity:
                        combined.setdefault(identity, row)
                # No further page is available when the final result is short.
                if len(payload) < limit:
                    break
            except Exception:
                # Keep successful pages, but expose an incomplete-scan count.
                failures += 1

        self.last_discovery_pages_attempted = attempted
        self.last_discovery_pages_succeeded = succeeded
        self.last_discovery_pages_failed = failures
        if succeeded == 0:
            raise RuntimeError("Gamma market discovery failed for every configured page")

        rows = list(combined.values())
        rows.sort(
            key=lambda row: _number(row.get("volume24hr")) or 0.0,
            reverse=True,
        )
        return rows

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)

        discovered_markets = self._discover_markets()
        self.last_discovered_market_count = len(discovered_markets)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for market in discovered_markets:
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
                "market_category": str(market.get("category") or "uncategorized").strip().lower()[:80],
                "market_slug": str(market.get("slug") or "")[:160],
                "market_end_date": str(market.get("endDate") or market.get("endDateIso") or "")[:80],
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "volume_24h": _number(market.get("volume24hr")),
                "liquidity": _number(market.get("liquidity")),
                "gamma_yes_price": _number(prices[yes_index]),
                "gamma_no_price": _number(prices[no_index]),
            })
        # Keep a bounded category-diverse watchlist: first take up to the
        # configured quota from each category in volume order, then fill any
        # remaining slots with the highest-volume candidates overall.
        candidates_by_volume = sorted(
            candidates,
            key=lambda row: row.get("volume_24h") or 0.0,
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        category_counts: dict[str, int] = {}
        for row in candidates_by_volume:
            category = str(row.get("market_category") or "uncategorized")
            if category_counts.get(category, 0) >= self.max_markets_per_category:
                continue
            selected.append(row)
            selected_ids.add(row["market_id"])
            category_counts[category] = category_counts.get(category, 0) + 1
            if len(selected) >= self.market_limit:
                break
        if len(selected) < self.market_limit:
            for row in candidates_by_volume:
                if row["market_id"] in selected_ids:
                    continue
                selected.append(row)
                selected_ids.add(row["market_id"])
                if len(selected) >= self.market_limit:
                    break
        candidates = selected
        self.last_selected_market_count = len(candidates)
        self.last_selected_categories = sorted({
            str(row.get("market_category") or "uncategorized")
            for row in candidates
        })
        token_rows = [
            {"token_id": token}
            for row in candidates
            for token in (row["yes_token_id"], row["no_token_id"])
        ]
        self.last_orderbook_token_requests = len(token_rows)
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
            observed = [_utc_timestamp(value) for value in observed_values]
            if any(stamp is None for stamp in observed):
                continue
            received_ages = [(current - stamp).total_seconds() for stamp in observed]
            if any(age < -5.0 or age > self.max_age_seconds for age in received_ages):
                continue

            # A recently received response can still contain an old cached book.
            # When the SDK supplies the venue timestamp, enforce the same freshness
            # bound against it. Missing venue timestamps remain explicitly absent;
            # malformed, timezone-naive, stale, or future-dated values fail closed.
            exchange_stamps: list[datetime | None] = []
            exchange_valid = True
            for book in (yes_book, no_book):
                raw_exchange_stamp = book.get("exchange_timestamp")
                if raw_exchange_stamp is None or not str(raw_exchange_stamp).strip():
                    exchange_stamps.append(None)
                    continue
                exchange_stamp = _utc_timestamp(raw_exchange_stamp)
                if exchange_stamp is None:
                    exchange_valid = False
                    break
                exchange_age = (current - exchange_stamp).total_seconds()
                if exchange_age < -5.0 or exchange_age > self.max_age_seconds:
                    exchange_valid = False
                    break
                exchange_stamps.append(exchange_stamp)
            if not exchange_valid:
                continue
            snapshots.append({
                "market_id": market["market_id"],
                "question": market["question"],
                "market_category": market["market_category"],
                "market_slug": market["market_slug"],
                "market_end_date": market["market_end_date"],
                "liquidity": market["liquidity"],
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
        self.last_usable_market_count = len(snapshots)
        return snapshots
