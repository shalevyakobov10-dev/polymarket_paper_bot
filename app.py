"""Public-market research worker with persistent paper simulation only."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from paper_trading import PaperTrader, rotate_jsonl_file
from news_monitor import DEFAULT_FEEDS, NewsCollector, NewsMarketMatcher

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "60")))
try:
    NEWS_POLL_SECONDS = min(3600, max(60, int(os.getenv("NEWS_POLL_SECONDS", "300"))))
except (ValueError, TypeError):
    NEWS_POLL_SECONDS = 300
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
SNAPSHOTS = DATA_DIR / "market_snapshots.jsonl"
SIGNALS = DATA_DIR / "candidate_signals.jsonl"
_lock = threading.RLock()
_state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "markets_seen": 0,
    "markets_discovered": 0,
    "outcomes_seen": 0,
    "signals_total": 0,
    "mode": "paper_only",
    "worker_running": False,
    "paper_trading": None,
}
_previous: dict[str, float] = {}
_paper_trader: PaperTrader | None = None
_paper_state_path: Path | None = None
_news_lock = threading.RLock()
_news_collector: NewsCollector | None = None
_news_state = {
    "enabled": True,
    "worker_running": False,
    "feeds_configured": len(DEFAULT_FEEDS),
    "feeds_succeeded": 0,
    "feeds_failed": 0,
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "last_warning": None,
    "items_added_last_poll": 0,
    "items_total_recent": 0,
    "poll_interval_seconds": NEWS_POLL_SECONDS,
    "feed_errors": [],
    "matches_added_last_pass": 0,
    "market_matches_total_recent": 0,
    "last_match_check_at": None,
    "last_match_error": None,
}
_news_items_cache: list[dict] = []
_news_matches_cache: list[dict] = []
_news_matcher: NewsMarketMatcher | None = None
_latest_market_questions: list[dict] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list_field(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        value = default
    return min(maximum, max(minimum, value))


def _volume_value(market: dict) -> float:
    try:
        value = float(market.get("volume24hr") or 0.0)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return value if value >= 0 and value < float("inf") else 0.0


def fetch_markets() -> list[dict]:
    discovery_limit = _bounded_int_env("GAMMA_DISCOVERY_LIMIT", 400, 20, 500)
    markets_to_track = _bounded_int_env("MARKETS_TO_TRACK", 20, 1, 100)
    query = urlencode({"active": "true", "closed": "false", "limit": discovery_limit, "offset": 0})
    request = Request(f"{GAMMA_URL}?{query}", headers={"User-Agent": "PolymarketPaperResearchBot/1.0"})
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Gamma API returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Gamma response was not a list")
    markets = [item for item in payload if isinstance(item, dict)]
    markets.sort(key=_volume_value, reverse=True)
    with _lock:
        _state["markets_discovered"] = len(markets)
    return markets[:markets_to_track]


def parse_outcomes(markets: list[dict]) -> list[dict]:
    rows = []
    for market in markets:
        outcomes = _list_field(market.get("outcomes"))
        prices = _list_field(market.get("outcomePrices"))
        tokens = _list_field(market.get("clobTokenIds"))
        for index, price in enumerate(prices):
            try:
                observed_price = float(price)
            except (ValueError, TypeError):
                continue
            if not 0.0 <= observed_price <= 1.0:
                continue
            token_id = str(tokens[index]) if index < len(tokens) else ""
            market_id = str(market.get("conditionId") or market.get("id") or "")
            rows.append({
                "timestamp": utc_now(),
                "market_id": market_id,
                "token_id": token_id,
                "question": str(market.get("question") or "Unknown market")[:500],
                "outcome": str(outcomes[index]) if index < len(outcomes) else f"Outcome {index + 1}",
                "observed_price": observed_price,
                "volume_24h": market.get("volume24hr"),
            })
    return rows


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path == SNAPSHOTS:
        max_mb = max(1, int(os.getenv("SNAPSHOT_MAX_MB", "25")))
        backups = max(0, int(os.getenv("SNAPSHOT_BACKUPS", "4")))
    elif path == SIGNALS:
        max_mb = max(1, int(os.getenv("SIGNAL_LOG_MAX_MB", "5")))
        backups = max(0, int(os.getenv("SIGNAL_LOG_BACKUPS", "3")))
    else:
        max_mb = max(1, int(os.getenv("JSONL_LOG_MAX_MB", "5")))
        backups = max(0, int(os.getenv("JSONL_LOG_BACKUPS", "3")))
    rotate_jsonl_file(path, max_mb * 1024 * 1024, backups)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def get_paper_trader() -> PaperTrader:
    global _paper_trader, _paper_state_path
    expected_path = (DATA_DIR / "paper_state.json").resolve()
    with _lock:
        if _paper_trader is None or _paper_state_path != expected_path:
            _paper_trader = PaperTrader(expected_path)
            _paper_state_path = expected_path
        return _paper_trader


def poll_once() -> dict:
    with _lock:
        _state["last_poll_at"] = utc_now()
    markets = fetch_markets()
    with _lock:
        _latest_market_questions[:] = [
            {
                "market_id": str(market.get("conditionId") or market.get("id") or ""),
                "question": str(market.get("question") or "")[:500],
                "volume_24h": market.get("volume24hr"),
            }
            for market in markets
            if (market.get("conditionId") or market.get("id")) and market.get("question")
        ]
    rows = parse_outcomes(markets)
    trader = get_paper_trader()
    paper_result = trader.process_snapshot(rows)
    signal_delta = trader.config.signal_delta
    found = []
    for row in rows:
        key = row["token_id"] or f'{row["market_id"]}:{row["outcome"]}'
        old_price = _previous.get(key)
        _previous[key] = row["observed_price"]
        append_jsonl(SNAPSHOTS, row)
        if old_price is not None:
            delta = row["observed_price"] - old_price
            if abs(delta) >= signal_delta:
                candidate = {
                    **row,
                    "previous_observed_price": old_price,
                    "price_delta": round(delta, 6),
                    "signal": "momentum_up" if delta > 0 else "momentum_down",
                    "experimental": True,
                    "execution": "none",
                }
                append_jsonl(SIGNALS, candidate)
                found.append(candidate)
    paper_status = get_paper_trader().snapshot()
    with _lock:
        _state.update({
            "last_success_at": utc_now(),
            "last_error": None,
            "markets_seen": len(markets),
            "markets_discovered": _state["markets_discovered"],
            "outcomes_seen": len(rows),
            "signals_total": _state["signals_total"] + len(found),
            "paper_trading": paper_status,
        })
    match_result = {"added": []}
    try:
        match_result = match_recent_news()
    except Exception as exc:
        with _news_lock:
            _news_state["last_match_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
    return {
        "markets": len(markets),
        "outcomes": len(rows),
        "candidates": len(found),
        "news_market_matches": len(match_result.get("added", [])),
        "paper": paper_result,
        "paper_metrics": paper_status["metrics"],
    }


def worker_loop() -> None:
    with _lock:
        _state["worker_running"] = True
    while True:
        try:
            poll_once()
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            with _lock:
                _state["last_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        except Exception as exc:  # keep the worker alive on malformed third-party data
            with _lock:
                _state["last_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:300]}"
        time.sleep(POLL_SECONDS)


def health_payload(snapshot: dict, now: datetime | None = None) -> tuple[bool, dict]:
    """Report unhealthy when the worker has not produced a recent successful poll."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    last_success = snapshot.get("last_success_at")
    age_seconds = None
    if last_success:
        try:
            parsed = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (current - parsed.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            age_seconds = None
    stale_after = max(180, POLL_SECONDS * 3)
    healthy = bool(snapshot.get("worker_running")) and age_seconds is not None and age_seconds <= stale_after
    payload = {
        "ok": healthy,
        "mode": "paper_only",
        "worker_running": bool(snapshot.get("worker_running")),
        "last_success_at": last_success,
        "seconds_since_success": round(age_seconds, 1) if age_seconds is not None else None,
        "stale_after_seconds": stale_after,
        "last_error": snapshot.get("last_error"),
    }
    if not healthy and not payload["last_error"]:
        payload["last_error"] = "No recent successful market poll"
    return healthy, payload


def get_news_collector() -> NewsCollector:
    global _news_collector
    with _news_lock:
        if _news_collector is None:
            _news_collector = NewsCollector(DATA_DIR / "news_events.jsonl")
        return _news_collector


def get_news_matcher() -> NewsMarketMatcher:
    global _news_matcher
    with _news_lock:
        if _news_matcher is None:
            _news_matcher = NewsMarketMatcher(DATA_DIR / "news_market_candidates.jsonl")
        return _news_matcher


def match_recent_news() -> dict:
    """Link recent headlines to tracked market questions as non-trading research candidates."""
    with _lock:
        markets = list(_latest_market_questions)
    with _news_lock:
        news_items = list(_news_items_cache)
        existing_matches = list(_news_matches_cache)
    if not markets or not news_items:
        return {"added": [], "items_total_recent": len(existing_matches), "latest": existing_matches}
    result = get_news_matcher().match(news_items, markets)
    with _news_lock:
        _news_matches_cache[:] = result["latest"][:100]
        _news_state["matches_added_last_pass"] = len(result["added"])
        _news_state["market_matches_total_recent"] = result["items_total_recent"]
        _news_state["last_match_check_at"] = utc_now()
        _news_state["last_match_error"] = None
    return result


def news_worker_loop() -> None:
    """Continuously collect attributed headlines without blocking market polling."""
    with _news_lock:
        _news_state["worker_running"] = True
    while True:
        attempted_at = utc_now()
        with _news_lock:
            _news_state["last_poll_at"] = attempted_at
        try:
            result = get_news_collector().fetch_once()
            with _news_lock:
                _news_state.update({
                    "feeds_configured": result["feeds_configured"],
                    "feeds_succeeded": result["feeds_succeeded"],
                    "feeds_failed": result["feeds_failed"],
                    "items_added_last_poll": result["items_added"],
                    "items_total_recent": result["items_total_recent"],
                    "feed_errors": result["feed_errors"][:10],
                    "last_warning": (
                        str(result["feeds_failed"]) + " news feed(s) failed"
                        if result["feeds_failed"] else None
                    ),
                })
                _news_items_cache[:] = result["latest_items"][:100]
                if result["feeds_succeeded"] > 0:
                    _news_state["last_success_at"] = utc_now()
                    _news_state["last_error"] = None
                else:
                    _news_state["last_error"] = "All configured news feeds failed"
            try:
                match_recent_news()
            except Exception as exc:
                with _news_lock:
                    _news_state["last_match_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
        except Exception as exc:
            with _news_lock:
                _news_state["last_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:300]}"
        time.sleep(NEWS_POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/status", "/health", "/paper", "/news", "/matches"):
            with _lock:
                snapshot = dict(_state)
            if self.path == "/health":
                healthy, payload = health_payload(snapshot)
                self._send(200 if healthy else 503, payload)
            elif self.path == "/paper":
                self._send(200, get_paper_trader().snapshot())
            elif self.path == "/news":
                with _news_lock:
                    news_snapshot = {**_news_state, "items": list(_news_items_cache)}
                self._send(200, news_snapshot)
            elif self.path == "/matches":
                with _news_lock:
                    match_snapshot = {
                        "method": "keyword_overlap_v1",
                        "research_only": True,
                        "execution": "none",
                        "total_recent": _news_state["market_matches_total_recent"],
                        "last_match_check_at": _news_state["last_match_check_at"],
                        "last_match_error": _news_state["last_match_error"],
                        "items": list(_news_matches_cache),
                    }
                self._send(200, match_snapshot)
            else:
                with _news_lock:
                    news_snapshot = dict(_news_state)
                    news_snapshot["feed_errors"] = list(_news_state["feed_errors"])
                self._send(200, {"service": "Polymarket Paper Research Bot", **snapshot, "news": news_snapshot})
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        return


def main() -> None:
    threading.Thread(target=worker_loop, daemon=True, name="market-poller").start()
    threading.Thread(target=news_worker_loop, daemon=True, name="global-news-poller").start()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Paper research worker listening on port {port}; polling every {POLL_SECONDS}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
