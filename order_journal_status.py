"""Read-only, sanitized local order-journal status for operator visibility.

This module never creates a journal database, reads credentials, contacts the
venue, signs, submits, or cancels an order. Missing/corrupt/incomplete state
is reported as unavailable rather than as an empty or healthy order book.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from order_lifecycle import submission_policy


_MAX_INTENTS = 2000
_MAX_DISPLAY = 100
_MAX_INTENT_JSON_BYTES = 64_000


class OrderJournalStatusError(RuntimeError):
    """The local order journal could not be read and validated completely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


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

        active_status_counts = {"open": 0, "partially_filled": 0}
        unresolved_status_counts = {
            "submission_pending": 0,
            "submission_unknown": 0,
            "manual_review": 0,
        }
        unresolved_reason_counts: dict[str, int] = {}
        prepared = filled = cancelled = rejected = 0
        for row in rows:
            stored_client_id = _safe_identifier(row["client_order_id"], maximum=128)
            try:
                raw_state = row["state_json"]
                if not isinstance(raw_state, str) or len(raw_state.encode("utf-8")) > _MAX_INTENT_JSON_BYTES:
                    raise ValueError("intent state exceeds the local status reader limit")
                state = json.loads(
                    raw_state,
                    object_pairs_hook=_unique_object,
                )
                if not isinstance(state, dict):
                    raise ValueError("intent state is not an object")
                policy = submission_policy(state)
            except Exception:
                raise OrderJournalStatusError("order_journal_intent_invalid") from None
            client_id = _safe_identifier(state.get("client_order_id"), maximum=128)
            if client_id != stored_client_id:
                raise OrderJournalStatusError("order_journal_client_id_mismatch")

            status = policy["status"]
            if status in active_status_counts:
                active_status_counts[status] += 1
            elif status in unresolved_status_counts:
                unresolved_status_counts[status] += 1
                reason = state.get("last_reason_code")
                if isinstance(reason, str) and reason:
                    unresolved_reason_counts[reason] = unresolved_reason_counts.get(reason, 0) + 1
            elif status == "prepared":
                prepared += 1
            elif status == "filled":
                filled += 1
            elif status == "cancelled":
                cancelled += 1
            elif status == "rejected":
                rejected += 1

        total = len(rows)
        active_count = sum(active_status_counts.values())
        unresolved_count = sum(unresolved_status_counts.values())
        return {
            "status": "healthy" if unresolved_count == 0 else "degraded",
            "available": True,
            "snapshot_complete": True,
            "source": "local_order_journal_only",
            "venue_open_orders_checked": False,
            "venue_status_verified": False,
            "active_order_count": active_count,
            "active_order_status_counts": active_status_counts,
            "unresolved_order_count": unresolved_count,
            "unresolved_order_status_counts": unresolved_status_counts,
            "unresolved_reason_counts": unresolved_reason_counts,
            "prepared_intent_count": prepared,
            "filled_order_count": filled,
            "cancelled_order_count": cancelled,
            "rejected_order_count": rejected,
            "total_intent_count": total,
            "order_identifiers_exposed": False,
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
