"""Isolated SQLite paper execution engine for Polymarket Perps.

No network access, exchange account, signer, credential, order endpoint, or
wallet operation exists in this module. Entries can only be simulated from an
already validated public snapshot supplied by a caller. Stop/target exits and
funding observations are managed locally in paper state.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Mapping

from perps_risk import PerpsRiskLimits, evaluate_perps_entry


def _d(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid_{field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid_{field}") from None
    if not result.is_finite():
        raise ValueError(f"invalid_{field}")
    return result


def _now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(now: datetime | None = None) -> str:
    return _now(now).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _quantum(decimals: int) -> Decimal:
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise ValueError("invalid_quantity_decimals")
    return Decimal("1").scaleb(-decimals)


def sweep_perps_book(
    snapshot: Mapping[str, Any],
    *,
    order_side: str,
    quantity: Any,
    max_impact_bps: Any = "100",
) -> dict[str, Any]:
    """Sweep the visible book and return VWAP; reject incomplete or costly fills."""
    side = order_side.strip().upper() if isinstance(order_side, str) else ""
    if side not in {"BUY", "SELL"}:
        return {"filled": False, "reason": "invalid_order_side"}
    try:
        requested = _d(quantity, "quantity")
        max_impact = _d(max_impact_bps, "max_impact_bps")
        if requested <= 0 or max_impact <= 0:
            return {"filled": False, "reason": "invalid_fill_input"}
        levels_key = "asks" if side == "BUY" else "bids"
        levels = snapshot.get(levels_key)
        if not isinstance(levels, list) or not levels:
            return {"filled": False, "reason": "missing_book_depth"}
        remaining = requested
        total_quantity = Decimal("0")
        total_quote = Decimal("0")
        levels_consumed = 0
        for level in levels:
            if not isinstance(level, Mapping):
                return {"filled": False, "reason": "invalid_book_level"}
            price = _d(level.get("price"), "book_price")
            available = _d(level.get("quantity"), "book_quantity")
            if price <= 0 or available <= 0:
                return {"filled": False, "reason": "invalid_book_level"}
            take = min(remaining, available)
            if take > 0:
                levels_consumed += 1
            total_quantity += take
            total_quote += take * price
            remaining -= take
            if remaining <= 0:
                break
        if remaining > Decimal("0"):
            return {
                "filled": False,
                "reason": "insufficient_visible_depth",
                "requested_quantity": str(requested),
                "filled_quantity": str(total_quantity),
            }
        vwap = total_quote / total_quantity
        best_price = _d(levels[0].get("price"), "best_price")
        impact_bps = abs(vwap - best_price) / best_price * Decimal("10000")
        if impact_bps > max_impact:
            return {
                "filled": False,
                "reason": "paper_market_impact_above_limit",
                "impact_bps": str(impact_bps),
            }
        return {
            "filled": True,
            "order_side": side,
            "quantity": str(total_quantity),
            "vwap": str(vwap),
            "notional": str(total_quote),
            "impact_bps": str(impact_bps),
            "levels_consumed": levels_consumed,
            "execution": "paper_simulation_only",
        }
    except (ValueError, TypeError, ArithmeticError):
        return {"filled": False, "reason": "invalid_or_incomplete_order_book"}


class PerpsPaperEngine:
    """Persistent paper account with margin reservation and protective exits."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        starting_cash: Any = "100",
        limits: PerpsRiskLimits | None = None,
        taker_fee_rate: Any = "0.0005",
        max_entry_impact_bps: Any = "100",
        max_market_data_age_seconds: Any = "10",
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_cash = _d(starting_cash, "starting_cash")
        self.taker_fee_rate = _d(taker_fee_rate, "taker_fee_rate")
        self.max_entry_impact_bps = _d(max_entry_impact_bps, "max_entry_impact_bps")
        self.max_market_data_age_seconds = _d(max_market_data_age_seconds, "max_market_data_age_seconds")
        if self.starting_cash <= 0 or self.taker_fee_rate < 0:
            raise ValueError("invalid_paper_account_configuration")
        if self.max_entry_impact_bps <= 0 or not Decimal("1") <= self.max_market_data_age_seconds <= Decimal("10"):
            raise ValueError("invalid_paper_execution_configuration")
        self.limits = limits or PerpsRiskLimits()
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS perps_paper_account (
                    account_id INTEGER PRIMARY KEY CHECK(account_id=1),
                    cash_balance TEXT NOT NULL,
                    peak_equity TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS perps_paper_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    ticker_timestamp_ms INTEGER NOT NULL,
                    book_timestamp_ms INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    UNIQUE(instrument_id, ticker_timestamp_ms, book_timestamp_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_perps_paper_snapshots_symbol_time
                    ON perps_paper_snapshots(symbol, observed_at);
                CREATE TABLE IF NOT EXISTS perps_paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    instrument_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
                    quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    current_mark TEXT NOT NULL,
                    current_mark_at TEXT NOT NULL,
                    leverage TEXT NOT NULL,
                    initial_margin TEXT NOT NULL,
                    entry_fee TEXT NOT NULL,
                    funding_paid TEXT NOT NULL DEFAULT '0',
                    funding_rate TEXT NOT NULL DEFAULT '0',
                    next_funding_ms INTEGER NOT NULL,
                    funding_status TEXT NOT NULL DEFAULT 'tracking',
                    stop_price TEXT NOT NULL,
                    take_profit_price TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                    closed_at TEXT,
                    exit_price TEXT,
                    exit_fee TEXT,
                    gross_pnl TEXT,
                    net_pnl TEXT,
                    close_reason TEXT,
                    risk_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_perps_paper_trades_status
                    ON perps_paper_trades(status, instrument_id);
                CREATE INDEX IF NOT EXISTS idx_perps_paper_trades_closed_at
                    ON perps_paper_trades(closed_at);
                CREATE TABLE IF NOT EXISTS perps_strategy_decisions (
                    instrument_id INTEGER NOT NULL,
                    bar_close_ms INTEGER NOT NULL,
                    decision_status TEXT NOT NULL,
                    side TEXT,
                    reason TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    trade_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(instrument_id, bar_close_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_perps_strategy_decisions_created_at
                    ON perps_strategy_decisions(created_at);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO perps_paper_account
                    (account_id, cash_balance, peak_equity, updated_at)
                VALUES (1, ?, ?, ?)
                """,
                (str(self.starting_cash), str(self.starting_cash), _iso()),
            )

    def _validate_snapshot(
        self,
        snapshot: Mapping[str, Any],
        now: datetime,
        *,
        allow_close_only: bool = False,
    ) -> None:
        if not isinstance(snapshot, Mapping):
            raise ValueError("invalid_snapshot")
        if snapshot.get("research_only") is not True or snapshot.get("execution") != "none":
            raise ValueError("snapshot_not_research_only")
        if not allow_close_only and (
            snapshot.get("entry_eligible") is not True or snapshot.get("close_only") is True
        ):
            raise ValueError("instrument_not_entry_eligible")
        if snapshot.get("settlement") is True:
            raise ValueError("instrument_settled")
        observed = _parse_time(str(snapshot.get("observed_at", "")))
        if (now - observed).total_seconds() < -2:
            raise ValueError("future_snapshot")
        for field in ("ticker_age_seconds", "book_age_seconds"):
            age = _d(snapshot.get(field), field)
            if age < 0 or age > self.max_market_data_age_seconds:
                raise ValueError("stale_market_data")
        _d(snapshot.get("mark_price"), "mark_price")
        _d(snapshot.get("index_price"), "index_price")
        int(snapshot["instrument_id"])
        if not str(snapshot.get("symbol", "")).strip():
            raise ValueError("missing_instrument_symbol")

    def _record_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: Mapping[str, Any],
    ) -> None:
        serialized = json.dumps(
            dict(snapshot), ensure_ascii=False, sort_keys=True, allow_nan=False,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO perps_paper_snapshots
                (instrument_id, symbol, observed_at, ticker_timestamp_ms,
                 book_timestamp_ms, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(snapshot["instrument_id"]),
                str(snapshot["symbol"]),
                str(snapshot["observed_at"]),
                int(snapshot["ticker_timestamp_ms"]),
                int(snapshot["book_timestamp_ms"]),
                serialized,
            ),
        )

    def _update_mark_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: Mapping[str, Any],
        now: datetime,
    ) -> None:
        self._record_snapshot_locked(connection, snapshot)
        instrument_id = int(snapshot["instrument_id"])
        connection.execute(
            """
            UPDATE perps_paper_trades
            SET current_mark=?, current_mark_at=?
            WHERE instrument_id=? AND status='OPEN'
            """,
            (str(snapshot["mark_price"]), now.isoformat(), instrument_id),
        )
        self._apply_funding_locked(connection, instrument_id, snapshot)

    @staticmethod
    def _funding_interval_ms(value: Any) -> int | None:
        if not isinstance(value, str) or len(value) < 2:
            return None
        unit = value[-1].lower()
        try:
            count = int(value[:-1])
        except (TypeError, ValueError, OverflowError):
            return None
        units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
        if count <= 0 or unit not in units:
            return None
        return count * units[unit]

    def _apply_funding_locked(
        self,
        connection: sqlite3.Connection,
        instrument_id: int,
        snapshot: Mapping[str, Any],
    ) -> None:
        new_next = int(snapshot["next_funding_ms"])
        new_rate = _d(snapshot.get("funding_rate", "0"), "funding_rate")
        interval_ms = self._funding_interval_ms(snapshot.get("funding_interval"))
        rows = connection.execute(
            """
            SELECT trade_id, side, quantity, current_mark, funding_paid,
                   funding_rate, next_funding_ms, funding_status
            FROM perps_paper_trades
            WHERE instrument_id=? AND status='OPEN'
            """,
            (instrument_id,),
        ).fetchall()
        for row in rows:
            old_next = int(row["next_funding_ms"])
            status = str(row["funding_status"])
            if status != "tracking" or new_next <= old_next:
                continue
            if interval_ms is None or abs((new_next - old_next) - interval_ms) > 300_000:
                connection.execute(
                    "UPDATE perps_paper_trades SET funding_status='gap' WHERE trade_id=?",
                    (row["trade_id"],),
                )
                continue
            # Funding sign: positive rate means longs pay shorts; negative rate
            # means shorts pay longs. Apply the prior rate to the interval that
            # has just completed, not the newly published rate.
            direction = Decimal("1") if row["side"] == "LONG" else Decimal("-1")
            cost = _d(row["quantity"], "quantity") * _d(row["current_mark"], "mark") * _d(row["funding_rate"], "funding_rate") * direction
            paid = _d(row["funding_paid"], "funding_paid") + cost
            connection.execute(
                """
                UPDATE perps_paper_trades
                SET funding_paid=?, funding_rate=?, next_funding_ms=?
                WHERE trade_id=?
                """,
                (str(paid), str(new_rate), new_next, row["trade_id"]),
            )
            account = connection.execute(
                "SELECT cash_balance FROM perps_paper_account WHERE account_id=1"
            ).fetchone()
            cash = _d(account["cash_balance"], "cash_balance") - cost
            connection.execute(
                "UPDATE perps_paper_account SET cash_balance=?, updated_at=? WHERE account_id=1",
                (str(cash), _iso()),
            )

    def _metrics_locked(self, connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
        account = connection.execute(
            "SELECT cash_balance, peak_equity FROM perps_paper_account WHERE account_id=1"
        ).fetchone()
        cash = _d(account["cash_balance"], "cash_balance")
        peak = _d(account["peak_equity"], "peak_equity")
        rows = connection.execute(
            "SELECT * FROM perps_paper_trades WHERE status='OPEN'"
        ).fetchall()
        margin = Decimal("0")
        unrealized = Decimal("0")
        exposure = Decimal("0")
        funding_reserve = Decimal("0")
        open_positions = len(rows)
        for row in rows:
            quantity = _d(row["quantity"], "quantity")
            entry = _d(row["entry_price"], "entry_price")
            mark = _d(row["current_mark"], "current_mark")
            notional = quantity * mark
            direction = Decimal("1") if row["side"] == "LONG" else Decimal("-1")
            margin += _d(row["initial_margin"], "initial_margin")
            unrealized += (mark - entry) * quantity * direction
            exposure += notional
            funding_reserve += notional * self.limits.funding_reserve_rate
        equity = cash + margin + unrealized
        if equity > peak:
            peak = equity
            connection.execute(
                "UPDATE perps_paper_account SET peak_equity=?, updated_at=? WHERE account_id=1",
                (str(peak), now.isoformat()),
            )
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        closed = connection.execute(
            """
            SELECT COALESCE(SUM(CAST(net_pnl AS REAL)), 0) AS pnl
            FROM perps_paper_trades
            WHERE status='CLOSED' AND closed_at>=?
            """,
            (day_start,),
        ).fetchone()
        closed_daily_pnl = _d(closed["pnl"], "closed_daily_pnl")
        # For open trades, include mark-to-entry PnL, entry fees, and all
        # funding already settled. Funding is deducted from cash immediately,
        # but must also count toward the same-day loss circuit breaker.
        open_costs = sum(
            (
                _d(row["entry_fee"], "entry_fee")
                + _d(row["funding_paid"], "funding_paid")
                for row in rows
            ),
            Decimal("0"),
        )
        daily_pnl = closed_daily_pnl + unrealized - open_costs
        daily_loss = max(Decimal("0"), -daily_pnl)
        stale_open_position = False
        for row in rows:
            try:
                age = (now - _parse_time(row["current_mark_at"])).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            if age < -2 or age > float(self.max_market_data_age_seconds):
                stale_open_position = True
                break
        return {
            "cash_balance": cash,
            "equity": equity,
            "peak_equity": peak,
            "free_collateral": cash - funding_reserve,
            "margin_reserved": margin,
            "unrealized_pnl": unrealized,
            "open_exposure": exposure,
            "open_positions": open_positions,
            "daily_loss": daily_loss,
            "stale_open_position": stale_open_position,
            "open_trade_rows": rows,
        }

    def account_metrics(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _now(now)
        with self._connect() as connection:
            values = self._metrics_locked(connection, current)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in values.items()
            if key != "open_trade_rows"
        }

    def open_position(
        self,
        snapshot: Mapping[str, Any],
        *,
        side: str,
        requested_notional: Any,
        now: datetime | None = None,
        stop_loss_distance_pct: Any | None = None,
        reward_risk_multiple: Any = "2",
    ) -> dict[str, Any]:
        """Open a local paper position only after book-fill and risk validation."""
        current = _now(now)
        direction = side.strip().upper() if isinstance(side, str) else ""
        if direction not in {"LONG", "SHORT"}:
            return {"status": "PAPER_ENTRY_BLOCKED", "reason": "invalid_side", "execution": "none"}
        try:
            self._validate_snapshot(snapshot, current)
            requested = _d(requested_notional, "requested_notional")
            mark = _d(snapshot["mark_price"], "mark_price")
            quantity_step = _quantum(int(snapshot.get("quantity_decimals", 0)))
            quantity = (requested / mark).quantize(quantity_step, rounding=ROUND_DOWN)
            if quantity <= 0:
                return {"status": "PAPER_ENTRY_BLOCKED", "reason": "quantity_below_precision", "execution": "none"}
        except (ValueError, TypeError, ArithmeticError):
            return {"status": "PAPER_ENTRY_BLOCKED", "reason": "invalid_or_stale_snapshot", "execution": "none"}

        order_side = "BUY" if direction == "LONG" else "SELL"
        fill = sweep_perps_book(
            snapshot,
            order_side=order_side,
            quantity=quantity,
            max_impact_bps=self.max_entry_impact_bps,
        )
        if fill.get("filled") is not True:
            return {"status": "PAPER_ENTRY_BLOCKED", "reason": fill.get("reason", "fill_unavailable"), "execution": "none"}
        actual_notional = _d(fill["notional"], "fill_notional")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._update_mark_locked(connection, snapshot, current)
                open_same = connection.execute(
                    "SELECT trade_id FROM perps_paper_trades WHERE instrument_id=? AND status='OPEN'",
                    (int(snapshot["instrument_id"]),),
                ).fetchone()
                if open_same:
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "position_already_open_for_instrument", "execution": "none"}
                metrics = self._metrics_locked(connection, current)
                if metrics["stale_open_position"]:
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "existing_position_mark_stale", "execution": "none"}
                if any(row["funding_status"] != "tracking" for row in metrics["open_trade_rows"]):
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "existing_position_funding_gap", "execution": "none"}

                risk = evaluate_perps_entry(
                    snapshot,
                    side=direction,
                    requested_notional=actual_notional,
                    equity=metrics["equity"],
                    free_collateral=metrics["free_collateral"],
                    open_exposure=metrics["open_exposure"],
                    open_positions=metrics["open_positions"],
                    daily_loss=metrics["daily_loss"],
                    peak_equity=metrics["peak_equity"],
                    limits=self.limits,
                    stop_loss_distance_pct=stop_loss_distance_pct,
                )
                if risk.get("allowed") is not True:
                    connection.rollback()
                    return {
                        "status": "PAPER_ENTRY_BLOCKED",
                        "reason": risk.get("reason", "risk_rejected"),
                        "risk_gate": risk,
                        "fill": fill,
                        "execution": "none",
                    }

                entry_fee = actual_notional * self.taker_fee_rate
                margin = _d(risk["initial_margin"], "initial_margin")
                if metrics["cash_balance"] < margin + entry_fee:
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "insufficient_paper_cash_after_fee", "risk_gate": risk, "execution": "none"}
                entry_price = _d(fill["vwap"], "vwap")
                stop_distance = (
                    _d(stop_loss_distance_pct, "stop_loss_distance_pct")
                    if stop_loss_distance_pct is not None
                    else self.limits.stop_loss_distance_pct
                )
                reward_multiple = _d(reward_risk_multiple, "reward_risk_multiple")
                if not Decimal("0.0025") <= stop_distance <= Decimal("0.03"):
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "stop_loss_distance_out_of_bounds", "execution": "none"}
                if not Decimal("1") <= reward_multiple <= Decimal("4"):
                    connection.rollback()
                    return {"status": "PAPER_ENTRY_BLOCKED", "reason": "reward_risk_multiple_out_of_bounds", "execution": "none"}
                target_distance = stop_distance * reward_multiple
                stop_price = entry_price * (Decimal("1") - stop_distance if direction == "LONG" else Decimal("1") + stop_distance)
                take_profit = entry_price * (Decimal("1") + target_distance if direction == "LONG" else Decimal("1") - target_distance)
                next_funding = int(snapshot["next_funding_ms"])
                funding_rate = _d(snapshot.get("funding_rate", "0"), "funding_rate")
                trade_id = uuid.uuid4().hex
                cash_after = metrics["cash_balance"] - margin - entry_fee
                connection.execute(
                    """
                    INSERT INTO perps_paper_trades (
                        trade_id, instrument_id, symbol, side, quantity, entry_price,
                        current_mark, current_mark_at, leverage, initial_margin,
                        entry_fee, funding_paid, funding_rate, next_funding_ms,
                        funding_status, stop_price, take_profit_price, opened_at,
                        status, risk_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?, ?,
                              'tracking', ?, ?, ?, 'OPEN', ?)
                    """,
                    (
                        trade_id, int(snapshot["instrument_id"]), str(snapshot["symbol"]),
                        direction, str(quantity), str(entry_price), str(mark),
                        current.isoformat(), risk["leverage"], str(margin), str(entry_fee),
                        str(funding_rate), next_funding, str(stop_price),
                        str(take_profit), current.isoformat(),
                        json.dumps(risk, sort_keys=True, allow_nan=False),
                    ),
                )
                connection.execute(
                    "UPDATE perps_paper_account SET cash_balance=?, updated_at=? WHERE account_id=1",
                    (str(cash_after), current.isoformat()),
                )
                connection.commit()
                return {
                    "status": "PAPER_POSITION_OPEN",
                    "trade_id": trade_id,
                    "instrument_id": int(snapshot["instrument_id"]),
                    "symbol": str(snapshot["symbol"]),
                    "side": direction,
                    "quantity": str(quantity),
                    "entry_price": str(entry_price),
                    "notional": str(actual_notional),
                    "leverage": risk["leverage"],
                    "initial_margin": str(margin),
                    "entry_fee": str(entry_fee),
                    "stop_price": str(stop_price),
                    "take_profit_price": str(take_profit),
                    "stop_loss_distance_pct": str(stop_distance),
                    "reward_risk_multiple": str(reward_multiple),
                    "risk_gate": risk,
                    "fill": fill,
                    "research_only": True,
                    "live_execution_eligible": False,
                    "execution": "paper_simulation_only",
                }
            except Exception:
                connection.rollback()
                raise

    def _close_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        snapshot: Mapping[str, Any],
        *,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        original_side = str(row["side"])
        order_side = "SELL" if original_side == "LONG" else "BUY"
        quantity = _d(row["quantity"], "quantity")
        fill = sweep_perps_book(
            snapshot, order_side=order_side, quantity=quantity,
            max_impact_bps=self.max_entry_impact_bps * Decimal("2"),
        )
        if fill.get("filled") is not True:
            return {
                "status": "PAPER_EXIT_BLOCKED",
                "trade_id": row["trade_id"],
                "reason": fill.get("reason", "close_fill_unavailable"),
                "execution": "none",
            }
        exit_price = _d(fill["vwap"], "exit_vwap")
        exit_notional = _d(fill["notional"], "exit_notional")
        entry_price = _d(row["entry_price"], "entry_price")
        direction = Decimal("1") if original_side == "LONG" else Decimal("-1")
        gross_pnl = (exit_price - entry_price) * quantity * direction
        exit_fee = exit_notional * self.taker_fee_rate
        funding_paid = _d(row["funding_paid"], "funding_paid")
        entry_fee = _d(row["entry_fee"], "entry_fee")
        net_pnl = gross_pnl - entry_fee - exit_fee - funding_paid
        margin = _d(row["initial_margin"], "initial_margin")
        account = connection.execute(
            "SELECT cash_balance FROM perps_paper_account WHERE account_id=1"
        ).fetchone()
        cash_after = _d(account["cash_balance"], "cash_balance") + margin + gross_pnl - exit_fee
        connection.execute(
            """
            UPDATE perps_paper_trades
            SET status='CLOSED', closed_at=?, exit_price=?, exit_fee=?,
                gross_pnl=?, net_pnl=?, close_reason=?, current_mark=?,
                current_mark_at=?
            WHERE trade_id=? AND status='OPEN'
            """,
            (
                now.isoformat(), str(exit_price), str(exit_fee), str(gross_pnl),
                str(net_pnl), reason[:80], str(exit_price), now.isoformat(),
                row["trade_id"],
            ),
        )
        connection.execute(
            "UPDATE perps_paper_account SET cash_balance=?, updated_at=? WHERE account_id=1",
            (str(cash_after), now.isoformat()),
        )
        return {
            "status": "PAPER_POSITION_CLOSED",
            "trade_id": row["trade_id"],
            "symbol": row["symbol"],
            "side": original_side,
            "quantity": str(quantity),
            "exit_price": str(exit_price),
            "gross_pnl": str(gross_pnl),
            "entry_fee": str(entry_fee),
            "exit_fee": str(exit_fee),
            "funding_paid": str(funding_paid),
            "net_pnl": str(net_pnl),
            "close_reason": reason,
            "fill": fill,
            "research_only": True,
            "live_execution_eligible": False,
            "execution": "paper_simulation_only",
        }

    def close_position(
        self,
        trade_id: str,
        snapshot: Mapping[str, Any],
        *,
        reason: str = "manual_paper_close",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now)
        try:
            self._validate_snapshot(snapshot, current, allow_close_only=True)
        except (ValueError, TypeError, ArithmeticError):
            return {"status": "PAPER_EXIT_BLOCKED", "reason": "invalid_or_stale_snapshot", "execution": "none"}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._update_mark_locked(connection, snapshot, current)
                row = connection.execute(
                    "SELECT * FROM perps_paper_trades WHERE trade_id=? AND status='OPEN'",
                    (trade_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return {"status": "PAPER_EXIT_BLOCKED", "reason": "open_position_not_found", "execution": "none"}
                if int(row["instrument_id"]) != int(snapshot["instrument_id"]):
                    connection.rollback()
                    return {"status": "PAPER_EXIT_BLOCKED", "reason": "instrument_mismatch", "execution": "none"}
                result = self._close_locked(connection, row, snapshot, reason=reason, now=current)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def process_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a snapshot, mark open exposure, book funding, and honor exits."""
        current = _now(now)
        try:
            # An instrument temporarily marked close-only may still be used to
            # reduce/close an existing position. Do not permit entries here.
            if snapshot.get("research_only") is not True or snapshot.get("execution") != "none":
                raise ValueError("snapshot_not_research_only")
            if snapshot.get("settlement") is True:
                raise ValueError("instrument_settled")
            observed = _parse_time(str(snapshot.get("observed_at", "")))
            if (current - observed).total_seconds() < -2:
                raise ValueError("future_snapshot")
            for field in ("ticker_age_seconds", "book_age_seconds"):
                age = _d(snapshot.get(field), field)
                if age < 0 or age > self.max_market_data_age_seconds:
                    raise ValueError("stale_market_data")
            mark = _d(snapshot.get("mark_price"), "mark_price")
            if mark <= 0:
                raise ValueError("invalid_mark_price")
            int(snapshot["instrument_id"])
        except (ValueError, TypeError, ArithmeticError):
            return {"status": "PAPER_SNAPSHOT_REJECTED", "reason": "invalid_or_stale_snapshot", "execution": "none"}

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._update_mark_locked(connection, snapshot, current)
                open_rows = connection.execute(
                    "SELECT * FROM perps_paper_trades WHERE instrument_id=? AND status='OPEN'",
                    (int(snapshot["instrument_id"]),),
                ).fetchall()
                if not open_rows:
                    connection.commit()
                    return {"status": "PAPER_MARK_RECORDED", "open_position": False, "execution": "none"}
                row = open_rows[0]
                if row["funding_status"] == "gap":
                    connection.commit()
                    return {
                        "status": "PAPER_POSITION_FUNDING_GAP",
                        "trade_id": row["trade_id"],
                        "new_entries_blocked": True,
                        "execution": "none",
                    }
                mark = _d(snapshot["mark_price"], "mark_price")
                stop = _d(row["stop_price"], "stop_price")
                target = _d(row["take_profit_price"], "take_profit_price")
                hit_stop = (mark <= stop) if row["side"] == "LONG" else (mark >= stop)
                hit_target = (mark >= target) if row["side"] == "LONG" else (mark <= target)
                if hit_stop or hit_target:
                    reason = "stop_loss" if hit_stop else "take_profit"
                    result = self._close_locked(connection, row, snapshot, reason=reason, now=current)
                    connection.commit()
                    return result
                connection.commit()
                return {
                    "status": "PAPER_MARK_RECORDED",
                    "open_position": True,
                    "trade_id": row["trade_id"],
                    "mark_price": str(mark),
                    "funding_paid": str(_d(row["funding_paid"], "funding_paid")),
                    "funding_status": row["funding_status"],
                    "execution": "none",
                }
            except Exception:
                connection.rollback()
                raise

    def recent_snapshots(
        self,
        instrument_id: int,
        *,
        limit: int = 12000,
    ) -> list[dict[str, Any]]:
        """Return chronological archived snapshots for one instrument."""
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
            raise ValueError("instrument_id must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 100 <= limit <= 25000:
            raise ValueError("limit must be between 100 and 25000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json FROM perps_paper_snapshots
                WHERE instrument_id=?
                ORDER BY observed_at DESC, snapshot_id DESC LIMIT ?
                """,
                (instrument_id, limit),
            ).fetchall()
        snapshots = [json.loads(row["snapshot_json"]) for row in rows]
        snapshots.reverse()
        return snapshots

    def claim_strategy_bar(
        self,
        instrument_id: int,
        bar_close_ms: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Persist an idempotent per-instrument candle claim before strategy action."""
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
            raise ValueError("instrument_id must be a positive integer")
        if isinstance(bar_close_ms, bool) or not isinstance(bar_close_ms, int) or bar_close_ms <= 0:
            raise ValueError("bar_close_ms must be a positive integer")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO perps_strategy_decisions
                    (instrument_id, bar_close_ms, decision_status, reason, metrics_json, created_at)
                VALUES (?, ?, 'evaluating', 'claimed', '{}', ?)
                """,
                (instrument_id, bar_close_ms, _iso(now)),
            )
            return cursor.rowcount == 1

    def record_strategy_decision(
        self,
        instrument_id: int,
        bar_close_ms: int,
        *,
        decision_status: str,
        reason: str,
        metrics: Mapping[str, Any] | None = None,
        side: str | None = None,
        trade_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Finalize the audit row for a previously claimed candle."""
        allowed = {"no_signal", "entry_opened", "entry_blocked", "strategy_error"}
        if decision_status not in allowed:
            raise ValueError("invalid_strategy_decision_status")
        if side not in {None, "LONG", "SHORT"}:
            raise ValueError("invalid_strategy_side")
        safe_metrics = dict(metrics or {})
        serialized = json.dumps(safe_metrics, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE perps_strategy_decisions
                SET decision_status=?, side=?, reason=?, metrics_json=?, trade_id=?
                WHERE instrument_id=? AND bar_close_ms=? AND decision_status='evaluating'
                """,
                (
                    decision_status, side, str(reason)[:120], serialized,
                    trade_id, instrument_id, bar_close_ms,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("strategy_decision_claim_missing_or_already_finalized")

    def strategy_decision_summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT decision_status, COUNT(*) AS count
                FROM perps_strategy_decisions
                GROUP BY decision_status
                """
            ).fetchall()
            latest = connection.execute(
                """
                SELECT instrument_id, bar_close_ms, decision_status, side, reason, trade_id
                FROM perps_strategy_decisions
                ORDER BY created_at DESC, bar_close_ms DESC LIMIT 1
                """
            ).fetchone()
        counts = {str(row["decision_status"]): int(row["count"]) for row in rows}
        return {
            "decisions_total": sum(counts.values()),
            "signals_opened": counts.get("entry_opened", 0),
            "entry_blocks": counts.get("entry_blocked", 0),
            "latest": dict(latest) if latest else None,
        }

    def list_trades(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM perps_paper_trades ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        allowed = (
            "trade_id", "instrument_id", "symbol", "side", "quantity",
            "entry_price", "current_mark", "leverage", "initial_margin",
            "entry_fee", "funding_paid", "funding_status", "stop_price",
            "take_profit_price", "opened_at", "status", "closed_at",
            "exit_price", "exit_fee", "gross_pnl", "net_pnl", "close_reason",
        )
        return [{key: row[key] for key in allowed} for row in rows]

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        metrics = self.account_metrics(now=now)
        with self._connect() as connection:
            closed_count = connection.execute(
                "SELECT COUNT(*) AS count FROM perps_paper_trades WHERE status='CLOSED'"
            ).fetchone()["count"]
            snapshot_count = connection.execute(
                "SELECT COUNT(*) AS count FROM perps_paper_snapshots"
            ).fetchone()["count"]
        return {
            "mode": "perps_paper_only",
            "research_only": True,
            "live_execution_enabled": False,
            "live_execution_eligible": False,
            "execution": "none",
            "metrics": metrics,
            "closed_trades": int(closed_count),
            "market_snapshots": int(snapshot_count),
            "trades": self.list_trades(limit=100),
        }
