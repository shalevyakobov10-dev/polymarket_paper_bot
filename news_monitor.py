"""Read-only global news RSS collector for research and paper monitoring.

This module stores attributed headlines and links only. It does not infer trades,
place orders, or treat a headline as a verified event or trading signal.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
import re
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from paper_trading import rotate_jsonl_file


DEFAULT_FEEDS = (
    {"source": "BBC News", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"source": "BBC News - World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"source": "BBC News - Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"source": "BBC News - Technology", "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"source": "BBC News - Science and Environment", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"source": "The Guardian - World", "url": "https://www.theguardian.com/world/rss"},
    {"source": "The Guardian - US News", "url": "https://www.theguardian.com/us-news/rss"},
    {"source": "The Guardian - Business", "url": "https://www.theguardian.com/uk/business/rss"},
    {"source": "The Guardian - Technology", "url": "https://www.theguardian.com/uk/technology/rss"},
    {"source": "The Guardian - Science", "url": "https://www.theguardian.com/science/rss"},
    {"source": "Al Jazeera English", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"source": "NPR - Top Stories", "url": "https://feeds.npr.org/1001/rss.xml"},
    {"source": "Deutsche Welle - Top Stories", "url": "https://rss.dw.com/rdf/rss-en-top"},
    {"source": "Sky News - World", "url": "https://feeds.skynews.com/feeds/rss/world.xml"},
    {"source": "France 24 - English", "url": "https://www.france24.com/en/rss"},
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
        # Use the article URL as the global identity when present so one story
        # syndicated across multiple configured feeds is not stored repeatedly.
        identity = link.split("#", 1)[0].strip() if link else f"{source}|{stable_key}"
        item_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
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


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "can", "could", "did", "do", "does", "for", "from", "had",
    "has", "have", "he", "her", "here", "hers", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "may", "might", "more",
    "most", "must", "news", "of", "on", "or", "our", "ours", "shall",
    "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "then", "there", "these", "they", "this", "those",
    "through", "to", "under", "up", "us", "very", "was", "we", "were",
    "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your", "yours", "market", "markets", "update",
    "latest", "live", "report", "reports", "says", "said",
}


def _tokens(value: str) -> set[str]:
    tokens = set()
    for raw in re.findall(r"[a-z0-9]{3,}", (value or "").lower()):
        token = raw
        if len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 4 and token.endswith("ed"):
            token = token[:-2]
        elif len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) >= 3 and token not in _STOP_WORDS:
            tokens.add(token)
    return tokens


def match_news_to_markets(
    news_item: dict[str, Any],
    markets: list[dict[str, Any]],
    *,
    max_matches: int = 3,
) -> list[dict[str, Any]]:
    """Return conservative keyword-overlap candidates, never probabilities."""
    title_tokens = _tokens(str(news_item.get("title") or ""))
    news_tokens = title_tokens | _tokens(str(news_item.get("summary") or ""))
    if len(title_tokens) < 2 or len(news_tokens) < 2:
        return []

    candidates = []
    seen_market_ids: set[str] = set()
    for market in markets:
        market_id = str(market.get("market_id") or market.get("conditionId") or market.get("id") or "")
        question = str(market.get("question") or "").strip()
        if not market_id or not question or market_id in seen_market_ids:
            continue
        seen_market_ids.add(market_id)
        market_tokens = _tokens(question)
        shared = news_tokens & market_tokens
        title_shared = title_tokens & market_tokens
        # Require multiple non-generic terms and at least one in the headline itself.
        if len(shared) < 2 or not title_shared:
            continue
        candidates.append({
            "market_id": market_id,
            "question": question[:500],
            "matched_terms": sorted(shared),
            "headline_matched_terms": sorted(title_shared),
            "shared_term_count": len(shared),
            "lexical_overlap": round(len(shared) / max(1, len(market_tokens)), 4),
            "method": "keyword_overlap_v1",
            "research_only": True,
            "execution": "none",
        })
    candidates.sort(
        key=lambda row: (row["shared_term_count"], row["lexical_overlap"]),
        reverse=True,
    )
    return candidates[:max(1, min(10, int(max_matches)))]


class NewsMarketMatcher:
    """Persist de-duplicated news-to-market keyword links for research review."""

    def __init__(
        self,
        log_path: str | Path,
        *,
        max_recent: int = 500,
    ) -> None:
        self.log_path = Path(log_path)
        self.max_recent = max(1, min(5000, int(max_recent)))
        self._lock = threading.RLock()
        self.seen_pairs: set[str] = set()
        self.recent: list[dict[str, Any]] = []
        self._load_existing()

    def _paths(self) -> list[Path]:
        try:
            backups = int(os.getenv("NEWS_MATCH_LOG_BACKUPS", "3"))
        except (ValueError, TypeError):
            backups = 3
        backups = min(10, max(0, backups))
        return [self.log_path] + [
            self.log_path.with_name(f"{self.log_path.name}.{index}")
            for index in range(1, backups + 1)
        ]

    def _load_existing(self) -> None:
        records: list[dict[str, Any]] = []
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
                        if not isinstance(record, dict) or not record.get("pair_id"):
                            continue
                        self.seen_pairs.add(str(record["pair_id"]))
                        records.append(record)
            except OSError:
                continue
        records.sort(key=lambda row: str(row.get("matched_at") or ""))
        self.recent = records[-self.max_recent:]
        if len(self.seen_pairs) > 100_000:
            self.seen_pairs = {str(row["pair_id"]) for row in self.recent if row.get("pair_id")}

    def match(
        self,
        news_items: list[dict[str, Any]],
        markets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            added: list[dict[str, Any]] = []
            for news in news_items:
                news_id = str(news.get("id") or "").strip()
                if not news_id:
                    continue
                for candidate in match_news_to_markets(news, markets):
                    market_id = str(candidate["market_id"])
                    pair_id = hashlib.sha256(f"{news_id}|{market_id}".encode("utf-8")).hexdigest()
                    if pair_id in self.seen_pairs:
                        continue
                    record = {
                        "pair_id": pair_id,
                        "news_id": news_id,
                        "source": str(news.get("source") or "")[:100],
                        "news_title": str(news.get("title") or "")[:MAX_TITLE_CHARS],
                        "news_url": str(news.get("url") or "")[:2000],
                        "news_published_at": str(news.get("published_at") or "")[:100],
                        "market_id": market_id,
                        "market_question": candidate["question"],
                        "matched_terms": candidate["matched_terms"],
                        "headline_matched_terms": candidate["headline_matched_terms"],
                        "shared_term_count": candidate["shared_term_count"],
                        "lexical_overlap": candidate["lexical_overlap"],
                        "method": candidate["method"],
                        "research_only": True,
                        "execution": "none",
                        "matched_at": utc_now(),
                    }
                    self._append(record)
                    self.seen_pairs.add(pair_id)
                    self.recent.append(record)
                    added.append(record)
            self.recent = self.recent[-self.max_recent:]
            if len(self.seen_pairs) > 100_000:
                self.seen_pairs = {str(row["pair_id"]) for row in self.recent if row.get("pair_id")}
            return {
                "added": added,
                "items_total_recent": len(self.recent),
                "latest": list(reversed(self.recent)),
            }

    def _append(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            max_mb = int(os.getenv("NEWS_MATCH_LOG_MAX_MB", "5"))
        except (ValueError, TypeError):
            max_mb = 5
        max_mb = min(100, max(1, max_mb))
        try:
            backups = int(os.getenv("NEWS_MATCH_LOG_BACKUPS", "3"))
        except (ValueError, TypeError):
            backups = 3
        backups = min(10, max(0, backups))
        rotate_jsonl_file(self.log_path, max_mb * 1024 * 1024, backups)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


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

    @staticmethod
    def _sort_timestamp(record: dict[str, Any]) -> float:
        for key in ("published_at", "fetched_at"):
            raw = str(record.get(key) or "").strip()
            if not raw:
                continue
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
                except (TypeError, ValueError, OverflowError):
                    continue
        return 0.0

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
        stored.sort(key=self._sort_timestamp)
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

    @staticmethod
    def _fetch_one(feed: dict[str, str]) -> tuple[str, list[dict[str, str]], dict[str, str] | None]:
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
            return source, parse_feed(payload, source, url), None
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            return source, [], {"source": source, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        except Exception as exc:
            return source, [], {"source": source, "error": f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"}

    def fetch_once(self) -> dict[str, Any]:
        """Fetch feeds concurrently; isolate errors and persist de-duplicated items."""
        successes = 0
        added: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        workers = max(1, min(5, len(self.feeds)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rss-fetch") as pool:
            # executor.map retains configured feed order while network requests run concurrently.
            results = list(pool.map(self._fetch_one, self.feeds))
        for source, parsed, error in results:
            if error:
                errors.append(error)
                continue
            successes += 1
            for record in parsed:
                if record["id"] in self.seen_ids:
                    continue
                self._append(record)
                self.seen_ids.add(record["id"])
                self.items.append(record)


        self.items = self.items[-MAX_RECENT_ITEMS:]
        if len(self.seen_ids) > 20_000:
            self.seen_ids = {str(item["id"]) for item in self.items if item.get("id")}
        return {
            "feeds_configured": len(self.feeds),
            "feeds_succeeded": successes,
            "feeds_failed": len(errors),
            "feed_errors": errors,
            "items_added": len(added),
            "new_items": added,
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-MAX_RECENT_ITEMS:])),
        }
