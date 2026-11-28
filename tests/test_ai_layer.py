import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ai.arena_router import ArenaRouter
from ai.chatgpt_analyst import ChatGPTAnalyst
from arena_bridge import main as bridge_main
from arena_bridge import parse_result, serve
from config import LIVE_ACK_PHRASE, BotSettings, load_settings
from core.decision_loop import decide_and_execute
from core.live_gateway import LiveOrderGateway
from core.order_state_machine import FILLED, OrderStateMachine
from core.public_stream import build_subscribe_message, decode_market_frame
from core.risk_manager import evaluate_sized_entry, fractional_kelly_notional
from core.trade_log import TradeJournal, _redact
from execution_readiness import execution_readiness_payload
from main import main
from paper_trading import PaperConfig, PaperTrader


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ai_cycle_markets.json"


def market(price=0.42, estimate=0.70, confidence=0.80):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    payload["observed_price"] = price
    if estimate is None:
        payload.pop("research_estimate", None)
        payload.pop("research_confidence", None)
    else:
        payload["research_estimate"] = estimate
        payload["research_confidence"] = confidence
    return payload


def settings(**overrides) -> BotSettings:
    base = dict(
        paper_mode=True,
        live_mode=False,
        ai_offline=True,
        min_consensus_score=0.67,
        max_position_usdc=2.0,
        max_daily_drawdown_usdc=5.0,
        kelly_fraction=0.25,
        min_edge_bps=100,
        min_confidence=0.55,
    )
    base.update(overrides)
    return BotSettings(**base)


def paper_trader(directory: Path) -> PaperTrader:
    return PaperTrader(directory / "paper_state.json", PaperConfig(require_orderbook=True, max_position_usd=2.0))


class ConfigAndRiskTests(unittest.TestCase):
    def test_paper_wins_when_both_modes_are_set_and_secrets_stay_out_of_logs(self):
        with patch.dict(os.environ, {
            "PAPER_MODE": "true",
            "LIVE_MODE": "true",
            "OPENAI_API_KEY": "sk-test-secret",
            "POLYMARKET_PRIVATE_KEY": "0xshould-not-load",
        }):
            loaded = load_settings()
        self.assertTrue(loaded.paper_mode)
        self.assertFalse(loaded.live_mode)
        self.assertEqual(loaded.mode_note, "paper_wins_conflicting_live_flag")
        self.assertEqual(loaded.polymarket_private_key, "")
        public = json.dumps(loaded.public_dict())
        self.assertNotIn("sk-test-secret", public)
        self.assertNotIn("0xshould-not-load", public)
        self.assertTrue(loaded.public_dict()["openai_api_key_present"])
        self.assertFalse(loaded.public_dict()["live_submission_implemented"])

    def test_environment_still_cannot_mark_the_service_live(self):
        with patch.dict(os.environ, {"PAPER_MODE": "false", "LIVE_MODE": "true", "LIVE_TRADING_ACK": LIVE_ACK_PHRASE}):
            self.assertEqual(load_settings().mode_note, "live_requested_submission_still_blocked")
        report = execution_readiness_payload()
        self.assertEqual(report["mode"], "paper_only")
        self.assertFalse(report["order_submission_allowed"])

    def test_fractional_kelly_is_capped_and_then_hard_limited(self):
        self.assertEqual(
            fractional_kelly_notional(
                probability=0.6, market_price=0.5, bankroll=100, kelly_fraction=0.5, max_position_usdc=10
            ),
            10.0,
        )
        self.assertEqual(
            fractional_kelly_notional(
                probability=0.9, market_price=0.4, bankroll=100, kelly_fraction=1, max_position_usdc=3
            ),
            3.0,
        )
        self.assertEqual(
            fractional_kelly_notional(
                probability=0.4, market_price=0.5, bankroll=100, kelly_fraction=0.25, max_position_usdc=2
            ),
            0.0,
        )
        decision, notional = evaluate_sized_entry(
            probability=0.7,
            market_price=0.42,
            bankroll=100,
            kelly_fraction=0.25,
            max_position_usdc=2,
            cash=100,
            open_exposure=0,
            open_positions=0,
            max_open_exposure_usd=10,
            max_open_positions=5,
            daily_realized_pnl=-5,
            open_unrealized_pnl=0,
            max_daily_drawdown_usdc=5,
            fee_rate=0,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "daily_loss_limit_reached")
        self.assertGreater(notional, 0)


class AnalystAndArenaTests(unittest.TestCase):
    def test_openai_edge_is_recomputed_and_search_is_domain_filtered(self):
        seen = {}

        def transport(url, payload, headers):
            seen["url"] = url
            seen["payload"] = payload
            seen["auth"] = headers["Authorization"]
            return {
                "output_text": json.dumps({
                    "estimated_probability": 0.62,
                    "confidence_score": 0.9,
                    "edge_bps": 99999,
                    "reasoning": "Reuters reported a concrete development.",
                    "risk_flags": ["thin_book"],
                })
            }

        analyst = ChatGPTAnalyst(settings(ai_offline=False, openai_api_key="sk-test"), transport)
        view, prompt = analyst.analyze(market())
        self.assertIn("/v1/responses", seen["url"])
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertNotIn("sk-test", prompt)
        tool = seen["payload"]["tools"][0]
        self.assertEqual(tool["type"], "web_search")
        self.assertIn("reuters.com", tool["filters"]["allowed_domains"])
        self.assertEqual(view.edge_bps, 2000)
        self.assertEqual(view.side, "BUY")
        self.assertEqual(view.source, "openai_responses")

    def test_failed_model_call_abstains_instead_of_using_a_fixture(self):
        def transport(url, payload, headers):
            raise RuntimeError("down")

        analyst = ChatGPTAnalyst(settings(ai_offline=False, openai_api_key="sk-test"), transport)
        view, _ = analyst.analyze(market())
        self.assertEqual(view.side, "ABSTAIN")
        self.assertIn("model_request_failed", view.risk_flags)

    def test_divergent_models_fail_consensus_and_agreement_passes(self):
        def transport(url, payload, headers):
            model = payload["model"]
            probability = 0.8 if "gpt" in model else 0.2
            return {"choices": [{"message": {"content": json.dumps({
                "estimated_probability": probability,
                "confidence_score": 0.9,
                "edge_bps": 0,
                "reasoning": model,
                "risk_flags": [],
            })}}]}

        router = ArenaRouter(
            settings(ai_offline=False, openrouter_api_key="or-test", openai_api_key=""),
            transport,
        )
        consensus, _ = router.evaluate(market())
        self.assertFalse(consensus.approved)
        self.assertGreaterEqual(len(consensus.votes), 2)

        def agreeing(url, payload, headers):
            return {"choices": [{"message": {"content": json.dumps({
                "estimated_probability": 0.7,
                "confidence_score": 0.8,
                "edge_bps": 1,
                "reasoning": "agree",
                "risk_flags": [],
            })}}]}

        approved, _ = ArenaRouter(
            settings(ai_offline=False, openrouter_api_key="or-test"),
            agreeing,
        ).evaluate(market())
        self.assertTrue(approved.approved)
        self.assertGreaterEqual(approved.consensus_score, 0.67)
        self.assertEqual(approved.edge_bps, 2800)

    def test_manual_arena_reply_parses_and_stays_unapproved_alone(self):
        text = """```json
        {"estimated_probability": 0.66, "confidence_score": 0.77, "edge_bps": 5,
         "reasoning": "winning arena reply", "risk_flags": ["rules_ambiguity"]}
        ```"""
        parsed = parse_result(text, market_price=0.42)
        self.assertEqual(parsed["vote"]["side"], "BUY")
        self.assertEqual(parsed["vote"]["estimated_probability"], 0.66)
        self.assertFalse(parsed["consensus"]["approved"])
        self.assertEqual(parsed["consensus"]["reason"], "insufficient_model_votes")
        self.assertEqual(parsed["execution"], "none")


class DecisionLoopTests(unittest.TestCase):
    def test_paper_cycle_fills_once_and_blocks_the_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trader = paper_trader(directory)
            machine = OrderStateMachine(directory / "orders.json")
            journal = TradeJournal(directory)
            now = datetime.now(timezone.utc)
            first = decide_and_execute(
                market(),
                settings=settings(),
                trader=trader,
                machine=machine,
                analyst=ChatGPTAnalyst(settings()),
                arena=ArenaRouter(settings()),
                journal=journal,
                now=now,
                idempotency_key="cycle-1",
            )
            self.assertEqual(first["order_state"], FILLED, first)
            self.assertEqual(trader.snapshot()["mode"], "PAPER_ONLY")
            self.assertEqual(trader.snapshot()["metrics"]["open_positions"], 1)
            self.assertEqual(first["analyst_edge_bps"], 2800)
            second = decide_and_execute(
                market(),
                settings=settings(),
                trader=trader,
                machine=machine,
                analyst=ChatGPTAnalyst(settings()),
                arena=ArenaRouter(settings()),
                journal=journal,
                now=now + timedelta(minutes=1),
                idempotency_key="cycle-1",
            )
            self.assertEqual(second["risk_reason"], "duplicate_order")
            self.assertEqual(trader.snapshot()["metrics"]["open_positions"], 1)
            logged = (directory / "ai_decisions.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("sk-", logged)
            self.assertIn("offline_fixture", logged)

    def test_live_flag_does_not_construct_a_client_or_open_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trader = paper_trader(directory)
            calls = []

            def factory(bot_settings):
                calls.append(bot_settings.polymarket_private_key)
                raise AssertionError("client factory must not run")

            live_settings = settings(
                paper_mode=False,
                live_mode=True,
                ai_offline=True,
                live_trading_ack=LIVE_ACK_PHRASE,
                polymarket_private_key="0xsecret",
            )
            gateway = LiveOrderGateway(live_settings, client_factory=factory)
            result = decide_and_execute(
                market(),
                settings=live_settings,
                trader=trader,
                machine=OrderStateMachine(directory / "orders.json"),
                analyst=ChatGPTAnalyst(live_settings),
                arena=ArenaRouter(live_settings),
                journal=TradeJournal(directory),
                gateway=gateway,
                idempotency_key="live-1",
            )
            self.assertEqual(result["order_reason"], "live_preflight_blocked", result)
            self.assertFalse(result["submitted"])
            self.assertFalse(result["preflight"]["preflight_passed"])
            blockers = result["preflight"]["blockers"]
            # Every release gate must be reported as unverified, not fabricated.
            for gate in (
                "account_eligibility_verified",
                "jurisdiction_and_legal_review_approved",
                "strategy_validation_passed",
                "independent_security_review_passed",
                "production_operations_verified",
                "explicit_release_approved",
            ):
                self.assertIn(f"release_gate_not_verified:{gate}", blockers)
            # The operational checks still ran against the real book values.
            self.assertNotIn("stale_or_invalid_order_book", blockers)
            self.assertNotIn("invalid_best_ask", blockers)
            self.assertNotIn("limit_price_not_on_tick", blockers)
            self.assertNotIn("below_minimum_order_size", blockers)
            self.assertNotIn("price_impact_limit_exceeded", blockers)
            self.assertNotIn("max_order_notional_exceeded", blockers)
            self.assertEqual(calls, [])
            self.assertEqual(gateway.factory_calls, 0)
            self.assertEqual(trader.snapshot()["metrics"]["open_positions"], 0)
            self.assertNotIn("0xsecret", (directory / "ai_decisions.jsonl").read_text(encoding="utf-8"))

    def test_main_offline_cycle_writes_csv_and_json_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AI_OFFLINE": "1", "PAPER_MODE": "true", "LIVE_MODE": "false"}):
                code = main([
                    "--once",
                    "--markets-file",
                    str(FIXTURE),
                    "--data-dir",
                    tmp,
                ])
            self.assertEqual(code, 0)
            summary = json.loads((Path(tmp) / "cycle_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["filled"], 1)
            self.assertEqual(summary["rejected"], 1)
            self.assertFalse(summary["live_submission_implemented"])
            self.assertTrue((Path(tmp) / "ai_decisions.csv").exists())
            self.assertTrue((Path(tmp) / "pnl_summary.csv").exists())
            import pandas as pd
            frame = pd.read_csv(Path(tmp) / "pnl_summary.csv")
            self.assertEqual(len(frame), 2)
            self.assertIn("consensus_score", frame.columns)


class BridgeAndStreamTests(unittest.TestCase):
    def test_webhook_formats_prompt_and_parses_result(self):
        server = serve("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            prompt_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/prompt",
                data=json.dumps({"market": market()}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(prompt_request, timeout=5) as response:
                prompt = json.loads(response.read().decode("utf-8"))
            self.assertIn("Observed outcome price", prompt["prompt"])
            self.assertEqual(prompt["execution"], "none")
            result_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/result",
                data=json.dumps({
                    "market_price": 0.42,
                    "response": json.dumps({
                        "estimated_probability": 0.61,
                        "confidence_score": 0.7,
                        "edge_bps": 0,
                        "reasoning": "pasted",
                        "risk_flags": [],
                    }),
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(result_request, timeout=5) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            self.assertEqual(parsed["vote"]["edge_bps"], 1900)
            self.assertEqual(parsed["execution"], "none")
        finally:
            server.shutdown()
            server.server_close()

    def test_prompt_cli_and_public_stream_frames(self):
        import websockets

        buffer = StringIO()
        with patch("sys.stdout", buffer):
            code = bridge_main(["prompt", "--markets-file", str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertIn("Observed outcome price", buffer.getvalue())
        self.assertTrue(hasattr(websockets, "connect"))
        message = build_subscribe_message(["a", "a", "b"])
        self.assertEqual(message["assets_ids"], ["a", "b"])
        self.assertEqual(decode_market_frame('[{"event_type": "book"}]'), [{"event_type": "book"}])
        self.assertEqual(decode_market_frame("PONG"), [])

    def test_redaction_drops_secret_fields(self):
        cleaned = _redact({"openai_api_key": "sk-secret", "token_id": "token-1", "nested": {"polymarket_private_key": "0xabc"}})
        self.assertEqual(cleaned["openai_api_key"], "present")
        self.assertEqual(cleaned["token_id"], "token-1")
        self.assertEqual(cleaned["nested"]["polymarket_private_key"], "present")


if __name__ == "__main__":
    unittest.main()
