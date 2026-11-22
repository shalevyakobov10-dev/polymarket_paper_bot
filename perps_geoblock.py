"""Read-only live Polymarket geoblock lookup for a future Perps order gateway.

The official endpoint evaluates the requesting public IP. Its response IP is
intentionally discarded: it is neither persisted nor returned to callers.
This check is an additional runtime barrier, not a substitute for the separate
residency, sanctions, account-eligibility, and jurisdiction release review.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
USER_AGENT = "PolymarketResearchBot/1.0"
MAX_RESPONSE_BYTES = 64 * 1024
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class PerpsGeoblockError(RuntimeError):
    """The official Polymarket geoblock endpoint could not be verified."""


class PerpsGeoblockProvider:
    """Fetch the official geoblock response without retaining the caller IP."""

    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.25 <= float(timeout_seconds) <= 10.0
        ):
            raise ValueError("timeout_seconds must be between 0.25 and 10")
        self.timeout_seconds = float(timeout_seconds)

    def check(self) -> dict[str, Any]:
        request = Request(
            GEOBLOCK_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise PerpsGeoblockError(f"Geoblock API returned HTTP {status}")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise PerpsGeoblockError(f"Geoblock API returned HTTP {exc.code}") from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            raise PerpsGeoblockError(
                f"Geoblock API network error: {type(reason).__name__ if reason else 'unavailable'}"
            ) from None
        except TimeoutError:
            raise PerpsGeoblockError("Geoblock API request timed out") from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise PerpsGeoblockError("Geoblock API response exceeded the size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PerpsGeoblockError("Geoblock API returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("blocked"), bool):
            raise PerpsGeoblockError("Geoblock API response is missing a boolean blocked field")
        country = payload.get("country")
        if not isinstance(country, str) or not COUNTRY_RE.fullmatch(country):
            raise PerpsGeoblockError("Geoblock API response has an invalid country code")
        region = payload.get("region")
        if region is not None and (
            not isinstance(region, str) or not region.strip() or len(region) > 32
        ):
            raise PerpsGeoblockError("Geoblock API response has an invalid region code")

        return {
            "source": "polymarket_geoblock_api",
            "blocked": payload["blocked"],
            "country": country,
            "region": region.strip() if isinstance(region, str) else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "ip_retained": False,
            "execution": "none",
        }
