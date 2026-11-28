import unittest

from news_fusion import (
    canonical_article_identity_url,
    canonical_article_url,
    merge_news_sources,
    summarize_news_source_coverage,
)


class NewsFusionTests(unittest.TestCase):
    def test_tracking_parameters_and_fragments_do_not_duplicate_the_same_article(self):
        self.assertEqual(
            canonical_article_url(
                "https://www.bbc.com/news/world-123?utm_source=rss&fbclid=abc#section"
            ),
            "https://www.bbc.com/news/world-123",
        )
        self.assertEqual(
            canonical_article_identity_url("https://www.bbc.com/news/world-123"),
            "https://bbc.com/news/world-123",
        )
        rss = [{
            "id": "rss-copy",
            "source": "BBC News - World",
            "source_type": "rss_atom",
            "publisher_id": "bbc",
            "url": "https://www.bbc.com/news/world-123?utm_source=rss#section",
            "title": "Fed holds interest rates steady",
            "summary": "Officials left rates unchanged.",
            "published_at": "2026-11-23T10:00:00+00:00",
            "fetched_at": "2026-11-23T10:01:00+00:00",
            "source_language": "en",
            "coverage_region": "global",
        }]
        gdelt = [{
            "id": "gdelt-copy",
            "source": "bbc.com",
            "source_type": "gdelt_doc_api",
            "publisher_id": "bbc",
            "url": "https://bbc.com/news/world-123?fbclid=tracking",
            "title": "Fed holds interest rates steady",
            "published_at": "",
            "indexed_at": "2026-11-23T10:02:00+00:00",
            "fetched_at": "2026-11-23T10:03:00+00:00",
            "source_language": "en",
            "language": "English",
            "coverage_region": "US",
            "topic_categories": ["macroeconomics_finance"],
        }]

        rows = merge_news_sources(rss, gdelt)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://www.bbc.com/news/world-123")
        self.assertEqual(row["publisher_id"], "bbc")
        self.assertEqual(row["published_at"], "2026-11-23T10:00:00+00:00")
        self.assertEqual(row["indexed_at"], "2026-11-23T10:02:00+00:00")
        self.assertEqual(row["publication_time_status"], "publisher_timestamp_available")
        self.assertEqual(row["timestamp_basis"], "publisher_publication_plus_gdelt_index_observation")
        self.assertEqual(row["source_types"], ["gdelt_doc_api", "rss_atom"])
        self.assertEqual(row["source_languages"], ["en", "english"])
        self.assertTrue(row["directional_analysis_supported"])
        self.assertEqual(row["topic_categories"], ["macroeconomics_finance"])
        self.assertEqual(row["source_count"], 2)

        coverage = summarize_news_source_coverage(rows)
        self.assertEqual(coverage["unique_article_count"], 1)
        self.assertEqual(coverage["distinct_publisher_count"], 1)
        self.assertEqual(coverage["articles_with_publisher_publication_time"], 1)
        self.assertEqual(coverage["articles_with_index_observation_time"], 1)

    def test_gdelt_index_time_alone_never_becomes_publisher_publication_time(self):
        rows = merge_news_sources([{
            "id": "gdelt-only",
            "source": "example.com",
            "source_type": "gdelt_doc_api",
            "publisher_id": "example.com",
            "url": "https://example.com/news/1",
            "title": "Global policy development",
            "published_at": "",
            "indexed_at": "2026-11-23T10:00:00+00:00",
            "fetched_at": "2026-11-23T10:01:00+00:00",
            "source_language": "en",
            "directional_analysis_supported": True,
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["published_at"], "")
        self.assertEqual(rows[0]["indexed_at"], "2026-11-23T10:00:00+00:00")
        self.assertEqual(rows[0]["publication_time_status"], "unknown")
        self.assertEqual(rows[0]["timestamp_basis"], "index_time_not_publication_time")
        self.assertTrue(rows[0]["directional_analysis_supported"])

        coverage = summarize_news_source_coverage(rows)
        self.assertEqual(coverage["articles_with_publisher_publication_time"], 0)
        self.assertEqual(
            coverage["articles_with_index_time_only_or_unknown_publication_time"], 1
        )

    def test_mixed_language_versions_of_one_article_are_not_directionally_scored(self):
        rows = merge_news_sources(
            [{
                "id": "rss-en", "source_type": "rss_atom", "source": "BBC",
                "url": "https://www.bbc.com/story/1", "title": "Fed holds rates steady",
                "published_at": "2026-11-23T10:00:00+00:00", "source_language": "en",
            }],
            [{
                "id": "gdelt-ar", "source_type": "gdelt_doc_api", "source": "bbc.com",
                "url": "https://bbc.com/story/1?utm_source=gdelt", "title": "Fed holds rates steady",
                "published_at": "", "indexed_at": "2026-11-23T10:01:00+00:00",
                "source_language": "ar",
            }],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_languages"], ["ar", "en"])
        self.assertFalse(rows[0]["directional_analysis_supported"])


if __name__ == "__main__":
    unittest.main()
