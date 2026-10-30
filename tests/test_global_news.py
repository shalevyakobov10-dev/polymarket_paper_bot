import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import global_news


class GDELTCollectorTests(unittest.TestCase):
    def test_normalizes_articles_and_deduplicates_urls(self):
        payload = {"articles": [
            {"title": "Global event update", "url": "https://example.org/a", "seendate": "20261030T120000Z", "domain": "example.org", "sourcecountry": "US", "language": "English"},
            {"title": "Duplicate syndicated copy", "url": "https://example.org/a#fragment"},
            {"title": "Unsafe URL", "url": "http://example.org/b"},
            {"title": "", "url": "https://example.org/c"},
        ]}
        rows = global_news.normalize_gdelt_articles(payload, "2026-10-30T12:01:00+00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "2026-10-30T12:00:00+00:00")
        self.assertEqual(rows[0]["source_country"], "US")
        self.assertEqual(rows[0]["source_type"], "gdelt_doc_api")
        self.assertEqual(rows[0]["execution"], "none")

    def test_rejects_missing_articles_collection(self):
        with self.assertRaises(ValueError):
            global_news.normalize_gdelt_articles({})

    def test_fetch_once_persists_and_deduplicates(self):
        payload = {"articles": [
            {"title": "Global event headline", "url": "https://example.org/story", "seendate": "20261030T120000Z", "domain": "example.org"}
        ]}

        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                import json
                return json.dumps(payload).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gdelt.jsonl"
            collector = global_news.GDELTCollector(path)
            with patch("global_news.urlopen", return_value=FakeResponse()):
                first = collector.fetch_once()
                second = collector.fetch_once()
            self.assertEqual(first["items_added"], 1)
            self.assertEqual(second["items_added"], 0)
            restarted = global_news.GDELTCollector(path)
            with patch("global_news.urlopen", return_value=FakeResponse()):
                third = restarted.fetch_once()
            self.assertEqual(third["items_added"], 0)
            self.assertEqual(len(path.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
