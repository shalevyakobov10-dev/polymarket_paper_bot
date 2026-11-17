"""Local JSON market snapshots for offline simulation; no exchange connectivity."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_NUMERIC_FIELDS = ("yes_price", "no_price", "yes_bid", "yes_ask", "no_bid", "no_ask")


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_market_snapshot(row: Any, *, max_age_seconds: float = 180.0, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("each market snapshot must be an object")
    market_id = str(row.get("market_id") or "").strip()
    question = str(row.get("question") or "").strip()
    if not market_id or not question:
        raise ValueError("market_id and question are required")
    observed = parse_timestamp(row.get("observed_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current.astimezone(timezone.utc) - observed).total_seconds() if observed else None
    if age is None or age < -30 or age > max_age_seconds:
        raise ValueError("market snapshot timestamp is missing, future-dated, or stale")
    values = {field: _number(row.get(field)) for field in REQUIRED_NUMERIC_FIELDS}
    if any(value is None or not 0.0 < value < 1.0 for value in values.values()):
        raise ValueError("market prices and top-of-book quotes must be in (0, 1)")
    if values["yes_bid"] >= values["yes_ask"] or values["no_bid"] >= values["no_ask"]:
        raise ValueError("market snapshot contains a crossed or locked book")
    if values["yes_bid"] > values["yes_price"] or values["yes_price"] > values["yes_ask"]:
        raise ValueError("YES price must be between best bid and best ask")
    if values["no_bid"] > values["no_price"] or values["no_price"] > values["no_ask"]:
        raise ValueError("NO price must be between best bid and best ask")
    normalized = {
        **{key: value for key, value in row.items() if key not in REQUIRED_NUMERIC_FIELDS},
        **values,
        "market_id": market_id,
        "question": question[:500],
        "observed_at": observed.isoformat(),
        "age_seconds": round(max(0.0, age), 3),
        "yes_token_id": str(row.get("yes_token_id") or f"{market_id}:YES"),
        "no_token_id": str(row.get("no_token_id") or f"{market_id}:NO"),
        "research_only": True,
        "execution": "none",
    }
    for field in ("volume_24h",):
        parsed = _number(row.get(field))
        normalized[field] = parsed if parsed is not None and parsed >= 0 else None
    return normalized


class JsonMarketSnapshotProvider:
    """Loads manually supplied/replay snapshots from disk, never from Polymarket."""
    def __init__(self, path: str | Path, *, max_age_seconds: float = 180.0) -> None:
        self.path = Path(path)
        self.max_age_seconds = float(max_age_seconds)

    def load(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"market snapshot file does not exist: {self.path}; populate it with local research data"
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("market snapshot file is unreadable or invalid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("market snapshot file must contain a JSON array")
        rows = [
            validate_market_snapshot(item, max_age_seconds=self.max_age_seconds, now=now)
            for item in payload
        ]
        ids = [row["market_id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("market snapshot contains duplicate market_id values")
        return rows
