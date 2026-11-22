"""Conservative, paper-only risk gate for Polymarket Perps proposals.

This gate does not calculate an exchange liquidation price and must not be
presented as a replacement for the venue's live margin engine. It uses current
instrument risk tiers, reserves initial margin, fees, and a funding buffer,
and rejects stale, incomplete, or ambiguous market/account state.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


ZERO = Decimal("0")


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


@dataclass(frozen=True)
class PerpsRiskLimits:
    max_leverage: Decimal = Decimal("2")
    max_position_notional: Decimal = Decimal("10")
    max_total_open_exposure: Decimal = Decimal("20")
    max_open_positions: int = 3
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_daily_loss_pct: Decimal = Decimal("0.02")
    max_portfolio_drawdown_pct: Decimal = Decimal("0.05")
    max_spread_bps: Decimal = Decimal("250")
    stop_loss_distance_pct: Decimal = Decimal("0.01")
    taker_fee_reserve_rate: Decimal = Decimal("0.0005")
    funding_reserve_rate: Decimal = Decimal("0.001")
    min_liquidation_margin_multiple: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        positive = (
            "max_leverage", "max_position_notional", "max_total_open_exposure",
            "risk_per_trade_pct", "max_daily_loss_pct", "max_portfolio_drawdown_pct",
            "max_spread_bps", "stop_loss_distance_pct", "min_liquidation_margin_multiple",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive Decimal")
        for name in ("taker_fee_reserve_rate", "funding_reserve_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a non-negative Decimal")
        if isinstance(self.max_open_positions, bool) or not isinstance(self.max_open_positions, int) or self.max_open_positions < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if self.max_leverage > Decimal("2"):
            raise ValueError("paper max leverage cannot exceed 2x")


def _tier_max_leverage(snapshot: Mapping[str, Any], notional: Decimal) -> Decimal:
    tiers = snapshot.get("risk_tiers")
    if not isinstance(tiers, list) or not tiers:
        raise ValueError("missing_risk_tiers")
    eligible: list[tuple[Decimal, Decimal]] = []
    for tier in tiers:
        if not isinstance(tier, Mapping):
            continue
        lower = _d(tier.get("lower_bound"), "risk_tier_lower_bound")
        maximum = _d(tier.get("max_leverage"), "risk_tier_max_leverage")
        if lower < ZERO or maximum <= ZERO:
            raise ValueError("invalid_risk_tier")
        if lower <= notional:
            eligible.append((lower, maximum))
    if not eligible:
        raise ValueError("no_eligible_risk_tier")
    eligible.sort(key=lambda item: item[0])
    return eligible[-1][1]


def evaluate_perps_entry(
    snapshot: Mapping[str, Any],
    *,
    side: str,
    requested_notional: Any,
    equity: Any,
    free_collateral: Any,
    open_exposure: Any = ZERO,
    open_positions: int = 0,
    daily_loss: Any = ZERO,
    peak_equity: Any | None = None,
    limits: PerpsRiskLimits | None = None,
    stop_loss_distance_pct: Any | None = None,
) -> dict[str, Any]:
    """Return a stable allow/block decision for a *paper* Perps entry."""
    policy = limits or PerpsRiskLimits()

    def reject(reason: str, **details: Any) -> dict[str, Any]:
        return {
            "allowed": False,
            "reason": reason,
            "research_only": True,
            "execution": "none",
            **details,
        }

    try:
        if snapshot.get("research_only") is not True or snapshot.get("execution") != "none":
            return reject("snapshot_not_research_only")
        if snapshot.get("entry_eligible") is not True or snapshot.get("close_only") is True:
            return reject("instrument_close_only")
        if snapshot.get("settlement") is True:
            return reject("instrument_settled")
        for field in ("ticker_age_seconds", "book_age_seconds"):
            age = _d(snapshot.get(field), field)
            if age < ZERO or age > Decimal("10"):
                return reject("stale_market_data", field=field)
        if str(snapshot.get("side", side)).upper() != side.upper():
            return reject("side_mismatch")
        direction = side.strip().upper()
        if direction not in {"LONG", "SHORT"}:
            return reject("invalid_side")

        notional = _d(requested_notional, "requested_notional")
        account_equity = _d(equity, "equity")
        collateral = _d(free_collateral, "free_collateral")
        current_exposure = _d(open_exposure, "open_exposure")
        current_daily_loss = _d(daily_loss, "daily_loss")
        peak = _d(peak_equity if peak_equity is not None else equity, "peak_equity")
        if notional <= ZERO or account_equity <= ZERO or collateral < ZERO or current_exposure < ZERO:
            return reject("invalid_account_or_order_amount")
        if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
            return reject("invalid_open_position_count")
        if open_positions >= policy.max_open_positions:
            return reject("max_open_positions_reached")
        if notional > policy.max_position_notional:
            return reject("max_position_notional_exceeded")
        if current_exposure + notional > policy.max_total_open_exposure:
            return reject("max_total_exposure_exceeded")

        mark = _d(snapshot.get("mark_price"), "mark_price")
        index = _d(snapshot.get("index_price"), "index_price")
        spread = _d(snapshot.get("spread_bps"), "spread_bps")
        min_notional_raw = snapshot.get("min_notional")
        max_notional_raw = snapshot.get("max_market_notional")
        if mark <= ZERO or index <= ZERO:
            return reject("invalid_reference_price")
        if spread < ZERO or spread > policy.max_spread_bps:
            return reject("spread_above_limit")
        if min_notional_raw is not None and notional < _d(min_notional_raw, "min_notional"):
            return reject("below_market_minimum")
        if max_notional_raw is not None and notional > _d(max_notional_raw, "max_market_notional"):
            return reject("market_maximum_exceeded")

        instrument_max = _d(snapshot.get("max_leverage"), "max_leverage")
        tier_max = _tier_max_leverage(snapshot, notional)
        effective_max = min(instrument_max, tier_max)
        leverage = policy.max_leverage
        if leverage > effective_max:
            return reject("leverage_exceeds_market_tier", market_max_leverage=str(effective_max))
        if leverage > Decimal("2"):
            return reject("paper_leverage_cap_exceeded")

        stop_distance = _d(
            stop_loss_distance_pct if stop_loss_distance_pct is not None else policy.stop_loss_distance_pct,
            "stop_loss_distance_pct",
        )
        if not Decimal("0.0025") <= stop_distance <= Decimal("0.03"):
            return reject("stop_loss_distance_out_of_bounds")
        stop_risk = notional * stop_distance
        allowed_risk = account_equity * policy.risk_per_trade_pct
        if stop_risk > allowed_risk:
            return reject("risk_per_trade_exceeded", estimated_stop_loss=str(stop_risk), max_risk=str(allowed_risk))

        initial_margin = notional / leverage
        # Polymarket documentation defines MMR as 0.5 / market MaxLeverage.
        # This is an estimate for the paper gate, not a liquidation-price model.
        maintenance_rate = Decimal("0.5") / effective_max
        maintenance_margin = notional * maintenance_rate
        if maintenance_margin <= ZERO or initial_margin < maintenance_margin * policy.min_liquidation_margin_multiple:
            return reject("insufficient_margin_safety_buffer")
        fee_reserve = notional * policy.taker_fee_reserve_rate
        funding_reserve = notional * policy.funding_reserve_rate
        required_collateral = initial_margin + fee_reserve + funding_reserve
        if collateral < required_collateral:
            return reject("insufficient_free_collateral", required_collateral=str(required_collateral))
        if current_daily_loss / account_equity >= policy.max_daily_loss_pct:
            return reject("daily_loss_limit_reached")
        if peak <= ZERO or account_equity > peak * Decimal("1.000001"):
            return reject("invalid_peak_equity")
        drawdown = max(ZERO, (peak - account_equity) / peak)
        if drawdown >= policy.max_portfolio_drawdown_pct:
            return reject("portfolio_drawdown_limit_reached")

        return {
            "allowed": True,
            "reason": "approved_paper_perps_entry",
            "side": direction,
            "notional": str(notional),
            "leverage": str(leverage),
            "effective_market_max_leverage": str(effective_max),
            "initial_margin": str(initial_margin),
            "maintenance_margin_estimate": str(maintenance_margin),
            "taker_fee_reserve": str(fee_reserve),
            "funding_reserve": str(funding_reserve),
            "required_free_collateral": str(required_collateral),
            "estimated_stop_loss": str(stop_risk),
            "stop_loss_distance_pct": str(stop_distance),
            "risk_budget": str(allowed_risk),
            "portfolio_drawdown": str(drawdown),
            "research_only": True,
            "execution": "none",
            "live_execution_eligible": False,
        }
    except (ValueError, TypeError, ArithmeticError):
        return reject("invalid_or_incomplete_risk_input")
