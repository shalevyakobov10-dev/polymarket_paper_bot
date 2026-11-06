import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from live_execution_gateway import (
    LIVE_CONFIRMATION_PHRASE,
    LiveExecutionConfig,
    LiveExecutionGateway,
)
from order_journal import OrderJournal
from order_lifecycle import create_order_intent
from live_order_preflight import REQUIRED_RELEASE_GATES


def complete_risk_snapshot():
    return {
        "snapshot_complete": True,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "collateral_decimals": 6,
        "collateral_balance_base_units": "100000000",
        "collateral_balance_usd": "100",
        "open_buy_commitment_usd": "0",
        "conservative_available_collateral_usd": "100",
        "collateral_shortfall_usd": "0",
        "current_open_exposure_usd": "0",
        "open_position_value_usd": "0",
        "open_position_cost_usd": "0",
        "open_position_risk_exposure_usd": "0",
        "open_unrealized_pnl_usd": "0",
        "open_sell_remaining_shares": "0",
        "current_open_positions": 0,
        "current_open_orders": 0,
        "open_position_assets_by_condition": {},
        "open_order_assets_by_condition": {},
        "execution": "none",
    }


def live_env():
    return {
        "LIVE_TRADING_ENABLED": "true",
        "LIVE_TRADING_ARMED": "true",
        "DRY_RUN": "false",
        "LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION_PHRASE,
        "LIVE_EXPECTED_WALLET": "0x" + "1" * 40,
        "POLYMARKET_PRIVATE_KEY": "test-secret-not-real",
        "POLYMARKET_DEPOSIT_WALLET": "0x" + "1" * 40,
        "LIVE_MAX_ORDER_NOTIONAL_USD": "10",
        "LIVE_MAX_OPEN_POSITIONS": "3",
        "LIVE_MAX_OPEN_EXPOSURE_USD": "20",
        "LIVE_DAILY_LOSS_LIMIT_USD": "5",
        "LIVE_MAX_DRAWDOWN_PCT": "5",
        "LIVE_MAX_PRICE_IMPACT_BPS": "100",
        "LIVE_MAX_BOOK_AGE_SECONDS": "3",
        "LIVE_RISK_CONTEXT_MAX_AGE_SECONDS": "30",
    }


class FakeClient:
    def __init__(self, *, response=None, post_exception=None, balance="100", allowance="100", closed_only=False):
        self.response = response if response is not None else {"order_id": "venue-order-001", "success": True}
        self.post_exception = post_exception
        self.balance = balance
        self.allowance = allowance
        self.closed_only = closed_only
        self.created = []
        self.posts = []
        self.closed = False

    def get_closed_only_mode(self):
        return self.closed_only

    def get_balance_allowance(self, *, asset_type):
        return {"balance": self.balance, "allowance": self.allowance, "asset_type": str(asset_type)}

    def create_market_order(self, **kwargs):
        self.created.append(kwargs)
        return {"signed": True, **kwargs}

    def post_order(self, signed_order):
        self.posts.append(signed_order)
        if self.post_exception:
            raise self.post_exception
        return self.response

    def close(self):
        self.closed = True


class RequestRejectedError(Exception):
    pass


class LiveExecutionGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "orders.sqlite3"
        self.journal = OrderJournal(self.db_path)
        snapshot = complete_risk_snapshot()
        self.snapshot_id = self.journal.save_account_risk_snapshot(snapshot)
        intent = create_order_intent(
            client_order_id="paper-intent-001",
            token_id="token-live-001",
            side="BUY",
            limit_price=0.50,
            size=10,
        )
        intent["market_condition_id"] = "condition-001"
        intent["account_risk_snapshot_id"] = self.snapshot_id
        intent["preflight_report"] = {
            "preflight_passed": True,
            "preflight_input_evidence": {"staged_at_utc": datetime.now(timezone.utc).isoformat()},
            "execution": "none",
        }
        self.journal.create_intent(intent)
        self.client = FakeClient()
        self.config_env = live_env()
        self.gateway_kwargs = {
            "geoblock_checker": lambda: {
                "verified": True, "blocked": False, "country": "IL",
                "region": "TA", "ip_redacted": True,
            },
            "book_provider": lambda token: {
                "token_id": token,
                "best_bid": 0.49,
                "best_ask": 0.50,
                "tick_size": "0.01",
                "min_order_size": "5",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "book_usable": True,
                "crossed_book": False,
            },
            "client_factory": lambda private_key, wallet: self.client,
        }

    def tearDown(self):
        self.journal._connect().close()
        self.temp.cleanup()

    def _risk_context(self, **overrides):
        result = {
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "release_gates": {gate: True for gate in REQUIRED_RELEASE_GATES},
            "estimated_fees_usd": 0.05,
            "opposing_market_outcome_open": False,
            "daily_realized_pnl_usd": 0.0,
            "current_drawdown_pct": 0.0,
            "market_active": True,
            "kill_switch_engaged": False,
        }
        result.update(overrides)
        return result

    def _run_gateway(self, *, env=None, **kwargs):
        with patch.dict(os.environ, env or self.config_env, clear=True):
            gateway = LiveExecutionGateway(
                self.journal,
                config=LiveExecutionConfig(),
                **{**self.gateway_kwargs, **kwargs},
            )
            return gateway.submit_prepared_buy(
                "paper-intent-001",
                risk_context=self._risk_context(),
            )

    def test_default_configuration_does_not_arm_live_orders(self):
        with patch.dict(os.environ, {}, clear=True):
            config = LiveExecutionConfig()
        self.assertFalse(config.armed_for_live_orders())

    def test_dry_run_or_missing_confirmation_blocks_submission(self):
        env = {**self.config_env, "DRY_RUN": "true"}
        result = self._run_gateway(env=env)
        self.assertEqual(result["reason"], "live_execution_not_armed")
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")
        self.assertEqual(self.client.posts, [])

    def test_geoblock_denial_fails_closed_before_signing(self):
        result = self._run_gateway(
            geoblock_checker=lambda: {
                "verified": True, "blocked": True, "country": "XX",
                "region": "YY", "ip_redacted": True,
            }
        )
        self.assertEqual(result["reason"], "polymarket_geoblock_rejected_or_unverified")
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.client.posts, [])

    def test_geoblock_network_error_fails_closed_before_signing(self):
        def fail():
            raise RuntimeError("network unavailable")
        result = self._run_gateway(geoblock_checker=fail)
        self.assertEqual(result["reason"], "polymarket_geoblock_check_failed")
        self.assertEqual(self.client.posts, [])

    def test_stale_risk_context_fails_closed(self):
        with patch.dict(os.environ, self.config_env, clear=True):
            gateway = LiveExecutionGateway(self.journal, config=LiveExecutionConfig(), **self.gateway_kwargs)
            stale = self._risk_context(captured_at_utc="2020-01-01T00:00:00+00:00")
            blocked = gateway.submit_prepared_buy("another-id", risk_context=stale)
        self.assertEqual(blocked["reason"], "risk_context_stale_or_future_dated")
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")
        self.assertEqual(self.client.posts, [])

    def test_success_posts_one_fak_buy_and_records_exchange_id(self):
        result = self._run_gateway()
        self.assertEqual(result["status"], "accepted_for_venue_processing")
        self.assertEqual(result["order_type"], "FAK")
        self.assertEqual(result["exchange_order_id"], "venue-order-001")
        self.assertEqual(len(self.client.posts), 1)
        params = self.client.created[0]
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["order_type"], "FAK")
        self.assertEqual(params["asset_id"], "token-live-001")
        state = self.journal.get_intent("paper-intent-001")
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["submission_attempts"], 1)
        self.assertTrue(self.client.closed)

    def test_ambiguous_transport_outcome_is_not_retried(self):
        self.client.post_exception = TimeoutError("timed out after request")
        first = self._run_gateway()
        self.assertEqual(first["reason"], "submission_outcome_ambiguous_reconcile_required")
        state = self.journal.get_intent("paper-intent-001")
        self.assertEqual(state["status"], "submission_unknown")
        self.assertEqual(state["submission_attempts"], 1)
        with patch.dict(os.environ, self.config_env, clear=True):
            gateway = LiveExecutionGateway(self.journal, config=LiveExecutionConfig(), **self.gateway_kwargs)
            second = gateway.submit_prepared_buy(
                "paper-intent-001", risk_context=self._risk_context()
            )
        self.assertEqual(second["reason"], "intent_is_not_eligible_for_first_submission")
        self.assertEqual(len(self.client.posts), 1)

    def test_explicit_venue_rejection_is_recorded_as_rejected(self):
        self.client.post_exception = RequestRejectedError("rejected")
        result = self._run_gateway()
        self.assertEqual(result["reason"], "venue_rejected_order")
        state = self.journal.get_intent("paper-intent-001")
        self.assertEqual(state["status"], "rejected")
        self.assertEqual(state["submission_attempts"], 1)
        self.assertEqual(len(self.client.posts), 1)

    def test_closed_only_account_is_blocked_before_submission(self):
        self.client.closed_only = True
        result = self._run_gateway()
        self.assertEqual(result["reason"], "account_is_closed_only")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")

    def test_insufficient_collateral_is_blocked_before_submission(self):
        self.client.balance = "1"
        result = self._run_gateway()
        self.assertEqual(result["reason"], "live_collateral_or_allowance_insufficient")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")

    def test_missing_release_gate_blocks_before_submission(self):
        with patch.dict(os.environ, self.config_env, clear=True):
            gateway = LiveExecutionGateway(self.journal, config=LiveExecutionConfig(), **self.gateway_kwargs)
            gates = {gate: True for gate in REQUIRED_RELEASE_GATES}
            gates["strategy_validation_passed"] = False
            result = gateway.submit_prepared_buy(
                "paper-intent-001",
                risk_context=self._risk_context(release_gates=gates),
            )
        self.assertEqual(result["reason"], "live_release_gates_not_verified")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")

    def test_price_impact_and_stale_book_are_blocked(self):
        bad_book = {
            "token_id": "token-live-001", "best_bid": 0.40, "best_ask": 0.45,
            "tick_size": "0.01", "min_order_size": "5",
            "observed_at": "2020-01-01T00:00:00+00:00",
            "book_usable": True, "crossed_book": False,
        }
        result = self._run_gateway(book_provider=lambda _token: bad_book)
        self.assertEqual(result["reason"], "live_order_book_stale")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.journal.get_intent("paper-intent-001")["status"], "prepared")

    def test_wallet_allowlist_mismatch_blocks_before_client_creation(self):
        env = {**self.config_env, "LIVE_EXPECTED_WALLET": "0x" + "2" * 40}
        result = self._run_gateway(env=env)
        self.assertEqual(result["reason"], "deposit_wallet_allowlist_mismatch")
        self.assertEqual(self.client.posts, [])

if __name__ == "__main__":
    unittest.main()
