"""Authenticated, read-only Polymarket account order monitor.

Consumes account order events and REST open-order snapshots using the official
polymarket-client SDK. It never creates, signs, posts, or cancels orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket import AsyncSecureClient
from polymarket.streams import UserSpec

from order_journal import OrderJournal


class UserEventGapError(RuntimeError):
    """Raised when the SDK reports dropped user-stream events."""


class OrderSnapshotIncompleteError(RuntimeError):
    """Raised when the SDK cannot confirm that all open-order pages were read."""


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
    """Map a known SDK order state to the local lifecycle; unknown states stay unresolved."""
    normalized = status.strip().upper()
    if filled == original:
        return "filled"
    if event_kind == "CANCELLATION" or normalized.startswith(("CANCELED", "CANCELLED")):
        return "cancelled"
    if normalized == "INVALID":
        return "rejected" if filled == 0 else None
    if filled > 0:
        # Cumulative size_matched is definitive fill information even when a newer
        # venue status string is not recognized by this version of the SDK.
        return "partially_filled"
    if normalized in {"LIVE", "UNMATCHED", "DELAYED", "MATCHED"}:
        return "open"
    return None


class OrderUserEventProcessor:
    """Translate SDK user order messages and REST snapshots to journal transitions."""

    def __init__(self, journal: OrderJournal) -> None:
        self.journal = journal

    def process(self, event: Any) -> dict[str, Any]:
        event_type = getattr(event, "type", None)
        if event_type == "trade":
            # User-order messages carry cumulative size_matched. Trade events are
            # not added to fills, which would double-count the same match.
            return {"result": "ignored_trade_event"}
        if event_type != "order":
            return {"result": "ignored_unknown_event"}
        payload = getattr(event, "payload", None)
        if payload is None:
            return {"result": "ignored_malformed_order_event"}
        event_kind = str(getattr(payload, "order_event_type", "") or "").upper()
        return self.process_order_payload(payload, event_kind=event_kind)

    def process_open_order_snapshot(self, order: Any) -> dict[str, Any]:
        """Apply one authoritative open-order REST record; never infer missing orders."""
        return self.process_order_payload(order, event_kind="")

    def process_order_payload(self, payload: Any, *, event_kind: str) -> dict[str, Any]:
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


async def reconcile_open_orders_snapshot(
    client: Any,
    journal: OrderJournal,
) -> dict[str, Any]:
    """Refresh known local orders from every page of the authenticated open-order list.

    Orders absent from the open-order snapshot are deliberately not marked filled
    or cancelled: absence alone is not evidence of their terminal outcome. They
    are returned in unresolved_missing_orders for a separate trade/history review.
    """
    processor = OrderUserEventProcessor(journal)
    seen_exchange_ids: set[str] = set()
    counts = {"venue_open_orders": 0, "updated": 0, "duplicate": 0,
              "manual_review": 0, "untracked": 0, "malformed": 0}

    async for page in client.list_open_orders():
        for order in page.items:
            exchange_order_id = str(getattr(order, "id", "") or "").strip()
            if exchange_order_id:
                seen_exchange_ids.add(exchange_order_id)
            counts["venue_open_orders"] += 1
            result = processor.process_open_order_snapshot(order)["result"]
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

        if getattr(page, "limit_reached", False):
            raise OrderSnapshotIncompleteError(
                "SDK open-order paginator reached its depth limit; do not treat this snapshot as complete"
            )

    initially_missing = [
        intent for intent in journal.list_active_intents()
        if intent.get("exchange_order_id")
        and intent["exchange_order_id"] not in seen_exchange_ids
    ]

    # Query account trade history by asset, then correlate each trade back to
    # the local exchange order ID. Collect every page before applying any
    # history-derived transition so a truncated/failed history read cannot
    # partially settle an absent order.
    trades_by_asset: dict[str, list[Any]] = {}
    trade_records_inspected = 0
    for asset_id in sorted({str(intent["token_id"]) for intent in initially_missing}):
        unique_trades: dict[str, Any] = {}
        async for page in client.list_account_trades(asset_id=asset_id):
            if getattr(page, "limit_reached", False):
                raise OrderSnapshotIncompleteError(
                    "SDK account-trade paginator reached its depth limit; trade history is incomplete"
                )
            for trade in page.items:
                trade_id = str(getattr(trade, "id", "") or "").strip()
                if not trade_id:
                    raise OrderSnapshotIncompleteError(
                        "Account trade history contains a record without a stable trade ID"
                    )
                existing_trade = unique_trades.get(trade_id)
                if existing_trade is not None and existing_trade != trade:
                    raise OrderSnapshotIncompleteError(
                        "Account trade history repeated a trade ID with conflicting contents"
                    )
                unique_trades[trade_id] = trade
        trades_by_asset[asset_id] = list(unique_trades.values())
        trade_records_inspected += len(unique_trades)

    history_results: dict[str, dict[str, Any]] = {}
    for intent in initially_missing:
        local_id = intent["client_order_id"]
        exchange_id = intent["exchange_order_id"]
        asset_id = str(intent["token_id"])
        order_side = str(intent["side"]).upper()
        matched_total = Decimal("0")
        mismatch: tuple[str, str] | None = None

        for trade in trades_by_asset.get(asset_id, []):
            if str(getattr(trade, "status", "")).upper() == "FAILED":
                continue
            trade_asset = str(getattr(trade, "asset_id", "") or "")
            matched_amounts: list[tuple[Decimal, str, str]] = []
            if str(getattr(trade, "taker_order_id", "") or "") == exchange_id:
                matched_amounts.append((
                    _decimal(getattr(trade, "size", None), "trade.size"),
                    trade_asset,
                    str(getattr(trade, "side", "") or "").upper(),
                ))
            for maker_order in getattr(trade, "maker_orders", ()) or ():
                if str(getattr(maker_order, "order_id", "") or "") == exchange_id:
                    matched_amounts.append((
                        _decimal(getattr(maker_order, "matched_amount", None), "maker_order.matched_amount"),
                        str(getattr(maker_order, "asset_id", "") or ""),
                        str(getattr(maker_order, "side", "") or "").upper(),
                    ))

            if len(matched_amounts) > 1:
                mismatch = ("multiple_order_matches_in_trade", trade_asset)
                break
            if not matched_amounts:
                continue
            amount, matched_asset, matched_side = matched_amounts[0]
            if matched_asset != asset_id or matched_side != order_side:
                mismatch = (matched_side or "unknown_side", matched_asset or "unknown_asset")
                break
            matched_total += amount

        if mismatch is not None:
            # The venue links a trade to this order ID but its asset or side
            # conflicts with the durable intent. Escalate; do not count the fill.
            current_fill = str(intent["filled_size"])
            safe_outcome = "open" if Decimal(current_fill) == 0 else "partially_filled"
            reviewed = journal.record_order_observation(
                local_id,
                outcome=safe_outcome,
                matched_client_order_id=local_id,
                exchange_order_id=exchange_id,
                cumulative_filled_size=current_fill,
                observed_token_id=mismatch[1] if mismatch[0] != "multiple_order_matches_in_trade" else asset_id,
                observed_side=mismatch[0] if mismatch[0] != "multiple_order_matches_in_trade" else order_side,
            )
            history_results[local_id] = {
                "result": "manual_review",
                "reason": reviewed.get("last_reason_code"),
            }
            continue

        original_size = _decimal(intent["size"], "order_size")
        current_fill = _decimal(intent["filled_size"], "filled_size")
        if matched_total > original_size:
            history_results[local_id] = {
                "result": "unresolved",
                "reason": "trade_history_exceeds_original_size",
                "trade_history_filled_size": str(matched_total),
            }
            continue
        if matched_total < current_fill:
            history_results[local_id] = {
                "result": "unresolved",
                "reason": "trade_history_below_journal_fill",
                "trade_history_filled_size": str(matched_total),
            }
            continue

        if matched_total == original_size:
            updated = journal.record_order_observation(
                local_id,
                outcome="filled",
                matched_client_order_id=local_id,
                exchange_order_id=exchange_id,
                cumulative_filled_size=str(matched_total),
                reason_code="reconciled_from_account_trade_history",
            )
            history_results[local_id] = {
                "result": "filled",
                "status": updated["status"],
                "filled_size": updated["filled_size"],
            }
        elif matched_total > current_fill:
            updated = journal.record_order_observation(
                local_id,
                outcome="partially_filled",
                matched_client_order_id=local_id,
                exchange_order_id=exchange_id,
                cumulative_filled_size=str(matched_total),
                reason_code="partial_fill_from_account_trade_history",
            )
            history_results[local_id] = {
                "result": "partial_fill_confirmed_terminal_unknown",
                "status": updated["status"],
                "filled_size": updated["filled_size"],
            }
        else:
            history_results[local_id] = {
                "result": "terminal_status_unconfirmed",
                "trade_history_filled_size": str(matched_total),
            }

    unresolved = []
    for intent in journal.list_active_intents():
        exchange_id = intent.get("exchange_order_id")
        if not exchange_id or exchange_id in seen_exchange_ids:
            continue
        history_result = history_results.get(intent["client_order_id"], {})
        unresolved.append({
            "client_order_id": intent["client_order_id"],
            "exchange_order_id": exchange_id,
            "status": intent["status"],
            "filled_size": intent["filled_size"],
            "terminal_status_confirmed": False,
            "trade_history_result": history_result.get("result", "not_checked"),
            "reason": history_result.get("reason", "order_absent_from_open_and_trade_history"),
        })

    return {
        "snapshot_complete": True,
        "status": "open_orders_refreshed" if not unresolved else "snapshot_has_unresolved_absences",
        **counts,
        "account_trade_history_checked": True,
        "trade_records_inspected": trade_records_inspected,
        "unresolved_missing_orders": unresolved,
        "execution": "none",
    }


async def consume_user_order_stream(
    client: Any,
    journal: OrderJournal,
    *,
    max_events: int | None = None,
    initial_snapshot: bool = True,
    snapshot_interval_seconds: float | None = 300.0,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Subscribe first, refresh open orders, then consume the authenticated user stream.

    Subscribing before the REST snapshot narrows the startup race: stream events
    arriving while pagination runs remain queued and are processed afterward.
    SDK queue drops abort consumption. REST snapshots can refresh currently open
    orders, but an absent order remains unresolved until its terminal history is
    independently confirmed.
    """
    if max_events is not None and (
        isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1
    ):
        raise ValueError("max_events must be a positive integer or None")
    if not isinstance(initial_snapshot, bool):
        raise ValueError("initial_snapshot must be a bool")
    if snapshot_interval_seconds is not None:
        if (
            isinstance(snapshot_interval_seconds, bool)
            or not isinstance(snapshot_interval_seconds, (int, float))
            or not math.isfinite(float(snapshot_interval_seconds))
            or float(snapshot_interval_seconds) <= 0
        ):
            raise ValueError("snapshot_interval_seconds must be positive and finite or None")

    log = logger or logging.getLogger(__name__)
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

        snapshot = None
        if initial_snapshot:
            snapshot = await reconcile_open_orders_snapshot(client, journal)
            log.info(
                "initial open-order snapshot status=%s orders=%s unresolved=%s",
                snapshot["status"],
                snapshot["venue_open_orders"],
                len(snapshot["unresolved_missing_orders"]),
            )
            current_dropped = getattr(handle, "dropped", None)
            if isinstance(current_dropped, bool) or not isinstance(current_dropped, int):
                raise UserEventGapError("SDK user-stream dropped-event counter became invalid")
            if current_dropped != previous_dropped:
                raise UserEventGapError("SDK dropped user events during the REST snapshot; reconciliation required")
            previous_dropped = current_dropped

        periodic_snapshots: list[dict[str, Any]] = []
        iterator = handle.__aiter__()
        while True:
            try:
                if snapshot_interval_seconds is None:
                    event = await iterator.__anext__()
                else:
                    event = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=float(snapshot_interval_seconds),
                    )
            except asyncio.TimeoutError:
                # A quiet stream still gets periodically refreshed from REST.
                # The subscription remains live, so events queue while the
                # paginated snapshot is fetched; queue growth is checked below.
                periodic = await reconcile_open_orders_snapshot(client, journal)
                periodic_snapshots.append(periodic)
                log.info(
                    "periodic open-order snapshot status=%s orders=%s unresolved=%s",
                    periodic["status"],
                    periodic["venue_open_orders"],
                    len(periodic["unresolved_missing_orders"]),
                )
                current_dropped = getattr(handle, "dropped", None)
                if isinstance(current_dropped, bool) or not isinstance(current_dropped, int):
                    raise UserEventGapError("SDK user-stream dropped-event counter became invalid")
                if current_dropped != previous_dropped:
                    raise UserEventGapError("SDK dropped user events during REST snapshot; reconciliation required")
                previous_dropped = current_dropped
                continue
            except StopAsyncIteration:
                break

            current_dropped = getattr(handle, "dropped", None)
            if isinstance(current_dropped, bool) or not isinstance(current_dropped, int):
                raise UserEventGapError("SDK user-stream dropped-event counter became invalid")
            if current_dropped != previous_dropped:
                raise UserEventGapError("SDK dropped one or more user events; reconciliation required")
            previous_dropped = current_dropped

            counts["received"] += 1
            result = processor.process(event)["result"]
            if result in {"manual_review", "rejected_malformed_order_observation"}:
                log.warning("user order event requires attention: result=%s", result)
            elif result == "updated":
                log.debug("user order event persisted")
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
        return {
            **counts,
            "initial_open_orders_snapshot": snapshot,
            "periodic_open_orders_snapshots": periodic_snapshots,
        }
    finally:
        await handle.close()


async def monitor_user_orders_from_env(
    journal: OrderJournal,
    *,
    private_key: str | None = None,
    wallet: str | None = None,
    max_events: int | None = None,
    snapshot_interval_seconds: float | None = 300.0,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run a read-only account monitor using secrets supplied by the environment.

    POLYMARKET_PRIVATE_KEY and optional POLYMARKET_DEPOSIT_WALLET should come
    from a deployment secret manager. Never paste them into source code, logs,
    commits, or chat. The SDK client is used only to read open orders and consume
    the authenticated UserSpec stream.
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
            snapshot_interval_seconds=snapshot_interval_seconds,
            logger=logger,
        )

def main() -> None:
    """CLI entry point for a separately supervised, read-only account monitor."""
    parser = argparse.ArgumentParser(description="Read-only Polymarket account order monitor")
    parser.add_argument("--database", default=os.environ.get("ORDER_JOURNAL_DB"),
                        help="SQLite journal path (default: DATA_DIR/order_journal.sqlite3)")
    parser.add_argument(
        "--snapshot-interval-seconds",
        type=float,
        default=float(os.environ.get("USER_ORDER_SNAPSHOT_INTERVAL_SECONDS", "300")),
        help="REST open-order refresh interval in seconds (default: 300)",
    )
    args = parser.parse_args()
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    database_path = Path(args.database) if args.database else data_dir / "order_journal.sqlite3"
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger("polymarket.user_order_monitor")
    journal = OrderJournal(database_path)
    logger.info("starting read-only Polymarket user-order monitor")
    try:
        summary = asyncio.run(
            monitor_user_orders_from_env(
                journal,
                snapshot_interval_seconds=args.snapshot_interval_seconds,
                logger=logger,
            )
        )
    except KeyboardInterrupt:
        logger.info("user-order monitor stopped by operator")
        return
    except UserEventGapError:
        logger.exception("user stream lost events; process exiting for supervised restart")
        raise
    logger.info("user-order monitor stopped; summary=%s", json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
