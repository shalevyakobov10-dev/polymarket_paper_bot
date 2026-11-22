from __future__ import annotations

import unittest

from perps_sdk_contract import EXPECTED_VERSION, evaluate_sdk_contract


class CompatibleSecureClient:
    @classmethod
    def _create(
        cls, *, private_key, wallet, credentials, validate_credentials=True
    ):
        raise AssertionError("contract inspection must not call SDK methods")

    def open_perps_session(self, *, credentials=None, expires_in=None):
        raise AssertionError("contract inspection must not call SDK methods")


class CompatibleApiKeyCreds:
    model_fields = {"key": object(), "secret": object(), "passphrase": object()}

    @classmethod
    def model_validate(cls, value):
        raise AssertionError("contract inspection must not validate model instances")


class CompatiblePerpsCredentials:
    model_fields = {
        "proxy": object(), "private_key": object(), "secret": object(),
        "expires_at": object(),
    }

    @classmethod
    def model_validate(cls, value):
        raise AssertionError("contract inspection must not validate model instances")


class CompatiblePerpsSession:
    def fetch_balances(self): pass
    def fetch_portfolio(self): pass
    def fetch_auto_cancel_status(self): pass
    def fetch_account_config(self, *, instrument_id=None): pass
    def fetch_open_orders(self, *, instrument_id=None): pass
    def fetch_orders(self, *, client_order_id=None): pass
    def list_fills(self, *, instrument_id=None, start=None, end=None, sort=None): pass
    def list_funding_payments(self, *, instrument_id=None, start=None, end=None): pass
    async def close(self): pass
    def place_order(
        self, *, instrument_id, side, quantity, time_in_force, reduce_only=False,
        client_order_id=None,
    ): pass
    def post_orders(self, orders, *, expires_at=None): pass
    def cancel_order(
        self, *, order_id=None, client_order_id=None, expires_at=None, retry=None
    ): pass
    def cancel_all_orders(self, *, instrument_id=None, expires_at=None): pass
    def arm_auto_cancel(self, *, cancel_at, expires_at=None): pass
    def disarm_auto_cancel(self, *, expires_at=None): pass
    def update_leverage(self, *, instrument_id, leverage, cross_margin): pass
    def update_margin(self, *, instrument_id, amount): pass


class IncompatiblePerpsSession:
    def fetch_balances(self): pass


class PerpsSdkContractTests(unittest.TestCase):
    def test_pinned_public_perps_surface_passes_without_calling_methods(self):
        report = evaluate_sdk_contract(
            installed_version=EXPECTED_VERSION,
            async_secure_client=CompatibleSecureClient,
            perps_session=CompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=CompatiblePerpsCredentials,
        )
        self.assertTrue(report["compatible"])
        self.assertTrue(report["perps_api_experimental"])
        self.assertFalse(report["client_created"])
        self.assertFalse(report["credentials_loaded"])
        self.assertFalse(report["network_requests_made"])
        self.assertFalse(report["signing_performed"])
        self.assertFalse(report["order_submission_allowed"])
        self.assertEqual(report["execution"], "none")

    def test_unexpected_version_fails_closed_even_when_methods_match(self):
        report = evaluate_sdk_contract(
            installed_version="0.20.0",
            async_secure_client=CompatibleSecureClient,
            perps_session=CompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=CompatiblePerpsCredentials,
        )
        self.assertFalse(report["compatible"])
        self.assertFalse(report["version_matches"])

    def test_missing_sdk_methods_fail_closed_with_method_names(self):
        report = evaluate_sdk_contract(
            installed_version=EXPECTED_VERSION,
            async_secure_client=CompatibleSecureClient,
            perps_session=IncompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=CompatiblePerpsCredentials,
        )
        self.assertFalse(report["compatible"])
        self.assertIn("fetch_portfolio", report["perps_session_method_failures"])
        self.assertIn("place_order", report["perps_session_method_failures"])
        self.assertIn("close", report["perps_session_method_failures"])

    def test_missing_private_client_construction_hook_fails_closed(self):
        class SecureClientWithoutExistingCredentialConstruction:
            def open_perps_session(self, *, credentials=None, expires_in=None):
                pass

        report = evaluate_sdk_contract(
            installed_version=EXPECTED_VERSION,
            async_secure_client=SecureClientWithoutExistingCredentialConstruction,
            perps_session=CompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=CompatiblePerpsCredentials,
        )
        self.assertFalse(report["compatible"])
        self.assertIn(
            "_create",
            report["async_secure_method_failures"],
        )

    def test_changed_credential_model_schema_fails_closed(self):
        class IncompatiblePerpsCredentials:
            model_fields = {"proxy": object(), "secret": object()}
            @classmethod
            def model_validate(cls, value):
                raise AssertionError("contract inspection must not validate credentials")

        report = evaluate_sdk_contract(
            installed_version=EXPECTED_VERSION,
            async_secure_client=CompatibleSecureClient,
            perps_session=CompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=IncompatiblePerpsCredentials,
        )
        self.assertFalse(report["compatible"])
        self.assertEqual(
            report["credential_model_failures"]["PerpsCredentials"],
            ["expires_at", "private_key"],
        )

    def test_changed_parameter_surface_fails_closed(self):
        class IncompatibleSecureClient:
            def open_perps_session(self, *, label=None):
                pass

        report = evaluate_sdk_contract(
            installed_version=EXPECTED_VERSION,
            async_secure_client=IncompatibleSecureClient,
            perps_session=CompatiblePerpsSession,
            api_key_credentials=CompatibleApiKeyCreds,
            perps_credentials=CompatiblePerpsCredentials,
        )
        self.assertFalse(report["compatible"])
        self.assertEqual(
            report["async_secure_method_failures"]["open_perps_session"],
            ["credentials", "expires_in"],
        )


if __name__ == "__main__":
    unittest.main()
