import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_snapshot_provider import JsonMarketSnapshotProvider, validate_market_snapshot


def sample(now):
    return {
        "market_id": "market-1", "question": "Will the example event happen?",
        "observed_at": now.isoformat(),
        "yes_price": 0.45, "no_price": 0.55,
        "yes_bid": 0.44, "yes_ask": 0.46,
        "no_bid": 0.54, "no_ask": 0.56,
    }


class MarketSnapshotProviderTests(unittest.TestCase):
    def test_valid_local_snapshot_is_normalized(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        result = validate_market_snapshot(sample(now), now=now)
        self.assertEqual(result["yes_token_id"], "market-1:YES")
        self.assertTrue(result["research_only"])
        self.assertEqual(result["execution"], "none")

    def test_stale_snapshot_is_rejected(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        old = sample(datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_market_snapshot(old, now=now, max_age_seconds=60)

    def test_crossed_book_is_rejected(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        row = sample(now)
        row["yes_bid"] = 0.48
        row["yes_ask"] = 0.46
        with self.assertRaisesRegex(ValueError, "crossed"):
            validate_market_snapshot(row, now=now)

    def test_provider_rejects_duplicate_market_ids(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "markets.json"
            path.write_text(json.dumps([sample(now), sample(now)]), encoding="utf-8")
            provider = JsonMarketSnapshotProvider(path)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                provider.load(now=now)


if __name__ == "__main__":
    unittest.main()
