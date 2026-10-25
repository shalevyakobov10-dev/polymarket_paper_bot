"""Read-only global news RSS collector for research and paper monitoring.

This module stores attributed headlines and links only. It does not infer trades,
place orders, or treat a headline as a verified event or trading signal.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from paper_trading import rotate_jsonl_file


DEFAULT_FEEDS = (
    {
        "source": "BBC News",
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
    },
    {
        "source": "BBC News - World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "source": "BBC News - Business",
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
    },
    {
        "source": "BBC News - Technology",
        "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    },
    {
        "source": "BBC News - Science and Environment",
        "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    },
)

MAX_FEED_BYTES = 1_000_000
MAX_RECENT_ITEMS = 500
MAX_SUMMARY_CHARS = 1200
MAX_TITLE_CHARS = 400
USER_AGENT = "PolymarketPaperResearchBot/1.0 (+read-only RSS monitor)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str:
    accepted = {name.lower() for name in names}
    for child in list(element):
        if _local_name(child.tag) in accepted:
            if _local_name(child.tag) == "link":
                href = child.attrib.get("href")
                if href:
                    return href.strip()
            return " ".join("".join(child.itertext()).split()).strip()
    return ""


def _clean_text(value: str, limit: int) -> str:
    unescaped = html.unescape(value or "")
    without_tags = re.sub(r"<[^>]{0,500}>", " ", unescaped)
    normalized = " ".join(without_tags.split())
    return normalized[:limit]


def parse_feed(
    xml_data: bytes,
    source: str,
    feed_url: str,
    fetched_at: str | None = None,
) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom XML into bounded, attributed headline records."""
    if len(xml_data) > MAX_FEED_BYTES:
        raise ValueError("RSS feed exceeded the 1 MB response limit")
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed RSS/Atom XML: {exc}") from exc

    observed_at = fetched_at or utc_now()
    records: list[dict[str, str]] = []
    item_tag = "entry" if _local_name(root.tag) == "feed" else "item"

    for entry in root.iter():
        if _local_name(entry.tag) != item_tag:
            continue
        title_raw = _child_text(entry, "title")
        link = _child_text(entry, "link")
        stable_id = _child_text(entry, "guid", "id") or link
        published = _child_text(entry, "pubDate", "published", "updated", "date")
        summary_raw = _child_text(entry, "description", "summary", "content", "encoded")
        title = _clean_text(title_raw, MAX_TITLE_CHARS)
        summary = _clean_text(summary_raw, MAX_SUMMARY_CHARS)
        link = link[:2000]
        if not title or not link.startswith(("https://", "http://")):
            continue
        stable_key = stable_id.strip() or f"{title}|{published}"
        item_id = hashlib.sha256(f"{source}|{stable_key}".encode("utf-8")).hexdigest()
        records.append({
            "id": item_id,
            "source": source[:100],
            "feed_url": feed_url[:1000],
            "title": title,
            "summary": summary,
            "url": link,
            "published_at": published[:100],
            "fetched_at": observed_at,
        })
    return records


class NewsCollector:
    """Fetch a small, configurable set of public RSS feeds with de-duplication."""

    def __init__(
        self,
        log_path: str | Path,
        feeds: tuple[dict[str, str], ...] | list[dict[str, str]] = DEFAULT_FEEDS,
    ) -> None:
        self.log_path = Path(log_path)
        self.feeds = tuple(feeds)
        self.items: list[dict[str, str]] = []
        self.seen_ids: set[str] = set()
        self._load_existing()

    def _paths(self) -> list[Path]:
        backups = self._bounded_env("NEWS_LOG_BACKUPS", 3, 0, 10)
        return [self.log_path] + [
            self.log_path.with_name(f"{self.log_path.name}.{index}")
            for index in range(1, backups + 1)
        ]

    def _load_existing(self) -> None:
        stored: list[dict[str, str]] = []
        for path in reversed(self._paths()):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(record, dict) or not record.get("id"):
                            continue
                        record_id = str(record["id"])
                        self.seen_ids.add(record_id)
                        stored.append({str(k): str(v) for k, v in record.items()})
            except OSError:
                continue
        stored.sort(key=lambda row: row.get("fetched_at", ""))
        self.items = stored[-MAX_RECENT_ITEMS:]
        # A bounded set is enough to avoid duplicates across recent restarts.
        if len(self.seen_ids) > 20_000:
            self.seen_ids = {str(item["id"]) for item in self.items if item.get("id")}

    @staticmethod
    def _bounded_env(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except (ValueError, TypeError):
            value = default
        return min(maximum, max(minimum, value))

    def _append(self, record: dict[str, str]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        max_mb = self._bounded_env("NEWS_LOG_MAX_MB", 5, 1, 100)
        backups = self._bounded_env("NEWS_LOG_BACKUPS", 3, 0, 10)
        rotate_jsonl_file(self.log_path, max_mb * 1024 * 1024, backups)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def fetch_once(self) -> dict[str, Any]:
        """Fetch every configured feed independently; one source cannot block others."""
        successes = 0
        added: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        for feed in self.feeds:
            source = str(feed.get("source") or "Unknown feed")[:100]
            url = str(feed.get("url") or "")
            try:
                if not url.startswith("https://"):
                    raise ValueError("Feed URL must use HTTPS")
                request = Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                    },
                )
                with urlopen(request, timeout=10) as response:
                    status = getattr(response, "status", 200)
                    if status != 200:
                        raise RuntimeError(f"HTTP {status}")
                    payload = response.read(MAX_FEED_BYTES + 1)
                if len(payload) > MAX_FEED_BYTES:
                    raise ValueError("Feed exceeded the 1 MB response limit")
                parsed = parse_feed(payload, source, url)
                successes += 1
                for record in parsed:
                    if record["id"] in self.seen_ids:
                        continue
                    self._append(record)
                    self.seen_ids.add(record["id"])
                    self.items.append(record)
                    added.append(record)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                errors.append({"source": source, "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
            except Exception as exc:
                errors.append({"source": source, "error": f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"})

        self.items = self.items[-MAX_RECENT_ITEMS:]
        if len(self.seen_ids) > 20_000:
            self.seen_ids = {str(item["id"]) for item in self.items if item.get("id")}
        return {
            "feeds_configured": len(self.feeds),
            "feeds_succeeded": successes,
            "feeds_failed": len(errors),
            "feed_errors": errors,
            "items_added": len(added),
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-MAX_RECENT_ITEMS:])),
        }
