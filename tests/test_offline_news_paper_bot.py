import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from offline_news_paper_bot import OfflineNewsPaperBot, PaperConfig
from live_market_snapshot_provider import LiveClobMarketSnapshotProvider


class FakeNewsCollector:
    def __init__(self, items=None):
        self.items = list(items or [])

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

    def test_event_evidence_threshold_defaults_to_consensus_floor(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                config = PaperConfig()
                self.assertEqual(config.min_event_evidence_threshold, 0.75)

    def test_event_evidence_threshold_rejects_values_below_consensus_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {
                "PAPER_EVENT_EVIDENCE_THRESHOLD": "0.50"
            }):
                with self.assertRaisesRegex(ValueError, "PAPER_EVENT_EVIDENCE_THRESHOLD"):
                    PaperConfig()

    def test_market_data_source_defaults_to_local_json(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                config = PaperConfig()
                self.assertEqual(config.market_data_source, "local_json")
                self.assertEqual(config.live_market_max_markets, 10)
                self.assertEqual(config.live_market_max_book_age_seconds, 30.0)

    def test_live_public_provider_is_selected_only_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {
                "PAPER_MARKET_DATA_SOURCE": "polymarket_public"
            }):
                config = PaperConfig()
                bot = OfflineNewsPaperBot(
                    config, news_collector=FakeNewsCollector(),
                )
                try:
                    self.assertIsInstance(bot.snapshot_provider, LiveClobMarketSnapshotProvider)
                    self.assertTrue(config.market_data_source == "polymarket_public")
                    self.assertTrue(os.environ["DRY_RUN"] == "true")
                finally:
                    bot.close()

    def test_unknown_market_data_source_is_rejected_at_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {
                "PAPER_MARKET_DATA_SOURCE": "automatic"
            }):
                with self.assertRaisesRegex(ValueError, "PAPER_MARKET_DATA_SOURCE"):
                    PaperConfig()

    def test_live_public_data_limits_are_validated_even_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {
                "PAPER_LIVE_MARKET_MAX_MARKETS": "0"
            }):
                with self.assertRaisesRegex(ValueError, "PAPER_LIVE_MARKET_MAX_MARKETS"):
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

    def test_event_outcome_consensus_sets_direction_instead_of_vader_sentiment(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        row = {
            "market_id": "market-fed",
            "question": "Will the Fed cut interest rates in December?",
            "yes_token_id": "market-fed:YES",
            "no_token_id": "market-fed:NO",
            "observed_at": now.isoformat(),
            "yes_price": 0.45, "no_price": 0.55,
            "yes_bid": 0.44, "yes_ask": 0.46,
            "no_bid": 0.54, "no_ask": 0.56,
        }
        items = [
            {"id": "1", "source": "Reuters", "publisher_id": "reuters.com",
             "title": "Fed holds interest rates steady", "summary": "The central bank made no change.",
             "published_at": now.isoformat()},
            {"id": "2", "source": "AP", "publisher_id": "apnews.com",
             "title": "Fed holds interest rates steady", "summary": "Officials kept rates unchanged.",
             "published_at": now.isoformat()},
        ]
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(items),
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    signal = bot._build_signals(items, [row], now)["market-fed"]
                    self.assertTrue(signal["qualified"])
                    self.assertEqual(signal["direction"], "NO")
                    self.assertLess(signal["score"], 0)
                    self.assertEqual(signal["publisher_count"], 2)
                    self.assertEqual(signal["method"], "explicit_event_cue_publisher_consensus_v1")
                    self.assertEqual(signal["sentiment_interpretation"],
                                     "diagnostic_only_not_used_for_yes_no_direction")
                finally:
                    bot.close()

    def test_generic_negative_sentiment_without_event_cue_does_not_create_direction(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        row = {
            "market_id": "market-fed",
            "question": "Will the Fed cut interest rates in December?",
            "yes_token_id": "market-fed:YES",
            "no_token_id": "market-fed:NO",
            "observed_at": now.isoformat(),
            "yes_price": 0.45, "no_price": 0.55,
            "yes_bid": 0.44, "yes_ask": 0.46,
            "no_bid": 0.54, "no_ask": 0.56,
        }
        items = [
            {"id": "1", "source": "Reuters", "publisher_id": "reuters.com",
             "title": "Fed warns investors about December interest rates amid economic crisis",
             "summary": "Officials discussed risks around interest rates.",
             "published_at": now.isoformat()},
            {"id": "2", "source": "AP", "publisher_id": "apnews.com",
             "title": "Fed warns investors about December interest rates amid economic crisis",
             "summary": "Officials discussed risks around interest rates.",
             "published_at": now.isoformat()},
        ]
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(items),
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    signal = bot._build_signals(items, [row], now)["market-fed"]
                    self.assertFalse(signal["qualified"])
                    self.assertIsNone(signal["direction"])
                    self.assertIsNone(signal["score"])
                finally:
                    bot.close()

    def test_conflicting_publishers_below_consensus_threshold_do_not_qualify(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        row = {
            "market_id": "market-fed",
            "question": "Will the Fed cut interest rates in December?",
            "yes_token_id": "market-fed:YES",
            "no_token_id": "market-fed:NO",
            "observed_at": now.isoformat(),
            "yes_price": 0.45, "no_price": 0.55,
            "yes_bid": 0.44, "yes_ask": 0.46,
            "no_bid": 0.54, "no_ask": 0.56,
        }
        items = [
            {"id": "1", "source": "Reuters", "publisher_id": "reuters.com",
             "title": "Fed cuts interest rates", "summary": "A rate cut was announced.",
             "published_at": now.isoformat()},
            {"id": "2", "source": "AP", "publisher_id": "apnews.com",
             "title": "Fed cuts interest rates", "summary": "A rate cut was announced.",
             "published_at": now.isoformat()},
            {"id": "3", "source": "BBC News", "publisher_id": "bbc.com",
             "title": "Fed hikes interest rates", "summary": "Rates were increased.",
             "published_at": now.isoformat()},
        ]
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(items),
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    signal = bot._build_signals(items, [row], now)["market-fed"]
                    self.assertFalse(signal["qualified"])
                    self.assertIsNone(signal["direction"])
                    self.assertAlmostEqual(signal["consensus_ratio"], 2 / 3, places=5)
                finally:
                    bot.close()

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
                    self.assertEqual(result["mode"], "NEWS_PAPER_RESEARCH_ONLY")
                    self.assertEqual(result["market_data_source"], "local_json")
                    self.assertEqual(result["market_snapshots_loaded"], 1)
                    self.assertTrue(result["market_data_diagnostics"]["research_only"])
                    self.assertTrue(result["dry_run"])
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["portfolio"]["open_positions"], 0)
                    self.assertEqual(result["portfolio"]["cash"], 100.0)
                finally:
                    bot.close()


if __name__ == "__main__":
    unittest.main()
