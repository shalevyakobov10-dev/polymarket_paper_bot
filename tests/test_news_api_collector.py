from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import news_api_collector


NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
API_KEY = "do-not-leak-newsapi-secret"


class FakeResponse:
    status = 200

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def article(url="https://www.reuters.com/world/example-story?utm_source=newsapi"):
    return {
        "source": {"id": "reuters", "name": "Reuters"},
        "author": "Reporter",
        "title": "Central bank holds interest rates steady",
        "description": "The central bank kept rates unchanged.",
        "url": url,
        "publishedAt": "2026-11-23T17:30:00Z",
        "content": "Truncated publisher content",
    }


def payload(rows=None):
    return {"status": "ok", "totalResults": len(rows or []), "articles": list(rows or [])}


class NewsAPICollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "newsapi.jsonl"

    def test_disabled_by_default_never_makes_a_network_request(self):
        with patch.dict("os.environ", {}, clear=True):
            collector = news_api_collector.NewsAPICollector(self.path)
            with patch("news_api_collector.urlopen") as mocked:
                result = collector.fetch_once(now=NOW)

        mocked.assert_not_called()
        self.assertFalse(result["enabled"])
        self.assertFalse(result["poll_status_available"])
        self.assertEqual(result["requests_made"], 0)
        self.assertEqual(result["items_total_recent"], 0)

    def test_explicitly_enabled_without_api_key_fails_closed(self):
        with patch.dict("os.environ", {}, clear=True):
            collector = news_api_collector.NewsAPICollector(
                self.path, enabled=True, api_key=""
            )
            with patch("news_api_collector.urlopen") as mocked:
                result = collector.fetch_once(now=NOW)

        mocked.assert_not_called()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["query_errors"], [{"code": "api_key_missing"}])
        self.assertEqual(result["requests_made"], 0)
        self.assertNotIn(API_KEY, json.dumps(result))

    def test_sends_api_key_only_in_header_and_rotates_topic_language_combinations(self):
        requests = []
        body = payload([article()])

        def fake_urlopen(request, timeout=12):
            requests.append((request, timeout))
            return FakeResponse(body)

        with patch.dict("os.environ", {
            "NEWSAPI_REQUESTS_PER_POLL": "2",
            "NEWSAPI_PAGE_SIZE": "100",
            "NEWSAPI_MIN_REQUEST_INTERVAL_SECONDS": "1.0",
        }, clear=True):
            collector = news_api_collector.NewsAPICollector(
                self.path,
                enabled=True,
                api_key=API_KEY,
                languages=("en", "ar"),
                now_provider=lambda: NOW,
            )
            sleeps = []
            collector._sleep = sleeps.append
            collector._monotonic = iter((100.0, 102.0)).__next__
            with patch("news_api_collector.urlopen", side_effect=fake_urlopen):
                result = collector.fetch_once(now=NOW)

        self.assertEqual(len(requests), 2)
        first, second = requests
        self.assertEqual(first[0].get_header("X-api-key"), API_KEY)
        self.assertEqual(second[0].get_header("X-api-key"), API_KEY)
        self.assertNotIn(API_KEY, first[0].full_url)
        self.assertNotIn(API_KEY, second[0].full_url)
        first_query = parse_qs(urlsplit(first[0].full_url).query)
        second_query = parse_qs(urlsplit(second[0].full_url).query)
        self.assertEqual(first_query["language"], ["en"])
        self.assertEqual(second_query["language"], ["ar"])
        self.assertEqual(first_query["pageSize"], ["100"])
        self.assertEqual(first_query["sortBy"], ["publishedAt"])
        self.assertEqual(first_query["from"], [(NOW - timedelta(hours=24)).isoformat().replace("+00:00", "Z")])
        self.assertEqual(first_query["to"], [NOW.isoformat().replace("+00:00", "Z")])
        self.assertEqual(result["requests_made"], 2)
        self.assertEqual(result["queries_succeeded"], 2)
        self.assertEqual(result["queries_failed"], 0)
        self.assertEqual(result["languages_attempted"], ["ar", "en"])
        self.assertEqual(sleeps, [])

    def test_deduplicates_same_article_and_preserves_language_and_publication_provenance(self):
        rows = [article(), article("https://reuters.com/world/example-story?utm_medium=email#top")]
        with patch("news_api_collector.urlopen", return_value=FakeResponse(payload(rows))):
            collector = news_api_collector.NewsAPICollector(
                self.path,
                enabled=True,
                api_key=API_KEY,
                languages=("en",),
                now_provider=lambda: NOW,
            )
            result = collector.fetch_once(now=NOW)

        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["items_total_recent"], 1)
        item = collector.items[0]
        self.assertEqual(item["source_type"], "newsapi_everything")
        self.assertEqual(item["source_language"], "en")
        self.assertTrue(item["directional_analysis_supported"])
        self.assertEqual(item["published_at"], "2026-11-23T17:30:00+00:00")
        self.assertEqual(item["publication_time_status"], "provider_reported_publication_time")
        self.assertEqual(item["timestamp_basis"], "newsapi_published_at")
        self.assertEqual(item["publisher_id"], "reuters")

    def test_non_english_articles_remain_research_only_and_are_not_directionally_scored(self):
        with patch("news_api_collector.urlopen", return_value=FakeResponse(payload([article()]))):
            collector = news_api_collector.NewsAPICollector(
                self.path,
                enabled=True,
                api_key=API_KEY,
                languages=("ar",),
                now_provider=lambda: NOW,
            )
            result = collector.fetch_once(now=NOW)

        item = collector.items[0]
        self.assertEqual(result["items_added"], 1)
        self.assertEqual(item["source_language"], "ar")
        self.assertFalse(item["directional_analysis_supported"])
        self.assertTrue(item["research_only"])
        self.assertEqual(item["execution"], "none")

    def test_rotation_cursor_persists_between_collector_restarts(self):
        with patch.dict("os.environ", {"NEWSAPI_REQUESTS_PER_POLL": "1"}, clear=True):
            first = news_api_collector.NewsAPICollector(
                self.path,
                enabled=True,
                api_key=API_KEY,
                languages=("en", "ar"),
                now_provider=lambda: NOW,
            )
            with patch("news_api_collector.urlopen", return_value=FakeResponse(payload([]))) as mocked:
                first.fetch_once(now=NOW)
            first_url = mocked.call_args.args[0].full_url

            second = news_api_collector.NewsAPICollector(
                self.path,
                enabled=True,
                api_key=API_KEY,
                languages=("en", "ar"),
                now_provider=lambda: NOW,
            )
            with patch("news_api_collector.urlopen", return_value=FakeResponse(payload([]))) as mocked:
                second.fetch_once(now=NOW)
            second_url = mocked.call_args.args[0].full_url

        self.assertNotEqual(
            parse_qs(urlsplit(first_url).query)["language"],
            parse_qs(urlsplit(second_url).query)["language"],
        )
        state_text = second.state_path.read_text(encoding="utf-8")
        self.assertNotIn(API_KEY, state_text)
        self.assertNotIn("secret", state_text.lower())

    def test_http_errors_are_sanitized_and_do_not_persist_credentials(self):
        from urllib.error import HTTPError

        with patch("news_api_collector.urlopen", side_effect=HTTPError(
            "https://newsapi.org/v2/everything?q=test", 401, "unauthorized", {}, None
        )):
            collector = news_api_collector.NewsAPICollector(
                self.path, enabled=True, api_key=API_KEY,
                languages=("en",), now_provider=lambda: NOW,
            )
            result = collector.fetch_once(now=NOW)

        self.assertEqual(result["queries_failed"], 1)
        self.assertEqual(result["query_errors"][0]["code"], "http_401")
        self.assertNotIn(API_KEY, json.dumps(result))
        if self.path.exists():
            self.assertNotIn(API_KEY, self.path.read_text(encoding="utf-8"))

    def test_archive_snapshot_does_not_make_network_requests(self):
        with patch("news_api_collector.urlopen") as mocked:
            collector = news_api_collector.NewsAPICollector(
                self.path, enabled=False, api_key=API_KEY
            )
            result = collector.read_archive_snapshot()

        mocked.assert_not_called()
        self.assertFalse(result["poll_status_available"])
        self.assertEqual(result["requests_made"], 0)
        self.assertEqual(result["interpretation"], "shared_newsapi_archive_snapshot_not_a_new_poll")

    def test_rejects_unsupported_language_and_unsafe_configuration(self):
        with self.assertRaisesRegex(ValueError, "documented two-letter"):
            news_api_collector.NewsAPICollector(
                self.path, enabled=True, api_key=API_KEY, languages=("xx",)
            )
        with patch.dict("os.environ", {"NEWSAPI_PAGE_SIZE": "1000"}, clear=True):
            collector = news_api_collector.NewsAPICollector(
                self.path, enabled=True, api_key=API_KEY, languages=("en",)
            )
        self.assertEqual(collector.page_size, 100)


if __name__ == "__main__":
    unittest.main()
