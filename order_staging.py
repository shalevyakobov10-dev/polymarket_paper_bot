"""Fail-closed bridge from current account risk to a durable prepared intent.

A fresh, complete authenticated account-risk snapshot is mandatory. Account
collateral, open positions, exposure, and same-market outcome conflicts are
derived from that persisted snapshot, never trusted from caller-supplied
preflight values. This module cannot sign, submit, retry, or cancel orders.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from live_order_preflight import evaluate_live_order_preflight
from order_journal import OrderJournal
from order_lifecycle import create_order_intent


MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS = 300.0


def _blocked(*blockers: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blockers": list(blockers),
        "intent_created": False,
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "execution": "none",
        **extra,
    }


def _assets_for_condition(snapshot: dict[str, Any], field: str, condition_id: str) -> set[str]:
    mapping = snapshot.get(field)
    if not isinstance(mapping, dict):
        raise ValueError(f"invalid_account_risk_snapshot:{field}")
    values = mapping.get(condition_id, [])
    if not isinstance(values, (list, tuple, set)):
        raise ValueError(f"invalid_account_risk_snapshot:{field}:{condition_id}")
    assets: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"invalid_account_risk_snapshot_asset:{field}")
        assets.add(item.strip())
    return assets


def stage_buy_order_intent(
    journal: OrderJournal,
    *,
    client_order_id: str,
    token_id: str,
    market_condition_id: str,
    side: str,
    limit_price: float,
    size: float,
    preflight_context: Mapping[str, Any],
    max_account_snapshot_age_seconds: float = MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS,
) -> dict[str, Any]:
    """Evaluate all gates and stage a prepared intent against a fresh account snapshot.

    preflight_context provides market/book/fee/strategy and release-gate inputs.
    Account collateral, open positions, exposure, unrealized PnL, and opposing
    outcome exposure are always taken from the latest persisted complete snapshot.
    """
    if not isinstance(preflight_context, Mapping):
        return _blocked("invalid_preflight_context")
    if not isinstance(market_condition_id, str) or not market_condition_id.strip():
        return _blocked("missing_market_condition_id")
    if not isinstance(token_id, str) or not token_id.strip():
        return _blocked("missing_token_id")
    if (
        isinstance(max_account_snapshot_age_seconds, bool)
        or not isinstance(max_account_snapshot_age_seconds, (int, float))
        or not math.isfinite(float(max_account_snapshot_age_seconds))
        or not 0 < float(max_account_snapshot_age_seconds)
        <= MAX_ACCOUNT_RISK_SNAPSHOT_AGE_SECONDS
    ):
        return _blocked("account_risk_snapshot_freshness_window_out_of_policy")

    try:
        snapshot = journal.get_latest_account_risk_snapshot(
            max_age_seconds=max_account_snapshot_age_seconds
        )
    except (TypeError, ValueError) as exc:
        return _blocked("invalid_account_risk_snapshot_freshness_policy", str(exc)[:180])
    if snapshot is None:
        return _blocked("account_risk_snapshot_missing")
    if snapshot.get("fresh") is not True or snapshot.get("snapshot_complete") is not True:
        return _blocked("account_risk_snapshot_stale_or_incomplete")

    snapshot_id = snapshot.get("snapshot_id")
    captured_at = snapshot.get("captured_at_utc")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        return _blocked("account_risk_snapshot_missing_id")
    if not isinstance(captured_at, str) or not captured_at.strip():
        return _blocked("account_risk_snapshot_missing_timestamp")
    if snapshot.get("execution") != "none":
        return _blocked("account_risk_snapshot_not_read_only")

    try:
        position_assets = _assets_for_condition(
            snapshot, "open_position_assets_by_condition", market_condition_id
        )
        order_assets = _assets_for_condition(
            snapshot, "open_order_assets_by_condition", market_condition_id
        )
    except ValueError as exc:
        return _blocked(str(exc)[:180])

    same_market_assets = position_assets | order_assets
    opposing_outcome_open = any(asset != token_id for asset in same_market_assets)
    account_fields = {
        "available_collateral_usd": snapshot.get("conservative_available_collateral_usd"),
        "current_open_positions": snapshot.get("current_open_positions"),
        "current_open_exposure_usd": snapshot.get("current_open_exposure_usd"),
        "open_unrealized_pnl_usd": snapshot.get("open_unrealized_pnl_usd"),
        "opposing_market_outcome_open": opposing_outcome_open,
    }
    required_snapshot_fields = (
        "available_collateral_usd",
        "current_open_positions",
        "current_open_exposure_usd",
        "open_unrealized_pnl_usd",
    )
    missing = [name for name in required_snapshot_fields if account_fields[name] is None]
    if missing:
        return _blocked("account_risk_snapshot_missing_fields:" + ",".join(missing))

    try:
        normalized_side = side.upper() if isinstance(side, str) else side
        terms = {
            "token_id": token_id,
            "side": normalized_side,
            "limit_price": limit_price,
            "size": size,
        }
        context = dict(preflight_context)
        for name, value in terms.items():
            if name in context and str(context[name]) != str(value):
                return _blocked(f"preflight_order_term_mismatch:{name}")
            context[name] = value
        context.update(account_fields)
        report = evaluate_live_order_preflight(**context)
    except (TypeError, ValueError) as exc:
        return _blocked("invalid_preflight_context", str(exc)[:180])

    report["account_risk_snapshot"] = {
        "snapshot_id": snapshot_id,
        "captured_at_utc": captured_at,
        "age_seconds": snapshot.get("age_seconds"),
        "fresh": True,
        "market_condition_id": market_condition_id,
        "same_market_position_assets": sorted(position_assets),
        "same_market_open_order_assets": sorted(order_assets),
    }

    if (
        report.get("order_submission_implemented") is not False
        or report.get("order_submission_allowed") is not False
        or report.get("order_signing_allowed") is not False
        or report.get("execution") != "none"
    ):
        return _blocked("unsafe_preflight_execution_contract", preflight_report=report)

    if report.get("preflight_passed") is not True:
        blockers = report.get("blockers")
        if not isinstance(blockers, list):
            blockers = ["preflight_not_passed"]
        return _blocked(*blockers, preflight_report=report)

    try:
        intent = create_order_intent(
            client_order_id=client_order_id,
            token_id=token_id,
            side=normalized_side,
            limit_price=limit_price,
            size=size,
        )
    except (TypeError, ValueError) as exc:
        return _blocked("invalid_order_intent", str(exc)[:180], preflight_report=report)

    intent["market_condition_id"] = market_condition_id
    intent["account_risk_snapshot_id"] = snapshot_id
    intent["account_risk_snapshot_captured_at_utc"] = captured_at
    intent["preflight_report"] = report

    try:
        saved, created_now = journal.create_intent_with_status(intent)
    except (KeyError, TypeError, ValueError) as exc:
        return _blocked("order_intent_conflict_or_corrupt_journal", str(exc)[:180],
                        preflight_report=report)
    return {
        "status": "prepared_not_authorized" if saved["status"] == "prepared"
                  else "existing_intent_not_prepared",
        "intent_created": created_now,
        "client_order_id": saved["client_order_id"],
        "intent_status": saved["status"],
        "intent_reused": not created_now,
        "submission_attempts": saved["submission_attempts"],
        "account_risk_snapshot_id": saved.get("account_risk_snapshot_id"),
        "preflight_report": report,
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "execution": "none",
    }
