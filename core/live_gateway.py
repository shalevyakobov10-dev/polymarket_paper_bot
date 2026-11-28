"""Live CLOB adapter that is compiled in and switched off.

``LIVE_ORDER_SUBMISSION_IMPLEMENTED`` in ``live_order_preflight`` is the
repository's release latch. This gateway can build a py-clob-client limit
order, but it returns before any network post while that latch is false.
PAPER_MODE / LIVE_MODE cannot override it.
"""
from __future__ import annotations

from typing import Any, Callable

from config import LIVE_ACK_PHRASE, BotSettings
from live_order_preflight import LIVE_ORDER_SUBMISSION_IMPLEMENTED, evaluate_live_order_preflight

ClientFactory = Callable[[BotSettings], Any]


def submission_allowed() -> bool:
    return bool(LIVE_ORDER_SUBMISSION_IMPLEMENTED)


def _blocked(reason: str, preflight: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "submitted": False,
        "signed": False,
        "execution": "none",
        "reason": reason,
        "order_submission_allowed": False,
        "order_submission_implemented": LIVE_ORDER_SUBMISSION_IMPLEMENTED,
        "preflight": preflight,
    }


class LiveOrderGateway:
    def __init__(self, settings: BotSettings, client_factory: ClientFactory | None = None) -> None:
        self.settings = settings
        self.client_factory = client_factory or build_clob_client
        self.factory_calls = 0

    def execute(
        self,
        *,
        token_id: str,
        limit_price: float,
        size: float,
        preflight_args: dict[str, Any],
    ) -> dict[str, Any]:
        preflight = evaluate_live_order_preflight(**preflight_args)
        if not preflight.get("preflight_passed"):
            return _blocked("live_preflight_blocked", preflight)
        if not submission_allowed():
            return _blocked("live_order_adapter_not_implemented", preflight)
        if self.settings.paper_mode or not self.settings.live_mode:
            return _blocked("live_mode_not_selected", preflight)
        if self.settings.live_trading_ack != LIVE_ACK_PHRASE:
            return _blocked("live_trading_ack_missing", preflight)
        if not self.settings.polymarket_private_key:
            return _blocked("missing_private_key", preflight)
        self.factory_calls += 1
        client = self.client_factory(self.settings)
        response = post_limit_buy(
            client,
            token_id=token_id,
            limit_price=limit_price,
            size=size,
        )
        return {
            "submitted": True,
            "signed": True,
            "execution": "live_clob",
            "reason": "submitted",
            "order_submission_allowed": True,
            "order_submission_implemented": True,
            "preflight": preflight,
            "response": response,
        }


def build_clob_client(settings: BotSettings) -> Any:
    """Construct an authenticated client. Unreachable while the release latch is false."""
    if not submission_allowed():
        raise RuntimeError("live client construction is disabled")
    from py_clob_client.client import ClobClient

    client = ClobClient(
        settings.clob_host,
        key=settings.polymarket_private_key,
        chain_id=settings.chain_id,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def post_limit_buy(client: Any, *, token_id: str, limit_price: float, size: float) -> Any:
    """Post one GTC limit buy. Collateral is Polygon USDC on chain id 137."""
    if not submission_allowed():
        raise RuntimeError("live order posting is disabled")
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    signed = client.create_order(
        OrderArgs(token_id=str(token_id), price=float(limit_price), size=float(size), side=BUY)
    )
    return client.post_order(signed, OrderType.GTC)
