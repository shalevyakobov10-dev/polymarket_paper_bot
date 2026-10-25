import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import app
from paper_simulator import PaperSimulator


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
            try:
                app._previous.clear()
                app._state["signals_total"] = 0
                with patch.object(app, "DATA_DIR", root), \
                     patch.object(app, "SNAPSHOTS", root / "snapshots.jsonl"), \
                     patch.object(app, "SIGNALS", root / "signals.jsonl"), \
                     patch.object(app, "fetch_markets", side_effect=[[markets[0]], [markets[1]]]):
                    first = app.poll_once()
                    second = app.poll_once()
                self.assertEqual(first["candidates"], 0)
                self.assertEqual(second["candidates"], 1)
                record = json.loads((root / "signals.jsonl").read_text().strip())
                self.assertEqual(record["signal"], "momentum_up")
                self.assertEqual(record["execution"], "none")
                self.assertEqual(len((root / "snapshots.jsonl").read_text().splitlines()), 2)
            finally:
                app._previous.clear()
                app._previous.update(old_previous)
                app._state["signals_total"] = old_total


    def test_paper_simulator_respects_position_cap_and_never_executes(self):
        sim = PaperSimulator(initial_cash=100, max_position_usd=2, fee_rate=0, slippage_rate=0, exit_delta=0.02)
        sim.on_observation({"token_id": "t1", "observed_price": 0.50, "timestamp": "t0"})
        entry = sim.on_observation({"token_id": "t1", "observed_price": 0.54, "timestamp": "t1"})
        self.assertEqual(entry["type"], "entry")
        self.assertAlmostEqual(entry["stake_usd"], 2.0)
        self.assertEqual(entry["execution"], "simulated_only")
        self.assertAlmostEqual(sim.cash, 98.0)
        sim.on_observation({"token_id": "t1", "observed_price": 0.53, "timestamp": "t2"})
        exit_event = sim.on_observation({"token_id": "t1", "observed_price": 0.51, "timestamp": "t3"})
        self.assertEqual(exit_event["type"], "exit")
        self.assertEqual(exit_event["execution"], "simulated_only")
        self.assertEqual(len(sim.positions), 0)
        summary = sim.summary()
        self.assertEqual(summary["mode"], "paper_only")
        self.assertEqual(summary["closed_trades"], 1)
        self.assertAlmostEqual(summary["equity_usd"], 99.88888888888889)


if __name__ == "__main__":
    unittest.main()
