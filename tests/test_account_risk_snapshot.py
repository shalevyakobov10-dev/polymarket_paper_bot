import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace

from account_risk_snapshot import (
    AccountRiskSnapshotIncompleteError,
    collect_account_risk_snapshot,
)


class FakePaginator:
    def __init__(self, pages=()):
        self.pages = list(pages)

    def __aiter__(self):
        async def iterate():
            for page in self.pages:
                yield page
        return iterate()


def page(items=(), *, limit_reached=False):
    return SimpleNamespace(items=tuple(items), limit_reached=limit_reached)


def position(
    *,
    condition_id="condition-1",
    asset_id="token-yes",
    size="10",
    value="7.5",
    cost="5.0",
    unrealized="2.5",
    status="OPEN",
):
    return SimpleNamespace(
        condition_id=condition_id,
        asset_id=asset_id,
        current_size=Decimal(size),
        current_value=Decimal(value),
        total_cost_usdc=Decimal(cost),
        unrealized_pnl=Decimal(unrealized),
        status=status,
    )


def order(
    *,
    order_id="order-1",
    condition_id="condition-1",
    asset_id="token-yes",
    side="BUY",
    original="10",
    matched="2",
    price="0.50",
):
    return SimpleNamespace(
        id=order_id,
        market=condition_id,
        asset_id=asset_id,
        side=side,
        original_size=Decimal(original),
        size_matched=Decimal(matched),
        price=Decimal(price),
    )


class FakeAccountClient:
    def __init__(
        self,
        *,
        balance=100_000_000,
        positions_by_status=None,
        open_order_pages=(),
    ):
        self.balance = balance
        self.positions_by_status = positions_by_status or {}
        self.open_order_pages = list(open_order_pages)
        self.balance_calls = []

    async def get_balance_allowance(self, *, asset_type):
        self.balance_calls.append(asset_type)
        return SimpleNamespace(balance=self.balance)

    def list_positions(self, *, status, full_history):
        if full_history is not True:
            raise AssertionError("snapshot must request full position history")
        return FakePaginator(self.positions_by_status.get(status, ()))

    def list_open_orders(self):
        return FakePaginator(self.open_order_pages)


class AccountRiskSnapshotTests(unittest.TestCase):
    def test_converts_collateral_and_conservatively_reserves_open_buy_orders(self):
        client = FakeAccountClient(
            balance=100_000_000,
            positions_by_status={
                "OPEN": [page([position(value="7.5", cost="5", unrealized="2.5")])],
                "REDEEMABLE": [page([
                    position(
                        condition_id="condition-2",
                        asset_id="token-no",
                        size="4",
                        value="2",
                        cost="3",
                        unrealized="-1",
                        status="REDEEMABLE",
                    )
                ])],
            },
        )
        open_orders = [
            order(order_id="buy-1", original="10", matched="2", price="0.5"),
            order(
                order_id="sell-1",
                condition_id="condition-2",
                asset_id="token-no",
                side="SELL",
                original="4",
                matched="1",
                price="0.7",
            ),
        ]
        result = asyncio.run(
            collect_account_risk_snapshot(client, open_orders=open_orders)
        )
        self.assertEqual(client.balance_calls, ["COLLATERAL"])
        self.assertEqual(result["collateral_balance_base_units"], "100000000")
        self.assertEqual(result["collateral_balance_usd"], "100")
        self.assertEqual(result["open_buy_commitment_usd"], "4.0")
        self.assertEqual(result["conservative_available_collateral_usd"], "96.0")
        self.assertEqual(result["current_open_exposure_usd"], "13.5")
        self.assertEqual(result["open_position_value_usd"], "9.5")
        self.assertEqual(result["open_position_cost_usd"], "8.0")
        self.assertEqual(result["open_unrealized_pnl_usd"], "1.5")
        self.assertEqual(result["current_open_positions"], 2)
        self.assertEqual(result["current_open_orders"], 2)
        self.assertEqual(result["open_sell_remaining_shares"], "3")
        self.assertEqual(
            result["open_position_assets_by_condition"],
            {
                "condition-1": ["token-yes"],
                "condition-2": ["token-no"],
            },
        )
        self.assertEqual(result["execution"], "none")

    def test_fetches_all_open_order_pages_when_not_supplied(self):
        client = FakeAccountClient(
            open_order_pages=[
                page([order(order_id="order-a")]),
                page([order(order_id="order-b", asset_id="token-no")]),
            ]
        )
        result = asyncio.run(collect_account_risk_snapshot(client))
        self.assertEqual(result["current_open_orders"], 2)
        self.assertEqual(result["open_buy_commitment_usd"], "8.0")

    def test_incomplete_open_order_pagination_fails_closed(self):
        client = FakeAccountClient(
            open_order_pages=[page([order()], limit_reached=True)]
        )
        with self.assertRaises(AccountRiskSnapshotIncompleteError):
            asyncio.run(collect_account_risk_snapshot(client))

    def test_incomplete_position_pagination_fails_closed(self):
        client = FakeAccountClient(
            positions_by_status={
                "OPEN": [page([position()], limit_reached=True)],
                "REDEEMABLE": [],
            }
        )
        with self.assertRaises(AccountRiskSnapshotIncompleteError):
            asyncio.run(collect_account_risk_snapshot(client, open_orders=[]))

    def test_invalid_collateral_balance_fails_closed(self):
        for balance in (-1, True, "100000000"):
            with self.subTest(balance=balance):
                client = FakeAccountClient(balance=balance)
                with self.assertRaisesRegex(ValueError, "base units"):
                    asyncio.run(collect_account_risk_snapshot(client, open_orders=[]))

    def test_conflicting_duplicate_position_records_fail_closed(self):
        one = position(value="7")
        other = position(value="8")
        client = FakeAccountClient(
            positions_by_status={
                "OPEN": [page([one]), page([other])],
                "REDEEMABLE": [],
            }
        )
        with self.assertRaises(AccountRiskSnapshotIncompleteError):
            asyncio.run(collect_account_risk_snapshot(client, open_orders=[]))

    def test_invalid_open_order_price_or_size_fails_closed(self):
        invalid = order(original="2", matched="3")
        client = FakeAccountClient()
        with self.assertRaisesRegex(ValueError, "invalid side or size"):
            asyncio.run(collect_account_risk_snapshot(client, open_orders=[invalid]))

    def test_captured_at_must_be_timezone_aware(self):
        client = FakeAccountClient()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            asyncio.run(
                collect_account_risk_snapshot(
                    client,
                    open_orders=[],
                    captured_at=__import__("datetime").datetime(2026, 11, 4),
                )
            )


if __name__ == "__main__":
    unittest.main()
