"""Fail-closed event live kill switch with separate operator ownership.

The gateway process is read-only. Only a separately configured operator UID
may atomically engage/update the switch file. There is intentionally no disarm
API or CLI. DISARMED must be installed out-of-band by an independent release
operator into the protected mount after all release gates have been reviewed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_MAX_BYTES = 4096
_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DEFAULT_PATH = Path("/etc/polymarket/event_live_kill_switch.json")


def _blocked(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "verified": False,
        "state": "engaged",
        "engaged": True,
        "reason_code": reason_code,
        "execution": "none",
    }


def _owner_uid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or str(parsed) != str(value).strip():
        return None
    return parsed


def _safe_parent(path: Path, *, expected_owner_uid: int) -> str | None:
    """Require an operator-owned parent and non-replaceable path ancestors."""
    try:
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            return "kill_switch_directory_unavailable"
        metadata = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            return "kill_switch_parent_not_directory"
        if metadata.st_uid != expected_owner_uid:
            return "kill_switch_directory_owner_mismatch"
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            return "kill_switch_directory_group_or_world_writable"

        for ancestor in parent.parents:
            if ancestor.is_symlink():
                return "kill_switch_ancestor_symlink_rejected"
            ancestor_metadata = ancestor.stat(follow_symlinks=False)
            ancestor_mode = stat.S_IMODE(ancestor_metadata.st_mode)
            # Sticky directories (e.g. /tmp) protect operator-owned child
            # entries; any other group/world-writable ancestor is rejected.
            if ancestor_mode & 0o022 and not (ancestor_mode & stat.S_ISVTX):
                return "kill_switch_ancestor_writable"
    except OSError:
        return "kill_switch_directory_unavailable"
    return None


def read_live_kill_switch(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_age_seconds: float = 60.0,
    expected_owner_uid: int | str | None = None,
) -> dict[str, Any]:
    """Read a fresh operator-owned status file; every uncertainty means engaged."""
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not 1 <= float(max_age_seconds) <= 3600
    ):
        return _blocked("kill_switch_freshness_policy_invalid")
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        return _blocked("kill_switch_clock_invalid")
    current = current.astimezone(timezone.utc)

    configured_owner = (
        expected_owner_uid
        if expected_owner_uid is not None
        else os.getenv("EVENT_LIVE_KILL_SWITCH_OWNER_UID")
    )
    owner_uid = _owner_uid(configured_owner)
    if owner_uid is None:
        return _blocked("kill_switch_operator_owner_not_configured")
    try:
        gateway_uid = os.geteuid()
    except (AttributeError, OSError):
        return _blocked("kill_switch_gateway_uid_unavailable")
    if owner_uid == gateway_uid:
        return _blocked("kill_switch_owner_must_be_separate_from_gateway")

    candidate = Path(path).expanduser()
    parent_error = _safe_parent(candidate, expected_owner_uid=owner_uid)
    if parent_error is not None:
        return _blocked(parent_error)

    descriptor: int | None = None
    try:
        if candidate.is_symlink():
            return _blocked("kill_switch_symlink_rejected")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return _blocked("kill_switch_nofollow_unsupported")
        descriptor = os.open(candidate, os.O_RDONLY | nofollow)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return _blocked("kill_switch_not_regular_file")
        if metadata.st_uid != owner_uid:
            return _blocked("kill_switch_file_owner_mismatch")
        if stat.S_IMODE(metadata.st_mode) != 0o444:
            return _blocked("kill_switch_file_permissions_invalid")
        if metadata.st_nlink != 1:
            return _blocked("kill_switch_hardlink_rejected")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_BYTES:
            return _blocked("kill_switch_file_size_invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return _blocked("kill_switch_file_size_invalid")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_object(pairs),
        )
    except FileNotFoundError:
        return _blocked("kill_switch_file_missing")
    except Exception:
        return _blocked("kill_switch_status_unreadable_or_invalid")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "state", "updated_at_utc", "reason_code"
    }:
        return _blocked("kill_switch_schema_invalid")
    if isinstance(payload.get("schema_version"), bool) or payload.get("schema_version") != _SCHEMA_VERSION:
        return _blocked("kill_switch_schema_version_unsupported")
    state = payload.get("state")
    if state not in {"ENGAGED", "DISARMED"}:
        return _blocked("kill_switch_state_invalid")
    reason = payload.get("reason_code")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        return _blocked("kill_switch_reason_invalid")

    raw_timestamp = payload.get("updated_at_utc")
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        return _blocked("kill_switch_timestamp_missing")
    try:
        updated = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return _blocked("kill_switch_timestamp_invalid")
    if updated.tzinfo is None or updated.utcoffset() is None:
        return _blocked("kill_switch_timestamp_invalid")
    age = (current - updated.astimezone(timezone.utc)).total_seconds()
    if age < -2 or age > float(max_age_seconds):
        return _blocked("kill_switch_status_stale_or_future_dated")

    engaged = state == "ENGAGED"
    return {
        "schema_version": _SCHEMA_VERSION,
        "verified": True,
        "state": state.lower(),
        "engaged": engaged,
        "reason_code": reason,
        "updated_at_utc": updated.astimezone(timezone.utc).isoformat(),
        "age_seconds": max(0.0, age),
        "owner_uid": owner_uid,
        "execution": "none",
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate kill-switch field")
        result[key] = value
    return result


def engage_live_kill_switch(
    path: str | Path,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically engage/update the switch. It never writes DISARMED."""
    if not isinstance(reason_code, str) or not _REASON_RE.fullmatch(reason_code):
        raise ValueError("reason_code must be a short safe identifier")
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)

    owner_uid = _owner_uid(os.getenv("EVENT_LIVE_KILL_SWITCH_OWNER_UID"))
    if owner_uid is None:
        raise PermissionError("EVENT_LIVE_KILL_SWITCH_OWNER_UID must identify the operator UID")
    if os.geteuid() != owner_uid:
        raise PermissionError("kill switch may only be engaged by its configured operator UID")

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    parent_error = _safe_parent(target, expected_owner_uid=owner_uid)
    if parent_error is not None:
        raise PermissionError(parent_error)

    payload = json.dumps({
        "schema_version": _SCHEMA_VERSION,
        "state": "ENGAGED",
        "updated_at_utc": current.isoformat(),
        "reason_code": reason_code,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")

    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{target.name}.engage-",
            dir=str(target.parent),
        )
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    metadata = target.stat(follow_symlinks=False)
    if (
        metadata.st_uid != owner_uid
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("kill_switch_engage_write_permissions_unverified")
    written = json.loads(target.read_text(encoding="utf-8"))
    if written != json.loads(payload.decode("utf-8")):
        raise RuntimeError("kill_switch_engage_write_could_not_be_verified")
    return {
        "schema_version": _SCHEMA_VERSION,
        "verified": True,
        "state": "engaged",
        "engaged": True,
        "reason_code": reason_code,
        "updated_at_utc": current.isoformat(),
        "age_seconds": 0.0,
        "owner_uid": owner_uid,
        "execution": "none",
    }


def check_live_kill_switch() -> dict[str, Any]:
    """Read the operator-mounted switch; never fall back to app-writable DATA_DIR."""
    configured = os.getenv("EVENT_LIVE_KILL_SWITCH_PATH")
    return read_live_kill_switch(configured or _DEFAULT_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Operate the local event-market live kill switch."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    engage = subparsers.add_parser(
        "engage", help="engage the live switch (there is no disarm command)"
    )
    engage.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command == "engage":
        configured = os.getenv("EVENT_LIVE_KILL_SWITCH_PATH")
        report = engage_live_kill_switch(
            configured or _DEFAULT_PATH,
            reason_code=args.reason,
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report.get("verified") is True and report.get("engaged") is True else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
