"""Restore a standalone SQLite order-journal backup into a new, unused path.

The source is opened read-only and immutable. The destination is built and
validated in a temporary file in the destination directory, then published
with an atomic no-overwrite hard link. This command never overwrites a live
journal or an existing destination.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from order_lifecycle import submission_policy


_REQUIRED_COLUMNS = {
    "order_intents": {"client_order_id", "state_json", "created_at", "updated_at"},
    "order_events": {"event_id", "client_order_id", "event_type", "state_json", "created_at"},
    "order_exchange_index": {"exchange_order_id", "client_order_id"},
    "account_risk_snapshots": {"snapshot_seq", "snapshot_id", "captured_at_utc", "snapshot_json"},
    "account_risk_sync_state": {
        "singleton_id", "status", "attempted_at_utc", "snapshot_id", "error_code"
    },
}
_JSON_COLUMNS = {
    "order_intents": "state_json",
    "order_events": "state_json",
    "account_risk_snapshots": "snapshot_json",
}


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/:')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _validate_event_transition(
    event_type: str,
    previous_state: dict[str, Any] | None,
    state: dict[str, Any],
) -> None:
    """Validate event label, target status, and legal predecessor status."""
    status = state.get("status")
    fixed_targets = {
        "intent_created": {"prepared"},
        "submission_started": {"submission_pending"},
        "submission_response_accepted": {"open"},
        "submission_response_rejected": {"rejected"},
        "submission_response_ambiguous": {"submission_unknown"},
        "interrupted_submission_recovered": {"submission_unknown"},
    }
    if event_type in fixed_targets:
        allowed_targets = fixed_targets[event_type]
    elif event_type.startswith("reconciliation_"):
        outcome = event_type.removeprefix("reconciliation_")
        targets = {
            "not_found": {"submission_unknown"},
            "lookup_error": {"submission_unknown"},
            "found_open": {"open"},
            "found_partially_filled": {"partially_filled"},
            "found_filled": {"filled"},
            "found_cancelled": {"cancelled"},
            "found_rejected": {"rejected"},
        }
        allowed_targets = targets.get(outcome, set())
        if outcome.startswith("found_"):
            allowed_targets = allowed_targets | {"manual_review"}
    elif event_type.startswith("order_observation_"):
        outcome = event_type.removeprefix("order_observation_")
        targets = {
            "open": {"open", "partially_filled", "filled"},
            "partially_filled": {"partially_filled"},
            "filled": {"filled"},
            "cancelled": {"cancelled"},
            "rejected": {"rejected"},
        }
        allowed_targets = targets.get(outcome, set()) | {"manual_review"}
    else:
        allowed_targets = set()

    if not allowed_targets or status not in allowed_targets:
        raise ValueError("backup audit event type does not match its lifecycle state")

    if previous_state is None:
        if event_type != "intent_created" or status != "prepared":
            raise ValueError("backup audit history must begin with a prepared intent")
        if state.get("submission_attempts") != 0 or state.get("reconciliation_attempts") != 0:
            raise ValueError("backup initial intent has nonzero lifecycle counters")
        if state.get("exchange_order_id") is not None:
            raise ValueError("backup initial intent already has an exchange order ID")
        return

    previous_status = previous_state.get("status")
    predecessor_by_event = {
        "submission_started": {"prepared"},
        "submission_response_accepted": {"submission_pending"},
        "submission_response_rejected": {"submission_pending"},
        "submission_response_ambiguous": {"submission_pending"},
        "interrupted_submission_recovered": {"submission_pending"},
    }
    if event_type.startswith("reconciliation_"):
        valid_predecessors = {"submission_unknown"}
    elif event_type.startswith("order_observation_"):
        valid_predecessors = {
            "open", "partially_filled", "filled", "cancelled", "rejected"
        }
    else:
        valid_predecessors = predecessor_by_event.get(event_type, set())
    if previous_status not in valid_predecessors:
        raise ValueError("backup audit event sequence contains an illegal state transition")


def _snapshot_decimal(
    value: Any,
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"backup account-risk field {field} is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"backup account-risk field {field} is invalid") from None
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise ValueError(f"backup account-risk field {field} is outside valid bounds")
    return parsed


def _validate_account_risk_snapshot_document(
    snapshot: Any,
    captured_at_column: Any,
) -> None:
    """Apply the same structural and arithmetic safety checks used at journal write."""
    if not isinstance(snapshot, dict) or snapshot.get("snapshot_complete") is not True:
        raise ValueError("backup contains an incomplete account-risk snapshot")
    if snapshot.get("execution") != "none":
        raise ValueError("backup account-risk snapshot is not read-only")

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
        raise ValueError("backup account-risk snapshot is missing required fields")

    captured_raw = snapshot["captured_at_utc"]
    if not isinstance(captured_raw, str) or not captured_raw.strip():
        raise ValueError("backup account-risk snapshot timestamp is invalid")
    try:
        captured = datetime.fromisoformat(captured_raw)
        column_captured = datetime.fromisoformat(str(captured_at_column))
    except (TypeError, ValueError):
        raise ValueError("backup account-risk snapshot timestamp is malformed") from None
    if (
        captured.tzinfo is None or captured.utcoffset() is None
        or column_captured.tzinfo is None or column_captured.utcoffset() is None
    ):
        raise ValueError("backup account-risk snapshot timestamps must be timezone-aware")
    captured_utc = captured.astimezone(timezone.utc)
    if captured_utc != column_captured.astimezone(timezone.utc):
        raise ValueError("backup account-risk snapshot JSON timestamp differs from journal timestamp")
    if (captured_utc - datetime.now(timezone.utc)).total_seconds() > 60:
        raise ValueError("backup account-risk snapshot timestamp is in the future")

    decimals = snapshot["collateral_decimals"]
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
        raise ValueError("backup account-risk collateral decimals are invalid")
    raw_balance = snapshot["collateral_balance_base_units"]
    if (
        not isinstance(raw_balance, str)
        or not raw_balance.isascii()
        or not raw_balance.isdigit()
    ):
        raise ValueError("backup account-risk collateral base units are invalid")

    for field in ("current_open_positions", "current_open_orders"):
        value = snapshot[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"backup account-risk count {field} is invalid")

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
    amounts = {field: _snapshot_decimal(snapshot[field], field) for field in amount_fields}
    amounts["open_unrealized_pnl_usd"] = _snapshot_decimal(
        snapshot["open_unrealized_pnl_usd"],
        "open_unrealized_pnl_usd",
        allow_negative=True,
    )

    base_balance = Decimal(int(raw_balance)) / (Decimal(10) ** decimals)
    if amounts["collateral_balance_usd"] != base_balance:
        raise ValueError("backup account-risk collateral balance does not match base units")
    expected_available = max(
        Decimal("0"),
        amounts["collateral_balance_usd"] - amounts["open_buy_commitment_usd"],
    )
    if amounts["conservative_available_collateral_usd"] != expected_available:
        raise ValueError("backup account-risk available collateral is inconsistent")
    expected_shortfall = max(
        Decimal("0"),
        amounts["open_buy_commitment_usd"] - amounts["collateral_balance_usd"],
    )
    if amounts["collateral_shortfall_usd"] != expected_shortfall:
        raise ValueError("backup account-risk collateral shortfall is inconsistent")
    if amounts["open_position_risk_exposure_usd"] < max(
        amounts["open_position_value_usd"],
        amounts["open_position_cost_usd"],
    ):
        raise ValueError("backup account-risk position exposure is below value or cost")
    if amounts["current_open_exposure_usd"] != (
        amounts["open_position_risk_exposure_usd"] + amounts["open_buy_commitment_usd"]
    ):
        raise ValueError("backup account-risk total exposure is inconsistent")

    for field in ("open_position_assets_by_condition", "open_order_assets_by_condition"):
        mapping = snapshot[field]
        if not isinstance(mapping, dict):
            raise ValueError(f"backup account-risk field {field} must be a mapping")
        for condition_id, assets in mapping.items():
            if not isinstance(condition_id, str) or not condition_id.strip():
                raise ValueError(f"backup account-risk field {field} has an invalid condition ID")
            if not isinstance(assets, list):
                raise ValueError(f"backup account-risk field {field} values must be lists")
            if any(not isinstance(asset, str) or not asset.strip() for asset in assets):
                raise ValueError(f"backup account-risk field {field} has an invalid asset ID")
            if len(assets) != len(set(assets)):
                raise ValueError(f"backup account-risk field {field} has duplicate asset IDs")


def _validate_database(connection: sqlite3.Connection) -> dict[str, int]:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise sqlite3.DatabaseError("backup integrity check failed")

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise sqlite3.IntegrityError("backup foreign-key check failed")

    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - existing_tables)
    if missing_tables:
        raise ValueError("backup is missing required journal tables")

    counts: dict[str, int] = {}
    for table, required_columns in _REQUIRED_COLUMNS.items():
        actual_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required_columns.issubset(actual_columns):
            raise ValueError("backup journal table schema is incomplete")
        counts[table] = int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )


    # The venue-ID lookup index is part of order recovery, not a disposable
    # cache. Verify it agrees with the immutable order state in both directions.
    for row in connection.execute(
        "SELECT client_order_id, state_json FROM order_intents"
    ):
        try:
            state = json.loads(row["state_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("backup contains malformed order state") from None
        if not isinstance(state, dict) or state.get("client_order_id") != row["client_order_id"]:
            raise ValueError("backup order state identity does not match its journal key")
        try:
            submission_policy(state)
        except (TypeError, ValueError):
            raise ValueError("backup contains an invalid order lifecycle state") from None
        exchange_id = state.get("exchange_order_id")
        if exchange_id:
            index_row = connection.execute(
                "SELECT client_order_id FROM order_exchange_index WHERE exchange_order_id = ?",
                (exchange_id,),
            ).fetchone()
            if index_row is None:
                if state.get("status") != "manual_review":
                    raise ValueError("backup is missing a required venue-order index entry")
            elif index_row["client_order_id"] != row["client_order_id"]:
                raise ValueError("backup venue-order index is inconsistent with order state")

    # Audit snapshots are recovery evidence too. Validate every state with
    # the same transition invariants as the live journal and ensure the final
    # event for each intent is exactly the current durable state.
    event_cursor = connection.execute(
        """
        SELECT
            i.client_order_id AS client_order_id,
            i.state_json AS current_state_json,
            e.state_json AS event_state_json,
            e.event_type AS event_type,
            e.event_id AS event_id
        FROM order_intents AS i
        LEFT JOIN order_events AS e
            ON e.client_order_id = i.client_order_id
        ORDER BY i.client_order_id, e.event_id
        """
    )
    active_client_id: str | None = None
    latest_event_state: dict[str, Any] | None = None
    previous_event_state: dict[str, Any] | None = None
    current_state: dict[str, Any] | None = None

    def verify_event_group() -> None:
        if active_client_id is None:
            return
        if latest_event_state is None or current_state is None:
            raise ValueError("backup intent is missing its audit event history")
        if latest_event_state != current_state:
            raise ValueError("backup latest audit event does not match current order state")

    for row in event_cursor:
        client_id = row["client_order_id"]
        if client_id != active_client_id:
            verify_event_group()
            active_client_id = client_id
            try:
                current_state = json.loads(row["current_state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("backup contains malformed current order state") from None
            if not isinstance(current_state, dict) or current_state.get("client_order_id") != client_id:
                raise ValueError("backup current order state identity is inconsistent")
            try:
                submission_policy(current_state)
            except (TypeError, ValueError):
                raise ValueError("backup contains an invalid current order lifecycle state") from None
            latest_event_state = None
            previous_event_state = None

        if row["event_id"] is None:
            raise ValueError("backup intent is missing its audit event history")
        try:
            event_state = json.loads(row["event_state_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("backup contains malformed order audit state") from None
        if not isinstance(event_state, dict) or event_state.get("client_order_id") != client_id:
            raise ValueError("backup order audit state identity is inconsistent")
        try:
            submission_policy(event_state)
        except (TypeError, ValueError):
            raise ValueError("backup contains an invalid order audit lifecycle state") from None
        event_type = row["event_type"]
        if not isinstance(event_type, str):
            raise ValueError("backup audit event type is missing or invalid")
        _validate_event_transition(event_type, previous_event_state, event_state)
        if previous_event_state is not None:
            immutable_fields = (
                "client_order_id", "token_id", "side", "limit_price", "size",
                "market_condition_id", "account_risk_snapshot_id", "preflight_report",
            )
            if any(
                previous_event_state.get(field) != event_state.get(field)
                for field in immutable_fields
            ):
                raise ValueError("backup audit history changes immutable order terms or preflight evidence")
            if event_state["submission_attempts"] < previous_event_state["submission_attempts"]:
                raise ValueError("backup audit submission attempt count moves backwards")
            if event_state["reconciliation_attempts"] < previous_event_state["reconciliation_attempts"]:
                raise ValueError("backup audit reconciliation count moves backwards")
            if Decimal(event_state["filled_size"]) < Decimal(previous_event_state["filled_size"]):
                raise ValueError("backup audit cumulative filled size moves backwards")
        previous_event_state = event_state
        latest_event_state = event_state

    verify_event_group()

    for row in connection.execute(
        "SELECT exchange_order_id, client_order_id FROM order_exchange_index"
    ):
        intent_row = connection.execute(
            "SELECT state_json FROM order_intents WHERE client_order_id = ?",
            (row["client_order_id"],),
        ).fetchone()
        if intent_row is None:
            raise ValueError("backup venue-order index refers to a missing intent")
        try:
            indexed_state = json.loads(intent_row["state_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("backup contains malformed indexed order state") from None
        if not isinstance(indexed_state, dict) or indexed_state.get("exchange_order_id") != row["exchange_order_id"]:
            raise ValueError("backup venue-order index points to a conflicting intent")

    sync_row = connection.execute(
        "SELECT status, snapshot_id, error_code FROM account_risk_sync_state WHERE singleton_id = 1"
    ).fetchone()
    if sync_row is not None:
        if sync_row["status"] == "success":
            if not sync_row["snapshot_id"] or sync_row["error_code"] is not None:
                raise ValueError("backup successful account sync state is malformed")
            snapshot_row = connection.execute(
                "SELECT captured_at_utc FROM account_risk_snapshots WHERE snapshot_id = ?",
                (sync_row["snapshot_id"],),
            ).fetchone()
            if snapshot_row is None:
                raise ValueError("backup account sync state references an unknown snapshot")
            latest_snapshot = connection.execute(
                "SELECT snapshot_id FROM account_risk_snapshots "
                "ORDER BY captured_at_utc DESC, snapshot_seq DESC LIMIT 1"
            ).fetchone()
            if latest_snapshot is None or latest_snapshot["snapshot_id"] != sync_row["snapshot_id"]:
                raise ValueError("backup successful account sync state is not bound to the latest snapshot")
        elif sync_row["status"] == "failed":
            if sync_row["snapshot_id"] is not None or not sync_row["error_code"]:
                raise ValueError("backup failed account sync state is malformed")
        else:
            raise ValueError("backup account sync state has an unknown status")

    # Parse every persisted state document before publishing the restore.
    # Reject JSON extensions such as NaN/Infinity and non-object payloads.
    for table, column in _JSON_COLUMNS.items():
        cursor = connection.execute(f"SELECT {column} FROM {table}")
        while True:
            batch = cursor.fetchmany(256)
            if not batch:
                break
            for row in batch:
                raw = row[0]
                if not isinstance(raw, str):
                    raise ValueError("backup contains a non-text state document")
                try:
                    value: Any = json.loads(
                        raw,
                        parse_constant=lambda _value: (_ for _ in ()).throw(
                            ValueError("non-finite JSON constant")
                        ),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError("backup contains malformed state JSON") from None
                if not isinstance(value, dict):
                    raise ValueError("backup state document must be a JSON object")
    return counts


def restore_order_journal(
    source_path: str | Path,
    destination_path: str | Path,
) -> dict[str, Any]:
    """Validate a standalone backup and atomically restore it to a new path."""
    raw_source = str(source_path)
    raw_destination = str(destination_path)
    if not raw_source or raw_source == ":memory:":
        raise ValueError("source must be a standalone backup file")
    if not raw_destination or raw_destination == ":memory:":
        raise ValueError("destination must be a durable file path")

    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be a regular backup file")
    destination = Path(destination_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve(strict=False)

    protected_source_paths = {
        source,
        Path(f"{source}-wal").resolve(strict=False),
        Path(f"{source}-shm").resolve(strict=False),
        Path(f"{source}-journal").resolve(strict=False),
    }
    protected_destination_paths = {
        resolved_destination,
        Path(f"{resolved_destination}-wal").resolve(strict=False),
        Path(f"{resolved_destination}-shm").resolve(strict=False),
        Path(f"{resolved_destination}-journal").resolve(strict=False),
    }
    if protected_source_paths & protected_destination_paths:
        raise ValueError("restore destination must differ from the backup and its sidecars")

    # Online-backup output is a standalone SQLite file. Refuse ambiguous
    # sources that still have sidecars; otherwise immutable mode could ignore
    # committed WAL records.
    if any(Path(f"{source}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("source backup must not have SQLite sidecar files")

    if os.path.lexists(destination):
        raise FileExistsError("restore destination already exists")
    if any(os.path.lexists(Path(f"{destination}{suffix}")) for suffix in ("-wal", "-shm", "-journal")):
        raise FileExistsError("restore destination has SQLite sidecar files")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    published = False
    try:
        source_connection = _read_only_connection(source)
        source_counts = _validate_database(source_connection)

        target_connection = sqlite3.connect(str(temporary), timeout=15.0)
        target_connection.row_factory = sqlite3.Row
        source_connection.backup(target_connection, pages=256, sleep=0.05)
        target_counts = _validate_database(target_connection)
        if target_counts != source_counts:
            raise sqlite3.DatabaseError("restored database row counts do not match source")

        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None

        with temporary.open("rb") as restored_file:
            os.fsync(restored_file.fileno())

        # Hard-link publication is atomic and fails rather than replacing an
        # existing destination created by another process during validation.
        os.link(temporary, destination)
        published = True
        temporary.unlink()
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

        return {
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "integrity_check": "ok",
            "restored_table_counts": target_counts,
        }
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        # A published file is intentionally retained even if directory fsync
        # is unsupported; never attempt to roll back a successful publication.
        _ = published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="standalone backup created by backup_order_journal.py")
    parser.add_argument("--destination", required=True, help="new, unused journal path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        result = restore_order_journal(args.source, args.destination)
    except Exception as exc:
        parser.exit(2, f"restore failed: {type(exc).__name__}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
