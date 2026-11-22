"""Durable Perps order state journal; deliberately contains no exchange adapter.

The SDK's Perps client_order_id is a 32-character lowercase hex identifier.
Every first submission must be journaled before network I/O. An ambiguous
outcome is reconciled by that same ID and is never automatically resubmitted.
A submission acknowledgement is not a fill: fills and order state are only
accepted from a later account-order reconciliation observation.

This module never loads credentials, signs, connects to Polymarket, submits,
cancels, or authorizes trading.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
CLIENT_ORDER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ORDER_STATUSES = frozenset({
    "prepared",
    "submission_pending",
    "submission_unknown",
    "acknowledged_unreconciled",
    "open",
    "partially_filled",
    "cancel_pending",
    "filled",
    "cancelled",
    "rejected",
    "manual_review",
})
RECONCILABLE_STATUSES = frozenset({
    "submission_unknown",
    "acknowledged_unreconciled",
    "open",
    "partially_filled",
    "cancel_pending",
})
REMOTE_OPEN_STATUSES = frozenset({"open", "live", "accepted", "pending", "new"})
REMOTE_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "expired"})
REMOTE_REJECTED_STATUSES = frozenset({"rejected", "failed"})


def _decimal(value: Any, field: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite() or (not allow_zero and parsed <= 0):
        raise ValueError(f"{field} is outside valid bounds")
    return parsed


def _required_text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field} must be non-empty bounded text")
    return value.strip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _copy_state(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_json(value))


def _validate_client_order_id(value: Any) -> str:
    result = _required_text(value, "client_order_id", maximum=32)
    if not CLIENT_ORDER_ID_RE.fullmatch(result):
        raise ValueError("client_order_id must be 32 lowercase hexadecimal characters")
    return result


def _validate_state(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("Perps order state must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported Perps order journal schema")
    if state.get("mode") != "PERPS_ORDER_JOURNAL_ONLY" or state.get("execution") != "none":
        raise ValueError("this journal must remain non-executing")
    client_id = _validate_client_order_id(state.get("client_order_id"))
    instrument_id = state.get("instrument_id")
    if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
        raise ValueError("instrument_id must be a positive integer")
    if state.get("side") not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    quantity = _decimal(state.get("quantity"), "quantity", allow_zero=False)
    tif = state.get("time_in_force")
    if tif not in {"gtc", "gtd", "ioc", "fok"}:
        raise ValueError("unsupported Perps time_in_force")
    price_value = state.get("limit_price")
    if tif in {"gtc", "gtd"}:
        price = _decimal(price_value, "limit_price", allow_zero=False)
    elif price_value is None:
        price = None
    else:
        price = _decimal(price_value, "limit_price", allow_zero=False)
    if tif == "gtd" and not state.get("gtd_expiry_utc"):
        raise ValueError("gtd orders require gtd_expiry_utc")
    if tif != "gtd" and state.get("gtd_expiry_utc") is not None:
        raise ValueError("gtd_expiry_utc is only valid for gtd orders")
    if not isinstance(state.get("reduce_only"), bool):
        raise ValueError("reduce_only must be boolean")
    if not isinstance(state.get("position_intent"), str) or state["position_intent"] not in {
        "open_long", "open_short", "close_long", "close_short"
    }:
        raise ValueError("invalid position_intent")
    expected_side = {
        "open_long": "buy",
        "close_short": "buy",
        "open_short": "sell",
        "close_long": "sell",
    }[state["position_intent"]]
    expected_reduce_only = state["position_intent"].startswith("close_")
    if state["side"] != expected_side or state["reduce_only"] is not expected_reduce_only:
        raise ValueError("side/reduce_only does not match position_intent")
    if state.get("status") not in ORDER_STATUSES:
        raise ValueError("invalid Perps order status")
    attempts = state.get("submission_attempts")
    expected_attempts = 0 if state["status"] == "prepared" else 1
    if isinstance(attempts, bool) or attempts != expected_attempts:
        raise ValueError("submission_attempts conflicts with the order status")
    reconciliations = state.get("reconciliation_attempts")
    if isinstance(reconciliations, bool) or not isinstance(reconciliations, int) or reconciliations < 0:
        raise ValueError("reconciliation_attempts must be a nonnegative integer")
    filled = _decimal(state.get("filled_quantity", "0"), "filled_quantity")
    if filled > quantity:
        raise ValueError("filled_quantity cannot exceed order quantity")
    average_price_raw = state.get("average_fill_price")
    if filled == 0 and average_price_raw is not None:
        raise ValueError("zero-fill orders cannot have an average fill price")
    if filled > 0:
        average_price = _decimal(average_price_raw, "average_fill_price", allow_zero=False)
    else:
        average_price = None
    venue_id = state.get("venue_order_id")
    if venue_id is not None:
        _required_text(venue_id, "venue_order_id", maximum=128)
    if state["status"] in {
        "acknowledged_unreconciled", "open", "partially_filled",
        "cancel_pending", "filled", "cancelled"
    } and venue_id is None:
        raise ValueError("reconciled venue state requires venue_order_id")
    if state["status"] == "partially_filled" and not (Decimal("0") < filled < quantity):
        raise ValueError("partially_filled requires a positive incomplete fill")
    if state["status"] == "filled" and filled != quantity:
        raise ValueError("filled status requires the complete requested quantity")
    if state["status"] == "open" and filled != 0:
        raise ValueError("open status must have zero filled quantity")
    if state["status"] == "rejected" and (venue_id is not None or filled != 0):
        raise ValueError("rejected status cannot contain a venue order or fill")
    if state["status"] == "prepared" and (
        venue_id is not None or filled != 0 or reconciliations != 0
    ):
        raise ValueError("prepared order has inconsistent execution metadata")
    if state.get("automatic_retry_permitted") is not False:
        raise ValueError("automatic order retries must remain disabled")
    _required_text(state.get("created_at"), "created_at", maximum=64)
    _required_text(state.get("updated_at"), "updated_at", maximum=64)
    state["client_order_id"] = client_id
    state["quantity"] = str(quantity)
    state["filled_quantity"] = str(filled)
    state["average_fill_price"] = str(average_price) if average_price is not None else None
    state["limit_price"] = str(price) if price is not None else None
    return state


class PerpsOrderJournal:
    """WAL-backed append-only event journal with an atomic latest order state."""

    def __init__(self, database_path: str | Path) -> None:
        if str(database_path) == ":memory:":
            raise ValueError("PerpsOrderJournal requires a file-backed durable database")
        self.database_path = str(Path(database_path).expanduser())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=15.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise RuntimeError("SQLite WAL mode unavailable for Perps order journal")
            if [str(row[0]) for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
                raise sqlite3.DatabaseError("Perps order journal quick_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("Perps order journal foreign_key_check failed")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS perps_order_intents (
                    client_order_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS perps_order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(client_order_id)
                        REFERENCES perps_order_intents(client_order_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_perps_order_events_client "
                "ON perps_order_events(client_order_id, event_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_perps_order_intents_status "
                "ON perps_order_intents(status)"
            ) if False else None
        finally:
            connection.close()

    def create_intent(
        self,
        *,
        client_order_id: str,
        instrument_id: int,
        side: str,
        quantity: Any,
        position_intent: str,
        time_in_force: str,
        limit_price: Any = None,
        gtd_expiry_utc: str | None = None,
        reduce_only: bool | None = None,
        strategy_signal_id: str | None = None,
        risk_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a prepared Perps order. Repeating identical terms is idempotent."""
        client_id = _validate_client_order_id(client_order_id)
        normalized_intent = _required_text(position_intent, "position_intent", maximum=32)
        expected_reduce_only = normalized_intent.startswith("close_")
        expected_side = {
            "open_long": "buy", "close_short": "buy",
            "open_short": "sell", "close_long": "sell",
        }.get(normalized_intent)
        if expected_side is None:
            raise ValueError("position_intent must be open_long/open_short/close_long/close_short")
        normalized_side = _required_text(side, "side", maximum=8).lower()
        if normalized_side != expected_side:
            raise ValueError("side does not match position_intent")
        if reduce_only is None:
            reduce_only = expected_reduce_only
        if not isinstance(reduce_only, bool) or reduce_only is not expected_reduce_only:
            raise ValueError("reduce_only must match open/close position intent")
        qty = _decimal(quantity, "quantity", allow_zero=False)
        tif = _required_text(time_in_force, "time_in_force", maximum=8).lower()
        price = None if limit_price is None else _decimal(limit_price, "limit_price", allow_zero=False)
        if tif in {"gtc", "gtd"} and price is None:
            raise ValueError("gtc/gtd orders require limit_price")
        if tif == "gtd":
            expiry = _required_text(gtd_expiry_utc, "gtd_expiry_utc", maximum=64)
            parsed_expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
                raise ValueError("gtd_expiry_utc must be timezone-aware")
            expiry = parsed_expiry.astimezone(timezone.utc).isoformat()
        else:
            expiry = None
            if gtd_expiry_utc is not None:
                raise ValueError("gtd_expiry_utc is only valid for gtd orders")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
            raise ValueError("instrument_id must be a positive integer")
        signal_id = None if strategy_signal_id is None else _required_text(
            strategy_signal_id, "strategy_signal_id", maximum=128
        )
        risk_id = None if risk_snapshot_id is None else _required_text(
            risk_snapshot_id, "risk_snapshot_id", maximum=128
        )
        now = _timestamp()
        state = _validate_state({
            "schema_version": SCHEMA_VERSION,
            "mode": "PERPS_ORDER_JOURNAL_ONLY",
            "client_order_id": client_id,
            "instrument_id": instrument_id,
            "side": normalized_side,
            "quantity": str(qty),
            "position_intent": normalized_intent,
            "time_in_force": tif,
            "limit_price": str(price) if price is not None else None,
            "gtd_expiry_utc": expiry,
            "reduce_only": reduce_only,
            "strategy_signal_id": signal_id,
            "risk_snapshot_id": risk_id,
            "status": "prepared",
            "submission_attempts": 0,
            "reconciliation_attempts": 0,
            "venue_order_id": None,
            "filled_quantity": "0",
            "average_fill_price": None,
            "last_reason_code": None,
            "automatic_retry_permitted": False,
            "created_at": now,
            "updated_at": now,
            "execution": "none",
        })
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state_json FROM perps_order_intents WHERE client_order_id=?",
                (client_id,),
            ).fetchone()
            if row is not None:
                existing = _validate_state(json.loads(row["state_json"]))
                immutable_fields = (
                    "client_order_id", "instrument_id", "side", "quantity",
                    "position_intent", "time_in_force", "limit_price",
                    "gtd_expiry_utc", "reduce_only", "strategy_signal_id",
                    "risk_snapshot_id",
                )
                if any(existing.get(field) != state.get(field) for field in immutable_fields):
                    raise ValueError("client_order_id idempotency conflict: immutable terms differ")
                return existing
            payload = _json(state)
            connection.execute(
                "INSERT INTO perps_order_intents(client_order_id,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (client_id, payload, now, now),
            )
            self._append_event(connection, client_id, "INTENT_PREPARED", state, now)
        return _copy_state(state)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        client_order_id: str,
        event_type: str,
        state: dict[str, Any],
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO perps_order_events(client_order_id,event_type,state_json,created_at) "
            "VALUES(?,?,?,?)",
            (client_order_id, event_type, _json(state), timestamp),
        )

    def _transition(
        self,
        client_order_id: str,
        event_type: str,
        transition: Any,
    ) -> dict[str, Any]:
        client_id = _validate_client_order_id(client_order_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state_json FROM perps_order_intents WHERE client_order_id=?",
                (client_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Perps order intent not found")
            state = _validate_state(json.loads(row["state_json"]))
            updated = transition(state)
            updated["updated_at"] = _timestamp()
            updated = _validate_state(updated)
            connection.execute(
                "UPDATE perps_order_intents SET state_json=?,updated_at=? WHERE client_order_id=?",
                (_json(updated), updated["updated_at"], client_id),
            )
            self._append_event(connection, client_id, event_type, updated, updated["updated_at"])
        return _copy_state(updated)

    def begin_submission(self, client_order_id: str) -> dict[str, Any]:
        """Commit the single submission attempt before the caller performs I/O."""
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] != "prepared" or state["submission_attempts"] != 0:
                raise RuntimeError("duplicate or unsafe Perps submission attempt blocked")
            state["status"] = "submission_pending"
            state["submission_attempts"] = 1
            return state
        return self._transition(client_order_id, "SUBMISSION_STARTED", transition)

    def record_submission_acknowledgement(
        self, client_order_id: str, *, venue_order_id: str
    ) -> dict[str, Any]:
        """Record an ACK only; deliberately does not claim a fill or open state."""
        order_id = _required_text(venue_order_id, "venue_order_id", maximum=128)
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] != "submission_pending":
                raise RuntimeError("Perps acknowledgement requires submission_pending")
            state["status"] = "acknowledged_unreconciled"
            state["venue_order_id"] = order_id
            state["last_reason_code"] = None
            return state
        return self._transition(client_order_id, "SUBMISSION_ACKNOWLEDGED", transition)

    def record_submission_unknown(
        self, client_order_id: str, *, reason_code: str = "transport_outcome_unknown"
    ) -> dict[str, Any]:
        reason = _required_text(reason_code, "reason_code", maximum=64)
        if not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
            raise ValueError("reason_code contains unsafe characters")
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] != "submission_pending":
                raise RuntimeError("ambiguous outcome requires submission_pending")
            state["status"] = "submission_unknown"
            state["last_reason_code"] = reason
            return state
        return self._transition(client_order_id, "SUBMISSION_OUTCOME_UNKNOWN", transition)

    def record_submission_rejection(
        self, client_order_id: str, *, reason_code: str = "venue_rejected"
    ) -> dict[str, Any]:
        reason = _required_text(reason_code, "reason_code", maximum=64)
        if not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
            raise ValueError("reason_code contains unsafe characters")
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] != "submission_pending":
                raise RuntimeError("rejection requires submission_pending")
            state["status"] = "rejected"
            state["last_reason_code"] = reason
            return state
        return self._transition(client_order_id, "SUBMISSION_REJECTED", transition)

    def record_cancel_requested(self, client_order_id: str) -> dict[str, Any]:
        """Persist a cancel request; a request/ACK is not proof the order is cancelled."""
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] not in {
                "acknowledged_unreconciled", "open", "partially_filled"
            }:
                raise RuntimeError("only acknowledged/open Perps orders can request cancellation")
            state["status"] = "cancel_pending"
            return state
        return self._transition(client_order_id, "CANCEL_REQUESTED", transition)

    def record_reconciliation(
        self,
        client_order_id: str,
        *,
        outcome: str,
        matched_client_order_id: str | None = None,
        venue_order_id: str | None = None,
        instrument_id: int | None = None,
        side: str | None = None,
        quantity: Any = None,
        remote_status: str | None = None,
        filled_quantity: Any = "0",
        average_fill_price: Any = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile against an authenticated order read; not-found stays ambiguous."""
        if outcome not in {"found", "not_found", "lookup_error"}:
            raise ValueError("outcome must be found, not_found, or lookup_error")
        reason = reason_code or ("order_not_visible_yet" if outcome == "not_found" else "order_lookup_failed")
        if not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
            raise ValueError("reason_code contains unsafe characters")
        normalized_side = side.lower() if isinstance(side, str) else None
        def transition(state: dict[str, Any]) -> dict[str, Any]:
            if state["status"] not in RECONCILABLE_STATUSES:
                raise RuntimeError("only submitted Perps orders can be reconciled")
            state["reconciliation_attempts"] += 1
            if outcome != "found":
                state["last_reason_code"] = reason
                return state
            mismatch = None
            if matched_client_order_id != state["client_order_id"]:
                mismatch = "client_order_id_mismatch"
            elif isinstance(instrument_id, bool) or instrument_id != state["instrument_id"]:
                mismatch = "instrument_id_mismatch"
            elif normalized_side != state["side"]:
                mismatch = "side_mismatch"
            else:
                try:
                    observed_quantity = _decimal(quantity, "venue_quantity", allow_zero=False)
                    if observed_quantity != Decimal(state["quantity"]):
                        mismatch = "order_quantity_mismatch"
                    observed_filled = _decimal(filled_quantity, "venue_filled_quantity")
                    if observed_filled > observed_quantity:
                        mismatch = "venue_filled_quantity_exceeds_order"
                    previous_filled = Decimal(state["filled_quantity"])
                    if observed_filled < previous_filled:
                        mismatch = "venue_filled_quantity_regressed"
                    observed_average = (
                        _decimal(average_fill_price, "average_fill_price", allow_zero=False)
                        if observed_filled > 0 else None
                    )
                    status = _required_text(remote_status, "remote_status", maximum=32).lower()
                    external_id = _required_text(venue_order_id, "venue_order_id", maximum=128)
                except ValueError:
                    mismatch = mismatch or "invalid_venue_order_observation"
                    observed_filled = Decimal(state["filled_quantity"])
                    observed_average = (
                        Decimal(state["average_fill_price"])
                        if state["average_fill_price"] is not None else None
                    )
                    status = ""
                    external_id = ""
            if mismatch is not None:
                state["status"] = "manual_review"
                state["last_reason_code"] = mismatch
                if venue_order_id and isinstance(venue_order_id, str) and len(venue_order_id) <= 128:
                    state["last_conflicting_venue_order_id"] = venue_order_id
                return state
            if state["venue_order_id"] is not None and state["venue_order_id"] != external_id:
                state["status"] = "manual_review"
                state["last_reason_code"] = "venue_order_id_mismatch"
                state["last_conflicting_venue_order_id"] = external_id
                return state
            if status in REMOTE_REJECTED_STATUSES:
                if observed_filled != 0:
                    state["status"] = "manual_review"
                    state["last_reason_code"] = "rejected_order_has_fill"
                    return state
                state["status"] = "rejected"
                state["last_reason_code"] = "venue_rejected"
                return state
            if status not in REMOTE_OPEN_STATUSES | REMOTE_CANCELLED_STATUSES | {"filled", "partially_filled"}:
                state["status"] = "manual_review"
                state["last_reason_code"] = "unrecognized_venue_order_status"
                state["last_conflicting_venue_order_id"] = external_id
                return state
            state["venue_order_id"] = external_id
            state["filled_quantity"] = str(observed_filled)
            state["average_fill_price"] = (
                str(observed_average) if observed_average is not None else None
            )
            state["last_reason_code"] = None
            requested = Decimal(state["quantity"])
            if status in REMOTE_CANCELLED_STATUSES:
                state["status"] = "cancelled"
            elif status == "filled" or observed_filled == requested:
                state["status"] = "filled"
            elif status == "partially_filled" or observed_filled > 0:
                state["status"] = "partially_filled"
            elif status in REMOTE_OPEN_STATUSES:
                state["status"] = "cancel_pending" if state["status"] == "cancel_pending" else "open"
            return state
        return self._transition(
            client_order_id,
            "RECONCILIATION_" + outcome.upper(),
            transition,
        )

    def get_intent(self, client_order_id: str) -> dict[str, Any] | None:
        client_id = _validate_client_order_id(client_order_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state_json FROM perps_order_intents WHERE client_order_id=?",
                (client_id,),
            ).fetchone()
            return _validate_state(json.loads(row["state_json"])) if row else None
        finally:
            connection.close()

    def list_intents(self, *, statuses: tuple[str, ...] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if statuses is None:
            query = "SELECT state_json FROM perps_order_intents ORDER BY created_at, client_order_id LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            if not statuses or any(status not in ORDER_STATUSES for status in statuses):
                raise ValueError("statuses must be a non-empty tuple of known statuses")
            placeholders = ",".join("?" for _ in statuses)
            query = (
                "SELECT state_json FROM perps_order_intents WHERE json_extract(state_json,'$.status') "
                f"IN ({placeholders}) ORDER BY created_at, client_order_id LIMIT ?"
            )
            params = (*statuses, limit)
        connection = self._connect()
        try:
            return [
                _validate_state(json.loads(row["state_json"]))
                for row in connection.execute(query, params).fetchall()
            ]
        finally:
            connection.close()

    def list_events(self, client_order_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        client_id = _validate_client_order_id(client_order_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT event_id,event_type,state_json,created_at FROM perps_order_events "
                "WHERE client_order_id=? ORDER BY event_id LIMIT ?",
                (client_id, limit),
            ).fetchall()
            return [
                {
                    "event_id": int(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "created_at": str(row["created_at"]),
                    "state": _validate_state(json.loads(row["state_json"])),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def integrity_check(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            return {
                "integrity_ok": integrity == ["ok"],
                "integrity_results": integrity,
                "foreign_key_errors": len(foreign),
                "open_or_ambiguous_orders": connection.execute(
                    "SELECT COUNT(*) FROM perps_order_intents WHERE "
                    "json_extract(state_json,'$.status') IN "
                    "('submission_pending','submission_unknown','acknowledged_unreconciled',"
                    "'open','partially_filled','cancel_pending')"
                ).fetchone()[0],
                "execution": "none",
            }
        finally:
            connection.close()
