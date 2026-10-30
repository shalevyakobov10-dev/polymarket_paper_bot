"""Persistent paper portfolio simulator. It never sends live orders."""
from __future__ import annotations

import copy
import json
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from risk_manager import evaluate_paper_entry
from market_quality import evaluate_order_book_quality


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
    fee_rate: float = 0.0
    daily_loss_limit_usd: float = 5.0
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
            "fee_rate": "PAPER_FEE_RATE",
            "daily_loss_limit_usd": "PAPER_DAILY_LOSS_LIMIT_USD",
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
        if config.daily_loss_limit_usd <= 0:
            raise ValueError("Daily loss limit must be positive")
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
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Do not silently reset state after a corrupted file.
            raise ValueError(f"Could not read paper state file {self.state_path}: {exc}") from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
            raise ValueError("Paper state file has an unsupported or malformed schema")
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
        loaded.setdefault("recent_events", [])
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
                    book_quality = evaluate_order_book_quality(
                        row.get("orderbook"),
                        now=current_time,
                        max_age_seconds=self.config.max_orderbook_age_seconds,
                        max_spread_bps=self.config.max_orderbook_spread_bps,
                        min_side_depth_usd=self.config.min_orderbook_side_depth_usd,
                        require_orderbook=False,
                    )
                    if book_quality.get("reason") == "approved_market_quality":
                        bid = finite_float((row.get("orderbook") or {}).get("best_bid"))
                        if bid is not None and 0.0 < bid < 1.0:
                            mark_price = bid
                            mark_source = "clob_best_bid"
                    position["mark_price"] = mark_price
                    position["mark_source"] = mark_source
                    position["mark_updated_at"] = iso_time(current_time)
                    position["unrealized_pnl"] = round(self._position_unrealized(position, mark_price), 8)
                    reason = self._exit_reason(position, mark_price, current_time)
                    if reason:
                        closed = self._close_position(
                            key, mark_price, reason, current_time, price_source=mark_source
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
            if persist_state:
                self._save()
                for event in events:
                    self._write_event(event)
        return result

    def _try_open(self, key: str, row: dict[str, Any], observed_price: float, now: datetime, delta: float) -> tuple[dict[str, Any] | None, str]:
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
        positions: dict[str, dict[str, Any]] = self._state["positions"]
        exposure = sum(float(position.get("gross_cost", 0.0)) for position in positions.values())
        entry_reference_price = observed_price
        entry_price_source = "gamma_outcome_price_fallback"
        initial_mark_price = observed_price
        initial_mark_source = "gamma_outcome_price_fallback"
        if market_quality.get("reason") == "approved_market_quality":
            orderbook = row.get("orderbook") or {}
            ask = finite_float(orderbook.get("best_ask"))
            bid = finite_float(orderbook.get("best_bid"))
            if ask is not None and 0.0 < ask < 1.0:
                entry_reference_price = ask
                entry_price_source = "clob_best_ask"
            if bid is not None and 0.0 < bid < 1.0:
                initial_mark_price = bid
                initial_mark_source = "clob_best_bid"
        entry_price = entry_reference_price * (1.0 + self.config.simulated_slippage)
        if entry_price >= 1.0:
            return None, "slippage_makes_entry_price_invalid"
        available_cash = float(self._state["cash"])
        spend = min(
            self.config.max_position_usd,
            max(0.0, self.config.max_open_exposure_usd - exposure),
            available_cash / (1.0 + self.config.fee_rate),
        )
        open_unrealized_total = sum(
            float(position.get("unrealized_pnl", 0.0)) for position in positions.values()
        )
        open_unrealized_pnl = (
            open_unrealized_total - float(self._state.get("daily_unrealized_baseline", 0.0))
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
            fee_rate=self.config.fee_rate,
        )
        if not decision.approved:
            return None, decision.reason
        spend = decision.approved_notional
        shares = spend / entry_price
        entry_fee = spend * self.config.fee_rate
        position = {
            "token_id": key,
            "market_id": str(row.get("market_id") or ""),
            "question": str(row.get("question") or "Unknown market")[:500],
            "outcome": str(row.get("outcome") or "Unknown outcome")[:100],
            "opened_at": iso_time(now),
            "entry_signal_price": round(observed_price, 8),
            "entry_reference_price": round(entry_reference_price, 8),
            "entry_price_source": entry_price_source,
            "entry_price": round(entry_price, 8),
            "mark_price": round(initial_mark_price, 8),
            "mark_source": initial_mark_source,
            "shares": round(shares, 12),
            "gross_cost": round(spend, 8),
            "entry_fee": round(entry_fee, 8),
            "signal_delta": round(delta, 8),
            "entry_market_quality": market_quality,
            "unrealized_pnl": 0.0,
            "status": "OPEN",
        }
        position["unrealized_pnl"] = round(
            self._position_unrealized(position, initial_mark_price), 8
        )
        self._state["cash"] = round(available_cash - spend - entry_fee, 8)
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

    def _position_unrealized(self, position: dict[str, Any], observed_price: float) -> float:
        exit_price = observed_price * (1.0 - self.config.simulated_slippage)
        net_proceeds = float(position["shares"]) * exit_price * (1.0 - self.config.fee_rate)
        return net_proceeds - float(position["gross_cost"]) - float(position["entry_fee"])

    def _close_position(
        self,
        key: str,
        observed_price: float,
        reason: str,
        now: datetime,
        *,
        price_source: str = "gamma_outcome_price_fallback",
    ) -> dict[str, Any]:
        position = self._state["positions"].pop(key)
        exit_price = observed_price * (1.0 - self.config.simulated_slippage)
        gross_proceeds = float(position["shares"]) * exit_price
        exit_fee = gross_proceeds * self.config.fee_rate
        net_proceeds = gross_proceeds - exit_fee
        pnl = net_proceeds - float(position["gross_cost"]) - float(position["entry_fee"])
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
            "shares": position["shares"],
            "gross_cost": position["gross_cost"],
            "entry_fee": position["entry_fee"],
            "exit_fee": round(exit_fee, 8),
            "pnl": round(pnl, 8),
            "reason": reason,
            "execution": "simulated_only",
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
                marked_value += max(0.0, float(position.get("shares", 0.0)) * mark * (1.0 - self.config.simulated_slippage) * (1.0 - self.config.fee_rate))
                unrealized += float(position.get("unrealized_pnl", 0.0))
                exposure += float(position.get("gross_cost", 0.0))
            equity = float(state["cash"]) + marked_value
            state["metrics"] = {
                "cash": round(float(state["cash"]), 4),
                "equity": round(equity, 4),
                "total_pnl": round(equity - float(state["starting_cash"]), 4),
                "return_pct": round((equity / float(state["starting_cash"]) - 1.0) * 100.0, 4),
                "realized_pnl": round(float(state["realized_pnl"]), 4),
                "unrealized_pnl": round(unrealized, 4),
                "open_exposure": round(exposure, 4),
                "open_positions": len(positions),
                "closed_trades": int(state["closed_trades"]),
                "win_rate_pct": round(100.0 * int(state["winning_trades"]) / int(state["closed_trades"]), 4) if int(state["closed_trades"]) else 0.0,
                "total_fees": round(float(state["total_fees"]), 4),
                "daily_realized_pnl": round(float(state["daily_realized_pnl"]), 4),
                "daily_loss_limit_usd": self.config.daily_loss_limit_usd,
            }
            state["positions_list"] = positions
            # Keep detailed price history out of the public status response.
            state.pop("last_prices", None)
            return state

    def reset(self) -> dict[str, Any]:
        """Reset only the simulated portfolio; it cannot affect a real account."""
        with self._lock:
            self._state = self._new_state()
            self._save()
            return self.snapshot()
