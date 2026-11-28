"""Cross-process, Paper-only portfolio admission reservations.

This is a coordination primitive, not yet wired into either trading engine.
SQLite BEGIN IMMEDIATE serializes reservations across processes on the same
database. Reservations remain risk-consuming until the coordinator verifies the exact
entry ID and entry notional against the corresponding read-only venue ledger. Uncertain/crashed admissions therefore fail closed.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote


class PortfolioAdmissionError(RuntimeError):
    """Raised when an admission cannot be safely evaluated or persisted."""


def _amount(
    value: Any, field: str, *, allow_negative: bool = False
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PortfolioAdmissionError(f"{field}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PortfolioAdmissionError(f"{field}_invalid") from None
    if not result.is_finite() or (not allow_negative and result < 0):
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
    Then mark_committed after that commit, and use reconcile_from_ledger() to
    verify the exact entry ID and notional before reconciling the reservation. Never auto-expire uncertain rows.
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
        snapshot_exposure_usd: Any, snapshot_positions: int,
        snapshot_daily_pnl_usd: Any, max_combined_daily_loss_usd: Any,
        snapshot_at: datetime | str, max_snapshot_age_seconds: float,
        max_total_exposure_usd: Any, max_total_positions: int,
        now: datetime | None = None,
    ) -> AdmissionResult:
        if not isinstance(admission_id, str) or not admission_id.strip() or len(admission_id) > 160:
            raise PortfolioAdmissionError("admission_id_invalid")
        venue = str(venue).upper()
        if venue not in {"POLYMARKET", "PERPS"}:
            raise PortfolioAdmissionError("venue_invalid")
        requested = _amount(exposure_usd, "exposure_usd")
        observed = _amount(snapshot_exposure_usd, "snapshot_exposure_usd")
        daily_pnl = _amount(
            snapshot_daily_pnl_usd, "snapshot_daily_pnl_usd", allow_negative=True
        )
        daily_loss_limit = _amount(
            max_combined_daily_loss_usd, "max_combined_daily_loss_usd"
        )
        maximum = _amount(max_total_exposure_usd, "max_total_exposure_usd")
        if requested <= 0 or maximum <= 0:
            raise PortfolioAdmissionError("exposure_must_be_positive")
        if daily_loss_limit <= 0:
            raise PortfolioAdmissionError("combined_daily_loss_limit_must_be_positive")
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
                # An idempotent replay proves that this admission key already
                # exists; it must never authorize a second venue-ledger write.
                # PENDING/AWAITING require reconciliation, RECONCILED means the
                # original entry already exists, and RELEASED is terminal.
                return AdmissionResult(
                    admission_id, False, "idempotent_existing_reservation_no_new_entry",
                    existing[1], existing[2], existing[3],
                )

            # Combined realized + mark-to-market daily PnL is supplied by the
            # complete, fresh unified read-only snapshot. Reject new risk once
            # the portfolio-wide loss limit has been reached.
            if daily_pnl <= -daily_loss_limit:
                db.rollback()
                return AdmissionResult(
                    admission_id, False, "combined_daily_loss_limit",
                    "0", 0, "REJECTED",
                )

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

    @staticmethod
    def _verify_ledger_entry(
        *, venue: str, ledger_database_path: str | Path,
        ledger_entry_id: str, expected_exposure: Decimal,
        coordinator_path: Path,
    ) -> None:
        """Verify a durable venue-ledger row by immutable ID and entry notional."""
        if not isinstance(ledger_entry_id, str) or not ledger_entry_id.strip() or len(ledger_entry_id) > 32:
            raise PortfolioAdmissionError("ledger_entry_id_invalid")
        try:
            numeric_id = int(ledger_entry_id)
        except (TypeError, ValueError, OverflowError):
            raise PortfolioAdmissionError("ledger_entry_id_invalid") from None
        if numeric_id <= 0 or str(numeric_id) != ledger_entry_id:
            raise PortfolioAdmissionError("ledger_entry_id_invalid")

        candidate = Path(ledger_database_path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise PortfolioAdmissionError("ledger_database_missing_or_unsafe")
        try:
            resolved = candidate.resolve(strict=True)
            if resolved == coordinator_path.expanduser().resolve():
                raise PortfolioAdmissionError("ledger_database_must_be_distinct")
            uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
            ledger = sqlite3.connect(uri, uri=True, timeout=2.0)
            ledger.row_factory = sqlite3.Row
            ledger.execute("PRAGMA query_only=ON")
            ledger.execute("PRAGMA busy_timeout=2000")
        except PortfolioAdmissionError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise PortfolioAdmissionError("ledger_database_unavailable") from None

        try:
            if venue == "POLYMARKET":
                required = {"trade_id", "notional", "status"}
                columns = {
                    str(row["name"])
                    for row in ledger.execute("PRAGMA table_info(trades)").fetchall()
                }
                if not required.issubset(columns):
                    raise PortfolioAdmissionError("polymarket_ledger_schema_incomplete")
                row = ledger.execute(
                    "SELECT notional,status FROM trades WHERE trade_id=?",
                    (numeric_id,),
                ).fetchone()
                if row is None:
                    raise PortfolioAdmissionError("ledger_entry_not_found")
                if str(row["status"]) not in {"OPEN", "CLOSED"}:
                    raise PortfolioAdmissionError("ledger_entry_status_invalid")
                actual_exposure = _amount(row["notional"], "ledger_entry_notional")
            elif venue == "PERPS":
                required = {"position_id", "quantity", "entry_price", "status"}
                columns = {
                    str(row["name"])
                    for row in ledger.execute("PRAGMA table_info(perps_paper_positions)").fetchall()
                }
                if not required.issubset(columns):
                    raise PortfolioAdmissionError("perps_ledger_schema_incomplete")
                row = ledger.execute(
                    "SELECT quantity,entry_price,status FROM perps_paper_positions WHERE position_id=?",
                    (numeric_id,),
                ).fetchone()
                if row is None:
                    raise PortfolioAdmissionError("ledger_entry_not_found")
                if str(row["status"]) not in {"OPEN", "CLOSED", "LIQUIDATED"}:
                    raise PortfolioAdmissionError("ledger_entry_status_invalid")
                quantity = _amount(row["quantity"], "ledger_entry_quantity")
                entry_price = _amount(row["entry_price"], "ledger_entry_price")
                if quantity <= 0 or entry_price <= 0:
                    raise PortfolioAdmissionError("ledger_entry_size_invalid")
                actual_exposure = quantity * entry_price
            else:
                raise PortfolioAdmissionError("venue_invalid")

            tolerance = max(Decimal("0.00000001"), expected_exposure * Decimal("0.00000001"))
            if actual_exposure <= 0 or abs(actual_exposure - expected_exposure) > tolerance:
                raise PortfolioAdmissionError("ledger_entry_exposure_mismatch")
        except PortfolioAdmissionError:
            raise
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"ledger_verification_failed:{type(exc).__name__}"
            ) from None
        finally:
            ledger.close()

    @staticmethod
    def _find_ledger_entry_by_admission(
        *, venue: str, ledger_database_path: str | Path,
        admission_id: str, expected_exposure: Decimal,
        coordinator_path: Path,
    ) -> str:
        """Find exactly one ledger row carrying this immutable reservation ID."""
        if not isinstance(admission_id, str) or not admission_id.strip() or len(admission_id) > 160:
            raise PortfolioAdmissionError("admission_id_invalid")
        candidate = Path(ledger_database_path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise PortfolioAdmissionError("ledger_database_missing_or_unsafe")
        try:
            resolved = candidate.resolve(strict=True)
            if resolved == coordinator_path.expanduser().resolve():
                raise PortfolioAdmissionError("ledger_database_must_be_distinct")
            uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
            ledger = sqlite3.connect(uri, uri=True, timeout=2.0)
            ledger.row_factory = sqlite3.Row
            ledger.execute("PRAGMA query_only=ON")
            ledger.execute("PRAGMA busy_timeout=2000")
        except PortfolioAdmissionError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise PortfolioAdmissionError("ledger_database_unavailable") from None

        try:
            if venue == "POLYMARKET":
                table = "trades"
                id_column = "trade_id"
                required = {"trade_id", "notional", "status", "admission_id"}
                columns = {
                    str(row["name"])
                    for row in ledger.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not required.issubset(columns):
                    raise PortfolioAdmissionError("polymarket_ledger_admission_schema_incomplete")
                rows = ledger.execute(
                    "SELECT trade_id,notional,status FROM trades WHERE admission_id=? LIMIT 2",
                    (admission_id,),
                ).fetchall()
                if len(rows) != 1:
                    if not rows:
                        raise PortfolioAdmissionError("ledger_entry_not_found")
                    raise PortfolioAdmissionError("ledger_admission_id_not_unique")
                row = rows[0]
                if str(row["status"]) not in {"OPEN", "CLOSED"}:
                    raise PortfolioAdmissionError("ledger_entry_status_invalid")
                actual_exposure = _amount(row["notional"], "ledger_entry_notional")
            elif venue == "PERPS":
                table = "perps_paper_positions"
                id_column = "position_id"
                required = {"position_id", "quantity", "entry_price", "status", "admission_id"}
                columns = {
                    str(row["name"])
                    for row in ledger.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not required.issubset(columns):
                    raise PortfolioAdmissionError("perps_ledger_admission_schema_incomplete")
                rows = ledger.execute(
                    "SELECT position_id,quantity,entry_price,status "
                    "FROM perps_paper_positions WHERE admission_id=? LIMIT 2",
                    (admission_id,),
                ).fetchall()
                if len(rows) != 1:
                    if not rows:
                        raise PortfolioAdmissionError("ledger_entry_not_found")
                    raise PortfolioAdmissionError("ledger_admission_id_not_unique")
                row = rows[0]
                if str(row["status"]) not in {"OPEN", "CLOSED", "LIQUIDATED"}:
                    raise PortfolioAdmissionError("ledger_entry_status_invalid")
                quantity = _amount(row["quantity"], "ledger_entry_quantity")
                entry_price = _amount(row["entry_price"], "ledger_entry_price")
                if quantity <= 0 or entry_price <= 0:
                    raise PortfolioAdmissionError("ledger_entry_size_invalid")
                actual_exposure = quantity * entry_price
            else:
                raise PortfolioAdmissionError("venue_invalid")

            tolerance = max(
                Decimal("0.00000001"),
                expected_exposure * Decimal("0.00000001"),
            )
            if actual_exposure <= 0 or abs(actual_exposure - expected_exposure) > tolerance:
                raise PortfolioAdmissionError("ledger_entry_exposure_mismatch")
            return str(row[id_column])
        except PortfolioAdmissionError:
            raise
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"ledger_verification_failed:{type(exc).__name__}"
            ) from None
        finally:
            ledger.close()

    def reconcile_admission_from_ledger(
        self, admission_id: str, *, ledger_database_path: str | Path,
        now: datetime | None = None,
    ) -> str:
        """Recover a reservation by its exact durable admission_id after a crash.

        This handles the case where the venue ledger commit succeeded but the
        process crashed before mark_committed()/reconcile(). It may reconcile
        PENDING reservations only when exactly one matching immutable ledger
        row and the reserved notional are verified through a read-only DB.
        """
        db = self._connect()
        try:
            reservation = db.execute(
                "SELECT venue,exposure_usd,status,ledger_entry_id "
                "FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()
        if reservation is None:
            raise PortfolioAdmissionError("reservation_not_found")
        venue, exposure_text, status, stored_id = reservation
        if status not in {"PENDING", "AWAITING_RECONCILIATION", "RECONCILED"}:
            raise PortfolioAdmissionError("reservation_not_recoverable")
        entry_id = self._find_ledger_entry_by_admission(
            venue=str(venue),
            ledger_database_path=ledger_database_path,
            admission_id=admission_id,
            expected_exposure=_amount(exposure_text, "reserved_exposure"),
            coordinator_path=self.path,
        )
        if status == "RECONCILED" and str(stored_id) != entry_id:
            raise PortfolioAdmissionError("reconciliation_entry_conflict")

        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,ledger_entry_id FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if row is None:
                raise PortfolioAdmissionError("reservation_not_found")
            if row[0] == "RECONCILED":
                if str(row[1]) != entry_id:
                    raise PortfolioAdmissionError("reconciliation_entry_conflict")
                db.commit()
                return entry_id
            if row[0] not in {"PENDING", "AWAITING_RECONCILIATION"}:
                raise PortfolioAdmissionError("reservation_state_conflict_or_missing")
            db.execute(
                "UPDATE portfolio_admissions SET status='RECONCILED',ledger_entry_id=?,"
                "reconciled_at=?,updated_at=? WHERE admission_id=?",
                (entry_id, stamp, stamp, admission_id),
            )
            db.commit()
            return entry_id
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()

    def release_admission_if_absent_from_ledger(
        self, admission_id: str, *, ledger_database_path: str | Path,
        now: datetime | None = None,
    ) -> None:
        """Release a PENDING reservation only if a trusted ledger has no row.

        This path is intended for a failed/rolled-back write or recovery after
        the writer has been proven quiescent. It deliberately cannot release
        AWAITING_RECONCILIATION, whose commit outcome may still be uncertain.
        """
        db = self._connect()
        try:
            reservation = db.execute(
                "SELECT venue,status FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()
        if reservation is None:
            raise PortfolioAdmissionError("reservation_not_found")
        venue, status = reservation
        if status != "PENDING":
            raise PortfolioAdmissionError("awaiting_reconciliation_requires_recovery_lock")

        candidate = Path(ledger_database_path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise PortfolioAdmissionError("ledger_database_missing_or_unsafe")
        try:
            resolved = candidate.resolve(strict=True)
            if resolved == self.path.expanduser().resolve():
                raise PortfolioAdmissionError("ledger_database_must_be_distinct")
            uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
            ledger = sqlite3.connect(uri, uri=True, timeout=2.0)
            ledger.execute("PRAGMA query_only=ON")
            ledger.execute("PRAGMA busy_timeout=2000")
        except PortfolioAdmissionError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise PortfolioAdmissionError("ledger_database_unavailable") from None
        try:
            if venue == "POLYMARKET":
                table, id_column = "trades", "trade_id"
            elif venue == "PERPS":
                table, id_column = "perps_paper_positions", "position_id"
            else:
                raise PortfolioAdmissionError("venue_invalid")
            columns = {
                str(row[1])
                for row in ledger.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not {id_column, "admission_id"}.issubset(columns):
                raise PortfolioAdmissionError("ledger_admission_schema_incomplete")
            rows = ledger.execute(
                f"SELECT {id_column} FROM {table} WHERE admission_id=? LIMIT 1",
                (admission_id,),
            ).fetchone()
            if rows is not None:
                raise PortfolioAdmissionError("ledger_entry_present_cannot_release")
        except PortfolioAdmissionError:
            raise
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"ledger_absence_verification_failed:{type(exc).__name__}"
            ) from None
        finally:
            ledger.close()

        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE portfolio_admissions SET status='RELEASED',updated_at=? "
                "WHERE admission_id=? AND status='PENDING'",
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
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()

    def reconcile_from_ledger(
        self, admission_id: str, *, ledger_database_path: str | Path,
        ledger_entry_id: str, now: datetime | None = None,
    ) -> None:
        """Reconcile only after this coordinator verifies the exact venue row.

        The venue ledger is opened read-only. The immutable entry ID, valid
        lifecycle status, and saved entry notional must match the reservation.
        Uncertain or absent entries stay unresolved and continue consuming risk.
        """
        if not isinstance(admission_id, str) or not admission_id.strip() or len(admission_id) > 160:
            raise PortfolioAdmissionError("admission_id_invalid")
        db = self._connect()
        try:
            reservation = db.execute(
                "SELECT venue,exposure_usd,status,ledger_entry_id "
                "FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()
        if reservation is None:
            raise PortfolioAdmissionError("reservation_not_found")
        venue, exposure_text, status, stored_entry_id = reservation
        if status == "RECONCILED" and stored_entry_id != ledger_entry_id:
            raise PortfolioAdmissionError("reconciliation_entry_conflict")
        if status not in {"AWAITING_RECONCILIATION", "RECONCILED"}:
            raise PortfolioAdmissionError("reservation_not_awaiting_reconciliation")

        self._verify_ledger_entry(
            venue=str(venue),
            ledger_database_path=ledger_database_path,
            ledger_entry_id=ledger_entry_id,
            expected_exposure=_amount(exposure_text, "reserved_exposure"),
            coordinator_path=self.path,
        )

        db = self._connect()
        stamp = _aware(now or datetime.now(timezone.utc), "now").isoformat()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,ledger_entry_id FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
            if not row:
                raise PortfolioAdmissionError("reservation_not_found")
            if row[0] == "RECONCILED":
                if row[1] != ledger_entry_id:
                    raise PortfolioAdmissionError("reconciliation_entry_conflict")
                db.commit()
                return
            if row[0] != "AWAITING_RECONCILIATION":
                raise PortfolioAdmissionError("reservation_state_conflict_or_missing")
            db.execute(
                "UPDATE portfolio_admissions SET status='RECONCILED',ledger_entry_id=?,"
                "reconciled_at=?,updated_at=? WHERE admission_id=?",
                (ledger_entry_id, stamp, stamp, admission_id),
            )
            db.commit()
        except PortfolioAdmissionError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()

    def list_unresolved(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """List reservations that still require ledger reconciliation.

        This is a read-only recovery aid. It never releases, expires, or
        authorizes an admission; callers must inspect the corresponding venue
        ledger and use the explicit verified reconcile/release transitions.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise PortfolioAdmissionError("unresolved_limit_invalid")
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT admission_id,venue,exposure_usd,positions,status,created_at,updated_at "
                "FROM portfolio_admissions WHERE status IN ('PENDING','AWAITING_RECONCILIATION') "
                "ORDER BY created_at,admission_id LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "admission_id": str(row[0]),
                    "venue": str(row[1]),
                    "exposure_usd": str(row[2]),
                    "positions": int(row[3]),
                    "status": str(row[4]),
                    "created_at": str(row[5]),
                    "updated_at": str(row[6]),
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(f"coordinator_storage_failure:{type(exc).__name__}") from None
        finally:
            db.close()

    def release_from_ledger(
        self, admission_id: str, *, ledger_database_path: str | Path,
        ledger_entry_id: str, now: datetime | None = None,
    ) -> None:
        """Release only when a valid read-only venue ledger proves ID absence.

        A missing/unreadable database, incomplete schema, malformed ID, any row at
        the requested ID, or an AWAITING_RECONCILIATION state keeps the
        reservation risk-consuming. This simple path releases PENDING only.
        This is intentionally conservative for uncertain commit outcomes.
        """
        if not isinstance(admission_id, str) or not admission_id.strip() or len(admission_id) > 160:
            raise PortfolioAdmissionError("admission_id_invalid")
        db = self._connect()
        try:
            reservation = db.execute(
                "SELECT venue,exposure_usd,status FROM portfolio_admissions WHERE admission_id=?",
                (admission_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
        finally:
            db.close()
        if reservation is None:
            raise PortfolioAdmissionError("reservation_not_found")
        venue, exposure_text, status = reservation
        # Once a venue write has been attempted/committed, a missing row is
        # not sufficient proof that the outcome is safe to release. A separate
        # recovery coordinator must first establish writer quiescence.
        if status != "PENDING":
            raise PortfolioAdmissionError("awaiting_reconciliation_requires_recovery_lock")

        try:
            self._verify_ledger_entry(
                venue=str(venue),
                ledger_database_path=ledger_database_path,
                ledger_entry_id=ledger_entry_id,
                expected_exposure=_amount(exposure_text, "reserved_exposure"),
                coordinator_path=self.path,
            )
        except PortfolioAdmissionError as exc:
            if exc.args != ("ledger_entry_not_found",):
                raise
        else:
            raise PortfolioAdmissionError("ledger_entry_present_cannot_release")

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
            raise PortfolioAdmissionError(
                f"coordinator_storage_failure:{type(exc).__name__}"
            ) from None
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
