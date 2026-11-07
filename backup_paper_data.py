"""Consistent, integrity-checked backups for event-aware paper SQLite databases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()), safe=\"/:\")}?mode=ro", uri=True, timeout=20)
    try:
        result = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise sqlite3.DatabaseError("backup integrity check failed")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise sqlite3.IntegrityError("backup foreign-key check failed")
    finally:
        connection.close()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def _prune_backups(directory: Path, prefix: str, retain: int) -> int:
    candidates = sorted(
        directory.glob(f"{prefix}-*.sqlite3"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = 0
    for old in candidates[retain:]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def create_paper_data_backups(
    database_paths: Iterable[str | Path],
    backup_directory: str | Path,
    *,
    retain: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Back up each existing DB with SQLite's online backup API and verify it.

    Each destination is uniquely named, never overwrites a previous backup,
    and is published only after integrity and foreign-key checks pass.
    """
    if isinstance(retain, bool) or not isinstance(retain, int) or not 1 <= retain <= 365:
        raise ValueError("retain must be an integer between 1 and 365")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("backup timestamp must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = Path(backup_directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    removed = 0
    seen_sources: set[Path] = set()
    for raw in database_paths:
        source = Path(raw).expanduser()
        try:
            resolved = source.resolve(strict=True)
        except FileNotFoundError:
            skipped.append(source.name)
            continue
        if resolved in seen_sources:
            continue
        seen_sources.add(resolved)
        if not resolved.is_file():
            raise ValueError("database source must be a regular file")
        prefix = resolved.stem
        destination = directory / f"{prefix}-{stamp}.sqlite3"
        if destination.exists():
            raise FileExistsError("backup destination already exists")

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(directory), prefix=f".{prefix}-", suffix=".tmp", delete=False,
            ) as temporary:
                temp_path = temporary.name

            source_connection = sqlite3.connect(str(resolved), timeout=30.0)
            destination_connection = sqlite3.connect(temp_path, timeout=30.0)
            try:
                source_connection.backup(destination_connection, pages=256, sleep=0.05)
                destination_connection.commit()
            finally:
                destination_connection.close()
                source_connection.close()

            temp_file = Path(temp_path)
            _verify_sqlite(temp_file)
            with temp_file.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp_file, destination)
            temp_file.unlink()
            temp_path = None
            size = destination.stat().st_size
            files.append({
                "source_name": resolved.name,
                "backup_name": destination.name,
                "size_bytes": size,
                "sha256": _sha256(destination),
                "integrity_check": "ok",
                "foreign_key_check": "ok",
            })
            removed += _prune_backups(directory, prefix, retain)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    if not files:
        raise FileNotFoundError("no paper SQLite databases were available to back up")
    manifest = {
        "created_at": current.astimezone(timezone.utc).isoformat(),
        "files": files,
        "skipped_missing_sources": skipped,
        "retention_per_database": retain,
        "old_backups_removed": removed,
        "research_only": True,
        "execution": "none",
    }
    manifest_path = directory / f"paper-backup-manifest-{stamp}.json"
    _atomic_json(manifest_path, manifest)
    return {**manifest, "manifest_name": manifest_path.name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    data_dir = Path(os.environ.get("DATA_DIR", "data")).expanduser()
    parser.add_argument(
        "--paper-database",
        default=os.environ.get("PAPER_DATABASE_PATH", str(data_dir / "offline_news_paper.sqlite3")),
    )
    parser.add_argument(
        "--market-history-database",
        default=os.environ.get("MARKET_HISTORY_DATABASE_PATH", str(data_dir / "market_history.sqlite3")),
    )
    parser.add_argument(
        "--backup-directory",
        default=os.environ.get("PAPER_BACKUP_DIRECTORY", str(data_dir / "backups")),
    )
    parser.add_argument(
        "--retain",
        type=int,
        default=os.environ.get("PAPER_BACKUP_RETAIN", "30"),
    )
    args = parser.parse_args()
    try:
        result = create_paper_data_backups(
            [args.paper_database, args.market_history_database],
            args.backup_directory,
            retain=args.retain,
        )
    except Exception as exc:
        parser.exit(2, f"paper database backup failed: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
