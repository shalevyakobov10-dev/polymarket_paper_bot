"""Read-only public market data adapter for Polymarket perpetual contracts.

This module intentionally contains no authentication, signing, account, funding
transfer, or order-submission code. It validates exchange timestamps and order
books before returning paper-research snapshots.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PERPS_API_BASE_URL = "https://api.perpetuals.polymarket.com"
USER_AGENT = "PolymarketResearchBot/1.0"
MAX_RESPONSE_BYTES = 5_000_000


class PerpsAPIError(RuntimeError):
    """Public Perps API request or schema failure."""


class PerpsNoUsableMarkets(PerpsAPIError):
    """No instrument returned a fresh and executable-looking two-sided book."""


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a decimal string or number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a valid decimal") from None
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"{field} must be finite" + (" and positive" if positive else ""))
    return parsed


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be an integer") from None
    if parsed < minimum or str(parsed) != str(value).strip():
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return parsed


def _epoch_ms(value: Any, field: str) -> int:
    parsed = _integer(value, field, minimum=1)
    if parsed < 1_000_000_000_000 or parsed > 9_999_999_999_999:
        raise ValueError(f"{field} must be a Unix timestamp in milliseconds")
    return parsed


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def _age_is_acceptable(timestamp_ms: int, current: datetime, max_age: float) -> bool:
    stamp = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    age = (current - stamp).total_seconds()
    return -2.0 <= age <= max_age


def _field(row: dict[str, Any], snake: str, camel: str | None = None) -> Any:
    value = row.get(snake)
    if value is None and camel:
        value = row.get(camel)
    return value


def _normalize_instrument(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    instrument_id = _integer(_field(raw, "instrument_id", "id"), "instrument_id")
    instrument_type = str(raw.get("instrument_type") or "perpetual").strip().lower()
    if instrument_type != "perpetual":
        return None
    symbol = str(raw.get("symbol") or "").strip()
    category = str(raw.get("category") or "").strip().lower()
    if not symbol or not category:
        raise ValueError("instrument symbol and category are required")
    settlement = raw.get("settlement")
    close_only = raw.get("close_only", False)
    isolated_only = raw.get("isolated_only", False)
    if not isinstance(close_only, bool):
        raise ValueError("instrument close_only must be boolean")
    if not isinstance(isolated_only, bool):
        raise ValueError("instrument isolated_only must be boolean")
    if settlement is not None:
        return None
    min_notional = _decimal(raw.get("min_notional"), "min_notional", positive=True)
    max_market_notional = _decimal(
        raw.get("max_market_notional"), "max_market_notional", positive=True
    )
    max_limit_notional = _decimal(
        raw.get("max_limit_notional"), "max_limit_notional", positive=True
    )
    max_leverage = _decimal(raw.get("max_leverage"), "max_leverage", positive=True)
    if max_market_notional < min_notional or max_limit_notional < min_notional:
        raise ValueError("instrument maximum notional must be >= min_notional")
    quantity_decimals = _integer(raw.get("quantity_decimals"), "quantity_decimals")
    price_decimals = _integer(raw.get("price_decimals"), "price_decimals")
    price_bounds = _decimal(raw.get("price_bounds"), "price_bounds", positive=True)
    liquidation_fee = _decimal(raw.get("liquidation_fee"), "liquidation_fee")
    if price_bounds > 1 or not Decimal("0") <= liquidation_fee < 1:
        raise ValueError("instrument price_bounds or liquidation_fee is outside valid bounds")
    risk_tiers = raw.get("risk_tiers")
    if not isinstance(risk_tiers, list) or not risk_tiers:
        raise ValueError("instrument risk_tiers must be a non-empty list")
    normalized_tiers = []
    previous_bound = Decimal("-1")
    for tier in risk_tiers:
        if not isinstance(tier, dict):
            raise ValueError("risk tier must be an object")
        lower = _decimal(tier.get("lower_bound"), "risk_tier.lower_bound")
        leverage = _decimal(tier.get("max_leverage"), "risk_tier.max_leverage", positive=True)
        if lower < 0 or lower <= previous_bound:
            raise ValueError("risk tier lower bounds must be strictly increasing and nonnegative")
        if leverage > max_leverage:
            raise ValueError("risk tier leverage cannot exceed instrument max leverage")
        previous_bound = lower
        normalized_tiers.append({
            "lower_bound": str(lower),
            "max_leverage": str(leverage),
        })
    return {
        "instrument_id": instrument_id,
        "symbol": symbol,
        "display_symbol": str(raw.get("display_symbol") or symbol),
        "category": category,
        "base_asset": str(raw.get("base_asset") or ""),
        "quote_asset": str(raw.get("quote_asset") or "USD"),
        "funding_interval": str(raw.get("funding_interval") or ""),
        "close_only": close_only,
        "isolated_only": isolated_only,
        "min_notional": str(min_notional),
        "max_market_notional": str(max_market_notional),
        "max_limit_notional": str(max_limit_notional),
        "max_leverage": str(max_leverage),
        "quantity_decimals": quantity_decimals,
        "price_decimals": price_decimals,
        "price_bounds": str(price_bounds),
        "liquidation_fee": str(liquidation_fee),
        "risk_tiers": normalized_tiers,
        "instrument_type": instrument_type,
    }


def _normalize_levels(value: Any, side: str, maximum_levels: int = 10) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"book {side} must be a list")
    rows: list[tuple[Decimal, Decimal]] = []
    for raw in value[:maximum_levels]:
        if isinstance(raw, dict):
            price_value, quantity_value = raw.get("price"), raw.get("quantity")
        elif isinstance(raw, (list, tuple)) and len(raw) == 2:
            price_value, quantity_value = raw
        else:
            raise ValueError(f"book {side} level has an invalid shape")
        price = _decimal(price_value, f"{side}.price", positive=True)
        quantity = _decimal(quantity_value, f"{side}.quantity", positive=True)
        rows.append((price, quantity))
    if not rows:
        raise ValueError(f"book {side} is empty")
    expected = sorted(rows, key=lambda pair: pair[0], reverse=(side == "bids"))
    if rows != expected:
        raise ValueError(f"book {side} levels are not price ordered")
    return [{"price": str(price), "quantity": str(quantity)} for price, quantity in rows]


def _normalize_ticker(raw: Any, instrument_id: int, current: datetime, max_age: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("ticker must be an object")
    returned_id = _integer(
        _field(raw, "instrument_id", "instrumentId"), "ticker.instrument_id"
    )
    if returned_id != instrument_id:
        raise ValueError("ticker instrument ID mismatch")
    timestamp_ms = _epoch_ms(raw.get("timestamp"), "ticker.timestamp")
    if not _age_is_acceptable(timestamp_ms, current, max_age):
        raise ValueError("ticker timestamp is stale or future-dated")
    ticker_symbol = str(raw.get("symbol") or "").strip()
    if not ticker_symbol:
        raise ValueError("ticker symbol is required")
    result = {
        "instrument_id": instrument_id,
        "symbol": ticker_symbol,
        "index_price": str(_decimal(_field(raw, "index_price", "indexPrice"), "ticker.index_price", positive=True)),
        "mark_price": str(_decimal(_field(raw, "mark_price", "markPrice"), "ticker.mark_price", positive=True)),
        "last_price": str(_decimal(_field(raw, "last_price", "lastPrice"), "ticker.last_price", positive=True)),
        "mid_price": str(_decimal(_field(raw, "mid_price", "midPrice"), "ticker.mid_price", positive=True)),
        "open_interest": str(_decimal(_field(raw, "open_interest", "openInterest"), "ticker.open_interest")),
        "funding_rate": str(_decimal(_field(raw, "funding_rate", "fundingRate"), "ticker.funding_rate")),
        "timestamp_ms": timestamp_ms,
        "timestamp": _iso_from_ms(timestamp_ms),
    }
    next_funding = _field(raw, "next_funding", "nextFunding")
    if next_funding is not None:
        result["next_funding_ms"] = _epoch_ms(next_funding, "ticker.next_funding")
    volume = _field(raw, "volume_24h", "volume24h")
    if volume is not None:
        result["volume_24h"] = str(_decimal(volume, "ticker.volume_24h"))
    return result




def _normalize_fee_schedule(raw: Any, retrieved_at: datetime) -> dict[str, dict[str, str]]:
    """Validate public perpetual fee schedules and choose a conservative base rate.

    The public endpoint does not disclose the user's authenticated 30-day tier.
    Paper mode therefore uses the highest published taker rate for each category,
    never an assumed volume discount.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("fee_schedule"), list):
        raise PerpsAPIError("Perps fee schedule response is missing fee_schedule")
    result: dict[str, dict[str, str]] = {}
    for entry in raw["fee_schedule"]:
        if not isinstance(entry, dict) or str(entry.get("instrument_type", "")).lower() != "perpetual":
            continue
        category = str(entry.get("category") or "").strip().lower()
        if not category:
            raise PerpsAPIError("Perps fee schedule category is missing")
        base_taker = _decimal(entry.get("taker_fee_rate"), "fee_schedule.taker_fee_rate")
        base_maker = _decimal(entry.get("maker_fee_rate"), "fee_schedule.maker_fee_rate")
        if not ZERO <= base_taker < 1 or not ZERO <= base_maker < 1:
            raise PerpsAPIError("Perps fee schedule rates must be in [0, 1)")
        taker_rates = [base_taker]
        maker_rates = [base_maker]
        tiers = entry.get("tiers", [])
        if not isinstance(tiers, list):
            raise PerpsAPIError("Perps fee schedule tiers must be a list")
        prior_volume = Decimal("-1")
        for tier in tiers:
            if not isinstance(tier, dict):
                raise PerpsAPIError("Perps fee tier must be an object")
            volume = _decimal(tier.get("min_volume_30d"), "fee_tier.min_volume_30d")
            taker_rate = _decimal(tier.get("taker_fee_rate"), "fee_tier.taker_fee_rate")
            maker_rate = _decimal(tier.get("maker_fee_rate"), "fee_tier.maker_fee_rate")
            if volume < 0 or volume <= prior_volume:
                raise PerpsAPIError("Perps fee tier volume thresholds must be strictly increasing")
            if not ZERO <= taker_rate < 1 or not ZERO <= maker_rate < 1:
                raise PerpsAPIError("Perps fee tier rates must be in [0, 1)")
            prior_volume = volume
            taker_rates.append(taker_rate)
            maker_rates.append(maker_rate)
        if category in result:
            raise PerpsAPIError(f"duplicate Perps fee schedule category: {category}")
        result[category] = {
            "taker_fee_rate": str(max(taker_rates)),
            "maker_fee_rate": str(max(maker_rates)),
            "tier_assumption": "highest_published_rate_no_volume_discount",
            "source": "public_perps_fee_schedule",
            "retrieved_at": retrieved_at.astimezone(timezone.utc).isoformat(),
        }
    if not result:
        raise PerpsAPIError("Perps fee schedule contains no perpetual categories")
    return result


def _funding_timestamp_ms(value: Any) -> int:
    parsed = _integer(value, "funding.timestamp", minimum=1)
    # The endpoint has returned Unix seconds in documented examples and may
    # expose milliseconds in SDK-normalized responses; accept both explicitly.
    if 1_000_000_000 <= parsed <= 9_999_999_999:
        return parsed * 1000
    if 1_000_000_000_000 <= parsed <= 9_999_999_999_999:
        return parsed
    raise ValueError("funding timestamp must be Unix seconds or milliseconds")


def _normalize_book(raw: Any, instrument_id: int, current: datetime, max_age: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("book must be an object")
    returned_id = _integer(
        _field(raw, "instrument_id", "instrumentId"), "book.instrument_id"
    )
    if returned_id != instrument_id:
        raise ValueError("book instrument ID mismatch")
    timestamp_ms = _epoch_ms(raw.get("timestamp"), "book.timestamp")
    if not _age_is_acceptable(timestamp_ms, current, max_age):
        raise ValueError("book timestamp is stale or future-dated")
    sequence = _integer(raw.get("sequence"), "book.sequence")
    bids = _normalize_levels(raw.get("bids"), "bids")
    asks = _normalize_levels(raw.get("asks"), "asks")
    best_bid, best_ask = Decimal(bids[0]["price"]), Decimal(asks[0]["price"])
    if best_bid >= best_ask:
        raise ValueError("book is crossed or locked")
    return {
        "instrument_id": instrument_id,
        "timestamp_ms": timestamp_ms,
        "timestamp": _iso_from_ms(timestamp_ms),
        "sequence": sequence,
        "bids": bids,
        "asks": asks,
        "best_bid": str(best_bid),
        "best_ask": str(best_ask),
    }


class PerpsPublicMarketDataProvider:
    """Fetch a bounded set of fresh public Perps instruments, tickers and books."""

    def __init__(
        self,
        *,
        base_url: str = PERPS_API_BASE_URL,
        market_limit: int | None = None,
        max_age_seconds: float = 10.0,
        depth: int = 10,
        timeout_seconds: float = 8.0,
    ) -> None:
        if not math.isfinite(float(max_age_seconds)) or not 1.0 <= float(max_age_seconds) <= 30.0:
            raise ValueError("max_age_seconds must be between 1 and 30")
        if depth not in {10, 100, 500, 1000}:
            raise ValueError("depth must be one of 10, 100, 500, or 1000")
        self.base_url = base_url.rstrip("/")
        self.market_limit = market_limit or _bounded_int_env("PERPS_MARKET_LIMIT", 10, 1, 50)
        if not 1 <= self.market_limit <= 50:
            raise ValueError("market_limit must be between 1 and 50")
        self.max_age_seconds = float(max_age_seconds)
        self.depth = depth
        self.timeout_seconds = max(2.0, min(20.0, float(timeout_seconds)))

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise PerpsAPIError(f"Perps API returned HTTP {status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise PerpsAPIError(f"Perps API returned HTTP {exc.code}") from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            raise PerpsAPIError(
                f"Perps API network error: {type(reason).__name__ if reason else 'unavailable'}"
            ) from None
        except TimeoutError:
            raise PerpsAPIError("Perps API request timed out") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PerpsAPIError("Perps API response exceeded the size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PerpsAPIError("Perps API returned invalid JSON") from None

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        fixed_time = now is not None

        raw_instruments = self._get_json("/v1/info/instruments")
        raw_tickers = self._get_json("/v1/info/tickers")
        raw_fees = self._get_json("/v1/info/fees")
        if not isinstance(raw_instruments, list) or not isinstance(raw_tickers, list):
            raise PerpsAPIError("Perps instruments and tickers responses must be arrays")
        fee_schedule = _normalize_fee_schedule(
            raw_fees, current if fixed_time else datetime.now(timezone.utc)
        )

        instruments: list[dict[str, Any]] = []
        for raw in raw_instruments:
            # Keep close-only instruments visible so an existing paper position
            # can still be marked and exited. The paper engine blocks new entries.
            instrument = _normalize_instrument(raw)
            if instrument is not None:
                instruments.append(instrument)
        ticker_rows = {}
        for row in raw_tickers:
            if not isinstance(row, dict):
                continue
            raw_id = _field(row, "instrument_id", "instrumentId")
            try:
                instrument_id = _integer(raw_id, "ticker.instrument_id")
            except ValueError:
                continue
            ticker_rows[instrument_id] = row
        instruments.sort(key=lambda row: row["instrument_id"])

        snapshots: list[dict[str, Any]] = []
        failures: list[str] = []
        for instrument in instruments:
            if len(snapshots) >= self.market_limit:
                break
            instrument_id = int(instrument["instrument_id"])
            raw_ticker = ticker_rows.get(instrument_id)
            if raw_ticker is None:
                continue
            try:
                ticker_check_time = current if fixed_time else datetime.now(timezone.utc)
                ticker = _normalize_ticker(
                    raw_ticker, instrument_id, ticker_check_time, self.max_age_seconds
                )
                raw_book = self._get_json(
                    "/v1/info/book", {"instrument_id": instrument_id, "depth": self.depth}
                )
                book_check_time = current if fixed_time else datetime.now(timezone.utc)
                book = _normalize_book(
                    raw_book, instrument_id, book_check_time, self.max_age_seconds
                )
                if ticker["symbol"] != instrument["symbol"]:
                    raise ValueError("ticker symbol does not match instrument metadata")
                market_fees = fee_schedule.get(instrument["category"])
                if market_fees is None:
                    raise ValueError("no current fee schedule for instrument category")
                snapshots.append({
                    "instrument": instrument,
                    "ticker": ticker,
                    "book": book,
                    "fees": market_fees,
                    "received_at": (current if fixed_time else datetime.now(timezone.utc)).isoformat(),
                    "research_only": True,
                    "execution": "none",
                })
            except (PerpsAPIError, ValueError, InvalidOperation) as exc:
                failures.append(f"{instrument['symbol']}:{str(exc)[:160]}")
        if not snapshots:
            detail = "; ".join(failures[:3]) if failures else "no active instrument had a fresh ticker"
            raise PerpsNoUsableMarkets(f"No fresh Perps market snapshots: {detail}")

    def load_funding_history(
        self,
        instrument_id: int,
        *,
        start_timestamp_ms: int,
        end_timestamp_ms: int,
    ) -> list[dict[str, Any]]:
        """Fetch exact published settlement rates for one bounded time range."""
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        start_ms = _epoch_ms(start_timestamp_ms, "start_timestamp_ms")
        end_ms = _epoch_ms(end_timestamp_ms, "end_timestamp_ms")
        if start_ms > end_ms:
            raise ValueError("funding history start must not exceed end")
        payload = self._get_json("/v1/info/funding", {
            "instrument_id": instrument_id,
            "start_timestamp": start_ms // 1000,
            "end_timestamp": end_ms // 1000,
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PerpsAPIError("Perps funding history response is missing data")
        more = payload.get("more", False)
        if not isinstance(more, bool):
            raise PerpsAPIError("Perps funding history more flag must be boolean")
        if more:
            raise PerpsAPIError("Perps funding history was truncated; refusing incomplete settlement data")
        result: dict[int, dict[str, Any]] = {}
        for row in payload["data"]:
            if not isinstance(row, dict):
                raise PerpsAPIError("Perps funding history row must be an object")
            timestamp_ms = _funding_timestamp_ms(row.get("timestamp"))
            rate = _decimal(row.get("funding_rate"), "funding.funding_rate")
            if timestamp_ms < start_ms - 1000 or timestamp_ms > end_ms + 1000:
                continue
            if timestamp_ms in result:
                raise PerpsAPIError("duplicate Perps funding settlement timestamp")
            result[timestamp_ms] = {
                "instrument_id": instrument_id,
                "timestamp_ms": timestamp_ms,
                "funding_rate": str(rate),
            }
        return [result[key] for key in sorted(result)]

        return snapshots
