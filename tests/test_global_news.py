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

    def test_normalize_records_topic_category(self):
        payload = {"articles": [
            {"title": "AI chip supply", "url": "https://example.org/ai", "seendate": "20261030T120000Z"}
        ]}
        rows = global_news.normalize_gdelt_articles(
            payload, topic_category="technology_cyber_ai"
        )
        self.assertEqual(rows[0]["topic_categories"], ["technology_cyber_ai"])

    def test_fetch_once_rotates_topics_and_merges_cross_topic_duplicates(self):
        payload = {"articles": [
            {"title": "Global policy update", "url": "https://example.org/shared", "seendate": "20261030T120000Z", "domain": "example.org"}
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

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "GDELT_TOPICS": "geopolitics_security,macroeconomics_finance",
                "GDELT_TOPICS_PER_POLL": "2",
            },
        ):
            path = Path(directory) / "gdelt.jsonl"
            collector = global_news.GDELTCollector(path)
            with patch("global_news.urlopen", return_value=FakeResponse()) as mocked:
                result = collector.fetch_once()
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(result["topics_attempted"], [
                "geopolitics_security", "macroeconomics_finance"
            ])
            self.assertEqual(result["topics_succeeded"], 2)
            self.assertEqual(result["topics_failed"], 0)
            self.assertEqual(result["items_added"], 1)
            self.assertEqual(
                result["latest_items"][0]["topic_categories"],
                ["geopolitics_security", "macroeconomics_finance"],
            )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_fetch_once_isolates_a_failed_topic_when_another_succeeds(self):
        payload = {"articles": [
            {"title": "Health update", "url": "https://example.org/health", "seendate": "20261030T120000Z"}
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

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "GDELT_TOPICS": "health_biotech,science_space",
                "GDELT_TOPICS_PER_POLL": "2",
            },
        ):
            collector = global_news.GDELTCollector(Path(directory) / "gdelt.jsonl")
            with patch(
                "global_news.urlopen",
                side_effect=[RuntimeError("temporary API failure"), FakeResponse()],
            ):
                result = collector.fetch_once()
            self.assertEqual(result["topics_succeeded"], 1)
            self.assertEqual(result["topics_failed"], 1)
            self.assertEqual(result["items_added"], 1)
            self.assertEqual(result["latest_items"][0]["topic_categories"], ["science_space"])

    def test_default_catalog_covers_additional_global_topic_lanes(self):
        names = {name for name, _ in global_news.DEFAULT_TOPIC_QUERIES}
        expected = {
            "law_regulation", "transport_logistics", "agriculture_food",
            "labor_employment", "education_policy", "consumer_retail",
            "migration_demographics", "infrastructure_utilities",
        }
        self.assertTrue(expected.issubset(names))
        self.assertEqual(len(names), len(global_news.DEFAULT_TOPIC_QUERIES))

    def test_default_poll_size_rotates_through_full_catalog(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {}, clear=True):
            first, cursor = global_news._topics_for_poll(global_news.DEFAULT_TOPIC_QUERIES, 0)
            second, cursor = global_news._topics_for_poll(global_news.DEFAULT_TOPIC_QUERIES, cursor)
            third, cursor = global_news._topics_for_poll(global_news.DEFAULT_TOPIC_QUERIES, cursor)
        seen = [name for batch in (first, second, third) for name, _ in batch]
        self.assertEqual(len(first), 6)
        self.assertEqual(len(seen), len(global_news.DEFAULT_TOPIC_QUERIES))
        self.assertEqual(len(set(seen)), len(global_news.DEFAULT_TOPIC_QUERIES))

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
