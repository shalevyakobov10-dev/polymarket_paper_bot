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
    preflight_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate every preflight gate, then persist a prepared intent only.

    preflight_context must contain the live_order_preflight inputs other than
    token_id, side, limit_price, and size. If any order term is duplicated in
    that mapping, it must match the explicit argument. No network client or
    account secret is accepted by this function.
    """
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
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "execution": "none",
    }
