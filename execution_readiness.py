"""Fail-closed operator readiness inventory for future live execution.

Narrow gateway adapters exist in this repository, but this module is only a
read-only status report. It cannot arm, sign, or submit orders. It deliberately
distinguishes adapter code from a connected, independently reviewed production
execution service.
"""
from __future__ import annotations

from typing import Any


LIVE_ORDER_SUBMISSION_IMPLEMENTED = True
POLYMARKET_MINIMUM_USER_AGE_YEARS = 18
POLYMARKET_TERMS_EFFECTIVE_DATE = "2026-08-11"
POLYMARKET_TERMS_URL = "https://polymarket.com/tos"
POLYMARKET_GEO_BLOCK_URL = "https://polymarket.com/api/geoblock"


def execution_readiness_payload() -> dict[str, Any]:
    """Return a sanitized inventory; never grants execution permission."""
    blockers = [
        {
            "code": "live_execution_disabled_by_default",
            "detail": (
                "Bounded event-market BUY/FAK and Perps IOC/FOK limit gateway "
                "code exists, but neither gateway is enabled by this readiness "
                "report. Runtime arming, credentials, and source-controlled "
                "release conditions are not satisfied here."
            ),
        },
        {
            "code": "event_source_release_gates_closed",
            "detail": (
                "All eight source-controlled event-market live release gates "
                "remain closed; caller-supplied risk-context flags cannot "
                "override this independent gateway check."
            ),
        },
        {
            "code": "event_order_management_not_integrated",
            "detail": (
                "A cancel-only adapter supports fresh open-order reads, "
                "single-order cancellation, cancel-all, read-back verification, "
                "and a shared lock with BUY submission. It is disabled by "
                "default and is not wired to an authenticated operator service."
            ),
        },
        {
            "code": "event_gateway_not_integrated",
            "detail": (
                "The event-market gateway is not connected to the Paper signal "
                "worker or an operator-facing execution service. It currently "
                "supports BUY FAK only; SELL, GTC, batch, and automated cancel "
                "operations are not part of this adapter."
            ),
        },
        {
            "code": "perps_gateway_not_integrated",
            "detail": (
                "The Perps IOC/FOK limit gateway is not connected to the Perps "
                "strategy worker or an operator-facing execution service. "
                "The gateway has no independent per-order geoblock check and repeats it immediately before durable submission intent."
            ),
        },
        {
            "code": "perps_source_release_gates_closed",
            "detail": (
                "All eight source-controlled Perps release gates remain "
                "closed; environment flags and confirmation phrases cannot "
                "override them."
            ),
        },
        {
            "code": "shared_paper_admission_not_integrated",
            "detail": (
                "The durable admission coordinator can verify exact ledger "
                "rows, but neither Paper engine calls it yet. Shared daily-loss "
                "and combined drawdown enforcement are also not implemented."
            ),
        },
        {
            "code": "account_eligibility_not_verified",
            "detail": (
                "Current Polymarket Terms require users to be at least 18. "
                "Account identity, age, sanctions, and platform eligibility "
                "must be independently verified by the account holder."
            ),
        },
        {
            "code": "jurisdiction_and_runtime_geoblock_not_verified",
            "detail": (
                "Local legal review has not been recorded. The live event "
                "gateway checks Polymarket geoblock on its order path, but this "
                "readiness endpoint does not call it; the Perps gateway now performs a fresh "
                "independent per-order geoblock check."
            ),
        },
        {
            "code": "strategy_edge_not_established",
            "detail": (
                "No robust, sufficiently large, chronological out-of-sample "
                "edge net of real fees, funding, spread, slippage, and partial "
                "fills has been established."
            ),
        },
        {
            "code": "account_credential_isolation_not_verified",
            "detail": (
                "The current read-only account observer is application-"
                "allowlisted, not venue-enforced read-only. Its delegated "
                "credentials may sign writes, and same-UID isolation in the "
                "shared app container is not verified."
            ),
        },
        {
            "code": "production_operations_not_verified",
            "detail": (
                "No production deployment, isolated account service, external "
                "alerting, off-host encrypted backup, incident response, or "
                "independent recovery drill has been verified."
            ),
        },
    ]
    return {
        "schema_version": 2,
        "mode": "paper_only",
        "status": "blocked",
        # True means bounded adapter source code exists, not that it is armed.
        "live_order_submission_implemented": LIVE_ORDER_SUBMISSION_IMPLEMENTED,
        "live_execution_enabled": False,
        "live_order_submission_enabled": False,
        "order_submission_allowed": False,
        "order_signing_enabled": False,
        "kill_switch": "engaged",
        "gateway_inventory": {
            "event_contracts": {
                "adapter_code_present": True,
                "integrated_into_paper_worker": False,
                "source_controlled_release_gates_passed": False,
                "supported_side": "BUY",
                "supported_order_type": "FAK",
                "per_order_geoblock_check": True,
                "supports_sell": False,
                "supports_resting_orders": False,
                "supports_batch_orders": False,
                "supports_automated_cancel": False,
                "runtime_enabled": False,
                "order_submission_allowed": False,
            },
            "perps": {
                "adapter_code_present": True,
                "integrated_into_strategy_worker": False,
                "supported_order_type": "LIMIT",
                "supported_time_in_force": ["IOC", "FOK"],
                "per_order_geoblock_check": True,
                "source_controlled_release_gates_passed": False,
                "runtime_enabled": False,
                "order_submission_allowed": False,
            },
        },
        "event_order_management": {
            "adapter_code_present": True,
            "supports_open_order_reads": True,
            "supports_cancel_one": True,
            "supports_cancel_all": True,
            "readback_verification": True,
            "shared_submission_cancel_lock": True,
            "requires_explicit_confirmation": True,
            "runtime_enabled": False,
            "integrated_into_operator_service": False,
            "new_order_submission_allowed": False,
        },
        "shared_paper_risk": {
            "coordinator_code_present": True,
            "ledger_entry_verification_present": True,
            "wired_into_event_engine": False,
            "wired_into_perps_engine": False,
            "combined_daily_loss_enforced": False,
            "combined_drawdown_enforced": False,
            "order_admission_allowed": False,
        },
        "platform_eligibility_policy": {
            "minimum_user_age_years": POLYMARKET_MINIMUM_USER_AGE_YEARS,
            "terms_effective_date": POLYMARKET_TERMS_EFFECTIVE_DATE,
            "terms_url": POLYMARKET_TERMS_URL,
            "geoblock_endpoint": POLYMARKET_GEO_BLOCK_URL,
            "runtime_geoblock_checked_by_this_endpoint": False,
            "independent_account_verification_required": True,
        },
        "blockers": blockers,
        "requirements_before_any_separate_live_launch_review": [
            "Independently verify account-holder age (18+), identity, sanctions status, and account eligibility under current platform terms.",
            "Obtain qualified local legal review and confirm the current platform geoblock from the actual execution host; never use VPNs or other bypasses.",
            "Connect each gateway only through a reviewed, isolated execution service with per-order eligibility checks and strict credential boundaries.",
            "Wire both Paper engines through one shared admission coordinator, atomic reservations, and a startup recovery lock.",
            "Persist a combined portfolio ledger with immutable starting capital, UTC daily-loss baselines, and a combined high-water mark.",
            "Complete chronological out-of-sample, walk-forward, stress, fault-injection, fee/funding, and execution-cost validation.",
            "Complete independent wallet/key custody, application, dependency, infrastructure, and threat-model review.",
            "Verify position sizing, exposure, leverage, margin, liquidation, stale-data, kill-switch, cancel, and reconciliation controls.",
            "Verify isolated production deployment, external monitoring and alerts, encrypted off-host backups, restore rehearsal, and incident runbooks.",
            "Require an independent release review and a separate explicit operator decision; no environment variable alone may satisfy the release gates.",
        ],
        "research_only": True,
        "execution": "none",
    }
