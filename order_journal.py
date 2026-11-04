"""Durable SQLite journal for the local, non-executing order lifecycle.

The journal commits submission_pending before any caller can perform network
I/O. It never connects to a venue, signs, or sends orders. A process restart
must explicitly recover pending attempts as ambiguous before reconciliation.
"""
from __future__ import annotations

import json
from copy import deepcopy
import math
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS order_exchange_index (
                    exchange_order_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(client_order_id)
                        REFERENCES order_intents(client_order_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_risk_snapshots (
                    snapshot_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    captured_at_utc TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_risk_sync_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
                    attempted_at_utc TEXT NOT NULL,
                    snapshot_id TEXT,
                    error_code TEXT,
                    CHECK (
                        (status = 'success' AND snapshot_id IS NOT NULL AND error_code IS NULL)
                        OR
                        (status = 'failed' AND snapshot_id IS NULL AND error_code IS NOT NULL)
                    )
                )
                """
            )
            # Backfill the immutable venue-ID index for databases created by an
            # earlier draft of this journal. Conflicts stop startup rather than
            # attaching one venue order to two local intents.
            rows = connection.execute(
                "SELECT client_order_id, state_json FROM order_intents"
            ).fetchall()
            for row in rows:
                try:
                    saved = json.loads(row["state_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("stored order intent is malformed; cannot rebuild venue index") from exc
                external_id = saved.get("exchange_order_id")
                if not external_id or saved.get("status") == "manual_review":
                    continue
                existing = connection.execute(
                    "SELECT client_order_id FROM order_exchange_index WHERE exchange_order_id = ?",
                    (external_id,),
                ).fetchone()
                if existing is not None and existing["client_order_id"] != row["client_order_id"]:
                    raise ValueError("exchange_order_id is already indexed to another local order")
                connection.execute(
                    "INSERT OR IGNORE INTO order_exchange_index(exchange_order_id, client_order_id) VALUES (?, ?)",
                    (external_id, row["client_order_id"]),
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
        exchange_order_id = state.get("exchange_order_id")
        if exchange_order_id and state.get("status") != "manual_review":
            existing = connection.execute(
                "SELECT client_order_id FROM order_exchange_index WHERE exchange_order_id = ?",
                (exchange_order_id,),
            ).fetchone()
            if existing is not None and existing["client_order_id"] != state["client_order_id"]:
                raise ValueError("exchange_order_id is already linked to another local order")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO order_exchange_index(exchange_order_id, client_order_id) VALUES (?, ?)",
                    (exchange_order_id, state["client_order_id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("local order already has a different exchange_order_id") from exc

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
        saved, _created = self.create_intent_with_status(intent)
        return saved

    @staticmethod
    def _stable_preflight_report(report: Any) -> Any:
        """Copy audit evidence while excluding only the naturally changing age counter."""
        if report is None:
            return None
        if not isinstance(report, dict):
            raise ValueError("preflight_report must be a dictionary")
        normalized = deepcopy(report)
        account_snapshot = normalized.get("account_risk_snapshot")
        if isinstance(account_snapshot, dict):
            account_snapshot.pop("age_seconds", None)
        return normalized

    def create_intent_with_status(
        self,
        intent: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist or reuse an intent and report whether it was created.

        The creation flag is computed inside the same SQLite write transaction,
        so concurrent callers cannot both claim that they created the same intent.
        """
        submission_policy(intent)
        if intent.get("status") != "prepared" or intent.get("submission_attempts") != 0:
            raise ValueError("only a new prepared order intent can be inserted")
        client_order_id = intent["client_order_id"]
        snapshot_id = intent.get("account_risk_snapshot_id")
        if snapshot_id is not None and (
            not isinstance(snapshot_id, str) or not snapshot_id.strip()
        ):
            raise ValueError("account_risk_snapshot_id must be a non-empty string")

        with self._transaction() as connection:
            if snapshot_id is not None:
                latest_snapshot = connection.execute(
                    """
                    SELECT snapshot_id
                    FROM account_risk_snapshots
                    ORDER BY snapshot_seq DESC
                    LIMIT 1
                    """
                ).fetchone()
                if latest_snapshot is None or latest_snapshot["snapshot_id"] != snapshot_id:
                    raise ValueError(
                        "account risk snapshot changed before intent commit"
                    )
                latest_sync = connection.execute(
                    """
                    SELECT status, snapshot_id
                    FROM account_risk_sync_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if (
                    latest_sync is None
                    or latest_sync["status"] != "success"
                    or latest_sync["snapshot_id"] != snapshot_id
                ):
                    raise ValueError(
                        "account risk sync is not the latest successful attempt"
                    )

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
                    "client_order_id", "token_id", "side", "limit_price", "size",
                    "market_condition_id", "account_risk_snapshot_id",
                )
                if any(existing.get(field) != intent.get(field) for field in immutable_fields):
                    raise ValueError("client_order_id already exists with different order terms")
                existing_preflight = self._stable_preflight_report(
                    existing.get("preflight_report")
                )
                requested_preflight = self._stable_preflight_report(
                    intent.get("preflight_report")
                )
                if existing_preflight != requested_preflight:
                    raise ValueError(
                        "client_order_id already exists with different preflight evidence"
                    )
                return existing, False

            serialized = self._serialize(intent)
            connection.execute(
                "INSERT INTO order_intents(client_order_id, state_json) VALUES (?, ?)",
                (client_order_id, serialized),
            )
            self._append_event(connection, intent, "intent_created")
            return intent.copy(), True


    @staticmethod
    def _snapshot_decimal(
        value: Any,
        field: str,
        *,
        allow_negative: bool = False,
    ) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise ValueError(f"account risk snapshot field {field} must be a finite decimal")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(
                f"account risk snapshot field {field} must be a finite decimal"
            ) from None
        if not parsed.is_finite() or (not allow_negative and parsed < 0):
            raise ValueError(f"account risk snapshot field {field} is outside valid bounds")
        return parsed

    @staticmethod
    def _validate_snapshot_asset_map(value: Any, field: str) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            raise ValueError(f"account risk snapshot field {field} must be a mapping")
        normalized: dict[str, list[str]] = {}
        for condition_id, assets in value.items():
            if not isinstance(condition_id, str) or not condition_id.strip():
                raise ValueError(f"account risk snapshot field {field} has an invalid condition ID")
            if not isinstance(assets, list):
                raise ValueError(f"account risk snapshot field {field} values must be lists")
            clean_assets: list[str] = []
            for asset_id in assets:
                if not isinstance(asset_id, str) or not asset_id.strip():
                    raise ValueError(f"account risk snapshot field {field} has an invalid asset ID")
                clean_assets.append(asset_id.strip())
            if len(clean_assets) != len(set(clean_assets)):
                raise ValueError(f"account risk snapshot field {field} contains duplicate asset IDs")
            normalized[condition_id.strip()] = sorted(clean_assets)
        return dict(sorted(normalized.items()))

    def save_account_risk_snapshot(self, snapshot: dict[str, Any]) -> str:
        """Append a complete, internally consistent, read-only account risk snapshot."""
        if not isinstance(snapshot, dict) or snapshot.get("snapshot_complete") is not True:
            raise ValueError("only complete account risk snapshots can be persisted")
        if snapshot.get("execution") != "none":
            raise ValueError("account risk snapshot must remain read-only")

        required = (
            "captured_at_utc",
            "collateral_decimals",
            "collateral_balance_base_units",
            "collateral_balance_usd",
            "open_buy_commitment_usd",
            "conservative_available_collateral_usd",
            "collateral_shortfall_usd",
            "current_open_exposure_usd",
            "open_position_value_usd",
            "open_position_cost_usd",
            "open_position_risk_exposure_usd",
            "open_unrealized_pnl_usd",
            "open_sell_remaining_shares",
            "current_open_positions",
            "current_open_orders",
            "open_position_assets_by_condition",
            "open_order_assets_by_condition",
        )
        if any(field not in snapshot for field in required):
            raise ValueError("account risk snapshot is missing required fields")

        captured_raw = snapshot["captured_at_utc"]
        if not isinstance(captured_raw, str) or not captured_raw.strip():
            raise ValueError("captured_at_utc must be a non-empty ISO timestamp")
        try:
            captured = datetime.fromisoformat(captured_raw)
        except ValueError:
            raise ValueError("captured_at_utc must be a valid ISO timestamp") from None
        if captured.tzinfo is None or captured.utcoffset() is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        captured_utc = captured.astimezone(timezone.utc)
        if (captured_utc - datetime.now(timezone.utc)).total_seconds() > 60:
            raise ValueError("captured_at_utc must not be more than 60 seconds in the future")

        decimals = snapshot["collateral_decimals"]
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
            raise ValueError("collateral_decimals must be an integer between 0 and 18")
        raw_balance = snapshot["collateral_balance_base_units"]
        if (
            not isinstance(raw_balance, str)
            or not raw_balance.isascii()
            or not raw_balance.isdigit()
        ):
            raise ValueError("collateral_balance_base_units must be a nonnegative integer string")

        count_fields = ("current_open_positions", "current_open_orders")
        for field in count_fields:
            value = snapshot[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"account risk snapshot field {field} must be a nonnegative integer")

        amount_fields = (
            "collateral_balance_usd",
            "open_buy_commitment_usd",
            "conservative_available_collateral_usd",
            "collateral_shortfall_usd",
            "current_open_exposure_usd",
            "open_position_value_usd",
            "open_position_cost_usd",
            "open_position_risk_exposure_usd",
            "open_sell_remaining_shares",
        )
        amounts = {
            field: self._snapshot_decimal(snapshot[field], field)
            for field in amount_fields
        }
        amounts["open_unrealized_pnl_usd"] = self._snapshot_decimal(
            snapshot["open_unrealized_pnl_usd"],
            "open_unrealized_pnl_usd",
            allow_negative=True,
        )

        base_unit_balance = Decimal(int(raw_balance)) / (Decimal(10) ** decimals)
        if amounts["collateral_balance_usd"] != base_unit_balance:
            raise ValueError("collateral_balance_usd does not match base-unit balance")
        expected_available = max(
            Decimal("0"),
            amounts["collateral_balance_usd"] - amounts["open_buy_commitment_usd"],
        )
        if amounts["conservative_available_collateral_usd"] != expected_available:
            raise ValueError("conservative available collateral is inconsistent with open BUY commitments")
        expected_shortfall = max(
            Decimal("0"),
            amounts["open_buy_commitment_usd"] - amounts["collateral_balance_usd"],
        )
        if amounts["collateral_shortfall_usd"] != expected_shortfall:
            raise ValueError("collateral shortfall is inconsistent with balance and BUY commitments")
        if amounts["open_position_risk_exposure_usd"] < max(
            amounts["open_position_value_usd"],
            amounts["open_position_cost_usd"],
        ):
            raise ValueError("position risk exposure cannot be below market value or cost")
        expected_exposure = (
            amounts["open_position_risk_exposure_usd"]
            + amounts["open_buy_commitment_usd"]
        )
        if amounts["current_open_exposure_usd"] != expected_exposure:
            raise ValueError("current open exposure is inconsistent with positions and BUY commitments")

        state = dict(snapshot)
        state["captured_at_utc"] = captured_utc.isoformat()
        state["open_position_assets_by_condition"] = self._validate_snapshot_asset_map(
            snapshot["open_position_assets_by_condition"],
            "open_position_assets_by_condition",
        )
        state["open_order_assets_by_condition"] = self._validate_snapshot_asset_map(
            snapshot["open_order_assets_by_condition"],
            "open_order_assets_by_condition",
        )
        serialized = json.dumps(
            state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_id = str(uuid4())
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO account_risk_snapshots(
                    snapshot_id, captured_at_utc, snapshot_json
                ) VALUES (?, ?, ?)
                """,
                (snapshot_id, state["captured_at_utc"], serialized),
            )
            # A delayed successful fetch must never overwrite a newer failed
            # attempt. ISO UTC timestamps are normalized before lexical compare.
            connection.execute(
                """
                INSERT INTO account_risk_sync_state(
                    singleton_id, status, attempted_at_utc, snapshot_id, error_code
                ) VALUES (1, 'success', ?, ?, NULL)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    status = excluded.status,
                    attempted_at_utc = excluded.attempted_at_utc,
                    snapshot_id = excluded.snapshot_id,
                    error_code = NULL
                WHERE excluded.attempted_at_utc >= account_risk_sync_state.attempted_at_utc
                """,
                (state["captured_at_utc"], snapshot_id),
            )
        return snapshot_id

    def record_account_risk_snapshot_failure(
        self,
        *,
        error_code: str,
        attempted_at: datetime | None = None,
    ) -> None:
        """Persist a sanitized failed sync attempt, immediately invalidating prior freshness."""
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code):
            raise ValueError("error_code must be a short sanitized identifier")
        timestamp = attempted_at or datetime.now(timezone.utc)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("attempted_at must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if (timestamp - now).total_seconds() > 60:
            raise ValueError("attempted_at must not be more than 60 seconds in the future")
        attempted_at_utc = timestamp.isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO account_risk_sync_state(
                    singleton_id, status, attempted_at_utc, snapshot_id, error_code
                ) VALUES (1, 'failed', ?, NULL, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    status = excluded.status,
                    attempted_at_utc = excluded.attempted_at_utc,
                    snapshot_id = NULL,
                    error_code = excluded.error_code
                WHERE excluded.attempted_at_utc >= account_risk_sync_state.attempted_at_utc
                """,
                (attempted_at_utc, error_code),
            )

    def get_latest_account_risk_sync_state(
        self,
        *,
        max_age_seconds: float = 300.0,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest account-risk sync attempt; a failed attempt is never fresh."""
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(float(max_age_seconds))
            or float(max_age_seconds) <= 0
        ):
            raise ValueError("max_age_seconds must be positive and finite")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT status, attempted_at_utc, snapshot_id, error_code
                FROM account_risk_sync_state
                WHERE singleton_id = 1
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            attempted = datetime.fromisoformat(row["attempted_at_utc"])
        except (TypeError, ValueError):
            raise ValueError("stored account-risk sync state timestamp is malformed") from None
        if attempted.tzinfo is None or attempted.utcoffset() is None:
            raise ValueError("stored account-risk sync state timestamp is not timezone-aware")
        age = (current - attempted.astimezone(timezone.utc)).total_seconds()
        if age < -60:
            raise ValueError("stored account-risk sync state timestamp is in the future")
        status = row["status"]
        fresh = status == "success" and age <= float(max_age_seconds)
        return {
            "status": status,
            "attempted_at_utc": attempted.astimezone(timezone.utc).isoformat(),
            "age_seconds": max(0.0, age),
            "snapshot_id": row["snapshot_id"],
            "error_code": row["error_code"],
            "fresh": fresh,
        }

    def get_latest_account_risk_snapshot(
        self,
        *,
        max_age_seconds: float = 300,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return latest account risk snapshot with an explicit freshness verdict."""
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(float(max_age_seconds))
            or float(max_age_seconds) <= 0
        ):
            raise ValueError("max_age_seconds must be positive and finite")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(timezone.utc)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT snapshot_id, captured_at_utc, snapshot_json
                FROM account_risk_snapshots
                ORDER BY snapshot_seq DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot_json"])
            captured = datetime.fromisoformat(row["captured_at_utc"])
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("stored account risk snapshot is malformed") from exc
        if captured.tzinfo is None or captured.utcoffset() is None:
            raise ValueError("stored account risk snapshot timestamp is not timezone-aware")
        age = (current - captured.astimezone(timezone.utc)).total_seconds()
        if age < -60:
            raise ValueError("stored account risk snapshot timestamp is in the future")
        snapshot["snapshot_id"] = row["snapshot_id"]
        snapshot["age_seconds"] = max(0.0, age)
        snapshot["fresh"] = age <= float(max_age_seconds)
        return snapshot

    def list_account_risk_snapshots(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List recent immutable account risk snapshots for audit and diagnostics."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT snapshot_id, captured_at_utc, snapshot_json
                FROM account_risk_snapshots
                ORDER BY snapshot_seq DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("stored account risk snapshot is malformed") from exc
            snapshot["snapshot_id"] = row["snapshot_id"]
            snapshots.append(snapshot)
        return snapshots

    def find_client_order_id_by_exchange_order_id(self, exchange_order_id: str) -> str | None:
        """Return the local intent mapped to a venue order ID, if known."""
        if not isinstance(exchange_order_id, str) or not exchange_order_id.strip():
            raise ValueError("exchange_order_id must be a non-empty string")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT client_order_id FROM order_exchange_index WHERE exchange_order_id = ?",
                (exchange_order_id.strip(),),
            ).fetchone()
            return row["client_order_id"] if row is not None else None
        finally:
            connection.close()

    def list_active_intents(self) -> list[dict[str, Any]]:
        """List locally active orders for read-only REST snapshot comparison."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT state_json FROM order_intents
                ORDER BY created_at, client_order_id
                """
            ).fetchall()
        finally:
            connection.close()
        active: list[dict[str, Any]] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("stored order intent is malformed; refusing active-order listing") from exc
            submission_policy(state)
            if state["status"] in {"open", "partially_filled"}:
                active.append(state)
        return active

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
        observed_token_id: str | None = None,
        observed_side: str | None = None,
        observed_original_size: float | None = None,
        observed_price: float | None = None,
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
                observed_token_id=observed_token_id,
                observed_side=observed_side,
                observed_original_size=observed_original_size,
                observed_price=observed_price,
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
