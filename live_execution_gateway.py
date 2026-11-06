"""Gated Polymarket FAK execution gateway using the official polymarket-client SDK.

The adapter is disabled by default. It requires an explicit runtime arm, a
second confirmation phrase, a wallet allow-list match, a fresh risk snapshot,
a fresh point-in-time risk context, a passing live preflight, and a positive
Polymarket geoblock response from this same host. It records submission_pending
before the network post and never automatically retries ambiguous requests.

This module only supports bounded BUY FAK orders. FAK is chosen to avoid leaving
resting orders on the book; any unfilled remainder is intended to be cancelled
by the venue. No SELL, GTC, batch, cancel-all, or auto-retry path is exposed.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from account_risk_policy import MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS
from live_order_preflight import REQUIRED_RELEASE_GATES, evaluate_live_order_preflight
from order_journal import OrderJournal
from polymarket_geo import check_polymarket_geoblock

LIVE_CONFIRMATION_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"
LIVE_ORDER_TYPE = "FAK"
DEFAULT_RISK_CONTEXT_MAX_AGE_SECONDS = 30.0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} is missing or invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is missing or invalid") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _float(value: Any, field: str) -> float:
    parsed = float(_decimal(value, field))
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("risk_context captured_at_utc is required")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("risk_context timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _response_order_id(response: Any) -> str | None:
    for key in ("order_id", "id"):
        value = _field(response, key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _safe_reason(exc: Exception) -> str:
    # Exception text can contain request details. Persist only a stable type code.
    name = type(exc).__name__.lower()
    if "requestrejected" in name:
        return "venue_explicit_rejection"
    if "timeout" in name:
        return "transport_timeout_unknown"
    if "transport" in name or "connection" in name:
        return "transport_outcome_unknown"
    return "submission_exception_outcome_unknown"


class LiveExecutionConfig:
    """Validated, deliberately restrictive execution policy."""

    def __init__(self) -> None:
        self.enabled = _env_bool("LIVE_TRADING_ENABLED", False)
        self.armed = _env_bool("LIVE_TRADING_ARMED", False)
        self.dry_run = _env_bool("DRY_RUN", True)
        self.confirmation = os.getenv("LIVE_TRADING_CONFIRMATION", "")
        self.expected_wallet = os.getenv("LIVE_EXPECTED_WALLET", "").strip().lower()
        self.max_order_notional_usd = _float(
            os.getenv("LIVE_MAX_ORDER_NOTIONAL_USD", "5"), "LIVE_MAX_ORDER_NOTIONAL_USD"
        )
        self.max_open_positions = int(os.getenv("LIVE_MAX_OPEN_POSITIONS", "3"))
        self.max_open_exposure_usd = _float(
            os.getenv("LIVE_MAX_OPEN_EXPOSURE_USD", "20"), "LIVE_MAX_OPEN_EXPOSURE_USD"
        )
        self.daily_loss_limit_usd = _float(
            os.getenv("LIVE_DAILY_LOSS_LIMIT_USD", "5"), "LIVE_DAILY_LOSS_LIMIT_USD"
        )
        self.max_drawdown_pct = _float(
            os.getenv("LIVE_MAX_DRAWDOWN_PCT", "5"), "LIVE_MAX_DRAWDOWN_PCT"
        )
        self.max_price_impact_bps = _float(
            os.getenv("LIVE_MAX_PRICE_IMPACT_BPS", "100"), "LIVE_MAX_PRICE_IMPACT_BPS"
        )
        self.max_book_age_seconds = _float(
            os.getenv("LIVE_MAX_BOOK_AGE_SECONDS", "3"), "LIVE_MAX_BOOK_AGE_SECONDS"
        )
        self.risk_context_max_age_seconds = _float(
            os.getenv("LIVE_RISK_CONTEXT_MAX_AGE_SECONDS", "30"),
            "LIVE_RISK_CONTEXT_MAX_AGE_SECONDS",
        )
        if (
            self.max_order_notional_usd <= 0
            or self.max_open_positions < 1
            or self.max_open_exposure_usd <= 0
            or self.daily_loss_limit_usd <= 0
            or not 0 < self.max_drawdown_pct <= 25
            or not 0 <= self.max_price_impact_bps <= 1000
            or not 0 < self.max_book_age_seconds <= 10
            or not 0 < self.risk_context_max_age_seconds <= 60
        ):
            raise ValueError("live execution limits are outside the fail-closed policy")

    def armed_for_live_orders(self) -> bool:
        return bool(
            self.enabled
            and self.armed
            and not self.dry_run
            and self.confirmation == LIVE_CONFIRMATION_PHRASE
            and self.expected_wallet
        )


class LiveExecutionGateway:
    def __init__(
        self,
        journal: OrderJournal,
        *,
        config: LiveExecutionConfig | None = None,
        geoblock_checker: Callable[[], dict[str, Any]] = check_polymarket_geoblock,
        book_provider: Callable[[str], dict[str, Any]] | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.journal = journal
        self.config = config or LiveExecutionConfig()
        self.geoblock_checker = geoblock_checker
        self.book_provider = book_provider or self._fetch_public_book
        self.client_factory = client_factory or self._create_secure_client

    @staticmethod
    def _fetch_public_book(token_id: str) -> dict[str, Any]:
        from market_data import fetch_order_book
        return fetch_order_book(token_id)

    @staticmethod
    def _create_secure_client(private_key: str, wallet: str) -> Any:
        from polymarket import SecureClient
        return SecureClient.create(private_key=private_key, wallet=wallet)

    def submit_prepared_buy(
        self,
        client_order_id: str,
        *,
        risk_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit exactly one BUY FAK for a prepared intent or fail closed.

        This method never changes a prepared intent to pending until all
        read-only checks and local signing have succeeded. The pending state is
        committed immediately before post_order. Any uncertain post outcome is
        recorded as submission_unknown; the caller must reconcile, never retry.
        """
        if not self.config.armed_for_live_orders():
            return self._blocked("live_execution_not_armed")
        if not isinstance(risk_context, dict):
            return self._blocked("risk_context_missing")
        try:
            captured_at = _timestamp(risk_context.get("captured_at_utc"))
        except (TypeError, ValueError):
            return self._blocked("risk_context_timestamp_invalid")
        age = (datetime.now(timezone.utc) - captured_at).total_seconds()
        if age < -2 or age > self.config.risk_context_max_age_seconds:
            return self._blocked("risk_context_stale_or_future_dated")

        intent = self.journal.get_intent(client_order_id)
        if not isinstance(intent, dict):
            return self._blocked("prepared_intent_not_found")
        if intent.get("status") != "prepared" or intent.get("submission_attempts") != 0:
            return self._blocked("intent_is_not_eligible_for_first_submission")
        if intent.get("side") != "BUY":
            return self._blocked("only_buy_fak_is_supported")
        preflight_report = intent.get("preflight_report")
        if not isinstance(preflight_report, dict) or preflight_report.get("preflight_passed") is not True:
            return self._blocked("staging_preflight_not_passed")
        input_evidence = preflight_report.get("preflight_input_evidence")
        if not isinstance(input_evidence, dict):
            return self._blocked("staging_preflight_evidence_missing")
        release_gates = risk_context.get("release_gates")
        if not isinstance(release_gates, dict) or any(
            release_gates.get(gate) is not True for gate in REQUIRED_RELEASE_GATES
        ):
            return self._blocked("live_release_gates_not_verified")

        snapshot_id = intent.get("account_risk_snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            return self._blocked("intent_account_risk_snapshot_missing")
        try:
            snapshot = self.journal.get_latest_account_risk_snapshot(
                max_age_seconds=MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS
            )
            sync_state = self.journal.get_latest_account_risk_sync_state(
                max_age_seconds=MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS
            )
        except Exception:
            return self._blocked("account_risk_snapshot_unavailable")
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("fresh") is not True
            or snapshot.get("snapshot_complete") is not True
            or snapshot.get("snapshot_id") != snapshot_id
            or snapshot.get("execution") != "none"
        ):
            return self._blocked("account_risk_snapshot_stale_or_changed")
        if (
            not isinstance(sync_state, dict)
            or sync_state.get("status") != "success"
            or sync_state.get("fresh") is not True
            or sync_state.get("snapshot_id") != snapshot_id
        ):
            return self._blocked("account_risk_sync_not_currently_successful")

        try:
            geo = self.geoblock_checker()
        except Exception:
            return self._blocked("polymarket_geoblock_check_failed")
        if not isinstance(geo, dict) or geo.get("verified") is not True or geo.get("blocked") is not False:
            return self._blocked("polymarket_geoblock_rejected_or_unverified")
        if geo.get("country") != str(geo.get("country") or "").upper():
            return self._blocked("polymarket_geoblock_country_invalid")

        token_id = str(intent.get("token_id") or "").strip()
        if not token_id:
            return self._blocked("intent_token_id_missing")
        try:
            book = self.book_provider(token_id)
            best_ask = _decimal(book.get("best_ask"), "best_ask")
            tick_size = _decimal(book.get("tick_size"), "tick_size")
            minimum_order_size = _decimal(book.get("min_order_size"), "minimum_order_size")
            observed_at = _timestamp(book.get("observed_at"))
            book_age = (datetime.now(timezone.utc) - observed_at).total_seconds()
            limit_price = _decimal(intent.get("limit_price"), "limit_price")
            size = _decimal(intent.get("size"), "size")
            if book.get("book_usable") is not True or book.get("crossed_book") is True:
                return self._blocked("live_order_book_not_usable")
            if book_age < -2 or book_age > self.config.max_book_age_seconds:
                return self._blocked("live_order_book_stale")
            if tick_size <= 0 or minimum_order_size <= 0:
                return self._blocked("live_market_tick_or_lot_invalid")
            if limit_price % tick_size != 0:
                return self._blocked("live_order_price_off_tick")
            if size < minimum_order_size:
                return self._blocked("live_order_below_minimum_lot")
            if limit_price <= 0 or limit_price >= 1 or size <= 0:
                return self._blocked("live_order_terms_invalid")
            notional = limit_price * size
            if notional > Decimal(str(self.config.max_order_notional_usd)):
                return self._blocked("live_order_notional_limit_exceeded")
            max_allowed_price = best_ask * (
                Decimal("1") + Decimal(str(self.config.max_price_impact_bps)) / Decimal("10000")
            )
            if limit_price > max_allowed_price:
                return self._blocked("live_order_price_impact_exceeded")
        except Exception:
            return self._blocked("live_market_data_invalid")

        private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        deposit_wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
        if not private_key or not deposit_wallet:
            return self._blocked("secure_wallet_credentials_missing")
        if deposit_wallet.lower() != self.config.expected_wallet:
            return self._blocked("deposit_wallet_allowlist_mismatch")

        client = None
        submission_started = False
        try:
            client = self.client_factory(private_key, deposit_wallet)
            if client.get_closed_only_mode() is True:
                return self._blocked("account_is_closed_only")
            balance_allowance = client.get_balance_allowance(asset_type=self._collateral_asset_type())
            # The official SDK returns collateral balance and allowances in
            # base units (USDC uses six decimals), not whole-dollar values.
            balance_units = _decimal(_field(balance_allowance, "balance"), "collateral_balance_base_units")
            allowances = _field(balance_allowance, "allowances")
            if not isinstance(allowances, dict) or not allowances:
                return self._blocked("collateral_allowance_map_unavailable")
            allowance_units = max(
                (_decimal(value, "collateral_allowance_base_units") for value in allowances.values()),
                default=Decimal("0"),
            )
            balance = balance_units / Decimal("1000000")
            allowance = allowance_units / Decimal("1000000")
            approval_state = client.get_trading_approvals_state()
            if _field(approval_state, "is_fully_approved") is not True:
                return self._blocked("required_trading_approvals_missing")
            if balance <= 0 or allowance <= 0:
                return self._blocked("collateral_balance_or_allowance_unavailable")
            conservative_available = _decimal(
                snapshot.get("conservative_available_collateral_usd"),
                "conservative_available_collateral_usd",
            )
            estimated_fees = _decimal(risk_context.get("estimated_fees_usd"), "estimated_fees_usd")
            total_required = notional + estimated_fees
            if (
                conservative_available < total_required
                or balance < total_required
                or allowance < notional
            ):
                return self._blocked("live_collateral_or_allowance_insufficient")

            context = {
                "token_id": token_id,
                "side": "BUY",
                "limit_price": float(limit_price),
                "size": float(size),
                "best_ask": float(best_ask),
                "tick_size": float(tick_size),
                "minimum_order_size": float(minimum_order_size),
                "book_age_seconds": max(0.0, book_age),
                "max_book_age_seconds": self.config.max_book_age_seconds,
                "max_price_impact_bps": self.config.max_price_impact_bps,
                "max_order_notional_usd": self.config.max_order_notional_usd,
                "available_collateral_usd": float(min(balance, conservative_available)),
                "estimated_fees_usd": float(estimated_fees),
                "current_open_positions": int(snapshot.get("current_open_positions")),
                "max_open_positions": self.config.max_open_positions,
                "opposing_market_outcome_open": risk_context.get("opposing_market_outcome_open"),
                "current_open_exposure_usd": float(snapshot.get("current_open_exposure_usd")),
                "max_open_exposure_usd": self.config.max_open_exposure_usd,
                "daily_realized_pnl_usd": risk_context.get("daily_realized_pnl_usd"),
                "open_unrealized_pnl_usd": float(snapshot.get("open_unrealized_pnl_usd")),
                "daily_loss_limit_usd": self.config.daily_loss_limit_usd,
                "current_drawdown_pct": risk_context.get("current_drawdown_pct"),
                "max_drawdown_pct": self.config.max_drawdown_pct,
                "market_active": risk_context.get("market_active") is True,
                "kill_switch_engaged": risk_context.get("kill_switch_engaged") is not False,
                "release_gates": release_gates,
            }
            preflight = evaluate_live_order_preflight(**context)
            if preflight.get("preflight_passed") is not True:
                return self._blocked("live_order_preflight_blocked", blockers=preflight.get("blockers", []))

            # BUY FAK amount is quote spend, not shares. The limit price bounds
            # the worst execution price; any unfilled remainder is not left resting.
            spend = notional
            max_spend = notional + estimated_fees
            signed_order = client.create_market_order(
                asset_id=token_id,
                side="BUY",
                amount=str(spend),
                max_spend=str(max_spend),
                max_price=str(limit_price),
                order_type=LIVE_ORDER_TYPE,
            )
            # Durable local state must be committed before the first network post.
            self.journal.begin_submission(client_order_id)
            submission_started = True
            response = client.post_order(signed_order)
            order_id = _response_order_id(response)
            if order_id:
                self.journal.record_submission_response(
                    client_order_id, outcome="accepted", exchange_order_id=order_id
                )
                return {
                    "status": "accepted_for_venue_processing",
                    "client_order_id": client_order_id,
                    "exchange_order_id": order_id,
                    "order_type": LIVE_ORDER_TYPE,
                    "notional_usd": float(notional),
                    "geoblock": {"country": geo.get("country"), "region": geo.get("region")},
                    "execution": "live_order_submitted",
                    "automatic_retry_permitted": False,
                }
            explicit_error = (
                _field(response, "error_msg")
                or _field(response, "error")
                or _field(response, "message")
            )
            if explicit_error or _field(response, "success") is False or _field(response, "ok") is False:
                self.journal.record_submission_response(
                    client_order_id, outcome="rejected", reason_code="explicit_venue_rejection"
                )
                return self._blocked(
                    "venue_rejected_order",
                    client_order_id=client_order_id,
                    automatic_retry_permitted=False,
                )
            self.journal.record_submission_response(
                client_order_id, outcome="ambiguous", reason_code="missing_order_id_in_ack"
            )
            return self._blocked(
                "submission_outcome_ambiguous_reconcile_required",
                client_order_id=client_order_id,
                automatic_retry_permitted=False,
            )
        except Exception as exc:
            if submission_started:
                reason_code = _safe_reason(exc)
                try:
                    if reason_code == "venue_explicit_rejection":
                        self.journal.record_submission_response(
                            client_order_id, outcome="rejected", reason_code=reason_code
                        )
                        return self._blocked(
                            "venue_rejected_order",
                            client_order_id=client_order_id,
                            automatic_retry_permitted=False,
                        )
                    self.journal.record_submission_response(
                        client_order_id, outcome="ambiguous", reason_code=reason_code
                    )
                except Exception:
                    # Never issue another request if the local write itself fails.
                    pass
                return self._blocked(
                    "submission_outcome_ambiguous_reconcile_required",
                    client_order_id=client_order_id,
                    automatic_retry_permitted=False,
                )
            return self._blocked("live_execution_pre_submit_check_failed")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _collateral_asset_type() -> Any:
        try:
            from polymarket import AssetType
            return AssetType.COLLATERAL
        except Exception:
            return "COLLATERAL"

    @staticmethod
    def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": "blocked",
            "reason": reason,
            "order_submission_allowed": False,
            "automatic_retry_permitted": False,
            "execution": "none",
            **extra,
        }
