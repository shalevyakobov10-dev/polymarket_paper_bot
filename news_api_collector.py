"""Optional rotating NewsAPI Everything collector for research/Paper mode.

Disabled by default. The API key is read from NEWSAPI_API_KEY and sent only
through the X-Api-Key header; it is never added to the URL, archive, status,
or log output. Requests rotate through a bounded topic/language matrix.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from news_fusion import canonical_article_url
from publisher_identity import publisher_identity


NEWS_API_EVERYTHING_URL = "https://newsapi.org/v2/everything"
SUPPORTED_LANGUAGES = (
    "ar", "de", "en", "es", "fr", "he", "it",
    "nl", "no", "pt", "ru", "sv", "ud", "zh",
)
TOPIC_QUERIES = (
    ("geopolitics_security", '(election OR sanctions OR war OR ceasefire OR parliament OR diplomacy)'),
    ("macroeconomics_finance", '(central bank OR inflation OR interest rates OR bond yields OR recession)'),
    ("business_trade", '(trade OR tariffs OR supply chain OR merger OR export OR import)'),
    ("technology_cyber_ai", '(artificial intelligence OR cybersecurity OR cyberattack OR semiconductor OR chip)'),
    ("energy_commodities", '(oil OR natural gas OR electricity OR OPEC OR commodity prices)'),
    ("climate_disasters_health", '(earthquake OR wildfire OR flood OR outbreak OR pandemic OR climate)'),
)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_error_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {
        "api_key_invalid", "api_key_disabled", "api_key_exhausted",
        "rate_limited", "parameter_invalid", "sources_too_many",
        "unexpected_error", "maximum_results_reached",
    }:
        return raw
    return "newsapi_request_failed"


class NewsAPICollector:
    """One bounded poll of NewsAPI Everything; no account/trading capability."""

    def __init__(
        self,
        log_path: str | Path,
        *,
        api_key: str | None = None,
        enabled: bool | None = None,
        languages: tuple[str, ...] | list[str] | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_provider: Callable[[], datetime] | None = None,
        request_timeout_seconds: float = 12.0,
        max_items: int = 5000,
    ) -> None:
        if not callable(sleep) or not callable(monotonic):
            raise ValueError("NewsAPI timing hooks must be callable")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not 2.0 <= float(request_timeout_seconds) <= 30.0
        ):
            raise ValueError("request_timeout_seconds must be between 2 and 30")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 100 <= max_items <= 20_000:
            raise ValueError("max_items must be between 100 and 20000")
        selected_languages = tuple(languages or SUPPORTED_LANGUAGES)
        if not selected_languages or any(
            not isinstance(language, str) or language not in SUPPORTED_LANGUAGES
            for language in selected_languages
        ):
            raise ValueError("NewsAPI languages must be documented two-letter language codes")

        self.log_path = Path(log_path).expanduser()
        self.state_path = self.log_path.with_suffix(self.log_path.suffix + ".state.json")
        self.api_key = api_key if api_key is not None else os.getenv("NEWSAPI_API_KEY", "")
        self.enabled = (
            str(os.getenv("NEWSAPI_ENABLED", "false")).strip().lower()
            in {"1", "true", "yes", "on"}
            if enabled is None else enabled is True
        )
        self.languages = selected_languages
        self._sleep = sleep
        self._monotonic = monotonic
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_items = max_items
        self.requests_per_poll = _bounded_int("NEWSAPI_REQUESTS_PER_POLL", 2, 1, 6)
        self.page_size = _bounded_int("NEWSAPI_PAGE_SIZE", 100, 1, 100)
        self.minimum_request_interval_seconds = self._request_interval()
        self._last_request_monotonic: float | None = None
        self.items: list[dict[str, Any]] = []
        self.seen_ids: set[str] = set()
        self.next_combination_index = 0
        self._load_state()
        self._load_items()

    @staticmethod
    def _request_interval() -> float:
        try:
            value = float(os.getenv("NEWSAPI_MIN_REQUEST_INTERVAL_SECONDS", "1.0"))
        except (TypeError, ValueError, OverflowError):
            value = 1.0
        return value if 0.25 <= value <= 30.0 else 1.0

    @property
    def combinations(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (topic, query, language)
            for topic, query in TOPIC_QUERIES
            for language in self.languages
        )

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            value = raw.get("next_combination_index", 0) if isinstance(raw, dict) else 0
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.next_combination_index = value % len(self.combinations)
        except (OSError, UnicodeError, ValueError, TypeError):
            self.next_combination_index = 0

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "next_combination_index": self.next_combination_index,
            "updated_at": _parse_time(self._now_provider()).isoformat(),
            "schema_version": 1,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _load_items(self) -> None:
        if not self.log_path.exists():
            return
        try:
            with self.log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict) and item.get("id") and item.get("url"):
                        normalized = dict(item)
                        item_id = str(normalized["id"])
                        if item_id not in self.seen_ids:
                            self.seen_ids.add(item_id)
                            self.items.append(normalized)
        except (OSError, UnicodeError):
            self.items = []
            self.seen_ids = set()
        self.items = self.items[-self.max_items:]
        self.seen_ids = {str(item.get("id")) for item in self.items}

    def _append(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        added: list[dict[str, Any]] = []
        for item in rows:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in self.seen_ids:
                continue
            self.seen_ids.add(item_id)
            self.items.append(item)
            added.append(item)
        if added:
            with self.log_path.open("a", encoding="utf-8") as handle:
                for item in added:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            self.items = self.items[-self.max_items:]
            self.seen_ids = {str(item.get("id")) for item in self.items}
        return added

    def _normalize_articles(
        self,
        rows: Any,
        *,
        topic: str,
        language: str,
        fetched_at: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        items: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            url = canonical_article_url(raw.get("url"))
            title = " ".join(str(raw.get("title") or "").split())
            if not url or not title or title == "[Removed]":
                continue
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            source_name = str(source.get("name") or "").strip()[:120]
            published = _parse_time(raw.get("publishedAt"))
            if not published:
                publication_status = "unknown"
                timestamp_basis = "newsapi_publication_time_unavailable"
                published_at = ""
            else:
                publication_status = "provider_reported_publication_time"
                timestamp_basis = "newsapi_published_at"
                published_at = published.isoformat()
            summary = str(raw.get("description") or raw.get("content") or "").strip()
            publisher = publisher_identity(source_name, "", url)
            identifier = hashlib.sha256(url.encode("utf-8")).hexdigest()
            items.append({
                "id": identifier,
                "url": url,
                "title": title[:400],
                "summary": summary[:1200],
                "source": source_name or publisher,
                "publisher_id": publisher or "",
                "source_type": "newsapi_everything",
                "source_types": ["newsapi_everything"],
                "source_language": language,
                "source_languages": [language],
                "coverage_region": "",
                "coverage_regions": [],
                "topic_categories": [topic],
                "published_at": published_at,
                "publication_time_status": publication_status,
                "timestamp_basis": timestamp_basis,
                "fetched_at": fetched_at,
                "directional_analysis_supported": language == "en",
                "research_only": True,
                "execution": "none",
            })
        return items

    def fetch_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or self._now_provider()
        current = _parse_time(current)
        if current is None:
            raise ValueError("NewsAPI poll time must be timezone-aware")
        fetched_at = current.isoformat()

        if not self.enabled:
            return {
                "enabled": False,
                "poll_status_available": False,
                "requests_made": 0,
                "items_added": 0,
                "items_total_recent": len(self.items),
                "latest_items": list(reversed(self.items[-500:])),
                "queries_configured": len(TOPIC_QUERIES),
                "languages_configured": list(self.languages),
                "languages_attempted": [],
                "interpretation": "newsapi_disabled_by_configuration",
                "research_only": True,
                "execution": "none",
            }
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            return {
                "enabled": True,
                "poll_status_available": True,
                "requests_made": 0,
                "items_added": 0,
                "items_total_recent": len(self.items),
                "latest_items": list(reversed(self.items[-500:])),
                "queries_configured": len(TOPIC_QUERIES),
                "languages_configured": list(self.languages),
                "languages_attempted": [],
                "query_errors": [{"code": "api_key_missing"}],
                "interpretation": "newsapi_enabled_without_api_key_fail_closed",
                "research_only": True,
                "execution": "none",
            }

        requests_made = succeeded = failed = 0
        errors: list[dict[str, Any]] = []
        collected: list[dict[str, Any]] = []
        combinations = self.combinations
        attempted_languages: set[str] = set()
        attempted_topics: set[str] = set()
        from_time = current - timedelta(hours=24)

        for _ in range(min(self.requests_per_poll, len(combinations))):
            combination_index = self.next_combination_index % len(combinations)
            topic, query, language = combinations[combination_index]
            attempted_languages.add(language)
            attempted_topics.add(topic)
            params = {
                "q": query,
                "language": language,
                "sortBy": "publishedAt",
                "from": from_time.isoformat().replace("+00:00", "Z"),
                "to": current.isoformat().replace("+00:00", "Z"),
                "pageSize": self.page_size,
                "page": 1,
            }
            request = Request(
                f"{NEWS_API_EVERYTHING_URL}?{urlencode(params)}",
                headers={
                    "X-Api-Key": self.api_key.strip(),
                    "User-Agent": "PolymarketPaperResearchBot/1.0",
                    "Accept": "application/json",
                },
            )
            try:
                if self._last_request_monotonic is not None:
                    elapsed = self._monotonic() - self._last_request_monotonic
                    if not isinstance(elapsed, (int, float)) or elapsed < 0:
                        raise RuntimeError("NewsAPI monotonic clock returned an invalid value")
                    wait = self.minimum_request_interval_seconds - elapsed
                    if wait > 0:
                        self._sleep(wait)
                started = self._monotonic()
                if not isinstance(started, (int, float)) or started < 0:
                    raise RuntimeError("NewsAPI monotonic clock returned an invalid value")
                self._last_request_monotonic = started
                requests_made += 1
                with urlopen(request, timeout=self.request_timeout_seconds) as response:
                    if getattr(response, "status", 200) != 200:
                        raise RuntimeError("newsapi_non_200_response")
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    code = _safe_error_code(payload.get("code")) if isinstance(payload, dict) else "invalid_response"
                    raise ValueError(code)
                articles = payload.get("articles")
                if not isinstance(articles, list):
                    raise ValueError("invalid_articles_collection")
                collected.extend(self._normalize_articles(
                    articles, topic=topic, language=language, fetched_at=fetched_at
                ))
                succeeded += 1
            except HTTPError as exc:
                failed += 1
                errors.append({"code": f"http_{int(exc.code)}", "topic": topic, "language": language})
            except URLError:
                failed += 1
                errors.append({"code": "network_error", "topic": topic, "language": language})
            except Exception as exc:
                failed += 1
                code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                errors.append({"code": _safe_error_code(code), "topic": topic, "language": language})
            finally:
                self.next_combination_index = (combination_index + 1) % len(combinations)

        added = self._append(collected)
        self._save_state()
        return {
            "enabled": True,
            "poll_status_available": True,
            "requests_made": requests_made,
            "queries_succeeded": succeeded,
            "queries_failed": failed,
            "queries_configured": len(TOPIC_QUERIES),
            "topics_attempted": sorted(attempted_topics),
            "languages_configured": list(self.languages),
            "languages_attempted": sorted(attempted_languages),
            "items_added": len(added),
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-500:])),
            "query_errors": errors[:20],
            "research_only": True,
            "execution": "none",
        }

    def read_archive_snapshot(self) -> dict[str, Any]:
        """Read local records without a new NewsAPI request."""
        languages = sorted({
            str(item.get("source_language") or "").lower()
            for item in self.items if item.get("source_language")
        })
        return {
            "enabled": self.enabled,
            "poll_status_available": False,
            "requests_made": 0,
            "items_added": 0,
            "items_total_recent": len(self.items),
            "latest_items": list(reversed(self.items[-500:])),
            "languages_seen": languages,
            "queries_configured": len(TOPIC_QUERIES),
            "interpretation": "shared_newsapi_archive_snapshot_not_a_new_poll",
            "research_only": True,
            "execution": "none",
        }


__all__ = ["NewsAPICollector", "SUPPORTED_LANGUAGES", "TOPIC_QUERIES"]
