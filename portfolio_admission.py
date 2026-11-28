"""Cross-process, Paper-only portfolio admission reservations.

This is a coordination primitive, not yet wired into either trading engine.
SQLite BEGIN IMMEDIATE serializes reservations across processes on the same
database. Reservations remain risk-consuming until the caller verifies that
the corresponding durable entry is visible in its venue ledger and reconciles
the reservation. Uncertain/crashed admissions therefore fail closed.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class PortfolioAdmissionError(RuntimeError):
    """Raised when an admission cannot be safely evaluated or persisted."""


def _amount(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PortfolioAdmissionError(f"{field}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PortfolioAdmissionError(f"{field}_invalid") from None
    if not result.is_finite() or result < 0:
        raise PortfolioAdmissionError(f"{field}_invalid")
    return result


def _aware(value: datetime | str, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise PortfolioAdmissionError(f"{field}_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortfolioAdmissionError(f"{field}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class AdmissionResult:
    admission_id: str
    approved: bool
    reason: str
    reserved_exposure_usd: str
    reserved_positions: int
    status: str


class PortfolioAdmissionCoordinator:
    """Durable serialized reservations shared by Paper engine processes.

    Call reserve immediately before the venue's durable Paper entry commit.
    Then mark_committed after that commit, and reconcile only after a fresh
    ledger read proves the entry is present. Never auto-expire uncertain rows.
    """

    def __init__(self, database_path: str | Path, *, timeout_seconds: float = 10.0) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = float(timeout_seconds)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        db.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self) -> None:
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_admissions (
                    admission_id TEXT PRIMARY KEY,
                    venue TEXT NOT NULL CHECK(venue IN ('POLYMARKET','PERPS')),
                    exposure_usd TEXT NOT NULL,
                    positions INTEGER NOT NULL CHECK(positions > 0),
                    status TEXT NOT NULL CHECK(status IN ('PENDING','AWAITING_RECONCILIATION','RECONCILED','RELEASED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    ledger_entry_id TEXT
                )
            """)
        finally:
            db.close()

    def reserve(
        self, *, admission_id: str, venue: str, exposure_usd: Any, positions: int,
        snapshot_exposure_usd: Any, snapshot_positions: int, snapshot_at: datetime | str,
        max_snapshot_age_seconds: float, max_total_exposure_usd: Any,
        max_total_positions: int, now: datetime | None = None,
    ) -> AdmissionResult:
        if not isinstance(admission_id, str) or not admission_id.strip() or len(admission_id) > 160:
            raise PortfolioAdmissionError("admission_id_invalid")
        venue = str(venue).upper()
        if venue not in {"POLYMARKET", "PERPS"}:
            raise PortfolioAdmissionError("venue_invalid")
        requested = _amount(exposure_usd, "exposure_usd")
        observed = _amount(snapshot_exposure_usd, "snapshot_exposure_usd")
        maximum = _amount(max_total_exposure_usd, "max_total_exposure_usd")
        if requested <= 0 or maximum <= 0:
            raise PortfolioAdmissionError("exposure_must_be_positive")
        if isinstance(positions, bool) or not isinstance(positions, int) or positions < 1:
            raise PortfolioAdmissionError("positions_invalid")
        if isinstance(snapshot_positions, bool) or not isinstance(snapshot_positions, int) or snapshot_positions < 0:
            raise PortfolioAdmissionError("snapshot_positions_invalid")
        if isinstance(max_total_positions, bool) or not isinstance(max_total_positions, int) or max_total_positions < 1:
            raise PortfolioAdmissionError("max_total_positions_invalid")
        if isinstance(max_snapshot_age_seconds, bool) or not isinstance(max_snapshot_age_seconds, (int, float)) or not math.isfinite(float(max_snapshot_age_seconds)) or max_snapshot_age_seconds <= 0:
            raise PortfolioAdmissionError("snapshot_age_limit_invalid")
        captured = _aware(snapshot_at, "snapshot_at")
        current = _aware(now or datetime.now(timezone.utc), "now")
        age = (current - captured).total_seconds()
        if age < -2 or age > float(max_snapshot_age_seconds):
            raise PortfolioAdmissionError("portfolio_snapshot_stale_or_future_dated")
        if current.date() != captured.date():
            raise PortfolioAdmissionError("portfolio_snapshot_utc_day_mismatch")
        created = current.isoformat()

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT venue,exposure_usd,positions,status FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if existing:
                if existing[0] != venue or Decimal(existing[1]) != requested or existing[2] != positions:
                    raise PortfolioAdmissionError("idempotency_key_payload_conflict")
                db.commit()
                return AdmissionResult(admission_id, existing[3] != "RELEASED",
                    "idempotent_existing_reservation", existing[1], existing[2], existing[3])

            # A RECONCILED reservation is still counted for snapshots captured
            # before reconciliation; those snapshots cannot yet include its fill.
            rows = db.execute(
                "SELECT exposure_usd,positions,status,reconciled_at FROM portfolio_admissions WHERE status!='RELEASED'"
            ).fetchall()
            pending_exposure = Decimal("0")
            pending_positions = 0
            for amount, count, status, reconciled_at in rows:
                if status == "RECONCILED" and reconciled_at and _aware(reconciled_at, "reconciled_at") <= captured:
                    continue
                pending_exposure += Decimal(amount)
                pending_positions += int(count)
            total_exposure = observed + pending_exposure + requested
            total_positions = snapshot_positions + pending_positions + positions
            if total_exposure > maximum:
                db.rollback()
                return AdmissionResult(admission_id, False, "combined_exposure_limit",
                    str(pending_exposure), pending_positions, "REJECTED")
            if total_positions > max_total_positions:
                db.rollback()
                return AdmissionResult(admission_id, False, "combined_position_limit",
                    str(pending_exposure), pending_positions, "REJECTED")
            db.execute(
                "INSERT INTO portfolio_admissions(admission_id,venue,exposure_usd,positions,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (admission_id, venue, str(requested), positions, "PENDING", created, created),
            )
            db.commit()
            return AdmissionResult(admission_id, True, "reserved", str(total_exposure), total_positions, "PENDING")
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(f"coordinator_storage_failure:{type(exc).__name__}") from None
        finally:
            db.close()

    def mark_committed(self, admission_id: str, *, now: datetime | None = None) -> None:
        self._transition(admission_id, "PENDING", "AWAITING_RECONCILIATION", now=now)

    def reconcile(self, admission_id: str, *, ledger_entry_id: str, now: datetime | None = None) -> None:
        if not isinstance(ledger_entry_id, str) or not ledger_entry_id.strip() or len(ledger_entry_id) > 200:
            raise PortfolioAdmissionError("ledger_entry_id_invalid")
        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,ledger_entry_id FROM portfolio_admissions WHERE admission_id=?", (admission_id,)).fetchone()
            if not row:
                raise PortfolioAdmissionError("reservation_not_found")
            if row[0] == "RECONCILED":
                if row[1] != ledger_entry_id:
                    raise PortfolioAdmissionError("reconciliation_entry_conflict")
                db.commit()
                return
            if row[0] != "AWAITING_RECONCILIATION":
                raise PortfolioAdmissionError("reservation_not_awaiting_reconciliation")
            db.execute("UPDATE portfolio_admissions SET status='RECONCILED',ledger_entry_id=?,reconciled_at=?,updated_at=? WHERE admission_id=?",
                (ledger_entry_id, stamp, stamp, admission_id))
            db.commit()
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(f"coordinator_storage_failure:{type(exc).__name__}") from None
        finally:
            db.close()

    def release(self, admission_id: str, *, confirmed_absent_from_ledger: bool, now: datetime | None = None) -> None:
        if confirmed_absent_from_ledger is not True:
            raise PortfolioAdmissionError("release_requires_verified_ledger_absence")
        # A caller may verify absence after a crash or failed venue-ledger
        # commit, when the reservation has already reached
        # AWAITING_RECONCILIATION. Both pre-commit and uncertain reservations
        # may be released, but only on this explicit verified-absence path.
        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE portfolio_admissions SET status='RELEASED',updated_at=? "
                "WHERE admission_id=? AND status IN ('PENDING','AWAITING_RECONCILIATION')",
                (stamp, admission_id),
            )
            if cursor.rowcount != 1:
                raise PortfolioAdmissionError("reservation_state_conflict_or_missing")
            db.commit()
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(f"coordinator_storage_failure:{type(exc).__name__}") from None
        finally:
            db.close()

    def _transition(self, admission_id: str, expected: str, target: str, *, now: datetime | None) -> None:
        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("UPDATE portfolio_admissions SET status=?,updated_at=? WHERE admission_id=? AND status=?",
                (target, stamp, admission_id, expected))
            if cursor.rowcount != 1:
                raise PortfolioAdmissionError("reservation_state_conflict_or_missing")
            db.commit()
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(f"coordinator_storage_failure:{type(exc).__name__}") from None
        finally:
            db.close()
