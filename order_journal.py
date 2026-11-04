"""Durable SQLite journal for the local, non-executing order lifecycle.

The journal commits submission_pending before any caller can perform network
I/O. It never connects to a venue, signs, or sends orders. A process restart
must explicitly recover pending attempts as ambiguous before reconciliation.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from order_lifecycle import (
    begin_submission as transition_begin_submission,
    reconcile_submission as transition_reconcile_submission,
    record_submission_response as transition_record_submission_response,
    record_order_observation as transition_record_order_observation,
    submission_policy,
)


class OrderJournal:
    """Append-only event history plus an atomic latest-state SQLite record."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            raise ValueError("OrderJournal requires a durable file-backed SQLite database")
        self.database_path = str(Path(database_path).expanduser())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=15.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    client_order_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(client_order_id)
                        REFERENCES order_intents(client_order_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_order_events_client_order_id
                ON order_events(client_order_id, event_id)
                """
            )
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _serialize(state: dict[str, Any]) -> str:
        submission_policy(state)  # Validate every state before persisting it.
        return json.dumps(
            state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _load(
        connection: sqlite3.Connection,
        client_order_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT state_json FROM order_intents WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored order intent is malformed; refusing to reset it") from exc
        submission_policy(state)
        return state

    @staticmethod
    def _save(
        connection: sqlite3.Connection,
        state: dict[str, Any],
    ) -> None:
        serialized = OrderJournal._serialize(state)
        connection.execute(
            """
            UPDATE order_intents
            SET state_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE client_order_id = ?
            """,
            (serialized, state["client_order_id"]),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        state: dict[str, Any],
        event_type: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_events(client_order_id, event_type, state_json)
            VALUES (?, ?, ?)
            """,
            (
                state["client_order_id"],
                event_type,
                OrderJournal._serialize(state),
            ),
        )

    def create_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Persist a prepared intent; replaying identical terms is idempotent."""
        submission_policy(intent)
        if intent.get("status") != "prepared" or intent.get("submission_attempts") != 0:
            raise ValueError("only a new prepared order intent can be inserted")
        client_order_id = intent["client_order_id"]
        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT state_json FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if existing_row is not None:
                try:
                    existing = json.loads(existing_row["state_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "stored order intent is malformed; refusing to overwrite it"
                    ) from exc
                submission_policy(existing)
                immutable_fields = (
                    "client_order_id", "token_id", "side", "limit_price", "size"
                )
                if any(existing.get(field) != intent.get(field) for field in immutable_fields):
                    raise ValueError("client_order_id already exists with different order terms")
                return existing

            serialized = self._serialize(intent)
            connection.execute(
                "INSERT INTO order_intents(client_order_id, state_json) VALUES (?, ?)",
                (client_order_id, serialized),
            )
            self._append_event(connection, intent, "intent_created")
            return intent.copy()

    def get_intent(self, client_order_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state_json FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored order intent is malformed; refusing to reset it") from exc
        submission_policy(state)
        return state

    def _transition(
        self,
        client_order_id: str,
        event_type: str,
        transition: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            current = self._load(connection, client_order_id)
            updated = transition(current)
            self._save(connection, updated)
            self._append_event(connection, updated, event_type)
            return updated

    def begin_submission(self, client_order_id: str) -> dict[str, Any]:
        """Commit the one permitted attempt before the caller makes a request."""
        return self._transition(
            client_order_id,
            "submission_started",
            transition_begin_submission,
        )

    def record_submission_response(
        self,
        client_order_id: str,
        *,
        outcome: str,
        exchange_order_id: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            client_order_id,
            f"submission_response_{outcome}",
            lambda state: transition_record_submission_response(
                state,
                outcome=outcome,
                exchange_order_id=exchange_order_id,
                reason_code=reason_code,
            ),
        )

    def reconcile_submission(
        self,
        client_order_id: str,
        *,
        outcome: str,
        matched_client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        filled_size: float | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            client_order_id,
            f"reconciliation_{outcome}",
            lambda state: transition_reconcile_submission(
                state,
                outcome=outcome,
                matched_client_order_id=matched_client_order_id,
                exchange_order_id=exchange_order_id,
                filled_size=filled_size,
                reason_code=reason_code,
            ),
        )


    def record_order_observation(
        self,
        client_order_id: str,
        *,
        outcome: str,
        matched_client_order_id: str,
        exchange_order_id: str,
        cumulative_filled_size: float,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        """Persist a monotonic venue snapshot; duplicate snapshots are no-ops."""
        allowed = {"open", "partially_filled", "filled", "cancelled", "rejected"}
        if outcome not in allowed:
            raise ValueError(f"outcome must be one of {sorted(allowed)}")
        with self._transaction() as connection:
            current = self._load(connection, client_order_id)
            updated = transition_record_order_observation(
                current,
                outcome=outcome,
                matched_client_order_id=matched_client_order_id,
                exchange_order_id=exchange_order_id,
                cumulative_filled_size=cumulative_filled_size,
                reason_code=reason_code,
            )
            if updated == current:
                return current
            self._save(connection, updated)
            self._append_event(connection, updated, f"order_observation_{outcome}")
            return updated

    def recover_interrupted_submissions(self) -> int:
        """Mark crash-interrupted pending attempts ambiguous; never reset to prepared.

        Call only during exclusive process recovery, before starting a new
        execution worker. Concurrent live workers must coordinate ownership.
        """
        recovered = 0
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT client_order_id, state_json
                FROM order_intents
                ORDER BY client_order_id
                """
            ).fetchall()
            for row in rows:
                try:
                    state = json.loads(row["state_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "stored order intent is malformed; refusing recovery"
                    ) from exc
                submission_policy(state)
                if state["status"] != "submission_pending":
                    continue
                unknown = transition_record_submission_response(
                    state,
                    outcome="ambiguous",
                    reason_code="process_restart_during_submission",
                )
                self._save(connection, unknown)
                self._append_event(connection, unknown, "interrupted_submission_recovered")
                recovered += 1
        return recovered

    def list_events(self, client_order_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT event_id, event_type, state_json, created_at
                FROM order_events
                WHERE client_order_id = ?
                ORDER BY event_id
                """,
                (client_order_id,),
            ).fetchall()
        finally:
            connection.close()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("stored order event is malformed") from exc
            submission_policy(state)
            events.append({
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "state": state,
            })
        return events
