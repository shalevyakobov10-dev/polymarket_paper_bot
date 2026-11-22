"""Read-only live market snapshots from public Gamma metadata and CLOB books.

This provider discovers bounded pages of active binary YES/NO markets, fetches
public order books, and emits only fresh, validated snapshots for paper research.
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


def _list_field(value: Any) -> list[Any]:
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


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _utc_timestamp(value: Any) -> datetime | None:
    """Parse an explicit UTC offset; never guess a timezone for naive input."""
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
    if len(outcomes) != 2:
        return None
    yes = [index for index, value in enumerate(outcomes) if value in {"yes", "true"}]
    no = [index for index, value in enumerate(outcomes) if value in {"no", "false"}]
    if len(yes) != 1 or len(no) != 1:
        return None
    return yes[0], no[0]


def fetch_active_gamma_markets(
    *,
    page_size: int = 100,
    max_pages: int = 5,
    timeout_seconds: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch a bounded, deduplicated set of active Gamma markets.

    Every page has a strict response-size cap. Pagination stops when Gamma
    returns a short page, and the caller separately caps the selected markets.
    """
    if not 20 <= int(page_size) <= 200:
        raise ValueError("page_size must be between 20 and 200")
    if not 1 <= int(max_pages) <= 10:
        raise ValueError("max_pages must be between 1 and 10")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout_seconds must be finite and between 2 and 30") from None
    if not math.isfinite(timeout) or not 2 <= timeout <= 30:
        raise ValueError("timeout_seconds must be finite and between 2 and 30")

    discovered: dict[str, dict[str, Any]] = {}
    for page in range(int(max_pages)):
        query = urlencode({
            "active": "true",
            "closed": "false",
            "limit": int(page_size),
            "offset": page * int(page_size),
            "order": "volume24hr",
            "ascending": "false",
        })
        request = Request(
            f"{GAMMA_MARKETS_URL}?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError(f"Gamma API returned HTTP {status}")
            raw = response.read(MAX_GAMMA_RESPONSE_BYTES + 1)
        if len(raw) > MAX_GAMMA_RESPONSE_BYTES:
            raise ValueError("Gamma response exceeded the 3 MB safety limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Gamma response was not valid UTF-8 JSON") from None
        if not isinstance(payload, list):
            raise ValueError("Gamma market response must be a list")
        for market in payload:
            if not isinstance(market, dict):
                continue
            market_id = str(market.get("conditionId") or market.get("id") or "").strip()
            if market_id:
                discovered.setdefault(market_id, market)
        if len(payload) < int(page_size):
            break
    return list(discovered.values())


def _binary_yes_no_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve exact binary YES/NO labels to their matching CLOB token IDs."""
    market_id = str(market.get("conditionId") or market.get("id") or "").strip()
    question = " ".join(str(market.get("question") or "").split())
    if not market_id or not question:
        return None
    if market.get("active") is False or market.get("closed") is True:
        return None
    if market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
        return None

    outcomes = _list_field(market.get("outcomes"))
    tokens = _list_field(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    normalized = [str(item or "").strip().casefold() for item in outcomes]
    if sorted(normalized) != ["no", "yes"]:
        return None
    mapping = {normalized[index]: str(tokens[index] or "").strip() for index in range(2)}
    yes_token, no_token = mapping["yes"], mapping["no"]
    if not yes_token or not no_token or yes_token == no_token:
        return None

    volume = _number(market.get("volume24hr"))
    return {
        "market_id": market_id,
        "question": question[:500],
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "volume_24h": volume if volume is not None and volume >= 0 else None,
    }


class LiveClobMarketSnapshotProvider:
    """Build current, validated binary-market snapshots from public market data.

    The legacy constructor names (max_age_seconds and market_limit) are kept
    unchanged so existing paper-engine callers remain compatible.
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = 30.0,
        market_limit: int | None = None,
        timeout_seconds: float = 15.0,
        max_pages: int | None = None,
        page_size: int | None = None,
    ) -> None:
        try:
            age = float(max_age_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("max_age_seconds must be finite and between 3 and 180") from None
        if not math.isfinite(age) or not 3 <= age <= 180:
            raise ValueError("max_age_seconds must be finite and between 3 and 180")
        selected_limit = (
            _bounded_int_env("PAPER_LIVE_MARKETS_LIMIT", 10, 1, 50)
            if market_limit is None
            else int(market_limit)
        )
        if not 1 <= selected_limit <= 50:
            raise ValueError("market_limit must be between 1 and 50")
        selected_pages = (
            _bounded_int_env("PAPER_GAMMA_MAX_PAGES", 5, 1, 10)
            if max_pages is None else int(max_pages)
        )
        selected_page_size = (
            _bounded_int_env("PAPER_GAMMA_PAGE_SIZE", 100, 20, 200)
            if page_size is None else int(page_size)
        )
        if not 1 <= selected_pages <= 10:
            raise ValueError("max_pages must be between 1 and 10")
        if not 20 <= selected_page_size <= 200:
            raise ValueError("page_size must be between 20 and 200")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout_seconds must be finite and between 2 and 30") from None
        if not math.isfinite(timeout) or not 2 <= timeout <= 30:
            raise ValueError("timeout_seconds must be finite and between 2 and 30")

        self.max_age_seconds = age
        self.market_limit = selected_limit
        self.timeout_seconds = timeout
        self.max_pages = selected_pages
        self.page_size = selected_page_size
        self.last_diagnostics: dict[str, Any] = {
            "source": "polymarket_gamma_and_public_clob",
            "markets_discovered": 0,
            "binary_markets_considered": 0,
            "markets_selected": 0,
            "snapshots_returned": 0,
            "markets_skipped": 0,
            "orderbooks_requested": 0,
            "orderbooks_usable": 0,
            "research_only": True,
            "execution": "none",
        }

    def _discover_markets(self) -> list[dict[str, Any]]:
        return fetch_active_gamma_markets(
            page_size=self.page_size,
            max_pages=self.max_pages,
            timeout_seconds=self.timeout_seconds,
        )

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)

        raw_markets = self._discover_markets()
        if not isinstance(raw_markets, list):
            raise ValueError("Gamma market fetch must return a list")
        parsed_by_id: dict[str, dict[str, Any]] = {}
        for raw_market in raw_markets:
            if not isinstance(raw_market, dict):
                continue
            item = _binary_yes_no_market(raw_market)
            if item is not None:
                parsed_by_id.setdefault(item["market_id"], item)
        parsed = list(parsed_by_id.values())
        parsed.sort(
            key=lambda row: row["volume_24h"] if row["volume_24h"] is not None else -1.0,
            reverse=True,
        )
        selected = parsed[:self.market_limit]

        token_rows = [
            {"token_id": token}
            for market in selected
            for token in (market["yes_token_id"], market["no_token_id"])
        ]
        # Preserve the original one-argument call contract used by the paper
        # engine and its integrations. market_limit <= 50 means <= 100 tokens.
        books = fetch_order_books(token_rows) if token_rows else {}
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

            yes_observed = _utc_timestamp(yes_book.get("observed_at"))
            no_observed = _utc_timestamp(no_book.get("observed_at"))
            if yes_observed is None or no_observed is None:
                skipped += 1
                continue
            observed = min(yes_observed, no_observed)
            receipt_ages = [(current - stamp).total_seconds() for stamp in (yes_observed, no_observed)]
            if any(age < -5.0 or age > self.max_age_seconds for age in receipt_ages):
                skipped += 1
                continue

            exchange_raw = [
                yes_book.get("exchange_timestamp"),
                no_book.get("exchange_timestamp"),
            ]
            exchange_present = [bool(str(value or "").strip()) for value in exchange_raw]
            if any(exchange_present):
                # Partial venue timestamps are not enough to prove that both
                # YES and NO books belong to a consistent, fresh observation.
                if not all(exchange_present):
                    skipped += 1
                    continue
                exchange_stamps = [_utc_timestamp(value) for value in exchange_raw]
                if any(stamp is None for stamp in exchange_stamps):
                    skipped += 1
                    continue
                exchange_ages = [
                    (current - stamp).total_seconds()
                    for stamp in exchange_stamps if stamp is not None
                ]
                if any(age < -5.0 or age > self.max_age_seconds for age in exchange_ages):
                    skipped += 1
                    continue

            yes_bid = _number(yes_book.get("best_bid"))
            yes_ask = _number(yes_book.get("best_ask"))
            no_bid = _number(no_book.get("best_bid"))
            no_ask = _number(no_book.get("best_ask"))
            quotes = (yes_bid, yes_ask, no_bid, no_ask)
            if any(value is None or not 0.0 < value < 1.0 for value in quotes):
                skipped += 1
                continue
            yes_bid, yes_ask, no_bid, no_ask = (float(value) for value in quotes)
            if yes_bid >= yes_ask or no_bid >= no_ask:
                skipped += 1
                continue
            yes_price = (yes_bid + yes_ask) / 2.0
            no_price = (no_bid + no_ask) / 2.0

            row = {
                **market,
                "observed_at": observed.isoformat(),
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "yes_spread_bps": _number(yes_book.get("spread_bps")),
                "no_spread_bps": _number(no_book.get("spread_bps")),
                "yes_bid_depth_usd_top5": _number(yes_book.get("bid_depth_usd_top5")),
                "yes_ask_depth_usd_top5": _number(yes_book.get("ask_depth_usd_top5")),
                "no_bid_depth_usd_top5": _number(no_book.get("bid_depth_usd_top5")),
                "no_ask_depth_usd_top5": _number(no_book.get("ask_depth_usd_top5")),
                "yes_book_observed_at": yes_observed.isoformat(),
                "no_book_observed_at": no_observed.isoformat(),
                "yes_book_hash": str(yes_book.get("book_hash") or ""),
                "no_book_hash": str(no_book.get("book_hash") or ""),
                "yes_book": yes_book,
                "no_book": no_book,
                # Keep the established downstream metadata contract.
                "book_source": "official_polymarket_python_sdk",
                "market_source": "public_gamma_api",
                # Supplemental newer fields are additive only.
                "source": "gamma_plus_public_clob_orderbook",
                "market_data_mode": "read_only_live_public",
                "research_only": True,
                "execution": "none",
            }
            try:
                normalized = validate_market_snapshot(
                    row,
                    max_age_seconds=self.max_age_seconds,
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
            "orderbooks_requested": len(token_rows),
            "orderbooks_usable": usable_books,
            "max_book_age_seconds": self.max_age_seconds,
            "oldest_snapshot_age_seconds": round(
                max((row.get("age_seconds", 0.0) for row in snapshots), default=0.0), 3
            ),
            "research_only": True,
            "execution": "none",
        }
        return snapshots


def provider_from_env(
    *,
    local_path: str,
    local_max_age_seconds: float,
    source: str | None = None,
    max_markets: int | None = None,
    max_book_age_seconds: float | None = None,
) -> Any:
    """Select local replay or explicitly enabled read-only public CLOB data."""
    selected_source = (
        str(source).strip().lower()
        if source is not None
        else os.getenv("PAPER_MARKET_DATA_SOURCE", "local_json").strip().lower()
    )
    if selected_source == "local_json":
        from market_snapshot_provider import JsonMarketSnapshotProvider
        return JsonMarketSnapshotProvider(local_path, max_age_seconds=local_max_age_seconds)
    if selected_source not in {"polymarket_public", "live_clob"}:
        raise ValueError("PAPER_MARKET_DATA_SOURCE must be local_json or polymarket_public")
    return LiveClobMarketSnapshotProvider(
        max_age_seconds=(
            max_book_age_seconds if max_book_age_seconds is not None else 30.0
        ),
        market_limit=(
            max_markets if max_markets is not None
            else _bounded_int_env("PAPER_LIVE_MARKET_MAX_MARKETS", 10, 1, 50)
        ),
    )
