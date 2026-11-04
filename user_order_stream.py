"""Authenticated, read-only Polymarket user-order stream consumer.

This module consumes account order events via the official polymarket-client SDK.
It never creates, signs, posts, or cancels orders. It maps venue order IDs to local
intents and persists cumulative fills through the existing fail-closed journal.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket import AsyncSecureClient
from polymarket.streams import UserSpec

from order_journal import OrderJournal


class UserEventGapError(RuntimeError):
    """Raised when the SDK reports dropped user-stream events."""


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite nonnegative decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite nonnegative decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a finite nonnegative decimal")
    return parsed


def _event_outcome(*, event_kind: str, status: str, filled: Decimal, original: Decimal) -> str | None:
    """Map SDK order status to the local lifecycle; unknown statuses fail closed."""
    normalized = status.strip().upper()
    if filled == original:
        return "filled"
    if event_kind == "CANCELLATION" or normalized.startswith(("CANCELED", "CANCELLED")):
        return "cancelled"
    if normalized == "INVALID":
        return "rejected" if filled == 0 else None
    if filled > 0:
        return "partially_filled"
    if normalized in {"LIVE", "UNMATCHED", "DELAYED", "MATCHED"}:
        return "open"
    return None


class OrderUserEventProcessor:
    """Translate official SDK user events into durable local order observations."""

    def __init__(self, journal: OrderJournal) -> None:
        self.journal = journal

    def process(self, event: Any) -> dict[str, Any]:
        event_type = getattr(event, "type", None)
        if event_type == "trade":
            # User-order updates carry cumulative size_matched. Trade records are
            # deliberately not added to fills, which would double-count matches.
            return {"result": "ignored_trade_event"}
        if event_type != "order":
            return {"result": "ignored_unknown_event"}

        payload = getattr(event, "payload", None)
        if payload is None:
            return {"result": "ignored_malformed_order_event"}

        exchange_order_id = str(getattr(payload, "id", "") or "").strip()
        if not exchange_order_id:
            return {"result": "ignored_malformed_order_event"}

        local_order_id = self.journal.find_client_order_id_by_exchange_order_id(
            exchange_order_id
        )
        if local_order_id is None:
            return {
                "result": "untracked_order_event",
                "exchange_order_id": exchange_order_id,
            }

        current = self.journal.get_intent(local_order_id)
        if current is None:
            return {
                "result": "untracked_order_event",
                "exchange_order_id": exchange_order_id,
            }
        if current["status"] == "manual_review":
            return {
                "result": "manual_review_locked",
                "client_order_id": local_order_id,
                "exchange_order_id": exchange_order_id,
            }

        try:
            original_size = _decimal(getattr(payload, "original_size", None), "original_size")
            cumulative_fill = _decimal(getattr(payload, "size_matched", None), "size_matched")
            price = _decimal(getattr(payload, "price", None), "price")
            if original_size <= 0 or cumulative_fill > original_size:
                raise ValueError("order size or cumulative fill is outside valid bounds")
            event_kind = str(getattr(payload, "order_event_type", "") or "").upper()
            venue_status = str(getattr(payload, "status", "") or "").upper()
            outcome = _event_outcome(
                event_kind=event_kind,
                status=venue_status,
                filled=cumulative_fill,
                original=original_size,
            )
            if outcome is None:
                return {
                    "result": "ignored_unknown_venue_status",
                    "exchange_order_id": exchange_order_id,
                    "venue_status": venue_status,
                }

            updated = self.journal.record_order_observation(
                local_order_id,
                outcome=outcome,
                matched_client_order_id=local_order_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_size=str(cumulative_fill),
                observed_token_id=str(getattr(payload, "asset_id", "") or ""),
                observed_side=str(getattr(payload, "side", "") or ""),
                observed_original_size=str(original_size),
                observed_price=str(price),
            )
        except (TypeError, ValueError) as exc:
            return {
                "result": "rejected_malformed_order_observation",
                "exchange_order_id": exchange_order_id,
                "reason": str(exc)[:180],
            }

        if updated["status"] == "manual_review":
            result = "manual_review"
        elif updated == current:
            result = "duplicate_observation"
        else:
            result = "updated"
        return {
            "result": result,
            "client_order_id": local_order_id,
            "exchange_order_id": exchange_order_id,
            "status": updated["status"],
            "filled_size": updated["filled_size"],
            "reason": updated.get("last_reason_code"),
        }


async def consume_user_order_stream(
    client: Any,
    journal: OrderJournal,
    *,
    max_events: int | None = None,
) -> dict[str, int]:
    """Subscribe to the authenticated user stream and persist order updates.

    The SDK automatically reconnects its user stream. Its handle exposes a
    dropped-event counter; any increase makes the local event history incomplete,
    so this consumer stops with UserEventGapError rather than pretending the
    journal is current. A REST snapshot/reconciliation worker is still required
    to repair gaps and verify state after disconnects.
    """
    if max_events is not None and (
        isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1
    ):
        raise ValueError("max_events must be a positive integer or None")

    handle = await client.subscribe(UserSpec())
    processor = OrderUserEventProcessor(journal)
    counts = {
        "received": 0,
        "updated": 0,
        "duplicate": 0,
        "manual_review": 0,
        "untracked": 0,
        "ignored": 0,
        "malformed": 0,
    }
    previous_dropped = getattr(handle, "dropped", None)
    if isinstance(previous_dropped, bool) or not isinstance(previous_dropped, int):
        await handle.close()
        raise UserEventGapError("SDK user-stream handle has no valid dropped-event counter")
    try:
        if previous_dropped:
            raise UserEventGapError("SDK reported dropped user events before consumption began")
        async for event in handle:
            current_dropped = getattr(handle, "dropped", None)
            if isinstance(current_dropped, bool) or not isinstance(current_dropped, int):
                raise UserEventGapError("SDK user-stream dropped-event counter became invalid")
            if current_dropped != previous_dropped:
                raise UserEventGapError("SDK dropped one or more user events; reconciliation required")
            previous_dropped = current_dropped

            counts["received"] += 1
            result = processor.process(event)["result"]
            if result == "updated":
                counts["updated"] += 1
            elif result == "duplicate_observation":
                counts["duplicate"] += 1
            elif result == "manual_review":
                counts["manual_review"] += 1
            elif result == "untracked_order_event":
                counts["untracked"] += 1
            elif result == "rejected_malformed_order_observation":
                counts["malformed"] += 1
            else:
                counts["ignored"] += 1

            if max_events is not None and counts["received"] >= max_events:
                break

        current_dropped = getattr(handle, "dropped", None)
        if isinstance(current_dropped, bool) or not isinstance(current_dropped, int):
            raise UserEventGapError("SDK user-stream dropped-event counter became invalid")
        if current_dropped != previous_dropped:
            raise UserEventGapError("SDK dropped one or more user events; reconciliation required")
        return counts
    finally:
        await handle.close()


async def monitor_user_orders_from_env(
    journal: OrderJournal,
    *,
    private_key: str | None = None,
    wallet: str | None = None,
    max_events: int | None = None,
) -> dict[str, int]:
    """Run a read-only account stream using secrets supplied by the environment.

    POLYMARKET_PRIVATE_KEY and optional POLYMARKET_DEPOSIT_WALLET should come
    from a deployment secret manager. Never paste them into source code, logs,
    commits, or chat. The SDK client is used only for UserSpec subscription.
    """
    key = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required in the secure runtime environment")
    resolved_wallet = wallet or os.environ.get("POLYMARKET_DEPOSIT_WALLET") or None
    client = await AsyncSecureClient.create(
        private_key=key.strip(),
        wallet=resolved_wallet,
    )
    async with client:
        return await consume_user_order_stream(
            client,
            journal,
            max_events=max_events,
        )
