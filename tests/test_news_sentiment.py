import unittest
from datetime import datetime, timezone

from news_sentiment import (
    SentimentIntensityAnalyzer,
    aggregate_market_news_sentiment,
    analyze_news_sentiment,
)


class NewsSentimentTests(unittest.TestCase):
    def test_empty_text_fails_closed(self):
        result = analyze_news_sentiment("   ")
        self.assertFalse(result["supported"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["reason"], "empty_text")

    def test_non_english_text_is_not_scored_as_english(self):
        result = analyze_news_sentiment("ההסכם נכשל והמתיחות באזור גוברת")
        self.assertFalse(result["supported"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["reason"], "unsupported_language")

    @unittest.skipIf(SentimentIntensityAnalyzer is None, "vaderSentiment dependency is not installed")
    def test_english_positive_and_negative_headlines_are_bounded(self):
        positive = analyze_news_sentiment("The parties reached a successful peace agreement and signed a deal.")
        negative = analyze_news_sentiment("The violent attack killed civilians and triggered a severe crisis.")
        self.assertTrue(positive["supported"])
        self.assertTrue(negative["supported"])
        self.assertGreater(positive["score"], 0)
        self.assertLess(negative["score"], 0)
        self.assertGreaterEqual(positive["score"], -1)
        self.assertLessEqual(positive["score"], 1)

    def test_aggregate_caps_each_publisher_to_one_vote(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp()
        rows = [
            {"sentiment_supported": True, "sentiment_score": 1.0, "relevance_score": 0.9,
             "published_at_epoch": now - 60, "publisher_id": "publisher-a"},
            {"sentiment_supported": True, "sentiment_score": 1.0, "relevance_score": 0.9,
             "published_at_epoch": now - 120, "publisher_id": "publisher-a"},
            {"sentiment_supported": True, "sentiment_score": -1.0, "relevance_score": 0.9,
             "published_at_epoch": now - 60, "publisher_id": "publisher-b"},
        ]
        result = aggregate_market_news_sentiment(rows, now_epoch=now, minimum_publishers=2)
        self.assertEqual(result["publisher_count"], 2)
        self.assertEqual(result["article_count"], 3)
        self.assertAlmostEqual(result["score"], 0.0, places=5)
        self.assertTrue(result["qualified"])

    def test_old_articles_are_excluded(self):
        result = aggregate_market_news_sentiment(
            [{"sentiment_supported": True, "sentiment_score": 0.8,
              "relevance_score": 0.9, "published_at_epoch": 1,
              "publisher_id": "old-source"}],
            now_epoch=1_800_000_000, max_age_hours=24,
        )
        self.assertIsNone(result["score"])
        self.assertFalse(result["qualified"])


if __name__ == "__main__":
    unittest.main()
