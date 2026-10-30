import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from urllib.request import urlopen as http_urlopen
from urllib.parse import parse_qs, urlparse

import app


class ParserTests(unittest.TestCase):
    def test_fetch_markets_sorts_by_volume_and_limits_tracked_markets(self):
        payload = [
            {"id": "low", "volume24hr": "2"},
            {"id": "high", "volume24hr": "200"},
            {"id": "mid", "volume24hr": "50"},
        ]

        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch.dict(os.environ, {"GAMMA_DISCOVERY_LIMIT": "100", "MARKETS_TO_TRACK": "2"}):
            with patch.object(app, "urlopen", return_value=FakeResponse()) as mocked:
                markets = app.fetch_markets()
        self.assertEqual([market["id"] for market in markets], ["high", "mid"])
        query = parse_qs(urlparse(mocked.call_args.args[0].full_url).query)
        self.assertEqual(query["limit"], ["100"])
        self.assertEqual(query["active"], ["true"])
        self.assertEqual(query["closed"], ["false"])


    def test_fetch_markets_paginates_and_deduplicates_market_ids(self):
        first_page = [
            {"id": f"market-{i}", "volume24hr": str(i)}
            for i in range(20)
        ]
        second_page = [
            {"id": "market-19", "volume24hr": "19"},
            {"id": "market-20", "volume24hr": "100"},
        ]

        class FakeResponse:
            status = 200
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        with patch.dict(os.environ, {
            "GAMMA_DISCOVERY_LIMIT": "20",
            "GAMMA_DISCOVERY_PAGES": "2",
            "MARKETS_TO_TRACK": "100",
        }):
            with patch.object(
                app, "urlopen",
                side_effect=[FakeResponse(first_page), FakeResponse(second_page)],
            ) as mocked:
                markets = app.fetch_markets()

        self.assertEqual(mocked.call_count, 2)
        offsets = [
            parse_qs(urlparse(call.args[0].full_url).query)["offset"][0]
            for call in mocked.call_args_list
        ]
        self.assertEqual(offsets, ["0", "20"])
        self.assertEqual(len(markets), 21)
        self.assertEqual(markets[0]["id"], "market-20")
        self.assertEqual(sum(m["id"] == "market-19" for m in markets), 1)

    def test_parses_gamma_outcome_prices(self):
        markets = [{
            "id": "123",
            "question": "Will this happen?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.62", "0.38"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "volume24hr": 10,
        }]
        rows = app.parse_outcomes(markets)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["observed_price"], 0.62)
        self.assertEqual(rows[0]["token_id"], "yes-token")
        self.assertEqual(rows[1]["outcome"], "No")

    def test_skips_invalid_prices(self):
        rows = app.parse_outcomes([{
            "outcomes": ["Yes", "No", "Maybe"],
            "outcomePrices": ["not-a-price", "1.2", "0.5"],
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_price"], 0.5)

    def test_bad_json_list_fields_are_empty(self):
        self.assertEqual(app._list_field("not json"), [])
        self.assertEqual(app._list_field('{"not":"list"}'), [])

    def test_append_jsonl_writes_one_valid_record(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "record.jsonl"
            app.append_jsonl(target, {"observed_price": 0.5})
            self.assertEqual(json.loads(target.read_text().strip()), {"observed_price": 0.5})

    def test_poll_records_price_change_candidate_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markets = [
                {"id": "m1", "question": "Test?", "outcomes": '["Yes"]',
                 "outcomePrices": '["0.50"]', "clobTokenIds": '["token-1"]'},
                {"id": "m1", "question": "Test?", "outcomes": '["Yes"]',
                 "outcomePrices": '["0.55"]', "clobTokenIds": '["token-1"]'},
            ]
            old_previous = dict(app._previous)
            old_total = app._state["signals_total"]
            old_feature_timestamps = dict(app._research_feature_last_written)
            old_feature_schedule_loaded = app._research_feature_schedule_loaded
            try:
                app._previous.clear()
                app._state["signals_total"] = 0
                app._research_feature_last_written.clear()
                app._research_feature_schedule_loaded = False
                with patch.object(app, "DATA_DIR", root), \
                     patch.object(app, "SNAPSHOTS", root / "snapshots.jsonl"), \
                     patch.object(app, "SIGNALS", root / "signals.jsonl"), \
                     patch.object(app, "RESEARCH_FEATURES", root / "research_features.jsonl"), \
                     patch.object(app, "fetch_markets", side_effect=[[markets[0]], [markets[1]]]), \
                     patch.object(app, "fetch_order_books", return_value={}):
                    first = app.poll_once()
                    app._research_feature_last_written["m1"] = (
                        datetime.now(timezone.utc).timestamp()
                        - app.RESEARCH_FEATURES_SNAPSHOT_SECONDS - 1
                    )
                    second = app.poll_once()
                self.assertEqual(first["candidates"], 0)
                self.assertEqual(second["candidates"], 1)
                record = json.loads((root / "signals.jsonl").read_text().strip())
                self.assertEqual(record["signal"], "momentum_up")
                self.assertEqual(record["execution"], "none")
                self.assertEqual(len((root / "snapshots.jsonl").read_text().splitlines()), 2)
                feature_records = [json.loads(line) for line in (root / "research_features.jsonl").read_text().splitlines()]
                self.assertEqual(first["research_features_written"], 1)
                self.assertEqual(second["research_features_written"], 1)
                self.assertEqual(len(feature_records), 2)
                self.assertEqual(feature_records[-1]["gamma_yes_probability"], 0.55)
                self.assertIsNone(feature_records[-1]["forecast"])
            finally:
                app._previous.clear()
                app._previous.update(old_previous)
                app._state["signals_total"] = old_total
                app._research_feature_last_written.clear()
                app._research_feature_last_written.update(old_feature_timestamps)
                app._research_feature_schedule_loaded = old_feature_schedule_loaded


    def test_forecast_evaluation_endpoint_is_available_without_resolved_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_snapshots = app.SNAPSHOTS
            old_resolutions = app.RESOLUTIONS
            old_collector = app._resolution_collector
            try:
                app.SNAPSHOTS = root / "market_snapshots.jsonl"
                app.RESOLUTIONS = root / "resolved_markets.jsonl"
                app._resolution_collector = None
                report = app.forecast_evaluation_payload()
                self.assertEqual(report["mode"], "RESOLVED_MARKET_FORECAST_EVALUATION")
                self.assertEqual(report["eligible_samples"], 0)
                self.assertIsNone(report["metrics"]["brier_score"])
            finally:
                app.SNAPSHOTS = old_snapshots
                app.RESOLUTIONS = old_resolutions
                app._resolution_collector = old_collector

    def test_health_requires_worker_and_recent_success(self):
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        fresh = {
            "worker_running": True,
            "last_success_at": (now - timedelta(seconds=30)).isoformat(),
            "last_error": None,
        }
        healthy, payload = app.health_payload(fresh, now)
        self.assertTrue(healthy)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["seconds_since_success"], 30.0)

        stale = {**fresh, "last_success_at": (now - timedelta(seconds=app.POLL_SECONDS * 4)).isoformat()}
        healthy, payload = app.health_payload(stale, now)
        self.assertFalse(healthy)
        self.assertFalse(payload["ok"])

    def test_health_is_unhealthy_before_first_success_or_when_worker_stops(self):
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        healthy, payload = app.health_payload({"worker_running": True, "last_success_at": None}, now)
        self.assertFalse(healthy)
        self.assertIsNone(payload["seconds_since_success"])

        stopped = {
            "worker_running": False,
            "last_success_at": now.isoformat(),
            "last_error": None,
        }
        healthy, payload = app.health_payload(stopped, now)
        self.assertFalse(healthy)


    def test_news_endpoint_returns_status_and_attributed_items(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        old_state = dict(app._news_state)
        old_items = list(app._news_items_cache)
        old_matches = list(app._news_matches_cache)
        try:
            with app._news_lock:
                app._news_state.update({
                    "worker_running": True,
                    "feeds_configured": 1,
                    "feeds_succeeded": 1,
                    "feeds_failed": 0,
                    "last_success_at": app.utc_now(),
                    "last_error": None,
                })
                app._news_items_cache[:] = [{
                    "id": "test-id",
                    "source": "Example News",
                    "title": "A test headline",
                    "url": "https://example.org/story",
                }]
                app._news_matches_cache[:] = [{
                    "news_title": "A test headline",
                    "market_question": "Will this happen?",
                    "research_only": True,
                    "execution": "none",
                }]
            thread.start()
            address = f"http://127.0.0.1:{server.server_port}/news"
            with http_urlopen(address, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["feeds_succeeded"], 1)
            self.assertEqual(payload["items"][0]["source"], "Example News")
            self.assertEqual(payload["items"][0]["url"], "https://example.org/story")
            with http_urlopen(f"http://127.0.0.1:{server.server_port}/matches", timeout=3) as response:
                matches = json.loads(response.read().decode("utf-8"))
            self.assertTrue(matches["research_only"])
            self.assertEqual(matches["execution"], "none")
            self.assertEqual(matches["items"][0]["news_title"], "A test headline")
        finally:
            server.shutdown()
            server.server_close()
            if thread.is_alive():
                thread.join(timeout=2)
            with app._news_lock:
                app._news_state.clear()
                app._news_state.update(old_state)
                app._news_items_cache[:] = old_items
                app._news_matches_cache[:] = old_matches


    def test_research_features_endpoint_returns_latest_snapshot_per_market(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        old_path = app.RESEARCH_FEATURES
        with tempfile.TemporaryDirectory() as directory:
            try:
                app.RESEARCH_FEATURES = Path(directory) / "research_features.jsonl"
                app.RESEARCH_FEATURES.write_text(
                    json.dumps({
                        "market_id": "m1", "observed_at": "2026-10-30T11:00:00+00:00",
                        "gamma_yes_probability": 0.4, "clob_book_usable": True,
                        "clob_book_quality_eligible": True, "news_match_count_24h": 0,
                    }) + "\n" +
                    json.dumps({
                        "market_id": "m1", "observed_at": "2026-10-30T12:00:00+00:00",
                        "gamma_yes_probability": 0.6, "clob_book_usable": False,
                        "clob_book_quality_eligible": False, "news_match_count_24h": 2,
                    }) + "\n",
                    encoding="utf-8",
                )
                thread.start()
                address = f"http://127.0.0.1:{server.server_port}/research-features"
                with http_urlopen(address, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["mode"], "RESEARCH_FEATURES_V1")
                self.assertEqual(payload["markets_with_latest_features"], 1)
                self.assertEqual(payload["items"][0]["gamma_yes_probability"], 0.6)
                self.assertEqual(payload["summary"]["news_matches_24h_total"], 2)
                self.assertIsNone(payload["forecast"])
                self.assertEqual(payload["execution"], "none")
            finally:
                server.shutdown()
                server.server_close()
                if thread.is_alive():
                    thread.join(timeout=2)
                app.RESEARCH_FEATURES = old_path


    def test_independent_model_evaluation_reports_insufficient_data_without_fitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_features = app.RESEARCH_FEATURES
            old_resolutions = app.RESOLUTIONS
            old_collector = app._resolution_collector
            try:
                app.RESEARCH_FEATURES = root / "research_features.jsonl"
                app.RESOLUTIONS = root / "resolved_markets.jsonl"
                app._resolution_collector = None
                report = app.independent_model_evaluation_payload()
                self.assertEqual(report["mode"], "INDEPENDENT_FEATURE_MODEL_EVALUATION_V1")
                self.assertEqual(report["status"], "insufficient_data")
                self.assertEqual(report["eligible_markets"], 0)
                self.assertTrue(report["research_only"])
                self.assertIsNone(report["forecast"])
                self.assertEqual(report["execution"], "none")
            finally:
                app.RESEARCH_FEATURES = old_features
                app.RESOLUTIONS = old_resolutions
                app._resolution_collector = old_collector

if __name__ == "__main__":
    unittest.main()
