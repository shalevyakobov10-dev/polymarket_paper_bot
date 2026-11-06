"""Fail-closed local order lifecycle helpers for a future execution adapter.

This module does not connect to Polymarket, sign, submit, cancel, or retry orders.
A future adapter must durably persist each returned state before making a network
request. An ambiguous submission is never safe to resend merely because a lookup
temporarily reports that the order is absent.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


SCHEMA_VERSION = 1
ORDER_STATES = frozenset({
    "prepared",
    "submission_pending",
    "submission_unknown",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "manual_review",
})
_RETRY_NEVER = False
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be a finite number") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _reason_code(value: Any, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise ValueError("reason_code must contain only safe identifier characters")
    return value


def create_order_intent(
    *,
    client_order_id: str,
    token_id: str,
    side: str,
    limit_price: float,
    size: float,
) -> dict[str, Any]:
    """Create a validated, local-only intent with a caller-supplied stable ID.

    The ID must be durably stored and reused for reconciliation. It is not a
    claim that the Polymarket API accepts this value as a native idempotency key.
    """
    client_id = _required_text(client_order_id, "client_order_id")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise ValueError("client_order_id must be 1-128 ASCII letters, digits, '_' or '-'")
    token = _required_text(token_id, "token_id")
    normalized_side = _required_text(side, "side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    price = _decimal(limit_price, "limit_price")
    quantity = _decimal(size, "size")
    if not Decimal("0") < price < Decimal("1"):
        raise ValueError("limit_price must be strictly between 0 and 1")
    if quantity <= 0:
        raise ValueError("size must be greater than zero")

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "LOCAL_ORDER_LIFECYCLE_ONLY",
        "client_order_id": client_id,
        "token_id": token,
        "side": normalized_side,
        "limit_price": str(price),
        "size": str(quantity),
        "status": "prepared",
        "submission_attempts": 0,
        "reconciliation_attempts": 0,
        "exchange_order_id": None,
        "last_reason_code": None,
        "filled_size": "0",
        "automatic_retry_permitted": False,
        "execution": "none",
    }


def _validate_venue_response(state: dict[str, Any]) -> None:
    """Validate sanitized SDK response evidence retained in the immutable audit state."""
    receipt = state.get("venue_response")
    if receipt is None:
        return
    if not isinstance(receipt, dict):
        raise ValueError("venue_response must be an object")
    kind = receipt.get("kind")
    if kind == "accepted":
        allowed = {
            "kind", "order_id", "status", "making_amount", "taking_amount",
            "trade_ids", "transactions_hashes",
        }
        if set(receipt) - allowed:
            raise ValueError("venue_response contains unsupported accepted fields")
        order_id = _required_text(receipt.get("order_id"), "venue_response.order_id")
        if order_id != state.get("exchange_order_id"):
            raise ValueError("venue_response order ID conflicts with the lifecycle state")
        if state.get("status") not in {
            "open", "partially_filled", "filled", "cancelled", "manual_review"
        }:
            raise ValueError("accepted venue_response conflicts with the lifecycle status")
        if receipt.get("status") not in {"live", "matched", "delayed"}:
            raise ValueError("venue_response has an unsupported accepted status")
        for field in ("making_amount", "taking_amount"):
            amount = receipt.get(field)
            if amount is not None and _decimal(amount, f"venue_response.{field}") < 0:
                raise ValueError("venue_response amounts cannot be negative")
        for field in ("trade_ids", "transactions_hashes"):
            values = receipt.get(field)
            if not isinstance(values, list) or len(values) > 100:
                raise ValueError(f"venue_response.{field} must be a bounded list")
            if any(
                not isinstance(value, str) or not value.strip() or len(value) > 256
                for value in values
            ):
                raise ValueError(f"venue_response.{field} contains an invalid identifier")
    elif kind == "rejected":
        if set(receipt) != {"kind", "code"}:
            raise ValueError("venue_response contains unsupported rejected fields")
        if state.get("status") != "rejected":
            raise ValueError("rejected venue_response conflicts with the lifecycle status")
        code = receipt.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            raise ValueError("venue_response rejection code is invalid")
    else:
        raise ValueError("venue_response kind is unsupported")


def _validated_copy(intent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise ValueError("order intent must be a dictionary")
    state = deepcopy(intent)
    _validate_venue_response(state)
    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported order intent schema_version")
    if state.get("mode") != "LOCAL_ORDER_LIFECYCLE_ONLY":
        raise ValueError("unsupported order lifecycle mode")
    if not isinstance(state.get("status"), str) or state["status"] not in ORDER_STATES:
        raise ValueError("invalid order lifecycle status")
    client_id = _required_text(state.get("client_order_id"), "client_order_id")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise ValueError("invalid stored client_order_id")
    _required_text(state.get("token_id"), "token_id")
    if not isinstance(state.get("side"), str) or state["side"] not in {"BUY", "SELL"}:
        raise ValueError("invalid order side")
    price = _decimal(state.get("limit_price"), "limit_price")
    size = _decimal(state.get("size"), "size")
    filled = _decimal(state.get("filled_size", "0"), "filled_size")
    if not Decimal("0") < price < Decimal("1") or size <= 0:
        raise ValueError("invalid stored order price or size")
    if filled < 0 or filled > size:
        raise ValueError("filled_size is outside the order size")
    attempts = state.get("submission_attempts")
    reconciliations = state.get("reconciliation_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts not in {0, 1}:
        raise ValueError("submission_attempts must be 0 or 1")
    if isinstance(reconciliations, bool) or not isinstance(reconciliations, int) or reconciliations < 0:
        raise ValueError("reconciliation_attempts must be a nonnegative integer")
    expected_attempts = 0 if state["status"] == "prepared" else 1
    if attempts != expected_attempts:
        raise ValueError("submission status and attempt count are inconsistent")
    status = state["status"]
    external_id = state.get("exchange_order_id")
    if external_id is not None:
        _required_text(external_id, "exchange_order_id")
    reason = state.get("last_reason_code")
    if reason is not None:
        _reason_code(reason, default="invalid_reason")
    if status in {"open", "partially_filled", "filled", "cancelled", "manual_review"}:
        if external_id is None:
            raise ValueError("resolved venue order state requires exchange_order_id")
    if status in {"prepared", "submission_pending", "submission_unknown", "open", "rejected"}:
        if filled != 0:
            raise ValueError("this order state must have zero filled_size")
    if status == "partially_filled" and not Decimal("0") < filled < size:
        raise ValueError("partially_filled state has inconsistent filled_size")
    if status == "filled" and filled != size:
        raise ValueError("filled state must have filled_size equal to order size")
    if status == "cancelled" and filled >= size:
        raise ValueError("cancelled state must retain an unfilled remainder")
    if status == "manual_review" and reason not in {
        "client_order_id_mismatch", "exchange_order_id_mismatch", "order_terms_mismatch"
    }:
        raise ValueError("manual_review state must retain its mismatch reason")
    conflicting_id = state.get("last_conflicting_exchange_order_id")
    if conflicting_id is not None:
        _required_text(conflicting_id, "last_conflicting_exchange_order_id")
        if status != "manual_review":
            raise ValueError("conflicting venue order ID requires manual_review")
    if status == "prepared" and (external_id is not None or reconciliations != 0):
        raise ValueError("prepared intent has inconsistent venue or reconciliation metadata")
    if status == "submission_pending" and (external_id is not None or reconciliations != 0):
        raise ValueError("pending submission has inconsistent reconciliation metadata")
    if status == "submission_unknown" and external_id is not None:
        raise ValueError("unknown submission cannot have a confirmed exchange_order_id")
    if state.get("automatic_retry_permitted") is not False:
        raise ValueError("automatic retries must remain disabled")
    if state.get("execution") != "none":
        raise ValueError("this module must remain non-executing")
    _validate_venue_response(state)
    return state


def submission_policy(intent: dict[str, Any]) -> dict[str, Any]:
    """Describe local transition eligibility; never authorizes a real order."""
    state = _validated_copy(intent)
    prepared = state["status"] == "prepared" and state["submission_attempts"] == 0
    unknown = state["status"] in {"submission_pending", "submission_unknown"}
    return {
        "first_attempt_transition_allowed": prepared,
        "automatic_retry_permitted": _RETRY_NEVER,
        "reconciliation_required": unknown,
        "status": state["status"],
        "execution": "none",
    }


def begin_submission(intent: dict[str, Any]) -> dict[str, Any]:
    """Record the one permitted initial attempt before the caller sends anything.

    The caller must persist this returned state durably before network I/O.
    Calling this function again on the returned state raises, preventing a
    second attempt after either a response or a process restart.
    """
    state = _validated_copy(intent)
    if state["status"] != "prepared" or state["submission_attempts"] != 0:
        raise RuntimeError("duplicate or unsafe submission attempt blocked")
    state["status"] = "submission_pending"
    state["submission_attempts"] = 1
    return state


def record_submission_response(
    intent: dict[str, Any],
    *,
    outcome: str,
    exchange_order_id: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Record an unambiguous acknowledgement/rejection or an ambiguous result."""
    state = _validated_copy(intent)
    if state["status"] != "submission_pending":
        raise RuntimeError("submission response is valid only for a pending attempt")
    if outcome == "accepted":
        external_id = _required_text(exchange_order_id, "exchange_order_id")
        state["status"] = "open"
        state["exchange_order_id"] = external_id
        state["last_reason_code"] = None
    elif outcome == "rejected":
        if exchange_order_id is not None:
            raise ValueError("a rejected response must not include exchange_order_id")
        state["status"] = "rejected"
        state["last_reason_code"] = _reason_code(
            reason_code, default="explicit_venue_rejection"
        )
    elif outcome == "ambiguous":
        if exchange_order_id is not None:
            raise ValueError("an ambiguous response cannot assert exchange_order_id")
        state["status"] = "submission_unknown"
        state["last_reason_code"] = _reason_code(
            reason_code, default="transport_outcome_unknown"
        )
    else:
        raise ValueError("outcome must be accepted, rejected, or ambiguous")
    return state


def reconcile_submission(
    intent: dict[str, Any],
    *,
    outcome: str,
    matched_client_order_id: str | None = None,
    exchange_order_id: str | None = None,
    filled_size: float | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """Reconcile an ambiguous attempt without ever authorizing a resend.

    A not-found result is deliberately inconclusive: venue indexing and
    propagation can lag. Only a result explicitly matched to the original
    client_order_id may move the intent out of submission_unknown.
    """
    state = _validated_copy(intent)
    if state["status"] != "submission_unknown":
        raise RuntimeError("only an ambiguous submission can be reconciled")

    if outcome in {"not_found", "lookup_error"}:
        state["reconciliation_attempts"] += 1
        state["last_reason_code"] = _reason_code(
            reason_code,
            default=("order_not_visible_yet" if outcome == "not_found" else "order_lookup_failed"),
        )
        return state

    target_status = {
        "found_open": "open",
        "found_partially_filled": "partially_filled",
        "found_filled": "filled",
        "found_cancelled": "cancelled",
        "found_rejected": "rejected",
    }.get(outcome)
    if target_status is None:
        raise ValueError("unsupported reconciliation outcome")

    found_id = _required_text(exchange_order_id, "exchange_order_id")
    matched_id = _required_text(matched_client_order_id, "matched_client_order_id")
    if matched_id != state["client_order_id"]:
        state["status"] = "manual_review"
        state["exchange_order_id"] = found_id
        state["last_reason_code"] = "client_order_id_mismatch"
        state["reconciliation_attempts"] += 1
        return state

    order_size = _decimal(state["size"], "size")
    reported_fill = _decimal(
        filled_size if filled_size is not None else (
            state["size"] if target_status == "filled" else "0"
        ),
        "filled_size",
    )
    if reported_fill < 0 or reported_fill > order_size:
        raise ValueError("reconciled filled_size is outside the order size")
    if target_status == "partially_filled" and not Decimal("0") < reported_fill < order_size:
        raise ValueError("partially_filled requires a fill strictly between zero and order size")
    if target_status == "filled" and reported_fill != order_size:
        raise ValueError("found_filled requires filled_size equal to order size")
    if target_status == "open" and reported_fill != 0:
        raise ValueError("found_open requires zero filled_size; use found_partially_filled")
    if target_status == "rejected" and reported_fill != 0:
        raise ValueError("found_rejected requires zero filled_size")

    state["status"] = target_status
    state["exchange_order_id"] = found_id
    state["filled_size"] = str(reported_fill)
    state["reconciliation_attempts"] += 1
    state["last_reason_code"] = _reason_code(reason_code, default="reconciled_from_venue")
    return state



def record_order_observation(
    intent: dict[str, Any],
    *,
    outcome: str,
    matched_client_order_id: str,
    exchange_order_id: str,
    cumulative_filled_size: float,
    reason_code: str | None = None,
    observed_token_id: str | None = None,
    observed_side: str | None = None,
    observed_original_size: float | None = None,
    observed_price: float | None = None,
) -> dict[str, Any]:
    """Apply a venue status snapshot using monotonic cumulative fill quantity.

    Repeated snapshots are idempotent because cumulative filled size is stored,
    never added to the previous fill. Older snapshots that report less filled
    quantity are rejected. An identity mismatch moves the intent to manual
    review rather than attaching activity to the wrong local order.
    """
    state = _validated_copy(intent)
    terminal_snapshot = state["status"] in {"filled", "cancelled", "rejected"}
    if state["status"] not in {
        "open", "partially_filled", "filled", "cancelled", "rejected"
    }:
        raise RuntimeError("only known venue orders can accept status observations")

    matched_id = _required_text(matched_client_order_id, "matched_client_order_id")
    observed_exchange_id = _required_text(exchange_order_id, "exchange_order_id")
    if matched_id != state["client_order_id"]:
        state["status"] = "manual_review"
        state["last_reason_code"] = "client_order_id_mismatch"
        state["last_conflicting_exchange_order_id"] = observed_exchange_id
        return state
    if observed_exchange_id != state["exchange_order_id"]:
        state["status"] = "manual_review"
        state["last_reason_code"] = "exchange_order_id_mismatch"
        state["last_conflicting_exchange_order_id"] = observed_exchange_id
        return state

    mismatches: list[str] = []
    if observed_token_id is not None:
        if _required_text(observed_token_id, "observed_token_id") != state["token_id"]:
            mismatches.append("token_id")
    if observed_side is not None:
        venue_side = _required_text(observed_side, "observed_side").upper()
        if venue_side not in {"BUY", "SELL"} or venue_side != state["side"]:
            mismatches.append("side")
    if observed_original_size is not None:
        if _decimal(observed_original_size, "observed_original_size") != _decimal(state["size"], "size"):
            mismatches.append("original_size")
    if observed_price is not None:
        if _decimal(observed_price, "observed_price") != _decimal(state["limit_price"], "limit_price"):
            mismatches.append("price")
    if mismatches:
        state["status"] = "manual_review"
        state["last_reason_code"] = "order_terms_mismatch"
        state["last_conflicting_exchange_order_id"] = observed_exchange_id
        state["last_mismatch_fields"] = mismatches
        return state

    if outcome not in {"open", "partially_filled", "filled", "cancelled", "rejected"}:
        raise ValueError("unsupported order observation outcome")

    order_size = _decimal(state["size"], "size")
    previous_fill = _decimal(state["filled_size"], "filled_size")
    reported_fill = _decimal(cumulative_filled_size, "cumulative_filled_size")
    if reported_fill < 0 or reported_fill > order_size:
        raise ValueError("cumulative_filled_size is outside the order size")
    if reported_fill < previous_fill:
        raise ValueError("cumulative filled size cannot decrease")

    if outcome == "filled" and reported_fill != order_size:
        raise ValueError("filled observation requires cumulative fill equal to order size")
    if outcome == "partially_filled" and not Decimal("0") < reported_fill < order_size:
        raise ValueError("partially_filled observation requires a partial cumulative fill")
    if outcome == "rejected" and reported_fill != 0:
        raise ValueError("rejected observation requires zero cumulative fill")
    if outcome == "rejected" and previous_fill != 0:
        raise ValueError("an order with prior fills cannot become rejected")
    if outcome == "cancelled" and reported_fill >= order_size:
        raise ValueError("cancelled observation must retain an unfilled remainder")

    # Normalize an 'open' snapshot carrying a nonzero cumulative matched size
    # to partially_filled (or filled), rather than erasing a prior fill.
    if outcome == "open":
        target_status = (
            "open" if reported_fill == 0
            else ("filled" if reported_fill == order_size else "partially_filled")
        )
    elif outcome == "partially_filled":
        target_status = "partially_filled"
    else:
        target_status = outcome

    if terminal_snapshot:
        if target_status == state["status"] and reported_fill == previous_fill:
            return state
        raise RuntimeError("terminal order cannot accept conflicting status observations")

    state["status"] = target_status
    state["filled_size"] = str(reported_fill)
    state["last_reason_code"] = _reason_code(
        reason_code, default="venue_order_status_observed"
    )
    return state
