import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from perps_market_data import (
    PerpsNoUsableMarkets,
    PerpsPublicMarketDataProvider,
    _normalize_book,
    _normalize_instrument,
    _normalize_ticker,
)


NOW = datetime(2026, 11, 22, 8, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def instrument(instrument_id=7, *, close_only=False):
    return {
        "instrument_id": instrument_id,
        "instrument_type": "perpetual",
        "symbol": "BTC-PERP",
        "display_symbol": "BTC",
        "category": "crypto",
        "base_asset": "BTC",
        "quote_asset": "USD",
        "funding_interval": "1h",
        "close_only": close_only,
        "settlement": None,
        "isolated_only": False,
        "min_notional": "10",
        "max_market_notional": "100000",
        "max_limit_notional": "1000000",
        "max_leverage": 20,
        "quantity_decimals": 4,
        "price_decimals": 2,
        "price_bounds": "0.1",
        "liquidation_fee": "0.01",
        "risk_tiers": [
            {"lower_bound": "0", "max_leverage": 20},
            {"lower_bound": "100000", "max_leverage": 10},
        ],
    }


def ticker(instrument_id=7, *, timestamp=None):
    return {
        "instrument_id": instrument_id,
        "symbol": "BTC-PERP",
        "index_price": "65000.00",
        "mark_price": "65010.00",
        "last_price": "65005.00",
        "mid_price": "65002.50",
        "open_interest": "125.4",
        "funding_rate": "0.0001",
        "next_funding": NOW_MS + 3_600_000,
        "timestamp": NOW_MS if timestamp is None else timestamp,
    }


def book(instrument_id=7, *, timestamp=None):
    return {
        "instrument_id": instrument_id,
        "bids": [["65000.00", "0.75"], ["64999.50", "1.2"]],
        "asks": [["65005.00", "0.6"], ["65005.50", "1.1"]],
        "timestamp": NOW_MS if timestamp is None else timestamp,
        "sequence": 123456,
    }


class FakePerpsProvider(PerpsPublicMarketDataProvider):
    def __init__(self, instruments, tickers, books, **kwargs):
        super().__init__(**kwargs)
        self.instruments_payload = instruments
        self.tickers_payload = tickers
        self.books_payload = books
        self.calls = []

    def _get_json(self, path, params=None):
        self.calls.append((path, params))
        if path == "/v1/info/instruments":
            return self.instruments_payload
        if path == "/v1/info/tickers":
            return self.tickers_payload
        if path == "/v1/info/book":
            return self.books_payload[params["instrument_id"]]
        raise AssertionError(f"unexpected endpoint {path}")


class PerpsMarketDataTests(unittest.TestCase):
    def test_normalizes_instrument_and_preserves_risk_tiers(self):
        result = _normalize_instrument(instrument())
        self.assertEqual(result["instrument_id"], 7)
        self.assertEqual(result["symbol"], "BTC-PERP")
        self.assertEqual(result["max_leverage"], "20")
        self.assertEqual(result["risk_tiers"][1]["max_leverage"], "10")
        self.assertFalse(result["close_only"])

    def test_skips_close_only_and_settled_instruments(self):
        self.assertIsNone(_normalize_instrument(instrument(close_only=True)))
        retired = instrument()
        retired["settlement"] = {"price": "100"}
        self.assertIsNone(_normalize_instrument(retired))

    def test_rejects_invalid_or_non_monotonic_risk_tiers(self):
        invalid = instrument()
        invalid["risk_tiers"] = [{"lower_bound": "0", "max_leverage": "30"}]
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            _normalize_instrument(invalid)
        invalid["risk_tiers"] = [
            {"lower_bound": "0", "max_leverage": "20"},
            {"lower_bound": "0", "max_leverage": "10"},
        ]
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            _normalize_instrument(invalid)

    def test_rejects_missing_required_instrument_constraints(self):
        invalid = instrument()
        del invalid["min_notional"]
        with self.assertRaisesRegex(ValueError, "min_notional"):
            _normalize_instrument(invalid)

    def test_ticker_validation_requires_fresh_timestamp(self):
        result = _normalize_ticker(ticker(), 7, NOW, 10)
        self.assertEqual(result["mark_price"], "65010.00")
        self.assertEqual(result["funding_rate"], "0.0001")
        stale = ticker(timestamp=NOW_MS - 11_000)
        with self.assertRaisesRegex(ValueError, "stale or future"):
            _normalize_ticker(stale, 7, NOW, 10)

    def test_ticker_rejects_future_timestamp_and_instrument_mismatch(self):
        with self.assertRaisesRegex(ValueError, "stale or future"):
            _normalize_ticker(ticker(timestamp=NOW_MS + 3_000), 7, NOW, 10)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            _normalize_ticker(ticker(instrument_id=8), 7, NOW, 10)

    def test_ticker_requires_positive_prices_and_finite_decimal_values(self):
        invalid = ticker()
        invalid["mark_price"] = "NaN"
        with self.assertRaisesRegex(ValueError, "finite"):
            _normalize_ticker(invalid, 7, NOW, 10)
        invalid = ticker()
        invalid["index_price"] = "0"
        with self.assertRaisesRegex(ValueError, "positive"):
            _normalize_ticker(invalid, 7, NOW, 10)

    def test_book_normalizes_api_array_levels_and_preserves_timestamp(self):
        result = _normalize_book(book(), 7, NOW, 10)
        self.assertEqual(result["best_bid"], "65000.00")
        self.assertEqual(result["best_ask"], "65005.00")
        self.assertEqual(result["sequence"], 123456)
        self.assertEqual(result["bids"][0], {"price": "65000.00", "quantity": "0.75"})

    def test_book_also_accepts_official_sdk_object_level_shape(self):
        raw = book()
        raw["instrumentId"] = raw.pop("instrument_id")
        raw["bids"] = [{"price": "65000.00", "quantity": "0.75"}]
        raw["asks"] = [{"price": "65005.00", "quantity": "0.6"}]
        result = _normalize_book(raw, 7, NOW, 10)
        self.assertEqual(result["best_ask"], "65005.00")

    def test_book_rejects_stale_crossed_and_out_of_order_levels(self):
        with self.assertRaisesRegex(ValueError, "stale or future"):
            _normalize_book(book(timestamp=NOW_MS - 11_000), 7, NOW, 10)
        crossed = book()
        crossed["asks"][0][0] = "65000.00"
        with self.assertRaisesRegex(ValueError, "crossed or locked"):
            _normalize_book(crossed, 7, NOW, 10)
        unsorted = book()
        unsorted["bids"].reverse()
        with self.assertRaisesRegex(ValueError, "not price ordered"):
            _normalize_book(unsorted, 7, NOW, 10)

    def test_book_rejects_malformed_depth_and_zero_quantity(self):
        invalid = book()
        invalid["asks"][0][1] = "0"
        with self.assertRaisesRegex(ValueError, "positive"):
            _normalize_book(invalid, 7, NOW, 10)
        invalid = book()
        invalid["bids"] = []
        with self.assertRaisesRegex(ValueError, "empty"):
            _normalize_book(invalid, 7, NOW, 10)

    def test_provider_builds_fresh_snapshots_from_public_endpoints(self):
        provider = FakePerpsProvider(
            [instrument()], [ticker()], {7: book()}, market_limit=3
        )
        result = provider.load(now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["instrument"]["symbol"], "BTC-PERP")
        self.assertEqual(result[0]["ticker"]["mark_price"], "65010.00")
        self.assertEqual(result[0]["book"]["best_bid"], "65000.00")
        self.assertTrue(result[0]["research_only"])
        self.assertEqual(result[0]["execution"], "none")
        self.assertEqual(
            [call[0] for call in provider.calls],
            ["/v1/info/instruments", "/v1/info/tickers", "/v1/info/book"],
        )

    def test_provider_skips_close_only_and_stale_instruments(self):
        provider = FakePerpsProvider(
            [instrument(7, close_only=True), {**instrument(8), "symbol": "ETH-PERP"}],
            [ticker(8, timestamp=NOW_MS - 11_000)],
            {8: book(8)},
        )
        with self.assertRaises(PerpsNoUsableMarkets):
            provider.load(now=NOW)

    def test_provider_limits_market_book_requests(self):
        instruments = []
        tickers = []
        books = {}
        for instrument_id in range(1, 5):
            instruments.append({**instrument(instrument_id), "symbol": f"COIN{instrument_id}-PERP"})
            tickers.append({**ticker(instrument_id), "symbol": f"COIN{instrument_id}-PERP"})
            books[instrument_id] = book(instrument_id)
        provider = FakePerpsProvider(instruments, tickers, books, market_limit=2)
        result = provider.load(now=NOW)
        self.assertEqual(len(result), 2)
        self.assertEqual(
            sum(1 for path, _ in provider.calls if path == "/v1/info/book"), 2
        )

    def test_provider_rejects_naive_now(self):
        provider = FakePerpsProvider([instrument()], [ticker()], {7: book()})
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            provider.load(now=NOW.replace(tzinfo=None))

    def test_constructor_rejects_unsafe_age_and_depth(self):
        with self.assertRaisesRegex(ValueError, "max_age_seconds"):
            PerpsPublicMarketDataProvider(max_age_seconds=60)
        with self.assertRaisesRegex(ValueError, "depth"):
            PerpsPublicMarketDataProvider(depth=25)


if __name__ == "__main__":
    unittest.main()
