"""Read-only, sanitized local order-journal status for operator visibility.

This module never creates a journal database, reads credentials, contacts the
venue, signs, submits, or cancels an order. Missing/corrupt/incomplete state
is reported as unavailable rather than as an empty or healthy order book.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from order_lifecycle import submission_policy


_MAX_INTENTS = 2000
_MAX_DISPLAY = 100


class OrderJournalStatusError(RuntimeError):
    """The local order journal could not be read and validated completely."""


def _safe_identifier(value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OrderJournalStatusError("order_journal_identifier_invalid")
    return value


def read_local_order_journal_status(
    database_path: str | Path,
    *,
    maximum_intents: int = _MAX_INTENTS,
) -> dict[str, Any]:
    """Read and validate every local intent using SQLite read-only mode."""
    if (
        isinstance(maximum_intents, bool)
        or not isinstance(maximum_intents, int)
        or not 1 <= maximum_intents <= _MAX_INTENTS
    ):
        raise ValueError("maximum_intents must be between 1 and 2000")

    candidate = Path(database_path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise OrderJournalStatusError("order_journal_missing_or_unsafe")

    connection: sqlite3.Connection | None = None
    try:
        resolved = candidate.resolve(strict=True)
        uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(order_intents)").fetchall()
        }
        if not {"client_order_id", "state_json", "created_at"}.issubset(columns):
            raise OrderJournalStatusError("order_journal_schema_incomplete")

        rows = connection.execute(
            "SELECT client_order_id,state_json,created_at FROM order_intents "
            "ORDER BY created_at,client_order_id LIMIT ?",
            (maximum_intents + 1,),
        ).fetchall()
        if len(rows) > maximum_intents:
            raise OrderJournalStatusError("order_journal_intent_limit_exceeded")

        active: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        prepared = filled = cancelled = rejected = 0
        for row in rows:
            stored_client_id = _safe_identifier(row["client_order_id"], maximum=128)
            try:
                import json
                state = json.loads(row["state_json"])
                if not isinstance(state, dict):
                    raise ValueError("intent state is not an object")
                policy = submission_policy(state)
            except Exception:
                raise OrderJournalStatusError("order_journal_intent_invalid") from None
            client_id = _safe_identifier(state.get("client_order_id"), maximum=128)
            if client_id != stored_client_id:
                raise OrderJournalStatusError("order_journal_client_id_mismatch")

            status = policy["status"]
            summary = {
                "client_order_id": client_id,
                "exchange_order_id": (
                    _safe_identifier(state["exchange_order_id"])
                    if state.get("exchange_order_id") is not None else None
                ),
                "status": status,
                "side": state["side"],
                "filled_size": str(state["filled_size"]),
                "size": str(state["size"]),
                "last_reason_code": state.get("last_reason_code"),
                "reconciliation_required": policy["reconciliation_required"],
                "automatic_retry_permitted": False,
                "execution": "none",
            }

            if status in {"open", "partially_filled"}:
                active.append(summary)
            elif status in {"submission_pending", "submission_unknown", "manual_review"}:
                unresolved.append(summary)
            elif status == "prepared":
                prepared += 1
            elif status == "filled":
                filled += 1
            elif status == "cancelled":
                cancelled += 1
            elif status == "rejected":
                rejected += 1

        total = len(rows)
        return {
            "status": "healthy" if not unresolved else "degraded",
            "available": True,
            "snapshot_complete": True,
            "source": "local_order_journal_only",
            "venue_open_orders_checked": False,
            "venue_status_verified": False,
            "active_order_count": len(active),
            "unresolved_order_count": len(unresolved),
            "prepared_intent_count": prepared,
            "filled_order_count": filled,
            "cancelled_order_count": cancelled,
            "rejected_order_count": rejected,
            "total_intent_count": total,
            "active_orders": active[:_MAX_DISPLAY],
            "unresolved_orders": unresolved[:_MAX_DISPLAY],
            "active_orders_truncated": len(active) > _MAX_DISPLAY,
            "unresolved_orders_truncated": len(unresolved) > _MAX_DISPLAY,
            "order_submission_allowed": False,
            "order_cancellation_allowed": False,
            "automatic_retry_permitted": False,
            "live_execution_enabled": False,
            "execution": "none",
        }
    except OrderJournalStatusError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise OrderJournalStatusError("order_journal_read_or_validation_failed") from None
    finally:
        if connection is not None:
            connection.close()
