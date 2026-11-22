"""A strict read-only facade over an already-authenticated Polymarket Perps session.

The SDK PerpsSession also exposes order submission, cancellation, Auto-Cancel and
account-configuration mutation methods. Never pass that raw object to preflight,
reconciliation or strategy code. This facade intentionally exposes only account
read methods and an explicit close operation.

Opening a session requires pre-existing delegated Perps credentials. This module
never opens a session with credentials=None, because the SDK then creates a new
delegated key and signs an owner authorization.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable


READ_ONLY_PERPS_METHODS = frozenset({
    "fetch_balances",
    "fetch_portfolio",
    "fetch_auto_cancel_status",
    "fetch_account_config",
    "fetch_open_orders",
    "fetch_orders",
    "list_fills",
})


class ReadOnlyPerpsSession:
    """Allowlisted read-only view; the underlying SDK session is never returned."""

    __slots__ = ("__session", "__closed")

    def __init__(self, session: Any) -> None:
        missing = [
            name for name in READ_ONLY_PERPS_METHODS | {"close"}
            if not callable(getattr(session, name, None))
        ]
        if missing:
            raise ValueError(
                "authenticated Perps session is missing required read/close methods: "
                + ",".join(sorted(missing))
            )
        self.__session = session
        self.__closed = False

    def __repr__(self) -> str:
        return "ReadOnlyPerpsSession(methods=allowlisted, execution=none)"

    def _require_open(self) -> Any:
        if self.__closed:
            raise RuntimeError("read-only Perps session is closed")
        return self.__session

    async def fetch_balances(self) -> Any:
        return await self._require_open().fetch_balances()

    async def fetch_portfolio(self) -> Any:
        return await self._require_open().fetch_portfolio()

    async def fetch_auto_cancel_status(self) -> Any:
        return await self._require_open().fetch_auto_cancel_status()

    async def fetch_account_config(self, *, instrument_id: int | None = None) -> Any:
        return await self._require_open().fetch_account_config(
            instrument_id=instrument_id
        )

    async def fetch_open_orders(self, *, instrument_id: int | None = None) -> Any:
        return await self._require_open().fetch_open_orders(
            instrument_id=instrument_id
        )

    async def fetch_orders(self, *, client_order_id: str) -> Any:
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id must be non-empty text")
        return await self._require_open().fetch_orders(
            client_order_id=client_order_id
        )

    def list_fills(
        self,
        *,
        instrument_id: int,
        start: Any,
        end: Any,
        sort: str = "asc",
    ) -> Any:
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id <= 0:
            raise ValueError("instrument_id must be a positive integer")
        if sort not in {"asc", "desc"}:
            raise ValueError("fill sort must be asc or desc")
        # SDK paginator is intentionally synchronous to construct and async to
        # iterate. Do not await it or expose any additional session methods.
        return self._require_open().list_fills(
            instrument_id=instrument_id,
            start=start,
            end=end,
            sort=sort,
        )

    async def close(self) -> None:
        if self.__closed:
            return
        self.__closed = True
        await self.__session.close()

    async def __aenter__(self) -> "ReadOnlyPerpsSession":
        self._require_open()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()


@asynccontextmanager
async def open_existing_perps_read_only_session(
    secure_client: Any,
    *,
    credentials: Any,
) -> AsyncIterator[ReadOnlyPerpsSession]:
    """Resume pre-existing delegated credentials and expose reads only.

    credentials=None is rejected before the SDK is called. The adapter does not
    generate replacement credentials, sign an owner message, or enable writes.
    The caller owns the already-authenticated secure client; this context owns
    and closes only the Perps session it opens.
    """
    if credentials is None:
        raise ValueError("existing delegated Perps credentials are required")
    opener: Callable[..., Any] | None = getattr(secure_client, "open_perps_session", None)
    if not callable(opener):
        raise ValueError("secure_client must expose open_perps_session")
    raw_session = await opener(credentials=credentials)
    try:
        readonly = ReadOnlyPerpsSession(raw_session)
    except BaseException:
        close = getattr(raw_session, "close", None)
        if callable(close):
            await close()
        raise
    try:
        yield readonly
    finally:
        await readonly.close()


__all__ = [
    "READ_ONLY_PERPS_METHODS",
    "ReadOnlyPerpsSession",
    "open_existing_perps_read_only_session",
]
