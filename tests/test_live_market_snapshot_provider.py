import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch

import live_market_snapshot_provider as live


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def market():
    return {
        "conditionId": "condition-123",
        "question": "Will the example bill pass by December?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.51", "0.49"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "volume24hr": "12000",
        "active": True,
        "closed": False,
    }


def book(token, bid, ask, observed):
    return {
        "token_id": token,
        "best_bid": bid,
        "best_ask": ask,
        "observed_at": observed,
        "book_usable": True,
        "crossed_book": False,
        "source": "official_polymarket_python_sdk",
        "execution": "none",
    }


class LiveMarketSnapshotProviderTests(unittest.TestCase):
    def test_maps_gamma_yes_no_tokens_to_fresh_clob_midpoints(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])) as request:
            with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                provider = live.LiveClobMarketSnapshotProvider(market_limit=1)
                rows = provider.load(now=now)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["market_id"], "condition-123")
        self.assertEqual(row["yes_token_id"], "yes-token")
        self.assertEqual(row["no_token_id"], "no-token")
        self.assertAlmostEqual(row["yes_price"], 0.51)
        self.assertAlmostEqual(row["no_price"], 0.49)
        self.assertEqual(row["market_source"], "public_gamma_api")
        self.assertEqual(row["book_source"], "official_polymarket_python_sdk")
        self.assertTrue(row["research_only"])
        self.assertEqual(row["execution"], "none")
        request.assert_called_once()
        fetch.assert_called_once_with([{"token_id": "yes-token"}, {"token_id": "no-token"}])

    def test_withholds_market_when_either_book_is_stale(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        stale = "2026-11-07T09:50:00+00:00"
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, fresh),
            "no-token": book("no-token", 0.48, 0.50, stale),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value=books):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_withholds_market_when_one_side_is_not_usable(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        yes = book("yes-token", 0.50, 0.52, stamp)
        no = book("no-token", 0.48, 0.50, stamp)
        no["book_usable"] = False
        with patch.object(live, "urlopen", return_value=FakeResponse([market()])):
            with patch.object(live, "fetch_order_books", return_value={"yes-token": yes, "no-token": no}):
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(now=now)
        self.assertEqual(rows, [])

    def test_ignores_non_binary_or_ambiguous_outcome_markets(self):
        invalid = market()
        invalid["outcomes"] = '["Up", "Down"]'
        with patch.object(live, "urlopen", return_value=FakeResponse([invalid])):
            with patch.object(live, "fetch_order_books") as fetch:
                rows = live.LiveClobMarketSnapshotProvider(market_limit=1).load(
                    now=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
                )
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    def test_supports_gamma_json_string_fields_and_deduplicates_market_ids(self):
        now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        stamp = now.isoformat()
        books = {
            "yes-token": book("yes-token", 0.50, 0.52, stamp),
            "no-token": book("no-token", 0.48, 0.50, stamp),
        }
        with patch.object(live, "urlopen", return_value=FakeResponse([market(), market()])):
            with patch.object(live, "fetch_order_books", return_value=books) as fetch:
                rows = live.LiveClobMarketSnapshotProvider(market_limit=5).load(now=now)
        self.assertEqual(len(rows), 1)
        fetch.assert_called_once_with([{"token_id": "yes-token"}, {"token_id": "no-token"}])


if __name__ == "__main__":
    unittest.main()
