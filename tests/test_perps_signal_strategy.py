from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from perps_signal_strategy import PerpsEmaMomentumStrategy, PerpsStrategyConfig


BASE = datetime(2026, 11, 22, 7, 0, tzinfo=timezone.utc)


def snapshots_for_prices(prices, *, start=BASE, future_extra=False):
    rows = []
    for index, price in enumerate(prices):
        observed = start + timedelta(minutes=index, seconds=10)
        rows.append({
            "observed_at": observed.isoformat(),
            "mark_price": str(price),
            "instrument_id": 7,
            "symbol": "BTC-PERP",
            "entry_eligible": True,
            "close_only": False,
            "settlement": False,
            "research_only": True,
            "execution": "none",
        })
    if future_extra:
        rows.append({
            "observed_at": (start + timedelta(minutes=len(prices) + 10)).isoformat(),
            "mark_price": str(Decimal(prices[-1]) * Decimal("10")),
            "research_only": True,
            "execution": "none",
        })
    return rows


class PerpsEmaMomentumStrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = PerpsEmaMomentumStrategy()

    def test_bullish_crossover_requires_completed_bars_and_positive_momentum(self):
        prices = [Decimal("100")] * 55 + [
            Decimal("99.9"), Decimal("99.8"), Decimal("99.7"),
            Decimal("99.6"), Decimal("100.5"),
        ]
        now = BASE + timedelta(minutes=60, seconds=20)
        result = self.strategy.evaluate(
            snapshots_for_prices(prices),
            current_snapshot={"research_only": True, "execution": "none",
                              "entry_eligible": True, "close_only": False,
                              "settlement": False},
            now=now,
        )
        self.assertTrue(result["signal"], result)
        self.assertEqual(result["side"], "LONG")
        self.assertEqual(result["reason"], "bullish_ema_crossover_confirmed")
        self.assertGreater(Decimal(result["momentum_pct"]), 0)
        self.assertGreaterEqual(Decimal(result["stop_loss_distance_pct"]), Decimal("0.005"))
        self.assertLessEqual(Decimal(result["stop_loss_distance_pct"]), Decimal("0.015"))
        self.assertFalse(result["live_execution_eligible"])

    def test_bearish_crossover_requires_negative_momentum(self):
        prices = [Decimal("100")] * 55 + [
            Decimal("100.1"), Decimal("100.2"), Decimal("100.3"),
            Decimal("100.4"), Decimal("99.5"),
        ]
        now = BASE + timedelta(minutes=60, seconds=20)
        result = self.strategy.evaluate(snapshots_for_prices(prices), now=now)
        self.assertTrue(result["signal"], result)
        self.assertEqual(result["side"], "SHORT")
        self.assertLess(Decimal(result["momentum_pct"]), 0)

    def test_future_and_incomplete_snapshots_are_not_used(self):
        prices = [Decimal("100")] * 45
        for _ in range(15):
            prices.append(prices[-1] * Decimal("1.002"))
        rows = snapshots_for_prices(prices, future_extra=True)
        now = BASE + timedelta(minutes=60, seconds=20)
        bars = self.strategy.completed_minute_bars(rows, now=now)
        self.assertEqual(len(bars), 60)
        self.assertEqual(bars[-1]["close"], prices[-1])

    def test_incomplete_minute_history_and_stale_signal_abstain(self):
        prices = [Decimal("100")] * 45
        for _ in range(15):
            prices.append(prices[-1] * Decimal("1.002"))
        rows = snapshots_for_prices(prices)
        rows = [row for i, row in enumerate(rows) if i != 50]
        now = BASE + timedelta(minutes=60, seconds=20)
        result = self.strategy.evaluate(rows, now=now)
        self.assertEqual(result["reason"], "incomplete_contiguous_minute_history")

        stale = self.strategy.evaluate(
            snapshots_for_prices(prices),
            now=BASE + timedelta(minutes=65),
        )
        self.assertEqual(stale["reason"], "completed_bar_is_stale")

    def test_close_only_or_settling_instrument_never_generates_entry(self):
        prices = [Decimal("100")] * 45
        for _ in range(15):
            prices.append(prices[-1] * Decimal("1.002"))
        result = self.strategy.evaluate(
            snapshots_for_prices(prices),
            current_snapshot={"research_only": True, "execution": "none",
                              "entry_eligible": False, "close_only": True,
                              "settlement": False},
            now=BASE + timedelta(minutes=60, seconds=20),
        )
        self.assertFalse(result["signal"])
        self.assertEqual(result["reason"], "instrument_not_entry_eligible")

    def test_config_rejects_unsafe_position_notional_and_leverage_is_not_configurable(self):
        with self.assertRaises(ValueError):
            PerpsStrategyConfig(entry_notional_usd=Decimal("1000"))
        with self.assertRaises(ValueError):
            PerpsStrategyConfig(reward_risk_multiple=Decimal("5"))


if __name__ == "__main__":
    unittest.main()
