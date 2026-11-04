"""Fail-closed readiness report for a future live execution path.

This repository deliberately has no live order adapter. Readiness checks are
informational only and cannot authorize, sign, or submit an order. In
particular, environment variables cannot switch this module into live mode.
"""
from __future__ import annotations

from typing import Any


LIVE_ORDER_SUBMISSION_IMPLEMENTED = False


def execution_readiness_payload() -> dict[str, Any]:
    """Return an explicit, fail-closed report for the current execution mode."""
    blockers = [
        {
            "code": "live_order_adapter_not_implemented",
            "detail": "A separate authenticated account monitor can read open orders and user events, but there is no reviewed order-placement adapter or order-signing workflow.",
        },
        {
            "code": "account_eligibility_not_verified",
            "detail": "The account operator's age and platform eligibility have not been independently verified.",
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
        "live_execution_enabled": False,
        "order_submission_allowed": False,
        "order_signing_enabled": False,
        "kill_switch": "engaged",
        "blockers": blockers,
        "requirements_before_any_separate_live_launch_review": [
            "Verify the account operator meets Polymarket's current age and account-eligibility requirements.",
            "Obtain qualified review of applicable local law and confirm current platform geographic eligibility.",
            "Validate a strategy on sufficiently large, chronological out-of-sample data after realistic fees, spread, slippage, and partial fills.",
            "Complete independent security review of wallet custody, key isolation, authentication, and secrets handling.",
            "Define hard position, exposure, daily-loss, drawdown, stale-data, and emergency-stop limits.",
            "Verify cloud uptime, persistent state, alerting, backups, and incident recovery.",
            "Require a separate, explicit release decision after all gates are evidenced.",
        ],
        "research_only": True,
        "execution": "none",
    }
