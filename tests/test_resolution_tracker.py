import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import resolution_tracker


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


def resolved_market(market_id):
    return {
        "id": market_id,
        "conditionId": "condition-" + market_id,
        "question": "Will this happen?",
        "closed": True,
        "closedTime": "2026-10-30T12:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": '["yes-token", "no-token"]',
    }


class ResolutionTrackerTests(unittest.TestCase):
    def test_fetch_uses_bounded_closed_market_pages(self):
        first = [resolved_market("1"), resolved_market("2")]
        with patch.object(resolution_tracker, "urlopen", return_value=FakeResponse(first)) as mocked:
            with patch.object(resolution_tracker.time, "sleep"):
                result = resolution_tracker.fetch_recent_resolved_markets(limit=20, pages=5)
        self.assertEqual(len(result), 2)
        query = parse_qs(urlparse(mocked.call_args.args[0].full_url).query)
        self.assertEqual(query["closed"], ["true"])
        self.assertEqual(query["limit"], ["20"])
        self.assertEqual(query["order"], ["closedTime"])
        self.assertEqual(query["ascending"], ["false"])

    def test_paginates_and_deduplicates(self):
        pages = [
            [resolved_market("1"), resolved_market("2")],
            [resolved_market("2"), resolved_market("3")],
        ]
        with patch.object(resolution_tracker, "urlopen", side_effect=[FakeResponse(x) for x in pages]) as mocked:
            with patch.object(resolution_tracker.time, "sleep"):
                result = resolution_tracker.fetch_recent_resolved_markets(limit=2, pages=2)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(result), 3)

    def test_load_resolutions_reads_rotated_logs_for_open_markets_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolved.jsonl"
            path.with_name("resolved.jsonl.1").write_text(json.dumps({
                "market_id": "old-market", "resolved_yes": 0,
                "resolved_at": "2026-10-29T10:00:00+00:00",
            }) + "\n", encoding="utf-8")
            path.write_text(
                json.dumps({
                    "market_id": "open-market", "resolved_yes": 1,
                    "resolved_at": "2026-10-30T10:00:00+00:00",
                }) + "\n" +
                json.dumps({
                    "market_id": "unrelated", "resolved_yes": 1,
                    "resolved_at": "2026-10-30T11:00:00+00:00",
                }) + "\n",
                encoding="utf-8",
            )
            collector = resolution_tracker.ResolutionCollector(path)
            rows = collector.load_resolutions({"old-market", "open-market"})
            self.assertEqual({row["market_id"] for row in rows}, {"old-market", "open-market"})

    def test_fetch_once_returns_current_decisive_resolutions(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = resolution_tracker.ResolutionCollector(Path(tmp) / "resolved.jsonl")
            market = {
                "market_id": "market-1",
                "resolved_yes": 1,
                "resolved_at": "2026-10-30T12:00:00+00:00",
            }
            with patch.object(resolution_tracker, "fetch_recent_resolved_markets", return_value=[market]):
                result = collector.fetch_once()
            self.assertEqual(result["resolved_markets"], [market])
            self.assertEqual(result["resolutions_added"], 1)

    def test_collector_persists_once_and_deduplicates_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolved.jsonl"
            collector = resolution_tracker.ResolutionCollector(path)
            with patch.object(
                resolution_tracker, "fetch_recent_resolved_markets",
                return_value=[{
                    "market_id": "condition-1",
                    "resolved_yes": 1,
                    "resolved_at": "2026-10-30T12:00:00+00:00",
                }],
            ):
                first = collector.fetch_once()
                second = collector.fetch_once()
            self.assertEqual(first["resolutions_added"], 1)
            self.assertEqual(second["resolutions_added"], 0)
            restarted = resolution_tracker.ResolutionCollector(path)
            with patch.object(
                resolution_tracker, "fetch_recent_resolved_markets",
                return_value=[{
                    "market_id": "condition-1",
                    "resolved_yes": 1,
                    "resolved_at": "2026-10-30T12:00:00+00:00",
                }],
            ):
                third = restarted.fetch_once()
            self.assertEqual(third["resolutions_added"], 0)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
