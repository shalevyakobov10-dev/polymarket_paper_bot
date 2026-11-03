import unittest
from datetime import datetime, timezone
from email.utils import format_datetime

from resilience import exponential_backoff_seconds, parse_retry_after


class RetryPolicyTests(unittest.TestCase):
    def test_exponential_delay_grows_and_is_capped(self):
        delays = [
            exponential_backoff_seconds(
                failures,
                base_seconds=30,
                max_seconds=100,
                jitter_fraction=0,
                random_value=0.5,
            )
            for failures in (1, 2, 3, 4, 5)
        ]
        self.assertEqual(delays, [30.0, 60.0, 100.0, 100.0, 100.0])

    def test_jitter_stays_within_configured_bounds(self):
        low = exponential_backoff_seconds(
            2, base_seconds=60, max_seconds=600,
            jitter_fraction=0.2, random_value=0.0,
        )
        high = exponential_backoff_seconds(
            2, base_seconds=60, max_seconds=600,
            jitter_fraction=0.2, random_value=1.0,
        )
        self.assertEqual(low, 96.0)
        self.assertEqual(high, 144.0)

    def test_retry_after_seconds_is_a_minimum_wait(self):
        delay = exponential_backoff_seconds(
            1, base_seconds=30, max_seconds=300,
            jitter_fraction=0, random_value=0.5,
            retry_after_seconds=90,
        )
        self.assertEqual(delay, 90.0)

    def test_retry_after_is_still_capped(self):
        delay = exponential_backoff_seconds(
            1, base_seconds=30, max_seconds=120,
            jitter_fraction=0, random_value=0.5,
            retry_after_seconds=600,
        )
        self.assertEqual(delay, 120.0)

    def test_parse_retry_after_accepts_seconds_and_http_date(self):
        now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        retry_at = datetime(2026, 11, 3, 12, 2, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("45"), 45.0)
        self.assertEqual(parse_retry_after(format_datetime(retry_at), now=now), 120.0)

    def test_parse_retry_after_rejects_invalid_values(self):
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("not-a-date"))
        self.assertIsNone(parse_retry_after("NaN"))
        self.assertIsNone(parse_retry_after("-2"))

    def test_invalid_backoff_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            exponential_backoff_seconds(0, base_seconds=30, max_seconds=60)
        with self.assertRaises(ValueError):
            exponential_backoff_seconds(1, base_seconds=60, max_seconds=30)


if __name__ == "__main__":
    unittest.main()
