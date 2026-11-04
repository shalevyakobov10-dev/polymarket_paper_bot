import json
import tempfile
import unittest
from unittest.mock import patch
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

    def test_portfolio_drawdown_limit_blocks_new_entries_and_persists_peak(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_portfolio_drawdown_pct": 4.0,
        })
        bot = PaperTrader(Path(self.temp.name) / "drawdown_state.json", cfg)
        bot._state["cash"] = 95.0
        bot._state["realized_pnl"] = -5.0
        bot.process_snapshot([self.row(0.50)], self.now)
        result = bot.process_snapshot(
            [self.row(0.54)], self.now + timedelta(minutes=5)
        )
        state = bot.snapshot()
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["candidates"][0]["skip_reason"], "portfolio_drawdown_circuit_breaker_latched")
        self.assertAlmostEqual(state["metrics"]["current_drawdown_pct"], 5.0)
        self.assertAlmostEqual(state["metrics"]["max_drawdown_pct"], 5.0)
        self.assertTrue(state["metrics"]["drawdown_limit_reached"])
        persisted = json.loads((Path(self.temp.name) / "drawdown_state.json").read_text())
        self.assertAlmostEqual(persisted["equity_peak"], 100.0)
        self.assertAlmostEqual(persisted["max_drawdown_pct"], 5.0)

    def test_drawdown_circuit_breaker_latches_after_recovery_and_restart(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_portfolio_drawdown_pct": 4.0,
        })
        state_path = Path(self.temp.name) / "latched_breaker_state.json"
        bot = PaperTrader(state_path, cfg)
        bot._state["cash"] = 95.0
        bot._state["realized_pnl"] = -5.0
        bot.process_snapshot([self.row(0.50)], self.now)

        tripped = bot.snapshot()["metrics"]
        self.assertTrue(tripped["circuit_breaker_tripped"])
        self.assertTrue(tripped["new_entries_halted"])
        self.assertAlmostEqual(tripped["circuit_breaker_drawdown_pct"], 5.0)

        # Simulate equity recovery. The all-time drawdown latch must remain set.
        bot._state["cash"] = 100.0
        bot._state["realized_pnl"] = 0.0
        bot._state["last_prices"]["token-1"] = 0.50
        bot._save()
        recovered = bot.snapshot()["metrics"]
        self.assertAlmostEqual(recovered["current_drawdown_pct"], 0.0)
        self.assertTrue(recovered["circuit_breaker_tripped"])

        restarted = PaperTrader(state_path, cfg)
        result = restarted.process_snapshot(
            [self.row(0.54)], self.now + timedelta(minutes=5)
        )
        self.assertEqual(result["opened"], [])
        self.assertEqual(
            result["candidates"][0]["skip_reason"],
            "portfolio_drawdown_circuit_breaker_latched",
        )
        self.assertTrue(restarted.snapshot()["metrics"]["new_entries_halted"])

        events = [
            json.loads(line)
            for line in (Path(self.temp.name) / "paper_events.jsonl").read_text().splitlines()
        ]
        self.assertTrue(any(
            event.get("event_type") == "portfolio_drawdown_circuit_breaker_tripped"
            for event in events
        ))

    def test_partial_circuit_breaker_state_is_rejected(self):
        state = self.bot._new_state()
        state.pop("circuit_breaker_at")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete circuit-breaker fields"):
            PaperTrader(self.state_path, self.config)

    def test_legacy_state_migrates_to_latched_breaker_if_old_max_drawdown_crossed_limit(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_portfolio_drawdown_pct": 4.0,
        })
        legacy_path = Path(self.temp.name) / "legacy_latched_state.json"
        bot = PaperTrader(legacy_path, self.config)
        legacy = bot._new_state()
        legacy["max_drawdown_pct"] = 5.0
        legacy["max_drawdown_usd"] = 5.0
        for field in (
            "circuit_breaker_tripped",
            "circuit_breaker_reason",
            "circuit_breaker_at",
            "circuit_breaker_drawdown_pct",
        ):
            legacy.pop(field)
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

        migrated = PaperTrader(legacy_path, cfg).snapshot()["metrics"]
        self.assertTrue(migrated["circuit_breaker_tripped"])
        self.assertEqual(
            migrated["circuit_breaker_reason"],
            "max_portfolio_drawdown_reached",
        )
        self.assertAlmostEqual(migrated["circuit_breaker_drawdown_pct"], 5.0)

    def test_paper_reset_clears_latched_breaker_and_full_simulated_portfolio(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "max_portfolio_drawdown_pct": 4.0,
        })
        bot = PaperTrader(Path(self.temp.name) / "reset_breaker_state.json", cfg)
        bot._state["cash"] = 95.0
        bot._state["realized_pnl"] = -5.0
        bot.process_snapshot([self.row(0.50)], self.now)
        self.assertTrue(bot.snapshot()["metrics"]["circuit_breaker_tripped"])

        reset = bot.reset()
        self.assertFalse(reset["metrics"]["circuit_breaker_tripped"])
        self.assertFalse(reset["metrics"]["new_entries_halted"])
        self.assertEqual(reset["metrics"]["open_positions"], 0)

    def test_legacy_paper_state_receives_safe_drawdown_defaults(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state.pop("equity_peak", None)
        state.pop("max_drawdown_usd", None)
        state.pop("max_drawdown_pct", None)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        restarted = PaperTrader(self.state_path, self.config)
        self.assertAlmostEqual(restarted.snapshot()["metrics"]["equity_peak"], 100.0)
        self.assertEqual(restarted.snapshot()["metrics"]["max_drawdown_pct"], 0.0)

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


    def test_resolution_settlement_pays_winning_yes_position(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        opened = self.bot.process_snapshot(
            [self.row(0.54)], self.now + timedelta(minutes=5)
        )["opened"][0]
        before_cash = self.bot.snapshot()["metrics"]["cash"]
        resolution = {
            "market_id": "market-1",
            "resolved_yes": 1,
            "resolved_at": (self.now + timedelta(hours=2)).isoformat(),
            "resolution_source": "official_resolution_source",
        }
        result = self.bot.settle_resolved_positions(
            [resolution], now=self.now + timedelta(hours=3)
        )
        state = self.bot.snapshot()
        self.assertEqual(result["settled_count"], 1)
        self.assertTrue(result["settled"][0]["won"])
        self.assertEqual(result["settled"][0]["event_type"], "position_settled")
        self.assertAlmostEqual(result["settled"][0]["settlement_payout_usd"], opened["shares"])
        self.assertAlmostEqual(state["metrics"]["cash"], before_cash + opened["shares"], places=4)
        self.assertEqual(state["metrics"]["open_positions"], 0)
        self.assertEqual(state["metrics"]["closed_trades"], 1)
        self.assertAlmostEqual(state["metrics"]["realized_pnl"], opened["shares"] - opened["gross_cost"])

    def test_resolution_settlement_handles_losing_no_and_is_idempotent(self):
        no_row = {**self.row(0.50), "outcome": "No"}
        self.bot.process_snapshot([no_row], self.now)
        opened = self.bot.process_snapshot(
            [{**no_row, "observed_price": 0.54}], self.now + timedelta(minutes=5)
        )["opened"][0]
        before_cash = self.bot.snapshot()["metrics"]["cash"]
        resolution = {
            "market_id": "market-1",
            "resolved_yes": 1,
            "resolved_at": (self.now + timedelta(hours=2)).isoformat(),
        }
        first = self.bot.settle_resolved_positions([resolution], now=self.now + timedelta(hours=3))
        second = self.bot.settle_resolved_positions([resolution], now=self.now + timedelta(hours=4))
        state = self.bot.snapshot()
        self.assertEqual(first["settled_count"], 1)
        self.assertFalse(first["settled"][0]["won"])
        self.assertEqual(first["settled"][0]["settlement_payout_usd"], 0.0)
        self.assertAlmostEqual(state["metrics"]["cash"], before_cash)
        self.assertEqual(second["settled_count"], 0)
        self.assertEqual(state["metrics"]["closed_trades"], 1)
        self.assertEqual(state["metrics"]["open_positions"], 0)

    def test_resolution_settlement_respects_share_deducted_entry_fee(self):
        cfg = PaperConfig(**{**self.config.__dict__, "require_orderbook": True})
        bot = PaperTrader(Path(self.temp.name) / "settlement_share_fee.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=5)).isoformat(),
            "best_bid": 0.52, "best_ask": 0.54, "spread_bps": 377.36,
            "bid_depth_usd_top5": 20.0, "ask_depth_usd_top5": 18.0,
            "bids_top5": [{"price": 0.52, "size": 100.0}],
            "asks_top5": [{"price": 0.54, "size": 100.0}],
            "book_usable": True, "crossed_book": False,
        }
        opened = bot.process_snapshot([{
            **self.row(0.54), "fees_enabled": True,
            "fee_schedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
            "orderbook": book,
        }], self.now + timedelta(minutes=5))["opened"][0]
        fees_before = bot.snapshot()["metrics"]["total_fees"]
        result = bot.settle_resolved_positions([{
            "market_id": "market-1", "resolved_yes": 1,
            "resolved_at": (self.now + timedelta(hours=1)).isoformat(),
        }], now=self.now + timedelta(hours=2))
        event = result["settled"][0]
        self.assertEqual(opened["entry_fee_collection"], "shares")
        self.assertEqual(event["settlement_fee"], 0.0)
        self.assertAlmostEqual(event["settlement_payout_usd"], opened["shares"])
        self.assertAlmostEqual(event["pnl"], opened["shares"] - opened["gross_cost"], places=8)
        self.assertEqual(bot.snapshot()["metrics"]["total_fees"], fees_before)

    def test_resolution_settlement_rejects_conflicting_future_and_pre_entry_records(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        valid_time = (self.now + timedelta(hours=2)).isoformat()
        result = self.bot.settle_resolved_positions([
            {"market_id": "market-1", "resolved_yes": 1, "resolved_at": valid_time},
            {"market_id": "market-1", "resolved_yes": 0, "resolved_at": valid_time},
            {"market_id": "market-1", "resolved_yes": 1,
             "resolved_at": (self.now + timedelta(days=2)).isoformat()},
        ], now=self.now + timedelta(hours=3))
        self.assertEqual(result["settled_count"], 0)
        self.assertEqual(result["skipped"]["conflicting_resolution"], 1)
        self.assertEqual(self.bot.snapshot()["metrics"]["open_positions"], 1)


    def test_resolution_timestamp_equal_to_entry_is_not_settled(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        result = self.bot.settle_resolved_positions([{
            "market_id": "market-1",
            "resolved_yes": 1,
            "resolved_at": (self.now + timedelta(minutes=5)).isoformat(),
        }], now=self.now + timedelta(hours=1))
        self.assertEqual(result["settled_count"], 0)
        self.assertEqual(result["skipped"]["invalid_resolution"], 1)
        self.assertEqual(self.bot.snapshot()["metrics"]["open_positions"], 1)

    def test_resolution_settlement_persists_state_and_audit_event(self):
        self.bot.process_snapshot([self.row(0.50)], self.now)
        self.bot.process_snapshot([self.row(0.54)], self.now + timedelta(minutes=5))
        result = self.bot.settle_resolved_positions([{
            "market_id": "market-1", "resolved_yes": 1,
            "resolved_at": (self.now + timedelta(hours=2)).isoformat(),
            "resolution_source": "official",
        }], now=self.now + timedelta(hours=3))
        self.assertEqual(result["settled_count"], 1)
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["positions"], {})
        event_path = Path(self.temp.name) / "paper_events.jsonl"
        events = [json.loads(line) for line in event_path.read_text().splitlines()]
        self.assertTrue(any(event["event_type"] == "position_settled" for event in events))

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

    def test_entry_fill_is_rejected_when_depth_requires_price_above_fok_cap(self):
        cfg = PaperConfig(**{
            **self.config.__dict__,
            "require_orderbook": True,
            "max_entry_price_impact_bps": 100.0,
        })
        bot = PaperTrader(Path(self.temp.name) / "fok_cap_state.json", cfg)
        bot.process_snapshot([self.row(0.50)], self.now)
        book = {
            "token_id": "token-1",
            "observed_at": (self.now + timedelta(minutes=5)).isoformat(),
            "best_bid": 0.52,
            "best_ask": 0.54,
            "spread_bps": 377.36,
            "bid_depth_usd_top5": 20.0,
            "ask_depth_usd_top5": 18.0,
            "bids_top5": [{"price": 0.52, "size": 100.0}],
            "asks_top5": [
                {"price": 0.54, "size": 2.0},
                {"price": 0.56, "size": 100.0},
            ],
            "book_usable": True,
            "crossed_book": False,
        }
        result = bot.process_snapshot(
            [{**self.row(0.54), "orderbook": book}],
            self.now + timedelta(minutes=5),
        )
        self.assertEqual(result["opened"], [])
        self.assertEqual(
            result["candidates"][0]["skip_reason"],
            "insufficient_visible_ask_depth",
        )
        self.assertEqual(bot.snapshot()["metrics"]["open_positions"], 0)

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
        self.assertAlmostEqual(opened["entry_limit_price"], 0.55 * 1.02)
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
            opened["gross_shares"] * 0.07 * opened["entry_price"] * (1.0 - opened["entry_price"]),
            5,
        )
        self.assertEqual(opened["fee_model"]["mode"], "polymarket_taker_curve")
        self.assertAlmostEqual(opened["entry_fee"], expected_entry_fee, places=8)
        self.assertEqual(opened["entry_fee_details"]["source"], "gamma_fee_schedule")
        self.assertEqual(opened["entry_fee_collection"], "shares")
        self.assertAlmostEqual(
            opened["entry_fee_shares"],
            opened["entry_fee"] / opened["entry_price"],
            places=12,
        )
        self.assertLess(opened["shares"], opened["gross_shares"])
        self.assertAlmostEqual(bot.snapshot()["metrics"]["cash"], 98.0, places=6)

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


    def test_state_loader_rejects_nonstandard_nonfinite_numbers(self):
        self.state_path.write_text(
            '{"schema_version":1,"cash":NaN,"starting_cash":100,"positions":{},'
            '"last_prices":{},"realized_pnl":0}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Non-standard JSON numeric constant"):
            PaperTrader(self.state_path, self.config)

    def test_state_loader_rejects_negative_cash_and_invalid_last_price(self):
        state = self.bot._new_state()
        state["cash"] = -1.0
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cash"):
            PaperTrader(self.state_path, self.config)

        state["cash"] = 100.0
        state["last_prices"] = {"token-1": 1.0}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "last price"):
            PaperTrader(self.state_path, self.config)

    def test_state_loader_rejects_malformed_open_position(self):
        state = self.bot._new_state()
        state["positions"] = {
            "token-1": {
                "shares": 2.0,
                "gross_cost": 2.0,
                "entry_fee": 0.0,
                "entry_price": 0.5,
                "mark_price": "NaN",
                "opened_at": self.now.isoformat(),
            }
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mark_price"):
            PaperTrader(self.state_path, self.config)


    def test_state_loader_validates_new_share_fee_accounting_fields(self):
        state = self.bot._new_state()
        state["positions"] = {
            "token-1": {
                "shares": 1.9,
                "gross_shares": 2.0,
                "entry_fee_shares": 0.1,
                "entry_fee_collection": "shares",
                "gross_cost": 1.0,
                "entry_fee": 0.05,
                "entry_price": 0.5,
                "mark_price": 0.5,
                "opened_at": self.now.isoformat(),
            }
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        loaded = PaperTrader(self.state_path, self.config)
        self.assertEqual(loaded.snapshot()["positions_list"][0]["entry_fee_collection"], "shares")

        state["positions"]["token-1"]["shares"] = 1.95
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inconsistent share-fee accounting"):
            PaperTrader(self.state_path, self.config)

    def test_legacy_position_without_fee_collection_remains_cash_compatible(self):
        state = self.bot._new_state()
        state["positions"] = {
            "legacy-token": {
                "shares": 2.0,
                "gross_cost": 1.0,
                "entry_fee": 0.02,
                "entry_price": 0.5,
                "mark_price": 0.5,
                "opened_at": self.now.isoformat(),
            }
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        loaded = PaperTrader(self.state_path, self.config)
        self.assertEqual(loaded.snapshot()["positions_list"][0]["shares"], 2.0)

    def test_failed_exit_valuation_does_not_remove_open_position(self):
        bot = PaperTrader(Path(self.temp.name) / "atomic_exit_state.json", self.config)
        bot.process_snapshot([self.row(0.50)], self.now)
        opened = bot.process_snapshot(
            [{**self.row(0.54), "fees_enabled": False}],
            self.now + timedelta(minutes=5),
        )["opened"][0]
        with patch("paper_trading.estimate_taker_fee", return_value={"known": False, "fee": None}):
            with self.assertRaisesRegex(ValueError, "valid fee estimate"):
                bot._close_position(
                    "token-1", 0.6, "test_exit", self.now + timedelta(minutes=10)
                )
        self.assertIn("token-1", bot._state["positions"])
        self.assertEqual(bot._state["positions"]["token-1"]["shares"], opened["shares"])

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
