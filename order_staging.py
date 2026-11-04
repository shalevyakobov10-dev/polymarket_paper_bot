"""Fail-closed bridge from live-order preflight to a durable local intent.

A passing preflight may create a local prepared intent for audit and review.
This module cannot sign, submit, retry, or cancel orders. It deliberately
preserves the preflight contract that execution remains disabled.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from live_order_preflight import evaluate_live_order_preflight
from order_journal import OrderJournal
from order_lifecycle import create_order_intent


def stage_buy_order_intent(
    journal: OrderJournal,
    *,
    client_order_id: str,
    token_id: str,
    side: str,
    limit_price: float,
    size: float,
    market_condition_id: str,
    preflight_context: Mapping[str, Any],
    max_account_snapshot_age_seconds: float = 60,
) -> dict[str, Any]:
    """Evaluate every preflight gate, then persist a prepared intent only.

    preflight_context contains non-account inputs such as book quality, fees,
    configured limits, and release gates. Account balance, exposure, position
    count, and opposing-outcome exposure are always overwritten from the latest
    persisted complete account snapshot. No network client or secret is accepted.
    """
    if not isinstance(market_condition_id, str) or not market_condition_id.strip():
        return {
            "status": "blocked",
            "blockers": ["missing_market_condition_id"],
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    try:
        account_snapshot = journal.get_latest_account_risk_snapshot(
            max_age_seconds=max_account_snapshot_age_seconds
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "blockers": ["invalid_account_risk_snapshot_freshness", str(exc)[:180]],
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }
    if (
        not isinstance(account_snapshot, dict)
        or account_snapshot.get("snapshot_complete") is not True
        or account_snapshot.get("execution") != "none"
        or account_snapshot.get("fresh") is not True
    ):
        return {
            "status": "blocked",
            "blockers": ["account_risk_snapshot_missing_or_stale"],
            "intent_created": False,
            "order_submission_allowed": False,
            "execution": "none",
        }

    if not isinstance(preflight_context, Mapping):
        return {
            "status": "blocked",
            "blockers": ["invalid_preflight_context"],
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    try:
        normalized_side = side.upper() if isinstance(side, str) else side
        terms = {
            "token_id": token_id,
            "side": normalized_side,
            "limit_price": limit_price,
            "size": size,
        }
        context = dict(preflight_context)
        # Never trust caller-supplied account metrics. These values must come
        # from the most recent complete authenticated account snapshot.
        required_snapshot_fields = (
            "snapshot_id", "captured_at_utc", "conservative_available_collateral_usd",
            "current_open_exposure_usd", "current_open_positions", "current_open_orders",
            "open_unrealized_pnl_usd", "open_position_assets_by_condition",
            "open_order_assets_by_condition",
        )
        if any(field not in account_snapshot for field in required_snapshot_fields):
            raise ValueError("account risk snapshot is missing required risk fields")
        positions_by_condition = account_snapshot["open_position_assets_by_condition"]
        orders_by_condition = account_snapshot["open_order_assets_by_condition"]
        if not isinstance(positions_by_condition, dict) or not isinstance(orders_by_condition, dict):
            raise ValueError("account risk snapshot has invalid condition/asset maps")
        condition_assets = set()
        for asset_map in (positions_by_condition, orders_by_condition):
            assets = asset_map.get(market_condition_id, [])
            if not isinstance(assets, (list, tuple, set)) or any(
                not isinstance(asset, str) or not asset.strip() for asset in assets
            ):
                raise ValueError("account risk snapshot has malformed condition assets")
            condition_assets.update(assets)
        positions = account_snapshot["current_open_positions"]
        open_orders = account_snapshot["current_open_orders"]
        if (
            isinstance(positions, bool) or not isinstance(positions, int) or positions < 0
            or isinstance(open_orders, bool) or not isinstance(open_orders, int) or open_orders < 0
        ):
            raise ValueError("account risk snapshot has invalid position/order counts")
        opposing_open = any(asset != token_id for asset in condition_assets)
        context.update({
            "available_collateral_usd": account_snapshot["conservative_available_collateral_usd"],
            "current_open_exposure_usd": account_snapshot["current_open_exposure_usd"],
            "current_open_positions": positions + open_orders,
            "open_unrealized_pnl_usd": account_snapshot["open_unrealized_pnl_usd"],
            "opposing_market_outcome_open": opposing_open,
        })
        for name, value in terms.items():
            if name in context and str(context[name]) != str(value):
                return {
                    "status": "blocked",
                    "blockers": [f"preflight_order_term_mismatch:{name}"],
                    "intent_created": False,
                    "order_submission_allowed": False,
                    "order_signing_allowed": False,
                    "execution": "none",
                }
            context[name] = value
        report = evaluate_live_order_preflight(**context)
    except (TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "blockers": ["invalid_preflight_context", str(exc)[:180]],
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    # This is an invariant, not a configurable feature flag. If the preflight
    # contract ever changes, staging must stop until separately reviewed.
    if (
        report.get("order_submission_implemented") is not False
        or report.get("order_submission_allowed") is not False
        or report.get("order_signing_allowed") is not False
        or report.get("execution") != "none"
    ):
        return {
            "status": "blocked",
            "blockers": ["unsafe_preflight_execution_contract"],
            "preflight_report": report,
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    if report.get("preflight_passed") is not True:
        return {
            "status": "blocked",
            "blockers": list(report.get("blockers", ["preflight_not_passed"])),
            "preflight_report": report,
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    try:
        intent = create_order_intent(
            client_order_id=client_order_id,
            token_id=token_id,
            side=normalized_side,
            limit_price=limit_price,
            size=size,
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "blocked",
            "blockers": ["invalid_order_intent", str(exc)[:180]],
            "preflight_report": report,
            "intent_created": False,
            "order_submission_allowed": False,
            "order_signing_allowed": False,
            "execution": "none",
        }

    # Preserve the passing preflight with the initial append-only intent event.
    existing_before = journal.get_intent(client_order_id)
    intent["preflight_report"] = report
    intent["market_condition_id"] = market_condition_id.strip()
    intent["account_risk_snapshot_id"] = account_snapshot["snapshot_id"]
    intent["account_risk_snapshot_captured_at_utc"] = account_snapshot["captured_at_utc"]
    saved = journal.create_intent(intent)
    created_now = existing_before is None
    return {
        "status": "prepared_not_authorized" if saved["status"] == "prepared"
                  else "existing_intent_not_prepared",
        "intent_created": created_now,
        "client_order_id": saved["client_order_id"],
        "intent_status": saved["status"],
        "intent_reused": not created_now,
        "submission_attempts": saved["submission_attempts"],
        "preflight_report": report,
        "account_risk_snapshot_id": account_snapshot["snapshot_id"],
        "account_risk_snapshot_age_seconds": account_snapshot["age_seconds"],
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "execution": "none",
    }
