"""Market-specific taker-fee estimates for the paper-only simulator.

Fee schedules are sourced from public Gamma market metadata. This module has no
account, signing, order, or execution capability.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def resolve_taker_fee_model(
    market: dict[str, Any] | None,
    *,
    fallback_rate: float = 0.0,
) -> dict[str, Any]:
    """Resolve an auditable taker fee model from one Gamma market row.

    A market explicitly marked fee-enabled without a usable feeSchedule is
    ineligible for a new paper entry. Unknown fee metadata uses the configured
    legacy flat-rate assumption, which is clearly labelled in the position.
    """
    fallback = _finite(fallback_rate)
    if fallback is None or not 0.0 <= fallback < 0.5:
        raise ValueError("fallback_rate must be finite and in [0, 0.5)")

    row = market if isinstance(market, dict) else {}
    fees_enabled = row.get("fees_enabled", row.get("feesEnabled"))
    schedule = row.get("fee_schedule", row.get("feeSchedule"))

    if fees_enabled is False or str(fees_enabled).strip().lower() in {"false", "0", "no"}:
        return {
            "status": "known",
            "mode": "fee_free",
            "rate": 0.0,
            "source": "gamma_fees_enabled_false",
            "exponent": None,
            "taker_only": True,
            "research_only": True,
            "execution": "none",
        }

    if isinstance(schedule, dict):
        rate = _finite(schedule.get("rate"))
        exponent = _finite(schedule.get("exponent"))
        taker_only = schedule.get("takerOnly", schedule.get("taker_only"))
        if rate is not None and 0.0 <= rate < 0.5 and exponent == 1.0:
            return {
                "status": "known",
                "mode": "polymarket_taker_curve",
                "rate": rate,
                "source": "gamma_fee_schedule",
                "exponent": exponent,
                "taker_only": taker_only is True,
                "rebate_rate": _finite(schedule.get("rebateRate", schedule.get("rebate_rate"))),
                "research_only": True,
                "execution": "none",
            }
        return {
            "status": "unknown",
            "mode": "unsupported_fee_schedule",
            "rate": None,
            "source": "gamma_fee_schedule",
            "reason": "invalid_rate_or_unsupported_exponent",
            "exponent": exponent,
            "research_only": True,
            "execution": "none",
        }

    if fees_enabled is True or str(fees_enabled).strip().lower() in {"true", "1", "yes"}:
        return {
            "status": "unknown",
            "mode": "missing_fee_schedule",
            "rate": None,
            "source": "gamma_market_metadata",
            "reason": "fees_enabled_but_fee_schedule_missing",
            "research_only": True,
            "execution": "none",
        }

    return {
        "status": "assumed",
        "mode": "legacy_flat_rate_fallback",
        "rate": fallback,
        "source": "paper_fee_rate_configuration",
        "exponent": None,
        "research_only": True,
        "execution": "none",
    }


def estimate_taker_fee(
    fee_model: dict[str, Any],
    *,
    shares: float,
    price: float,
    notional: float | None = None,
) -> dict[str, Any]:
    """Estimate one taker-side fee using the documented market fee curve.

    For a Gamma feeSchedule with exponent 1:
      fee = shares * rate * price * (1 - price)

    Fees are rounded to five decimal places with the published 0.00001 minimum
    for a positive charge. The legacy fallback deliberately retains a flat
    notional percentage and is labelled as an assumption.
    """
    quantity = _finite(shares)
    execution_price = _finite(price)
    gross = _finite(notional)
    if quantity is None or quantity < 0 or execution_price is None or not 0.0 <= execution_price <= 1.0:
        raise ValueError("shares must be nonnegative and price must be in [0, 1]")
    if gross is None:
        gross = quantity * execution_price
    if gross < 0:
        raise ValueError("notional must be nonnegative")

    mode = str((fee_model or {}).get("mode") or "unknown")
    rate = _finite((fee_model or {}).get("rate"))
    if mode in {"fee_free"}:
        raw_fee = 0.0
    elif mode == "polymarket_taker_curve":
        if rate is None or not 0.0 <= rate < 0.5:
            raise ValueError("invalid taker fee rate")
        raw_fee = quantity * rate * execution_price * (1.0 - execution_price)
    elif mode == "legacy_flat_rate_fallback":
        if rate is None or not 0.0 <= rate < 0.5:
            raise ValueError("invalid fallback fee rate")
        raw_fee = gross * rate
    else:
        return {
            "known": False,
            "fee": None,
            "raw_fee": None,
            "mode": mode,
            "source": (fee_model or {}).get("source"),
            "execution": "none",
            "research_only": True,
        }

    rounded = round(raw_fee, 5)
    if raw_fee > 0.0 and rounded < 0.00001:
        rounded = 0.00001
    return {
        "known": True,
        "fee": round(rounded, 5),
        "raw_fee": round(raw_fee, 10),
        "mode": mode,
        "rate": rate,
        "source": (fee_model or {}).get("source"),
        "price": execution_price,
        "shares": quantity,
        "notional": gross,
        "execution": "none",
        "research_only": True,
    }


def conservative_fee_fraction_of_notional(
    fee_model: dict[str, Any],
    *,
    lowest_price: float,
) -> float | None:
    """Return a cash-reserve fraction; market curve is highest at lowest price."""
    price = _finite(lowest_price)
    if price is None or not 0.0 < price <= 1.0:
        return None
    mode = str((fee_model or {}).get("mode") or "")
    rate = _finite((fee_model or {}).get("rate"))
    if mode == "fee_free":
        return 0.0
    if mode == "polymarket_taker_curve" and rate is not None and 0.0 <= rate < 0.5:
        # Fee / notional = rate * (1 - p); evaluate at lowest expected fill price.
        return rate * (1.0 - price)
    if mode == "legacy_flat_rate_fallback" and rate is not None and 0.0 <= rate < 0.5:
        return rate
    return None
