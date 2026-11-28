"""Read-only global news discovery via the GDELT DOC 2.0 API."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from news_fusion import canonical_article_identity_url, canonical_article_url
from paper_trading import rotate_jsonl_file
from publisher_identity import publisher_identity

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERY = (
    "(war OR conflict OR election OR sanctions OR earthquake OR hurricane "
    "OR inflation OR central bank OR interest rates OR oil OR cyberattack "
    "OR ceasefire OR missile OR trade tariff OR government resignation)"
)

DEFAULT_TOPIC_QUERIES = (
    ("geopolitics_security", '(election OR parliament OR government OR diplomacy OR sanctions OR war OR ceasefire OR conflict OR military OR missile)'),
    ("macroeconomics_finance", '(inflation OR "central bank" OR "interest rates" OR GDP OR recession OR currency OR "bond yields" OR "stock market")'),
    ("business_trade", '(merger OR acquisition OR bankruptcy OR earnings OR exports OR imports OR tariffs OR "trade agreement" OR layoffs)'),
    ("technology_cyber_ai", '("artificial intelligence" OR semiconductor OR cybersecurity OR "data breach" OR chipmaker OR robotics OR "technology company")'),
    ("energy_commodities", '(oil OR "natural gas" OR OPEC OR electricity OR uranium OR lithium OR copper OR wheat OR grain)'),
    ("health_biotech", '(outbreak OR epidemic OR pandemic OR vaccine OR "clinical trial" OR biotech OR disease OR hospital)'),
    ("climate_disasters", '(earthquake OR hurricane OR flood OR wildfire OR drought OR cyclone OR "extreme weather" OR climate)'),
    ("sports", '(football OR soccer OR basketball OR tennis OR cricket OR Olympics OR FIFA OR championship OR tournament)'),
    ("science_space", '(NASA OR spaceflight OR astronomy OR "scientific discovery" OR research OR physics OR "space mission")'),
    ("entertainment_culture", '(movie OR film OR music OR celebrity OR actor OR "award ceremony" OR gaming OR "video game")'),
    ("law_regulation", '("court ruling" OR lawsuit OR regulation OR antitrust OR legislation OR "supreme court" OR investigation OR regulator)'),
    ("transport_logistics", '(airline OR airport OR shipping OR port OR freight OR railway OR logistics OR vessel)'),
    ("agriculture_food", '(harvest OR crop OR "food prices" OR agriculture OR livestock OR fertilizer OR "food supply" OR "food safety")'),
    ("labor_employment", '(unemployment OR wage OR strike OR union OR "labor market" OR hiring OR employment OR pension)'),
    ("education_policy", '(school OR university OR tuition OR "education policy" OR student OR exam OR "research funding")'),
    ("consumer_retail", '(retail OR "consumer spending" OR "e-commerce" OR supermarket OR "consumer confidence" OR "product recall")'),
    ("migration_demographics", '(migration OR immigration OR refugee OR population OR census OR "border crossing")'),
    ("infrastructure_utilities", '("power grid" OR "water supply" OR infrastructure OR blackout OR "nuclear plant" OR "data center" OR construction)'),
)


def math_is_finite_nonnegative(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return parsed >= 0 and parsed < float("inf")


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _configured_topics() -> tuple[tuple[str, str], ...]:
    custom = os.getenv("GDELT_QUERY", "").strip()
    if custom:
        return (("custom", custom[:1000]),)
    requested = [part.strip() for part in os.getenv("GDELT_TOPICS", "").split(",") if part.strip()]
    available = dict(DEFAULT_TOPIC_QUERIES)
    selected = [(name, available[name]) for name in requested if name in available] if requested else list(DEFAULT_TOPIC_QUERIES)
    return tuple(selected or list(DEFAULT_TOPIC_QUERIES))


def _topics_for_poll(topics: tuple[tuple[str, str], ...], cursor: int) -> tuple[list[tuple[str, str]], int]:
    if not topics:
        return [], 0
    count = _bounded_env_int("GDELT_TOPICS_PER_POLL", 6, 1, len(topics))
    selected = [topics[(cursor + offset) % len(topics)] for offset in range(count)]
    return selected, (cursor + count) % len(topics)


def _published_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return ""


_LANGUAGE_CODES = {
    "arabic": "ar", "chinese": "zh", "english": "en", "french": "fr",
    "german": "de", "hindi": "hi", "italian": "it", "japanese": "ja",
    "korean": "ko", "portuguese": "pt", "russian": "ru", "spanish": "es",
    "turkish": "tr", "ukrainian": "uk", "vietnamese": "vi",
}


def _language_code(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return "und"
    if len(raw) in {2, 3} and raw.isalpha():
        return raw[:3]
    return _LANGUAGE_CODES.get(raw, "und")


def normalize_gdelt_articles(
    payload: dict[str, Any],
    fetched_at: str | None = None,
    *,
    topic_category: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize attributed article metadata and the query lane that found it."""
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
        canonical_url = canonical_article_url(url)
        identity_url = canonical_article_identity_url(canonical_url)
        if (
            not title
            or not canonical_url.startswith("https://")
            or not identity_url
            or identity_url in seen
        ):
            continue
        seen.add(identity_url)
        source_language = _language_code(article.get("language"))
        records.append({
            "id": hashlib.sha256(identity_url.encode("utf-8")).hexdigest(),
            "source": str(article.get("domain") or "GDELT indexed source")[:100],
            "publisher_id": publisher_identity(
                str(article.get("domain") or ""), article_url=url
            ),
            "source_type": "gdelt_doc_api",
            "source_country": str(article.get("sourcecountry") or "")[:10],
            "language": str(article.get("language") or "")[:20],
            "source_language": source_language,
            "coverage_region": str(article.get("sourcecountry") or "global")[:48].lower(),
            "directional_analysis_supported": source_language == "en",
            "title": title,
            "summary": "",
            "url": url,
            # DOC 2.0's seendate is when GDELT observed/indexed the article,
            # not the publisher's verified publication time. Never feed it into
            # publication-age gates or event consensus as if it were a pubDate.
            "published_at": "",
            "indexed_at": _published_time(article.get("seendate")),
            "publication_time_status": "unknown",
            "timestamp_basis": "gdelt_index_observation_not_publisher_publication",
            "fetched_at": observed_at,
            "indexing_source": "GDELT DOC 2.0",
            "topic_categories": [str(topic_category)[:80]] if topic_category else [],
            "interpretation": "aggregated_media_coverage_not_event_verification",
            "research_only": "true",
            "execution": "none",
        })
    return records


class GDELTCollector:
    """Fetch a bounded, deduplicated one-hour global news window."""

    def __init__(
        self,
        log_path: str | Path,
        *,
        sleep: Callable[[float], Any] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(sleep) or not callable(monotonic):
            raise ValueError("GDELT request timing hooks must be callable")
        self.log_path = Path(log_path)
        self._sleep = sleep
        self._monotonic = monotonic
        try:
            configured_interval = float(os.getenv("GDELT_MIN_REQUEST_INTERVAL_SECONDS", "5"))
        except (TypeError, ValueError, OverflowError):
            configured_interval = 5.0
        if not 5.0 <= configured_interval <= 60.0:
            configured_interval = 5.0
        self._minimum_request_interval_seconds = configured_interval
        self._last_request_monotonic: float | None = None
        self.seen_ids: set[str] = set()
        self.items: list[dict[str, Any]] = []
        self._topic_queries = _configured_topics()
        self._topic_cursor_path = self.log_path.with_name(self.log_path.name + ".cursor.json")
        self._topic_cursor = self._load_topic_cursor()
        self._load_existing()

    def _load_topic_cursor(self) -> int:
        """Restore the round-robin cursor only when the configured catalog matches."""
        try:
            state = json.loads(self._topic_cursor_path.read_text(encoding="utf-8"))
            expected = [name for name, _ in self._topic_queries]
            if isinstance(state, dict) and state.get("topics") == expected:
                cursor = state.get("cursor")
                if isinstance(cursor, int) and not isinstance(cursor, bool):
                    return cursor % max(1, len(self._topic_queries))
        except (OSError, ValueError, TypeError):
            pass
        return 0

    def _save_topic_cursor(self) -> None:
        """Persist rotation progress atomically so restarts do not starve later topics."""
        if not self._topic_queries:
            return
        temporary = self._topic_cursor_path.with_name(self._topic_cursor_path.name + ".tmp")
        state = {
            "topics": [name for name, _ in self._topic_queries],
            "cursor": self._topic_cursor,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._topic_cursor_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self._topic_cursor_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_existing(self) -> None:
        # This method is called both at construction and by the shared-archive
        # reader. Rebuild the in-memory view from disk instead of appending the
        # same persisted rows on every Paper cycle.
        self.seen_ids.clear()
        self.items.clear()
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
                            normalized = dict(item)
                            # Migrate older local logs where seendate was incorrectly
                            # labelled as the publisher's publication time.
                            if normalized.get("source_type") == "gdelt_doc_api":
                                legacy_published = normalized.get("published_at")
                                if legacy_published and not normalized.get("indexed_at"):
                                    normalized["indexed_at"] = legacy_published
                                normalized["published_at"] = ""
                                normalized["publication_time_status"] = "unknown"
                                normalized["timestamp_basis"] = (
                                    "gdelt_index_observation_not_publisher_publication"
                                )
                            item_id = str(normalized["id"])
                            self.seen_ids.add(item_id)
                            self.items.append(normalized)
            except OSError:
                continue
        self.items = self.items[-500:]
        if len(self.seen_ids) > 20000:
            self.seen_ids = {item["id"] for item in self.items if item.get("id")}

    def read_archive_snapshot(self) -> dict[str, Any]:
        """Reload the shared GDELT archive without issuing another network request.

        The HTTP app owns GDELT polling. Separate Paper workers consume this
        append-only archive so the same deployment does not multiply anonymous
        DOC API queries merely because multiple processes need the headlines.
        """
        self._load_existing()
        languages = sorted({
            str(item.get("source_language") or "und").lower()
            for item in self.items
            if isinstance(item, dict)
        })
        countries = sorted({
            str(item.get("source_country") or "").lower()
            for item in self.items
            if isinstance(item, dict) and item.get("source_country")
        })
        topic_categories = sorted({
            str(category).lower()
            for item in self.items
            if isinstance(item, dict)
            for category in (item.get("topic_categories") or [])
            if isinstance(category, str)
        })
        return {
            "items_added": 0,
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-500:])),
            "source": "shared_gdelt_archive",
            "topics_configured": len(self._topic_queries),
            "topics_seen_in_archive": topic_categories,
            "languages_seen": languages,
            "countries_seen": countries,
            "network_requests_made": 0,
            "poll_status_available": False,
            "interpretation": "shared_archive_snapshot_not_a_new_gdelt_poll",
        }

    def fetch_once(self) -> dict[str, Any]:
        """Rotate through broad global-news topic lanes and persist unique articles."""
        topics, next_cursor = _topics_for_poll(self._topic_queries, self._topic_cursor)
        self._topic_cursor = next_cursor
        self._save_topic_cursor()
        max_records = _bounded_env_int("GDELT_MAX_RECORDS_PER_TOPIC", 75, 1, 250)
        window = os.getenv("GDELT_TIMESPAN", "1h").strip() or "1h"
        allowed_windows = {"15min", "30min", "1h", "2h", "4h", "6h", "12h", "1d"}
        if window not in allowed_windows:
            window = "1h"

        aggregated: dict[str, dict[str, Any]] = {}
        topic_results: dict[str, int] = {}
        topic_errors: list[dict[str, str]] = []
        network_requests_made = 0
        for category, query in topics:
            params = {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "timespan": window,
                "maxrecords": max_records,
                "sort": "datedesc",
            }
            request = Request(
                f"{GDELT_DOC_URL}?{urlencode(params)}",
                headers={
                    "User-Agent": "PolymarketPaperResearchBot/1.0 (+read-only global news research)",
                    "Accept": "application/json",
                },
            )
            try:
                # The anonymous DOC API is rate-limited. Keep a strict minimum
                # five-second gap between every topic request in this process.
                if self._last_request_monotonic is not None:
                    elapsed = self._monotonic() - self._last_request_monotonic
                    remaining = self._minimum_request_interval_seconds - elapsed
                    if remaining > 0:
                        self._sleep(remaining)
                request_started = self._monotonic()
                if not math_is_finite_nonnegative(request_started):
                    raise RuntimeError("GDELT monotonic clock returned an invalid value")
                self._last_request_monotonic = request_started
                network_requests_made += 1
                with urlopen(request, timeout=12) as response:
                    if getattr(response, "status", 200) != 200:
                        raise RuntimeError(f"GDELT API returned HTTP {response.status}")
                    payload = json.loads(response.read().decode("utf-8"))
                parsed = normalize_gdelt_articles(payload, topic_category=category)
                topic_results[category] = len(parsed)
                for article in parsed:
                    article_id = article["id"]
                    previous = aggregated.get(article_id)
                    if previous is None:
                        aggregated[article_id] = article
                    else:
                        previous["topic_categories"] = sorted(
                            set(previous.get("topic_categories") or [])
                            | set(article.get("topic_categories") or [])
                        )
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                topic_errors.append({
                    "topic": category,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                })

        if not topic_results:
            detail = "; ".join(item["topic"] + ": " + item["error"] for item in topic_errors[:3])
            raise RuntimeError(f"All GDELT topic queries failed: {detail}")

        existing_by_id = {str(item.get("id")): item for item in self.items if item.get("id")}
        added = []
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for item in aggregated.values():
            existing = existing_by_id.get(item["id"])
            if existing is not None:
                existing["topic_categories"] = sorted(
                    set(existing.get("topic_categories") or [])
                    | set(item.get("topic_categories") or [])
                )
                continue
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
            self.seen_ids = {str(item["id"]) for item in self.items if item.get("id")}
        languages_seen = sorted({
            str(item.get("source_language") or "und").lower()
            for item in self.items
        })
        countries_seen = sorted({
            str(item.get("source_country") or "").lower()
            for item in self.items
            if item.get("source_country")
        })
        topic_categories_seen = sorted({
            str(category).lower()
            for item in self.items
            for category in (item.get("topic_categories") or [])
            if isinstance(category, str)
        })
        non_english_items_total_recent = sum(
            1 for item in self.items
            if item.get("directional_analysis_supported") is False
        )
        return {
            "items_added": len(added),
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-500:])),
            "languages_seen": languages_seen,
            "countries_seen": countries_seen,
            "topic_categories_seen": topic_categories_seen,
            "non_english_items_total_recent": non_english_items_total_recent,
            "query_window": window,
            "source": "GDELT DOC 2.0",
            "topics_configured": len(self._topic_queries),
            "topics_attempted": [category for category, _ in topics],
            "topics_succeeded": len(topic_results),
            "topics_failed": len(topic_errors),
            "topics_seen_in_archive": topic_categories_seen,
            "network_requests_made": network_requests_made,
            "poll_status_available": True,
            "topic_results": topic_results,
            "topic_errors": topic_errors,
            "max_records_per_topic": max_records,
            "interpretation": "aggregated_media_coverage_not_event_verification",
        }
