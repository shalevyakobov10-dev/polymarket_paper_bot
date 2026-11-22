"""Hard release gate for future Polymarket Perps live execution.

This module performs local configuration checks only. It does not import the
Polymarket SDK, create a client/session, read credentials, access the network,
sign, or submit/cancel orders. Every release condition is deliberately
code-reviewed and hard-closed until its evidence is independently verified.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any


CONFIRMATION_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"

# These are source-controlled release conditions, not environment overrides.
# Changing one to True requires independent evidence and an explicit code review.
RELEASE_GATES: tuple[tuple[str, bool], ...] = (
    ("strategy_out_of_sample_validation", False),
    ("historical_stress_and_failure_testing", False),
    ("perps_auto_cancel_supervisor_verified", False),
    ("live_account_integration_verified", False),
    ("geoblock_and_jurisdiction_review", False),
    ("managed_secrets_and_deployment_review", False),
    ("independent_risk_review_and_recovery_drill", False),
)


def _parse_bool(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> tuple[bool, bool]:
    raw = environment.get(name)
    if raw is None:
        return default, True
    if not isinstance(raw, str):
        return default, False
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True, True
    if value in {"0", "false", "no", "off"}:
        return False, True
    return default, False


def evaluate_perps_live_release_gate(
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return an auditable, fail-closed report; never enables an order itself."""
    env = os.environ if environment is None else environment
    dry_run, dry_run_valid = _parse_bool(env, "DRY_RUN", default=True)
    enabled, enabled_valid = _parse_bool(
        env, "PERPS_LIVE_EXECUTION_ENABLED", default=False
    )
    armed, armed_valid = _parse_bool(
        env, "PERPS_LIVE_TRADING_ARMED", default=False
    )

    blocked: list[str] = []
    for name, passed in RELEASE_GATES:
        if passed is not True:
            blocked.append(f"release_gate:{name}")

    if not dry_run_valid:
        blocked.append("invalid_runtime_flag:DRY_RUN")
    elif dry_run:
        blocked.append("dry_run_must_be_explicitly_disabled")
    if not enabled_valid:
        blocked.append("invalid_runtime_flag:PERPS_LIVE_EXECUTION_ENABLED")
    elif enabled is not True:
        blocked.append("perps_live_execution_disabled")
    if not armed_valid:
        blocked.append("invalid_runtime_flag:PERPS_LIVE_TRADING_ARMED")
    elif armed is not True:
        blocked.append("perps_live_trading_not_armed")
    if env.get("PERPS_LIVE_CONFIRMATION") != CONFIRMATION_PHRASE:
        blocked.append("perps_live_confirmation_missing_or_invalid")

    unique = list(dict.fromkeys(blocked))
    all_release_gates_passed = all(passed is True for _, passed in RELEASE_GATES)
    runtime_flags_passed = (
        dry_run_valid and not dry_run
        and enabled_valid and enabled
        and armed_valid and armed
        and env.get("PERPS_LIVE_CONFIRMATION") == CONFIRMATION_PHRASE
    )
    # This remains False while any source-controlled gate is False. This module
    # never performs execution; it only reports whether a separately reviewed
    # gateway could even be considered for release.
    return {
        "schema_version": 1,
        "mode": "PERPS_LIVE_RELEASE_GATE_ONLY",
        "release_gates": {
            name: {"passed": passed, "source_controlled": True}
            for name, passed in RELEASE_GATES
        },
        "all_release_gates_passed": all_release_gates_passed,
        "runtime_flags_passed": runtime_flags_passed,
        "blocked_reasons": unique,
        "eligible_for_separate_gateway_review": (
            all_release_gates_passed and runtime_flags_passed
        ),
        "order_submission_allowed": False,
        "live_execution_enabled": False,
        "execution": "none",
    }


def main() -> int:
    report = evaluate_perps_live_release_gate()
    print(json.dumps(report, sort_keys=True))
    # The gate being closed is the expected state. The command succeeds when
    # the safety report is generated; it never makes live trading eligible.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
