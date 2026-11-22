"""Isolated-margin Polymarket Perps paper simulator.

This module is deliberately non-executable: it has no API client, credentials,
signer, wallet, or order-submission path. It consumes already-captured public
market snapshots and records hypothetical isolated-margin paper positions.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite() or (positive and parsed <= ZERO):
        raise ValueError(f"{field} must be finite" + (" and positive" if positive else ""))
    return parsed


def _timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _interval_ms(value: Any) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(value or "").lower())
    if not match:
        raise ValueError("funding_interval must be a positive duration such as 1h")
    amount = int(match.group(1))
    multiplier = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[match.group(2)]
    if amount <= 0:
        raise ValueError("funding_interval must be positive")
    return amount * multiplier


def _quantize_down(value: Decimal, places: int) -> Decimal:
    if isinstance(places, bool) or not isinstance(places, int) or not 0 <= places <= 12:
        raise ValueError("quantity_decimals must be between 0 and 12")
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_DOWN)


class PerpsPaperEngine:
    """SQLite-backed paper ledger for isolated-margin perpetual positions."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        initial_equity: Any = "1000",
        max_position_notional: Any = "25",
        max_total_notional: Any = "75",
        max_open_positions: int = 3,
        max_leverage: Any = "2",
        risk_per_trade_pct: Any = "0.0025",
        stop_loss_pct: Any = "0.0075",
        take_profit_pct: Any = "0.015",
        taker_fee_rate: Any = "0.0005",
        liquidation_fee_rate: Any = "0.01",
        max_spread_bps: Any = "20",
        lookback_observations: int = 10,
        momentum_threshold_bps: Any = "20",
        minimum_history_span_seconds: int = 60,
        daily_drawdown_pct: Any = "0.03",
        maximum_drawdown_pct: Any = "0.10",
        max_data_age_seconds: int = 15,
        max_entry_impact_bps: Any = "50",
        funding_schedule_grace_seconds: int = 60,
        max_post_funding_mark_delay_seconds: int = 90,
    ) -> None:
        self.initial_equity = _decimal(initial_equity, "initial_equity", positive=True)
        self.max_position_notional = _decimal(max_position_notional, "max_position_notional", positive=True)
        self.max_total_notional = _decimal(max_total_notional, "max_total_notional", positive=True)
        self.max_leverage = _decimal(max_leverage, "max_leverage", positive=True)
        self.risk_per_trade_pct = _decimal(risk_per_trade_pct, "risk_per_trade_pct", positive=True)
        self.stop_loss_pct = _decimal(stop_loss_pct, "stop_loss_pct", positive=True)
        self.take_profit_pct = _decimal(take_profit_pct, "take_profit_pct", positive=True)
        self.taker_fee_rate = _decimal(taker_fee_rate, "taker_fee_rate")
        self.liquidation_fee_rate = _decimal(liquidation_fee_rate, "liquidation_fee_rate")
        self.max_spread_bps = _decimal(max_spread_bps, "max_spread_bps", positive=True)
        self.max_entry_impact_bps = _decimal(max_entry_impact_bps, "max_entry_impact_bps", positive=True)
        self.momentum_threshold_bps = _decimal(momentum_threshold_bps, "momentum_threshold_bps", positive=True)
        self.daily_drawdown_pct = _decimal(daily_drawdown_pct, "daily_drawdown_pct", positive=True)
        self.maximum_drawdown_pct = _decimal(maximum_drawdown_pct, "maximum_drawdown_pct", positive=True)
        for name, value in (
            ("taker_fee_rate", self.taker_fee_rate),
            ("liquidation_fee_rate", self.liquidation_fee_rate),
        ):
            if value < ZERO or value >= ONE:
                raise ValueError(f"{name} must be in [0, 1)")
        if self.risk_per_trade_pct >= ONE or self.stop_loss_pct >= ONE or self.take_profit_pct >= ONE:
            raise ValueError("risk and stop/target percentages must be below 1")
        if self.daily_drawdown_pct >= ONE or self.maximum_drawdown_pct >= ONE:
            raise ValueError("drawdown limits must be below 1")
        if isinstance(max_open_positions, bool) or not isinstance(max_open_positions, int) or not 1 <= max_open_positions <= 100:
            raise ValueError("max_open_positions must be between 1 and 100")
        if isinstance(lookback_observations, bool) or not isinstance(lookback_observations, int) or not 3 <= lookback_observations <= 500:
            raise ValueError("lookback_observations must be between 3 and 500")
        if isinstance(minimum_history_span_seconds, bool) or not isinstance(minimum_history_span_seconds, int) or minimum_history_span_seconds < 0:
            raise ValueError("minimum_history_span_seconds must be a nonnegative integer")
        if isinstance(max_data_age_seconds, bool) or not isinstance(max_data_age_seconds, int) or not 1 <= max_data_age_seconds <= 60:
            raise ValueError("max_data_age_seconds must be between 1 and 60")
        self.max_open_positions = max_open_positions
        self.lookback_observations = lookback_observations
        self.minimum_history_span_seconds = minimum_history_span_seconds
        self.max_data_age_seconds = max_data_age_seconds
        if (
            isinstance(funding_schedule_grace_seconds, bool)
            or not isinstance(funding_schedule_grace_seconds, int)
            or not 5 <= funding_schedule_grace_seconds <= 300
        ):
            raise ValueError("funding_schedule_grace_seconds must be between 5 and 300")
        if (
            isinstance(max_post_funding_mark_delay_seconds, bool)
            or not isinstance(max_post_funding_mark_delay_seconds, int)
            or not 15 <= max_post_funding_mark_delay_seconds <= 120
        ):
            raise ValueError("max_post_funding_mark_delay_seconds must be between 15 and 120")
        self.funding_schedule_grace_seconds = funding_schedule_grace_seconds
        self.max_post_funding_mark_delay_seconds = max_post_funding_mark_delay_seconds

        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS perps_paper_account (
                account_id INTEGER PRIMARY KEY CHECK(account_id = 1),
                cash TEXT NOT NULL,
                peak_equity TEXT NOT NULL,
                utc_day TEXT NOT NULL,
                day_start_equity TEXT NOT NULL,
                halted INTEGER NOT NULL DEFAULT 0,
                halt_reason TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS perps_paper_positions (
                position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
                quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                margin_balance TEXT NOT NULL,
                entry_fee TEXT NOT NULL,
                taker_fee_rate TEXT NOT NULL DEFAULT '0',
                liquidation_fee_rate TEXT NOT NULL DEFAULT '0',
                funding_paid TEXT NOT NULL DEFAULT '0',
                stop_price TEXT NOT NULL,
                take_profit_price TEXT NOT NULL,
                last_mark TEXT NOT NULL,
                last_funding_rate TEXT NOT NULL,
                next_funding_ms INTEGER NOT NULL,
                funding_interval_ms INTEGER NOT NULL,
                maintenance_leverage TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED','LIQUIDATED')),
                close_reason TEXT,
                pending_exit_reason TEXT,
                pending_exit_liquidated INTEGER NOT NULL DEFAULT 0,
                exit_price TEXT,
                gross_pnl TEXT NOT NULL DEFAULT '0',
                net_pnl TEXT NOT NULL DEFAULT '0',
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_perps_paper_open_positions
                ON perps_paper_positions(status, instrument_id);
            CREATE TABLE IF NOT EXISTS perps_paper_processed (
                snapshot_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS perps_paper_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_key TEXT NOT NULL,
                instrument_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_perps_paper_events_time
                ON perps_paper_events(event_at, event_id);
            """
        )
        position_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(perps_paper_positions)").fetchall()
        }
        if "taker_fee_rate" not in position_columns:
            self.db.execute(
                "ALTER TABLE perps_paper_positions ADD COLUMN taker_fee_rate TEXT NOT NULL DEFAULT '0'"
            )
            self.db.execute(
                "UPDATE perps_paper_positions SET taker_fee_rate=? WHERE taker_fee_rate='0'",
                (str(self.taker_fee_rate),),
            )
        if "liquidation_fee_rate" not in position_columns:
            self.db.execute(
                "ALTER TABLE perps_paper_positions ADD COLUMN liquidation_fee_rate TEXT NOT NULL DEFAULT '0'"
            )
            self.db.execute(
                "UPDATE perps_paper_positions SET liquidation_fee_rate=? WHERE liquidation_fee_rate='0'",
                (str(self.liquidation_fee_rate),),
            )
        if "pending_exit_reason" not in position_columns:
            self.db.execute(
                "ALTER TABLE perps_paper_positions ADD COLUMN pending_exit_reason TEXT"
            )
        if "pending_exit_liquidated" not in position_columns:
            self.db.execute(
                "ALTER TABLE perps_paper_positions ADD COLUMN pending_exit_liquidated INTEGER NOT NULL DEFAULT 0"
            )
        self.db.execute(
            """
            INSERT OR IGNORE INTO perps_paper_account
                (account_id,cash,peak_equity,utc_day,day_start_equity,halted,halt_reason,updated_at)
            VALUES (1,?,?,?,?,0,NULL,?)
            """,
            (
                str(self.initial_equity),
                str(self.initial_equity),
                datetime.now(timezone.utc).date().isoformat(),
                str(self.initial_equity),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.db.commit()

    def _account(self) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM perps_paper_account WHERE account_id=1").fetchone()
        if row is None:
            raise RuntimeError("paper account row is missing")
        return row

    def _open_positions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM perps_paper_positions WHERE status='OPEN' ORDER BY position_id"
        ).fetchall()

    def open_instrument_ids(self) -> list[int]:
        """Return every instrument that currently has an open isolated position."""
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT instrument_id FROM perps_paper_positions "
                "WHERE status='OPEN' ORDER BY instrument_id"
            ).fetchall()
            return [int(row["instrument_id"]) for row in rows]

    @staticmethod
    def _current_fee_rate(snapshot: dict[str, Any], now: datetime) -> Decimal | None:
        fees = snapshot.get("fees")
        if not isinstance(fees, dict) or fees.get("source") != "public_perps_fee_schedule":
            return None
        try:
            rate = _decimal(fees.get("taker_fee_rate"), "fees.taker_fee_rate")
            retrieved = _timestamp(fees.get("retrieved_at"), "fees.retrieved_at")
        except ValueError:
            return None
        age = (now.astimezone(timezone.utc) - retrieved).total_seconds()
        if not ZERO <= rate < ONE or not -2 <= age <= 900:
            return None
        return rate

    @staticmethod
    def _unrealized(position: sqlite3.Row, mark: Decimal) -> Decimal:
        sign = ONE if position["side"] == "LONG" else Decimal("-1")
        return (mark - Decimal(position["entry_price"])) * Decimal(position["quantity"]) * sign

    def _equity(self) -> Decimal:
        account = self._account()
        equity = Decimal(account["cash"])
        for position in self._open_positions():
            margin = Decimal(position["margin_balance"])
            mark = Decimal(position["last_mark"])
            equity += margin + self._unrealized(position, mark)
        return equity

    def _refresh_account(self, now: datetime) -> None:
        account = self._account()
        equity = self._equity()
        peak = max(Decimal(account["peak_equity"]), equity)
        day = now.date().isoformat()
        day_start = Decimal(account["day_start_equity"])
        if account["utc_day"] != day:
            day_start = equity
        halted = bool(account["halted"])
        reason = account["halt_reason"]
        if day_start > ZERO and (day_start - equity) / day_start >= self.daily_drawdown_pct:
            halted, reason = True, "daily_drawdown_limit"
        if peak > ZERO and (peak - equity) / peak >= self.maximum_drawdown_pct:
            halted, reason = True, "maximum_drawdown_limit"
        self.db.execute(
            """
            UPDATE perps_paper_account
            SET peak_equity=?, utc_day=?, day_start_equity=?, halted=?, halt_reason=?, updated_at=?
            WHERE account_id=1
            """,
            (str(peak), day, str(day_start), int(halted), reason, now.isoformat()),
        )

    def _event(
        self,
        snapshot_key: str,
        instrument_id: int,
        event_type: str,
        now: datetime,
        details: dict[str, Any],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO perps_paper_events
                (snapshot_key,instrument_id,event_type,event_at,details_json)
            VALUES (?,?,?,?,?)
            """,
            (
                snapshot_key, instrument_id, event_type, now.isoformat(),
                json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False),
            ),
        )

    @staticmethod
    def _sweep(levels: Any, quantity: Decimal, side: str) -> tuple[Decimal, Decimal] | None:
        if not isinstance(levels, list) or quantity <= ZERO or side not in {"BUY", "SELL"}:
            return None
        remaining = quantity
        filled = ZERO
        quote = ZERO
        for level in levels:
            if not isinstance(level, dict):
                return None
            price = _decimal(level.get("price"), "book price", positive=True)
            available = _decimal(level.get("quantity"), "book quantity", positive=True)
            take = min(remaining, available)
            filled += take
            quote += take * price
            remaining -= take
            if remaining <= ZERO:
                break
        if remaining > Decimal("0.000000000001") or filled <= ZERO:
            return None
        return quote / filled, quote

    @staticmethod
    def _tier_leverage(instrument: dict[str, Any], notional: Decimal) -> Decimal:
        tiers = instrument.get("risk_tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ValueError("instrument risk tiers are missing")
        eligible: list[tuple[Decimal, Decimal]] = []
        for tier in tiers:
            if not isinstance(tier, dict):
                raise ValueError("invalid risk tier")
            lower = _decimal(tier.get("lower_bound"), "risk tier lower bound")
            maximum = _decimal(tier.get("max_leverage"), "risk tier max leverage", positive=True)
            if lower < ZERO:
                raise ValueError("risk tier lower bound cannot be negative")
            if lower <= notional:
                eligible.append((lower, maximum))
        if not eligible:
            return _decimal(tiers[0].get("max_leverage"), "risk tier max leverage", positive=True)
        eligible.sort(key=lambda item: item[0])
        return eligible[-1][1]

    def _funding(
        self,
        position: sqlite3.Row,
        instrument: dict[str, Any],
        ticker: dict[str, Any],
        now: datetime,
        snapshot_key: str,
        history_store: Any,
    ) -> bool:
        old_next = int(position["next_funding_ms"])
        current_next = ticker.get("next_funding_ms")
        current_rate = _decimal(ticker.get("funding_rate"), "funding_rate")
        if isinstance(current_next, bool) or not isinstance(current_next, int) or current_next <= 0:
            self._halt("funding_schedule_missing")
            self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_RECONCILIATION_REQUIRED", now, {
                "reason": "current_next_funding_missing",
            })
            return False
        interval = int(position["funding_interval_ms"])
        if current_next < old_next:
            self._halt("funding_schedule_regressed")
            self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_RECONCILIATION_REQUIRED", now, {
                "previous_next_funding_ms": old_next,
                "current_next_funding_ms": current_next,
            })
            return False
        if current_next == old_next and int(now.timestamp() * 1000) > (
            old_next + self.funding_schedule_grace_seconds * 1000
        ):
            self._halt("funding_schedule_not_advanced")
            self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_RECONCILIATION_REQUIRED", now, {
                "previous_next_funding_ms": old_next,
                "current_next_funding_ms": current_next,
                "reason": "funding time passed but API schedule did not advance",
            })
            return False
        if current_next > old_next:
            delta = current_next - old_next
            if delta != interval:
                self._halt("funding_history_gap")
                self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_RECONCILIATION_REQUIRED", now, {
                    "previous_next_funding_ms": old_next,
                    "current_next_funding_ms": current_next,
                    "expected_interval_ms": interval,
                    "observed_delta_ms": delta,
                    "reason": "one or more funding observations may be missing; no estimate applied",
                })
                return False
            now_ms = int(now.timestamp() * 1000)
            ticker_ms = ticker.get("timestamp_ms")
            if (
                isinstance(ticker_ms, bool)
                or not isinstance(ticker_ms, int)
                or not 0 <= ticker_ms - old_next <= self.max_post_funding_mark_delay_seconds * 1000
            ):
                self._halt("funding_mark_observation_missing")
                self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_RECONCILIATION_REQUIRED", now, {
                    "scheduled_funding_timestamp_ms": old_next,
                    "ticker_timestamp_ms": ticker_ms,
                    "reason": f"no fresh Mark Price observation within {self.max_post_funding_mark_delay_seconds} seconds after settlement",
                })
                return False
            settled = history_store.funding_rate_at(
                int(position["instrument_id"]), old_next, tolerance_ms=1000
            ) if callable(getattr(history_store, "funding_rate_at", None)) else None
            if settled is None:
                conflict_reader = getattr(history_store, "funding_conflict_at", None)
                conflict = (
                    conflict_reader(
                        int(position["instrument_id"]), old_next, tolerance_ms=1000
                    )
                    if callable(conflict_reader) else None
                )
                has_conflict = isinstance(conflict, dict)
                halt_reason = (
                    "published_funding_settlement_conflict"
                    if has_conflict else "published_funding_settlement_missing"
                )
                self._halt(halt_reason)
                details = {
                    "scheduled_funding_timestamp_ms": old_next,
                    "reason": (
                        "public funding settlement has conflicting published rates; "
                        "manual reconciliation required; no ticker-rate estimate applied"
                        if has_conflict else
                        "public settled funding rate missing or ambiguous; "
                        "no ticker-rate estimate applied"
                    ),
                }
                if has_conflict:
                    details["observed_funding_rates"] = conflict.get("observed_rates", [])
                self._event(
                    snapshot_key,
                    int(position["instrument_id"]),
                    "FUNDING_RECONCILIATION_REQUIRED",
                    now,
                    details,
                )
                return False
            mark = _decimal(ticker.get("mark_price"), "mark_price", positive=True)
            quantity = Decimal(position["quantity"])
            sign = ONE if position["side"] == "LONG" else Decimal("-1")
            previous_rate = _decimal(settled.get("funding_rate"), "settled_funding_rate")
            payment = quantity * mark * previous_rate * sign
            margin = Decimal(position["margin_balance"]) - payment
            total_paid = Decimal(position["funding_paid"]) + payment
            self.db.execute(
                """
                UPDATE perps_paper_positions
                SET margin_balance=?, funding_paid=?, next_funding_ms=?, last_funding_rate=?
                WHERE position_id=?
                """,
                (str(margin), str(total_paid), current_next, str(current_rate), position["position_id"]),
            )
            self._event(snapshot_key, int(position["instrument_id"]), "FUNDING_SETTLED", now, {
                "payment": str(payment),
                "funding_rate": str(previous_rate),
                "funding_timestamp_ms": int(settled["timestamp_ms"]),
                "mark_price": str(mark),
                "mark_price_method": "first_fresh_post_settlement_observation",
                "next_funding_ms": current_next,
            })
        else:
            self.db.execute(
                "UPDATE perps_paper_positions SET last_funding_rate=? WHERE position_id=?",
                (str(current_rate), position["position_id"]),
            )
        return True

    def _halt(self, reason: str) -> None:
        self.db.execute(
            "UPDATE perps_paper_account SET halted=1, halt_reason=? WHERE account_id=1",
            (str(reason)[:120],),
        )

    def _close(
        self,
        position: sqlite3.Row,
        snapshot: dict[str, Any],
        *,
        reason: str,
        now: datetime,
        snapshot_key: str,
        liquidated: bool = False,
    ) -> bool:
        book = snapshot["book"]
        side = "SELL" if position["side"] == "LONG" else "BUY"
        levels = book["bids"] if side == "SELL" else book["asks"]
        quantity = Decimal(position["quantity"])
        fill = self._sweep(levels, quantity, side)
        if fill is None:
            if liquidated:
                self._halt("liquidation_close_unfilled")
            self._event(snapshot_key, int(position["instrument_id"]), "CLOSE_BLOCKED", now, {
                "reason": reason,
                "side": side,
                "quantity": str(quantity),
                "liquidation": liquidated,
                "detail": "insufficient visible order-book depth could not absorb the full position",
            })
            self.db.execute(
                """
                UPDATE perps_paper_positions
                SET pending_exit_reason=?, pending_exit_liquidated=?
                WHERE position_id=? AND status='OPEN'
                """,
                (reason, int(liquidated), position["position_id"]),
            )
            return False
        exit_price, exit_notional = fill
        entry_price = Decimal(position["entry_price"])
        sign = ONE if position["side"] == "LONG" else Decimal("-1")
        gross = (exit_price - entry_price) * quantity * sign
        stored_taker_fee = Decimal(position["taker_fee_rate"])
        current_taker_fee = self._current_fee_rate(snapshot, now)
        exit_fee_rate = max(stored_taker_fee, current_taker_fee or ZERO)
        exit_fee = exit_notional * exit_fee_rate
        liquidation_fee_rate = Decimal(position["liquidation_fee_rate"])
        liquidation_fee = exit_notional * liquidation_fee_rate if liquidated else ZERO
        entry_fee = Decimal(position["entry_fee"])
        funding_paid = Decimal(position["funding_paid"])
        net = gross - entry_fee - exit_fee - liquidation_fee - funding_paid
        margin_balance = Decimal(position["margin_balance"])
        funding_paid = Decimal(position["funding_paid"])
        initial_margin = margin_balance + funding_paid
        released_before_isolated_cap = margin_balance + gross - exit_fee - liquidation_fee
        isolated_margin_shortfall = max(ZERO, -released_before_isolated_cap)
        if isolated_margin_shortfall > ZERO:
            # Isolated losses cannot consume the account's free collateral or
            # other positions. Cap the loss at this position's initial allocation.
            released = ZERO
            net = -initial_margin - entry_fee
            self._halt("isolated_margin_exhausted")
        else:
            released = released_before_isolated_cap
        account = self._account()
        new_cash = Decimal(account["cash"]) + released
        self.db.execute(
            "UPDATE perps_paper_account SET cash=? WHERE account_id=1",
            (str(new_cash),),
        )
        status = "LIQUIDATED" if liquidated else "CLOSED"
        self.db.execute(
            """
            UPDATE perps_paper_positions
            SET status=?, close_reason=?, pending_exit_reason=NULL,
                pending_exit_liquidated=0, exit_price=?, gross_pnl=?, net_pnl=?, closed_at=?, last_mark=?
            WHERE position_id=?
            """,
            (
                status, reason, str(exit_price), str(gross), str(net),
                now.isoformat(), str(exit_price), position["position_id"],
            ),
        )
        self._event(snapshot_key, int(position["instrument_id"]), status, now, {
            "position_id": int(position["position_id"]),
            "side": position["side"],
            "quantity": str(quantity),
            "entry_price": str(entry_price),
            "exit_price": str(exit_price),
            "gross_pnl": str(gross),
            "entry_fee": str(entry_fee),
            "exit_fee": str(exit_fee),
            "exit_taker_fee_rate": str(exit_fee_rate),
            "funding_paid": str(funding_paid),
            "liquidation_fee_rate": str(liquidation_fee_rate),
            "liquidation_fee": str(liquidation_fee),
            "isolated_margin_shortfall": str(isolated_margin_shortfall),
            "free_collateral_used": False,
            "net_pnl": str(net),
            "reason": reason,
        })
        return True

    def _signal(self, instrument_id: int, history_store: Any) -> tuple[str | None, dict[str, Any]]:
        try:
            rows = history_store.history(instrument_id, limit=self.lookback_observations)
        except Exception as exc:
            return None, {"reason": f"history_unavailable:{type(exc).__name__}"}
        if not isinstance(rows, list) or len(rows) < self.lookback_observations:
            return None, {"reason": "insufficient_history", "observations": len(rows) if isinstance(rows, list) else 0}
        try:
            first = rows[0]
            last = rows[-1]
            first_mark = _decimal(first["ticker"]["mark_price"], "first mark", positive=True)
            last_mark = _decimal(last["ticker"]["mark_price"], "last mark", positive=True)
            first_ms = int(first["book_timestamp_ms"])
            last_ms = int(last["book_timestamp_ms"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, {"reason": "invalid_history"}
        span = max(0, (last_ms - first_ms) // 1000)
        if span < self.minimum_history_span_seconds or last_mark == first_mark:
            return None, {"reason": "history_span_or_movement_insufficient", "span_seconds": span}
        momentum = (last_mark - first_mark) / first_mark * BPS
        if momentum >= self.momentum_threshold_bps:
            side = "LONG"
        elif momentum <= -self.momentum_threshold_bps:
            side = "SHORT"
        else:
            side = None
        return side, {
            "reason": "momentum_threshold_met" if side else "momentum_below_threshold",
            "momentum_bps": str(momentum),
            "observations": len(rows),
            "span_seconds": span,
        }

    def _try_open(
        self,
        snapshot: dict[str, Any],
        history_store: Any,
        now: datetime,
        snapshot_key: str,
    ) -> None:
        instrument = snapshot["instrument"]
        ticker = snapshot["ticker"]
        book = snapshot["book"]
        instrument_id = int(instrument["instrument_id"])
        if instrument.get("close_only") is True:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "instrument_close_only"})
            return
        try:
            min_notional = _decimal(instrument.get("min_notional"), "min_notional", positive=True)
            max_market_notional = _decimal(instrument.get("max_market_notional"), "max_market_notional", positive=True)
            max_instrument_leverage = _decimal(instrument.get("max_leverage"), "max_leverage", positive=True)
            interval = _interval_ms(instrument.get("funding_interval"))
            liquidation_fee_rate = _decimal(instrument.get("liquidation_fee"), "liquidation_fee")
            if not ZERO <= liquidation_fee_rate < ONE:
                raise ValueError("instrument liquidation fee must be in [0, 1)")
            next_funding = ticker.get("next_funding_ms")
            if isinstance(next_funding, bool) or not isinstance(next_funding, int) or next_funding <= 0:
                raise ValueError("next_funding_ms is unavailable")
            decimals = int(instrument.get("quantity_decimals"))
            mark = _decimal(ticker.get("mark_price"), "mark_price", positive=True)
            bid = _decimal(book.get("best_bid"), "best_bid", positive=True)
            ask = _decimal(book.get("best_ask"), "best_ask", positive=True)
        except (ValueError, TypeError, OverflowError) as exc:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": f"invalid_instrument_or_ticker:{str(exc)[:120]}"})
            return
        if bid >= ask:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "crossed_or_locked_book"})
            return
        mid = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / mid * BPS
        if spread_bps > self.max_spread_bps:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "spread_too_wide", "spread_bps": str(spread_bps)})
            return

        taker_fee_rate = self._current_fee_rate(snapshot, now)
        if taker_fee_rate is None:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {
                "reason": "current_public_taker_fee_schedule_missing_or_stale"
            })
            return
        side, signal = self._signal(instrument_id, history_store)
        if side is None:
            self._event(snapshot_key, instrument_id, "NO_SIGNAL", now, signal)
            return

        account = self._account()
        if bool(account["halted"]):
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": account["halt_reason"] or "account_halted"})
            return
        positions = self._open_positions()
        if any(int(row["instrument_id"]) == instrument_id for row in positions):
            return
        if len(positions) >= self.max_open_positions:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "max_open_positions"})
            return
        equity = self._equity()
        if equity <= ZERO:
            self._halt("non_positive_equity")
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "non_positive_equity"})
            return
        exposure = sum(
            Decimal(row["quantity"]) * Decimal(row["last_mark"])
            for row in positions
        )
        desired_notional = min(
            self.max_position_notional,
            equity * self.risk_per_trade_pct / self.stop_loss_pct,
            self.max_total_notional - exposure,
            max_market_notional,
        )
        if desired_notional <= ZERO:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "total_exposure_limit"})
            return
        execution_reference = ask if side == "LONG" else bid
        quantity = _quantize_down(desired_notional / execution_reference, decimals)
        if quantity <= ZERO:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "quantity_below_precision"})
            return
        levels = book["asks"] if side == "LONG" else book["bids"]
        fill = self._sweep(levels, quantity, "BUY" if side == "LONG" else "SELL")
        if fill is None:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "insufficient_visible_depth"})
            return
        entry_price, entry_notional = fill
        best_execution_price = _decimal(
            levels[0].get("price") if isinstance(levels[0], dict) else None,
            "best executable entry price", positive=True,
        )
        entry_impact_bps = abs(entry_price - best_execution_price) / best_execution_price * BPS
        if entry_impact_bps > self.max_entry_impact_bps:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {
                "reason": "entry_impact_too_high",
                "entry_impact_bps": str(entry_impact_bps),
                "maximum_entry_impact_bps": str(self.max_entry_impact_bps),
            })
            return
        if (
            entry_notional < min_notional
            or entry_notional > max_market_notional
            or entry_notional > self.max_position_notional
            or entry_notional > desired_notional
        ):
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {
                "reason": "instrument_or_position_notional_constraint",
                "notional": str(entry_notional),
                "min_notional": str(min_notional),
                "max_market_notional": str(max_market_notional),
                "max_position_notional": str(self.max_position_notional),
                "risk_budget_notional": str(desired_notional),
            })
            return
        tier_max = self._tier_leverage(instrument, entry_notional)
        leverage = min(self.max_leverage, max_instrument_leverage, tier_max)
        if leverage < ONE:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "invalid_leverage_tier"})
            return
        margin = entry_notional / leverage
        entry_fee = entry_notional * taker_fee_rate
        if Decimal(account["cash"]) < margin + entry_fee:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "insufficient_free_cash"})
            return
        new_exposure = exposure + entry_notional
        if new_exposure > self.max_total_notional:
            self._event(snapshot_key, instrument_id, "ENTRY_BLOCKED", now, {"reason": "total_exposure_limit"})
            return
        sign = ONE if side == "LONG" else Decimal("-1")
        stop = entry_price * (ONE - self.stop_loss_pct * sign)
        target = entry_price * (ONE + self.take_profit_pct * sign)
        new_cash = Decimal(account["cash"]) - margin - entry_fee
        cursor = self.db.execute(
            """
            INSERT INTO perps_paper_positions (
                instrument_id,symbol,side,quantity,entry_price,margin_balance,entry_fee,
                taker_fee_rate,liquidation_fee_rate,funding_paid,
                stop_price,take_profit_price,last_mark,last_funding_rate,
                next_funding_ms,funding_interval_ms,maintenance_leverage,opened_at,status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')
            """,
            (
                instrument_id, str(instrument.get("symbol") or "")[:80], side,
                str(quantity), str(entry_price), str(margin), str(entry_fee),
                str(taker_fee_rate), str(liquidation_fee_rate), "0",
                str(stop), str(target), str(mark), str(_decimal(ticker.get("funding_rate"), "funding_rate")),
                next_funding, interval, str(max_instrument_leverage), now.isoformat(),
            ),
        )
        self.db.execute(
            "UPDATE perps_paper_account SET cash=? WHERE account_id=1",
            (str(new_cash),),
        )
        self._event(snapshot_key, instrument_id, "PAPER_POSITION_OPENED", now, {
            "position_id": int(cursor.lastrowid),
            "side": side,
            "quantity": str(quantity),
            "entry_price": str(entry_price),
            "notional": str(entry_notional),
            "leverage": str(leverage),
            "isolated_margin": str(margin),
            "entry_fee": str(entry_fee),
            "entry_impact_bps": str(entry_impact_bps),
            "taker_fee_rate": str(taker_fee_rate),
            "liquidation_fee_rate": str(liquidation_fee_rate),
            "maintenance_margin_rate": str(Decimal("0.5") / max_instrument_leverage),
            "stop_price": str(stop),
            "take_profit_price": str(target),
            "signal": signal,
        })

    def process_snapshot(
        self,
        snapshot: dict[str, Any],
        history_store: Any,
        *,
        now: datetime | None = None,
        allow_new_entries: bool = True,
    ) -> dict[str, Any]:
        """Process one public snapshot idempotently and return a sanitized state."""
        if not isinstance(allow_new_entries, bool):
            raise ValueError("allow_new_entries must be a boolean")
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        instrument = snapshot.get("instrument")
        ticker = snapshot.get("ticker")
        book = snapshot.get("book")
        if not all(isinstance(item, dict) for item in (instrument, ticker, book)):
            raise ValueError("snapshot requires instrument, ticker and book objects")
        instrument_id = instrument.get("instrument_id")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        if ticker.get("instrument_id") != instrument_id or book.get("instrument_id") != instrument_id:
            raise ValueError("snapshot instrument identifiers do not match")
        book_ms = ticker_ms = 0
        try:
            book_ms = int(book["timestamp_ms"])
            ticker_ms = int(ticker["timestamp_ms"])
            received = _timestamp(snapshot.get("received_at"), "received_at")
            mark = _decimal(ticker.get("mark_price"), "mark_price", positive=True)
            bid = _decimal(book.get("best_bid"), "best_bid", positive=True)
            ask = _decimal(book.get("best_ask"), "best_ask", positive=True)
            if bid >= ask:
                raise ValueError("book is crossed or locked")
            if abs(book_ms - ticker_ms) > self.max_data_age_seconds * 1000:
                raise ValueError("ticker and book timestamps are too far apart")
            captured_now = now
            if captured_now is not None:
                if captured_now.tzinfo is None or captured_now.utcoffset() is None:
                    raise ValueError("now must be timezone-aware")
                captured_now = captured_now.astimezone(timezone.utc)
            else:
                captured_now = datetime.now(timezone.utc)
            capture_ms = int(captured_now.timestamp() * 1000)
            received_ms = int(received.timestamp() * 1000)
            book_age_ms = capture_ms - book_ms
            ticker_age_ms = capture_ms - ticker_ms
            received_age_ms = capture_ms - received_ms
            maximum_age_ms = self.max_data_age_seconds * 1000
            if book_age_ms < -2_000 or ticker_age_ms < -2_000 or received_age_ms < -2_000:
                raise ValueError("snapshot exchange or receipt timestamp is future-dated")
            if (
                book_age_ms > maximum_age_ms
                or ticker_age_ms > maximum_age_ms
                or received_age_ms > maximum_age_ms
            ):
                raise ValueError("snapshot exchange or receipt timestamp is stale relative to current time")
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"invalid or stale Perps snapshot: {str(exc)[:160]}") from None
        snapshot_key = f"{instrument_id}:{book_ms}:{int(book.get('sequence', 0))}"

        with self._lock, self.db:
            duplicate = self.db.execute(
                "SELECT 1 FROM perps_paper_processed WHERE snapshot_key=?",
                (snapshot_key,),
            ).fetchone()
            if duplicate:
                return self.summary()
            self.db.execute(
                "INSERT INTO perps_paper_processed(snapshot_key,processed_at) VALUES (?,?)",
                (snapshot_key, captured_now.isoformat()),
            )
            existing = self.db.execute(
                "SELECT * FROM perps_paper_positions WHERE instrument_id=? AND status='OPEN' ORDER BY position_id DESC LIMIT 1",
                (instrument_id,),
            ).fetchone()
            had_open_position = existing is not None
            if existing:
                if not self._funding(existing, instrument, ticker, captured_now, snapshot_key, history_store):
                    pass
                current = self.db.execute(
                    "SELECT * FROM perps_paper_positions WHERE position_id=?",
                    (existing["position_id"],),
                ).fetchone()
                self.db.execute(
                    "UPDATE perps_paper_positions SET last_mark=? WHERE position_id=?",
                    (str(mark), current["position_id"]),
                )
                current = self.db.execute(
                    "SELECT * FROM perps_paper_positions WHERE position_id=?",
                    (existing["position_id"],),
                ).fetchone()
                quantity = Decimal(current["quantity"])
                notional = quantity * mark
                maintenance_rate = Decimal("0.5") / Decimal(current["maintenance_leverage"])
                maintenance = notional * maintenance_rate
                position_equity = Decimal(current["margin_balance"]) + self._unrealized(current, mark)
                margin_ratio = position_equity / maintenance if maintenance > ZERO else Decimal("0")

                # Exit triggers are latched durably. A stop or liquidation that
                # cannot consume the current book must not disappear on recovery.
                reason = current["pending_exit_reason"]
                liquidation = bool(current["pending_exit_liquidated"])
                if reason is None:
                    if margin_ratio < ONE:
                        reason, liquidation = "maintenance_margin_breached", True
                    elif instrument.get("close_only") is True:
                        reason = "instrument_close_only_exit_required"
                    elif current["side"] == "LONG" and mark <= Decimal(current["stop_price"]):
                        reason = "stop_loss"
                    elif current["side"] == "SHORT" and mark >= Decimal(current["stop_price"]):
                        reason = "stop_loss"
                    elif current["side"] == "LONG" and mark >= Decimal(current["take_profit_price"]):
                        reason = "take_profit"
                    elif current["side"] == "SHORT" and mark <= Decimal(current["take_profit_price"]):
                        reason = "take_profit"
                    if reason:
                        self.db.execute(
                            """
                            UPDATE perps_paper_positions
                            SET pending_exit_reason=?, pending_exit_liquidated=?
                            WHERE position_id=? AND status='OPEN'
                            """,
                            (reason, int(liquidation), current["position_id"]),
                        )
                if reason:
                    fresh_position = self.db.execute(
                        "SELECT * FROM perps_paper_positions WHERE position_id=?",
                        (current["position_id"],),
                    ).fetchone()
                    self._close(
                        fresh_position, snapshot, reason=reason, now=captured_now,
                        snapshot_key=snapshot_key, liquidated=liquidation,
                    )
            self._refresh_account(captured_now)
            open_for_market = self.db.execute(
                "SELECT 1 FROM perps_paper_positions WHERE instrument_id=? AND status='OPEN' LIMIT 1",
                (instrument_id,),
            ).fetchone()
            if not had_open_position and not open_for_market:
                if instrument.get("close_only") is True:
                    self._event(
                        snapshot_key, instrument_id, "ENTRY_BLOCKED", captured_now,
                        {"reason": "instrument_close_only"},
                    )
                elif not allow_new_entries:
                    self._event(
                        snapshot_key, instrument_id, "ENTRY_BLOCKED", captured_now,
                        {"reason": "open_position_market_data_incomplete"},
                    )
                else:
                    self._try_open(snapshot, history_store, captured_now, snapshot_key)
            self._refresh_account(captured_now)
            return self.summary()

    def funding_requirements(self, snapshots: list[dict[str, Any]]) -> list[dict[str, int]]:
        """Return exact funding settlement windows required by open positions."""
        required: list[dict[str, int]] = []
        with self._lock:
            for snapshot in snapshots:
                instrument = snapshot.get("instrument", {})
                ticker = snapshot.get("ticker", {})
                instrument_id = instrument.get("instrument_id")
                if isinstance(instrument_id, bool) or not isinstance(instrument_id, int):
                    continue
                position = self.db.execute(
                    "SELECT next_funding_ms FROM perps_paper_positions WHERE instrument_id=? AND status='OPEN' ORDER BY position_id DESC LIMIT 1",
                    (instrument_id,),
                ).fetchone()
                if position is None:
                    continue
                old_next = int(position["next_funding_ms"])
                new_next = ticker.get("next_funding_ms")
                if isinstance(new_next, int) and not isinstance(new_next, bool) and new_next > old_next:
                    required.append({
                        "instrument_id": instrument_id,
                        "start_timestamp_ms": old_next,
                        "end_timestamp_ms": new_next,
                    })
        return required

    def summary(self) -> dict[str, Any]:
        with self._lock:
            account = self._account()
            positions = self._open_positions()
            equity = self._equity()
            cash = Decimal(account["cash"])
            margin = sum((Decimal(row["margin_balance"]) for row in positions), ZERO)
            unrealized = sum(
                (self._unrealized(row, Decimal(row["last_mark"])) for row in positions),
                ZERO,
            )
            current_notional = sum(
                (
                    Decimal(row["quantity"]) * Decimal(row["last_mark"])
                    for row in positions
                ),
                ZERO,
            )
            peak_equity = Decimal(account["peak_equity"])
            daily_start_equity = Decimal(account["day_start_equity"])
            peak_drawdown_pct = (
                max(ZERO, (peak_equity - equity) / peak_equity * Decimal("100"))
                if peak_equity > ZERO else ZERO
            )
            daily_pnl_pct = (
                (equity - daily_start_equity) / daily_start_equity * Decimal("100")
                if daily_start_equity > ZERO else ZERO
            )
            effective_leverage = (
                current_notional / equity if equity > ZERO else ZERO
            )
            closed_rows = self.db.execute(
                """
                SELECT net_pnl,gross_pnl,entry_fee,funding_paid
                FROM perps_paper_positions
                WHERE status IN ('CLOSED','LIQUIDATED')
                ORDER BY position_id
                """
            ).fetchall()
            closed_count = len(closed_rows)
            closed_net_pnl = sum((Decimal(row["net_pnl"]) for row in closed_rows), ZERO)
            closed_gross_pnl = sum((Decimal(row["gross_pnl"]) for row in closed_rows), ZERO)
            winning_net = sum(
                (Decimal(row["net_pnl"]) for row in closed_rows if Decimal(row["net_pnl"]) > ZERO),
                ZERO,
            )
            losing_net = -sum(
                (Decimal(row["net_pnl"]) for row in closed_rows if Decimal(row["net_pnl"]) < ZERO),
                ZERO,
            )
            win_count = sum(1 for row in closed_rows if Decimal(row["net_pnl"]) > ZERO)
            loss_count = sum(1 for row in closed_rows if Decimal(row["net_pnl"]) < ZERO)
            breakeven_count = closed_count - win_count - loss_count
            entry_fees_paid = sum((Decimal(row["entry_fee"]) for row in closed_rows), ZERO)
            funding_paid_total = sum((Decimal(row["funding_paid"]) for row in closed_rows), ZERO)
            close_events = self.db.execute(
                """
                SELECT details_json FROM perps_paper_events
                WHERE event_type IN ('CLOSED','LIQUIDATED')
                ORDER BY event_id
                """
            ).fetchall()
            exit_fees_paid = ZERO
            liquidation_fees_paid = ZERO
            for event in close_events:
                details = json.loads(event["details_json"])
                exit_fees_paid += _decimal(details.get("exit_fee", "0"), "exit_fee")
                liquidation_fees_paid += _decimal(
                    details.get("liquidation_fee", "0"), "liquidation_fee"
                )
            total_fees_paid = entry_fees_paid + exit_fees_paid + liquidation_fees_paid
            performance = {
                "closed_trades": closed_count,
                "winning_trades": win_count,
                "losing_trades": loss_count,
                "breakeven_trades": breakeven_count,
                "win_rate_pct": (
                    str((Decimal(win_count) / Decimal(closed_count) * Decimal("100")).quantize(Decimal("0.01")))
                    if closed_count else None
                ),
                "gross_closed_pnl": str(closed_gross_pnl),
                "net_closed_pnl_after_fees_and_funding": str(closed_net_pnl),
                "net_expectancy_per_closed_trade": (
                    str((closed_net_pnl / Decimal(closed_count)).quantize(Decimal("0.000001")))
                    if closed_count else None
                ),
                "profit_factor": (
                    str((winning_net / losing_net).quantize(Decimal("0.0001")))
                    if losing_net > ZERO else None
                ),
                "profit_factor_status": (
                    "calculated" if losing_net > ZERO
                    else "undefined_without_closed_losses" if closed_count
                    else "no_closed_trades"
                ),
                "entry_fees_paid": str(entry_fees_paid),
                "exit_fees_paid": str(exit_fees_paid),
                "liquidation_fees_paid": str(liquidation_fees_paid),
                "total_fees_paid": str(total_fees_paid),
                "funding_paid": str(funding_paid_total),
                "descriptive_only": True,
                "out_of_sample_validated": False,
                "live_execution_eligible": False,
            }
            events = self.db.execute(
                "SELECT event_type, COUNT(*) AS count FROM perps_paper_events GROUP BY event_type"
            ).fetchall()
            pending_exit_count = self.db.execute(
                "SELECT COUNT(*) FROM perps_paper_positions "
                "WHERE status='OPEN' AND pending_exit_reason IS NOT NULL"
            ).fetchone()[0]
            return {
                "mode": "POLYMARKET_PERPS_ISOLATED_MARGIN_PAPER",
                "enabled": True,
                "worker_state": "healthy" if not bool(account["halted"]) else "halted",
                "cash": str(cash),
                "equity": str(equity),
                "peak_equity": str(peak_equity),
                "isolated_margin_reserved": str(margin),
                "unrealized_pnl": str(unrealized),
                "open_notional": str(current_notional),
                "effective_leverage": str(effective_leverage),
                "peak_drawdown_pct": str(peak_drawdown_pct.quantize(Decimal("0.0001"))),
                "daily_pnl_pct": str(daily_pnl_pct.quantize(Decimal("0.0001"))),
                "open_positions": len(positions),
                "pending_exit_positions": int(pending_exit_count),
                "closed_positions": closed_count,
                "closed_net_pnl": str(closed_net_pnl),
                "performance": performance,
                "halted": bool(account["halted"]),
                "halt_reason": account["halt_reason"],
                "utc_day": account["utc_day"],
                "daily_start_equity": account["day_start_equity"],
                "max_leverage": str(self.max_leverage),
                "max_position_notional": str(self.max_position_notional),
                "max_total_notional": str(self.max_total_notional),
                "max_entry_impact_bps": str(self.max_entry_impact_bps),
                "margin_mode": "isolated",
                "cross_margin_supported": False,
                "maintenance_margin_rule": "0.5 / instrument_max_leverage",
                "liquidation_is_venue_exact": False,
                "liquidation_model_limitations": "no insurance-fund backstop or auto-deleveraging simulation",
                "fee_rate_source": "public_perps_fee_schedule_highest_published_category_rate",
                "liquidation_fee_source": "instrument_liquidation_fee_rate",
                "event_counts": {row["event_type"]: int(row["count"]) for row in events},
                "research_only": True,
                "live_execution_enabled": False,
                "order_submission_allowed": False,
                "execution": "none",
            }

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "PerpsPaperEngine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
