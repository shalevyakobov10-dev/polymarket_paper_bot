"""Read-only, fail-closed combined risk snapshot for the two Paper ledgers.

This module deliberately does not place orders or mutate either database. It
provides the first adapter boundary for the event-paper and isolated-margin
Perps engines while preserving their separate SQLite schemas.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote


class UnifiedPaperRiskError(RuntimeError):
    """A complete, fresh combined Paper risk snapshot could not be proven."""


@dataclass(frozen=True)
class UnifiedPaperRiskSnapshot:
    captured_at_utc: str
    utc_day: str
    polymarket_equity: str
    perps_equity: str
    combined_equity: str
    combined_starting_capital: str | None
    polymarket_peak_equity: str
    perps_peak_equity: str
    combined_peak_equity: str | None
    combined_drawdown_pct: str | None
    polymarket_daily_pnl: str
    perps_daily_pnl: str
    combined_daily_pnl: str
    polymarket_realized_today: str
    perps_realized_today: str
    polymarket_unrealized_pnl: str
    perps_unrealized_pnl: str
    polymarket_open_exposure: str
    perps_open_notional: str
    perps_margin_used: str
    combined_open_exposure: str
    combined_open_positions: int
    freshest_mark_age_seconds: str
    stalest_mark_age_seconds: str
    cross_venue_mark_skew_seconds: str
    perps_halted: bool
    marks_fresh: bool
    snapshot_complete: bool
    execution: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decimal(value: Any, field: str, *, allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise UnifiedPaperRiskError(f"{field}: missing or invalid numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise UnifiedPaperRiskError(f"{field}: missing or invalid numeric value") from None
    if not number.is_finite() or (not allow_negative and number < 0):
        raise UnifiedPaperRiskError(f"{field}: non-finite or negative numeric value")
    return number


def _text(value: Decimal) -> str:
    if not value.is_finite():
        raise UnifiedPaperRiskError("non-finite calculated risk value")
    return format(value.normalize(), "f") if value else "0"


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise UnifiedPaperRiskError(f"{field}: missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise UnifiedPaperRiskError(f"{field}: invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UnifiedPaperRiskError(f"{field}: timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _readonly(path: str | Path) -> sqlite3.Connection:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise UnifiedPaperRiskError(f"ledger is missing: {candidate.name}")
    uri = f"file:{quote(str(candidate.resolve()))}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection
    except sqlite3.Error as exc:
        raise UnifiedPaperRiskError(f"cannot open read-only ledger: {candidate.name}: {exc}") from None


def _require_columns(db: sqlite3.Connection, table: str, required: set[str]) -> None:
    try:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        raise UnifiedPaperRiskError(f"cannot inspect {table} schema: {exc}") from None
    found = {str(row["name"]) for row in rows}
    if not required.issubset(found):
        missing = ",".join(sorted(required - found))
        raise UnifiedPaperRiskError(f"{table} schema is incomplete: {missing}")


def _polymarket_snapshot(
    path: str | Path, now: datetime, max_age: float
) -> dict[str, Any]:
    db = _readonly(path)
    try:
        _require_columns(db, "account_state", {
            "cash", "starting_cash", "equity_peak", "day_key", "day_start_equity"
        })
        _require_columns(db, "trades", {
            "trade_id", "token_id", "opened_at", "shares", "notional", "entry_fee",
            "last_mark_price", "status", "closed_at", "pnl"
        })
        _require_columns(db, "observations", {"observation_id", "token_id", "observed_at"})
        account = db.execute(
            "SELECT cash,starting_cash,equity_peak,day_key,day_start_equity "
            "FROM account_state WHERE singleton_id=1"
        ).fetchone()
        if account is None:
            raise UnifiedPaperRiskError("Polymarket Paper account row is missing")
        day = now.date().isoformat()
        if str(account["day_key"]) != day:
            raise UnifiedPaperRiskError("Polymarket UTC-day baseline is not current")

        open_rows = db.execute(
            "SELECT trade_id,token_id,shares,notional,entry_fee,last_mark_price "
            "FROM trades WHERE status='OPEN' ORDER BY trade_id"
        ).fetchall()
        marks: list[datetime] = []
        unrealized = Decimal("0")
        exposure = Decimal("0")
        marked_value = Decimal("0")
        for row in open_rows:
            shares = _decimal(row["shares"], "Polymarket shares")
            notional = _decimal(row["notional"], "Polymarket notional")
            entry_fee = _decimal(row["entry_fee"], "Polymarket entry fee")
            mark = _decimal(row["last_mark_price"], "Polymarket last mark")
            if shares <= 0 or mark <= 0:
                raise UnifiedPaperRiskError("Polymarket open position has invalid size or mark")
            observation = db.execute(
                "SELECT observed_at FROM observations WHERE token_id=? "
                "ORDER BY observed_at DESC, observation_id DESC LIMIT 1",
                (str(row["token_id"]),),
            ).fetchone()
            if observation is None:
                raise UnifiedPaperRiskError("Polymarket open position has no timestamped market observation")
            observed_at = _timestamp(observation["observed_at"], "Polymarket observation")
            age = (now - observed_at).total_seconds()
            if age < -2 or age > max_age:
                raise UnifiedPaperRiskError("Polymarket open-position mark is future-dated or stale")
            marks.append(observed_at)
            value = shares * mark
            marked_value += value
            unrealized += value - notional - entry_fee
            exposure += max(value, notional + entry_fee)

        cash = _decimal(account["cash"], "Polymarket cash")
        equity = cash + marked_value
        realized_row = db.execute(
            "SELECT COALESCE(SUM(pnl),0) AS pnl FROM trades "
            "WHERE status='CLOSED' AND closed_at>=? AND closed_at<?",
            (f"{day}T00:00:00+00:00", now.isoformat()),
        ).fetchone()
        realized = _decimal(realized_row["pnl"], "Polymarket realized PnL", allow_negative=True)
        return {
            "equity": equity,
            "starting": _decimal(account["starting_cash"], "Polymarket starting capital"),
            "peak": _decimal(account["equity_peak"], "Polymarket peak equity"),
            "baseline": _decimal(account["day_start_equity"], "Polymarket day baseline"),
            "daily_pnl": equity - _decimal(account["day_start_equity"], "Polymarket day baseline"),
            "realized": realized,
            "unrealized": unrealized,
            "exposure": exposure,
            "positions": len(open_rows),
            "marks": marks,
        }
    except sqlite3.Error as exc:
        raise UnifiedPaperRiskError(f"Polymarket Paper ledger read failed: {exc}") from None
    finally:
        db.close()


def _perps_snapshot(
    path: str | Path, history_path: str | Path, now: datetime, max_age: float
) -> dict[str, Any]:
    db = _readonly(path)
    history = _readonly(history_path)
    try:
        _require_columns(db, "perps_paper_account", {
            "cash", "peak_equity", "utc_day", "day_start_equity", "halted"
        })
        _require_columns(db, "perps_paper_positions", {
            "position_id", "instrument_id", "symbol", "side", "quantity",
            "entry_price", "margin_balance", "entry_fee", "funding_paid",
            "last_mark", "status", "closed_at", "net_pnl"
        })
        _require_columns(history, "perps_market_snapshots", {
            "snapshot_id", "instrument_id", "ticker_timestamp_ms", "mark_price"
        })
        account = db.execute(
            "SELECT cash,peak_equity,utc_day,day_start_equity,halted "
            "FROM perps_paper_account WHERE account_id=1"
        ).fetchone()
        if account is None:
            raise UnifiedPaperRiskError("Perps Paper account row is missing")
        day = now.date().isoformat()
        if str(account["utc_day"]) != day:
            raise UnifiedPaperRiskError("Perps UTC-day baseline is not current")

        rows = db.execute(
            "SELECT position_id,instrument_id,symbol,side,quantity,entry_price,"
            "margin_balance,entry_fee,funding_paid,last_mark "
            "FROM perps_paper_positions WHERE status='OPEN' ORDER BY position_id"
        ).fetchall()
        marks: list[datetime] = []
        unrealized = Decimal("0")
        notional_total = Decimal("0")
        margin_total = Decimal("0")
        for row in rows:
            quantity = _decimal(row["quantity"], "Perps quantity")
            entry = _decimal(row["entry_price"], "Perps entry price", allow_negative=False)
            mark = _decimal(row["last_mark"], "Perps last mark", allow_negative=False)
            margin = _decimal(row["margin_balance"], "Perps margin")
            funding = _decimal(row["funding_paid"], "Perps funding paid", allow_negative=True)
            entry_fee = _decimal(row["entry_fee"], "Perps entry fee")
            if quantity <= 0 or entry <= 0 or mark <= 0 or str(row["side"]) not in {"LONG", "SHORT"}:
                raise UnifiedPaperRiskError("Perps open position has invalid side, size, or mark")
            mark_row = history.execute(
                "SELECT ticker_timestamp_ms,mark_price FROM perps_market_snapshots "
                "WHERE instrument_id=? ORDER BY ticker_timestamp_ms DESC,snapshot_id DESC LIMIT 1",
                (int(row["instrument_id"]),),
            ).fetchone()
            if mark_row is None:
                raise UnifiedPaperRiskError("Perps open position has no timestamped market mark")
            history_mark = _decimal(mark_row["mark_price"], "Perps history mark")
            if history_mark <= 0 or abs(history_mark - mark) > max(Decimal("0.00000001"), mark * Decimal("0.00000001")):
                raise UnifiedPaperRiskError("Perps position mark does not match latest public market history")
            observed_at = datetime.fromtimestamp(int(mark_row["ticker_timestamp_ms"]) / 1000, timezone.utc)
            age = (now - observed_at).total_seconds()
            if age < -2 or age > max_age:
                raise UnifiedPaperRiskError("Perps open-position mark is future-dated or stale")
            marks.append(observed_at)
            sign = Decimal("1") if str(row["side"]) == "LONG" else Decimal("-1")
            unrealized += (mark - entry) * quantity * sign
            notional_total += mark * quantity
            margin_total += margin

        cash = _decimal(account["cash"], "Perps cash")
        equity = cash + margin_total + unrealized
        realized_row = db.execute(
            "SELECT COALESCE(SUM(net_pnl),0) AS pnl FROM perps_paper_positions "
            "WHERE status IN ('CLOSED','LIQUIDATED') AND closed_at>=? AND closed_at<?",
            (f"{day}T00:00:00+00:00", now.isoformat()),
        ).fetchone()
        realized = _decimal(realized_row["pnl"], "Perps realized PnL", allow_negative=True)
        return {
            "equity": equity,
            "peak": _decimal(account["peak_equity"], "Perps peak equity"),
            "baseline": _decimal(account["day_start_equity"], "Perps day baseline"),
            "daily_pnl": equity - _decimal(account["day_start_equity"], "Perps day baseline"),
            "realized": realized,
            "unrealized": unrealized,
            "exposure": notional_total,
            "margin": margin_total,
            "positions": len(rows),
            "marks": marks,
            "halted": bool(account["halted"]),
        }
    except sqlite3.Error as exc:
        raise UnifiedPaperRiskError(f"Perps Paper ledger read failed: {exc}") from None
    finally:
        db.close()
        history.close()


def build_unified_paper_risk_snapshot(
    *,
    polymarket_db_path: str | Path,
    perps_db_path: str | Path,
    perps_history_db_path: str | Path,
    now: datetime | None = None,
    max_mark_age_seconds: float = 120.0,
    max_cross_venue_skew_seconds: float = 120.0,
) -> UnifiedPaperRiskSnapshot:
    """Build a complete, read-only combined snapshot; any uncertainty raises.

    This is a reporting/adapter primitive, not yet an order-admission lock. Both
    engines must not treat it as a shared risk gate until their entry paths are
    wired to a serialized admission transaction.
    """
    captured = now or datetime.now(timezone.utc)
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    captured = captured.astimezone(timezone.utc)
    if (
        isinstance(max_mark_age_seconds, bool)
        or not isinstance(max_mark_age_seconds, (int, float))
        or not math.isfinite(float(max_mark_age_seconds))
        or float(max_mark_age_seconds) <= 0
        or isinstance(max_cross_venue_skew_seconds, bool)
        or not isinstance(max_cross_venue_skew_seconds, (int, float))
        or not math.isfinite(float(max_cross_venue_skew_seconds))
        or float(max_cross_venue_skew_seconds) < 0
    ):
        raise ValueError("mark age and skew limits must be finite nonnegative values")
    paths = [Path(polymarket_db_path).expanduser().resolve(),
             Path(perps_db_path).expanduser().resolve(),
             Path(perps_history_db_path).expanduser().resolve()]
    if len(set(paths)) != 3:
        raise UnifiedPaperRiskError("Paper account and market-history databases must be distinct")

    poly = _polymarket_snapshot(paths[0], captured, float(max_mark_age_seconds))
    perps = _perps_snapshot(paths[1], paths[2], captured, float(max_mark_age_seconds))
    marks = poly["marks"] + perps["marks"]
    if marks:
        ages = [(captured - mark).total_seconds() for mark in marks]
        if any(age < -2 or age > float(max_mark_age_seconds) for age in ages):
            raise UnifiedPaperRiskError("combined Paper mark set is stale or future-dated")
        skew = max((mark.timestamp() for mark in marks)) - min((mark.timestamp() for mark in marks))
        if skew > float(max_cross_venue_skew_seconds):
            raise UnifiedPaperRiskError("cross-venue Paper mark timestamp skew exceeds limit")
        freshest, stalest = min(ages), max(ages)
    else:
        skew = 0.0
        freshest = stalest = 0.0

    equity = poly["equity"] + perps["equity"]
    # The current Perps schema does not persist immutable initial equity; do not
    # substitute peak equity or the current baseline and mislabel it as starting capital.
    starting = None
    # The two engines retain independent peak histories. Adding their peaks
    # would invent a combined high-water mark that may never have existed at
    # the same time, so combined drawdown remains unavailable until a shared
    # portfolio-level peak ledger is introduced.
    combined_daily = poly["daily_pnl"] + perps["daily_pnl"]
    return UnifiedPaperRiskSnapshot(
        captured_at_utc=captured.isoformat(),
        utc_day=captured.date().isoformat(),
        polymarket_equity=_text(poly["equity"]),
        perps_equity=_text(perps["equity"]),
        combined_equity=_text(equity),
        combined_starting_capital=_text(starting) if starting is not None else None,
        polymarket_peak_equity=_text(poly["peak"]),
        perps_peak_equity=_text(perps["peak"]),
        combined_peak_equity=None,
        combined_drawdown_pct=None,
        polymarket_daily_pnl=_text(poly["daily_pnl"]),
        perps_daily_pnl=_text(perps["daily_pnl"]),
        combined_daily_pnl=_text(combined_daily),
        polymarket_realized_today=_text(poly["realized"]),
        perps_realized_today=_text(perps["realized"]),
        polymarket_unrealized_pnl=_text(poly["unrealized"]),
        perps_unrealized_pnl=_text(perps["unrealized"]),
        polymarket_open_exposure=_text(poly["exposure"]),
        perps_open_notional=_text(perps["exposure"]),
        perps_margin_used=_text(perps["margin"]),
        combined_open_exposure=_text(poly["exposure"] + perps["exposure"]),
        combined_open_positions=poly["positions"] + perps["positions"],
        freshest_mark_age_seconds=_text(Decimal(str(freshest))),
        stalest_mark_age_seconds=_text(Decimal(str(stalest))),
        cross_venue_mark_skew_seconds=_text(Decimal(str(skew))),
        perps_halted=perps["halted"],
        marks_fresh=True,
        snapshot_complete=True,
    )

