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
from publisher_identity import publisher_identity


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
MATCHER_METHOD = "market_corpus_idf_keyword_v2"
USER_AGENT = "PolymarketPaperResearchBot/1.0 (+read-only RSS monitor)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize_news_freshness(
    items: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize observed publication/ingestion times without implying event freshness."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    def parse_time(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    published = [parse_time(item.get("published_at")) for item in items]
    fetched = [parse_time(item.get("fetched_at")) for item in items]
    published = [value for value in published if value is not None]
    fetched = [value for value in fetched if value is not None]
    latest_published = max(published) if published else None
    latest_fetched = max(fetched) if fetched else None
    return {
        "items_considered": len(items),
        "items_with_publish_time": len(published),
        "latest_published_at": latest_published.isoformat() if latest_published else None,
        "seconds_since_latest_publish": round(max(0.0, (current - latest_published).total_seconds()), 1) if latest_published else None,
        "latest_fetched_at": latest_fetched.isoformat() if latest_fetched else None,
        "seconds_since_latest_fetch": round(max(0.0, (current - latest_fetched).total_seconds()), 1) if latest_fetched else None,
        "interpretation": "feed_item_timestamps_only_not_event_verification_or_market_signal",
    }


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
            "publisher_id": publisher_identity(source, feed_url, link),
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
    """Rank research-only news candidates using market-corpus rarity and title evidence.

    Terms repeated across many tracked markets are down-weighted and cannot alone
    anchor a match. With fewer than four distinct markets, corpus rarity is not
    reliable, so the matcher records that limitation and uses the headline overlap
    only as a provisional anchor. Scores are ranking diagnostics, never probabilities.
    """
    title_tokens = _tokens(str(news_item.get("title") or ""))
    news_tokens = title_tokens | _tokens(str(news_item.get("summary") or ""))
    if len(title_tokens) < 2 or len(news_tokens) < 2:
        return []

    # Build a unique market-question corpus first so duplicate market rows cannot
    # distort document frequencies or artificially suppress distinctive terms.
    market_rows: list[tuple[str, str, set[str]]] = []
    seen_market_ids: set[str] = set()
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_id = str(
            market.get("market_id") or market.get("conditionId") or market.get("id") or ""
        ).strip()
        question = str(market.get("question") or "").strip()
        if not market_id or not question or market_id in seen_market_ids:
            continue
        seen_market_ids.add(market_id)
        tokens = _tokens(question)
        if tokens:
            market_rows.append((market_id, question, tokens))

    corpus_size = len(market_rows)
    if not corpus_size:
        return []
    document_frequency: dict[str, int] = {}
    for _, _, tokens in market_rows:
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    rarity_filter_active = corpus_size >= 4
    max_common_documents = max(1, math.floor(corpus_size * 0.35))

    def idf(token: str) -> float:
        frequency = document_frequency.get(token, 0)
        return math.log((corpus_size + 1) / (frequency + 1)) + 1.0

    candidates = []
    for market_id, question, market_tokens in market_rows:
        shared = news_tokens & market_tokens
        title_shared = title_tokens & market_tokens
        if len(shared) < 2 or not title_shared:
            continue

        distinctive_title = (
            {term for term in title_shared if document_frequency.get(term, 0) <= max_common_documents}
            if rarity_filter_active
            else set(title_shared)
        )
        # Once the tracked-market corpus is large enough, generic headline terms
        # seen in most markets cannot independently qualify a candidate.
        if not distinctive_title:
            continue

        shared_weight = sum(idf(term) for term in shared)
        market_weight = sum(idf(term) for term in market_tokens)
        title_weight = sum(idf(term) for term in title_tokens)
        title_shared_weight = sum(idf(term) for term in title_shared)
        market_coverage = shared_weight / max(1e-12, market_weight)
        headline_precision = title_shared_weight / max(1e-12, title_weight)
        relevance_score = 0.65 * headline_precision + 0.35 * market_coverage

        candidates.append({
            "market_id": market_id,
            "question": question[:500],
            "matched_terms": sorted(shared),
            "headline_matched_terms": sorted(title_shared),
            "distinctive_headline_terms": sorted(distinctive_title),
            "common_headline_terms": sorted(title_shared - distinctive_title),
            "term_document_frequency": {
                term: document_frequency.get(term, 0) for term in sorted(shared)
            },
            "shared_term_count": len(shared),
            "lexical_overlap": round(len(shared) / max(1, len(market_tokens)), 4),
            "market_coverage": round(market_coverage, 6),
            "headline_precision": round(headline_precision, 6),
            "relevance_score": round(relevance_score, 6),
            "market_corpus_size": corpus_size,
            "market_corpus_quality": (
                "rarity_filter_active" if rarity_filter_active
                else "insufficient_for_rarity_filter"
            ),
            "method": MATCHER_METHOD,
            "research_only": True,
            "execution": "none",
        })
    candidates.sort(
        key=lambda row: (
            row["relevance_score"],
            len(row["distinctive_headline_terms"]),
            row["shared_term_count"],
            row["lexical_overlap"],
        ),
        reverse=True,
    )
    try:
        limit = int(max_matches)
    except (TypeError, ValueError, OverflowError):
        limit = 3
    return candidates[:max(1, min(10, limit))]

def summarize_market_corroboration(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize publisher diversity and exact-title repetition without claiming verification."""
    grouped: dict[str, dict[str, Any]] = {}
    for match in matches:
        market_id = str(match.get("market_id") or "").strip()
        source = str(match.get("source") or "").strip()
        if not market_id or not source:
            continue
        # Multiple sections of one publisher (e.g. BBC World and BBC Business)
        # are one source, not independent corroboration.
        publisher = source.split(" - ", 1)[0].strip()[:100]
        publisher_id = str(match.get("publisher_id") or publisher_identity(source)).strip()
        title = " ".join(str(match.get("news_title") or match.get("title") or "").casefold().split())
        # Exact normalized title fingerprints expose repeated/syndicated headlines.
        # They are deliberately not treated as independent evidence.
        title_tokens = sorted(_tokens(title))
        headline_key = " ".join(title_tokens) if len(title_tokens) >= 3 else ""
        row = grouped.setdefault(market_id, {
            "market_id": market_id,
            "market_question": str(match.get("market_question") or "")[:500],
            "publishers": {},
            "headline_count": 0,
            "headline_publishers": {},
        })
        row["publishers"].setdefault(publisher_id, publisher)
        row["headline_count"] += 1
        if headline_key:
            row["headline_publishers"].setdefault(headline_key, set()).add(publisher_id)

    summaries = []
    for row in grouped.values():
        publisher_ids = sorted(row["publishers"])
        publishers = sorted(set(row["publishers"].values()))
        headline_groups = list(row["headline_publishers"].values())
        repeated_groups = [group for group in headline_groups if len(group) > 1]
        summaries.append({
            "market_id": row["market_id"],
            "market_question": row["market_question"],
            "distinct_publisher_count": len(publisher_ids),
            "publisher_ids": publisher_ids,
            "publishers": publishers,
            "matched_headline_count": row["headline_count"],
            "distinct_normalized_headline_count": len(headline_groups),
            "repeated_headline_group_count": len(repeated_groups),
            "max_publishers_sharing_exact_headline": max((len(group) for group in headline_groups), default=0),
            "interpretation": "publisher_diversity_and_exact_title_repetition_only_not_independent_event_verification",
            "research_only": True,
            "execution": "none",
        })
    summaries.sort(
        key=lambda row: (row["distinct_publisher_count"], row["matched_headline_count"]),
        reverse=True,
    )
    return summaries


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
        self.latest_method_by_pair: dict[str, str] = {}
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
        # Replay rotated logs oldest-to-newest and keep only the latest revision
        # for each stable news/market pair. This permits matcher-version upgrades
        # without showing duplicate historical candidates after a restart.
        latest_records: dict[str, dict[str, Any]] = {}
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
                        pair_id = str(record["pair_id"])
                        latest_records[pair_id] = record
            except OSError:
                continue
        ordered = sorted(
            latest_records.values(),
            key=lambda row: str(row.get("matched_at") or ""),
        )
        self.recent = ordered[-self.max_recent:]
        self.latest_method_by_pair = {
            pair_id: str(record.get("method") or "")
            for pair_id, record in latest_records.items()
        }
        self.seen_pairs = set(latest_records)
        if len(self.seen_pairs) > 100_000:
            self.seen_pairs = {str(row["pair_id"]) for row in self.recent if row.get("pair_id")}
            self.latest_method_by_pair = {
                str(row["pair_id"]): str(row.get("method") or "")
                for row in self.recent if row.get("pair_id")
            }

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
                    # Re-evaluate old persisted pairs once when the matcher changes.
                    # Pairs already scored by the current version remain idempotent.
                    if self.latest_method_by_pair.get(pair_id) == MATCHER_METHOD:
                        continue
                    record = {
                        "pair_id": pair_id,
                        "news_id": news_id,
                        "source": str(news.get("source") or "")[:100],
                        "publisher_id": str(news.get("publisher_id") or publisher_identity(
                            str(news.get("source") or ""),
                            str(news.get("feed_url") or ""),
                            str(news.get("url") or ""),
                        ))[:120],
                        "news_title": str(news.get("title") or "")[:MAX_TITLE_CHARS],
                        "news_url": str(news.get("url") or "")[:2000],
                        "news_published_at": str(news.get("published_at") or "")[:100],
                        "topic_categories": sorted({
                            str(value)[:80] for value in (
                                news.get("topic_categories")
                                if isinstance(news.get("topic_categories"), list) else []
                            ) if str(value).strip()
                        })[:10],
                        "source_country": str(news.get("source_country") or "")[:10],
                        "language": str(news.get("language") or "")[:20],
                        "market_id": market_id,
                        "market_question": candidate["question"],
                        "matched_terms": candidate["matched_terms"],
                        "headline_matched_terms": candidate["headline_matched_terms"],
                        "shared_term_count": candidate["shared_term_count"],
                        "lexical_overlap": candidate["lexical_overlap"],
                        "distinctive_headline_terms": candidate["distinctive_headline_terms"],
                        "common_headline_terms": candidate["common_headline_terms"],
                        "term_document_frequency": candidate["term_document_frequency"],
                        "market_coverage": candidate["market_coverage"],
                        "headline_precision": candidate["headline_precision"],
                        "relevance_score": candidate["relevance_score"],
                        "market_corpus_size": candidate["market_corpus_size"],
                        "market_corpus_quality": candidate["market_corpus_quality"],
                        "method": candidate["method"],
                        "research_only": True,
                        "execution": "none",
                        "matched_at": utc_now(),
                    }
                    self._append(record)
                    self.seen_pairs.add(pair_id)
                    self.latest_method_by_pair[pair_id] = MATCHER_METHOD
                    self.recent = [row for row in self.recent if str(row.get("pair_id")) != pair_id]
                    self.recent.append(record)
                    added.append(record)
            self.recent.sort(key=lambda row: str(row.get("matched_at") or ""))
            self.recent = self.recent[-self.max_recent:]
            if len(self.seen_pairs) > 100_000:
                self.seen_pairs = {str(row["pair_id"]) for row in self.recent if row.get("pair_id")}
                self.latest_method_by_pair = {
                    str(row["pair_id"]): str(row.get("method") or "")
                    for row in self.recent if row.get("pair_id")
                }
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
                added.append(record)

        self.items.sort(key=self._sort_timestamp)
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
