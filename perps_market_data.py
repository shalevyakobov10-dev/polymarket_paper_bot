"""Read-only Polymarket Perps market data adapter.

This module only calls the public /v1/info endpoints. It never creates an
authenticated client, reads private account state, signs payloads, or submits
orders. All numeric market values remain decimal strings at the API boundary.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PERPS_API_BASE_URL = "https://api.perpetuals.polymarket.com"
PERPS_MAX_DOCUMENTED_TICKER_AGE_SECONDS = 10.0
SUPPORTED_BOOK_DEPTHS = {10, 100, 500, 1000}


class PerpsMarketDataError(RuntimeError):
    """A public Perps response could not be safely used."""


def _pick(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PerpsMarketDataError(f"invalid_{field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PerpsMarketDataError(f"invalid_{field}") from None
    if not parsed.is_finite():
        raise PerpsMarketDataError(f"invalid_{field}")
    return parsed


def _positive_decimal(value: Any, field: str) -> Decimal:
    parsed = _decimal(value, field)
    if parsed <= 0:
        raise PerpsMarketDataError(f"non_positive_{field}")
    return parsed


def _timestamp_ms(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PerpsMarketDataError(f"invalid_{field}_timestamp")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PerpsMarketDataError(f"invalid_{field}_timestamp") from None
    # Perps timestamps are Unix milliseconds. Reject seconds-sized values
    # rather than silently interpreting them as 1970-era milliseconds.
    if parsed < 100_000_000_000:
        raise PerpsMarketDataError(f"invalid_{field}_timestamp")
    return parsed


def _utc_now_ms(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return int(current.astimezone(timezone.utc).timestamp() * 1000)


def _normalise_levels(raw: Any, *, side: str) -> list[dict[str, str]]:
    if not isinstance(raw, (list, tuple)):
        raise PerpsMarketDataError(f"invalid_{side}_book")
    levels: list[dict[str, str]] = []
    for item in raw[:100]:
        if isinstance(item, Mapping):
            price = _pick(item, "price", "p")
            quantity = _pick(item, "quantity", "qty", "size", "q")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, quantity = item[0], item[1]
        else:
            raise PerpsMarketDataError(f"invalid_{side}_book_level")
        parsed_price = _positive_decimal(price, f"{side}_price")
        parsed_quantity = _positive_decimal(quantity, f"{side}_quantity")
        levels.append({"price": str(parsed_price), "quantity": str(parsed_quantity)})
    if not levels:
        raise PerpsMarketDataError(f"empty_{side}_book")
    prices = [Decimal(level["price"]) for level in levels]
    expected = sorted(prices, reverse=(side == "bids"))
    if prices != expected:
        raise PerpsMarketDataError(f"unsorted_{side}_book")
    return levels


def _items(payload: Any, field: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        rows = payload.get("data", payload.get(field))
    else:
        rows = None
    if not isinstance(rows, list):
        raise PerpsMarketDataError(f"invalid_{field}_response")
    return [row for row in rows if isinstance(row, dict)]


class PerpsMarketDataProvider:
    """Fetch and validate public Perps instruments, tickers, and order books."""

    def __init__(
        self,
        *,
        base_url: str = PERPS_API_BASE_URL,
        timeout_seconds: float = 12.0,
        max_age_seconds: float = PERPS_MAX_DOCUMENTED_TICKER_AGE_SECONDS,
        max_spread_bps: float = 250.0,
        book_depth: int = 10,
        market_limit: int = 5,
        transport: Callable[[str, Mapping[str, str], float], Any] | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if not math.isfinite(max_age_seconds) or not 1 <= max_age_seconds <= 10:
            raise ValueError("max_age_seconds must be between 1 and 10")
        if not math.isfinite(max_spread_bps) or max_spread_bps <= 0:
            raise ValueError("max_spread_bps must be positive and finite")
        if book_depth not in SUPPORTED_BOOK_DEPTHS:
            raise ValueError("unsupported Perps book depth")
        if isinstance(market_limit, bool) or not isinstance(market_limit, int) or not 1 <= market_limit <= 50:
            raise ValueError("market_limit must be an integer between 1 and 50")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_age_seconds = float(max_age_seconds)
        self.max_spread_bps = float(max_spread_bps)
        self.book_depth = book_depth
        self.market_limit = market_limit
        self._transport = transport or self._http_get_json

    def _http_get_json(self, path: str, params: Mapping[str, str], timeout: float) -> Any:
        query = urlencode(dict(params))
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polymarket-paper-bot-perps/1.0",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise PerpsMarketDataError("response_too_large")
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            # Do not include response bodies, URLs, headers, or credentials in
            # status files/logs. Public endpoints are unauthenticated.
            raise PerpsMarketDataError(f"perps_http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise PerpsMarketDataError("perps_network_error") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PerpsMarketDataError("perps_invalid_json") from None

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        return self._transport(path, params or {}, self.timeout_seconds)

    def load_snapshots(
        self,
        *,
        symbols: set[str] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return only fresh, two-sided, non-crossed public market snapshots."""
        now_ms = _utc_now_ms(now)
        instruments = _items(self._get("/v1/info/instruments"), "instruments")
        tickers = _items(self._get("/v1/info/tickers"), "tickers")

        ticker_by_id: dict[int, dict[str, Any]] = {}
        for ticker in tickers:
            try:
                instrument_id = int(_pick(ticker, "instrument_id", "instrumentId", "id"))
            except (TypeError, ValueError, OverflowError):
                continue
            ticker_by_id[instrument_id] = ticker

        selected: list[dict[str, Any]] = []
        for instrument in instruments:
            try:
                instrument_id = int(_pick(instrument, "instrument_id", "instrumentId", "id"))
            except (TypeError, ValueError, OverflowError):
                continue
            symbol = str(_pick(instrument, "symbol", "display_symbol", "displaySymbol", default="")).strip()
            if not symbol or (symbols and symbol.upper() not in {s.upper() for s in symbols}):
                continue
            settlement = _pick(instrument, "settlement")
            if settlement:
                continue
            ticker = ticker_by_id.get(instrument_id)
            if ticker is None:
                continue
            selected.append(instrument | {"_ticker": ticker, "_id": instrument_id, "_symbol": symbol})

        if symbols:
            selected.sort(key=lambda row: row["_symbol"])
        selected = selected[: self.market_limit]

        snapshots: list[dict[str, Any]] = []
        failures: list[str] = []
        for instrument in selected:
            try:
                snapshot = self._build_snapshot(instrument, now_ms)
                snapshots.append(snapshot)
            except PerpsMarketDataError as exc:
                failures.append(f"{instrument['_symbol']}:{str(exc)[:80]}")
        if not snapshots:
            detail = ",".join(failures[:5]) or "no_active_matching_instruments"
            raise PerpsMarketDataError(f"no_usable_perps_markets:{detail}")
        return snapshots

    def _build_snapshot(self, instrument: dict[str, Any], now_ms: int) -> dict[str, Any]:
        ticker = instrument["_ticker"]
        instrument_id = instrument["_id"]
        symbol = instrument["_symbol"]

        ticker_timestamp_ms = _timestamp_ms(
            _pick(ticker, "timestamp", "updated_at", "updatedAt"),
            "ticker",
        )
        book_payload = self._get(
            "/v1/info/book",
            {"instrument_id": str(instrument_id), "depth": str(self.book_depth)},
        )
        if not isinstance(book_payload, Mapping):
            raise PerpsMarketDataError("invalid_book_response")
        book_timestamp_ms = _timestamp_ms(
            _pick(book_payload, "timestamp", "updated_at", "updatedAt"),
            "book",
        )

        ticker_age = (now_ms - ticker_timestamp_ms) / 1000.0
        book_age = (now_ms - book_timestamp_ms) / 1000.0
        future_tolerance = 2.0
        if ticker_age < -future_tolerance or ticker_age > self.max_age_seconds:
            raise PerpsMarketDataError("stale_or_future_ticker")
        if book_age < -future_tolerance or book_age > self.max_age_seconds:
            raise PerpsMarketDataError("stale_or_future_book")

        bids = _normalise_levels(_pick(book_payload, "bids"), side="bids")
        asks = _normalise_levels(_pick(book_payload, "asks"), side="asks")
        best_bid = Decimal(bids[0]["price"])
        best_ask = Decimal(asks[0]["price"])
        if best_bid >= best_ask:
            raise PerpsMarketDataError("crossed_or_locked_book")
        midpoint = (best_bid + best_ask) / Decimal("2")
        spread_bps = (best_ask - best_bid) / midpoint * Decimal("10000")
        if spread_bps > Decimal(str(self.max_spread_bps)):
            raise PerpsMarketDataError("spread_above_limit")

        mark_price = _positive_decimal(_pick(ticker, "mark_price", "markPrice"), "mark_price")
        index_price = _positive_decimal(_pick(ticker, "index_price", "indexPrice"), "index_price")
        last_raw = _pick(ticker, "last_price", "lastPrice")
        last_price = str(_positive_decimal(last_raw, "last_price")) if last_raw is not None else None
        ticker_mid_raw = _pick(ticker, "mid_price", "midPrice")
        ticker_mid = str(_positive_decimal(ticker_mid_raw, "mid_price")) if ticker_mid_raw is not None else str(midpoint)
        funding_rate = _decimal(_pick(ticker, "funding_rate", "fundingRate", default="0"), "funding_rate")
        next_funding = _timestamp_ms(_pick(ticker, "next_funding", "nextFunding"), "next_funding")

        risk_tiers_raw = _pick(instrument, "risk_tiers", "riskTiers", default=[])
        if not isinstance(risk_tiers_raw, list):
            raise PerpsMarketDataError("invalid_risk_tiers")
        risk_tiers: list[dict[str, str | float]] = []
        for tier in risk_tiers_raw:
            if not isinstance(tier, Mapping):
                continue
            lower = _pick(tier, "lower_bound", "lowerBound")
            leverage = _pick(tier, "max_leverage", "maxLeverage")
            try:
                lower_value = _decimal(lower, "risk_tier_lower_bound")
                leverage_value = float(leverage)
            except (PerpsMarketDataError, TypeError, ValueError, OverflowError):
                continue
            if lower_value < 0 or not math.isfinite(leverage_value) or leverage_value <= 0:
                continue
            risk_tiers.append({"lower_bound": str(lower_value), "max_leverage": leverage_value})
        risk_tiers.sort(key=lambda row: Decimal(str(row["lower_bound"])))

        max_leverage_raw = _pick(instrument, "max_leverage", "maxLeverage")
        try:
            max_leverage = float(max_leverage_raw)
        except (TypeError, ValueError, OverflowError):
            raise PerpsMarketDataError("invalid_max_leverage") from None
        if not math.isfinite(max_leverage) or max_leverage <= 0:
            raise PerpsMarketDataError("invalid_max_leverage")

        def optional_decimal(*names: str) -> str | None:
            value = _pick(instrument, *names)
            if value is None:
                return None
            return str(_decimal(value, names[0]))

        return {
            "schema_version": 1,
            "source": "polymarket_perps_public_rest",
            "research_only": True,
            "execution": "none",
            "observed_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
            "instrument_id": instrument_id,
            "symbol": symbol,
            "category": str(_pick(instrument, "category", default="unknown")),
            "base_asset": str(_pick(instrument, "base_asset", "baseAsset", default="")),
            "quote_asset": str(_pick(instrument, "quote_asset", "quoteAsset", default="")),
            "close_only": bool(_pick(instrument, "close_only", "closeOnly", default=False)),
            "settlement": bool(_pick(instrument, "settlement") is not None),
            "funding_interval": str(_pick(instrument, "funding_interval", "fundingInterval", default="")),
            "quantity_decimals": int(_pick(instrument, "quantity_decimals", "quantityDecimals", default=0)),
            "price_decimals": int(_pick(instrument, "price_decimals", "priceDecimals", default=0)),
            "min_notional": optional_decimal("min_notional", "minNotional"),
            "max_market_notional": optional_decimal("max_market_notional", "maxMarketNotional"),
            "max_limit_notional": optional_decimal("max_limit_notional", "maxLimitNotional"),
            "price_bounds": optional_decimal("price_bounds", "priceBounds"),
            "liquidation_fee": optional_decimal("liquidation_fee", "liquidationFee"),
            "max_leverage": max_leverage,
            "isolated_only": bool(_pick(instrument, "isolated_only", "isolatedOnly", default=False)),
            "risk_tiers": risk_tiers,
            "mark_price": str(mark_price),
            "index_price": str(index_price),
            "last_price": last_price,
            "mid_price": ticker_mid,
            "open_interest": str(_decimal(_pick(ticker, "open_interest", "openInterest", default="0"), "open_interest")),
            "funding_rate": str(funding_rate),
            "next_funding_ms": next_funding,
            "ticker_timestamp_ms": ticker_timestamp_ms,
            "book_timestamp_ms": book_timestamp_ms,
            "ticker_age_seconds": round(max(0.0, ticker_age), 3),
            "book_age_seconds": round(max(0.0, book_age), 3),
            "best_bid": str(best_bid),
            "best_ask": str(best_ask),
            "midpoint_from_book": str(midpoint),
            "spread_bps": round(float(spread_bps), 4),
            "bids": bids,
            "asks": asks,
            "entry_eligible": not bool(_pick(instrument, "close_only", "closeOnly", default=False)),
        }


def provider_from_env() -> PerpsMarketDataProvider:
    """Build a strictly public provider from bounded runtime configuration."""
    try:
        market_limit = int(os.getenv("PERPS_MARKET_LIMIT", "5"))
        max_age = float(os.getenv("PERPS_MAX_MARKET_AGE_SECONDS", "10"))
        max_spread = float(os.getenv("PERPS_MAX_SPREAD_BPS", "250"))
        depth = int(os.getenv("PERPS_BOOK_DEPTH", "10"))
        timeout = float(os.getenv("PERPS_HTTP_TIMEOUT_SECONDS", "12"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_perps_market_data_configuration") from None
    return PerpsMarketDataProvider(
        market_limit=market_limit,
        max_age_seconds=max_age,
        max_spread_bps=max_spread,
        book_depth=depth,
        timeout_seconds=timeout,
    )
