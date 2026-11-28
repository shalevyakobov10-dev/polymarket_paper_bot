import json
import tempfile
import unittest
from pathlib import Path

from core.order_state_machine import (
    CANCELLED,
    DUPLICATE_BLOCKED,
    FILLED,
    INTENT,
    REJECTED,
    RISK_CHECKED,
    OrderStateError,
    OrderStateMachine,
)


class OrderStateMachineTests(unittest.TestCase):
    def test_rejects_illegal_transition_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.json"
            machine = OrderStateMachine(path)
            record, created = machine.begin(
                token_id="token-1", market_id="m1", side="BUY", idempotency_key="k1"
            )
            self.assertTrue(created)
            self.assertEqual(record.state, INTENT)
            with self.assertRaises(OrderStateError):
                machine.transition(record.order_id, FILLED, reason="skip")
            machine.transition(record.order_id, REJECTED, reason="no_edge")
            reloaded = OrderStateMachine(path)
            stored = reloaded.get(record.order_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.state, REJECTED)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload[0]["reason"], "no_edge")

    def test_same_key_and_active_token_do_not_create_a_second_live_intent(self):
        machine = OrderStateMachine()
        first, created = machine.begin(token_id="token-1", market_id="m1", side="BUY", idempotency_key="k1")
        self.assertTrue(created)
        again, created_again = machine.begin(token_id="token-1", market_id="m1", side="BUY", idempotency_key="k1")
        self.assertFalse(created_again)
        self.assertEqual(again.order_id, first.order_id)
        blocked, created_blocked = machine.begin(
            token_id="token-1", market_id="m1", side="BUY", idempotency_key="k2"
        )
        self.assertFalse(created_blocked)
        self.assertEqual(blocked.state, DUPLICATE_BLOCKED)
        machine.transition(first.order_id, CANCELLED, reason="operator_cancel")
        self.assertEqual(machine.get(first.order_id).state, CANCELLED)

    def test_terminal_order_does_not_block_a_new_key(self):
        machine = OrderStateMachine()
        first, _ = machine.begin(token_id="token-1", market_id="m1", side="BUY", idempotency_key="k1")
        machine.transition(first.order_id, REJECTED, reason="no_edge")
        second, created = machine.begin(token_id="token-1", market_id="m1", side="BUY", idempotency_key="k2")
        self.assertTrue(created)
        self.assertEqual(second.state, INTENT)
        self.assertNotEqual(second.order_id, first.order_id)
        machine.transition(second.order_id, "CONSENSUS_CHECKED", reason="ok")
        machine.transition(second.order_id, RISK_CHECKED, reason="ok")
        self.assertEqual(machine.get(second.order_id).state, RISK_CHECKED)


if __name__ == "__main__":
    unittest.main()
