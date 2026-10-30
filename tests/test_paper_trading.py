import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from paper_trading import PaperConfig, PaperTrader, rotate_jsonl_file


class PaperTraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "paper_state.json"
        self.config = PaperConfig(
            starting_cash=100.0, max_position_usd=2.0, max_open_exposure_usd=10.0,
            max_open_positions=5, signal_delta=0.03, take_profit_pct=0.10,
            stop_loss_pct=0.10, max_hold_hours=24, simulated_slippage=0.005,
            fee_rate=0.0, daily_loss_limit_usd=5.0,
        )
        self.bot = PaperTrader(self.state_path, self.config)
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def row(price, token="token-1"):
        return {"token_id": token, "market_id": "market-1", "question": "Test?",
                "outcome": "Yes", "observed_price": price}

    def test_starting_balance_and_no_signal_on_first_observation(self):
        result = self.bot.process_snapshot([self.row(0.5)], self.now)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(self.bot.snapshot()["metrics"]["cash"], 100.0)
        self.assertEqual(self.bot.snapshot()["metrics"]["open_positions"], 0)

    def test_positive_delta_opens_at_most_two_dollar_position(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        result = self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        state = self.bot.snapshot()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["opened"]), 1)
        self.assertLessEqual(state["metrics"]["open_exposure"], 2.0)
        self.assertAlmostEqual(state["metrics"]["cash"], 98.0, places=4)

    def test_rejects_new_entries_when_position_limit_is_reached(self):
        cfg = PaperConfig(max_open_positions=1, **{k: v for k, v in self.config.__dict__.items() if k != "max_open_positions"})
        bot = PaperTrader(Path(self.temp.name) / "other_state.json", cfg)
        bot.process_snapshot([self.row(0.50, "a"), self.row(0.50, "b")], self.now)
        result = bot.process_snapshot([self.row(0.54, "a"), self.row(0.54, "b")], self.now + timedelta(minutes=5))
        self.assertEqual(len(result["opened"]), 1)
        skipped = [c for c in result["candidates"] if c.get("paper_action") == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["skip_reason"], "max_open_positions_reached")

    def test_take_profit_closes_position_and_records_realized_pnl(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        result = self.bot.process_snapshot([self.row(0.62)], self.now + timedelta(minutes=10))
        state = self.bot.snapshot()
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(result["closed"][0]["reason"], "simulated_take_profit")
        self.assertEqual(state["metrics"]["open_positions"], 0)
        self.assertGreater(state["metrics"]["realized_pnl"], 0)

    def test_stop_loss_closes_position(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        result = self.bot.process_snapshot([self.row(0.47)], self.now + timedelta(minutes=10))
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(result["closed"][0]["reason"], "simulated_stop_loss")
        self.assertLess(result["closed"][0]["pnl"], 0)

    def test_state_survives_restart(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        restarted = PaperTrader(self.state_path, self.config)
        state = restarted.snapshot()
        self.assertEqual(state["metrics"]["open_positions"], 1)
        self.assertLess(state["metrics"]["cash"], 100.0)

    def test_daily_loss_guard_stops_entries(self):
        self.bot._state["daily_date_utc"] = self.now.date().isoformat()
        self.bot._state["daily_realized_pnl"] = -5.0
        self.bot.process_snapshot([self.row(0.50)], self.now)
        result = self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["candidates"][0]["skip_reason"], "daily_loss_limit_reached")

    def test_daily_loss_guard_includes_open_unrealized_losses(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_open_exposure_usd": 10.0,
            "daily_loss_limit_usd": 0.5,
        })
        bot = PaperTrader(Path(self.temp.name) / "unrealized_state.json", cfg)
        tokens = [f"token-{index}" for index in range(6)]
        bot.process_snapshot([self.row(0.50, token) for token in tokens], self.now)
        bot.process_snapshot(
            [self.row(0.54, token) for token in tokens[:5]] + [self.row(0.50, tokens[5])],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(bot.snapshot()["metrics"]["open_positions"], 5)
        result = bot.process_snapshot(
            [self.row(0.50, token) for token in tokens[:5]] + [self.row(0.54, tokens[5])],
            self.now + timedelta(minutes=10),
        )
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["candidates"][0]["skip_reason"], "daily_loss_limit_reached")

    def test_daily_unrealized_baseline_resets_at_utc_day_rollover(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_open_exposure_usd": 10.0,
            "daily_loss_limit_usd": 0.5,
        })
        bot = PaperTrader(Path(self.temp.name) / "daily_rollover_state.json", cfg)
        tokens = [f"rollover-{index}" for index in range(5)]
        bot.process_snapshot([self.row(0.50, token) for token in tokens], self.now)
        bot.process_snapshot(
            [self.row(0.54, token) for token in tokens[:4]] + [self.row(0.50, tokens[4])],
            self.now + timedelta(minutes=5),
        )
        bot.process_snapshot(
            [self.row(0.50, token) for token in tokens],
            self.now + timedelta(minutes=10),
        )
        next_day = self.now + timedelta(days=1)
        result = bot.process_snapshot(
            [self.row(0.50, token) for token in tokens[:4]] + [self.row(0.54, tokens[4])],
            next_day,
        )
        self.assertEqual(len(result["opened"]), 1)
        self.assertNotEqual(result["candidates"][0].get("skip_reason"), "daily_loss_limit_reached")

    def test_fee_assumption_is_applied_on_entry_and_exit(self):
        cfg = PaperConfig(**{**self.config.__dict__, "fee_rate": 0.01})
        bot = PaperTrader(Path(self.temp.name) / "fee_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        opened = bot.snapshot()
        self.assertGreater(opened["metrics"]["total_fees"], 0)
        bot.process_snapshot([self.row(0.62)], self.now + timedelta(minutes=10))
        closed = bot.snapshot()
        self.assertEqual(closed["metrics"]["open_positions"], 0)
        self.assertGreater(closed["metrics"]["total_fees"], opened["metrics"]["total_fees"])

    def test_bad_or_nonfinite_prices_are_ignored(self):
        result = self.bot.process_snapshot([self.row(float("nan")), self.row(1.0, "invalid")], self.now)
        self.assertEqual(result, {"candidates": [], "opened": [], "closed": []})
        self.assertEqual(self.bot.snapshot()["metrics"]["open_positions"], 0)

    def test_in_memory_replay_can_skip_disk_writes(self):
        self.bot.process_snapshot([self.row(0.50)], self.now, persist_state=False)
        result = self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5), persist_state=False)
        self.assertEqual(len(result["opened"]), 1)
        self.assertFalse(self.state_path.exists())
        self.assertFalse((Path(self.temp.name) / "paper_events.jsonl").exists())
        self.assertEqual(self.bot.snapshot()["metrics"]["open_positions"], 1)

    def test_state_file_is_persisted_and_events_are_logged(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        self.assertTrue(self.state_path.exists())
        state_file = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state_file["mode"], "PAPER_ONLY")
        event_file = Path(self.temp.name) / "paper_events.jsonl"
        self.assertTrue(event_file.exists())
        events = [json.loads(line) for line in event_file.read_text().splitlines()]
        self.assertTrue(any(e["event_type"] == "position_opened" for e in events))


    def test_time_limit_closes_a_stale_position(self):
        cfg = PaperConfig(**{**self.config.__dict__, "max_hold_hours": 1.0})
        bot = PaperTrader(Path(self.temp.name) / "time_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        result = bot.process_snapshot([self.row(0.54)], self.now + timedelta(hours=1, minutes=5))
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(result["closed"][0]["reason"], "simulated_time_limit")

    def test_prevents_opposing_outcomes_from_opening_in_same_market(self):
        bot = PaperTrader(Path(self.temp.name) / "same_market_state.json", self.config)
        yes = {**self.row(0.50, "yes-token"), "outcome": "Yes"}
        no = {**self.row(0.50, "no-token"), "outcome": "No"}
        bot.process_snapshot([yes, no], self.now)
        opened = bot.process_snapshot(
            [{**yes, "observed_price": 0.54}, no],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(len(opened["opened"]), 1)
        blocked = bot.process_snapshot(
            [{**yes, "observed_price": 0.54}, {**no, "observed_price": 0.54}],
            self.now + timedelta(minutes=10),
        )
        self.assertEqual(blocked["opened"], [])
        self.assertEqual(blocked["candidates"][0]["skip_reason"], "opposing_outcome_already_open")

    def test_orderbook_gate_blocks_entries_without_a_book(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True})
        bot = PaperTrader(Path(self.temp.name) / "quality_missing_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        result = bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["candidates"][0]["skip_reason"], "missing_orderbook")

    def test_orderbook_gate_accepts_fresh_liquid_book(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True})
        bot = PaperTrader(Path(self.temp.name) / "quality_valid_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        good_book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=5)).isoformat(),
            "best_bid": 0.52,
            "best_ask": 0.54,
            "spread_bps": 377.36,
            "bid_depth_usd_top5": 20.0,
            "ask_depth_usd_top5": 18.0,
            "bids_top5": [{"price": 0.52, "size": 100.0}],
            "asks_top5": [{"price": 0.54, "size": 100.0}],
            "book_usable": True,
            "crossed_book": False,
        }
        result = bot.process_snapshot(
            [{**self.row(0.54), "orderbook": good_book}],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(len(result["opened"]), 1)
        self.assertEqual(result["opened"][0]["entry_market_quality"]["reason"], "approved_market_quality")

    def test_uses_ask_for_entry_and_bid_for_mark_and_exit(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True})
        bot = PaperTrader(Path(self.temp.name) / "bid_ask_fills_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        entry_book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=5)).isoformat(),
            "best_bid": 0.53,
            "best_ask": 0.55,
            "spread_bps": 370.37,
            "bid_depth_usd_top5": 20.0,
            "ask_depth_usd_top5": 18.0,
            "bids_top5": [{"price": 0.53, "size": 100.0}],
            "asks_top5": [{"price": 0.55, "size": 100.0}],
            "book_usable": True,
            "crossed_book": False,
        }
        opened_result = bot.process_snapshot(
            [{**self.row(0.54), "orderbook": entry_book}],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(len(opened_result["opened"]), 1)
        opened = opened_result["opened"][0]
        self.assertEqual(opened["entry_price_source"], "clob_top5_depth_vwap")
        self.assertAlmostEqual(opened["entry_reference_price"], 0.55)
        self.assertAlmostEqual(opened["entry_price"], 0.55 * 1.005)
        self.assertAlmostEqual(opened["mark_price"], 0.53)
        self.assertEqual(opened["mark_source"], "clob_top5_depth_vwap")
        self.assertLess(opened["unrealized_pnl"], 0)

        exit_book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=10)).isoformat(),
            "best_bid": 0.61,
            "best_ask": 0.62,
            "spread_bps": 162.6,
            "bid_depth_usd_top5": 25.0,
            "ask_depth_usd_top5": 21.0,
            "bids_top5": [{"price": 0.61, "size": 100.0}],
            "asks_top5": [{"price": 0.62, "size": 100.0}],
            "book_usable": True,
            "crossed_book": False,
        }
        closed_result = bot.process_snapshot(
            [{**self.row(0.65), "orderbook": exit_book}],
            self.now + timedelta(minutes=10),
        )
        self.assertEqual(len(closed_result["closed"]), 1)
        closed = closed_result["closed"][0]
        self.assertEqual(closed["reason"], "simulated_take_profit")
        self.assertEqual(closed["exit_price_source"], "clob_top5_depth_vwap")
        self.assertAlmostEqual(closed["exit_observed_price"], 0.61)
        self.assertTrue(closed["exit_fill_details"]["eligible"])
        self.assertEqual(closed["exit_fill_details"]["levels_used"], 1)

    def test_uses_gamma_market_fee_schedule_for_entry_and_exit(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True, "fee_rate": 0.0})
        bot = PaperTrader(Path(self.temp.name) / "market_fee_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        market_fee = {
            **self.row(0.54),
            "fees_enabled": True,
            "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
            "orderbook": {
                "token_id": "token-1",
                "observed_at": (self.now + timedelta(minutes=5)).isoformat(),
                "best_bid": 0.52,
                "best_ask": 0.54,
                "spread_bps": 377.36,
                "bid_depth_usd_top5": 20.0,
                "ask_depth_usd_top5": 18.0,
                "bids_top5": [{"price": 0.52, "size": 100.0}],
                "asks_top5": [{"price": 0.54, "size": 100.0}],
                "book_usable": True,
                "crossed_book": False,
            },
        }
        opened = bot.process_snapshot(
            [market_fee], self.now + timedelta(minutes=5)
        )["opened"][0]
        expected_entry_fee = round(
            opened["shares"] * 0.07 * opened["entry_price"] * (1.0 - opened["entry_price"]),
            5,
        )
        self.assertEqual(opened["fee_model"]["mode"], "polymarket_taker_curve")
        self.assertAlmostEqual(opened["entry_fee"], expected_entry_fee, places=8)
        self.assertEqual(opened["entry_fee_details"]["source"], "gamma_fee_schedule")

        exit_book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=10)).isoformat(),
            "best_bid": 0.61,
            "best_ask": 0.62,
            "spread_bps": 162.6,
            "bid_depth_usd_top5": 25.0,
            "ask_depth_usd_top5": 21.0,
            "bids_top5": [{"price": 0.61, "size": 100.0}],
            "asks_top5": [{"price": 0.62, "size": 100.0}],
            "book_usable": True,
            "crossed_book": False,
        }
        closed = bot.process_snapshot(
            [{**self.row(0.65), "orderbook": exit_book}],
            self.now + timedelta(minutes=10),
        )["closed"][0]
        expected_exit_price = 0.61 * (1.0 - cfg.simulated_slippage)
        expected_exit_fee = round(
            closed["shares"] * 0.07 * expected_exit_price * (1.0 - expected_exit_price),
            5,
        )
        self.assertAlmostEqual(closed["exit_fee"], expected_exit_fee, places=8)
        self.assertEqual(closed["exit_fee_details"]["mode"], "polymarket_taker_curve")

    def test_fee_enabled_market_without_schedule_is_blocked_for_paper_entry(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": False})
        bot = PaperTrader(Path(self.temp.name) / "missing_fee_schedule_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        result = bot.process_snapshot(
            [{**self.row(0.54), "fees_enabled": True}],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(result["opened"], [])
        self.assertEqual(
            result["candidates"][0]["skip_reason"],
            "fees_enabled_but_fee_schedule_missing",
        )

    def test_explicitly_fee_free_market_overrides_legacy_fallback(self):
        cfg = PaperConfig(**{**self.config.__dict__, "fee_rate": 0.02})
        bot = PaperTrader(Path(self.temp.name) / "fee_free_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        opened = bot.process_snapshot(
            [{**self.row(0.54), "fees_enabled": False}],
            self.now + timedelta(minutes=5),
        )["opened"][0]
        self.assertEqual(opened["fee_model"]["mode"], "fee_free")
        self.assertEqual(opened["entry_fee"], 0.0)
        self.assertEqual(opened["entry_fee_details"]["fee"], 0.0)

    def test_orderbook_gate_rejects_stale_or_wide_book(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True})
        bot = PaperTrader(Path(self.temp.name) / "quality_stale_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        old_book = {
            "token_id": "token-1",
            "observed_at": (self.now - timedelta(hours=1)).isoformat(),
            "best_bid": 0.52,
            "best_ask": 0.53,
            "spread_bps": 190.48,
            "bid_depth_usd_top5": 20.0,
            "ask_depth_usd_top5": 18.0,
            "book_usable": True,
            "crossed_book": False,
        }
        result = bot.process_snapshot(
            [{**self.row(0.54), "orderbook": old_book}],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["candidates"][0]["skip_reason"], "stale_orderbook")

    def test_corrupt_state_refuses_to_reset_account_silently(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            PaperTrader(self.state_path, self.config)

    def test_jsonl_rotation_retains_configured_backups(self):
        target = Path(self.temp.name) / "events.jsonl"
        target.write_text("1234567890", encoding="utf-8")
        rotate_jsonl_file(target, max_bytes=10, backups=2)
        self.assertFalse(target.exists())
        self.assertEqual(target.with_name("events.jsonl.1").read_text(), "1234567890")
        target.write_text("abcdefghij", encoding="utf-8")
        rotate_jsonl_file(target, max_bytes=10, backups=2)
        self.assertEqual(target.with_name("events.jsonl.2").read_text(), "1234567890")
        self.assertEqual(target.with_name("events.jsonl.1").read_text(), "abcdefghij")


if __name__ == "__main__":
    unittest.main()
