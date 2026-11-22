"""Offline compatibility contract for the pinned official Polymarket Perps SDK.

This module imports public SDK types only. It does not create clients, load
credentials, open a session, make network requests, sign, or submit orders.
"""

from __future__ import annotations

import json
import sys
from importlib import metadata
from inspect import signature
from typing import Any

EXPECTED_DISTRIBUTION = "polymarket-client"
EXPECTED_VERSION = "0.20.1"

ASYNC_SECURE_METHODS: dict[str, set[str]] = {
    "open_perps_session": {"credentials", "expires_in"},
}

PERPS_SESSION_METHODS: dict[str, set[str]] = {
    "fetch_balances": set(),
    "fetch_portfolio": set(),
    "fetch_open_orders": {"instrument_id"},
    "fetch_orders": {"client_order_id"},
    "list_fills": {"instrument_id", "start", "end", "sort"},
    "list_funding_payments": {"instrument_id", "start", "end"},
    "place_order": {
        "instrument_id",
        "side",
        "quantity",
        "time_in_force",
        "reduce_only",
        "client_order_id",
    },
    "post_orders": {"orders"},
    "cancel_order": {"order_id", "client_order_id"},
    "arm_auto_cancel": set(),
    "disarm_auto_cancel": set(),
    "update_leverage": {"instrument_id", "leverage", "cross_margin"},
    "update_margin": {"instrument_id", "amount"},
}


def _check_methods(
    owner: type[Any],
    expected: dict[str, set[str]],
) -> dict[str, list[str]]:
    failures: dict[str, list[str]] = {}
    for name, required_parameters in expected.items():
        method = getattr(owner, name, None)
        if not callable(method):
            failures[name] = ["method_missing"]
            continue
        try:
            parameters = set(signature(method).parameters)
        except (TypeError, ValueError):
            failures[name] = ["signature_unavailable"]
            continue
        missing = sorted(required_parameters - parameters)
        if missing:
            failures[name] = missing
    return failures


def evaluate_sdk_contract(
    *,
    installed_version: str,
    async_secure_client: type[Any],
    perps_session: type[Any],
) -> dict[str, Any]:
    """Evaluate a supplied SDK surface; safe to call with test doubles."""
    version_matches = installed_version == EXPECTED_VERSION
    async_failures = _check_methods(async_secure_client, ASYNC_SECURE_METHODS)
    session_failures = _check_methods(perps_session, PERPS_SESSION_METHODS)
    compatible = version_matches and not async_failures and not session_failures
    return {
        "schema_version": 1,
        "distribution": EXPECTED_DISTRIBUTION,
        "expected_version": EXPECTED_VERSION,
        "installed_version": installed_version,
        "version_matches": version_matches,
        "compatible": compatible,
        "async_secure_method_failures": async_failures,
        "perps_session_method_failures": session_failures,
        "perps_api_experimental": True,
        "account_adapter_mode": "injected_read_only_session",
        "client_created": False,
        "credentials_loaded": False,
        "network_requests_made": False,
        "signing_performed": False,
        "order_submission_allowed": False,
        "execution": "none",
    }


def inspect_installed_sdk() -> dict[str, Any]:
    try:
        installed_version = metadata.version(EXPECTED_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return {
            "schema_version": 1,
            "distribution": EXPECTED_DISTRIBUTION,
            "expected_version": EXPECTED_VERSION,
            "installed_version": None,
            "version_matches": False,
            "compatible": False,
            "error": "sdk_distribution_not_installed",
            "client_created": False,
            "credentials_loaded": False,
            "network_requests_made": False,
            "signing_performed": False,
            "order_submission_allowed": False,
            "execution": "none",
        }

    try:
        from polymarket import AsyncSecureClient
        from polymarket.perps import PerpsSession
    except Exception as error:
        return {
            "schema_version": 1,
            "distribution": EXPECTED_DISTRIBUTION,
            "expected_version": EXPECTED_VERSION,
            "installed_version": installed_version,
            "version_matches": installed_version == EXPECTED_VERSION,
            "compatible": False,
            "error": f"sdk_public_import_failed:{type(error).__name__}",
            "client_created": False,
            "credentials_loaded": False,
            "network_requests_made": False,
            "signing_performed": False,
            "order_submission_allowed": False,
            "execution": "none",
        }

    return evaluate_sdk_contract(
        installed_version=installed_version,
        async_secure_client=AsyncSecureClient,
        perps_session=PerpsSession,
    )


def main() -> int:
    report = inspect_installed_sdk()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("compatible") is True else 1


if __name__ == "__main__":
    sys.exit(main())
