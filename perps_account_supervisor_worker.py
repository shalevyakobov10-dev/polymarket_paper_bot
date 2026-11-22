"""Opt-in read-only Perps account supervisor process.

Requires a mounted, permission-restricted JSON secret file containing existing
owner/API/delegated Perps credentials. It resumes existing Perps credentials
only; it never generates credentials or exposes the SDK writable session surface.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import signal
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from perps_account_preflight import PerpsAccountPreflight
from perps_account_supervisor import PerpsAccountSupervisor
from perps_market_data import PerpsPublicMarketDataProvider
from perps_order_journal import PerpsOrderJournal
from perps_read_only_session import open_existing_perps_read_only_session


LOGGER = logging.getLogger("polymarket.perps_account_supervisor_worker")
MAX_SECRET_FILE_BYTES = 16 * 1024
PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SECRET_SCHEMA = {
    "owner_private_key",
    "deposit_wallet",
    "clob_api_credentials",
    "perps_session_credentials",
}


class PerpsAccountSecretsError(RuntimeError):
    """Sanitized credential-source error; never includes secret values."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def load_perps_account_secrets(path: str | Path) -> dict[str, Any]:
    """Load a strict schema from a non-symlink file not readable by others.

    Only owner-readable modes 0400 and 0600 are accepted. Group/other access,
    executable modes, non-regular files and oversized files are denied.
    The raw document is never logged or included in raised exception text.
    """
    target = Path(path).expanduser()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PerpsAccountSecretsError("perps_account_secrets_must_be_regular_file")
        permissions = stat.S_IMODE(metadata.st_mode)
        if permissions not in {0o400, 0o600}:
            raise PerpsAccountSecretsError("perps_account_secrets_permissions_invalid")
        if metadata.st_size <= 0 or metadata.st_size > MAX_SECRET_FILE_BYTES:
            raise PerpsAccountSecretsError("perps_account_secrets_file_size_invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw_bytes = handle.read(MAX_SECRET_FILE_BYTES + 1)
        if len(raw_bytes) > MAX_SECRET_FILE_BYTES:
            raise PerpsAccountSecretsError("perps_account_secrets_file_size_invalid")
        raw = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except PerpsAccountSecretsError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise PerpsAccountSecretsError("perps_account_secrets_unavailable_or_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if not isinstance(raw, dict) or set(raw) != SECRET_SCHEMA:
        raise PerpsAccountSecretsError("perps_account_secrets_schema_invalid")
    owner_key = raw.get("owner_private_key")
    wallet = raw.get("deposit_wallet")
    api = raw.get("clob_api_credentials")
    perps = raw.get("perps_session_credentials")
    if not isinstance(owner_key, str) or not PRIVATE_KEY_RE.fullmatch(owner_key):
        raise PerpsAccountSecretsError("perps_account_owner_key_invalid")
    if not isinstance(wallet, str) or not WALLET_RE.fullmatch(wallet):
        raise PerpsAccountSecretsError("perps_account_deposit_wallet_invalid")
    if not isinstance(api, dict) or set(api) != {"key", "secret", "passphrase"}:
        raise PerpsAccountSecretsError("perps_account_clob_credentials_invalid")
    if any(
        not isinstance(api.get(name), str)
        or not api[name].strip()
        or len(api[name]) > 1024
        for name in ("key", "secret", "passphrase")
    ):
        raise PerpsAccountSecretsError("perps_account_clob_credentials_invalid")
    if not isinstance(perps, dict) or set(perps) != {
        "proxy", "private_key", "secret", "expires_at"
    }:
        raise PerpsAccountSecretsError("perps_account_session_credentials_invalid")
    if (
        not isinstance(perps.get("proxy"), str)
        or not WALLET_RE.fullmatch(perps["proxy"])
        or not isinstance(perps.get("private_key"), str)
        or not PRIVATE_KEY_RE.fullmatch(perps["private_key"])
        or not isinstance(perps.get("secret"), str)
        or not perps["secret"].strip()
        or len(perps["secret"]) > 1024
        or not isinstance(perps.get("expires_at"), str)
    ):
        raise PerpsAccountSecretsError("perps_account_session_credentials_invalid")
    try:
        expiry = datetime.fromisoformat(perps["expires_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise PerpsAccountSecretsError("perps_account_session_expiry_invalid") from None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise PerpsAccountSecretsError("perps_account_session_expiry_invalid")
    if expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise PerpsAccountSecretsError("perps_account_session_credentials_expired")

    # Return a fresh object, not the caller-owned JSON object. Callers must keep
    # this value in memory only and must never log, serialize or persist it.
    return {
        "owner_private_key": owner_key,
        "deposit_wallet": wallet,
        "clob_api_credentials": dict(api),
        "perps_session_credentials": dict(perps),
    }


async def create_secure_client_from_existing_credentials(
    secrets: dict[str, Any],
) -> tuple[Any, Any]:
    """Construct SDK transports without API-key derivation, then resume Perps creds.

    The pinned SDK's public client factory can derive replacement CLOB API
    credentials after validation failure and performs wallet-readiness logic
    that can deploy a missing default Deposit Wallet. This monitor must do
    neither. Its pinned/private _create path is contract-checked and explicitly
    uses existing API credentials with validation disabled; a supplied wallet
    is mandatory, so no wallet discovery or deployment flow is entered.
    open_perps_session receives existing delegated Perps credentials, never None.
    """
    if not isinstance(secrets, dict) or set(secrets) != SECRET_SCHEMA:
        raise PerpsAccountSecretsError("perps_account_secrets_schema_invalid")
    try:
        from polymarket import ApiKeyCreds, AsyncSecureClient, PerpsCredentials

        api = secrets["clob_api_credentials"]
        perps_credentials = PerpsCredentials.model_validate(
            secrets["perps_session_credentials"]
        )
        api_credentials = ApiKeyCreds(
            key=api["key"],
            secret=api["secret"],
            passphrase=api["passphrase"],
        )
        client = await AsyncSecureClient._create(
            private_key=secrets["owner_private_key"],
            wallet=secrets["deposit_wallet"],
            credentials=api_credentials,
            validate_credentials=False,
        )
        return client, perps_credentials
    except Exception:
        # Never propagate an SDK exception string; it may contain request data.
        raise PerpsAccountSecretsError(
            "perps_account_sdk_client_or_credentials_initialization_failed"
        ) from None


async def run_perps_account_supervisor_worker(
    *,
    secure_client: Any,
    credentials: Any,
    database_path: str | Path,
    status_path: str | Path,
    submission_lock_path: str | Path,
    fee_schedule_provider: Callable[[], Any] | None = None,
    stop_event: asyncio.Event | None = None,
    poll_interval_seconds: int = 30,
    cycle_timeout_seconds: float = 60.0,
    max_consecutive_failures: int = 10,
    max_cycles: int | None = None,
    now_provider: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> int:
    """Run account preflight inside the read-only session and shared gateway lock."""
    if credentials is None:
        raise ValueError("existing delegated Perps credentials are required")
    provider = fee_schedule_provider or PerpsPublicMarketDataProvider().load_fee_schedule_snapshot
    if not callable(provider):
        raise ValueError("fee_schedule_provider must be callable")

    async with secure_client:
        journal = PerpsOrderJournal(database_path)
        async with open_existing_perps_read_only_session(
            secure_client,
            credentials=credentials,
        ) as session:
            preflight = PerpsAccountPreflight(
                journal=journal,
                session=session,
                fee_schedule_provider=provider,
            )
            supervisor = PerpsAccountSupervisor(
                journal=journal,
                account_preflight=preflight,
                status_path=status_path,
                submission_lock_path=submission_lock_path,
                poll_interval_seconds=poll_interval_seconds,
                cycle_timeout_seconds=cycle_timeout_seconds,
                max_consecutive_failures=max_consecutive_failures,
                now_provider=now_provider,
                sleep=sleep,
            )
            return await supervisor.run(stop_event=stop_event, max_cycles=max_cycles)


def _required_worker_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty path")
    return value


def _bounded_worker_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} is outside its permitted range") from None
    if isinstance(value, bool) or str(value).strip() != str(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside its permitted range")
    return parsed


async def _run_from_environment(args: argparse.Namespace) -> int:
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    secrets_path_raw = (
        args.secrets_file
        if args.secrets_file is not None
        else os.environ.get("PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE")
    )
    try:
        secrets_path = _required_worker_path(
            secrets_path_raw, "PERPS_ACCOUNT_SUPERVISOR_SECRETS_FILE"
        )
    except ValueError:
        raise PerpsAccountSecretsError("perps_account_secrets_file_not_configured") from None

    database_path = _required_worker_path(
        args.database if args.database is not None else os.environ.get(
            "PERPS_ORDER_JOURNAL_DB", str(data_dir / "perps_order_journal.sqlite3")
        ),
        "PERPS_ORDER_JOURNAL_DB",
    )
    status_path = _required_worker_path(
        args.status_file if args.status_file is not None else os.environ.get(
            "PERPS_ACCOUNT_SUPERVISOR_STATUS_FILE",
            str(data_dir / "perps_account_supervisor_status.json"),
        ),
        "PERPS_ACCOUNT_SUPERVISOR_STATUS_FILE",
    )
    lock_path = _required_worker_path(
        args.submission_lock if args.submission_lock is not None else os.environ.get(
            "PERPS_SUBMISSION_LOCK_PATH", str(data_dir / "perps_submission.lock")
        ),
        "PERPS_SUBMISSION_LOCK_PATH",
    )
    poll_raw = (
        args.poll_interval
        if args.poll_interval is not None
        else os.environ.get("PERPS_ACCOUNT_SUPERVISOR_POLL_SECONDS", "30")
    )
    poll = _bounded_worker_integer(
        poll_raw,
        "PERPS_ACCOUNT_SUPERVISOR_POLL_SECONDS",
        minimum=15,
        maximum=300,
    )
    try:
        cycle_timeout = float(
            os.environ.get("PERPS_ACCOUNT_SUPERVISOR_CYCLE_TIMEOUT_SECONDS", "60")
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("PERPS_ACCOUNT_SUPERVISOR_CYCLE_TIMEOUT_SECONDS is invalid") from None
    if (
        not math.isfinite(cycle_timeout)
        or not 0.05 <= cycle_timeout <= 3600
    ):
        raise ValueError("PERPS_ACCOUNT_SUPERVISOR_CYCLE_TIMEOUT_SECONDS is invalid")
    max_failures = _bounded_worker_integer(
        os.environ.get("PERPS_ACCOUNT_SUPERVISOR_MAX_FAILURES", "10"),
        "PERPS_ACCOUNT_SUPERVISOR_MAX_FAILURES",
        minimum=1,
        maximum=50,
    )

    secrets = load_perps_account_secrets(secrets_path)
    try:
        client, credentials = await create_secure_client_from_existing_credentials(secrets)
    finally:
        # Drop the JSON secret container even if SDK client construction fails.
        secrets.clear()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    return await run_perps_account_supervisor_worker(
        secure_client=client,
        credentials=credentials,
        database_path=database_path,
        status_path=status_path,
        submission_lock_path=lock_path,
        stop_event=stop_event,
        poll_interval_seconds=poll,
        cycle_timeout_seconds=cycle_timeout,
        max_consecutive_failures=max_failures,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in read-only Polymarket Perps account supervisor"
    )
    parser.add_argument("--secrets-file", default=None)
    parser.add_argument("--database", default=None)
    parser.add_argument("--status-file", default=None)
    parser.add_argument("--submission-lock", default=None)
    parser.add_argument("--poll-interval", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER.info("starting read-only Perps account supervisor worker")
    try:
        result = asyncio.run(_run_from_environment(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOGGER.error("Perps account supervisor startup/cycle failed: %s", type(exc).__name__)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
