import unittest
from datetime import datetime, timezone

import research_features


class ResearchFeatureTests(unittest.TestCase):
    def test_joins_yes_no_prices_order_book_and_recent_news(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        rows = [
            {
                "market_id": "market-1", "token_id": "yes-token", "question": "Will event happen?",
                "outcome": "Yes", "observed_price": 0.62, "volume_24h": "12000",
                "orderbook": {
                    "observed_at": "2026-10-30T11:59:00+00:00", "book_usable": True,
                    "best_bid": 0.61, "best_ask": 0.63, "midpoint": 0.62, "spread_bps": 322.58,
                    "bid_depth_usd_top5": 50, "ask_depth_usd_top5": 40, "depth_imbalance_top5": 0.111111,
                },
                "market_quality": {"eligible": True, "reason": "approved_market_quality"},
                "fees_enabled": True,
                "fee_type": "general_fees",
                "fee_schedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
            },
            {"market_id": "market-1", "token_id": "no-token", "outcome": "No", "observed_price": 0.38},
            {"market_id": "market-2", "token_id": "maybe", "outcome": "Maybe", "observed_price": 0.5},
        ]
        matches = [
            {
                "market_id": "market-1", "source": "Example News",
                "news_title": "A fresh headline", "news_published_at": "2026-10-30T11:30:00Z",
                "lexical_overlap": 0.75, "shared_term_count": 3,
            },
            {
                "market_id": "market-1", "source": "Old News",
                "news_title": "An old headline", "news_published_at": "2026-10-28T11:00:00Z",
            },
            {
                "market_id": "market-1", "source": "Unknown Time",
                "news_title": "No publication timestamp",
            },
            {
                "market_id": "market-2", "source": "Wrong Market",
                "news_title": "Should not be joined", "news_published_at": "2026-10-30T11:30:00Z",
            },
        ]

        features = research_features.build_market_feature_rows(rows, matches, now=now)

        self.assertEqual(len(features), 1)
        item = features[0]
        self.assertEqual(item["gamma_yes_probability"], 0.62)
        self.assertEqual(item["gamma_no_probability"], 0.38)
        self.assertEqual(item["gamma_binary_sum"], 1.0)
        self.assertEqual(item["volume_24h"], 12000.0)
        self.assertTrue(item["fees_enabled"])
        self.assertEqual(item["fee_type"], "general_fees")
        self.assertEqual(item["fee_schedule_rate"], 0.05)
        self.assertEqual(item["fee_schedule_exponent"], 1.0)
        self.assertEqual(item["clob_spread_bps"], 322.58)
        self.assertEqual(item["clob_book_age_seconds"], 60.0)
        self.assertTrue(item["clob_book_quality_eligible"])
        self.assertEqual(item["news_match_count_24h"], 1)
        self.assertEqual(item["news_unique_source_label_count_24h"], 1)
        self.assertEqual(item["news_matches_without_publish_time"], 1)
        self.assertEqual(item["news_headlines"][0]["title"], "A fresh headline")
        self.assertIsNone(item["forecast"])
        self.assertEqual(item["execution"], "none")

    def test_skips_yes_outcome_with_invalid_price_and_non_yes_only_markets(self):
        rows = [
            {"market_id": "invalid", "outcome": "Yes", "observed_price": float("nan")},
            {"market_id": "no-yes", "outcome": "No", "observed_price": 0.3},
            {"market_id": "other", "outcome": "Maybe", "observed_price": 0.5},
        ]
        self.assertEqual(research_features.build_market_feature_rows(rows, []), [])

    def test_future_published_news_is_not_counted_as_fresh(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        rows = [{"market_id": "m", "outcome": "Yes", "observed_price": 0.5}]
        matches = [{
            "market_id": "m", "source": "Future",
            "news_title": "Future headline", "news_published_at": "2026-10-30T13:00:00Z",
        }]
        item = research_features.build_market_feature_rows(rows, matches, now=now)[0]
        self.assertEqual(item["news_match_count_24h"], 0)
        self.assertEqual(item["news_matches_with_future_publish_time"], 1)

    def test_rejects_invalid_window_or_headline_limit(self):
        with self.assertRaises(ValueError):
            research_features.build_market_feature_rows([], [], news_window_hours=0)
        with self.assertRaises(ValueError):
            research_features.build_market_feature_rows([], [], max_headlines_per_market=-1)

    def test_summary_reports_coverage_not_a_forecast(self):
        rows = [
            {"clob_book_usable": True, "clob_book_quality_eligible": True,
             "clob_spread_bps": 100, "clob_bid_depth_usd_top5": 10,
             "clob_ask_depth_usd_top5": 20, "news_match_count_24h": 2},
            {"clob_book_usable": False, "clob_book_quality_eligible": False,
             "clob_spread_bps": None, "news_match_count_24h": 0},
        ]
        result = research_features.summarize_feature_rows(rows)
        self.assertEqual(result["markets_in_snapshot"], 2)
        self.assertEqual(result["markets_with_usable_clob_book"], 1)
        self.assertEqual(result["news_matches_24h_total"], 2)
        self.assertEqual(result["mean_observed_spread_bps"], 100)
        self.assertIsNone(result.get("forecast"))
        self.assertIn("not_a_probability_forecast", result["interpretation"])

    def test_reads_rotated_jsonl_in_chronological_file_order_and_bounds_output(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "features.jsonl"
            older = root / "features.jsonl.1"
            older.write_text(json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2}) + "\n", encoding="utf-8")
            current.write_text(json.dumps({"n": 3}) + "\n", encoding="utf-8")
            self.assertEqual(
                research_features.read_recent_jsonl([current, older], limit=2),
                [{"n": 2}, {"n": 3}],
            )


if __name__ == "__main__":
    unittest.main()
