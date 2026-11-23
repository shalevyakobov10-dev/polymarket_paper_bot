"""Fuse RSS/Atom and GDELT article metadata without inventing independence.

The URL identity is used to merge the same article across collectors. Each
article retains source lanes and regional metadata, while publisher identity is
normalized from the article's own domain when possible. GDELT index-observation
time is never converted into a publisher publication timestamp.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from publisher_identity import publisher_identity


def canonical_article_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.lower().rstrip(".")
        port = parsed.port
        if port and not (
            (parsed.scheme.lower() == "https" and port == 443)
            or (parsed.scheme.lower() == "http" and port == 80)
        ):
            host = f"{hostname}:{port}"
        else:
            host = hostname
        return urlunsplit((
            parsed.scheme.lower(),
            host,
            parsed.path or "/",
            parsed.query,
            "",
        ))
    except (TypeError, ValueError, UnicodeError):
        return ""


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sorted_unique(values: Any, *, max_items: int = 20, max_length: int = 80) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values is not None else []
    cleaned = {
        str(value).strip()[:max_length]
        for value in values
        if str(value or "").strip()
    }
    return sorted(cleaned)[:max_items]


def merge_news_sources(
    *collections: list[dict[str, Any]],
    max_items: int = 500,
) -> list[dict[str, Any]]:
    """Merge recent RSS/GDELT items by canonical article URL, preserving provenance.

    When one source provides a publisher publication timestamp, it is preserved.
    GDELT's indexed-at value is retained separately and cannot replace published_at.
    """
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        raise ValueError("max_items must be an integer between 1 and 5000")

    merged: dict[str, dict[str, Any]] = {}
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for raw in collection:
            if not isinstance(raw, dict):
                continue
            url = canonical_article_url(raw.get("url"))
            raw_id = str(raw.get("id") or "").strip()
            if not url and not raw_id:
                continue
            identity = url or raw_id
            item_id = (
                hashlib.sha256(url.encode("utf-8")).hexdigest()
                if url else raw_id[:160]
            )
            source = str(raw.get("source") or "").strip()[:100]
            feed_url = str(raw.get("feed_url") or "").strip()[:1000]
            source_type = str(raw.get("source_type") or "unknown").strip().lower()[:40]
            raw_publisher = str(raw.get("publisher_id") or "").strip()[:120]
            article_publisher = publisher_identity("", "", url) if url else ""
            publisher = article_publisher or raw_publisher or publisher_identity(source, feed_url, url)
            title = " ".join(str(raw.get("title") or "").split())[:400]
            if not title:
                continue

            item = merged.get(identity)
            if item is None:
                item = {
                    "id": item_id,
                    "url": url or str(raw.get("url") or "")[:2000],
                    "title": title,
                    "summary": str(raw.get("summary") or "")[:1200],
                    "source": source,
                    "publisher_id": publisher,
                    "published_at": "",
                    "indexed_at": "",
                    "fetched_at": "",
                    "source_types": [],
                    "source_names": [],
                    "feed_urls": [],
                    "publisher_ids_observed": [],
                    "topic_categories": [],
                    "source_countries": [],
                    "source_languages": [],
                    "coverage_regions": [],
                    "publication_time_status": "unknown",
                    "timestamp_basis": "publisher_timestamp_if_available",
                    "research_only": True,
                    "execution": "none",
                }
                merged[identity] = item

            # Prefer article-domain publisher identity. A feed section or GDELT
            # query lane is not an independent publisher identity.
            if article_publisher:
                item["publisher_id"] = article_publisher
            elif not item.get("publisher_id") and publisher:
                item["publisher_id"] = publisher

            if len(title) > len(str(item.get("title") or "")):
                item["title"] = title
            summary = str(raw.get("summary") or "").strip()
            if len(summary) > len(str(item.get("summary") or "")):
                item["summary"] = summary[:1200]
            if not item.get("source") and source:
                item["source"] = source

            published = _parse_time(raw.get("published_at"))
            current_published = _parse_time(item.get("published_at"))
            if published is not None and (
                current_published is None or published < current_published
            ):
                # Earliest valid publication time prevents later syndication
                # timestamps from making an older story appear newly published.
                item["published_at"] = published.isoformat()
                item["publication_time_status"] = "publisher_timestamp_available"

            indexed = _parse_time(raw.get("indexed_at"))
            current_indexed = _parse_time(item.get("indexed_at"))
            if indexed is not None and (
                current_indexed is None or indexed > current_indexed
            ):
                item["indexed_at"] = indexed.isoformat()

            fetched = _parse_time(raw.get("fetched_at"))
            current_fetched = _parse_time(item.get("fetched_at"))
            if fetched is not None and (
                current_fetched is None or fetched > current_fetched
            ):
                item["fetched_at"] = fetched.isoformat()

            item["source_types"] = _sorted_unique(
                item["source_types"] + [source_type], max_items=10, max_length=40
            )
            item["source_names"] = _sorted_unique(
                item["source_names"] + [source], max_items=20, max_length=100
            )
            item["feed_urls"] = _sorted_unique(
                item["feed_urls"] + ([feed_url] if feed_url else []),
                max_items=20,
                max_length=1000,
            )
            item["publisher_ids_observed"] = _sorted_unique(
                item["publisher_ids_observed"] + [raw_publisher, publisher],
                max_items=20,
                max_length=120,
            )
            for output_field, input_field in (
                ("topic_categories", "topic_categories"),
                ("source_countries", "source_country"),
                ("source_languages", "language"),
                ("coverage_regions", "coverage_region"),
                ("source_languages", "source_language"),
            ):
                values = raw.get(input_field)
                if isinstance(values, (list, tuple, set)):
                    additions = list(values)
                elif values is None:
                    additions = []
                else:
                    additions = [values]
                item[output_field] = _sorted_unique(
                    item[output_field] + additions,
                    max_items=20,
                    max_length=80,
                )

            if not item["published_at"]:
                item["publication_time_status"] = "unknown"
                item["timestamp_basis"] = "index_time_not_publication_time"
            elif item["indexed_at"]:
                item["timestamp_basis"] = "publisher_publication_plus_gdelt_index_observation"
            else:
                item["timestamp_basis"] = "publisher_publication_time"

            item["source_type"] = (
                item["source_types"][0] if len(item["source_types"]) == 1
                else "multi_source_article"
            )
            item["source_count"] = len(item["source_names"])
            item["source_type_count"] = len(item["source_types"])
            item["publisher_observation_count"] = len(item["publisher_ids_observed"])

    rows = list(merged.values())

    def order_key(item: dict[str, Any]) -> tuple[float, str]:
        stamp = (
            _parse_time(item.get("published_at"))
            or _parse_time(item.get("indexed_at"))
            or _parse_time(item.get("fetched_at"))
        )
        return (stamp.timestamp() if stamp else 0.0, str(item.get("id") or ""))

    rows.sort(key=order_key)
    return rows[-max_items:]


def summarize_news_source_coverage(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe source/region/language coverage without equating breadth with truth."""
    source_types: set[str] = set()
    publishers: set[str] = set()
    regions: set[str] = set()
    languages: set[str] = set()
    indexed = 0
    publication_timed = 0
    unknown_publication_time = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        source_types.update(_sorted_unique(item.get("source_types") or item.get("source_type")))
        if item.get("publisher_id"):
            publishers.add(str(item["publisher_id"])[:120])
        regions.update(_sorted_unique(item.get("coverage_regions")))
        languages.update(_sorted_unique(item.get("source_languages") or item.get("source_language")))
        if _parse_time(item.get("indexed_at")):
            indexed += 1
        if _parse_time(item.get("published_at")):
            publication_timed += 1
        else:
            unknown_publication_time += 1
    return {
        "unique_article_count": len(items),
        "source_types": sorted(source_types),
        "distinct_publisher_count": len(publishers),
        "coverage_regions_observed": sorted(regions),
        "source_languages_observed": sorted(languages),
        "articles_with_publisher_publication_time": publication_timed,
        "articles_with_index_time_only_or_unknown_publication_time": unknown_publication_time,
        "articles_with_index_observation_time": indexed,
        "interpretation": "observed_feed_and_index_coverage_not_global_completeness_or_event_verification",
        "research_only": True,
        "execution": "none",
    }


__all__ = ["canonical_article_url", "merge_news_sources", "summarize_news_source_coverage"]
