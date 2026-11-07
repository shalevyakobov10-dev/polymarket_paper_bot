"""Fail-closed live IP eligibility check against Polymarket's official endpoint."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from typing import Any

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


def check_polymarket_geoblock(*, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Check the IP used by this process; never returns or persists the raw IP."""
    if timeout_seconds <= 0 or timeout_seconds > 15:
        raise ValueError("geoblock timeout must be in (0, 15] seconds")
    request = Request(
        GEOBLOCK_URL,
        headers={"Accept": "application/json", "User-Agent": "PolymarketExecutionGateway/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError("geoblock endpoint returned a non-200 status")
            payload = json.loads(response.read(16_384).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Polymarket geoblock verification failed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("blocked"), bool):
        raise RuntimeError("Polymarket geoblock response schema is invalid")
    country = str(payload.get("country") or "").strip().upper()
    region = str(payload.get("region") or "").strip().upper()
    if len(country) != 2 or not country.isalpha() or not region:
        raise RuntimeError("Polymarket geoblock response omitted country or region")
    return {
        "verified": True,
        "blocked": payload["blocked"],
        "country": country,
        "region": region,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": GEOBLOCK_URL,
        "ip_redacted": True,
    }
