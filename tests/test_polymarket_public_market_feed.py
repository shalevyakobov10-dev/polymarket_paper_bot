import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_public_market_feed import PolymarketPublicMarketSnapshotProvider


NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)


def binary_market():
    # Deliberately reverse the outcomes: YES/NO must be mapped by labels.
    return {
        "id": "market-feed-1",
        "conditionId": "condition-feed-1",
        "question": "Will the example bill pass?",
        "active": True,
        "closed": False,
        "outcomes": '["No", "Yes"]',
        "outcomePrices": '["0.59", "0.41"]',
        "clobTokenIds": '["token-no-1", "token-yes-1"]',
        "volume24hr": "12000",
    }


def rest_books(now=NOW, *, yes_age=1, no_age=2):
    yes_bid, yes_ask = 0.40, 0.42
    no_bid, no_ask = 0.58, 0.60
    return {
        "token-yes-1": {
            "token_id": "token-yes-1", "best_bid": yes_bid, "best_ask": yes_ask,
            "midpoint": 0.41, "spread_bps": 487.8,
            "observed_at": (now - timedelta(seconds=yes_age)).isoformat(),
            "book_usable": True, "crossed_book": False,
        },
        "token-no-1": {
            "token_id": "token-no-1", "best_bid": no_bid, "best_ask": no_ask,
            "midpoint": 0.59, "spread_bps": 339.0,
            "observed_at": (now - timedelta(seconds=no_age)).isoformat(),
            "book_usable": True, "crossed_book": False,
        },
    }


def ws_records(now=NOW, *, yes_age=1, no_age=2):
    return [
        {
            "event_type": "book", "token_id": "token-yes-1",
            "best_bid": 0.43, "best_ask": 0.45, "midpoint": 0.44,
            "observed_at": (now - timedelta(seconds=yes_age)).isoformat(),
        },
        {
            "event_type": "book", "token_id": "token-no-1",
            "best_bid": 0.55, "best_ask": 0.57, "midpoint": 0.56,
            "observed_at": (now - timedelta(seconds=no_age)).isoformat(),
        },
    ]


class FakeStreamCollector:
    def __init__(self, path, records):
        self.path = Path(path)
        self.records = records
        self.calls = []

    def run_session(self, token_ids, max_seconds=45):
        self.calls.append((list(token_ids), max_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record) + "\n")
        return {
            "connected": True, "subscribed_tokens": len(token_ids),
            "events_persisted": len(self.records), "reason": None,
        }


class PolymarketPublicMarketSnapshotProviderTests(unittest.TestCase):
    def provider(self, market=None, book_map=None, *, max_skew=5, **kwargs):
        item = market or binary_market()
        result = book_map or rest_books()
        gamma_calls = []

        def gamma_fetcher(page_size, offset):
            gamma_calls.append((page_size, offset))
            return [item] if offset == 0 else []

        def book_fetcher(rows):
            if rows:
                self.assertEqual(
                    {row["token_id"] for row in rows},
                    {"token-yes-1", "token-no-1"},
                )
            return result

        provider = PolymarketPublicMarketSnapshotProvider(
            max_age_seconds=180,
            max_markets=10,
            max_quote_skew_seconds=max_skew,
            gamma_page_fetcher=gamma_fetcher,
            book_fetcher=book_fetcher,
            **kwargs,
        )
        return provider, gamma_calls

    def test_maps_yes_no_by_label_and_uses_public_rest_midpoints(self):
        provider, calls = self.provider()
        snapshots = provider.load(now=NOW)
        self.assertEqual(len(snapshots), 1)
        row = snapshots[0]
        self.assertEqual(row["yes_token_id"], "token-yes-1")
        self.assertEqual(row["no_token_id"], "token-no-1")
        self.assertAlmostEqual(row["yes_price"], 0.41)
        self.assertAlmostEqual(row["no_price"], 0.59)
        self.assertAlmostEqual(row["gamma_yes_probability"], 0.41)
        self.assertAlmostEqual(row["gamma_no_probability"], 0.59)
        self.assertEqual(row["market_data_source"], "official_sdk_rest_snapshot")
        self.assertTrue(row["research_only"])
        self.assertEqual(row["execution"], "none")
        self.assertEqual(provider.last_diagnostics["snapshots_returned"], 1)
        self.assertEqual(calls[0][1], 0)

    def test_non_binary_outcomes_are_skipped(self):
        market = binary_market()
        market["outcomes"] = ["Up", "Down"]
        provider, _ = self.provider(market=market, book_fetcher=lambda _rows: {})
        self.assertEqual(provider.load(now=NOW), [])
        self.assertEqual(provider.last_diagnostics["skipped_non_binary"], 1)

    def test_stale_rest_book_is_skipped(self):
        provider, _ = self.provider(book_map=rest_books(yes_age=200, no_age=1))
        self.assertEqual(provider.load(now=NOW), [])
        self.assertEqual(provider.last_diagnostics["skipped_stale_book"], 1)

    def test_rest_quote_time_skew_is_skipped(self):
        provider, _ = self.provider(book_map=rest_books(yes_age=1, no_age=12), max_skew=5)
        self.assertEqual(provider.load(now=NOW), [])
        self.assertEqual(provider.last_diagnostics["skipped_quote_time_skew"], 1)

    def test_missing_one_rest_side_is_skipped(self):
        data = rest_books()
        data.pop("token-no-1")
        provider, _ = self.provider(book_map=data)
        self.assertEqual(provider.load(now=NOW), [])
        self.assertEqual(provider.last_diagnostics["skipped_missing_or_invalid_book"], 1)

    def test_crossed_rest_book_is_skipped(self):
        data = rest_books()
        data["token-yes-1"]["book_usable"] = False
        data["token-yes-1"]["crossed_book"] = True
        provider, _ = self.provider(book_map=data)
        self.assertEqual(provider.load(now=NOW), [])
        self.assertEqual(provider.last_diagnostics["skipped_missing_or_invalid_book"], 1)

    def test_fresh_complete_websocket_pair_is_preferred_over_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market_stream.jsonl"
            collector = FakeStreamCollector(path, ws_records())
            provider, _ = self.provider(
                websocket_enabled=True, stream_collector=collector,
                stream_log_path=path, websocket_session_seconds=12,
            )
            snapshots = provider.load(now=NOW)
            self.assertEqual(len(snapshots), 1)
            row = snapshots[0]
            self.assertEqual(row["market_data_source"], "public_clob_market_websocket")
            self.assertAlmostEqual(row["yes_price"], 0.44)
            self.assertAlmostEqual(row["no_price"], 0.56)
            self.assertEqual(collector.calls[0][1], 12)
            self.assertEqual(provider.last_diagnostics["source_counts"]["public_clob_market_websocket"], 1)

    def test_partial_websocket_pair_falls_back_to_paired_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market_stream.jsonl"
            collector = FakeStreamCollector(path, ws_records()[:1])
            provider, _ = self.provider(
                websocket_enabled=True, stream_collector=collector,
                stream_log_path=path,
            )
            row = provider.load(now=NOW)[0]
            self.assertEqual(row["market_data_source"], "official_sdk_rest_snapshot")
            self.assertAlmostEqual(row["yes_price"], 0.41)
            self.assertAlmostEqual(row["no_price"], 0.59)

    def test_stale_websocket_pair_falls_back_to_fresh_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market_stream.jsonl"
            collector = FakeStreamCollector(path, ws_records(yes_age=400, no_age=401))
            provider, _ = self.provider(
                websocket_enabled=True, stream_collector=collector,
                stream_log_path=path,
            )
            row = provider.load(now=NOW)[0]
            self.assertEqual(row["market_data_source"], "official_sdk_rest_snapshot")
            self.assertAlmostEqual(row["yes_price"], 0.41)

    def test_skewed_websocket_pair_falls_back_to_paired_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market_stream.jsonl"
            collector = FakeStreamCollector(path, ws_records(yes_age=1, no_age=12))
            provider, _ = self.provider(
                websocket_enabled=True, stream_collector=collector,
                stream_log_path=path, max_skew=5,
            )
            row = provider.load(now=NOW)[0]
            self.assertEqual(row["market_data_source"], "official_sdk_rest_snapshot")
            self.assertAlmostEqual(row["yes_price"], 0.41)
            self.assertAlmostEqual(row["no_price"], 0.59)


if __name__ == "__main__":
    unittest.main()
