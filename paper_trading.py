"""Persistent paper portfolio simulator. It never sends live orders."""
from __future__ import annotations

import copy
import json
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from risk_manager import evaluate_paper_entry
from market_quality import evaluate_order_book_quality
from paper_fills import simulate_orderbook_fill
from paper_fees import conservative_fee_fraction_of_notional, estimate_taker_fee, resolve_taker_fee_model


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def rotate_jsonl_file(path: Path, max_bytes: int, backups: int) -> None:
    """Rotate an append-only JSONL file before the next record would grow it further."""
    if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
        return
    if backups <= 0:
        path.unlink(missing_ok=True)
        return
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


@dataclass(frozen=True)
class PaperConfig:
    starting_cash: float = 100.0
    max_position_usd: float = 2.0
    max_open_exposure_usd: float = 10.0
    max_open_positions: int = 5
    signal_delta: float = 0.03
    take_profit_pct: float = 0.10
    stop_loss_pct: float = 0.10
    max_hold_hours: float = 24.0
    simulated_slippage: float = 0.005
    max_entry_price_impact_bps: float = 200.0
    fee_rate: float = 0.0
    daily_loss_limit_usd: float = 5.0
    max_portfolio_drawdown_pct: float = 20.0
    require_orderbook: bool = False
    max_orderbook_age_seconds: float = 180.0
    max_orderbook_spread_bps: float = 1000.0
    min_orderbook_side_depth_usd: float = 5.0

    @classmethod
    def from_env(cls, *, require_orderbook_default: bool = False) -> "PaperConfig":
        """Read validated simulation settings; production may require a fresh CLOB book."""
        defaults = cls()
        values: dict[str, Any] = {}
        float_fields = {
            "starting_cash": "PAPER_STARTING_CASH",
            "max_position_usd": "PAPER_MAX_POSITION_USD",
            "max_open_exposure_usd": "PAPER_MAX_OPEN_EXPOSURE_USD",
            "signal_delta": "PAPER_SIGNAL_DELTA",
            "take_profit_pct": "PAPER_TAKE_PROFIT_PCT",
            "stop_loss_pct": "PAPER_STOP_LOSS_PCT",
            "max_hold_hours": "PAPER_MAX_HOLD_HOURS",
            "simulated_slippage": "PAPER_SLIPPAGE_RATE",
            "max_entry_price_impact_bps": "PAPER_MAX_ENTRY_PRICE_IMPACT_BPS",
            "fee_rate": "PAPER_FEE_RATE",
            "daily_loss_limit_usd": "PAPER_DAILY_LOSS_LIMIT_USD",
            "max_portfolio_drawdown_pct": "PAPER_MAX_PORTFOLIO_DRAWDOWN_PCT",
            "max_orderbook_age_seconds": "PAPER_MAX_ORDERBOOK_AGE_SECONDS",
            "max_orderbook_spread_bps": "PAPER_MAX_ORDERBOOK_SPREAD_BPS",
            "min_orderbook_side_depth_usd": "PAPER_MIN_ORDERBOOK_SIDE_DEPTH_USD",
        }
        for field, env_name in float_fields.items():
            if env_name in os.environ:
                parsed = finite_float(os.environ[env_name])
                if parsed is None:
                    raise ValueError(f"{env_name} must be a finite number")
                values[field] = parsed
        if "PAPER_MAX_OPEN_POSITIONS" in os.environ:
            try:
                values["max_open_positions"] = int(os.environ["PAPER_MAX_OPEN_POSITIONS"])
            except ValueError as exc:
                raise ValueError("PAPER_MAX_OPEN_POSITIONS must be an integer") from exc
        require_orderbook_raw = os.getenv("PAPER_REQUIRE_ORDERBOOK")
        if require_orderbook_raw is None:
            values["require_orderbook"] = require_orderbook_default
        else:
            normalized = require_orderbook_raw.strip().lower()
            if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                raise ValueError("PAPER_REQUIRE_ORDERBOOK must be a boolean")
            values["require_orderbook"] = normalized in {"1", "true", "yes", "on"}
        config = cls(**{**asdict(defaults), **values})
        if config.starting_cash <= 0 or config.max_position_usd <= 0:
            raise ValueError("Starting cash and position size must be positive")
        if config.max_open_exposure_usd <= 0 or config.max_open_positions < 1:
            raise ValueError("Exposure limit and maximum positions must be positive")
        if config.signal_delta <= 0 or config.max_hold_hours <= 0:
            raise ValueError("Signal delta and max holding time must be positive")
        if not 0 < config.take_profit_pct <= 1 or not 0 < config.stop_loss_pct < 1:
            raise ValueError("Take-profit must be in (0, 1] and stop-loss in (0, 1)")
        if not 0 <= config.simulated_slippage < 0.5 or not 0 <= config.fee_rate < 0.5:
            raise ValueError("Slippage and fee rate must be in [0, 0.5)")
        if not 0 < config.max_entry_price_impact_bps <= 5000:
            raise ValueError("Maximum entry book impact must be in (0, 5000] basis points")
        if config.daily_loss_limit_usd <= 0:
            raise ValueError("Daily loss limit must be positive")
        if not 0.0 < config.max_portfolio_drawdown_pct <= 100.0:
            raise ValueError("Maximum portfolio drawdown must be in (0, 100]")
        if (
            config.max_orderbook_age_seconds <= 0
            or config.max_orderbook_spread_bps <= 0
            or config.min_orderbook_side_depth_usd < 0
        ):
            raise ValueError("Order-book age/spread limits must be positive and minimum depth nonnegative")
        return config


class PaperTrader:
    """Manage a small, persistent simulated portfolio using observed outcome prices.

    This is a simple experimental price-change rule, not a predictive model. Every
    entry and exit is simulated, and the class has no order-placement capability.
    """

    def __init__(self, state_path: str | Path, config: PaperConfig | None = None) -> None:
        self.state_path = Path(state_path)
        self.events_path = self.state_path.with_name("paper_events.jsonl")
        self.config = config or PaperConfig.from_env()
        self._lock = threading.RLock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_or_initialize()

    def _new_state(self) -> dict[str, Any]:
        now = utc_now()
        return {
            "schema_version": 1,
            "mode": "PAPER_ONLY",
            "created_at": iso_time(now),
            "updated_at": iso_time(now),
            "starting_cash": round(self.config.starting_cash, 8),
            "cash": round(self.config.starting_cash, 8),
            "equity_peak": round(self.config.starting_cash, 8),
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct": 0.0,
            "circuit_breaker_tripped": False,
            "circuit_breaker_reason": None,
            "circuit_breaker_at": None,
            "circuit_breaker_drawdown_pct": None,
            "realized_pnl": 0.0,
            "total_fees": 0.0,
            "closed_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "daily_date_utc": now.date().isoformat(),
            "daily_realized_pnl": 0.0,
            "daily_unrealized_baseline": 0.0,
            "positions": {},
            "last_prices": {},
            "recent_events": [],
        }

    def _load_or_initialize(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._new_state()
        def reject_nonstandard_constant(value: str):
            raise ValueError(f"Non-standard JSON numeric constant: {value}")

        try:
            loaded = json.loads(
                self.state_path.read_text(encoding="utf-8"),
                parse_constant=reject_nonstandard_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # Do not silently reset state after a corrupted or non-standard file.
            raise ValueError(f"Could not read paper state file {self.state_path}: {exc}") from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
            raise ValueError("Paper state file has an unsupported or malformed schema")
        breaker_fields = (
            "circuit_breaker_tripped",
            "circuit_breaker_reason",
            "circuit_breaker_at",
            "circuit_breaker_drawdown_pct",
        )
        breaker_fields_present = [field in loaded for field in breaker_fields]
        if any(breaker_fields_present) and not all(breaker_fields_present):
            raise ValueError("Paper state has incomplete circuit-breaker fields; refusing to guess its state")
        has_circuit_breaker_state = all(breaker_fields_present)
        required = {"cash", "starting_cash", "positions", "last_prices", "realized_pnl"}
        if not required.issubset(loaded):
            raise ValueError("Paper state file is missing required fields; refusing to reset it")
        if not isinstance(loaded["positions"], dict) or not isinstance(loaded["last_prices"], dict):
            raise ValueError("Paper state positions/last_prices have invalid types")
        loaded.setdefault("mode", "PAPER_ONLY")
        loaded.setdefault("total_fees", 0.0)
        loaded.setdefault("closed_trades", 0)
        loaded.setdefault("winning_trades", 0)
        loaded.setdefault("losing_trades", 0)
        loaded.setdefault("daily_date_utc", utc_now().date().isoformat())
        loaded.setdefault("daily_realized_pnl", 0.0)
        loaded.setdefault("daily_unrealized_baseline", 0.0)
        loaded.setdefault("equity_peak", loaded.get("starting_cash", self.config.starting_cash))
        loaded.setdefault("max_drawdown_usd", 0.0)
        loaded.setdefault("max_drawdown_pct", 0.0)
        loaded.setdefault("recent_events", [])
        if not has_circuit_breaker_state:
            # Migrate legacy states conservatively. If the historical maximum
            # drawdown already crossed the configured limit, do not silently
            # resume entries after upgrading the simulator.
            historical_drawdown = loaded.get("max_drawdown_pct", 0.0)
            if (
                isinstance(historical_drawdown, bool)
                or not isinstance(historical_drawdown, (int, float))
                or not math.isfinite(float(historical_drawdown))
            ):
                raise ValueError("Legacy paper state has an invalid maximum drawdown")
            migrated_tripped = (
                float(historical_drawdown) + 1e-9
                >= self.config.max_portfolio_drawdown_pct
            )
            migrated_at = loaded.get("updated_at") or loaded.get("created_at") or iso_time()
            try:
                parse_time(str(migrated_at))
            except (TypeError, ValueError, OverflowError):
                migrated_at = iso_time()
            loaded.update({
                "circuit_breaker_tripped": migrated_tripped,
                "circuit_breaker_reason": (
                    "max_portfolio_drawdown_reached" if migrated_tripped else None
                ),
                "circuit_breaker_at": str(migrated_at) if migrated_tripped else None,
                "circuit_breaker_drawdown_pct": (
                    round(float(historical_drawdown), 8) if migrated_tripped else None
                ),
            })

        if loaded["mode"] != "PAPER_ONLY":
            raise ValueError("Persisted portfolio mode is not PAPER_ONLY; refusing to load it")

        def checked_number(field: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
            raw = loaded.get(field)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"Paper state field {field} must be numeric")
            parsed = float(raw)
            if not math.isfinite(parsed):
                raise ValueError(f"Paper state field {field} must be finite")
            if minimum is not None and parsed < minimum:
                raise ValueError(f"Paper state field {field} is below its allowed minimum")
            if maximum is not None and parsed > maximum:
                raise ValueError(f"Paper state field {field} is above its allowed maximum")
            return parsed

        checked_number("cash", minimum=0.0)
        checked_number("starting_cash", minimum=1e-12)
        checked_number("realized_pnl")
        checked_number("total_fees", minimum=0.0)
        checked_number("daily_realized_pnl")
        checked_number("daily_unrealized_baseline")
        checked_number("equity_peak", minimum=1e-12)
        checked_number("max_drawdown_usd", minimum=0.0)
        checked_number("max_drawdown_pct", minimum=0.0, maximum=100.0)
        if not isinstance(loaded["circuit_breaker_tripped"], bool):
            raise ValueError("Paper state circuit_breaker_tripped must be a boolean")
        if loaded["circuit_breaker_tripped"]:
            if not isinstance(loaded["circuit_breaker_reason"], str) or not loaded["circuit_breaker_reason"].strip():
                raise ValueError("Tripped paper circuit breaker must include a reason")
            try:
                parse_time(str(loaded["circuit_breaker_at"] or ""))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Tripped paper circuit breaker has an invalid timestamp") from exc
            checked_number("circuit_breaker_drawdown_pct", minimum=0.0, maximum=100.0)
        elif any(loaded.get(field) is not None for field in (
            "circuit_breaker_reason", "circuit_breaker_at", "circuit_breaker_drawdown_pct"
        )):
            raise ValueError("Untripped paper circuit breaker has inconsistent metadata")
        if not isinstance(loaded["recent_events"], list):
            raise ValueError("Paper state recent_events must be a list")

        for counter in ("closed_trades", "winning_trades", "losing_trades"):
            value = loaded[counter]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Paper state counter {counter} must be a nonnegative integer")
        if loaded["winning_trades"] + loaded["losing_trades"] > loaded["closed_trades"]:
            raise ValueError("Paper state win/loss counters exceed the number of closed trades")

        for token_id, price in loaded["last_prices"].items():
            if not isinstance(token_id, str) or not token_id.strip():
                raise ValueError("Paper state contains an invalid last-price token key")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise ValueError("Paper state contains a non-numeric last price")
            if not math.isfinite(float(price)) or not 0.0 < float(price) < 1.0:
                raise ValueError("Paper state contains a last price outside the open interval (0, 1)")

        required_position_numbers = ("shares", "gross_cost", "entry_fee", "entry_price", "mark_price")
        for token_id, position in loaded["positions"].items():
            if not isinstance(token_id, str) or not token_id.strip() or not isinstance(position, dict):
                raise ValueError("Paper state contains a malformed open position")
            for field in required_position_numbers:
                raw = position.get(field)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
                    raise ValueError(f"Paper position {token_id} has an invalid {field}")
            if float(position["shares"]) <= 0.0 or float(position["gross_cost"]) < 0.0 or float(position["entry_fee"]) < 0.0:
                raise ValueError(f"Paper position {token_id} has a negative or zero quantity/cost")
            if not 0.0 < float(position["entry_price"]) < 1.0 or not 0.0 < float(position["mark_price"]) < 1.0:
                raise ValueError(f"Paper position {token_id} has a price outside the open interval (0, 1)")
            if "unrealized_pnl" in position and (
                isinstance(position["unrealized_pnl"], bool)
                or not isinstance(position["unrealized_pnl"], (int, float))
                or not math.isfinite(float(position["unrealized_pnl"]))
            ):
                raise ValueError(f"Paper position {token_id} has invalid unrealized P&L")
            # New fee-accounting fields are optional for backward compatibility
            # with schema-v1 positions created before share-denominated fee support.
            if "gross_shares" in position:
                gross_shares = position["gross_shares"]
                if (
                    isinstance(gross_shares, bool)
                    or not isinstance(gross_shares, (int, float))
                    or not math.isfinite(float(gross_shares))
                    or float(gross_shares) <= 0.0
                    or float(gross_shares) + 1e-10 < float(position["shares"])
                ):
                    raise ValueError(f"Paper position {token_id} has invalid gross_shares")
            if "entry_fee_shares" in position:
                fee_shares = position["entry_fee_shares"]
                if (
                    isinstance(fee_shares, bool)
                    or not isinstance(fee_shares, (int, float))
                    or not math.isfinite(float(fee_shares))
                    or float(fee_shares) < 0.0
                ):
                    raise ValueError(f"Paper position {token_id} has invalid entry_fee_shares")
            collection = position.get("entry_fee_collection")
            if collection is not None and collection not in {"shares", "cash", "none"}:
                raise ValueError(f"Paper position {token_id} has invalid entry_fee_collection")
            if collection == "shares":
                if "gross_shares" not in position or "entry_fee_shares" not in position:
                    raise ValueError(f"Paper position {token_id} is missing share-fee accounting fields")
                expected_shares = float(position["gross_shares"]) - float(position["entry_fee_shares"])
                if abs(expected_shares - float(position["shares"])) > 1e-8:
                    raise ValueError(f"Paper position {token_id} has inconsistent share-fee accounting")
            try:
                parse_time(str(position.get("opened_at") or ""))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Paper position {token_id} has an invalid opened_at timestamp") from exc
        return loaded

    def _save(self) -> None:
        self._state["updated_at"] = iso_time()
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        encoded = json.dumps(self._state, ensure_ascii=False, allow_nan=False, indent=2)
        temp_path.write_text(encoded + "\n", encoding="utf-8")
        temp_path.replace(self.state_path)

    def _write_event(self, event: dict[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        max_mb = max(1, int(os.getenv("PAPER_EVENTS_MAX_MB", "5")))
        backups = max(0, int(os.getenv("PAPER_EVENTS_BACKUPS", "3")))
        rotate_jsonl_file(self.events_path, max_mb * 1024 * 1024, backups)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

    @staticmethod
    def _token_key(row: dict[str, Any]) -> str:
        token = str(row.get("token_id") or "").strip()
        if token:
            return token
        market = str(row.get("market_id") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        return f"{market}:{outcome}" if market and outcome else ""

    def _update_drawdown_metrics(self) -> float:
        """Persist the all-time peak equity and maximum observed mark-to-market drawdown."""
        equity = float(self.snapshot()["metrics"]["equity"])
        peak = max(
            float(self._state.get("equity_peak", self._state["starting_cash"])),
            equity,
        )
        drawdown_usd = max(0.0, peak - equity)
        drawdown_pct = 100.0 * drawdown_usd / peak if peak > 0.0 else 0.0
        self._state["equity_peak"] = round(peak, 8)
        self._state["max_drawdown_usd"] = round(
            max(float(self._state.get("max_drawdown_usd", 0.0)), drawdown_usd), 8
        )
        self._state["max_drawdown_pct"] = round(
            max(float(self._state.get("max_drawdown_pct", 0.0)), drawdown_pct), 8
        )
        if (
            not self._state.get("circuit_breaker_tripped", False)
            and drawdown_pct + 1e-9 >= self.config.max_portfolio_drawdown_pct
        ):
            self._state.update({
                "circuit_breaker_tripped": True,
                "circuit_breaker_reason": "max_portfolio_drawdown_reached",
                "circuit_breaker_at": iso_time(),
                "circuit_breaker_drawdown_pct": round(drawdown_pct, 8),
            })
        return drawdown_pct

    def _daily_rollover(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._state.get("daily_date_utc") != today:
            self._state["daily_date_utc"] = today
            self._state["daily_realized_pnl"] = 0.0
            self._state["daily_unrealized_baseline"] = sum(
                float(position.get("unrealized_pnl", 0.0))
                for position in self._state["positions"].values()
            )

    def process_snapshot(self, rows: list[dict[str, Any]], now: datetime | None = None, *, persist_state: bool = True) -> dict[str, list[dict[str, Any]]]:
        """Update marks, simulate exits, and consider entries from one observed snapshot."""
        current_time = now or utc_now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        current_time = current_time.astimezone(timezone.utc)
        result: dict[str, list[dict[str, Any]]] = {"candidates": [], "opened": [], "closed": []}
        events: list[dict[str, Any]] = []
        with self._lock:
            breaker_was_tripped = bool(self._state.get("circuit_breaker_tripped"))
            self._daily_rollover(current_time)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = self._token_key(row)
                price = finite_float(row.get("observed_price"))
                if not key or price is None or not 0.0 < price < 1.0:
                    continue
                old_price = finite_float(self._state["last_prices"].get(key))
                had_position = key in self._state["positions"]

                if had_position:
                    position = self._state["positions"][key]
                    mark_price = price
                    mark_source = "gamma_outcome_price_fallback"
                    exit_fill = None
                    book_quality = evaluate_order_book_quality(
                        row.get("orderbook"),
                        now=current_time,
                        max_age_seconds=self.config.max_orderbook_age_seconds,
                        max_spread_bps=self.config.max_orderbook_spread_bps,
                        min_side_depth_usd=self.config.min_orderbook_side_depth_usd,
                        require_orderbook=False,
                    )
                    if book_quality.get("reason") == "approved_market_quality":
                        exit_fill = simulate_orderbook_fill(
                            row.get("orderbook"),
                            side="sell",
                            target_shares=float(position.get("shares") or 0.0),
                        )
                        if exit_fill.get("eligible"):
                            mark_price = float(exit_fill["average_price"])
                            mark_source = "clob_top5_depth_vwap"
                        else:
                            # Do not pretend the full position can be liquidated at top bid
                            # when visible depth cannot absorb all of its shares.
                            mark_price = finite_float(position.get("mark_price")) or price
                            mark_source = "clob_insufficient_visible_depth_last_mark"
                            position["last_exit_fill_attempt"] = exit_fill
                    position["mark_price"] = mark_price
                    position["mark_source"] = mark_source
                    position["mark_updated_at"] = iso_time(current_time)
                    position["unrealized_pnl"] = round(self._position_unrealized(position, mark_price), 8)
                    reason = self._exit_reason(position, mark_price, current_time)
                    if reason and (exit_fill is None or exit_fill.get("eligible")):
                        closed = self._close_position(
                            key, mark_price, reason, current_time,
                            price_source=mark_source, fill_details=exit_fill,
                        )
                        result["closed"].append(closed)
                        events.append(closed)
                elif old_price is not None:
                    delta = price - old_price
                    if delta >= self.config.signal_delta:
                        candidate = {
                            "event_type": "candidate_signal",
                            "timestamp": iso_time(current_time),
                            "token_id": key,
                            "market_id": str(row.get("market_id") or ""),
                            "question": str(row.get("question") or "Unknown market")[:500],
                            "outcome": str(row.get("outcome") or "Unknown outcome")[:100],
                            "previous_observed_price": round(old_price, 8),
                            "observed_price": round(price, 8),
                            "price_delta": round(delta, 8),
                            "rule": "positive_observed_price_change",
                            "execution": "simulated_only",
                        }
                        result["candidates"].append(candidate)
                        opened, skip_reason = self._try_open(key, row, price, current_time, delta)
                        if opened:
                            result["opened"].append(opened)
                            events.append(opened)
                            candidate["paper_action"] = "opened"
                        else:
                            candidate["paper_action"] = "skipped"
                            candidate["skip_reason"] = skip_reason
                        events.append(candidate)
                self._state["last_prices"][key] = round(price, 8)

            # Production polling persists each complete snapshot. Historical backtests
            # may opt out of repeated disk writes while keeping the identical in-memory
            # state transitions; they use a fresh disposable portfolio for each segment.
            self._state["recent_events"] = (self._state.get("recent_events", []) + events)[-100:]
            self._update_drawdown_metrics()
            if not breaker_was_tripped and self._state.get("circuit_breaker_tripped"):
                breaker_event = {
                    "event_type": "portfolio_drawdown_circuit_breaker_tripped",
                    "timestamp": self._state.get("circuit_breaker_at") or iso_time(current_time),
                    "reason": self._state.get("circuit_breaker_reason"),
                    "drawdown_pct": self._state.get("circuit_breaker_drawdown_pct"),
                    "execution": "none",
                }
                events.append(breaker_event)
                self._state["recent_events"] = (
                    self._state.get("recent_events", []) + [breaker_event]
                )[-100:]
            if persist_state:
                self._save()
                for event in events:
                    self._write_event(event)
        return result

    def _try_open(self, key: str, row: dict[str, Any], observed_price: float, now: datetime, delta: float) -> tuple[dict[str, Any] | None, str]:
        if self._state.get("circuit_breaker_tripped"):
            return None, "portfolio_drawdown_circuit_breaker_latched"
        market_quality = evaluate_order_book_quality(
            row.get("orderbook"),
            now=now,
            max_age_seconds=self.config.max_orderbook_age_seconds,
            max_spread_bps=self.config.max_orderbook_spread_bps,
            min_side_depth_usd=self.config.min_orderbook_side_depth_usd,
            require_orderbook=self.config.require_orderbook,
        )
        if not market_quality["eligible"]:
            return None, str(market_quality["reason"])
        fee_model = resolve_taker_fee_model(row, fallback_rate=self.config.fee_rate)
        if fee_model.get("status") == "unknown":
            return None, str(fee_model.get("reason") or fee_model.get("mode") or "unknown_market_fee_schedule")
        orderbook = row.get("orderbook") if isinstance(row.get("orderbook"), dict) else {}
        lowest_entry_price = finite_float(orderbook.get("best_ask")) if market_quality.get("reason") == "approved_market_quality" else observed_price
        fee_reserve_rate = conservative_fee_fraction_of_notional(
            fee_model, lowest_price=lowest_entry_price
        )
        if fee_reserve_rate is None:
            return None, "market_fee_reserve_unavailable"
        positions: dict[str, dict[str, Any]] = self._state["positions"]
        exposure = sum(float(position.get("gross_cost", 0.0)) for position in positions.values())
        entry_reference_price = observed_price
        entry_price_source = "gamma_outcome_price_fallback"
        entry_limit_price = None
        initial_mark_price = observed_price
        initial_mark_source = "gamma_outcome_price_fallback"
        available_cash = float(self._state["cash"])
        spend = min(
            self.config.max_position_usd,
            max(0.0, self.config.max_open_exposure_usd - exposure),
            available_cash / (1.0 + fee_reserve_rate),
        )
        open_unrealized_total = sum(
            float(position.get("unrealized_pnl", 0.0)) for position in positions.values()
        )
        open_unrealized_pnl = (
            open_unrealized_total - float(self._state.get("daily_unrealized_baseline", 0.0))
        )
        market_id = str(row.get("market_id") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        opposing_market_outcome_open = bool(
            market_id
            and outcome
            and any(
                str(position.get("market_id") or "").strip() == market_id
                and str(position.get("outcome") or "").strip()
                and str(position.get("outcome") or "").strip() != outcome
                for position in positions.values()
            )
        )
        breaker_was_tripped = bool(self._state.get("circuit_breaker_tripped"))
        current_drawdown_pct = self._update_drawdown_metrics()
        if self._state.get("circuit_breaker_tripped"):
            return None, (
                "portfolio_drawdown_circuit_breaker_latched"
                if breaker_was_tripped else "max_portfolio_drawdown_reached"
            )
        decision = evaluate_paper_entry(
            price=observed_price,
            requested_notional=spend,
            cash=available_cash,
            open_exposure=exposure,
            open_positions=len(positions),
            max_position_usd=self.config.max_position_usd,
            max_open_exposure_usd=self.config.max_open_exposure_usd,
            max_open_positions=self.config.max_open_positions,
            daily_realized_pnl=float(self._state["daily_realized_pnl"]),
            open_unrealized_pnl=open_unrealized_pnl,
            daily_loss_limit_usd=self.config.daily_loss_limit_usd,
            fee_rate=fee_reserve_rate,
            opposing_market_outcome_open=opposing_market_outcome_open,
            current_drawdown_pct=current_drawdown_pct,
            max_portfolio_drawdown_pct=self.config.max_portfolio_drawdown_pct,
        )
        if not decision.approved:
            return None, decision.reason
        spend = decision.approved_notional
        entry_fill_details = None
        initial_mark_fill_details = None
        if market_quality.get("reason") == "approved_market_quality":
            best_ask = finite_float(orderbook.get("best_ask"))
            if best_ask is None or not 0.0 < best_ask < 1.0:
                return None, "invalid_best_ask_for_entry_limit"
            entry_limit_price = min(
                0.999999,
                best_ask * (1.0 + self.config.max_entry_price_impact_bps / 10000.0),
            )
            entry_fill_details = simulate_orderbook_fill(
                row.get("orderbook"),
                side="buy",
                target_notional_usd=spend,
                limit_price=entry_limit_price,
            )
            if not entry_fill_details.get("eligible"):
                return None, str(entry_fill_details.get("reason") or "entry_book_depth_unavailable")
            entry_reference_price = float(entry_fill_details["average_price"])
            entry_price_source = "clob_top5_depth_vwap"
        entry_price = entry_reference_price * (1.0 + self.config.simulated_slippage)
        if entry_price >= 1.0:
            return None, "slippage_makes_entry_price_invalid"
        gross_shares = spend / entry_price
        entry_fee_details = estimate_taker_fee(
            fee_model,
            shares=gross_shares,
            price=entry_price,
            notional=spend,
        )
        if not entry_fee_details.get("known"):
            return None, "entry_fee_estimate_unavailable"
        entry_fee = float(entry_fee_details["fee"])
        if fee_model.get("mode") == "polymarket_taker_curve":
            # Polymarket deducts taker fees in shares on buys. Convert the
            # USDC-denominated fee equivalent to shares at the execution price.
            entry_fee_shares = entry_fee / entry_price if entry_price > 0 else 0.0
            shares = gross_shares - entry_fee_shares
            entry_fee_collection = "shares"
            cash_entry_fee = 0.0
        elif fee_model.get("mode") == "legacy_flat_rate_fallback":
            # Explicit legacy fallback is a cash assumption, not a claimed
            # reproduction of Polymarket's market-specific fee settlement.
            entry_fee_shares = 0.0
            shares = gross_shares
            entry_fee_collection = "cash"
            cash_entry_fee = entry_fee
        else:
            entry_fee_shares = 0.0
            shares = gross_shares
            entry_fee_collection = "none"
            cash_entry_fee = 0.0
        if shares <= 0.0:
            return None, "entry_fee_exceeds_gross_shares"
        if spend + cash_entry_fee > available_cash + 1e-8:
            return None, "insufficient_cash_after_market_fee"

        if market_quality.get("reason") == "approved_market_quality":
            initial_mark_fill_details = simulate_orderbook_fill(
                row.get("orderbook"), side="sell", target_shares=shares
            )
            if initial_mark_fill_details.get("eligible"):
                initial_mark_price = float(initial_mark_fill_details["average_price"])
                initial_mark_source = "clob_top5_depth_vwap"
            else:
                initial_mark_price = observed_price
                initial_mark_source = "gamma_outcome_price_fallback_insufficient_bid_depth"
        position = {
            "token_id": key,
            "market_id": str(row.get("market_id") or ""),
            "question": str(row.get("question") or "Unknown market")[:500],
            "outcome": str(row.get("outcome") or "Unknown outcome")[:100],
            "opened_at": iso_time(now),
            "entry_signal_price": round(observed_price, 8),
            "entry_reference_price": round(entry_reference_price, 8),
            "entry_limit_price": (
                round(entry_limit_price, 8) if entry_limit_price is not None else None
            ),
            "entry_price_source": entry_price_source,
            "entry_price": round(entry_price, 8),
            "mark_price": round(initial_mark_price, 8),
            "mark_source": initial_mark_source,
            "gross_shares": round(gross_shares, 12),
            "shares": round(shares, 12),
            "entry_fee_shares": round(entry_fee_shares, 12),
            "entry_fee_collection": entry_fee_collection,
            "gross_cost": round(spend, 8),
            "entry_fee": round(entry_fee, 8),
            "fee_model": fee_model,
            "entry_fee_details": entry_fee_details,
            "signal_delta": round(delta, 8),
            "entry_market_quality": market_quality,
            "entry_fill_details": entry_fill_details,
            "initial_mark_fill_details": initial_mark_fill_details,
            "unrealized_pnl": 0.0,
            "status": "OPEN",
        }
        position["unrealized_pnl"] = round(
            self._position_unrealized(position, initial_mark_price), 8
        )
        self._state["cash"] = round(available_cash - spend - cash_entry_fee, 8)
        self._state["total_fees"] = round(self._state["total_fees"] + entry_fee, 8)
        positions[key] = position
        return {"event_type": "position_opened", "timestamp": iso_time(now), **copy.deepcopy(position)}, ""

    def _exit_reason(self, position: dict[str, Any], price: float, now: datetime) -> str | None:
        entry = float(position["entry_price"])
        if price <= entry * (1.0 - self.config.stop_loss_pct):
            return "simulated_stop_loss"
        if price >= entry * (1.0 + self.config.take_profit_pct):
            return "simulated_take_profit"
        try:
            age_hours = (now - parse_time(str(position["opened_at"]))).total_seconds() / 3600.0
        except (ValueError, TypeError, KeyError):
            age_hours = 0.0
        if age_hours >= self.config.max_hold_hours:
            return "simulated_time_limit"
        return None

    def _position_fee_model(self, position: dict[str, Any]) -> dict[str, Any]:
        stored = position.get("fee_model")
        if isinstance(stored, dict):
            return stored
        # Backward compatibility for positions created before market-specific fees.
        return {
            "status": "assumed",
            "mode": "legacy_flat_rate_fallback",
            "rate": self.config.fee_rate,
            "source": "legacy_paper_config",
            "research_only": True,
            "execution": "none",
        }

    def _position_unrealized(self, position: dict[str, Any], observed_price: float) -> float:
        exit_price = observed_price * (1.0 - self.config.simulated_slippage)
        shares = float(position["shares"])
        gross_proceeds = shares * exit_price
        fee_details = estimate_taker_fee(
            self._position_fee_model(position),
            shares=shares,
            price=exit_price,
            notional=gross_proceeds,
        )
        entry_fee_cash_cost = (
            0.0 if position.get("entry_fee_collection") == "shares"
            else float(position.get("entry_fee", 0.0))
        )
        if not fee_details.get("known"):
            return -float(position["gross_cost"]) - entry_fee_cash_cost
        net_proceeds = gross_proceeds - float(fee_details["fee"])
        return net_proceeds - float(position["gross_cost"]) - entry_fee_cash_cost

    def _close_position(
        self,
        key: str,
        observed_price: float,
        reason: str,
        now: datetime,
        *,
        price_source: str = "gamma_outcome_price_fallback",
        fill_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        position = self._state["positions"][key]
        exit_price = observed_price * (1.0 - self.config.simulated_slippage)
        gross_proceeds = float(position["shares"]) * exit_price
        fee_model = self._position_fee_model(position)
        exit_fee_details = estimate_taker_fee(
            fee_model,
            shares=float(position["shares"]),
            price=exit_price,
            notional=gross_proceeds,
        )
        if not exit_fee_details.get("known"):
            raise ValueError("Cannot close paper position without a valid fee estimate")
        exit_fee = float(exit_fee_details["fee"])
        net_proceeds = gross_proceeds - exit_fee
        entry_fee_cash_cost = (
            0.0 if position.get("entry_fee_collection") == "shares"
            else float(position.get("entry_fee", 0.0))
        )
        pnl = net_proceeds - float(position["gross_cost"]) - entry_fee_cash_cost
        # Commit the state transition only after the complete exit valuation succeeds.
        self._state["positions"].pop(key)
        self._state["cash"] = round(float(self._state["cash"]) + net_proceeds, 8)
        self._state["realized_pnl"] = round(float(self._state["realized_pnl"]) + pnl, 8)
        self._state["daily_realized_pnl"] = round(float(self._state["daily_realized_pnl"]) + pnl, 8)
        self._state["total_fees"] = round(float(self._state["total_fees"]) + exit_fee, 8)
        self._state["closed_trades"] = int(self._state["closed_trades"]) + 1
        if pnl > 0:
            self._state["winning_trades"] = int(self._state["winning_trades"]) + 1
        elif pnl < 0:
            self._state["losing_trades"] = int(self._state["losing_trades"]) + 1
        return {
            "event_type": "position_closed",
            "timestamp": iso_time(now),
            "token_id": key,
            "market_id": position.get("market_id", ""),
            "question": position.get("question", ""),
            "outcome": position.get("outcome", ""),
            "opened_at": position.get("opened_at"),
            "closed_at": iso_time(now),
            "entry_price": position["entry_price"],
            "exit_observed_price": round(observed_price, 8),
            "exit_price_source": price_source,
            "exit_price_after_slippage": round(exit_price, 8),
            "exit_fill_details": fill_details,
            "shares": position["shares"],
            "gross_shares": position.get("gross_shares", position["shares"]),
            "entry_fee_shares": position.get("entry_fee_shares", 0.0),
            "entry_fee_collection": position.get("entry_fee_collection", "cash"),
            "gross_cost": position["gross_cost"],
            "entry_fee": position["entry_fee"],
            "exit_fee": round(exit_fee, 8),
            "fee_model": fee_model,
            "exit_fee_details": exit_fee_details,
            "pnl": round(pnl, 8),
            "reason": reason,
            "execution": "simulated_only",
        }

    def settle_resolved_positions(
        self,
        resolutions: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Settle open paper positions from definitive, timestamped binary resolutions.

        This is a simulated $1/$0 contract settlement, not a redemption request.
        Any buy fee already deducted in shares is reflected in the remaining shares.
        """
        current_time = now or utc_now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        current_time = current_time.astimezone(timezone.utc)

        by_market: dict[str, dict[str, Any]] = {}
        conflicts: set[str] = set()
        for row in resolutions:
            if not isinstance(row, dict):
                continue
            market_id = str(row.get("market_id") or "").strip()
            resolved_yes = row.get("resolved_yes")
            try:
                resolved_at = parse_time(str(row.get("resolved_at") or ""))
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                not market_id
                or isinstance(resolved_yes, bool)
                or not isinstance(resolved_yes, int)
                or resolved_yes not in (0, 1)
                or resolved_at > current_time + timedelta(minutes=1)
            ):
                continue
            normalized = {**row, "market_id": market_id, "resolved_yes": resolved_yes}
            previous = by_market.get(market_id)
            if previous is not None and previous["resolved_yes"] != resolved_yes:
                conflicts.add(market_id)
            elif previous is None or resolved_at > parse_time(str(previous["resolved_at"])):
                by_market[market_id] = normalized

        settled: list[dict[str, Any]] = []
        skipped = {"invalid_resolution": 0, "conflicting_resolution": len(conflicts)}
        with self._lock:
            self._daily_rollover(current_time)
            for key, position in list(self._state["positions"].items()):
                market_id = str(position.get("market_id") or "").strip()
                if not market_id or market_id not in by_market:
                    continue
                if market_id in conflicts:
                    continue
                resolution = by_market[market_id]
                outcome = str(position.get("outcome") or "").strip().casefold()
                if outcome not in {"yes", "no"}:
                    skipped["invalid_resolution"] += 1
                    continue
                try:
                    opened_at = parse_time(str(position.get("opened_at") or ""))
                    resolved_at = parse_time(str(resolution["resolved_at"]))
                except (TypeError, ValueError, OverflowError):
                    skipped["invalid_resolution"] += 1
                    continue
                if resolved_at <= opened_at:
                    skipped["invalid_resolution"] += 1
                    continue

                resolved_yes = int(resolution["resolved_yes"])
                won = (outcome == "yes" and resolved_yes == 1) or (
                    outcome == "no" and resolved_yes == 0
                )
                shares = finite_float(position.get("shares"))
                gross_cost = finite_float(position.get("gross_cost"))
                entry_fee = finite_float(position.get("entry_fee"))
                if (
                    shares is None or shares <= 0.0
                    or gross_cost is None or gross_cost < 0.0
                    or entry_fee is None or entry_fee < 0.0
                ):
                    skipped["invalid_resolution"] += 1
                    continue

                payout = shares if won else 0.0
                entry_fee_cash_cost = (
                    0.0 if position.get("entry_fee_collection") == "shares"
                    else entry_fee
                )
                pnl = payout - gross_cost - entry_fee_cash_cost
                self._state["positions"].pop(key)
                self._state["last_prices"].pop(key, None)
                self._state["cash"] = round(float(self._state["cash"]) + payout, 8)
                self._state["realized_pnl"] = round(float(self._state["realized_pnl"]) + pnl, 8)
                self._state["daily_realized_pnl"] = round(
                    float(self._state["daily_realized_pnl"]) + pnl, 8
                )
                self._state["closed_trades"] = int(self._state["closed_trades"]) + 1
                if pnl > 0.0:
                    self._state["winning_trades"] = int(self._state["winning_trades"]) + 1
                elif pnl < 0.0:
                    self._state["losing_trades"] = int(self._state["losing_trades"]) + 1

                event = {
                    "event_type": "position_settled",
                    "timestamp": iso_time(current_time),
                    "token_id": key,
                    "market_id": market_id,
                    "question": position.get("question", ""),
                    "outcome": position.get("outcome", ""),
                    "opened_at": position.get("opened_at"),
                    "resolved_at": resolution["resolved_at"],
                    "resolution_source": str(
                        resolution.get("resolution_source") or "public_gamma_resolved_market"
                    ),
                    "resolved_yes": resolved_yes,
                    "won": won,
                    "shares_settled": round(shares, 12),
                    "settlement_value_per_share": 1.0 if won else 0.0,
                    "settlement_payout_usd": round(payout, 8),
                    "gross_cost": round(gross_cost, 8),
                    "entry_fee": round(entry_fee, 8),
                    "entry_fee_collection": str(position.get("entry_fee_collection") or "cash"),
                    "entry_fee_cash_cost": round(entry_fee_cash_cost, 8),
                    "settlement_fee": 0.0,
                    "pnl": round(pnl, 8),
                    "reason": "definitive_binary_market_resolution",
                    "price_source": "public_gamma_resolved_binary_outcome",
                    "execution": "simulated_only",
                    "research_only": True,
                }
                self._state["recent_events"] = (
                    self._state.get("recent_events", []) + [event]
                )[-100:]
                settled.append(event)

            if settled:
                self._update_drawdown_metrics()
                self._save()
                for event in settled:
                    self._write_event(event)

        return {
            "settled_count": len(settled),
            "settled": settled,
            "skipped": skipped,
            "execution": "none",
            "research_only": True,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
            positions = list(state["positions"].values())
            marked_value = 0.0
            unrealized = 0.0
            exposure = 0.0
            for position in positions:
                mark = finite_float(position.get("mark_price")) or 0.0
                shares = float(position.get("shares", 0.0))
                exit_price = mark * (1.0 - self.config.simulated_slippage)
                gross_value = shares * exit_price
                fee_details = estimate_taker_fee(
                    self._position_fee_model(position),
                    shares=shares,
                    price=exit_price,
                    notional=gross_value,
                )
                marked_value += max(
                    0.0,
                    gross_value - float(fee_details["fee"] if fee_details.get("known") else gross_value),
                )
                unrealized += float(position.get("unrealized_pnl", 0.0))
                exposure += float(position.get("gross_cost", 0.0))
            equity = float(state["cash"]) + marked_value
            equity_peak = max(
                float(state.get("equity_peak", state["starting_cash"])),
                equity,
            )
            current_drawdown_usd = max(0.0, equity_peak - equity)
            current_drawdown_pct = (
                100.0 * current_drawdown_usd / equity_peak if equity_peak > 0.0 else 0.0
            )
            maximum_drawdown_usd = max(
                float(state.get("max_drawdown_usd", 0.0)), current_drawdown_usd
            )
            maximum_drawdown_pct = max(
                float(state.get("max_drawdown_pct", 0.0)), current_drawdown_pct
            )
            state["metrics"] = {
                "cash": round(float(state["cash"]), 8),
                "equity": round(equity, 8),
                "total_pnl": round(equity - float(state["starting_cash"]), 8),
                "return_pct": round((equity / float(state["starting_cash"]) - 1.0) * 100.0, 4),
                "realized_pnl": round(float(state["realized_pnl"]), 8),
                "unrealized_pnl": round(unrealized, 8),
                "open_exposure": round(exposure, 8),
                "open_positions": len(positions),
                "closed_trades": int(state["closed_trades"]),
                "win_rate_pct": round(100.0 * int(state["winning_trades"]) / int(state["closed_trades"]), 4) if int(state["closed_trades"]) else 0.0,
                "total_fees": round(float(state["total_fees"]), 8),
                "daily_realized_pnl": round(float(state["daily_realized_pnl"]), 8),
                "daily_loss_limit_usd": self.config.daily_loss_limit_usd,
                "equity_peak": round(equity_peak, 8),
                "current_drawdown_usd": round(current_drawdown_usd, 8),
                "current_drawdown_pct": round(current_drawdown_pct, 8),
                "max_drawdown_usd": round(maximum_drawdown_usd, 8),
                "max_drawdown_pct": round(maximum_drawdown_pct, 8),
                "max_portfolio_drawdown_pct": self.config.max_portfolio_drawdown_pct,
                "drawdown_limit_reached": (
                    current_drawdown_pct + 1e-9 >= self.config.max_portfolio_drawdown_pct
                ),
                "circuit_breaker_tripped": bool(state.get("circuit_breaker_tripped")),
                "circuit_breaker_reason": state.get("circuit_breaker_reason"),
                "circuit_breaker_at": state.get("circuit_breaker_at"),
                "circuit_breaker_drawdown_pct": state.get("circuit_breaker_drawdown_pct"),
                "new_entries_halted": bool(state.get("circuit_breaker_tripped")),
            }
            state["positions_list"] = positions
            # Keep detailed price history out of the public status response.
            state.pop("last_prices", None)
            return state

    def reset(self) -> dict[str, Any]:
        """Reset the paper portfolio and clear its latched simulator circuit breaker.

        This resets the complete simulated account state; there is no automatic
        breaker release after an equity recovery.
        """
        with self._lock:
            self._state = self._new_state()
            self._save()
            return self.snapshot()
