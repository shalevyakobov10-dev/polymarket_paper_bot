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


if __name__ == "__main__":
    unittest.main()
