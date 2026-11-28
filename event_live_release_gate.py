"""Source-controlled fail-closed release gate for event-market live execution.

This module is intentionally independent of environment flags and caller-
supplied risk context. A code review with external evidence is required before
any gate can be changed from False.
"""
from __future__ import annotations

from typing import Any


RELEASE_GATES: tuple[tuple[str, bool], ...] = (
    ("strategy_out_of_sample_validation", False),
    ("historical_stress_and_failure_testing", False),
    ("account_eligibility_and_kyc_verified", False),
    ("geoblock_and_local_legal_review", False),
    ("event_order_lifecycle_and_cancel_reconciliation_verified", False),
    ("shared_portfolio_admission_and_kill_switch_verified", False),
    ("managed_secrets_and_deployment_review", False),
    ("independent_security_and_recovery_review", False),
)


def evaluate_event_live_release_gate() -> dict[str, Any]:
    """Return auditable release status; never reads secrets or enables trading."""
    gates = {
        name: {"passed": passed, "source_controlled": True}
        for name, passed in RELEASE_GATES
    }
    all_passed = all(passed is True for _, passed in RELEASE_GATES)
    return {
        "schema_version": 1,
        "mode": "EVENT_MARKET_LIVE_RELEASE_GATE_ONLY",
        "release_gates": gates,
        "all_release_gates_passed": all_passed,
        "eligible_for_separate_gateway_review": all_passed,
        "order_submission_allowed": False,
        "order_signing_allowed": False,
        "live_execution_enabled": False,
        "blocked_reasons": [
            f"release_gate:{name}"
            for name, passed in RELEASE_GATES
            if passed is not True
        ],
        "execution": "none",
    }
