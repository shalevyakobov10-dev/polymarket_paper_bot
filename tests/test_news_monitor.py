import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import news_monitor


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item>
<title>Major event &amp; update</title>
<link>https://example.org/story/1</link>
<guid>story-1</guid>
<pubDate>Sun, 25 Oct 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>Verified context &amp; background.</p>]]></description>
</item></channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>tag:example.org,2026:2</id>
<title>Atom headline</title>
<link href="https://example.org/story/2" rel="alternate"/>
<updated>2026-10-25T10:00:00Z</updated>
<summary>Brief description</summary>
</entry></feed>"""


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=None):
        return self.payload if limit is None else self.payload[:limit]


class NewsMonitorTests(unittest.TestCase):
    def test_parse_rss_keeps_source_link_and_cleans_summary(self):
        rows = news_monitor.parse_feed(RSS, "Example News", "https://example.org/rss")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Major event & update")
        self.assertEqual(rows[0]["url"], "https://example.org/story/1")
        self.assertIn("Verified context & background.", rows[0]["summary"])
        self.assertEqual(rows[0]["source"], "Example News")
        self.assertTrue(rows[0]["id"])

    def test_parse_atom_supports_namespaces_and_link_attributes(self):
        rows = news_monitor.parse_feed(ATOM, "Example Atom", "https://example.org/atom")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Atom headline")
        self.assertEqual(rows[0]["url"], "https://example.org/story/2")

    def test_rejects_malformed_xml_and_non_http_links(self):
        with self.assertRaises(ValueError):
            news_monitor.parse_feed(b"<rss>", "bad", "https://example.org/rss")
        invalid = b"<rss><channel><item><title>No link</title><link>javascript:alert(1)</link></item></channel></rss>"
        self.assertEqual(news_monitor.parse_feed(invalid, "bad", "https://example.org/rss"), [])

    def test_collector_deduplicates_items_across_polls_and_restarts(self):
        feeds = ({"source": "Example News", "url": "https://example.org/rss"},)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.jsonl"
            collector = news_monitor.NewsCollector(path, feeds)
            with patch("news_monitor.urlopen", return_value=FakeResponse(RSS)):
                first = collector.fetch_once()
                second = collector.fetch_once()
            self.assertEqual(first["feeds_succeeded"], 1)
            self.assertEqual(first["items_added"], 1)
            self.assertEqual(second["items_added"], 0)
            restarted = news_monitor.NewsCollector(path, feeds)
            with patch("news_monitor.urlopen", return_value=FakeResponse(RSS)):
                third = restarted.fetch_once()
            self.assertEqual(third["items_added"], 0)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_one_failed_feed_does_not_block_other_sources(self):
        feeds = (
            {"source": "Working", "url": "https://example.org/rss"},
            {"source": "Unavailable", "url": "https://offline.example/rss"},
        )

        def fake_urlopen(request, timeout=10):
            if "offline.example" in request.full_url:
                raise URLError("offline")
            return FakeResponse(RSS)

        with tempfile.TemporaryDirectory() as directory:
            collector = news_monitor.NewsCollector(Path(directory) / "news.jsonl", feeds)
            with patch("news_monitor.urlopen", side_effect=fake_urlopen):
                result = collector.fetch_once()
        self.assertEqual(result["feeds_succeeded"], 1)
        self.assertEqual(result["feeds_failed"], 1)
        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["feed_errors"][0]["source"], "Unavailable")


    def test_matcher_links_specific_shared_terms_without_calling_them_probability(self):
        news = {
            "id": "headline-1",
            "source": "Example Finance",
            "title": "Fed signals interest-rate cuts",
            "summary": "The Federal Reserve signaled a possible cut in interest rates.",
            "url": "https://example.org/fed",
            "published_at": "Sun, 25 Oct 2026 10:00:00 GMT",
        }
        markets = [{
            "market_id": "market-fed",
            "question": "Will the Fed cut interest rates in December?",
        }]
        matches = news_monitor.match_news_to_markets(news, markets)
        self.assertEqual(len(matches), 1)
        self.assertIn("rate", matches[0]["matched_terms"])
        self.assertGreaterEqual(matches[0]["shared_term_count"], 2)
        self.assertTrue(matches[0]["research_only"])
        self.assertEqual(matches[0]["execution"], "none")
        self.assertIn("lexical_overlap", matches[0])
        self.assertNotIn("probability", matches[0])

    def test_matcher_deduplicates_pairs_across_calls_and_restarts(self):
        news = {
            "id": "headline-2",
            "source": "Example Finance",
            "title": "Fed signals interest-rate cuts",
            "summary": "The Federal Reserve signaled a possible cut in interest rates.",
            "url": "https://example.org/fed",
        }
        markets = [{
            "market_id": "market-fed",
            "question": "Will the Fed cut interest rates in December?",
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matches.jsonl"
            matcher = news_monitor.NewsMarketMatcher(path)
            first = matcher.match([news], markets)
            second = matcher.match([news], markets)
            restarted = news_monitor.NewsMarketMatcher(path)
            third = restarted.match([news], markets)
            self.assertEqual(len(first["added"]), 1)
            self.assertEqual(second["added"], [])
            self.assertEqual(third["added"], [])
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_matcher_ignores_generic_or_unrelated_overlap(self):
        news = {
            "id": "headline-3",
            "title": "Local club wins championship final",
            "summary": "Fans celebrate after the match.",
        }
        markets = [{
            "market_id": "market-fed",
            "question": "Will the Federal Reserve cut interest rates in December?",
        }]
        self.assertEqual(news_monitor.match_news_to_markets(news, markets), [])


    def test_same_story_has_same_id_across_sources(self):
        first = news_monitor.parse_feed(RSS, "Feed A", "https://example.org/rss-a")
        second = news_monitor.parse_feed(RSS, "Feed B", "https://example.org/rss-b")
        self.assertEqual(first[0]["id"], second[0]["id"])

    def test_recent_feed_items_are_sorted_by_publication_time_across_feeds(self):
        older = (
            RSS.replace(b"Major event &amp; update", b"Older event")
            .replace(b"https://example.org/story/1", b"https://example.org/story/old")
            .replace(b"story-1", b"story-old")
            .replace(b"10:00:00 GMT", b"09:00:00 GMT")
        )
        newer = (
            RSS.replace(b"Major event &amp; update", b"Newer event")
            .replace(b"https://example.org/story/1", b"https://example.org/story/new")
            .replace(b"story-1", b"story-new")
            .replace(b"10:00:00 GMT", b"11:00:00 GMT")
        )
        feeds = (
            {"source": "Older", "url": "https://example.org/old.xml"},
            {"source": "Newer", "url": "https://example.org/new.xml"},
        )

        def fake_urlopen(request, timeout=10):
            return FakeResponse(older if "/old.xml" in request.full_url else newer)

        with tempfile.TemporaryDirectory() as directory:
            collector = news_monitor.NewsCollector(Path(directory) / "news.jsonl", feeds)
            with patch("news_monitor.urlopen", side_effect=fake_urlopen):
                result = collector.fetch_once()
        self.assertEqual(result["items_added"], 2)
        self.assertEqual(result["latest_items"][0]["title"], "Newer event")


    def test_news_freshness_reports_publish_and_fetch_age_separately(self):
        now = news_monitor.datetime(2026, 10, 30, 12, 0, tzinfo=news_monitor.timezone.utc)
        items = [
            {"published_at": "Fri, 30 Oct 2026 11:00:00 GMT", "fetched_at": "2026-10-30T11:59:00Z"},
            {"published_at": "invalid", "fetched_at": "2026-10-30T11:58:00+00:00"},
        ]
        summary = news_monitor.summarize_news_freshness(items, now)
        self.assertEqual(summary["items_considered"], 2)
        self.assertEqual(summary["items_with_publish_time"], 1)
        self.assertEqual(summary["seconds_since_latest_publish"], 3600.0)
        self.assertEqual(summary["seconds_since_latest_fetch"], 60.0)
        self.assertIn("not_event_verification", summary["interpretation"])

    def test_news_freshness_handles_empty_and_invalid_timestamps(self):
        summary = news_monitor.summarize_news_freshness([{"published_at": "bad", "fetched_at": "bad"}])
        self.assertEqual(summary["items_considered"], 1)
        self.assertIsNone(summary["seconds_since_latest_publish"])
        self.assertIsNone(summary["seconds_since_latest_fetch"])

    def test_corroboration_counts_distinct_publishers_not_feed_sections(self):
        matches = [
            {"market_id": "m1", "market_question": "Question", "source": "BBC News - World"},
            {"market_id": "m1", "market_question": "Question", "source": "BBC News - Business"},
            {"market_id": "m1", "market_question": "Question", "source": "The Guardian - World"},
            {"market_id": "m1", "market_question": "Question", "source": "NPR - Top Stories"},
        ]
        summary = news_monitor.summarize_market_corroboration(matches)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["distinct_publisher_count"], 3)
        self.assertEqual(summary[0]["matched_headline_count"], 4)
        self.assertEqual(summary[0]["publishers"], ["BBC News", "NPR", "The Guardian"])
        self.assertEqual(
            summary[0]["interpretation"],
            "source_diversity_only_not_independent_event_verification",
        )
        self.assertEqual(summary[0]["execution"], "none")

    def test_corroboration_ignores_missing_market_or_source(self):
        matches = [
            {"market_id": "", "source": "BBC News"},
            {"market_id": "m1", "source": ""},
            {"market_id": "m2", "source": "NPR"},
        ]
        summary = news_monitor.summarize_market_corroboration(matches)
        self.assertEqual([row["market_id"] for row in summary], ["m2"])


if __name__ == "__main__":
    unittest.main()
