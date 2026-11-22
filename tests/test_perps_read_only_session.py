from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from perps_read_only_session import (
    READ_ONLY_PERPS_METHODS,
    ReadOnlyPerpsSession,
    open_existing_perps_read_only_session,
)
from perps_account_supervisor import read_perps_account_supervisor_status
from perps_account_supervisor_worker import (
    PerpsAccountSecretsError,
    create_secure_client_from_existing_credentials,
    load_perps_account_secrets,
    run_perps_account_supervisor_worker,
)


NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)


class FakeRawPerpsSession:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.write_calls = []

    async def fetch_balances(self):
        self.calls.append(("fetch_balances",))
        return ("balances",)

    async def fetch_portfolio(self):
        self.calls.append(("fetch_portfolio",))
        return "portfolio"

    async def fetch_auto_cancel_status(self):
        self.calls.append(("fetch_auto_cancel_status",))
        return "auto_cancel"

    async def fetch_account_config(self, *, instrument_id=None):
        self.calls.append(("fetch_account_config", instrument_id))
        return ("config",)

    async def fetch_open_orders(self, *, instrument_id=None):
        self.calls.append(("fetch_open_orders", instrument_id))
        return ("orders",)

    async def fetch_orders(self, *, client_order_id):
        self.calls.append(("fetch_orders", client_order_id))
        return ("order",)

    def list_fills(self, *, instrument_id, start, end, sort="asc"):
        result = object()
        self.calls.append(("list_fills", instrument_id, start, end, sort))
        return result

    async def place_order(self, **kwargs):
        self.write_calls.append(("place_order", kwargs))
        raise AssertionError("write method must never be forwarded")

    async def cancel_all_orders(self):
        self.write_calls.append(("cancel_all_orders",))
        raise AssertionError("write method must never be forwarded")

    async def close(self):
        self.closed = True


class FakeSecureClient:
    def __init__(self, session):
        self.session = session
        self.open_calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def open_perps_session(self, *, credentials=None):
        self.open_calls.append({"credentials": credentials})
        return self.session

    async def close(self):
        self.closed = True


class ReadOnlyPerpsSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_allowlisted_read_methods_are_exposed(self):
        raw = FakeRawPerpsSession()
        session = ReadOnlyPerpsSession(raw)

        self.assertEqual(
            {name for name in READ_ONLY_PERPS_METHODS if callable(getattr(session, name, None))},
            set(READ_ONLY_PERPS_METHODS),
        )
        for forbidden in (
            "place_order", "post_orders", "cancel_order", "cancel_all_orders",
            "arm_auto_cancel", "disarm_auto_cancel", "update_leverage",
            "update_margin", "create_chase", "create_twap",
        ):
            self.assertFalse(hasattr(session, forbidden), forbidden)

        self.assertEqual(await session.fetch_balances(), ("balances",))
        self.assertEqual(await session.fetch_portfolio(), "portfolio")
        self.assertEqual(await session.fetch_auto_cancel_status(), "auto_cancel")
        self.assertEqual(
            await session.fetch_account_config(instrument_id=17),
            ("config",),
        )
        self.assertEqual(await session.fetch_open_orders(), ("orders",))
        self.assertEqual(
            await session.fetch_orders(client_order_id="a" * 32),
            ("order",),
        )
        paginator = session.list_fills(
            instrument_id=17, start="start", end="end", sort="asc"
        )
        self.assertIsNotNone(paginator)
        self.assertEqual(raw.write_calls, [])
        self.assertTrue(all(call[0] in READ_ONLY_PERPS_METHODS for call in raw.calls))

        await session.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await session.fetch_portfolio()
        self.assertTrue(raw.closed)

    async def test_context_opens_only_with_existing_credentials_and_closes_session(self):
        raw = FakeRawPerpsSession()
        client = FakeSecureClient(raw)
        existing_credentials = object()

        async with open_existing_perps_read_only_session(
            client, credentials=existing_credentials
        ) as session:
            self.assertIsInstance(session, ReadOnlyPerpsSession)
            self.assertFalse(raw.closed)
            self.assertFalse(hasattr(session, "place_order"))

        self.assertEqual(client.open_calls, [{"credentials": existing_credentials}])
        self.assertTrue(raw.closed)
        self.assertEqual(raw.write_calls, [])

    async def test_missing_credentials_are_rejected_before_sdk_open(self):
        raw = FakeRawPerpsSession()
        client = FakeSecureClient(raw)
        with self.assertRaisesRegex(ValueError, "existing delegated Perps credentials"):
            async with open_existing_perps_read_only_session(client, credentials=None):
                self.fail("session must not open without existing credentials")
        self.assertEqual(client.open_calls, [])
        self.assertFalse(raw.closed)

    async def test_invalid_raw_session_is_closed_before_rethrow(self):
        class IncompleteSession:
            async def close(self):
                self.closed = True
        raw = IncompleteSession()
        raw.closed = False
        client = FakeSecureClient(raw)

        with self.assertRaisesRegex(ValueError, "missing required read/close methods"):
            async with open_existing_perps_read_only_session(client, credentials=object()):
                self.fail("incomplete session must not be yielded")
        self.assertTrue(raw.closed)

    async def test_context_closes_session_when_consumer_raises(self):
        raw = FakeRawPerpsSession()
        client = FakeSecureClient(raw)
        with self.assertRaisesRegex(RuntimeError, "consumer failure"):
            async with open_existing_perps_read_only_session(
                client, credentials=object()
            ):
                raise RuntimeError("consumer failure")
        self.assertTrue(raw.closed)


class PerpsAccountSupervisorWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def _secret_document(self, *, expiry=None):
        return {
            "owner_private_key": "0x" + "1" * 64,
            "deposit_wallet": "0x" + "2" * 40,
            "clob_api_credentials": {
                "key": "clob-key",
                "secret": "clob-secret",
                "passphrase": "clob-passphrase",
            },
            "perps_session_credentials": {
                "proxy": "0x" + "3" * 40,
                "private_key": "0x" + "4" * 64,
                "secret": "perps-secret",
                "expires_at": (expiry or NOW + timedelta(days=1)).isoformat(),
            },
        }

    def _write_secrets(self, payload, *, mode=0o600):
        path = self.directory / "perps-secrets.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(mode)
        return path

    def test_loads_strict_secret_document_from_owner_only_file(self):
        path = self._write_secrets(self._secret_document())
        secrets = load_perps_account_secrets(path)
        self.assertEqual(secrets["deposit_wallet"], "0x" + "2" * 40)
        self.assertEqual(secrets["clob_api_credentials"]["key"], "clob-key")
        self.assertEqual(
            secrets["perps_session_credentials"]["proxy"],
            "0x" + "3" * 40,
        )

    def test_loads_owner_read_only_secret_file(self):
        path = self._write_secrets(self._secret_document(), mode=0o400)
        secrets = load_perps_account_secrets(path)
        self.assertEqual(secrets["deposit_wallet"], "0x" + "2" * 40)

    def test_rejects_group_or_world_accessible_secret_file(self):
        for mode in (0o640, 0o644, 0o660):
            with self.subTest(mode=oct(mode)):
                path = self._write_secrets(self._secret_document(), mode=mode)
                with self.assertRaisesRegex(
                    PerpsAccountSecretsError, "permissions_invalid"
                ):
                    load_perps_account_secrets(path)

    def test_rejects_secret_json_with_duplicate_keys(self):
        path = self.directory / "duplicate-secrets.json"
        path.write_text(
            '{"owner_private_key":"0x' + "1" * 64
            + '","owner_private_key":"0x' + "2" * 64 + '"}',
            encoding="utf-8",
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(PerpsAccountSecretsError, "unavailable_or_invalid"):
            load_perps_account_secrets(path)

    def test_rejects_symlink_secret_file(self):
        path = self._write_secrets(self._secret_document())
        link = self.directory / "secret-link.json"
        link.symlink_to(path)
        with self.assertRaises(PerpsAccountSecretsError):
            load_perps_account_secrets(link)

    def test_rejects_extra_or_missing_secret_fields(self):
        payload = self._secret_document()
        payload["unexpected"] = "no"
        path = self._write_secrets(payload)
        with self.assertRaisesRegex(PerpsAccountSecretsError, "schema_invalid"):
            load_perps_account_secrets(path)

    def test_rejects_executable_secret_file_modes(self):
        path = self._write_secrets(self._secret_document(), mode=0o700)
        with self.assertRaisesRegex(
            PerpsAccountSecretsError, "permissions_invalid"
        ):
            load_perps_account_secrets(path)

    def test_rejects_expired_or_naive_delegated_credentials(self):
        expired = self._secret_document(expiry=NOW - timedelta(seconds=1))
        path = self._write_secrets(expired)
        with self.assertRaisesRegex(
            PerpsAccountSecretsError, "credentials_expired"
        ):
            load_perps_account_secrets(path)

        naive = self._secret_document()
        naive["perps_session_credentials"]["expires_at"] = "2026-11-23T18:00:00"
        path = self._write_secrets(naive)
        with self.assertRaisesRegex(
            PerpsAccountSecretsError, "expiry_invalid"
        ):
            load_perps_account_secrets(path)

    async def test_sdk_client_uses_existing_credentials_without_validation_or_derivation(self):
        calls = []
        class FakeApiKeyCreds:
            def __init__(self, *, key, secret, passphrase):
                self.key, self.secret, self.passphrase = key, secret, passphrase

        class FakePerpsCredentials:
            @classmethod
            def model_validate(cls, payload):
                return {"existing_perps_credentials": payload}

        class FakeSdkClient:
            @classmethod
            async def _create(cls, **kwargs):
                calls.append(kwargs)
                return cls()

            @classmethod
            async def create(cls, **kwargs):
                raise AssertionError("public create could derive new API credentials")

        module = types.ModuleType("polymarket")
        module.ApiKeyCreds = FakeApiKeyCreds
        module.PerpsCredentials = FakePerpsCredentials
        module.AsyncSecureClient = FakeSdkClient
        with patch.dict("sys.modules", {"polymarket": module}):
            client, credentials = await create_secure_client_from_existing_credentials(
                self._secret_document()
            )

        self.assertIsInstance(client, FakeSdkClient)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["private_key"], "0x" + "1" * 64)
        self.assertEqual(calls[0]["wallet"], "0x" + "2" * 40)
        self.assertFalse(calls[0]["validate_credentials"])
        self.assertEqual(calls[0]["credentials"].key, "clob-key")
        self.assertEqual(
            credentials["existing_perps_credentials"]["proxy"],
            "0x" + "3" * 40,
        )

    async def test_worker_runs_real_preflight_behind_read_only_session_and_shared_lock(self):
        class FakeRawAccountSession(FakeRawPerpsSession):
            async def fetch_balances(self):
                return (SimpleNamespace(asset="USDC", balance="100", value="100"),)

            async def fetch_portfolio(self):
                return SimpleNamespace(
                    timestamp=NOW - timedelta(seconds=1),
                    in_liquidation=False,
                    positions=(),
                    margin=SimpleNamespace(
                        total_account_value="100",
                        available_order_margin="100",
                        total_initial_margin="0",
                        total_maintenance_margin="0",
                        total_position_value="0",
                    ),
                    withdrawable="100",
                    fee_tier=0,
                )

            async def fetch_open_orders(self, *, instrument_id=None):
                return ()

            async def fetch_auto_cancel_status(self):
                return SimpleNamespace(
                    deadline=NOW + timedelta(seconds=60),
                    triggered=0,
                    daily_limit=5,
                )

            async def fetch_account_config(self, *, instrument_id=None):
                return ()

            async def fetch_orders(self, *, client_order_id):
                return ()

            def list_fills(self, **kwargs):
                class EmptyAsyncPaginator:
                    def __aiter__(self):
                        async def iterator():
                            if False:
                                yield None
                        return iterator()
                return EmptyAsyncPaginator()

        raw = FakeRawAccountSession()
        client = FakeSecureClient(raw)
        credentials = object()
        database = self.directory / "perps-orders.sqlite3"
        status_path = self.directory / "perps-account-status.json"
        lock_path = self.directory / "perps-submission.lock"

        result = await run_perps_account_supervisor_worker(
            secure_client=client,
            credentials=credentials,
            database_path=database,
            status_path=status_path,
            submission_lock_path=lock_path,
            fee_schedule_provider=lambda: {
                "retrieved_at": NOW - timedelta(seconds=1),
                "fee_schedule": [{
                    "instrument_type": "perpetual",
                    "tiers": [
                        {
                            "min_volume_30d": "0",
                            "taker_fee_rate": "0.0005",
                            "maker_fee_rate": "0.0002",
                        },
                    ],
                }],
            },
            max_cycles=1,
            now_provider=lambda: NOW,
        )

        status = read_perps_account_supervisor_status(status_path)
        self.assertEqual(result, 0)
        self.assertEqual(status["worker_state"], "healthy")
        self.assertTrue(status["read_only_preflight_passed"])
        self.assertTrue(status["recovery_completed"])
        self.assertFalse(status["order_submission_allowed"])
        self.assertFalse(status["live_execution_enabled"])
        self.assertEqual(status["execution"], "none")
        self.assertEqual(client.open_calls, [{"credentials": credentials}])
        self.assertTrue(client.closed)
        self.assertTrue(raw.closed)
        self.assertFalse(lock_path.exists())
        self.assertEqual(raw.write_calls, [])

    async def test_worker_closes_secure_client_when_journal_initialization_fails(self):
        client = FakeSecureClient(FakeRawPerpsSession())
        with self.assertRaises(Exception):
            await run_perps_account_supervisor_worker(
                secure_client=client,
                credentials=object(),
                database_path=self.directory,
                status_path=self.directory / "status.json",
                submission_lock_path=self.directory / "lock",
                max_cycles=1,
            )
        self.assertTrue(client.closed)
        self.assertEqual(client.open_calls, [])

    async def test_worker_rejects_missing_delegated_credentials_before_client_use(self):
        client = FakeSecureClient(FakeRawPerpsSession())
        with self.assertRaisesRegex(ValueError, "existing delegated Perps credentials"):
            await run_perps_account_supervisor_worker(
                secure_client=client,
                credentials=None,
                database_path=self.directory / "db.sqlite3",
                status_path=self.directory / "status.json",
                submission_lock_path=self.directory / "lock",
                max_cycles=1,
            )
        self.assertEqual(client.open_calls, [])
        self.assertFalse(client.closed)


if __name__ == "__main__":
    unittest.main()
