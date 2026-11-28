"""Shared fail-closed Paper admission runtime for Event and Perps engines.

This module performs only read-only source-ledger snapshotting and durable
reservation/recovery. It does not open, sign, submit, or cancel venue orders.
The caller must reserve before its durable Paper-ledger insert, persist the
returned admission_id into that row, commit, and then reconcile by admission ID.
"""
from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from portfolio_admission import (
    AdmissionResult,
    PortfolioAdmissionCoordinator,
    PortfolioAdmissionError,
)
from unified_paper_risk import (
    UnifiedPaperRiskError,
    build_unified_paper_risk_snapshot,
)


class PaperAdmissionRuntimeError(RuntimeError):
    """A shared Paper admission snapshot or reservation could not be proven safe."""


def _positive_decimal_env(name: str, default: str) -> Decimal:
    value = os.getenv(name, default)
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise PaperAdmissionRuntimeError(f"{name}_invalid") from None
    if not result.is_finite() or result <= 0:
        raise PaperAdmissionRuntimeError(f"{name}_must_be_positive")
    return result


def _positive_int_env(name: str, default: int, *, maximum: int) -> int:
    value = os.getenv(name, str(default))
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise PaperAdmissionRuntimeError(f"{name}_invalid") from None
    if isinstance(result, bool) or not 1 <= result <= maximum:
        raise PaperAdmissionRuntimeError(f"{name}_out_of_range")
    return result


def _bounded_seconds_env(name: str, default: float, *, maximum: float) -> float:
    value = os.getenv(name, str(default))
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PaperAdmissionRuntimeError(f"{name}_invalid") from None
    if not math.isfinite(result) or not 1 <= result <= maximum:
        raise PaperAdmissionRuntimeError(f"{name}_out_of_range")
    return result


def build_admission_id(*, venue: str, source_identity: str, strategy_version: str) -> str:
    """Create a stable, opaque key from venue, immutable source and strategy version."""
    normalized_venue = str(venue).upper()
    if normalized_venue not in {"POLYMARKET", "PERPS"}:
        raise PaperAdmissionRuntimeError("venue_invalid")
    if (
        not isinstance(source_identity, str)
        or not source_identity.strip()
        or len(source_identity) > 4096
    ):
        raise PaperAdmissionRuntimeError("source_identity_invalid")
    if (
        not isinstance(strategy_version, str)
        or not strategy_version.strip()
        or len(strategy_version) > 80
    ):
        raise PaperAdmissionRuntimeError("strategy_version_invalid")
    payload = "\n".join((normalized_venue, strategy_version, source_identity))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{normalized_venue.lower()}:{digest}"


@dataclass(frozen=True)
class PaperAdmissionTicket:
    admission_id: str
    venue: str
    ledger_database_path: Path
    result: AdmissionResult
    snapshot_at_utc: str


@dataclass(frozen=True)
class PaperAdmissionRecoveryReport:
    scanned: int
    reconciled: tuple[str, ...]
    still_pending: tuple[str, ...]
    blocked: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "reconciled_admission_ids": list(self.reconciled),
            "still_pending_admission_ids": list(self.still_pending),
            "blocked_admission_ids": list(self.blocked),
            "automatic_release_performed": False,
            "order_submission_allowed": False,
            "execution": "none",
        }


class PaperAdmissionRuntime:
    """One shared portfolio admission façade used by both Paper engines."""

    def __init__(
        self,
        *,
        polymarket_db_path: str | Path,
        perps_db_path: str | Path,
        perps_history_db_path: str | Path,
        coordinator_db_path: str | Path,
        max_snapshot_age_seconds: float = 120.0,
        max_cross_venue_skew_seconds: float = 120.0,
        max_total_exposure_usd: Decimal = Decimal("95"),
        max_total_positions: int = 8,
        max_combined_daily_loss_usd: Decimal = Decimal("35"),
    ) -> None:
        paths = [
            Path(polymarket_db_path).expanduser().resolve(),
            Path(perps_db_path).expanduser().resolve(),
            Path(perps_history_db_path).expanduser().resolve(),
            Path(coordinator_db_path).expanduser().resolve(),
        ]
        if len(set(paths)) != 4:
            raise PaperAdmissionRuntimeError("all_admission_database_paths_must_be_distinct")
        for value, name, minimum in (
            (max_snapshot_age_seconds, "max_snapshot_age_seconds", 1),
            (max_cross_venue_skew_seconds, "max_cross_venue_skew_seconds", 0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < minimum
                or float(value) > 3600
            ):
                raise PaperAdmissionRuntimeError(f"{name}_invalid")
        if not isinstance(max_total_positions, int) or isinstance(max_total_positions, bool) or not 1 <= max_total_positions <= 1000:
            raise PaperAdmissionRuntimeError("max_total_positions_invalid")
        self.polymarket_db_path, self.perps_db_path, self.perps_history_db_path, self.coordinator_db_path = paths
        self.max_snapshot_age_seconds = float(max_snapshot_age_seconds)
        self.max_cross_venue_skew_seconds = float(max_cross_venue_skew_seconds)
        self.max_total_exposure_usd = Decimal(str(max_total_exposure_usd))
        self.max_total_positions = max_total_positions
        self.max_combined_daily_loss_usd = Decimal(str(max_combined_daily_loss_usd))
        if (
            not self.max_total_exposure_usd.is_finite()
            or self.max_total_exposure_usd <= 0
            or not self.max_combined_daily_loss_usd.is_finite()
            or self.max_combined_daily_loss_usd <= 0
        ):
            raise PaperAdmissionRuntimeError("combined_risk_limits_invalid")
        self.coordinator = PortfolioAdmissionCoordinator(self.coordinator_db_path)

    @classmethod
    def from_env(
        cls,
        *,
        polymarket_db_path: str | Path,
        perps_db_path: str | Path | None = None,
        perps_history_db_path: str | Path | None = None,
    ) -> "PaperAdmissionRuntime":
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        perps_path = perps_db_path or os.getenv(
            "PERPS_PAPER_DB", str(data_dir / "perps_paper.sqlite3")
        )
        history_path = perps_history_db_path or os.getenv(
            "PERPS_MARKET_HISTORY_DB", str(data_dir / "perps_market_history.sqlite3")
        )
        coordinator_path = os.getenv(
            "PAPER_ADMISSION_DB", str(data_dir / "portfolio_admission.sqlite3")
        )
        return cls(
            polymarket_db_path=polymarket_db_path,
            perps_db_path=perps_path,
            perps_history_db_path=history_path,
            coordinator_db_path=coordinator_path,
            max_snapshot_age_seconds=_bounded_seconds_env(
                "PAPER_COMBINED_RISK_MAX_SNAPSHOT_AGE_SECONDS", 120.0, maximum=3600
            ),
            max_cross_venue_skew_seconds=_bounded_seconds_env(
                "PAPER_COMBINED_RISK_MAX_MARK_SKEW_SECONDS", 120.0, maximum=3600
            ),
            max_total_exposure_usd=_positive_decimal_env(
                "PAPER_COMBINED_MAX_OPEN_EXPOSURE_USD", "95"
            ),
            max_total_positions=_positive_int_env(
                "PAPER_COMBINED_MAX_OPEN_POSITIONS", 8, maximum=1000
            ),
            max_combined_daily_loss_usd=_positive_decimal_env(
                "PAPER_COMBINED_DAILY_LOSS_LIMIT_USD", "35"
            ),
        )

    def reserve(
        self, *, venue: str, source_identity: str, strategy_version: str,
        exposure_usd: Any, positions: int = 1, now: datetime | None = None,
    ) -> PaperAdmissionTicket:
        captured = now or datetime.now(timezone.utc)
        if captured.tzinfo is None or captured.utcoffset() is None:
            raise PaperAdmissionRuntimeError("now_must_be_timezone_aware")
        captured = captured.astimezone(timezone.utc)
        admission_id = build_admission_id(
            venue=venue,
            source_identity=source_identity,
            strategy_version=strategy_version,
        )
        ledger_path = (
            self.polymarket_db_path if str(venue).upper() == "POLYMARKET"
            else self.perps_db_path if str(venue).upper() == "PERPS"
            else None
        )
        if ledger_path is None:
            raise PaperAdmissionRuntimeError("venue_invalid")

        try:
            snapshot = build_unified_paper_risk_snapshot(
                polymarket_db_path=self.polymarket_db_path,
                perps_db_path=self.perps_db_path,
                perps_history_db_path=self.perps_history_db_path,
                now=captured,
                max_mark_age_seconds=self.max_snapshot_age_seconds,
                max_cross_venue_skew_seconds=self.max_cross_venue_skew_seconds,
            )
        except (UnifiedPaperRiskError, ValueError, OSError) as exc:
            raise PaperAdmissionRuntimeError(
                f"unified_paper_snapshot_unavailable:{type(exc).__name__}"
            ) from None

        if snapshot.snapshot_complete is not True or snapshot.marks_fresh is not True:
            raise PaperAdmissionRuntimeError("unified_paper_snapshot_not_complete_and_fresh")
        result = self.coordinator.reserve(
            admission_id=admission_id,
            venue=str(venue).upper(),
            exposure_usd=exposure_usd,
            positions=positions,
            snapshot_exposure_usd=snapshot.combined_open_exposure,
            snapshot_positions=snapshot.combined_open_positions,
            snapshot_daily_pnl_usd=snapshot.combined_daily_pnl,
            max_combined_daily_loss_usd=self.max_combined_daily_loss_usd,
            snapshot_at=snapshot.captured_at_utc,
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
            max_total_exposure_usd=self.max_total_exposure_usd,
            max_total_positions=self.max_total_positions,
            now=captured,
        )
        return PaperAdmissionTicket(
            admission_id=admission_id,
            venue=str(venue).upper(),
            ledger_database_path=ledger_path,
            result=result,
            snapshot_at_utc=snapshot.captured_at_utc,
        )

    def reconcile(self, ticket: PaperAdmissionTicket, *, now: datetime | None = None) -> str:
        if not isinstance(ticket, PaperAdmissionTicket) or not ticket.result.approved:
            raise PaperAdmissionRuntimeError("admission_ticket_not_approved")
        try:
            return self.coordinator.reconcile_admission_from_ledger(
                ticket.admission_id,
                ledger_database_path=ticket.ledger_database_path,
                now=now,
            )
        except PortfolioAdmissionError as exc:
            raise PaperAdmissionRuntimeError(
                f"admission_reconciliation_unresolved:{str(exc)[:120]}"
            ) from None

    def recover_unresolved(self, *, now: datetime | None = None) -> PaperAdmissionRecoveryReport:
        """Reconcile exact persisted entries; never auto-release a missing one."""
        rows = self.coordinator.list_unresolved(limit=1000)
        reconciled: list[str] = []
        still_pending: list[str] = []
        blocked: list[str] = []
        for row in rows:
            admission_id = row["admission_id"]
            venue = row["venue"]
            ledger_path = (
                self.polymarket_db_path if venue == "POLYMARKET"
                else self.perps_db_path if venue == "PERPS"
                else None
            )
            if ledger_path is None:
                blocked.append(admission_id)
                continue
            try:
                self.coordinator.reconcile_admission_from_ledger(
                    admission_id,
                    ledger_database_path=ledger_path,
                    now=now,
                )
                reconciled.append(admission_id)
            except PortfolioAdmissionError as exc:
                if "ledger_entry_not_found" in str(exc):
                    still_pending.append(admission_id)
                else:
                    blocked.append(admission_id)
        return PaperAdmissionRecoveryReport(
            scanned=len(rows),
            reconciled=tuple(reconciled),
            still_pending=tuple(still_pending),
            blocked=tuple(blocked),
        )
