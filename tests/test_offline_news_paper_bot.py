import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from offline_news_paper_bot import OfflineNewsPaperBot, PaperConfig


class FakeNewsCollector:
    def __init__(self):
        self.items = []

    def fetch_once(self):
        return {"feeds_configured": 0, "feeds_succeeded": 0, "feeds_failed": 0}


class StaticProvider:
    def __init__(self, row):
        self.row = row

    def load(self, *, now=None):
        return [dict(self.row)]


class OfflineNewsPaperBotTests(unittest.TestCase):
    def _make_config(self, folder):
        env = {
            "DRY_RUN": "true",
            "LIVE_EXECUTION_ENABLED": "false",
            "PAPER_DATABASE_PATH": str(Path(folder) / "paper.sqlite3"),
            "NEWS_LOG_PATH": str(Path(folder) / "news.jsonl"),
            "MARKET_SNAPSHOT_FILE": str(Path(folder) / "markets.json"),
        }
        return patch.dict(os.environ, env, clear=True)

    def test_unknown_market_data_source_is_rejected(self):
        with patch.dict(os.environ, {"MARKET_DATA_SOURCE": "authenticated_account"}, clear=True):
            with self.assertRaisesRegex(ValueError, "MARKET_DATA_SOURCE"):
                PaperConfig()

    def test_live_execution_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"DRY_RUN": "true", "LIVE_EXECUTION_ENABLED": "true"}, clear=True):
                with self.assertRaisesRegex(ValueError, "not part"):
                    PaperConfig()

    def test_dry_run_must_remain_true(self):
        with patch.dict(os.environ, {"DRY_RUN": "false"}, clear=True):
            with self.assertRaisesRegex(ValueError, "requires DRY_RUN=true"):
                PaperConfig()

    def test_one_offline_cycle_stays_simulated_only(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        row = {
            "market_id": "local-market-1", "question": "Will the example energy bill pass?",
            "yes_token_id": "local-market-1:YES", "no_token_id": "local-market-1:NO",
            "observed_at": now.isoformat(), "yes_price": 0.45, "no_price": 0.55,
            "yes_bid": 0.44, "yes_ask": 0.46, "no_bid": 0.54, "no_ask": 0.56,
        }
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(),
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    result = bot.run_once(now=now, fetch_news=False)
                    self.assertEqual(result["mode"], "OFFLINE_NEWS_PAPER_ONLY")
                    self.assertTrue(result["dry_run"])
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["portfolio"]["open_positions"], 0)
                    self.assertEqual(result["portfolio"]["cash"], 100.0)
                finally:
                    bot.close()


if __name__ == "__main__":
    unittest.main()
