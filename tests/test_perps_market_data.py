from datetime import datetime, timezone
import unittest

from perps_market_data import PerpsMarketDataError, PerpsMarketDataProvider


class FakeTransport:
    def __init__(self, *, ticker_age_ms=1000, book_age_ms=1000, crossed=False):
        now_ms = 1_800_000_000_000
        self.now_ms = now_ms
        self.ticker_age_ms = ticker_age_ms
        self.book_age_ms = book_age_ms
        self.crossed = crossed
        self.calls = []

    def __call__(self, path, params, timeout):
        self.calls.append((path, dict(params), timeout))
        if path == "/v1/info/instruments":
            return [{
                "instrument_id": 1,
                "instrument_type": "perpetual",
                "category": "crypto",
                "symbol": "BTC-PERP",
                "close_only": False,
                "settlement": None,
                "base_asset": "BTC",
                "quote_asset": "USD",
                "funding_interval": "1h",
                "quantity_decimals": 4,
                "price_decimals": 2,
                "min_notional": "1",
                "max_market_notional": "100000",
                "max_leverage": 10,
                "risk_tiers": [{"lower_bound": "0", "max_leverage": 10}],
            }]
        if path == "/v1/info/tickers":
            return [{
                "instrument_id": 1,
                "symbol": "BTC-PERP",
                "index_price": "65000",
                "mark_price": "65001",
                "last_price": "65002",
                "mid_price": "65001",
                "open_interest": "25",
                "funding_rate": "0.0001",
                "next_funding": self.now_ms + 3_600_000,
                "timestamp": self.now_ms - self.ticker_age_ms,
            }]
        if path == "/v1/info/book":
            bid = "65002" if self.crossed else "65000"
            return {
                "instrument_id": 1,
                "bids": [[bid, "1"], ["64999", "2"]],
                "asks": [["65001", "1"], ["65002", "2"]],
                "timestamp": self.now_ms - self.book_age_ms,
                "sequence": 123,
            }
        raise AssertionError(path)


class PerpsMarketDataTests(unittest.TestCase):
    def test_loads_public_perps_snapshot_without_credentials(self):
        transport = FakeTransport()
        provider = PerpsMarketDataProvider(
            transport=transport, market_limit=2,
            max_age_seconds=10, max_spread_bps=250,
        )
        now = datetime.fromtimestamp(transport.now_ms / 1000, timezone.utc)
        rows = provider.load_snapshots(now=now)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "BTC-PERP")
        self.assertEqual(row["mark_price"], "65001")
        self.assertTrue(row["entry_eligible"])
        self.assertEqual(row["execution"], "none")
        self.assertTrue(all(path.startswith("/v1/info/") for path, _, _ in transport.calls))
        self.assertEqual(transport.calls[-1][1]["depth"], "10")

    def test_stale_ticker_is_rejected(self):
        transport = FakeTransport(ticker_age_ms=11_000)
        provider = PerpsMarketDataProvider(transport=transport, max_age_seconds=10)
        now = datetime.fromtimestamp(transport.now_ms / 1000, timezone.utc)
        with self.assertRaisesRegex(PerpsMarketDataError, "no_usable_perps_markets"):
            provider.load_snapshots(now=now)

    def test_crossed_book_is_rejected(self):
        transport = FakeTransport(crossed=True)
        provider = PerpsMarketDataProvider(transport=transport)
        now = datetime.fromtimestamp(transport.now_ms / 1000, timezone.utc)
        with self.assertRaisesRegex(PerpsMarketDataError, "no_usable_perps_markets"):
            provider.load_snapshots(now=now)

    def test_future_timestamp_is_rejected(self):
        transport = FakeTransport(ticker_age_ms=-5000)
        provider = PerpsMarketDataProvider(transport=transport)
        now = datetime.fromtimestamp(transport.now_ms / 1000, timezone.utc)
        with self.assertRaisesRegex(PerpsMarketDataError, "no_usable_perps_markets"):
            provider.load_snapshots(now=now)

    def test_naive_now_is_rejected(self):
        provider = PerpsMarketDataProvider(transport=FakeTransport())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            provider.load_snapshots(now=datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
