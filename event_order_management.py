"""Risk-reducing event-market order management.

This adapter deliberately exposes only authenticated reads and cancellation.
It does not sign or place orders. Cancellation is opt-in, explicitly armed,
geoblock-checked, serialized with the event BUY gateway, and verified by fresh
account reads. Ambiguous outcomes are never retried automatically.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from order_journal import OrderJournal
from polymarket_geo import check_polymarket_geoblock


CANCEL_ONE_ACKNOWLEDGEMENT = "I_CONFIRM_CANCEL_POLYMARKET_ORDER"
CANCEL_ALL_ACKNOWLEDGEMENT = "I_CONFIRM_CANCEL_ALL_POLYMARKET_ORDERS"
MAX_OPEN_ORDER_SNAPSHOT = 500


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}_invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field}_invalid") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field}_invalid")
    return number


def _geo_error(
    checker: Callable[[], dict[str, Any]], *, now: datetime | None = None
) -> str | None:
    try:
        result = checker()
    except Exception:
        return "polymarket_geoblock_check_failed"
    if not isinstance(result, dict) or result.get("verified") is not True:
        return "polymarket_geoblock_response_unverified"
    if result.get("blocked") is not False:
        return "polymarket_geoblock_rejected_or_unverified"
    country = result.get("country")
    region = result.get("region")
    if (
        not isinstance(country, str) or len(country) != 2
        or not country.isalpha() or country != country.upper()
        or not isinstance(region, str) or not region.strip() or len(region) > 16
    ):
        return "polymarket_geoblock_location_invalid"
    raw_timestamp = result.get("checked_at_utc")
    try:
        checked = raw_timestamp if isinstance(raw_timestamp, datetime) else datetime.fromisoformat(
            str(raw_timestamp).replace("Z", "+00:00")
        )
    except (TypeError, ValueError, OverflowError):
        return "polymarket_geoblock_timestamp_invalid"
    if checked.tzinfo is None or checked.utcoffset() is None:
        return "polymarket_geoblock_timestamp_invalid"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return "polymarket_geoblock_clock_invalid"
    age = (
        current.astimezone(timezone.utc) - checked.astimezone(timezone.utc)
    ).total_seconds()
    if age < -2 or age > 30:
        return "polymarket_geoblock_stale_or_future_dated"
    return None


class EventOrderOperationLock:
    """Crash-conservative lock shared by event order submission and cancellation.

    A lock left behind by a crash is intentionally not auto-expired. An
    operator must reconcile open orders and establish process quiescence before
    removing it.
    """

    def __init__(self, journal_database_path: str | Path) -> None:
        database = Path(journal_database_path).expanduser()
        self.path = Path(str(database) + ".event-order-operation.lock")
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "token": self.token,
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "EVENT_ORDER_OPERATION_LOCK",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return True
        except BaseException:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                raise

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return
            if payload.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False


class EventOrderOperations:
    """Explicitly armed, cancel-only management adapter for Event market orders.

    The supplied session is caller-owned and is never created or closed here.
    Defaults are disabled. Only orders mapped to this local journal can be
    canceled individually; cancel-all can also affect manual orders and
    therefore requires a separate, stronger confirmation phrase.
    """

    def __init__(
        self,
        *,
        session: Any,
        journal: OrderJournal,
        enabled: bool = False,
        manually_armed: bool = False,
        dry_run: bool = True,
        geoblock_checker: Callable[[], dict[str, Any]] = check_polymarket_geoblock,
        max_open_orders: int = MAX_OPEN_ORDER_SNAPSHOT,
    ) -> None:
        required = ("list_open_orders", "get_order", "cancel_order", "cancel_all")
        missing = [name for name in required if not callable(getattr(session, name, None))]
        if missing:
            raise ValueError("session is missing required order-management methods")
        if not isinstance(journal, OrderJournal):
            raise ValueError("a durable OrderJournal is required")
        if not all(isinstance(flag, bool) for flag in (enabled, manually_armed, dry_run)):
            raise ValueError("order-management flags must be booleans")
        if not callable(geoblock_checker):
            raise ValueError("geoblock_checker must be callable")
        if (
            isinstance(max_open_orders, bool)
            or not isinstance(max_open_orders, int)
            or not 1 <= max_open_orders <= 5000
        ):
            raise ValueError("max_open_orders must be between 1 and 5000")
        self.session = session
        self.journal = journal
        self.enabled = enabled
        self.manually_armed = manually_armed
        self.dry_run = dry_run
        self.geoblock_checker = geoblock_checker
        self.max_open_orders = max_open_orders

    def _ready_error(self) -> str | None:
        if not self.enabled or not self.manually_armed or self.dry_run:
            return "event_order_management_not_explicitly_enabled_and_armed"
        return _geo_error(self.geoblock_checker)

    def _blocked(self, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "mode": "EVENT_ORDER_MANAGEMENT",
            "state": "blocked",
            "reason_code": reason,
            "order_signing_allowed": False,
            "new_order_submission_allowed": False,
            "automatic_retry_permitted": False,
            "execution": "none",
            **extra,
        }

    def _open_orders(self) -> tuple[list[Any], bool]:
        paginator = self.session.list_open_orders()
        orders: list[Any] = []
        for page in paginator:
            if _field(page, "limit_reached", False) is True:
                raise RuntimeError("open_order_snapshot_pagination_limit_reached")
            items = _field(page, "items")
            if not isinstance(items, (tuple, list)):
                raise RuntimeError("open_order_snapshot_page_invalid")
            orders.extend(items)
            if len(orders) > self.max_open_orders:
                raise RuntimeError("open_order_snapshot_exceeds_configured_limit")
        seen: set[str] = set()
        for order in orders:
            order_id = _field(order, "id")
            if not isinstance(order_id, str) or not order_id.strip() or order_id in seen:
                raise RuntimeError("open_order_snapshot_has_invalid_or_duplicate_id")
            seen.add(order_id)
        return orders, True

    @staticmethod
    def _snapshot_order(order: Any) -> dict[str, Any]:
        return {
            "order_id": str(_field(order, "id")),
            "condition_id": str(_field(order, "condition_id", _field(order, "market", ""))),
            "asset_id": str(_field(order, "asset_id", _field(order, "token_id", ""))),
            "side": str(_field(order, "side", "")).upper(),
            "price": str(_field(order, "price", "")),
            "original_size": str(_field(order, "original_size", "")),
            "size_matched": str(_field(order, "size_matched", "")),
            "status": str(_field(order, "status", "")).upper(),
            "order_type": str(_field(order, "order_type", "")),
        }

    def read_open_orders(self) -> dict[str, Any]:
        """Read a bounded, complete open-order snapshot; never changes account state."""
        try:
            orders, complete = self._open_orders()
        except Exception as exc:
            return {
                "mode": "EVENT_ORDER_MANAGEMENT",
                "state": "unavailable",
                "reason_code": type(exc).__name__[:80],
                "snapshot_complete": False,
                "orders": [],
                "execution": "none",
            }
        return {
            "mode": "EVENT_ORDER_MANAGEMENT",
            "state": "healthy",
            "snapshot_complete": complete,
            "open_order_count": len(orders),
            "orders": [self._snapshot_order(order) for order in orders],
            "order_signing_allowed": False,
            "new_order_submission_allowed": False,
            "execution": "none",
        }

    def _local_intent(self, order_id: str) -> tuple[str, dict[str, Any]]:
        client_order_id = self.journal.find_client_order_id_by_exchange_order_id(order_id)
        if client_order_id is None:
            raise ValueError("local_order_journal_mapping_missing")
        state = self.journal.get_intent(client_order_id)
        if not isinstance(state, dict):
            raise ValueError("local_order_journal_intent_missing")
        if state.get("exchange_order_id") != order_id:
            raise ValueError("local_order_journal_exchange_id_mismatch")
        if state.get("status") not in {"open", "partially_filled"}:
            raise ValueError("local_order_not_in_cancellable_state")
        return client_order_id, state

    @staticmethod
    def _verify_identity(order: Any, *, order_id: str, intent: dict[str, Any]) -> Decimal:
        if _field(order, "id") != order_id:
            raise ValueError("venue_order_id_mismatch")
        asset_id = _field(order, "asset_id", _field(order, "token_id"))
        if str(asset_id or "") != str(intent.get("token_id") or ""):
            raise ValueError("venue_order_asset_mismatch")
        if str(_field(order, "side", "")).upper() != str(intent.get("side", "")).upper():
            raise ValueError("venue_order_side_mismatch")
        expected_condition = intent.get("market_condition_id")
        if expected_condition is not None:
            observed_condition = _field(
                order, "condition_id", _field(order, "market")
            )
            if str(observed_condition or "") != str(expected_condition):
                raise ValueError("venue_order_condition_mismatch")
        original_size = _decimal(_field(order, "original_size"), "venue_original_size")
        expected_size = _decimal(intent.get("size"), "local_order_size")
        if original_size != expected_size:
            raise ValueError("venue_order_size_mismatch")
        price = _decimal(_field(order, "price"), "venue_order_price")
        expected_price = _decimal(intent.get("limit_price"), "local_order_price")
        if price != expected_price:
            raise ValueError("venue_order_price_mismatch")
        matched = _decimal(_field(order, "size_matched"), "venue_size_matched")
        local_filled = _decimal(intent.get("filled_size", "0"), "local_filled_size")
        if matched < local_filled or matched > original_size:
            raise ValueError("venue_cumulative_fill_invalid_or_regressed")
        return matched

    def _record_final_observation(
        self,
        *,
        client_order_id: str,
        order_id: str,
        intent: dict[str, Any],
        order: Any,
        cumulative_filled: Decimal,
    ) -> str | None:
        status = str(_field(order, "status", "")).upper()
        original_size = _decimal(intent.get("size"), "local_order_size")
        if status in {"CANCELLED", "CANCELED", "EXPIRED", "AUTO_CANCELLED"}:
            if cumulative_filled >= original_size:
                outcome = "filled"
            else:
                outcome = "cancelled"
        elif status in {"MATCHED", "FILLED", "FULLY_FILLED"} and cumulative_filled == original_size:
            outcome = "filled"
        else:
            return None
        self.journal.record_order_observation(
            client_order_id,
            outcome=outcome,
            matched_client_order_id=client_order_id,
            exchange_order_id=order_id,
            cumulative_filled_size=float(cumulative_filled),
            reason_code="verified_after_cancel_operation",
            observed_token_id=str(_field(order, "asset_id", _field(order, "token_id", ""))),
            observed_side=str(_field(order, "side", "")).upper(),
            observed_original_size=float(original_size),
            observed_price=float(_decimal(_field(order, "price"), "venue_order_price")),
        )
        return outcome

    def cancel_order(
        self, order_id: str, *, confirmation: str | None = None
    ) -> dict[str, Any]:
        """Cancel one journal-owned order and prove it is no longer open.

        The method never retries a cancellation. A cancel ACK is not proof of
        terminal venue state; get_order plus a fresh complete open-order
        snapshot must confirm a terminal outcome.
        """
        if confirmation != CANCEL_ONE_ACKNOWLEDGEMENT:
            return self._blocked("explicit_single_cancel_confirmation_required")
        reason = self._ready_error()
        if reason is not None:
            return self._blocked(reason)
        if not isinstance(order_id, str) or not order_id.strip() or len(order_id) > 200:
            return self._blocked("exchange_order_id_invalid")
        lock = EventOrderOperationLock(self.journal.database_path)
        if not lock.acquire():
            return self._blocked("event_order_operation_lock_busy_or_stale")
        try:
            try:
                client_order_id, intent = self._local_intent(order_id)
                remote_before = self.session.get_order(order_id=order_id)
                before_fill = self._verify_identity(
                    remote_before, order_id=order_id, intent=intent
                )
                before_orders, _ = self._open_orders()
                open_before = {str(_field(item, "id")) for item in before_orders}
                if order_id not in open_before:
                    outcome = self._record_final_observation(
                        client_order_id=client_order_id,
                        order_id=order_id,
                        intent=intent,
                        order=remote_before,
                        cumulative_filled=before_fill,
                    )
                    if outcome is not None:
                        return {
                            "mode": "EVENT_ORDER_MANAGEMENT",
                            "state": "already_terminal",
                            "reason_code": "order_was_not_open",
                            "client_order_id": client_order_id,
                            "exchange_order_id": order_id,
                            "terminal_outcome": outcome,
                            "filled_size": str(before_fill),
                            "automatic_retry_permitted": False,
                            "execution": "none",
                        }
                    return self._blocked("order_absence_not_matched_to_terminal_status")
                # A cancellation request is made at most once. Any exception or
                # uncertain response is reconciled by reads, never by retry.
                try:
                    cancel_response = self.session.cancel_order(order_id=order_id)
                except Exception:
                    # The request may have reached the venue. Reconcile by reads
                    # only; never repeat the cancel request after transport loss.
                    try:
                        remote_after_error = self.session.get_order(order_id=order_id)
                        fill_after_error = self._verify_identity(
                            remote_after_error, order_id=order_id, intent=intent
                        )
                        orders_after_error, _ = self._open_orders()
                        still_open_after_error = order_id in {
                            str(_field(item, "id")) for item in orders_after_error
                        }
                        if not still_open_after_error:
                            outcome_after_error = self._record_final_observation(
                                client_order_id=client_order_id,
                                order_id=order_id,
                                intent=intent,
                                order=remote_after_error,
                                cumulative_filled=fill_after_error,
                            )
                            if outcome_after_error is not None:
                                return {
                                    "mode": "EVENT_ORDER_MANAGEMENT",
                                    "state": "cancelled" if outcome_after_error == "cancelled" else "filled",
                                    "reason_code": "cancel_response_lost_but_terminal_state_verified",
                                    "client_order_id": client_order_id,
                                    "exchange_order_id": order_id,
                                    "cancel_acknowledged": False,
                                    "filled_size": str(fill_after_error),
                                    "automatic_retry_permitted": False,
                                    "execution": "none",
                                }
                    except Exception:
                        pass
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "manual_review",
                        "reason_code": "cancel_response_lost_outcome_not_proven",
                        "client_order_id": client_order_id,
                        "exchange_order_id": order_id,
                        "automatic_retry_permitted": False,
                        "execution": "none",
                    }
                cancelled_ids = tuple(_field(cancel_response, "canceled", ()) or ())
                not_cancelled = _field(cancel_response, "not_canceled", {}) or {}
                remote_after = self.session.get_order(order_id=order_id)
                after_fill = self._verify_identity(
                    remote_after, order_id=order_id, intent=intent
                )
                after_orders, _ = self._open_orders()
                still_open = order_id in {str(_field(item, "id")) for item in after_orders}
                outcome = None if still_open else self._record_final_observation(
                    client_order_id=client_order_id,
                    order_id=order_id,
                    intent=intent,
                    order=remote_after,
                    cumulative_filled=after_fill,
                )
                if outcome is None:
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "cancel_unresolved",
                        "reason_code": "venue_cancel_not_verified",
                        "client_order_id": client_order_id,
                        "exchange_order_id": order_id,
                        "cancel_acknowledged": order_id in cancelled_ids,
                        "venue_not_canceled_reason": str(not_cancelled.get(order_id, ""))[:120],
                        "still_open": still_open,
                        "filled_size": str(after_fill),
                        "automatic_retry_permitted": False,
                        "order_submission_allowed": False,
                        "execution": "cancel_only",
                    }
                return {
                    "mode": "EVENT_ORDER_MANAGEMENT",
                    "state": "cancelled" if outcome == "cancelled" else "filled",
                    "reason_code": "cancel_and_terminal_state_verified",
                    "client_order_id": client_order_id,
                    "exchange_order_id": order_id,
                    "cancel_acknowledged": order_id in cancelled_ids,
                    "filled_size": str(after_fill),
                    "unfilled_size": str(max(Decimal("0"), _decimal(intent["size"], "local_order_size") - after_fill)),
                    "automatic_retry_permitted": False,
                    "order_submission_allowed": False,
                    "execution": "cancel_only",
                }
            except Exception as exc:
                return {
                    "mode": "EVENT_ORDER_MANAGEMENT",
                    "state": "manual_review",
                    "reason_code": type(exc).__name__[:80],
                    "exchange_order_id": order_id,
                    "automatic_retry_permitted": False,
                    "execution": "none",
                }
        finally:
            lock.release()

    def cancel_all(
        self, *, confirmation: str | None = None
    ) -> dict[str, Any]:
        """Cancel all open orders for this authenticated client and verify absence.

        This can also affect orders not created by this bot. It therefore uses a
        separate stronger confirmation and surfaces untracked IDs for review.
        """
        if confirmation != CANCEL_ALL_ACKNOWLEDGEMENT:
            return self._blocked("explicit_cancel_all_confirmation_required")
        reason = self._ready_error()
        if reason is not None:
            return self._blocked(reason)
        lock = EventOrderOperationLock(self.journal.database_path)
        if not lock.acquire():
            return self._blocked("event_order_operation_lock_busy_or_stale")
        try:
            try:
                before_orders, _ = self._open_orders()
                before_ids = [str(_field(item, "id")) for item in before_orders]
                if not before_ids:
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "already_empty",
                        "open_order_count": 0,
                        "automatic_retry_permitted": False,
                        "execution": "none",
                    }
                # Do not repeat this request after an unknown outcome.
                response = self.session.cancel_all()
                cancelled_ids = tuple(_field(response, "canceled", ()) or ())
                not_cancelled = _field(response, "not_canceled", {}) or {}
                after_orders, _ = self._open_orders()
                after_ids = [str(_field(item, "id")) for item in after_orders]
                if after_ids:
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "cancel_unresolved",
                        "reason_code": "open_orders_remain_after_cancel_all",
                        "initial_order_ids": before_ids,
                        "cancelled_order_ids": list(cancelled_ids),
                        "not_canceled": {
                            str(key): str(value)[:120] for key, value in dict(not_cancelled).items()
                        },
                        "open_order_ids_after_cancel": after_ids,
                        "automatic_retry_permitted": False,
                        "execution": "none",
                    }
                untracked: list[str] = []
                unresolved: list[str] = []
                reconciled: list[str] = []
                for order_id in before_ids:
                    client_order_id = self.journal.find_client_order_id_by_exchange_order_id(order_id)
                    if client_order_id is None:
                        untracked.append(order_id)
                        continue
                    intent = self.journal.get_intent(client_order_id)
                    if not isinstance(intent, dict):
                        unresolved.append(order_id)
                        continue
                    remote = self.session.get_order(order_id=order_id)
                    matched = self._verify_identity(
                        remote, order_id=order_id, intent=intent
                    )
                    outcome = self._record_final_observation(
                        client_order_id=client_order_id,
                        order_id=order_id,
                        intent=intent,
                        order=remote,
                        cumulative_filled=matched,
                    )
                    if outcome is None:
                        unresolved.append(order_id)
                    else:
                        reconciled.append(order_id)
                state = "cancelled_all_verified" if not untracked and not unresolved else "manual_review"
                return {
                    "mode": "EVENT_ORDER_MANAGEMENT",
                    "state": state,
                    "reason_code": (
                        "all_open_orders_absent_and_local_orders_reconciled"
                        if state == "cancelled_all_verified"
                        else "all_open_orders_absent_but_some_order_history_requires_review"
                    ),
                    "initial_order_ids": before_ids,
                    "cancelled_order_ids": list(cancelled_ids),
                    "not_canceled": {
                        str(key): str(value)[:120] for key, value in dict(not_cancelled).items()
                    },
                    "untracked_order_ids": untracked,
                    "unresolved_order_ids": unresolved,
                    "reconciled_order_ids": reconciled,
                    "open_order_ids_after_cancel": [],
                    "automatic_retry_permitted": False,
                    "execution": "none",
                }
            except Exception:
                # If the cancel-all response is lost, do not repeat it. Re-read
                # the complete open-order book and reconcile only exact local IDs.
                try:
                    after_error, _ = self._open_orders()
                    after_error_ids = [str(_field(item, "id")) for item in after_error]
                    untracked_after_error: list[str] = []
                    unresolved_after_error: list[str] = []
                    reconciled_after_error: list[str] = []
                    if not after_error_ids:
                        for order_id in before_ids if "before_ids" in locals() else []:
                            client_order_id = self.journal.find_client_order_id_by_exchange_order_id(order_id)
                            if client_order_id is None:
                                untracked_after_error.append(order_id)
                                continue
                            intent = self.journal.get_intent(client_order_id)
                            if not isinstance(intent, dict):
                                unresolved_after_error.append(order_id)
                                continue
                            remote = self.session.get_order(order_id=order_id)
                            matched = self._verify_identity(
                                remote, order_id=order_id, intent=intent
                            )
                            outcome = self._record_final_observation(
                                client_order_id=client_order_id,
                                order_id=order_id,
                                intent=intent,
                                order=remote,
                                cumulative_filled=matched,
                            )
                            if outcome is None:
                                unresolved_after_error.append(order_id)
                            else:
                                reconciled_after_error.append(order_id)
                    if (
                        not after_error_ids
                        and not untracked_after_error
                        and not unresolved_after_error
                        and bool(reconciled_after_error)
                    ):
                        return {
                            "mode": "EVENT_ORDER_MANAGEMENT",
                            "state": "cancelled_all_verified",
                            "reason_code": "cancel_all_response_lost_but_readback_verified",
                            "cancel_acknowledged": False,
                            "reconciled_order_ids": reconciled_after_error,
                            "open_order_ids_after_cancel": [],
                            "automatic_retry_permitted": False,
                            "order_submission_allowed": False,
                            "execution": "cancel_only",
                        }
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "manual_review" if not after_error_ids else "cancel_unresolved",
                        "reason_code": "cancel_all_response_lost_readback_incomplete",
                        "open_order_ids_after_cancel": after_error_ids,
                        "untracked_order_ids": untracked_after_error,
                        "unresolved_order_ids": unresolved_after_error,
                        "reconciled_order_ids": reconciled_after_error,
                        "automatic_retry_permitted": False,
                        "order_submission_allowed": False,
                        "execution": "cancel_only",
                    }
                except Exception:
                    return {
                        "mode": "EVENT_ORDER_MANAGEMENT",
                        "state": "manual_review",
                        "reason_code": "cancel_all_response_lost_and_readback_failed",
                        "automatic_retry_permitted": False,
                        "order_submission_allowed": False,
                        "execution": "cancel_only",
                    }
        finally:
            lock.release()
