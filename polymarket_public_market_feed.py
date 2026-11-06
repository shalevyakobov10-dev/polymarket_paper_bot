"""Read-only live binary market snapshots from Gamma, public CLOB REST and WebSocket.

The provider has no wallet, authenticated account client, signing or order
submission path. It prefers a fresh paired YES/NO WebSocket snapshot and falls
back to a fresh paired REST snapshot when the stream is incomplete.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market_data import fetch_order_books
from market_snapshot_provider import parse_timestamp, validate_market_snapshot
from market_stream import MarketStreamCollector

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "PolymarketResearchMarketFeed/1.0"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


class PolymarketPublicMarketSnapshotProvider:
    """Discover liquid binary markets, then join fresh YES/NO quotes."""

    def __init__(
        self,
        *,
        max_age_seconds: float = 180.0,
        max_markets: int | None = None,
        max_quote_skew_seconds: float = 5.0,
        websocket_enabled: bool = False,
        websocket_session_seconds: int | None = None,
        stream_log_path: str | Path | None = None,
        gamma_page_fetcher: Callable[[int, int], list[dict[str, Any]]] | None = None,
        book_fetcher: Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]] | None = None,
        stream_collector: Any | None = None,
    ) -> None:
        if not math.isfinite(float(max_age_seconds)) or not 10 <= float(max_age_seconds) <= 3600:
            raise ValueError("max_age_seconds must be in [10, 3600]")
        if not math.isfinite(float(max_quote_skew_seconds)) or not 0 < float(max_quote_skew_seconds) <= 30:
            raise ValueError("max_quote_skew_seconds must be in (0, 30]")
        self.max_age_seconds = float(max_age_seconds)
        self.max_markets = int(max_markets) if max_markets is not None else _bounded_int(
            "PUBLIC_MARKETS_TO_TRACK", 10, 1, 10
        )
        if not 1 <= self.max_markets <= 10:
            raise ValueError("max_markets must be between 1 and 10 to bound CLOB requests")
        self.max_quote_skew_seconds = float(max_quote_skew_seconds)
        self.gamma_page_fetcher = gamma_page_fetcher or self._fetch_gamma_page
        self.book_fetcher = book_fetcher or fetch_order_books
        self.websocket_enabled = bool(websocket_enabled or stream_collector is not None)
        self.websocket_session_seconds = (
            int(websocket_session_seconds)
            if websocket_session_seconds is not None
            else _bounded_int("PUBLIC_MARKET_WS_SESSION_SECONDS", 15, 5, 120)
        )
        if not 5 <= self.websocket_session_seconds <= 120:
            raise ValueError("websocket_session_seconds must be in [5, 120]")
        self.stream_log_path = Path(
            stream_log_path or os.getenv("MARKET_STREAM_LOG_PATH", "./data/market_stream_events.jsonl")
        )
        self.stream_collector = stream_collector or (
            MarketStreamCollector(self.stream_log_path) if self.websocket_enabled else None
        )
        self.last_diagnostics: dict[str, Any] = {}

    @staticmethod
    def _fetch_gamma_page(page_size: int, offset: int) -> list[dict[str, Any]]:
        query = urlencode({
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        })
        request = Request(
            f"{GAMMA_MARKETS_URL}?{query}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            method="GET",
        )
        with urlopen(request, timeout=15) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError("Gamma market discovery returned a non-200 status")
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gamma market discovery response is not a list")
        if len(payload) > page_size:
            raise ValueError("Gamma market discovery exceeded the requested page size")
        return [item for item in payload if isinstance(item, dict)]

    def _discover_markets(self) -> list[dict[str, Any]]:
        page_size = _bounded_int("PUBLIC_GAMMA_PAGE_SIZE", 100, 20, 200)
        max_pages = _bounded_int("PUBLIC_GAMMA_MAX_PAGES", 3, 1, 5)
        discovered: dict[str, dict[str, Any]] = {}
        for page_number in range(max_pages):
            items = self.gamma_page_fetcher(page_size, page_number * page_size)
            if not isinstance(items, list):
                raise ValueError("Gamma page fetcher returned a non-list")
            for market in items:
                if not isinstance(market, dict):
                    continue
                if market.get("closed") is True or market.get("active") is False:
                    continue
                market_id = str(market.get("conditionId") or market.get("id") or "").strip()
                if market_id:
                    discovered.setdefault(market_id, market)

            if len(items) < page_size:
                break

        def volume(market: dict[str, Any]) -> float:
            value = _number(market.get("volume24hr"))
            return value if value is not None and value >= 0 else 0.0

        return sorted(discovered.values(), key=volume, reverse=True)[: self.max_markets]

    @staticmethod
    def _binary_tokens(market: dict[str, Any]) -> dict[str, Any] | None:
        outcomes = [str(value).strip().casefold() for value in _list_field(market.get("outcomes"))]
        prices = _list_field(market.get("outcomePrices"))
        tokens = _list_field(market.get("clobTokenIds"))
        if len(outcomes) != len(prices) or len(outcomes) != len(tokens):
            return None
        yes_indices = [i for i, value in enumerate(outcomes) if value == "yes"]
        no_indices = [i for i, value in enumerate(outcomes) if value == "no"]
        if len(yes_indices) != 1 or len(no_indices) != 1:
            return None
        yes_index, no_index = yes_indices[0], no_indices[0]
        yes_token = str(tokens[yes_index]).strip()
        no_token = str(tokens[no_index]).strip()
        if not yes_token or not no_token or yes_token == no_token:
            return None
        yes_probability = _number(prices[yes_index])
        no_probability = _number(prices[no_index])
        if (
            yes_probability is None or no_probability is None
            or not 0 <= yes_probability <= 1
            or not 0 <= no_probability <= 1
        ):
            return None
        question = str(market.get("question") or "").strip()[:500]
        if not question:
            return None
        return {
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "gamma_yes_probability": yes_probability,
            "gamma_no_probability": no_probability,
        }

    def _read_stream_books(self, now) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.websocket_enabled or not self.stream_log_path.exists():
            return latest
        try:
            with self.stream_log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("event_type") not in {"book", "price_change"}:
                        continue
                    token = str(record.get("token_id") or "").strip()
                    observed = parse_timestamp(record.get("observed_at"))
                    bid = _number(record.get("best_bid"))
                    ask = _number(record.get("best_ask"))
                    midpoint = _number(record.get("midpoint"))
                    if midpoint is None and bid is not None and ask is not None:
                        midpoint = (bid + ask) / 2
                    if (
                        not token or observed is None
                        or bid is None or ask is None or midpoint is None
                        or not 0 < bid < ask < 1
                        or not bid <= midpoint <= ask
                    ):
                        continue
                    age = (now - observed).total_seconds()
                    if age < -30 or age > self.max_age_seconds:
                        continue
                    previous = latest.get(token)
                    if previous is None or observed > previous["observed_at"]:
                        latest[token] = {
                            "best_bid": bid,
                            "best_ask": ask,
                            "midpoint": midpoint,
                            "observed_at": observed,
                            "source": "public_clob_market_websocket",
                        }
        except OSError:
            return {}
        return latest

    def _rest_book_failure(self, book: Any, now) -> str | None:
        """Classify one REST quote as missing/invalid or stale for diagnostics."""
        if not isinstance(book, dict):
            return "missing_or_invalid"
        if book.get("book_usable") is not True or book.get("crossed_book") is True:
            return "missing_or_invalid"
        observed = parse_timestamp(book.get("observed_at"))
        if observed is None:
            return "missing_or_invalid"
        age = (now - observed).total_seconds()
        if age < -30 or age > self.max_age_seconds:
            return "stale"
        bid = _number(book.get("best_bid"))
        ask = _number(book.get("best_ask"))
        midpoint = _number(book.get("midpoint"))
        if (
            bid is None or ask is None or midpoint is None
            or not 0 < bid < ask < 1
            or not bid <= midpoint <= ask
        ):
            return "missing_or_invalid"
        return None

    @staticmethod
    def _normalize_rest_book(book: Any, now) -> dict[str, Any] | None:
        if not isinstance(book, dict) or book.get("book_usable") is not True:
            return None
        if book.get("crossed_book") is True:
            return None
        observed = parse_timestamp(book.get("observed_at"))
        bid = _number(book.get("best_bid"))
        ask = _number(book.get("best_ask"))
        midpoint = _number(book.get("midpoint"))
        if observed is None or bid is None or ask is None or midpoint is None:
            return None
        age = (now - observed).total_seconds()
        if (
            age < -30 or age > self.max_age_seconds
            or not 0 < bid < ask < 1 or not bid <= midpoint <= ask
        ):
            return None
        return {
            "best_bid": bid,
            "best_ask": ask,
            "midpoint": midpoint,
            "observed_at": observed,
            "source": "official_sdk_rest_snapshot",
        }

    def load(self, *, now=None) -> list[dict[str, Any]]:
        from datetime import datetime, timezone

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        markets = self._discover_markets()
        candidates: list[dict[str, Any]] = []
        skipped_non_binary = 0
        for market in markets:
            binary = self._binary_tokens(market)
            if binary is None:
                skipped_non_binary += 1
                continue
            candidates.append({
                "market_id": str(market.get("conditionId") or market.get("id") or "").strip(),
                "question": str(market.get("question") or "").strip()[:500],
                "volume_24h": _number(market.get("volume24hr")),
                **binary,
            })

        token_rows = [
            {"token_id": token}
            for item in candidates
            for token in (item["yes_token_id"], item["no_token_id"])
        ]
        websocket_session = None
        if self.stream_collector is not None and token_rows:
            websocket_session = self.stream_collector.run_session(
                [row["token_id"] for row in token_rows],
                max_seconds=self.websocket_session_seconds,
            )
        stream_books = self._read_stream_books(current)

        rest_books = self.book_fetcher(token_rows) if token_rows else {}
        if not isinstance(rest_books, dict):
            raise ValueError("CLOB REST book fetcher returned an invalid result")

        snapshots: list[dict[str, Any]] = []
        source_counts = {"public_clob_market_websocket": 0, "official_sdk_rest_snapshot": 0}
        skipped_missing_book = 0
        skipped_stale_book = 0
        skipped_skewed_book = 0
        for item in candidates:
            yes_token, no_token = item["yes_token_id"], item["no_token_id"]
            yes_ws = stream_books.get(yes_token)
            no_ws = stream_books.get(no_token)
            selected_source = "public_clob_market_websocket"

            # Use WebSocket only when both sides are fresh together. Never mix
            # one streamed side with one REST side in a single market snapshot.
            if not isinstance(yes_ws, dict) or not isinstance(no_ws, dict):
                yes_ws = no_ws = None
            if yes_ws and no_ws:
                yes_quote, no_quote = yes_ws, no_ws
                skew = abs((yes_quote["observed_at"] - no_quote["observed_at"]).total_seconds())
                if skew > self.max_quote_skew_seconds:
                    yes_quote = no_quote = None
            else:
                yes_quote = no_quote = None

            if yes_quote is None or no_quote is None:
                raw_yes_book = rest_books.get(yes_token)
                raw_no_book = rest_books.get(no_token)
                yes_failure = self._rest_book_failure(raw_yes_book, current)
                no_failure = self._rest_book_failure(raw_no_book, current)
                yes_quote = (
                    self._normalize_rest_book(raw_yes_book, current)
                    if yes_failure is None else None
                )
                no_quote = (
                    self._normalize_rest_book(raw_no_book, current)
                    if no_failure is None else None
                )
                selected_source = "official_sdk_rest_snapshot"
                if yes_quote is None or no_quote is None:
                    failures = {yes_failure, no_failure}
                    if "missing_or_invalid" in failures:
                        skipped_missing_book += 1
                    if "stale" in failures:
                        skipped_stale_book += 1
                    continue
                skew = abs((yes_quote["observed_at"] - no_quote["observed_at"]).total_seconds())
                if skew > self.max_quote_skew_seconds:
                    skipped_skewed_book += 1
                    continue

            ages = [
                (current - yes_quote["observed_at"]).total_seconds(),
                (current - no_quote["observed_at"]).total_seconds(),
            ]
            if any(age < -30 or age > self.max_age_seconds for age in ages):
                skipped_stale_book += 1
                continue

            observed = min(yes_quote["observed_at"], no_quote["observed_at"])
            raw = {
                "market_id": item["market_id"],
                "question": item["question"],
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "observed_at": observed.isoformat(),
                "yes_price": yes_quote["midpoint"],
                "no_price": no_quote["midpoint"],
                "yes_bid": yes_quote["best_bid"],
                "yes_ask": yes_quote["best_ask"],
                "no_bid": no_quote["best_bid"],
                "no_ask": no_quote["best_ask"],
                "volume_24h": item["volume_24h"],
                "gamma_yes_probability": item["gamma_yes_probability"],
                "gamma_no_probability": item["gamma_no_probability"],
                "yes_book_observed_at": yes_quote["observed_at"].isoformat(),
                "no_book_observed_at": no_quote["observed_at"].isoformat(),
                "book_quote_skew_seconds": round(skew, 4),
                "market_data_source": selected_source,
                "research_only": True,
                "execution": "none",
            }
            try:
                snapshots.append(
                    validate_market_snapshot(
                        raw,
                        max_age_seconds=self.max_age_seconds,
                        now=current,
                    )
                )
                source_counts[selected_source] += 1
            except ValueError:
                skipped_stale_book += 1

        snapshots.sort(
            key=lambda row: (
                row.get("volume_24h") is not None,
                row.get("volume_24h") or 0.0,
            ),
            reverse=True,
        )
        self.last_diagnostics = {
            "source": "gamma_plus_public_clob",
            "gamma_markets_discovered": len(markets),
            "binary_markets_considered": len(candidates),
            "snapshots_returned": len(snapshots),
            "source_counts": source_counts,
            "websocket_enabled": self.websocket_enabled,
            "websocket_session": websocket_session,
            "websocket_books_available": len(stream_books),
            "skipped_non_binary": skipped_non_binary,
            "skipped_missing_or_invalid_book": skipped_missing_book,
            "skipped_stale_book": skipped_stale_book,
            "skipped_quote_time_skew": skipped_skewed_book,
            "research_only": True,
            "execution": "none",
        }
        return snapshots
