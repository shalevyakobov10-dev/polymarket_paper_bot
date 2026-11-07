"""Read-only live Polymarket Gamma + CLOB snapshots for paper research.

No account credentials, signing, order creation, or execution are used. Gamma
provides the active binary-market catalogue; the public CLOB provides the
current YES/NO top-of-book. A market is omitted unless both outcome books are
usable and fresh. The engine must remain in DRY_RUN mode.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market_data import fetch_order_books
from market_snapshot_provider import validate_market_snapshot

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "PolymarketPaperResearchBot/1.0 (+read-only public market data)"
MAX_GAMMA_RESPONSE_BYTES = 3 * 1024 * 1024


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_active_gamma_markets(
    *,
    page_size: int = 200,
    max_pages: int = 5,
    timeout_seconds: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch a bounded, deduplicated set of active Gamma markets."""
    if not 20 <= int(page_size) <= 500:
        raise ValueError("page_size must be between 20 and 500")
    if not 1 <= int(max_pages) <= 10:
        raise ValueError("max_pages must be between 1 and 10")
    discovered: dict[str, dict[str, Any]] = {}
    for page in range(int(max_pages)):
        query = urlencode({
            "active": "true",
            "closed": "false",
            "limit": int(page_size),
            "offset": page * int(page_size),
        })
        request = Request(
            f"{GAMMA_MARKETS_URL}?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError(f"Gamma API returned HTTP {status}")
            raw = response.read(MAX_GAMMA_RESPONSE_BYTES + 1)
        if len(raw) > MAX_GAMMA_RESPONSE_BYTES:
            raise ValueError("Gamma response exceeded the 3 MB safety limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gamma response was not a list")
        for market in payload:
            if not isinstance(market, dict):
                continue
            market_id = str(market.get("conditionId") or market.get("id") or "").strip()
            if not market_id:
                continue
            discovered.setdefault(market_id, market)
        if len(payload) < int(page_size):
            break
    return list(discovered.values())


def _binary_yes_no_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve exact YES/NO labels to their corresponding CLOB token IDs."""
    market_id = str(market.get("conditionId") or market.get("id") or "").strip()
    question = " ".join(str(market.get("question") or "").split())
    if not market_id or not question:
        return None
    if market.get("active") is False or market.get("closed") is True:
        return None
    if market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
        return None
    outcomes = _list_value(market.get("outcomes"))
    tokens = _list_value(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    normalized = [str(item or "").strip().casefold() for item in outcomes]
    if sorted(normalized) != ["no", "yes"]:
        return None
    mapping = {
        normalized[index]: str(tokens[index] or "").strip()
        for index in range(2)
    }
    yes_token, no_token = mapping["yes"], mapping["no"]
    if not yes_token or not no_token or yes_token == no_token:
        return None
    volume = _finite_number(market.get("volume24hr"))
    return {
        "market_id": market_id,
        "question": question[:500],
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "volume_24h": volume if volume is not None and volume >= 0 else None,
    }


class LiveClobMarketSnapshotProvider:
    """Build current, validated binary-market snapshots from public market data."""

    def __init__(
        self,
        *,
        max_markets: int = 10,
        max_pages: int = 5,
        page_size: int = 200,
        max_book_age_seconds: float = 30.0,
        market_fetcher: Callable[[], list[dict[str, Any]]] | None = None,
        orderbook_fetcher: Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        if not 1 <= int(max_markets) <= 50:
            raise ValueError("max_markets must be between 1 and 50")
        if not math.isfinite(float(max_book_age_seconds)) or not 3 <= float(max_book_age_seconds) <= 180:
            raise ValueError("max_book_age_seconds must be between 3 and 180")
        self.max_markets = int(max_markets)
        self.max_pages = int(max_pages)
        self.page_size = int(page_size)
        self.max_book_age_seconds = float(max_book_age_seconds)
        self.market_fetcher = market_fetcher
        self.orderbook_fetcher = orderbook_fetcher
        self.last_diagnostics: dict[str, Any] = {
            "source": "polymarket_gamma_and_public_clob",
            "markets_discovered": 0,
            "binary_markets_considered": 0,
            "snapshots_returned": 0,
            "markets_skipped": 0,
            "orderbooks_requested": 0,
            "orderbooks_usable": 0,
            "research_only": True,
            "execution": "none",
        }

    def _fetch_markets(self) -> list[dict[str, Any]]:
        if self.market_fetcher is not None:
            return self.market_fetcher()
        return fetch_active_gamma_markets(page_size=self.page_size, max_pages=self.max_pages)

    def _fetch_books(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        fetcher = self.orderbook_fetcher or fetch_order_books
        return fetcher([{"token_id": token} for token in tokens])

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        raw_markets = self._fetch_markets()
        if not isinstance(raw_markets, list):
            raise ValueError("Gamma market fetch must return a list")
        parsed = [
            item for item in (_binary_yes_no_market(row) for row in raw_markets if isinstance(row, dict))
            if item is not None
        ]
        parsed.sort(
            key=lambda row: row["volume_24h"] if row["volume_24h"] is not None else -1.0,
            reverse=True,
        )
        selected = parsed[:self.max_markets]
        tokens = list(dict.fromkeys(
            token
            for market in selected
            for token in (market["yes_token_id"], market["no_token_id"])
        ))
        books = self._fetch_books(tokens) if tokens else {}
        if not isinstance(books, dict):
            raise ValueError("CLOB order-book fetch must return a mapping")

        snapshots: list[dict[str, Any]] = []
        skipped = 0
        usable_books = 0
        for market in selected:
            yes_book = books.get(market["yes_token_id"])
            no_book = books.get(market["no_token_id"])
            if not isinstance(yes_book, dict) or not isinstance(no_book, dict):
                skipped += 1
                continue
            if yes_book.get("book_usable") is not True or no_book.get("book_usable") is not True:
                skipped += 1
                continue

            yes_observed = _timestamp(yes_book.get("observed_at"))
            no_observed = _timestamp(no_book.get("observed_at"))
            if yes_observed is None or no_observed is None:
                skipped += 1
                continue
            # Use the older of the two local receipt timestamps so one delayed
            # outcome book cannot make the combined market snapshot look newer.
            observed = min(yes_observed, no_observed)
            age = (current - observed).total_seconds()
            if age < -10 or age > self.max_book_age_seconds:
                skipped += 1
                continue

            exchange_stamps = [
                _timestamp(yes_book.get("exchange_timestamp")),
                _timestamp(no_book.get("exchange_timestamp")),
            ]
            if any(stamp is not None for stamp in exchange_stamps):
                # If the SDK exposes exchange time for either outcome, require it
                # for both and validate both against the same age ceiling.
                if any(stamp is None for stamp in exchange_stamps):
                    skipped += 1
                    continue
                exchange_is_stale = any(
                    (current - stamp).total_seconds() < -10
                    or (current - stamp).total_seconds() > self.max_book_age_seconds
                    for stamp in exchange_stamps
                    if stamp is not None
                )
                if exchange_is_stale:
                    skipped += 1
                    continue

            fields = (
                (yes_book, "best_bid"), (yes_book, "best_ask"), (yes_book, "midpoint"),
                (no_book, "best_bid"), (no_book, "best_ask"), (no_book, "midpoint"),
            )
            numbers = [_finite_number(book.get(key)) for book, key in fields]
            if any(value is None or not 0.0 < value < 1.0 for value in numbers):
                skipped += 1
                continue
            yes_bid, yes_ask, yes_mid, no_bid, no_ask, no_mid = [float(value) for value in numbers]
            row = {
                **market,
                "observed_at": observed.isoformat(),
                "yes_price": yes_mid,
                "no_price": no_mid,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "yes_spread_bps": yes_book.get("spread_bps"),
                "no_spread_bps": no_book.get("spread_bps"),
                "yes_bid_depth_usd_top5": yes_book.get("bid_depth_usd_top5"),
                "yes_ask_depth_usd_top5": yes_book.get("ask_depth_usd_top5"),
                "no_bid_depth_usd_top5": no_book.get("bid_depth_usd_top5"),
                "no_ask_depth_usd_top5": no_book.get("ask_depth_usd_top5"),
                "yes_book_observed_at": yes_observed.isoformat(),
                "no_book_observed_at": no_observed.isoformat(),
                "yes_book_hash": str(yes_book.get("book_hash") or ""),
                "no_book_hash": str(no_book.get("book_hash") or ""),
                "source": "gamma_plus_public_clob_orderbook",
                "market_data_mode": "read_only_live_public",
            }
            try:
                normalized = validate_market_snapshot(
                    row,
                    max_age_seconds=self.max_book_age_seconds,
                    now=current,
                )
            except ValueError:
                skipped += 1
                continue
            snapshots.append(normalized)
            usable_books += 2

        self.last_diagnostics = {
            "source": "polymarket_gamma_and_public_clob",
            "markets_discovered": len(raw_markets),
            "binary_markets_considered": len(parsed),
            "markets_selected": len(selected),
            "snapshots_returned": len(snapshots),
            "markets_skipped": skipped,
            "orderbooks_requested": len(tokens),
            "orderbooks_usable": usable_books,
            "max_book_age_seconds": self.max_book_age_seconds,
            "oldest_snapshot_age_seconds": round(
                max((row["age_seconds"] for row in snapshots), default=0.0), 3
            ),
            "research_only": True,
            "execution": "none",
        }
        if not snapshots:
            raise RuntimeError(
                "No active binary markets had two complete, fresh, valid CLOB books; "
                f"diagnostics={json.dumps(self.last_diagnostics, sort_keys=True)}"
            )
        return snapshots


def provider_from_env(
    *,
    local_path: str,
    local_max_age_seconds: float,
) -> Any:
    """Select the default local replay provider or explicit read-only public CLOB mode."""
    source = os.getenv("PAPER_MARKET_DATA_SOURCE", "local_json").strip().lower()
    if source == "local_json":
        from market_snapshot_provider import JsonMarketSnapshotProvider
        return JsonMarketSnapshotProvider(local_path, max_age_seconds=local_max_age_seconds)
    if source != "polymarket_public":
        raise ValueError("PAPER_MARKET_DATA_SOURCE must be local_json or polymarket_public")
    max_markets = _bounded_int_env("PAPER_LIVE_MARKET_MAX_MARKETS", 10, 1, 50)
    max_book_age = float(os.getenv("PAPER_LIVE_MARKET_MAX_BOOK_AGE_SECONDS", "30"))
    return LiveClobMarketSnapshotProvider(
        max_markets=max_markets,
        max_book_age_seconds=max_book_age,
    )
