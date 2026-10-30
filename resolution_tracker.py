"""Read-only ingestion of recently resolved binary Polymarket markets."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from forecast_evaluation import normalize_binary_resolution
from paper_trading import rotate_jsonl_file

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
USER_AGENT = "PolymarketPaperResearchBot/1.0"


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def fetch_recent_resolved_markets(
    *,
    limit: int = 100,
    pages: int = 2,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch a bounded recent page set of closed markets and keep decisive Yes/No outcomes."""
    page_size = min(100, max(1, int(limit)))
    page_count = min(10, max(1, int(pages)))
    discovered: dict[str, dict[str, Any]] = {}
    for page in range(page_count):
        params = urlencode({
            "closed": "true",
            "limit": page_size,
            "offset": page * page_size,
            "order": "closedTime",
            "ascending": "false",
        })
        request = Request(
            f"{GAMMA_MARKETS_URL}?{params}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError(f"Gamma resolved markets API returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gamma resolved markets response was not a list")
        for market in payload:
            if not isinstance(market, dict):
                continue
            normalized = normalize_binary_resolution(market)
            if normalized:
                discovered.setdefault(normalized["market_id"], normalized)
        if len(payload) < page_size:
            break
        time.sleep(0.05)
    return sorted(discovered.values(), key=lambda row: row["resolved_at"], reverse=True)


class ResolutionCollector:
    """Persist each resolved market once; safe to restart and research-only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._known_ids = self._load_known_ids()

    def _rotation_paths(self) -> list[Path]:
        backups = _bounded_env_int("RESOLUTION_LOG_BACKUPS", 3, 0, 10)
        return [
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(backups, 0, -1)
        ] + [self.path]

    def _load_known_ids(self) -> set[str]:
        known = set()
        for candidate in self._rotation_paths():
            if not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict) and row.get("market_id"):
                            known.add(str(row["market_id"]))
            except OSError:
                continue
        return known

    def fetch_once(self) -> dict[str, Any]:
        limit = _bounded_env_int("RESOLUTION_MARKETS_PER_POLL", 100, 1, 100)
        pages = _bounded_env_int("RESOLUTION_PAGES", 2, 1, 10)
        markets = fetch_recent_resolved_markets(limit=limit, pages=pages)
        added = []
        for market in markets:
            market_id = str(market["market_id"])
            if market_id in self._known_ids:
                continue
            record = {
                **market,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            max_mb = _bounded_env_int("RESOLUTION_LOG_MAX_MB", 5, 1, 50)
            backups = _bounded_env_int("RESOLUTION_LOG_BACKUPS", 3, 0, 10)
            rotate_jsonl_file(self.path, max_mb * 1024 * 1024, backups)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            self._known_ids.add(market_id)
            added.append(record)
        return {
            "markets_checked": len(markets),
            "resolutions_added": len(added),
            "resolutions_total_seen": len(self._known_ids),
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "research_only": True,
            "execution": "none",
        }
