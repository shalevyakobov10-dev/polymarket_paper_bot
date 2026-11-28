"""Position sizing in front of the existing paper risk gate.

Fractional Kelly proposes a notional. ``evaluate_paper_entry`` remains the hard
limit for cash, exposure, daily loss, and drawdown. This module never orders.
"""
from __future__ import annotations

from risk_manager import RiskDecision, evaluate_paper_entry


def fractional_kelly_notional(
    *,
    probability: float,
    market_price: float,
    bankroll: float,
    kelly_fraction: float,
    max_position_usdc: float,
) -> float:
    """Size a binary contract that costs ``market_price`` and pays 1 if it wins.

    Full Kelly is ``(q - p) / (1 - p)``. The fraction is capped by the configured
    maximum USDC notional. No edge, or an invalid price, sizes to zero.
    """
    try:
        q = float(probability)
        p = float(market_price)
        wealth = float(bankroll)
        fraction = float(kelly_fraction)
        cap = float(max_position_usdc)
    except (TypeError, ValueError):
        return 0.0
    if not all(value == value and abs(value) != float("inf") for value in (q, p, wealth, fraction, cap)):
        return 0.0
    if not 0.0 < q < 1.0 or not 0.0 < p < 1.0 or wealth <= 0.0 or fraction <= 0.0 or cap <= 0.0:
        return 0.0
    if q <= p:
        return 0.0
    full_kelly = (q - p) / (1.0 - p)
    raw = wealth * fraction * full_kelly
    if raw <= 0.0:
        return 0.0
    return round(min(raw, cap), 6)


def evaluate_sized_entry(
    *,
    probability: float,
    market_price: float,
    bankroll: float,
    kelly_fraction: float,
    max_position_usdc: float,
    cash: float,
    open_exposure: float,
    open_positions: int,
    max_open_exposure_usd: float,
    max_open_positions: int,
    daily_realized_pnl: float,
    open_unrealized_pnl: float,
    max_daily_drawdown_usdc: float,
    fee_rate: float,
    opposing_market_outcome_open: bool = False,
    current_drawdown_pct: float = 0.0,
    max_portfolio_drawdown_pct: float = 20.0,
) -> tuple[RiskDecision, float]:
    """Return the existing paper risk decision and the Kelly notional that was requested."""
    notional = fractional_kelly_notional(
        probability=probability,
        market_price=market_price,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        max_position_usdc=max_position_usdc,
    )
    if notional <= 0.0:
        return (
            RiskDecision(False, "non_positive_kelly", 0.0, 0.0),
            0.0,
        )
    decision = evaluate_paper_entry(
        price=market_price,
        requested_notional=notional,
        cash=cash,
        open_exposure=open_exposure,
        open_positions=open_positions,
        max_position_usd=max_position_usdc,
        max_open_exposure_usd=max_open_exposure_usd,
        max_open_positions=max_open_positions,
        daily_realized_pnl=daily_realized_pnl,
        open_unrealized_pnl=open_unrealized_pnl,
        daily_loss_limit_usd=max_daily_drawdown_usdc,
        fee_rate=fee_rate,
        opposing_market_outcome_open=opposing_market_outcome_open,
        current_drawdown_pct=current_drawdown_pct,
        max_portfolio_drawdown_pct=max_portfolio_drawdown_pct,
    )
    return decision, notional
