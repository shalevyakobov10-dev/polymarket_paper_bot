"""Deterministic risk gates for the paper-only portfolio simulator.

This module never places orders. It evaluates a proposed simulated entry and
returns a stable reason code suitable for audit logs and unit tests.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    requested_notional: float
    approved_notional: float

    def as_dict(self) -> dict:
        return asdict(self)


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _finite_nonnegative(value: float) -> bool:
    return _finite(value) and float(value) >= 0.0


def evaluate_paper_entry(
    *,
    price: float,
    requested_notional: float,
    cash: float,
    open_exposure: float,
    open_positions: int,
    max_position_usd: float,
    max_open_exposure_usd: float,
    max_open_positions: int,
    daily_realized_pnl: float,
    open_unrealized_pnl: float,
    daily_loss_limit_usd: float,
    fee_rate: float,
) -> RiskDecision:
    """Approve or reject a proposed paper entry using conservative hard limits."""
    requested = float(requested_notional) if _finite_nonnegative(requested_notional) else 0.0

    def reject(reason: str) -> RiskDecision:
        return RiskDecision(False, reason, requested, 0.0)

    if not _finite_nonnegative(price) or not 0.01 < float(price) < 0.99:
        return reject("price_outside_entry_range")
    nonnegative_values = (
        requested_notional, cash, open_exposure, max_position_usd,
        max_open_exposure_usd, daily_loss_limit_usd, fee_rate,
    )
    if not all(_finite_nonnegative(v) for v in nonnegative_values):
        return reject("invalid_risk_input")
    if not _finite(daily_realized_pnl) or not _finite(open_unrealized_pnl):
        return reject("invalid_risk_input")
    if not isinstance(open_positions, int) or not isinstance(max_open_positions, int):
        return reject("invalid_position_count")
    if open_positions < 0 or max_open_positions < 1:
        return reject("invalid_position_count")
    if open_positions >= max_open_positions:
        return reject("max_open_positions_reached")
    if open_exposure >= max_open_exposure_usd - 1e-9:
        return reject("max_open_exposure_reached")
    # The daily stop includes both closed P&L and currently marked open P&L.
    daily_pnl = float(daily_realized_pnl) + float(open_unrealized_pnl)
    if daily_pnl <= -float(daily_loss_limit_usd):
        return reject("daily_loss_limit_reached")
    if requested <= 0.0:
        return reject("insufficient_simulated_cash")
    if requested > max_position_usd + 1e-9:
        return reject("max_position_size_reached")
    if open_exposure + requested > max_open_exposure_usd + 1e-9:
        return reject("max_open_exposure_reached")
    if requested * (1.0 + fee_rate) > cash + 1e-9:
        return reject("insufficient_simulated_cash_after_fee")
    return RiskDecision(True, "approved_paper_entry", requested, requested)
