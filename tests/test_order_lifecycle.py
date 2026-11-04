import unittest

from order_lifecycle import (
    begin_submission,
    create_order_intent,
    reconcile_submission,
    record_submission_response,
    submission_policy,
)


def intent(**overrides):
    values = {
        "client_order_id": "local-order-0001",
        "token_id": "token-123",
        "side": "BUY",
        "limit_price": 0.42,
        "size": 10,
    }
    values.update(overrides)
    return create_order_intent(**values)


class OrderLifecycleTests(unittest.TestCase):
    def test_new_intent_is_local_and_prepared(self):
        order = intent()
        self.assertEqual(order["status"], "prepared")
        self.assertEqual(order["submission_attempts"], 0)
        self.assertFalse(order["automatic_retry_permitted"])
        self.assertEqual(order["execution"], "none")
        self.assertTrue(submission_policy(order)["first_attempt_transition_allowed"])

    def test_order_intent_rejects_invalid_identity_and_economics(self):
        invalid = [
            {"client_order_id": ""},
            {"client_order_id": "not safe/id"},
            {"token_id": " "},
            {"side": "HOLD"},
            {"limit_price": 0},
            {"limit_price": 1},
            {"limit_price": float("nan")},
            {"size": 0},
            {"size": -1},
            {"size": True},
        ]
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    intent(**override)

    def test_begin_submission_records_only_one_initial_attempt(self):
        prepared = intent()
        pending = begin_submission(prepared)
        self.assertEqual(pending["status"], "submission_pending")
        self.assertEqual(pending["submission_attempts"], 1)
        self.assertFalse(submission_policy(pending)["first_attempt_transition_allowed"])
        self.assertFalse(submission_policy(pending)["automatic_retry_permitted"])
        with self.assertRaisesRegex(RuntimeError, "duplicate or unsafe"):
            begin_submission(pending)

    def test_explicit_acceptance_records_exchange_order_id(self):
        pending = begin_submission(intent())
        accepted = record_submission_response(
            pending, outcome="accepted", exchange_order_id="venue-order-77"
        )
        self.assertEqual(accepted["status"], "open")
        self.assertEqual(accepted["exchange_order_id"], "venue-order-77")
        with self.assertRaises(RuntimeError):
            begin_submission(accepted)

    def test_explicit_rejection_is_terminal_for_this_intent(self):
        pending = begin_submission(intent())
        rejected = record_submission_response(
            pending, outcome="rejected", reason_code="insufficient_balance"
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["last_reason_code"], "insufficient_balance")
        self.assertFalse(submission_policy(rejected)["first_attempt_transition_allowed"])

    def test_ambiguous_transport_result_requires_reconciliation_and_never_retry(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(
            pending, outcome="ambiguous", reason_code="transport_timeout"
        )
        policy = submission_policy(unknown)
        self.assertEqual(unknown["status"], "submission_unknown")
        self.assertTrue(policy["reconciliation_required"])
        self.assertFalse(policy["first_attempt_transition_allowed"])
        self.assertFalse(policy["automatic_retry_permitted"])
        with self.assertRaises(RuntimeError):
            begin_submission(unknown)

    def test_not_found_and_lookup_error_do_not_allow_resubmission(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(pending, outcome="ambiguous")
        for outcome in ("not_found", "lookup_error"):
            with self.subTest(outcome=outcome):
                unknown = reconcile_submission(unknown, outcome=outcome)
                self.assertEqual(unknown["status"], "submission_unknown")
                self.assertEqual(unknown["submission_attempts"], 1)
                self.assertFalse(submission_policy(unknown)["automatic_retry_permitted"])
        self.assertEqual(unknown["reconciliation_attempts"], 2)

    def test_matching_venue_lookup_confirms_existing_open_order(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(pending, outcome="ambiguous")
        reconciled = reconcile_submission(
            unknown,
            outcome="found_open",
            matched_client_order_id="local-order-0001",
            exchange_order_id="venue-order-77",
        )
        self.assertEqual(reconciled["status"], "open")
        self.assertEqual(reconciled["exchange_order_id"], "venue-order-77")
        self.assertEqual(reconciled["submission_attempts"], 1)

    def test_partial_and_full_fills_are_validated(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(pending, outcome="ambiguous")
        partial = reconcile_submission(
            unknown,
            outcome="found_partially_filled",
            matched_client_order_id="local-order-0001",
            exchange_order_id="venue-order-77",
            filled_size=3,
        )
        self.assertEqual(partial["status"], "partially_filled")
        self.assertEqual(partial["filled_size"], "3")
        full = reconcile_submission(
            unknown,
            outcome="found_filled",
            matched_client_order_id="local-order-0001",
            exchange_order_id="venue-order-77",
        )
        self.assertEqual(full["status"], "filled")
        self.assertEqual(full["filled_size"], "10")

    def test_mismatched_client_id_requires_manual_review(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(pending, outcome="ambiguous")
        result = reconcile_submission(
            unknown,
            outcome="found_open",
            matched_client_order_id="different-order",
            exchange_order_id="venue-order-99",
        )
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["last_reason_code"], "client_order_id_mismatch")
        self.assertFalse(submission_policy(result)["first_attempt_transition_allowed"])

    def test_reconciliation_rejects_inconsistent_fill_data(self):
        pending = begin_submission(intent())
        unknown = record_submission_response(pending, outcome="ambiguous")
        common = {
            "matched_client_order_id": "local-order-0001",
            "exchange_order_id": "venue-order-77",
        }
        with self.assertRaisesRegex(ValueError, "strictly between"):
            reconcile_submission(
                unknown, outcome="found_partially_filled", filled_size=10, **common
            )
        with self.assertRaisesRegex(ValueError, "equal to order size"):
            reconcile_submission(
                unknown, outcome="found_filled", filled_size=8, **common
            )

    def test_only_unknown_submission_can_be_reconciled(self):
        with self.assertRaises(RuntimeError):
            reconcile_submission(intent(), outcome="not_found")


if __name__ == "__main__":
    unittest.main()
