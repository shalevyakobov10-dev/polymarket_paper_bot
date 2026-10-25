"""Deterministic, offline-only paper simulator. Never submits orders."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Position:
    token_id: str
    question: str
    units: float
    entry_price: float
    entry_fee: float
    entry_timestamp: str
    last_price: float


class PaperSimulator:
    """Toy long-only simulator for outcome tokens using observed snapshots.

    Assumptions are intentionally explicit: entries/exits are filled at the
    observed snapshot price plus configurable slippage, and a fee is charged
    on both sides. This is not a realistic order-book fill model.
    """

    def __init__(
        self,
        initial_cash: float = 100.0,
        max_position_usd: float = 2.0,
        fee_rate: float = 0.002,
        slippage_rate: float = 0.001,
        entry_delta: float = 0.03,
        exit_delta: float = 0.01,
    ):
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if max_position_usd <= 0:
            raise ValueError("max_position_usd must be positive")
        if not 0 <= fee_rate < 1 or not 0 <= slippage_rate < 1:
            raise ValueError("fee_rate and slippage_rate must be in [0, 1)")
        if not 0 < entry_delta <= 1 or not 0 < exit_delta <= 1:
            raise ValueError("entry_delta and exit_delta must be in (0, 1]")
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.max_position_usd = float(max_position_usd)
        self.fee_rate = float(fee_rate)
        self.slippage_rate = float(slippage_rate)
        self.entry_delta = float(entry_delta)
        self.exit_delta = float(exit_delta)
        self.positions: dict[str, Position] = {}
        self.previous_prices: dict[str, float] = {}
        self.closed_trades: list[dict] = []
        self.events: list[dict] = []

    def on_observation(self, row: dict) -> Optional[dict]:
        token = str(row.get("token_id") or "")
        if not token:
            return None
        try:
            price = float(row["observed_price"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 < price < 1:
            return None

        timestamp = str(row.get("timestamp") or "")
        question = str(row.get("question") or "")
        previous = self.previous_prices.get(token)
        self.previous_prices[token] = price
        position = self.positions.get(token)

        if position is not None:
            position.last_price = price
            delta = price - position.last_price if False else (
                price - float(row.get("previous_observed_price", previous if previous is not None else price))
            )
            # Close on a sufficiently negative observed move from prior snapshot.
            if delta <= -self.exit_delta:
                fill_price = min(0.999999, price * (1 + self.slippage_rate))
                proceeds = position.units * fill_price
                exit_fee = proceeds * self.fee_rate
                self.cash += proceeds - exit_fee
                pnl = proceeds - exit_fee - (position.units * position.entry_price + position.entry_fee)
                trade = {
                    "token_id": token,
                    "question": position.question,
                    "entry_price": position.entry_price,
                    "exit_price": fill_price,
                    "units": position.units,
                    "entry_timestamp": position.entry_timestamp,
                    "exit_timestamp": timestamp,
                    "pnl_usd": round(pnl, 8),
                    "execution": "simulated_only",
                }
                self.closed_trades.append(trade)
                del self.positions[token]
                self.events.append({"type": "exit", **trade})
                return self.events[-1]
            return None

        if previous is None:
            return None
        delta = price - previous
        if delta < self.entry_delta:
            return None

        stake = min(self.max_position_usd, self.cash / (1 + self.fee_rate))
        if stake <= 0:
            return None
        fill_price = max(0.000001, price * (1 + self.slippage_rate))
        if fill_price >= 1:
            return None
        fee = stake * self.fee_rate
        total = stake + fee
        if total > self.cash:
            stake = self.cash / (1 + self.fee_rate)
            fee = stake * self.fee_rate
            total = stake + fee
        units = stake / fill_price
        self.cash -= total
        position = Position(token, question, units, fill_price, fee, timestamp, price)
        self.positions[token] = position
        event = {
            "type": "entry",
            "token_id": token,
            "question": question,
            "entry_price": fill_price,
            "units": units,
            "stake_usd": stake,
            "fee_usd": fee,
            "timestamp": timestamp,
            "cash_after_usd": self.cash,
            "execution": "simulated_only",
        }
        self.events.append(event)
        return event

    def summary(self, latest_prices: Optional[dict[str, float]] = None) -> dict:
        latest_prices = latest_prices or {}
        unrealized = 0.0
        market_value = 0.0
        for token, position in self.positions.items():
            mark = float(latest_prices.get(token, position.last_price))
            value = position.units * mark
            market_value += value
            unrealized += value - position.units * position.entry_price - position.entry_fee
        realized = sum(float(t["pnl_usd"]) for t in self.closed_trades)
        equity = self.cash + market_value
        return {
            "mode": "paper_only",
            "initial_cash_usd": round(self.initial_cash, 8),
            "cash_usd": round(self.cash, 8),
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "realized_pnl_usd": round(realized, 8),
            "unrealized_pnl_usd": round(unrealized, 8),
            "equity_usd": round(equity, 8),
            "return_pct": round((equity / self.initial_cash - 1) * 100, 6),
            "execution": "simulated_only",
            "warning": "Illustrative fills; excludes real order-book liquidity and latency.",
        }
