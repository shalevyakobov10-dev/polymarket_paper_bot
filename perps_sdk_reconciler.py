"""Read-only reconciliation bridge from Polymarket's Perps SDK into the local journal.

This module does not create SDK clients or sessions and has no signing, wallet,
submission, cancellation, or credential-loading path. A caller must provide an
already-open account session. Every remote observation is reconciled by the
same client_order_id persisted before the original submission.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from perps_order_journal import PerpsOrderJournal, RECONCILABLE_STATUSES


SDK_OPEN = {"accepted", "open"}
SDK_PARTIAL = {"partial", "partially_filled"}
SDK_FILLED = {"filled"}
SDK_CANCELLED = {
    "cancelled", "canceled", "auto_cancelled", "ioc_expired",
    "order_expired", "stp_cancelled", "parent_cancelled", "expired",
}
SDK_REJECTED = {
    "post_only_rejected", "fok_unfilled", "ioc_no_fill", "zero_quantity",
    "reduce_only_invalid", "reduce_only_expired",
    "sweep_cap_exceeded", "resting_order_limit_exceeded", "below_min_notional",
    "instrument_disabled", "instrument_close_only", "instrument_settled",
    "insufficient_margin_at_fill", "mark_price_unavailable",
}
# The SDK paginator is instrument-filtered, not order-filtered. Bound every
# scanned fill row as well as matching rows so old/high-volume accounts cannot
# cause an unbounded reconciliation cycle.
MAX_FILL_ROWS_PER_ORDER = 1000
MAX_SCANNED_FILL_ROWS_PER_ORDER = 10_000


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field}_invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field}_invalid")
    return parsed


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _normalized_remote_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    status = value.strip().lower()
    if status in SDK_OPEN:
        return "open"
    if status in SDK_PARTIAL:
        return "partially_filled"
    if status in SDK_FILLED:
        return "filled"
    if status in SDK_CANCELLED:
        return "cancelled"
    if status in SDK_REJECTED:
        return "rejected"
    return None


class PerpsSdkReconciler:
    """Reconcile local Perps order intents using the authenticated SDK's read APIs."""

    def __init__(
        self,
        *,
        journal: PerpsOrderJournal,
        session: Any,
        max_fill_rows: int = MAX_FILL_ROWS_PER_ORDER,
        max_scanned_fill_rows: int = MAX_SCANNED_FILL_ROWS_PER_ORDER,
    ) -> None:
        if not callable(getattr(session, "fetch_orders", None)):
            raise ValueError("session must expose fetch_orders")
        if not callable(getattr(session, "list_fills", None)):
            raise ValueError("session must expose list_fills")
        if isinstance(max_fill_rows, bool) or not isinstance(max_fill_rows, int) or not 1 <= max_fill_rows <= 10_000:
            raise ValueError("max_fill_rows must be between 1 and 10000")
        if (
            isinstance(max_scanned_fill_rows, bool)
            or not isinstance(max_scanned_fill_rows, int)
            or not 1 <= max_scanned_fill_rows <= 100_000
        ):
            raise ValueError("max_scanned_fill_rows must be between 1 and 100000")
        self.journal = journal
        self.session = session
        self.max_fill_rows = max_fill_rows
        self.max_scanned_fill_rows = max_scanned_fill_rows

    async def _average_fill_price(self, order: Any, order_id: int, expected_quantity: Decimal) -> Decimal:
        instrument_id = _field(order, "instrument_id")
        created_at = _field(order, "created_at")
        updated_at = _field(order, "updated_at")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
            raise ValueError("fill_history_instrument_invalid")
        if created_at is None:
            raise ValueError("fill_history_start_time_missing")
        if updated_at is None:
            raise ValueError("fill_history_end_time_missing")
        paginator = self.session.list_fills(
            instrument_id=instrument_id,
            start=created_at,
            end=updated_at,
            sort="asc",
        )
        total_quantity = Decimal("0")
        total_value = Decimal("0")
        rows = 0
        scanned_rows = 0
        async for fill in paginator:
            scanned_rows += 1
            if scanned_rows > self.max_scanned_fill_rows:
                raise ValueError("fill_history_scan_limit_exceeded")
            if _field(fill, "order_id") != order_id:
                continue
            fill_instrument_id = _field(fill, "instrument_id")
            if fill_instrument_id != instrument_id:
                raise ValueError("fill_history_instrument_mismatch")
            rows += 1
            if rows > self.max_fill_rows:
                raise ValueError("fill_history_row_limit_exceeded")
            quantity = _decimal(_field(fill, "quantity"), "fill_quantity")
            price = _decimal(_field(fill, "price"), "fill_price")
            if quantity <= 0 or price <= 0:
                raise ValueError("fill_history_invalid_price_or_quantity")
            total_quantity += quantity
            total_value += quantity * price
        if total_quantity != expected_quantity:
            raise ValueError("fill_history_quantity_mismatch")
        if total_quantity <= 0:
            raise ValueError("fill_history_empty")
        return total_value / total_quantity

    async def reconcile(self, client_order_id: str) -> dict[str, Any]:
        """Read one order and its fills; never submits or retries an order."""
        intent = self.journal.get_intent(client_order_id)
        if intent is None:
            raise KeyError("Perps order intent not found")
        status = intent["status"]
        if status == "prepared":
            raise RuntimeError("prepared order has not entered submission; reconciliation is unsafe")
        if status not in RECONCILABLE_STATUSES and status != "submission_pending":
            return intent

        if status == "submission_pending":
            intent = self.journal.record_submission_unknown(
                client_order_id,
                reason_code="reconcile_after_ambiguous_submission",
            )

        try:
            orders = await self.session.fetch_orders(client_order_id=client_order_id)
        except Exception:
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="lookup_error",
                reason_code="venue_order_lookup_failed",
            )

        if not isinstance(orders, (tuple, list)):
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="lookup_error",
                reason_code="venue_order_response_invalid",
            )
        matches = [
            order for order in orders
            if _field(order, "client_order_id") == client_order_id
        ]
        if not matches:
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="not_found",
                reason_code="order_not_visible_yet",
            )
        if len(matches) != 1:
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="lookup_error",
                reason_code="duplicate_client_order_id_results",
            )

        order = matches[0]
        order_id = _field(order, "id")
        instrument_id = _field(order, "instrument_id")
        side_value = _field(order, "side")
        side = side_value.strip().lower() if isinstance(side_value, str) else None
        remote_status = _normalized_remote_status(_field(order, "status"))
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="lookup_error",
                reason_code="venue_order_id_invalid",
            )
        if remote_status is None:
            # Unknown SDK/venue states are deliberately sent through the
            # journal's strict status validator, which moves them to manual review.
            remote_status = "unknown_sdk_status"

        try:
            requested_quantity = _decimal(_field(order, "quantity"), "order_quantity")
            filled_quantity = _decimal(_field(order, "filled_quantity"), "filled_quantity")
        except ValueError:
            return self.journal.record_reconciliation(
                client_order_id,
                outcome="lookup_error",
                reason_code="venue_order_quantities_invalid",
            )
        average_price: Decimal | None = None
        if filled_quantity > 0:
            try:
                average_price = await self._average_fill_price(
                    order, order_id, filled_quantity
                )
            except Exception:
                return self.journal.record_reconciliation(
                    client_order_id,
                    outcome="lookup_error",
                    reason_code="fill_history_not_yet_reconciled",
                )

        return self.journal.record_reconciliation(
            client_order_id,
            outcome="found",
            matched_client_order_id=_field(order, "client_order_id"),
            venue_order_id=str(order_id),
            instrument_id=instrument_id,
            side=side,
            quantity=requested_quantity,
            remote_status=remote_status,
            filled_quantity=filled_quantity,
            average_fill_price=average_price,
        )
