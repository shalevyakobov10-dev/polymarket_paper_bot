import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from offline_news_paper_bot import OfflineNewsPaperBot, PaperConfig
from news_fusion import merge_news_sources


class FakeNewsCollector:
    def __init__(self, items=None):
        self.items = list(items or [])

    def fetch_once(self):
        return {"feeds_configured": 0, "feeds_succeeded": 0, "feeds_failed": 0}


class FakeArchiveGDELTCollector:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.calls = 0
        self.fetch_calls = 0

    def read_archive_snapshot(self):
        self.calls += 1
        return {
            "topics_configured": 18,
            "topics_seen_in_archive": ["geopolitics_security", "macroeconomics_finance"],
            "network_requests_made": 0,
            "items_added": 0,
            "items_total_recent": len(self.items),
            "latest_items": list(self.items),
        }

    def fetch_once(self):
        self.fetch_calls += 1
        raise AssertionError("Paper must not poll GDELT independently")


class FakeGDELTCollector:
    def __init__(self, items=None, result=None):
        self.items = list(items or [])
        self.result = dict(result or {
            "topics_configured": 17,
            "topics_succeeded": 4,
            "topics_failed": 0,
            "items_added": len(self.items),
            "items_total_recent": len(self.items),
            "topics_attempted": ["geopolitics_security", "macroeconomics_finance"],
            "topic_errors": [],
        })
        self.calls = 0

    def fetch_once(self):
        self.calls += 1
        return dict(self.result)


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
                    self.assertTrue(opened["independent_risk_gate"]["approved"])
                    self.assertEqual(
                        opened["independent_risk_gate"]["reason"], "approved_paper_entry"
                    )

                    no_depth = {**row, "market_id": "no-depth", "yes_book": {"asks_top5": [], "bids_top5": []}}
                    blocked, block_reason = bot._open_trade(
                        no_depth, {**signal, "direction": "YES"}, now
                    )
                    self.assertIsNone(blocked)
                    self.assertEqual(block_reason, "visible_ask_depth_unavailable")
                finally:
                    bot.close()

    def test_independent_risk_gate_can_block_entry_after_primary_sizing(self):
        from types import SimpleNamespace

        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        row = {
            "market_id": "independent-risk-market", "question": "Will the example energy bill pass?",
            "yes_token_id": "yes-token", "no_token_id": "no-token",
            "observed_at": now.isoformat(), "yes_price": 0.41, "no_price": 0.59,
            "yes_bid": 0.40, "yes_ask": 0.42, "no_bid": 0.58, "no_ask": 0.60,
            "yes_book": {
                "asks_top5": [{"price": 0.42, "size": 100}],
                "bids_top5": [{"price": 0.40, "size": 100}],
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
                    with patch("offline_news_paper_bot.evaluate_paper_entry",
                               return_value=SimpleNamespace(
                                   approved=False, reason="daily_loss_limit_reached",
                                   as_dict=lambda: {"approved": False, "reason": "daily_loss_limit_reached"},
                               )) as risk_gate:
                        opened, reason = bot._open_trade(row, signal, now)
                    self.assertIsNone(opened)
                    self.assertEqual(reason, "independent_risk_gate:daily_loss_limit_reached")
                    self.assertEqual(bot.summary()["open_positions"], 0)
                    risk_gate.assert_called_once()
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

    def test_summary_includes_minimum_sample_gated_closed_trade_performance(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).isoformat()
        opened = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(),
                    snapshot_provider=StaticProvider({}),
                )
                try:
                    with bot.db:
                        for index in range(60):
                            pnl = 1.0 if index < 40 else -0.5
                            bot.db.execute(
                                "INSERT INTO trades (market_id,question,direction,token_id,opened_at,"
                                "entry_price,shares,notional,entry_fee,stop_loss_price,take_profit_price,"
                                "signal_score,signal_strength,sentiment_confidence,publisher_count,"
                                "entry_threshold,last_mark_price,status,closed_at,exit_price,exit_fee,"
                                "pnl,net_roi,exit_reason) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CLOSED',?,?,?,?,?,?)",
                                (
                                    f"market-{index}", "Example question", "YES", f"token-{index}",
                                    opened, 0.4, 2.5, 1.0, 0.05, 0.3, 0.6, 0.9, 0.9, 0.9, 2,
                                    0.75, 0.4, now, 0.5, 0.05, pnl, pnl / 1.05, "time_limit",
                                ),
                            )
                    performance = bot.summary()["performance"]
                    self.assertEqual(performance["closed_trades"], 60)
                    self.assertEqual(performance["wins"], 40)
                    self.assertEqual(performance["losses"], 20)
                    self.assertEqual(performance["evaluation_status"], "positive_in_sample_unvalidated")
                    self.assertFalse(performance["out_of_sample_validated"])
                    self.assertFalse(performance["live_execution_eligible"])
                finally:
                    bot.close()

    def test_summary_exposes_last_tuning_audit_record(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(),
                    snapshot_provider=StaticProvider({}),
                )
                try:
                    report = {
                        "status": "rejected",
                        "applied": False,
                        "input_dataset_sha256": "a" * 64,
                        "evaluation_run_sha256": "b" * 64,
                        "live_execution_eligible": False,
                        "real_money_execution": "not_implemented",
                    }
                    with bot.db:
                        bot.db.execute(
                            "INSERT INTO tuning_runs(ran_at,result_json) VALUES(?,?)",
                            (now := datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).isoformat(),
                             json.dumps(report)),
                        )
                    summary = bot.summary()
                    self.assertEqual(summary["last_tuning"]["ran_at"], now)
                    self.assertEqual(summary["last_tuning"]["result"], report)
                finally:
                    bot.close()

    def test_summary_handles_unreadable_tuning_audit_record_without_failing(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(), news_collector=FakeNewsCollector(),
                    snapshot_provider=StaticProvider({}),
                )
                try:
                    with bot.db:
                        bot.db.execute(
                            "INSERT INTO tuning_runs(ran_at,result_json) VALUES(?,?)",
                            ("2026-11-07T12:00:00+00:00", "{broken-json"),
                        )
                    last_tuning = bot.summary()["last_tuning"]
                    self.assertEqual(last_tuning["status"], "unreadable_audit_record")
                    self.assertFalse(last_tuning["applied"])
                finally:
                    bot.close()

    def test_paper_cycle_merges_gdelt_news_and_keeps_non_english_out_of_directional_scoring(self):
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
        rss = FakeNewsCollector([{
            "id": "rss-english", "source": "BBC World", "publisher_id": "bbc",
            "source_language": "en", "directional_analysis_supported": True,
            "coverage_region": "global",
            "title": "Fed holds interest rates steady",
            "summary": "Officials made no change to interest rates.",
            "published_at": now.isoformat(),
        }])
        gdelt = FakeGDELTCollector([
            {
                "id": "gdelt-english", "source": "example.com",
                "publisher_id": "example.com", "source_language": "en",
                "directional_analysis_supported": True, "coverage_region": "US",
                "title": "Fed holds interest rates steady",
                "summary": "", "published_at": now.isoformat(),
            },
            {
                "id": "gdelt-arabic", "source": "example-ar.org",
                "publisher_id": "example-ar.org", "source_language": "ar",
                "directional_analysis_supported": False, "coverage_region": "MENA",
                "title": "قرار جديد بشأن السياسة النقدية في الولايات المتحدة",
                "summary": "", "published_at": now.isoformat(),
            },
        ])
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(),
                    news_collector=rss,
                    gdelt_collector=gdelt,
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    result = bot.run_once(now=now, fetch_news=True)
                    self.assertEqual(gdelt.calls, 1)
                    self.assertEqual(result["gdelt_topics_configured"], 17)
                    self.assertEqual(result["gdelt_topics_succeeded"], 4)
                    self.assertEqual(result["gdelt_items_added"], 2)
                    self.assertEqual(result["news_items_considered"], 3)
                    self.assertEqual(result["news_languages_seen"], ["ar", "en"])
                    self.assertEqual(result["news_regions_seen"], ["global", "mena", "us"])
                    self.assertEqual(result["news_non_english_items_unscored"], 1)

                    signal = bot._build_signals(
                        rss.items + gdelt.items, [row], now
                    )["market-fed"]
                    self.assertEqual(signal["publisher_count"], 2)
                    self.assertEqual(signal["direction"], "NO")
                    self.assertEqual(signal["article_count"], 2)
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["execution"], "none")
                finally:
                    bot.close()

    def test_paper_cycle_deduplicates_rss_gdelt_article_and_preserves_publisher_time(self):
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
        rss = FakeNewsCollector([
            {
                "id": "rss-bbc",
                "source": "BBC News - World",
                "source_type": "rss_atom",
                "publisher_id": "bbc",
                "url": "https://www.bbc.com/news/123?utm_source=rss",
                "source_language": "en",
                "directional_analysis_supported": True,
                "coverage_region": "global",
                "title": "Fed holds interest rates steady",
                "summary": "Officials left rates unchanged.",
                "published_at": now.isoformat(),
                "fetched_at": now.isoformat(),
            },
            {
                "id": "rss-reuters",
                "source": "Reuters",
                "source_type": "rss_atom",
                "publisher_id": "reuters",
                "url": "https://www.reuters.com/world/456",
                "source_language": "en",
                "directional_analysis_supported": True,
                "coverage_region": "global",
                "title": "Fed holds interest rates steady",
                "summary": "The central bank kept rates unchanged.",
                "published_at": now.isoformat(),
                "fetched_at": now.isoformat(),
            },
        ])
        gdelt = FakeGDELTCollector([
            {
                "id": "gdelt-bbc",
                "source": "bbc.com",
                "source_type": "gdelt_doc_api",
                "publisher_id": "bbc",
                "url": "https://bbc.com/news/123#top",
                "source_language": "en",
                "language": "English",
                "directional_analysis_supported": True,
                "coverage_region": "US",
                "title": "Fed holds interest rates steady",
                "summary": "",
                "published_at": "",
                "indexed_at": now.isoformat(),
                "fetched_at": now.isoformat(),
                "topic_categories": ["macroeconomics_finance"],
            }
        ])
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(),
                    news_collector=rss,
                    gdelt_collector=gdelt,
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    result = bot.run_once(now=now, fetch_news=True)
                    self.assertEqual(gdelt.calls, 1)
                    self.assertEqual(result["news_items_considered"], 2)
                    self.assertEqual(result["news_source_coverage"]["unique_article_count"], 2)
                    self.assertEqual(result["news_source_coverage"]["distinct_publisher_count"], 2)
                    self.assertEqual(
                        result["news_source_coverage"]["articles_with_publisher_publication_time"],
                        2,
                    )
                    self.assertEqual(result["news_source_coverage"]["articles_with_index_observation_time"], 1)
                    self.assertIn("gdelt_doc_api", result["news_sources_used"])
                    self.assertIn("rss_atom", result["news_sources_used"])
                    self.assertEqual(result["qualified_event_signals"], 1)
                    self.assertFalse(result["live_execution_enabled"])
                    self.assertEqual(result["execution"], "none")

                    merged_bbc = next(
                        item for item in merge_news_sources(rss.items, gdelt.items)
                        if item["publisher_id"] == "bbc"
                    )
                    self.assertEqual(merged_bbc["published_at"], now.isoformat())
                    self.assertEqual(merged_bbc["indexed_at"], now.isoformat())
                    self.assertEqual(merged_bbc["source_types"], ["gdelt_doc_api", "rss_atom"])
                    self.assertTrue(merged_bbc["directional_analysis_supported"])
                finally:
                    bot.close()

    def test_paper_reads_shared_gdelt_archive_without_duplicate_network_polling(self):
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
        rss = FakeNewsCollector()
        gdelt = FakeArchiveGDELTCollector([{
            "id": "shared-archive-article",
            "source": "Reuters",
            "publisher_id": "reuters.com",
            "source_language": "en",
            "directional_analysis_supported": True,
            "coverage_region": "global",
            "title": "Fed holds interest rates steady",
            "summary": "Officials made no change.",
            "published_at": now.isoformat(),
        }])
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(),
                    news_collector=rss,
                    gdelt_collector=gdelt,
                    snapshot_provider=StaticProvider(row),
                )
                try:
                    result = bot.run_once(now=now, fetch_news=True)
                    self.assertEqual(gdelt.calls, 1)
                    self.assertEqual(gdelt.fetch_calls, 0)
                    self.assertEqual(result["gdelt_items_total_recent"], 1)
                    self.assertEqual(result["gdelt_topics_seen_in_archive_count"], 2)
                    self.assertEqual(result["gdelt_network_requests_made"], 0)
                finally:
                    bot.close()

    def test_injected_rss_collector_does_not_implicitly_start_network_gdelt(self):
        with tempfile.TemporaryDirectory() as folder:
            with self._make_config(folder):
                bot = OfflineNewsPaperBot(
                    PaperConfig(),
                    news_collector=FakeNewsCollector(),
                    snapshot_provider=StaticProvider({}),
                )
                try:
                    self.assertIsNone(bot.gdelt_collector)
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
