"""Fail-closed readiness report for a future live execution path.

This repository deliberately has no live order adapter. Readiness checks are
informational only and cannot authorize, sign, or submit an order. In
particular, environment variables cannot switch this module into live mode.
"""
from __future__ import annotations

from typing import Any


LIVE_ORDER_SUBMISSION_IMPLEMENTED = True

# Polymarket Terms of Use effective 2026-08-11 state the Site and Features
# are intended only for users aged 18 or older. This is a documented readiness
# condition, never a switch that authorizes execution.
POLYMARKET_MINIMUM_USER_AGE_YEARS = 18
POLYMARKET_TERMS_EFFECTIVE_DATE = "2026-08-11"
POLYMARKET_TERMS_URL = "https://polymarket.com/es/tos"


def execution_readiness_payload() -> dict[str, Any]:
    """Return an explicit, fail-closed report for the current execution mode."""
    blockers = [
        {
            "code": "live_execution_disabled_by_default",
            "detail": "A BUY-only FAK adapter using the official polymarket-client SDK exists, but LIVE_TRADING_ENABLED and LIVE_TRADING_ARMED default to false; DRY_RUN defaults to true. A separate operator-controlled release is required.",
        },
        {
            "code": "live_release_gates_not_verified",
            "detail": "The six independent live release gates must all be evidenced by the live risk context for every submission; the repository does not set them to true by default.",
        },
        {
            "code": "resting_order_execution_not_supported",
            "detail": "The adapter exposes only immediate FAK orders and deliberately does not support GTC/resting orders, batch submission, or automated cancel workflows. Ambiguous post outcomes remain blocked for reconciliation and are never retried.",
        },
        {
            "code": "account_eligibility_not_verified",
            "detail": "Polymarket Terms of Use effective 2026-08-11 state that users must be 18 or older. The operator has self-reported age 41, but account-level identity and eligibility still require verification.",
        },
        {
            "code": "jurisdiction_and_legal_review_not_verified",
            "detail": "Local legal review and account-specific geographic eligibility have not been recorded.",
        },
        {
            "code": "strategy_edge_not_established",
            "detail": "Research and paper results do not establish a robust, net-of-cost predictive edge.",
        },
        {
            "code": "production_operations_not_verified",
            "detail": "A production deployment, monitoring, incident response, and recovery process have not been verified.",
        },
    ]
    return {
        "mode": "paper_only",
        "status": "blocked",
        "live_order_submission_implemented": LIVE_ORDER_SUBMISSION_IMPLEMENTED,
        "platform_eligibility_policy": {
            "minimum_user_age_years": POLYMARKET_MINIMUM_USER_AGE_YEARS,
            "terms_effective_date": POLYMARKET_TERMS_EFFECTIVE_DATE,
            "source_url": POLYMARKET_TERMS_URL,
            "independent_verification_required": True,
        },
        "live_execution_enabled": False,
        "order_submission_allowed": False,
        "order_signing_enabled": False,
        "kill_switch": "engaged",
        "blockers": blockers,
        "requirements_before_any_separate_live_launch_review": [
            "Independently verify the operator is at least 18 years old under the current Polymarket Terms of Use (effective 2026-08-11) and is otherwise eligible; do not rely on self-reported runtime flags.",
            "Obtain qualified review of applicable local law and confirm current platform geographic eligibility.",
            "Validate a strategy on sufficiently large, chronological out-of-sample data after realistic fees, spread, slippage, and partial fills.",
            "Complete independent security review of wallet custody, key isolation, authentication, and secrets handling.",
            "Define hard position, exposure, daily-loss, drawdown, stale-data, and emergency-stop limits.",
            "Keep the first live iteration restricted to bounded FAK orders; do not add resting GTC orders until watchdog/cancel/reconciliation semantics are independently validated.",
            "Verify cloud uptime, persistent state, alerting, backups, and incident recovery.",
            "Require a separate, explicit release decision after all gates are evidenced.",
        ],
        "research_only": True,
        "execution": "none",
    }
