"""Consistent, integrity-checked backups and safe offline restore for Paper SQLite data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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
    uri = "file:" + quote(str(path.resolve()), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=20)
    try:
        result = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            raise sqlite3.DatabaseError("backup integrity check failed")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise sqlite3.IntegrityError("backup foreign-key check failed")
    finally:
        connection.close()


def _prune_backups(directory: Path, prefix: str, retain: int) -> int:
    # Backup filenames contain a UTC timestamp, so ordering by name is stable
    # even when filesystem mtimes are restored or skewed by a clock correction.
    candidates = sorted(
        directory.glob(f"{prefix}-*.sqlite3"),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = 0
    for old in candidates[retain:]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def _prune_orphaned_manifests(directory: Path) -> int:
    """Remove manifests whose retained backup set is no longer complete."""
    removed = 0
    for manifest_path in directory.glob("paper-backup-manifest-*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            # Keep malformed manifests for operator inspection.
            continue
        entries = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(entries, list) or not entries:
            continue
        names = [
            entry.get("backup_name")
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("backup_name"), str)
        ]
        if len(names) != len(entries) or not names:
            continue
        if any(
            not name
            or Path(name).name != name
            or "\\" in name
            or not (directory / name).is_file()
            for name in names
        ):
            manifest_path.unlink(missing_ok=True)
            removed += 1
    return removed


def _stage_json(directory: Path, payload: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(directory),
        prefix=".paper-backup-manifest.", suffix=".tmp", delete=False,
    ) as handle:
        path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def create_paper_data_backups(
    database_paths: Iterable[str | Path],
    backup_directory: str | Path,
    *,
    retain: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one verified backup set; publish its manifest before pruning history.

    Every source database is copied and verified in a hidden staging file first.
    The visible backup files are linked into place only after every source has
    passed validation. The manifest is published last and retention pruning
    starts only after that manifest exists.
    """
    if isinstance(retain, bool) or not isinstance(retain, int) or not 1 <= retain <= 365:
        raise ValueError("retain must be an integer between 1 and 365")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("backup timestamp must be timezone-aware")
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = Path(backup_directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    staged: list[dict[str, Any]] = []
    published: list[Path] = []
    skipped: list[str] = []
    manifest_temp: Path | None = None
    manifest_published = False
    manifest_path = directory / f"paper-backup-manifest-{stamp}.json"
    try:
        seen_sources: set[Path] = set()
        seen_prefixes: set[str] = set()
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
            if prefix in seen_prefixes:
                raise ValueError("database source filenames must be unique across backup inputs")
            seen_prefixes.add(prefix)
            destination = directory / f"{prefix}-{stamp}.sqlite3"
            if destination.exists():
                raise FileExistsError("backup destination already exists")

            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=str(directory), prefix=f".{prefix}-",
                    suffix=".tmp", delete=False,
                ) as temporary:
                    temp_path = Path(temporary.name)

                source_connection = sqlite3.connect(str(resolved), timeout=30.0)
                destination_connection = sqlite3.connect(str(temp_path), timeout=30.0)
                try:
                    source_connection.backup(destination_connection, pages=256, sleep=0.05)
                    destination_connection.commit()
                finally:
                    destination_connection.close()
                    source_connection.close()

                _verify_sqlite(temp_path)
                with temp_path.open("rb+") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                staged.append({
                    "temp_path": temp_path,
                    "destination": destination,
                    "prefix": prefix,
                    "source_name": resolved.name,
                    "size_bytes": temp_path.stat().st_size,
                    "sha256": _sha256(temp_path),
                    "integrity_check": "ok",
                    "foreign_key_check": "ok",
                })
                temp_path = None
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

        if not staged:
            raise FileNotFoundError("no paper SQLite databases were available to back up")
        if manifest_path.exists():
            raise FileExistsError("backup manifest destination already exists")

        expected_pruned = sum(
            max(0, len(list(directory.glob(f"{item['prefix']}-*.sqlite3"))) + 1 - retain)
            for item in staged
        )
        files = [
            {
                "source_name": item["source_name"],
                "backup_name": item["destination"].name,
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "integrity_check": "ok",
                "foreign_key_check": "ok",
            }
            for item in staged
        ]
        manifest = {
            "created_at": current.astimezone(timezone.utc).isoformat(),
            "files": files,
            "skipped_missing_sources": skipped,
            "retention_per_database": retain,
            "old_backups_removed": expected_pruned,
            "research_only": True,
            "execution": "none",
        }
        manifest_temp = _stage_json(directory, manifest)

        # Publish every already-verified database first, without overwriting.
        for item in staged:
            os.link(item["temp_path"], item["destination"])
            published.append(item["destination"])

        # The manifest is the commit record for a complete backup set.
        os.link(manifest_temp, manifest_path)
        manifest_published = True

        removed = 0
        for prefix in seen_prefixes:
            removed += _prune_backups(directory, prefix, retain)
        manifests_removed = _prune_orphaned_manifests(directory)

        return {
            **manifest,
            "old_backups_removed": removed,
            "orphaned_manifests_removed": manifests_removed,
            "manifest_name": manifest_path.name,
        }
    except Exception:
        # If the manifest was not committed, roll back all visible files from
        # this attempt and preserve the prior verified backup set.
        if not manifest_published:
            for path in reversed(published):
                path.unlink(missing_ok=True)
        raise
    finally:
        for item in staged:
            item["temp_path"].unlink(missing_ok=True)
        if manifest_temp is not None:
            manifest_temp.unlink(missing_ok=True)


def verify_paper_data_backup(manifest_path: str | Path) -> dict[str, Any]:
    """Verify every file referenced by one backup manifest, including its hash."""
    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    if not manifest_file.is_file():
        raise ValueError("backup manifest must be a regular file")
    root = manifest_file.parent
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ValueError("backup manifest is unreadable or invalid JSON") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("backup manifest schema is invalid")
    entries = manifest["files"]
    if not entries:
        raise ValueError("backup manifest contains no database files")
    skipped = manifest.get("skipped_missing_sources", [])
    if not isinstance(skipped, list) or any(not isinstance(item, str) for item in skipped):
        raise ValueError("backup manifest skipped-source list is invalid")

    seen_sources: set[str] = set()
    seen_backups: set[str] = set()
    verified: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("backup manifest file entry is invalid")
        source_name = entry.get("source_name")
        backup_name = entry.get("backup_name")
        size_bytes = entry.get("size_bytes")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(source_name, str) or not source_name
            or Path(source_name).name != source_name
            or not isinstance(backup_name, str) or not backup_name
            or Path(backup_name).name != backup_name
            or "/" in backup_name or "\\" in backup_name
            or backup_name in {".", ".."}
            or "/" in source_name or "\\" in source_name
            or not backup_name.endswith(".sqlite3")
            or source_name in seen_sources
            or backup_name in seen_backups
        ):
            raise ValueError("backup manifest contains an unsafe or duplicate filename")
        if (
            isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0
            or not isinstance(expected_hash, str) or len(expected_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_hash)
        ):
            raise ValueError("backup manifest size or SHA-256 field is invalid")
        if entry.get("integrity_check") != "ok" or entry.get("foreign_key_check") != "ok":
            raise ValueError("backup manifest does not certify SQLite integrity")
        seen_sources.add(source_name)
        seen_backups.add(backup_name)
        backup_file = root / backup_name
        resolved_backup = backup_file.resolve(strict=True)
        if resolved_backup.parent != root or not resolved_backup.is_file() or backup_file.is_symlink():
            raise ValueError("backup file is missing or resolves outside the manifest directory")
        actual_size = resolved_backup.stat().st_size
        if actual_size != size_bytes:
            raise ValueError(f"backup size mismatch for {source_name}")
        actual_hash = _sha256(resolved_backup)
        if actual_hash != expected_hash:
            raise ValueError(f"backup SHA-256 mismatch for {source_name}")
        _verify_sqlite(resolved_backup)
        verified.append({
            "source_name": source_name,
            "backup_name": backup_name,
            "size_bytes": actual_size,
            "sha256": actual_hash,
            "integrity_check": "ok",
            "foreign_key_check": "ok",
        })

    return {
        "manifest_name": manifest_file.name,
        "created_at": manifest.get("created_at"),
        "files": verified,
        "complete": not bool(skipped),
        "skipped_missing_sources": skipped,
        "research_only": True,
        "execution": "none",
    }


def restore_paper_data_backup(
    manifest_path: str | Path,
    source_name: str,
    destination_path: str | Path,
) -> dict[str, Any]:
    """Restore one verified database to a new path without overwriting existing data."""
    if not isinstance(source_name, str) or not source_name or Path(source_name).name != source_name:
        raise ValueError("source_name must be a basename")
    verification = verify_paper_data_backup(manifest_path)
    if not verification["complete"]:
        raise ValueError("cannot restore from an incomplete backup set")
    matches = [item for item in verification["files"] if item["source_name"] == source_name]
    if len(matches) != 1:
        raise FileNotFoundError("source database is not present in the verified manifest")
    manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
    source_backup = manifest_file.parent / matches[0]["backup_name"]
    destination = Path(destination_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("restore destination already exists; active data will not be overwritten")

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=str(destination.parent),
            prefix=f".{destination.name}.", suffix=".restore.tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        shutil.copyfile(source_backup, temp_path)
        with temp_path.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(temp_path) != matches[0]["sha256"]:
            raise sqlite3.DatabaseError("restored database checksum does not match manifest")
        _verify_sqlite(temp_path)
        os.link(temp_path, destination)
        return {
            "source_name": source_name,
            "destination_path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": matches[0]["sha256"],
            "integrity_check": "ok",
            "research_only": True,
            "execution": "none",
        }
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
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
        "--perps-order-journal",
        default=os.environ.get("PERPS_ORDER_JOURNAL_DB", str(data_dir / "perps_order_journal.sqlite3")),
        help="optional durable Perps order journal path",
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
    parser.add_argument(
        "--verify-manifest",
        help="verify every database and SHA-256 hash listed in an existing manifest",
    )
    parser.add_argument(
        "--restore-source",
        help="restore this source database name from a verified manifest into a new path",
    )
    parser.add_argument(
        "--restore-to",
        help="new destination path for --restore-source; an existing file is never overwritten",
    )
    args = parser.parse_args(argv)
    if bool(args.restore_source) != bool(args.restore_to):
        parser.error("--restore-source and --restore-to must be provided together")
    if args.restore_source and not args.verify_manifest:
        parser.error("--restore-source requires --verify-manifest")
    try:
        if args.restore_source:
            result = restore_paper_data_backup(
                args.verify_manifest,
                args.restore_source,
                args.restore_to,
            )
        elif args.verify_manifest:
            result = verify_paper_data_backup(args.verify_manifest)
        else:
            result = create_paper_data_backups(
                [args.paper_database, args.market_history_database, args.perps_order_journal],
                args.backup_directory,
                retain=args.retain,
            )
    except Exception as exc:
        parser.exit(2, f"paper database backup operation failed: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
