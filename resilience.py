"""Small, deterministic retry helpers for long-running external-data workers."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_retry_after(value: object, *, now: datetime | None = None) -> float | None:
    """Parse Retry-After as seconds or an HTTP date; return None for invalid input."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        seconds = float(text)
    except (TypeError, ValueError, OverflowError):
        seconds = None
    if seconds is not None:
        return seconds if math.isfinite(seconds) and seconds >= 0.0 else None

    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds())


def exponential_backoff_seconds(
    failure_count: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_fraction: float = 0.15,
    random_value: float | None = None,
    retry_after_seconds: float | None = None,
) -> float:
    """Return capped exponential backoff with bounded jitter and Retry-After support."""
    if isinstance(failure_count, bool) or not isinstance(failure_count, int) or failure_count < 1:
        raise ValueError("failure_count must be a positive integer")

    numeric = (base_seconds, max_seconds, jitter_fraction)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in numeric):
        raise ValueError("backoff configuration must contain finite numbers")
    if base_seconds <= 0 or max_seconds < base_seconds or not 0.0 <= jitter_fraction < 1.0:
        raise ValueError("invalid backoff bounds or jitter fraction")

    sample = random.random() if random_value is None else random_value
    if isinstance(sample, bool) or not isinstance(sample, (int, float)) or not math.isfinite(float(sample)) or not 0.0 <= sample <= 1.0:
        raise ValueError("random_value must be finite and between 0 and 1")

    if retry_after_seconds is not None:
        if (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(float(retry_after_seconds))
            or retry_after_seconds < 0.0
        ):
            raise ValueError("retry_after_seconds must be finite and nonnegative")

    # Avoid constructing enormous integers after a long-running outage.
    exponent = min(failure_count - 1, 30)
    nominal = min(float(max_seconds), float(base_seconds) * (2 ** exponent))
    jitter_multiplier = 1.0 + float(jitter_fraction) * (2.0 * float(sample) - 1.0)
    delay = min(float(max_seconds), nominal * jitter_multiplier)
    if retry_after_seconds is not None:
        delay = max(delay, min(float(max_seconds), float(retry_after_seconds)))
    return round(max(0.0, delay), 3)
