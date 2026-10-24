import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ParserTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
