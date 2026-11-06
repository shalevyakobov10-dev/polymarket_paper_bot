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
from typing import Any
from urllib.parse import quote


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
