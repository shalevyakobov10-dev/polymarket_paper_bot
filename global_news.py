"""Read-only global news discovery via the GDELT DOC 2.0 API."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from paper_trading import rotate_jsonl_file

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERY = (
    "(war OR conflict OR election OR sanctions OR earthquake OR hurricane "
    "OR inflation OR central bank OR interest rates OR oil OR cyberattack "
    "OR ceasefire OR missile OR trade tariff OR government resignation)"
)


def _published_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return ""


def normalize_gdelt_articles(payload: dict[str, Any], fetched_at: str | None = None) -> list[dict[str, str]]:
    """Normalize article metadata; GDELT indexing is not independent event verification."""
    if not isinstance(payload, dict):
        raise ValueError("GDELT response must be a JSON object")
    articles = payload.get("articles")
    if not isinstance(articles, list):
        raise ValueError("GDELT response is missing its articles list")
    observed_at = fetched_at or datetime.now(timezone.utc).isoformat()
    records = []
    seen = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = " ".join(str(article.get("title") or "").split())[:400]
        url = str(article.get("url") or "").strip()[:2000]
        canonical_url = url.split("#", 1)[0]
        if not title or not canonical_url.startswith("https://") or canonical_url in seen:
            continue
        seen.add(canonical_url)
        records.append({
            "id": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
            "source": str(article.get("domain") or "GDELT indexed source")[:100],
            "source_type": "gdelt_doc_api",
            "source_country": str(article.get("sourcecountry") or "")[:10],
            "language": str(article.get("language") or "")[:20],
            "title": title,
            "summary": "",
            "url": url,
            "published_at": _published_time(article.get("seendate")),
            "fetched_at": observed_at,
            "indexing_source": "GDELT DOC 2.0",
            "interpretation": "aggregated_media_coverage_not_event_verification",
            "research_only": "true",
            "execution": "none",
        })
    return records


class GDELTCollector:
    """Fetch a bounded, deduplicated one-hour global news window."""

    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.seen_ids: set[str] = set()
        self.items: list[dict[str, str]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        paths = [self.log_path] + [
            self.log_path.with_name(f"{self.log_path.name}.{i}")
            for i in range(1, 4)
        ]
        for path in reversed(paths):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(item, dict) and item.get("id"):
                            item_id = str(item["id"])
                            self.seen_ids.add(item_id)
                            self.items.append({str(k): str(v) for k, v in item.items()})
            except OSError:
                continue
        self.items = self.items[-500:]
        if len(self.seen_ids) > 20000:
            self.seen_ids = {item["id"] for item in self.items if item.get("id")}

    def fetch_once(self) -> dict[str, Any]:
        query = os.getenv("GDELT_QUERY", DEFAULT_QUERY).strip() or DEFAULT_QUERY
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": "1h",
            "maxrecords": 100,
            "sort": "datedesc",
        }
        request = Request(
            f"{GDELT_DOC_URL}?{urlencode(params)}",
            headers={
                "User-Agent": "PolymarketPaperResearchBot/1.0 (+read-only global news research)",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=12) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError(f"GDELT API returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        parsed = normalize_gdelt_articles(payload)
        added = []
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for item in parsed:
            if item["id"] in self.seen_ids:
                continue
            rotate_jsonl_file(self.log_path, 5 * 1024 * 1024, 3)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            self.seen_ids.add(item["id"])
            self.items.append(item)
            added.append(item)
        self.items = self.items[-500:]
        if len(self.seen_ids) > 20000:
            self.seen_ids = {item["id"] for item in self.items if item.get("id")}
        return {
            "items_added": len(added),
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-500:])),
            "query_window": "last_1_hour",
            "source": "GDELT DOC 2.0",
            "interpretation": "aggregated_media_coverage_not_event_verification",
        }
