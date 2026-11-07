import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from offline_news_paper_bot import OfflineNewsPaperBot, PaperConfig


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
            "MARKET_HISTORY_DATABASE_PATH": str(Path(folder) / "market_history.sqlite3"),
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

    def test_open_trade_uses_depth_weighted_ask_price_and_blocks_without_depth(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        row = {
            "market_id": "depth-market", "question": "Will the example energy bill pass?",
            "yes_token_id": "yes-token", "no_token_id": "no-token",
            "observed_at": now.isoformat(), "yes_price": 0.41, "no_price": 0.59,
            "yes_bid": 0.40, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.60,
            "yes_book": {
                "asks_top5": [{"price": 0.42, "size": 2}, {"price": 0.44, "size": 20}],
                "bids_top5": [{"price": 0.40, "size": 20}],
            },
        }
        signal = {
            "direction": "YES", "score": 0.9, "threshold": 0.75,
            "publisher_count": 2, "sentiment_confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {"PAPER_MAX_ENTRY_IMPACT_BPS": "500"}):
                bot = OfflineNewsPaperBot(PaperConfig(), news_collector=FakeNewsCollector(),
                                          snapshot_provider=StaticProvider(row))
                try:
                    opened, reason = bot._open_trade(row, signal, now)
                    self.assertEqual(reason, "")
                    self.assertIsNotNone(opened)
                    self.assertGreater(opened["entry_price"], row["yes_ask"])
                    self.assertEqual(opened["levels_used"], 2)
                    self.assertGreater(opened["price_impact_bps"], 0)

                    no_depth = {**row, "market_id": "no-depth", "yes_book": {"asks_top5": [], "bids_top5": []}}
                    blocked, block_reason = bot._open_trade(
                        no_depth, {**signal, "direction": "YES"}, now
                    )
                    self.assertIsNone(blocked)
                    self.assertEqual(block_reason, "visible_ask_depth_unavailable")
                finally:
                    bot.close()

    def test_close_requires_full_visible_bid_depth_and_uses_weighted_exit_price(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        row = {
            "market_id": "exit-depth-market", "question": "Will the example energy bill pass?",
            "yes_token_id": "yes-token", "no_token_id": "no-token",
            "observed_at": now.isoformat(), "yes_price": 0.41, "no_price": 0.59,
            "yes_bid": 0.40, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.60,
            "yes_book": {
                "asks_top5": [{"price": 0.42, "size": 2}, {"price": 0.44, "size": 20}],
                "bids_top5": [{"price": 0.40, "size": 20}],
            },
        }
        signal = {
            "direction": "YES", "score": 0.9, "threshold": 0.75,
            "publisher_count": 2, "sentiment_confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder), patch.dict(os.environ, {"PAPER_MAX_ENTRY_IMPACT_BPS": "500"}):
                bot = OfflineNewsPaperBot(PaperConfig(), news_collector=FakeNewsCollector(),
                                          snapshot_provider=StaticProvider(row))
                try:
                    opened, reason = bot._open_trade(row, signal, now)
                    self.assertEqual(reason, "")
                    shares = opened["shares"]
                    exit_row = {
                        **row, "yes_price": 0.38, "yes_bid": 0.38, "yes_ask": 0.40,
                        "yes_book": {
                            "bids_top5": [{"price": 0.38, "size": 2}, {"price": 0.36, "size": 20}],
                            "asks_top5": [{"price": 0.40, "size": 20}],
                        },
                    }
                    closed, blocked = bot._close_due_trades(
                        {"exit-depth-market": exit_row},
                        now.replace(minute=5),
                    )
                    self.assertEqual(blocked, [])
                    self.assertEqual(len(closed), 1)
                    self.assertLess(closed[0]["exit_price"], 0.38)
                    self.assertEqual(closed[0]["levels_used"], 2)

                    # A fresh position must remain open when the visible bid ladder
                    # cannot absorb the entire simulated exit size.
                    other = {**row, "market_id": "thin-exit-market"}
                    opened2, reason2 = bot._open_trade(other, signal, now)
                    self.assertEqual(reason2, "")
                    thin = {
                        **exit_row, "market_id": "thin-exit-market",
                        "yes_book": {
                            "bids_top5": [{"price": 0.38, "size": max(0.001, opened2["shares"] / 10)}],
                            "asks_top5": [{"price": 0.40, "size": 20}],
                        },
                    }
                    closed2, blocked2 = bot._close_due_trades(
                        {"thin-exit-market": thin}, now.replace(minute=5)
                    )
                    self.assertEqual(closed2, [])
                    self.assertEqual(blocked2[0]["reason"], "insufficient_visible_bid_depth")
                finally:
                    bot.close()

    def test_entry_blocks_excessive_visible_depth_price_impact(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        row = {
            "market_id": "high-impact-market", "question": "Will the example energy bill pass?",
            "yes_token_id": "yes-token", "no_token_id": "no-token",
            "observed_at": now.isoformat(), "yes_price": 0.46, "no_price": 0.54,
            "yes_bid": 0.41, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.59,
            "yes_book": {
                "asks_top5": [{"price": 0.42, "size": 0.1}, {"price": 0.55, "size": 20}],
                "bids_top5": [{"price": 0.41, "size": 20}],
            },
        }
        signal = {
            "direction": "YES", "score": 0.9, "threshold": 0.75,
            "publisher_count": 2, "sentiment_confidence": 0.9,
        }
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(PaperConfig(), news_collector=FakeNewsCollector(),
                                          snapshot_provider=StaticProvider(row))
                try:
                    opened, reason = bot._open_trade(row, signal, now)
                    self.assertIsNone(opened)
                    self.assertEqual(reason, "entry_impact_above_limit")
                    self.assertEqual(bot.summary()["open_positions"], 0)
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
                    self.assertEqual(result["mode"], "OFFLINE_NEWS_PAPER_ONLY")
                    self.assertTrue(result["dry_run"])
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["portfolio"]["open_positions"], 0)
                    self.assertEqual(result["portfolio"]["cash"], 100.0)
                    self.assertEqual(result["market_history"]["inserted"], 1)
                    self.assertEqual(result["market_history"]["total_snapshots"], 1)

                    repeated = bot.run_once(now=now, fetch_news=False)
                    self.assertEqual(repeated["market_history"]["inserted"], 0)
                    self.assertEqual(repeated["market_history"]["duplicates"], 1)
                    self.assertEqual(repeated["market_history"]["total_snapshots"], 1)
                finally:
                    bot.close()


if __name__ == "__main__":
    unittest.main()
