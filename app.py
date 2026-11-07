"""Public-market research worker with persistent paper simulation only."""
from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from paper_trading import PaperConfig, PaperTrader, rotate_jsonl_file
from market_data import fetch_order_books
from market_quality import evaluate_order_book_quality
from market_stream import MarketStreamCollector
from forecast_evaluation import evaluate_jsonl_files
from resolution_tracker import ResolutionCollector
from global_news import DEFAULT_TOPIC_QUERIES, GDELTCollector
from news_monitor import DEFAULT_FEEDS, NewsCollector, NewsMarketMatcher, refresh_match_freshness, summarize_market_corroboration, summarize_match_freshness, summarize_news_freshness
from research_features import build_market_feature_rows, read_recent_jsonl, summarize_feature_rows
from independent_model_evaluation import evaluate_feature_files, forecast_current_feature_files
from learning_journal import LearningJournal
from model_governance import ModelGovernanceLedger
from execution_readiness import execution_readiness_payload
from resilience import exponential_backoff_seconds, parse_retry_after
from supervisor_status import read_supervisor_status
from event_paper_worker import read_event_paper_worker_status

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "60")))
try:
    EVENT_PAPER_POLL_SECONDS = min(86_400, max(30, int(os.getenv("PAPER_POLL_SECONDS", "300"))))
except (ValueError, TypeError):
    EVENT_PAPER_POLL_SECONDS = 300
try:
    NEWS_POLL_SECONDS = min(3600, max(60, int(os.getenv("NEWS_POLL_SECONDS", "300"))))
except (ValueError, TypeError):
    NEWS_POLL_SECONDS = 300
try:
    RESOLUTION_POLL_SECONDS = min(3600, max(300, int(os.getenv("RESOLUTION_POLL_SECONDS", "900"))))
except (ValueError, TypeError):
    RESOLUTION_POLL_SECONDS = 900
try:
    MODEL_LEARNING_POLL_SECONDS = min(3600, max(300, int(os.getenv("MODEL_LEARNING_POLL_SECONDS", "900"))))
except (ValueError, TypeError):
    MODEL_LEARNING_POLL_SECONDS = 900
try:
    MODEL_EVALUATION_SECONDS = min(86400, max(3600, int(os.getenv("MODEL_EVALUATION_SECONDS", "21600"))))
except (ValueError, TypeError):
    MODEL_EVALUATION_SECONDS = 21600
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
SUPERVISOR_STATUS_FILE = Path(os.getenv("SERVICE_SUPERVISOR_STATUS_FILE", str(DATA_DIR / "service_supervisor_status.json")))
EVENT_PAPER_WORKER_STATUS_FILE = Path(os.getenv("EVENT_PAPER_WORKER_STATUS_FILE", str(DATA_DIR / "event_paper_worker_status.json")))
SNAPSHOTS = DATA_DIR / "market_snapshots.jsonl"
SIGNALS = DATA_DIR / "candidate_signals.jsonl"
ORDERBOOK_SNAPSHOTS = DATA_DIR / "orderbook_snapshots.jsonl"
RESOLUTIONS = DATA_DIR / "resolved_markets.jsonl"
RESEARCH_FEATURES = DATA_DIR / "research_features.jsonl"
MODEL_FORECASTS = DATA_DIR / "model_forecasts.jsonl"
MODEL_FEEDBACK = DATA_DIR / "model_feedback.jsonl"
MODEL_LEARNING_EVALUATIONS = DATA_DIR / "model_learning_evaluations.jsonl"
MODEL_GOVERNANCE = DATA_DIR / "model_governance.jsonl"
try:
    RESEARCH_FEATURES_SNAPSHOT_SECONDS = min(3600, max(60, int(os.getenv("RESEARCH_FEATURES_SNAPSHOT_SECONDS", "900"))))
except (ValueError, TypeError):
    RESEARCH_FEATURES_SNAPSHOT_SECONDS = 900
_lock = threading.RLock()
_state = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "consecutive_failures": 0,
    "retry_delay_seconds": 0.0,
    "next_retry_at": None,
    "markets_seen": 0,
    "markets_discovered": 0,
    "outcomes_seen": 0,
    "orderbooks_seen": 0,
    "orderbooks_usable": 0,
    "orderbooks_failed": 0,
    "orderbooks_quality_eligible": 0,
    "orderbooks_quality_rejected": 0,
    "orderbook_last_success_at": None,
    "signals_total": 0,
    "research_features_written_last_poll": 0,
    "research_features_error": None,
    "mode": "paper_only",
    "worker_running": False,
    "paper_trading": None,
}
_market_stream_state = {
    "enabled": True,
    "worker_running": False,
    "consecutive_failures": 0,
    "retry_delay_seconds": 0.0,
    "next_retry_at": None,
    "connected": False,
    "last_connect_at": None,
    "last_message_at": None,
    "last_error": None,
    "subscribed_tokens": 0,
    "events_persisted_last_session": 0,
    "sessions": 0,
}
_latest_token_ids: list[str] = []
_market_stream_collector: MarketStreamCollector | None = None
_previous: dict[str, float] = {}
_paper_trader: PaperTrader | None = None
_paper_state_path: Path | None = None
_news_lock = threading.RLock()
_news_collector: NewsCollector | None = None
_news_state = {
    "enabled": True,
    "worker_running": False,
    "consecutive_failures": 0,
    "retry_delay_seconds": 0.0,
    "next_retry_at": None,
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
_gdelt_collector: GDELTCollector | None = None
_gdelt_state = {
    "enabled": True,
    "last_success_at": None,
    "last_error": None,
    "items_added_last_poll": 0,
    "items_total_recent": 0,
    "query_window": "1h",
    "topics_configured": len(DEFAULT_TOPIC_QUERIES),
    "topics_attempted": [],
    "topics_succeeded": 0,
    "topics_failed": 0,
    "topic_results": {},
    "topic_errors": [],
}
_latest_market_questions: list[dict] = []
_research_feature_last_written: dict[str, float] = {}
_research_feature_schedule_loaded = False
_learning_lock = threading.RLock()
_learning_journal: LearningJournal | None = None
_model_governance_ledger: ModelGovernanceLedger | None = None
_learning_state = {
    "enabled": True,
    "worker_running": False,
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "consecutive_failures": 0,
    "retry_delay_seconds": 0.0,
    "next_retry_at": None,
    "poll_interval_seconds": MODEL_LEARNING_POLL_SECONDS,
    "evaluation_interval_seconds": MODEL_EVALUATION_SECONDS,
    "model_status": "starting",
    "model_version": None,
    "training_markets": 0,
    "forecasts_saved_last_cycle": 0,
    "feedback_scored_last_cycle": 0,
    "last_evaluation_at": None,
    "last_evaluation_status": None,
    "live_execution_enabled": False,
}
_resolution_collector: ResolutionCollector | None = None
_resolution_state = {
    "enabled": True,
    "worker_running": False,
    "consecutive_failures": 0,
    "retry_delay_seconds": 0.0,
    "next_retry_at": None,
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "markets_checked_last_poll": 0,
    "resolutions_added_last_poll": 0,
    "resolutions_total_seen": 0,
    "paper_positions_settled_last_poll": 0,
    "paper_settlements_total_reported": 0,
    "paper_settlement_skipped_last_poll": {},
    "paper_settlement_conflicts_last_poll": [],
    "paper_settlement_warning": None,
    "poll_interval_seconds": RESOLUTION_POLL_SECONDS,
}


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


POLL_MAX_BACKOFF_SECONDS = max(POLL_SECONDS, _bounded_int_env("POLL_MAX_BACKOFF_SECONDS", 900, 60, 3600))


def _research_feature_rotation_paths() -> list[Path]:
    backups = _bounded_int_env("RESEARCH_FEATURES_LOG_BACKUPS", 5, 1, 10)
    return [
        RESEARCH_FEATURES.with_name(f"{RESEARCH_FEATURES.name}.{index}")
        for index in range(backups, 0, -1)
    ] + [RESEARCH_FEATURES]


def _read_feature_timestamp(value: object) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def load_research_feature_schedule() -> None:
    """Load recent per-market timestamps once so restarts do not bypass sampling cadence."""
    global _research_feature_schedule_loaded
    if _research_feature_schedule_loaded:
        return
    newest_first = list(reversed(_research_feature_rotation_paths()))
    records = read_recent_jsonl(newest_first, limit=5000)
    for record in records:
        market_id = str(record.get("market_id") or "").strip()
        stamp = _read_feature_timestamp(record.get("observed_at"))
        if market_id and stamp is not None:
            _research_feature_last_written[market_id] = max(
                stamp, _research_feature_last_written.get(market_id, 0.0)
            )
    _research_feature_schedule_loaded = True


def _volume_value(market: dict) -> float:
    try:
        value = float(market.get("volume24hr") or 0.0)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return value if value >= 0 and value < float("inf") else 0.0


def fetch_markets() -> list[dict]:
    """Discover active markets across bounded Gamma pages, then rank by 24h volume."""
    page_size = _bounded_int_env("GAMMA_DISCOVERY_LIMIT", 400, 20, 500)
    max_pages = _bounded_int_env("GAMMA_DISCOVERY_PAGES", 5, 1, 10)
    markets_to_track = _bounded_int_env("MARKETS_TO_TRACK", 20, 1, 100)
    discovered: dict[str, dict] = {}
    for page in range(max_pages):
        query = urlencode({
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": page * page_size,
        })
        request = Request(
            f"{GAMMA_URL}?{query}",
            headers={"User-Agent": "PolymarketPaperResearchBot/1.0"},
        )
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Gamma API returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Gamma response was not a list")
        page_markets = [item for item in payload if isinstance(item, dict)]
        for index, market in enumerate(page_markets):
            market_id = str(market.get("conditionId") or market.get("id") or "").strip()
            if not market_id:
                # Preserve anonymous records only within this page; they cannot be
                # safely deduplicated across pages without a stable market ID.
                market_id = f"page-{page}-item-{index}"
            discovered.setdefault(market_id, market)
        if len(payload) < page_size:
            break
    markets = list(discovered.values())
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
                "fees_enabled": market.get("feesEnabled"),
                "fee_schedule": market.get("feeSchedule"),
                "fee_type": market.get("feeType"),
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
    elif path == RESEARCH_FEATURES:
        max_mb = _bounded_int_env("RESEARCH_FEATURES_LOG_MAX_MB", 50, 10, 500)
        backups = _bounded_int_env("RESEARCH_FEATURES_LOG_BACKUPS", 5, 1, 10)
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
            paper_config = PaperConfig.from_env(require_orderbook_default=True)
            _paper_trader = PaperTrader(expected_path, paper_config)
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
    with _lock:
        _latest_token_ids[:] = list(dict.fromkeys(
            str(row.get("token_id") or "") for row in rows if row.get("token_id")
        ))[:200]
    # Read-only top-of-book snapshots enrich research data; Gamma prices remain
    # the signal/mark input. A separate quality gate protects new paper entries.
    books = fetch_order_books(rows)
    trader = get_paper_trader()
    usable_books = 0
    failed_books = 0
    quality_eligible = 0
    quality_rejected = 0
    quality_now = datetime.now(timezone.utc)
    for row in rows:
        book = books.get(row.get("token_id"))
        if book:
            row["orderbook"] = book
            append_jsonl(ORDERBOOK_SNAPSHOTS, book)
            if book.get("book_usable"):
                usable_books += 1
            elif book.get("error"):
                failed_books += 1
        quality = evaluate_order_book_quality(
            row.get("orderbook"),
            now=quality_now,
            max_age_seconds=trader.config.max_orderbook_age_seconds,
            max_spread_bps=trader.config.max_orderbook_spread_bps,
            min_side_depth_usd=trader.config.min_orderbook_side_depth_usd,
            require_orderbook=trader.config.require_orderbook,
        )
        row["market_quality"] = quality
        if quality["eligible"]:
            quality_eligible += 1
        else:
            quality_rejected += 1
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
            "orderbooks_seen": len(books),
            "orderbooks_usable": usable_books,
            "orderbooks_failed": failed_books,
            "orderbooks_quality_eligible": quality_eligible,
            "orderbooks_quality_rejected": quality_rejected,
            "orderbook_last_success_at": utc_now() if usable_books else _state.get("orderbook_last_success_at"),
            "signals_total": _state["signals_total"] + len(found),
            "paper_trading": paper_status,
        })
    match_result = {"added": []}
    try:
        match_result = match_recent_news()
    except Exception as exc:
        with _news_lock:
            _news_state["last_match_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
    feature_rows = []
    feature_rows_written = 0
    feature_error = None
    try:
        load_research_feature_schedule()
        feature_rows = build_market_feature_rows(rows, match_result.get("latest", []))
        for feature_row in feature_rows:
            market_id = str(feature_row.get("market_id") or "").strip()
            observed_stamp = _read_feature_timestamp(feature_row.get("observed_at"))
            if not market_id or observed_stamp is None:
                continue
            previous_stamp = _research_feature_last_written.get(market_id)
            if (
                previous_stamp is None
                or observed_stamp - previous_stamp >= RESEARCH_FEATURES_SNAPSHOT_SECONDS
            ):
                append_jsonl(RESEARCH_FEATURES, feature_row)
                _research_feature_last_written[market_id] = observed_stamp
                feature_rows_written += 1
        if len(_research_feature_last_written) > 20000:
            recent = sorted(
                _research_feature_last_written.items(), key=lambda item: item[1], reverse=True
            )[:10000]
            _research_feature_last_written.clear()
            _research_feature_last_written.update(recent)
    except Exception as exc:
        feature_error = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
    with _lock:
        _state["research_features_written_last_poll"] = feature_rows_written
        _state["research_features_error"] = feature_error
    return {
        "markets": len(markets),
        "outcomes": len(rows),
        "research_features_prepared": len(feature_rows),
        "research_features_written": feature_rows_written,
        "research_features_error": feature_error,
        "orderbooks": len(books),
        "orderbooks_usable": usable_books,
        "orderbooks_failed": failed_books,
        "orderbooks_quality_eligible": quality_eligible,
        "orderbooks_quality_rejected": quality_rejected,
        "candidates": len(found),
        "news_market_matches": len(match_result.get("added", [])),
        "paper": paper_result,
        "paper_metrics": paper_status["metrics"],
    }



def get_resolution_collector() -> ResolutionCollector:
    global _resolution_collector
    with _lock:
        if _resolution_collector is None:
            _resolution_collector = ResolutionCollector(RESOLUTIONS)
        return _resolution_collector


def forecast_evaluation_payload() -> dict:
    """Score stored pre-resolution Yes-price observations against resolved outcomes."""
    def rotated_paths(path: Path, backups: int = 4) -> list[Path]:
        return [
            path.with_name(f"{path.name}.{index}")
            for index in range(backups, 0, -1)
        ] + [path]

    return evaluate_jsonl_files(
        rotated_paths(SNAPSHOTS),
        get_resolution_collector()._rotation_paths(),
        horizon_hours=24.0,
        tolerance_hours=12.0,
    )


def research_features_payload() -> dict:
    """Return the latest point-in-time joined market/news feature row per market."""
    paths = list(reversed(_research_feature_rotation_paths()))
    records = read_recent_jsonl(paths, limit=10000)
    latest_by_market = {}
    for record in records:
        market_id = str(record.get("market_id") or "").strip()
        if not market_id:
            continue
        previous = latest_by_market.get(market_id)
        if previous is None or str(record.get("observed_at") or "") >= str(previous.get("observed_at") or ""):
            latest_by_market[market_id] = record
    items = sorted(
        latest_by_market.values(),
        key=lambda row: str(row.get("observed_at") or ""),
        reverse=True,
    )[:100]
    return {
        "mode": "RESEARCH_FEATURES_V1",
        "generated_at": utc_now(),
        "markets_with_latest_features": len(items),
        "summary": summarize_feature_rows(items),
        "items": items,
        "methodology": {
            "market_price_source": "Gamma outcomePrices; Yes and No joined by market ID",
            "book_source": "public CLOB order-book snapshot for the Yes token",
            "news_source": "market-corpus-aware news-to-market candidates with normalized publisher identity and publication-time filtering to 24h",
            "news_corroboration_caveat": "unique source labels are not verified independent reporting",
            "prediction": "none; features are descriptive inputs only",
        },
        "research_only": True,
        "forecast": None,
        "execution": "none",
    }


def current_model_forecasts_payload() -> dict:
    """Research-only current forecasts from resolved historical labels and fresh features."""
    return forecast_current_feature_files(
        _research_feature_rotation_paths(),
        get_resolution_collector()._rotation_paths(),
        minimum_training=80,
        max_feature_age_seconds=3600.0,
    )


def independent_model_evaluation_payload() -> dict:
    """Compare a feature model with market prices on a purged chronological holdout."""
    feature_paths = _research_feature_rotation_paths()
    return evaluate_feature_files(
        feature_paths,
        get_resolution_collector()._rotation_paths(),
        horizon_hours=24.0,
        tolerance_hours=12.0,
    )


def get_learning_journal() -> LearningJournal:
    global _learning_journal
    with _learning_lock:
        if _learning_journal is None:
            _learning_journal = LearningJournal(
                MODEL_FORECASTS,
                MODEL_FEEDBACK,
                MODEL_LEARNING_EVALUATIONS,
            )
        return _learning_journal


def get_model_governance_ledger() -> ModelGovernanceLedger:
    global _model_governance_ledger
    with _learning_lock:
        if _model_governance_ledger is None:
            _model_governance_ledger = ModelGovernanceLedger(MODEL_GOVERNANCE)
        return _model_governance_ledger


def model_learning_worker_loop() -> None:
    """Periodically refit on resolved history and score stored forecasts after resolution."""
    failure_count = 0
    last_evaluation_stamp = 0.0
    with _learning_lock:
        _learning_state["worker_running"] = True

    while True:
        attempted_at = utc_now()
        with _learning_lock:
            _learning_state["last_poll_at"] = attempted_at
        retry_delay = None
        try:
            journal = get_learning_journal()
            forecast = current_model_forecasts_payload()
            saved = journal.record_forecasts(
                forecast.get("forecasts") or [],
                model_version=str(forecast.get("model_version") or ""),
                generated_at=str(forecast.get("generated_at") or attempted_at),
            )
            resolution_rows = read_recent_jsonl(
                get_resolution_collector()._rotation_paths(),
                limit=100_000,
            )
            feedback = journal.score_resolved_forecasts(resolution_rows)

            evaluation_record = None
            current_stamp = time.time()
            if (
                last_evaluation_stamp == 0.0
                or current_stamp - last_evaluation_stamp >= MODEL_EVALUATION_SECONDS
            ):
                evaluation = independent_model_evaluation_payload()
                evaluation["model_version"] = forecast.get("model_version")
                evaluation_record = journal.record_evaluation(evaluation)
                get_model_governance_ledger().assess_and_record(
                    evaluation,
                    model_version=str(forecast.get("model_version") or ""),
                )
                last_evaluation_stamp = current_stamp

            summary = journal.summary()
            training = forecast.get("training") if isinstance(forecast.get("training"), dict) else {}
            with _learning_lock:
                _learning_state.update({
                    "last_success_at": utc_now(),
                    "last_error": None,
                    "consecutive_failures": 0,
                    "retry_delay_seconds": 0.0,
                    "next_retry_at": None,
                    "model_status": str(forecast.get("status") or "unknown"),
                    "model_version": forecast.get("model_version"),
                    "training_markets": int(training.get("eligible_unique_markets") or 0),
                    "forecasts_saved_last_cycle": int(saved.get("added") or 0),
                    "feedback_scored_last_cycle": int(feedback.get("scored_added") or 0),
                    "feedback_forecasts_scored_total": int(summary.get("forecasts_scored") or 0),
                    "model_versions_with_feedback": int(summary.get("model_versions_with_feedback") or 0),
                    "last_evaluation_at": (
                        evaluation_record.get("evaluated_at")
                        if evaluation_record else _learning_state.get("last_evaluation_at")
                    ),
                    "last_evaluation_status": (
                        evaluation_record.get("status")
                        if evaluation_record else _learning_state.get("last_evaluation_status")
                    ),
                    "last_evaluation_walk_forward_status": (
                        evaluation_record.get("walk_forward_status")
                        if evaluation_record else _learning_state.get("last_evaluation_walk_forward_status")
                    ),
                })
            failure_count = 0
        except Exception as exc:
            failure_count += 1
            retry_delay = _worker_retry_delay(
                exc, failure_count, base_seconds=MODEL_LEARNING_POLL_SECONDS
            )
            with _learning_lock:
                _learning_state.update({
                    "last_error": f"Unexpected {type(exc).__name__}: {str(exc)[:300]}",
                    "consecutive_failures": failure_count,
                    "retry_delay_seconds": retry_delay,
                    "next_retry_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
                    ).isoformat(),
                })
        time.sleep(retry_delay if retry_delay is not None else MODEL_LEARNING_POLL_SECONDS)


def settle_open_paper_positions(
    collector: ResolutionCollector,
    trader: PaperTrader,
    newly_fetched_resolutions: list[dict],
) -> dict:
    """Settle currently open paper positions from fresh and retained resolutions."""
    positions = trader.snapshot().get("positions_list", [])
    market_ids = {
        str(position.get("market_id") or "").strip()
        for position in positions
        if isinstance(position, dict) and str(position.get("market_id") or "").strip()
    }
    retained = collector.load_resolutions(market_ids)
    merged: dict[str, dict] = {}
    conflicting: set[str] = set()
    # Retained records provide restart recovery. A newly fetched result may
    # refresh its timestamp, but it must never silently override a different
    # already-persisted binary outcome.
    for record in retained + list(newly_fetched_resolutions or []):
        if not isinstance(record, dict):
            continue
        market_id = str(record.get("market_id") or "").strip()
        outcome = record.get("resolved_yes")
        if (
            market_id not in market_ids
            or isinstance(outcome, bool)
            or not isinstance(outcome, int)
            or outcome not in (0, 1)
        ):
            continue
        previous = merged.get(market_id)
        if previous is not None and previous.get("resolved_yes") != outcome:
            conflicting.add(market_id)
            continue
        if market_id not in conflicting:
            previous_stamp = str(previous.get("resolved_at") or "") if previous else ""
            current_stamp = str(record.get("resolved_at") or "")
            if previous is None or current_stamp > previous_stamp:
                merged[market_id] = record
    rows_to_settle = [
        record for market_id, record in merged.items()
        if market_id not in conflicting
    ]
    result = trader.settle_resolved_positions(rows_to_settle)
    skipped = result.setdefault("skipped", {})
    skipped["conflicting_persisted_resolution"] = len(conflicting)
    result["conflicting_market_ids"] = sorted(conflicting)
    return result


def resolution_worker_loop() -> None:
    """Collect public decisive binary resolutions for the forecast evaluation scorecard."""
    failure_count = 0
    with _lock:
        _resolution_state["worker_running"] = True
    while True:
        with _lock:
            _resolution_state["last_poll_at"] = utc_now()
        try:
            collector = get_resolution_collector()
            result = collector.fetch_once()
            settlement = settle_open_paper_positions(
                collector,
                get_paper_trader(),
                result.get("resolved_markets") or [],
            )
            failure_count = 0
            with _lock:
                _resolution_state.update({
                    "last_success_at": result.get("last_success_at"),
                    "last_error": None,
                    "consecutive_failures": 0,
                    "retry_delay_seconds": 0.0,
                    "next_retry_at": None,
                    "markets_checked_last_poll": int(result.get("markets_checked") or 0),
                    "resolutions_added_last_poll": int(result.get("resolutions_added") or 0),
                    "resolutions_total_seen": int(result.get("resolutions_total_seen") or 0),
                    "paper_positions_settled_last_poll": int(settlement.get("settled_count") or 0),
                    "paper_settlements_total_reported": (
                        int(_resolution_state.get("paper_settlements_total_reported") or 0)
                        + int(settlement.get("settled_count") or 0)
                    ),
                    "paper_settlement_skipped_last_poll": dict(settlement.get("skipped") or {}),
                    "paper_settlement_conflicts_last_poll": list(
                        settlement.get("conflicting_market_ids") or []
                    ),
                    "paper_settlement_warning": (
                        "One or more markets were withheld from automatic paper settlement because resolution evidence conflicted or was invalid."
                        if (
                            int((settlement.get("skipped") or {}).get("conflicting_persisted_resolution") or 0)
                            or int((settlement.get("skipped") or {}).get("conflicting_resolution") or 0)
                            or int((settlement.get("skipped") or {}).get("invalid_resolution") or 0)
                        ) else None
                    ),
                })
        except Exception as exc:
            failure_count += 1
            delay = _worker_retry_delay(exc, failure_count, base_seconds=POLL_SECONDS)
            with _lock:
                _resolution_state.update({
                    "last_error": f"Unexpected {type(exc).__name__}: {str(exc)[:300]}",
                    "consecutive_failures": failure_count,
                    "retry_delay_seconds": delay,
                    "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                })
            time.sleep(delay)
            continue
        time.sleep(RESOLUTION_POLL_SECONDS)


def get_market_stream_collector() -> MarketStreamCollector:
    global _market_stream_collector
    with _lock:
        if _market_stream_collector is None:
            _market_stream_collector = MarketStreamCollector(DATA_DIR / "market_stream_events.jsonl")
        return _market_stream_collector


def market_stream_worker_loop() -> None:
    """Reconnect bounded public market subscriptions with exponential backoff on errors."""
    failure_count = 0
    with _lock:
        _market_stream_state["worker_running"] = True
    while True:
        with _lock:
            tokens = list(_latest_token_ids)
        if not tokens:
            time.sleep(5)
            continue
        try:
            result = get_market_stream_collector().run_session(tokens, max_seconds=45)
        except Exception as exc:
            failure_count += 1
            delay = _worker_retry_delay(exc, failure_count, base_seconds=5)
            with _lock:
                _market_stream_state.update({
                    "connected": False,
                    "last_error": f"Unexpected {type(exc).__name__}: {str(exc)[:300]}",
                    "consecutive_failures": failure_count,
                    "retry_delay_seconds": delay,
                    "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                    "sessions": int(_market_stream_state["sessions"]) + 1,
                })
            time.sleep(delay)
            continue
        connected = bool(result.get("connected"))
        if connected:
            failure_count = 0
        else:
            failure_count += 1
        delay = 0.0 if connected else _worker_retry_delay(
            RuntimeError(str(result.get("reason") or "Market stream disconnected")),
            failure_count,
            base_seconds=5,
        )
        with _lock:
            _market_stream_state.update({
                "connected": connected,
                "last_connect_at": utc_now() if connected else _market_stream_state.get("last_connect_at"),
                "last_message_at": result.get("last_message_at") or _market_stream_state.get("last_message_at"),
                "last_error": result.get("reason"),
                "consecutive_failures": failure_count,
                "retry_delay_seconds": delay,
                "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat() if delay else None,
                "subscribed_tokens": int(result.get("subscribed_tokens") or 0),
                "events_persisted_last_session": int(result.get("events_persisted") or 0),
                "sessions": int(_market_stream_state["sessions"]) + 1,
            })
        if not connected:
            time.sleep(delay)


def _worker_retry_delay(exc: Exception, failure_count: int, *, base_seconds: int) -> float:
    """Share one bounded retry policy across all external-data workers."""
    headers = getattr(exc, "headers", None)
    retry_after_header = headers.get("Retry-After") if headers is not None else None
    return exponential_backoff_seconds(
        failure_count,
        base_seconds=base_seconds,
        max_seconds=max(base_seconds, min(3600, POLL_MAX_BACKOFF_SECONDS)),
        jitter_fraction=0.15,
        random_value=random.random(),
        retry_after_seconds=parse_retry_after(retry_after_header),
    )


def _record_market_worker_failure(exc: Exception, failure_count: int, *, unexpected: bool = False) -> float:
    """Record a failed market poll and calculate a bounded retry delay."""
    delay = _worker_retry_delay(exc, failure_count, base_seconds=POLL_SECONDS)
    message = (
        f"Unexpected {type(exc).__name__}: {str(exc)[:300]}"
        if unexpected else f"{type(exc).__name__}: {str(exc)[:300]}"
    )
    next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
    with _lock:
        _state.update({
            "last_error": message,
            "consecutive_failures": failure_count,
            "retry_delay_seconds": delay,
            "next_retry_at": next_retry.isoformat(),
        })
    return delay


def worker_loop() -> None:
    """Poll public market data, backing off safely during upstream outages/rate limits."""
    failure_count = 0
    with _lock:
        _state["worker_running"] = True
    while True:
        try:
            poll_once()
            failure_count = 0
            with _lock:
                _state.update({
                    "consecutive_failures": 0,
                    "retry_delay_seconds": 0.0,
                    "next_retry_at": None,
                })
        except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            failure_count += 1
            time.sleep(_record_market_worker_failure(exc, failure_count))
            continue
        except Exception as exc:  # keep the worker alive on malformed third-party data
            failure_count += 1
            time.sleep(_record_market_worker_failure(exc, failure_count, unexpected=True))
            continue
        time.sleep(POLL_SECONDS)


def _health_component(
    *,
    name: str,
    worker_running: bool,
    last_success_at: object,
    stale_after_seconds: int,
    now: datetime,
    last_error: object = None,
) -> dict:
    """Describe one collector without allowing optional feeds to mask core health."""
    if not worker_running:
        status = "stopped"
        age_seconds = None
    elif not last_success_at:
        status = "starting"
        age_seconds = None
    else:
        try:
            parsed = datetime.fromisoformat(str(last_success_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = (now - parsed.astimezone(timezone.utc)).total_seconds()
            if age_seconds < -5.0:
                status = "future_timestamp"
            elif age_seconds > stale_after_seconds:
                status = "stale"
            else:
                status = "healthy"
                age_seconds = max(0.0, age_seconds)
        except (TypeError, ValueError, OverflowError):
            status = "invalid_timestamp"
            age_seconds = None
    return {
        "name": name,
        "status": status,
        "worker_running": bool(worker_running),
        "last_success_at": last_success_at,
        "seconds_since_success": round(age_seconds, 1) if age_seconds is not None else None,
        "stale_after_seconds": stale_after_seconds,
        "last_error": str(last_error)[:300] if last_error else None,
    }


def health_payload(
    snapshot: dict,
    now: datetime | None = None,
    *,
    news_snapshot: dict | None = None,
    stream_snapshot: dict | None = None,
    resolution_snapshot: dict | None = None,
    gdelt_snapshot: dict | None = None,
    learning_snapshot: dict | None = None,
    paper_worker_snapshot: dict | None = None,
) -> tuple[bool, dict]:
    """Report core market readiness and separately expose optional collector degradation."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    market_stale_after = max(180, POLL_SECONDS * 3)
    news_stale_after = max(900, NEWS_POLL_SECONDS * 3)
    resolution_stale_after = max(1800, RESOLUTION_POLL_SECONDS * 3)
    stream_stale_after = max(180, POLL_SECONDS * 3)

    market_component = _health_component(
        name="market_data",
        worker_running=bool(snapshot.get("worker_running")),
        last_success_at=snapshot.get("last_success_at"),
        stale_after_seconds=market_stale_after,
        now=current,
        last_error=snapshot.get("last_error"),
    )
    components = {"market_data": market_component}

    if news_snapshot is not None:
        components["news"] = _health_component(
            name="news",
            worker_running=bool(news_snapshot.get("worker_running")),
            last_success_at=news_snapshot.get("last_success_at"),
            stale_after_seconds=news_stale_after,
            now=current,
            last_error=news_snapshot.get("last_error"),
        )

    if resolution_snapshot is not None:
        components["resolutions"] = _health_component(
            name="resolutions",
            worker_running=bool(resolution_snapshot.get("worker_running")),
            last_success_at=resolution_snapshot.get("last_success_at"),
            stale_after_seconds=resolution_stale_after,
            now=current,
            last_error=resolution_snapshot.get("last_error"),
        )

    if gdelt_snapshot is not None:
        gdelt_component = _health_component(
            name="gdelt",
            worker_running=bool(news_snapshot and news_snapshot.get("worker_running")),
            last_success_at=gdelt_snapshot.get("last_success_at"),
            stale_after_seconds=news_stale_after,
            now=current,
            last_error=gdelt_snapshot.get("last_error"),
        )
        gdelt_component["items_total_recent"] = int(gdelt_snapshot.get("items_total_recent") or 0)
        gdelt_component["query_window"] = gdelt_snapshot.get("query_window")
        gdelt_component["topics_configured"] = int(gdelt_snapshot.get("topics_configured") or 0)
        gdelt_component["topics_succeeded"] = int(gdelt_snapshot.get("topics_succeeded") or 0)
        gdelt_component["topics_failed"] = int(gdelt_snapshot.get("topics_failed") or 0)
        gdelt_component["topics_attempted"] = list(gdelt_snapshot.get("topics_attempted") or [])
        components["gdelt"] = gdelt_component

    if learning_snapshot is not None:
        learning_component = _health_component(
            name="model_learning",
            worker_running=bool(learning_snapshot.get("worker_running")),
            last_success_at=learning_snapshot.get("last_success_at"),
            stale_after_seconds=max(900, MODEL_LEARNING_POLL_SECONDS * 3),
            now=current,
            last_error=learning_snapshot.get("last_error"),
        )
        learning_component["model_status"] = learning_snapshot.get("model_status")
        learning_component["training_markets"] = int(learning_snapshot.get("training_markets") or 0)
        learning_component["feedback_forecasts_scored_total"] = int(
            learning_snapshot.get("feedback_forecasts_scored_total") or 0
        )
        components["model_learning"] = learning_component

    if paper_worker_snapshot is not None:
        paper_state = str(paper_worker_snapshot.get("worker_state") or "unknown")
        paper_component = _health_component(
            name="event_paper_worker",
            worker_running=paper_state in {"starting", "running", "degraded"},
            last_success_at=paper_worker_snapshot.get("last_success_at"),
            stale_after_seconds=max(900, EVENT_PAPER_POLL_SECONDS * 3),
            now=current,
            last_error=paper_worker_snapshot.get("last_error_type"),
        )
        paper_component["worker_state"] = paper_state
        paper_component["cycles"] = int(paper_worker_snapshot.get("cycles") or 0)
        paper_component["consecutive_failures"] = int(
            paper_worker_snapshot.get("consecutive_failures") or 0
        )
        paper_component["total_failures"] = int(
            paper_worker_snapshot.get("total_failures") or 0
        )
        paper_component["heartbeat_fresh"] = bool(
            paper_worker_snapshot.get("heartbeat_fresh")
        )
        paper_component["last_backup_at"] = paper_worker_snapshot.get("last_backup_at")
        paper_component["backup_manifest_name"] = paper_worker_snapshot.get("backup_manifest_name")
        paper_component["backup_files_last_run"] = int(
            paper_worker_snapshot.get("backup_files_last_run") or 0
        )
        paper_component["backup_error_type"] = paper_worker_snapshot.get("backup_error_type")
        paper_component["backup_consecutive_failures"] = int(
            paper_worker_snapshot.get("backup_consecutive_failures") or 0
        )
        paper_component["backup_retry_delay_seconds"] = float(
            paper_worker_snapshot.get("backup_retry_delay_seconds") or 0.0
        )
        if (
            (paper_state == "degraded" or paper_worker_snapshot.get("backup_error_type"))
            and paper_component["status"] == "healthy"
        ):
            paper_component["status"] = "degraded"
        if paper_state == "failed":
            paper_component["status"] = "failed"
        components["event_paper_worker"] = paper_component

    if stream_snapshot is not None:
        stream_worker_running = bool(stream_snapshot.get("worker_running"))
        stream_last_connect = stream_snapshot.get("last_connect_at")
        stream_component = _health_component(
            name="market_stream",
            worker_running=stream_worker_running and bool(stream_snapshot.get("connected")),
            last_success_at=stream_last_connect,
            stale_after_seconds=stream_stale_after,
            now=current,
            last_error=stream_snapshot.get("last_error"),
        )
        if stream_worker_running and not stream_snapshot.get("connected"):
            stream_component["status"] = "disconnected" if stream_last_connect else "starting"
        stream_component["last_message_at"] = stream_snapshot.get("last_message_at")
        components["market_stream"] = stream_component

    healthy = market_component["status"] == "healthy"
    degraded = [
        name for name, component in components.items()
        if name != "market_data" and component["status"] != "healthy"
    ]
    payload = {
        "ok": healthy,
        "status": "unhealthy" if not healthy else ("degraded" if degraded else "healthy"),
        "mode": "paper_only",
        "worker_running": bool(snapshot.get("worker_running")),
        "last_success_at": snapshot.get("last_success_at"),
        "seconds_since_success": market_component["seconds_since_success"],
        "stale_after_seconds": market_stale_after,
        "last_error": snapshot.get("last_error"),
        "consecutive_failures": int(snapshot.get("consecutive_failures") or 0),
        "retry_delay_seconds": float(snapshot.get("retry_delay_seconds") or 0.0),
        "next_retry_at": snapshot.get("next_retry_at"),
        "components": components,
        "degraded_components": degraded,
        "health_policy": "market_data_is_required; news_gdelt_resolution_stream_and_model_learning_are_reported_as_optional_degraded_components",
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


def get_gdelt_collector() -> GDELTCollector:
    global _gdelt_collector
    with _news_lock:
        if _gdelt_collector is None:
            _gdelt_collector = GDELTCollector(DATA_DIR / "gdelt_news.jsonl")
        return _gdelt_collector


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
    failure_count = 0
    with _news_lock:
        _news_state["worker_running"] = True
    while True:
        attempted_at = utc_now()
        with _news_lock:
            _news_state["last_poll_at"] = attempted_at
        retry_delay = None
        try:
            result = get_news_collector().fetch_once()
            feeds_succeeded = int(result.get("feeds_succeeded") or 0)
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
                _news_items_cache[:] = result["latest_items"][:500]
                if feeds_succeeded > 0:
                    _news_state["last_success_at"] = utc_now()
                    _news_state["last_error"] = None
                    _news_state["consecutive_failures"] = 0
                    _news_state["retry_delay_seconds"] = 0.0
                    _news_state["next_retry_at"] = None
                else:
                    _news_state["last_error"] = "All configured news feeds failed"
            if feeds_succeeded > 0:
                failure_count = 0
            else:
                failure_count += 1
                retry_delay = _worker_retry_delay(
                    RuntimeError("All configured news feeds failed"),
                    failure_count,
                    base_seconds=NEWS_POLL_SECONDS,
                )
                with _news_lock:
                    _news_state.update({
                        "consecutive_failures": failure_count,
                        "retry_delay_seconds": retry_delay,
                        "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat(),
                    })
            try:
                gdelt_result = get_gdelt_collector().fetch_once()
                with _news_lock:
                    _gdelt_state.update({
                        "last_success_at": utc_now(),
                        "last_error": None,
                        "items_added_last_poll": gdelt_result["items_added"],
                        "items_total_recent": gdelt_result["items_total_recent"],
                        "query_window": gdelt_result["query_window"],
                        "topics_configured": gdelt_result["topics_configured"],
                        "topics_attempted": list(gdelt_result["topics_attempted"]),
                        "topics_succeeded": gdelt_result["topics_succeeded"],
                        "topics_failed": gdelt_result["topics_failed"],
                        "topic_results": dict(gdelt_result["topic_results"]),
                        "topic_errors": list(gdelt_result["topic_errors"])[:10],
                    })
                    combined = gdelt_result["latest_items"] + list(_news_items_cache)
                    unique = {}
                    for item in combined:
                        unique.setdefault(str(item.get("id") or item.get("url") or ""), item)
                    _news_items_cache[:] = list(unique.values())[:500]
            except Exception as exc:
                with _news_lock:
                    _gdelt_state["last_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
            try:
                match_recent_news()
            except Exception as exc:
                with _news_lock:
                    _news_state["last_match_error"] = f"Unexpected {type(exc).__name__}: {str(exc)[:240]}"
        except Exception as exc:
            failure_count += 1
            retry_delay = _worker_retry_delay(exc, failure_count, base_seconds=NEWS_POLL_SECONDS)
            with _news_lock:
                _news_state.update({
                    "last_error": f"Unexpected {type(exc).__name__}: {str(exc)[:300]}",
                    "consecutive_failures": failure_count,
                    "retry_delay_seconds": retry_delay,
                    "next_retry_at": (datetime.now(timezone.utc) + timedelta(seconds=retry_delay)).isoformat(),
                })
        time.sleep(retry_delay if retry_delay is not None else NEWS_POLL_SECONDS)


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
        if self.path in ("/", "/status", "/health", "/paper", "/news", "/matches", "/market-stream", "/resolutions", "/forecast-evaluation", "/research-features", "/independent-model-evaluation", "/model-forecasts", "/learning", "/execution-readiness", "/service-supervisor", "/event-paper"):
            with _lock:
                snapshot = dict(_state)
            if self.path == "/health":
                with _news_lock:
                    news_snapshot = dict(_news_state)
                    gdelt_snapshot = dict(_gdelt_state)
                with _lock:
                    stream_snapshot = dict(_market_stream_state)
                    resolution_snapshot = dict(_resolution_state)
                with _learning_lock:
                    learning_snapshot = dict(_learning_state)
                supervisor_snapshot = read_supervisor_status(SUPERVISOR_STATUS_FILE)
                paper_worker_snapshot = (
                    read_event_paper_worker_status(EVENT_PAPER_WORKER_STATUS_FILE)
                    if supervisor_snapshot.get("event_paper_worker_enabled") is True
                    else None
                )
                healthy, payload = health_payload(
                    snapshot,
                    news_snapshot=news_snapshot,
                    stream_snapshot=stream_snapshot,
                    resolution_snapshot=resolution_snapshot,
                    gdelt_snapshot=gdelt_snapshot,
                    learning_snapshot=learning_snapshot,
                    paper_worker_snapshot=paper_worker_snapshot,
                )
                self._send(200 if healthy else 503, payload)
            elif self.path == "/paper":
                self._send(200, get_paper_trader().snapshot())
            elif self.path == "/news":
                with _news_lock:
                    news_snapshot = {
                        **_news_state,
                        "items": list(_news_items_cache[:500]),
                        "freshness": summarize_news_freshness(_news_items_cache),
                        "gdelt": dict(_gdelt_state),
                    }
                self._send(200, news_snapshot)
            elif self.path == "/market-stream":
                with _lock:
                    stream_snapshot = dict(_market_stream_state)
                self._send(200, stream_snapshot)
            elif self.path == "/resolutions":
                with _lock:
                    resolution_snapshot = dict(_resolution_state)
                self._send(200, {
                    **resolution_snapshot,
                    "research_only": True,
                    "execution": "none",
                })
            elif self.path == "/research-features":
                try:
                    self._send(200, research_features_payload())
                except Exception as exc:
                    self._send(503, {
                        "mode": "RESEARCH_FEATURES_V1",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "research_only": True,
                        "execution": "none",
                    })
            elif self.path == "/model-forecasts":
                try:
                    self._send(200, current_model_forecasts_payload())
                except Exception as exc:
                    self._send(503, {
                        "mode": "CURRENT_FEATURE_MODEL_FORECASTS_V1",
                        "status": "error",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "research_only": True,
                        "decision": "NO_ACTION_RESEARCH_ONLY",
                        "execution": "none",
                        "live_execution_enabled": False,
                    })
            elif self.path == "/independent-model-evaluation":
                try:
                    self._send(200, independent_model_evaluation_payload())
                except Exception as exc:
                    self._send(503, {
                        "mode": "INDEPENDENT_FEATURE_MODEL_EVALUATION_V1",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "research_only": True,
                        "forecast": None,
                        "execution": "none",
                    })
            elif self.path == "/execution-readiness":
                self._send(200, execution_readiness_payload())
            elif self.path == "/event-paper":
                supervisor_snapshot = read_supervisor_status(SUPERVISOR_STATUS_FILE)
                if supervisor_snapshot.get("event_paper_worker_enabled") is True:
                    worker_snapshot = read_event_paper_worker_status(EVENT_PAPER_WORKER_STATUS_FILE)
                else:
                    worker_snapshot = {
                        "worker_state": "disabled",
                        "heartbeat_fresh": False,
                        "execution": "none",
                    }
                worker_snapshot.update({
                    "mode": "OFFLINE_NEWS_PAPER_ONLY",
                    "research_only": True,
                    "live_execution_enabled": False,
                    "execution": "none",
                })
                self._send(200, worker_snapshot)
            elif self.path == "/service-supervisor":
                payload = read_supervisor_status(SUPERVISOR_STATUS_FILE)
                payload["event_paper_worker"] = read_event_paper_worker_status(EVENT_PAPER_WORKER_STATUS_FILE)
                payload.update({
                    "read_only": True,
                    "order_submission_allowed": False,
                    "order_signing_allowed": False,
                })
                self._send(200, payload)
            elif self.path == "/learning":
                try:
                    with _learning_lock:
                        learning_snapshot = dict(_learning_state)
                    self._send(200, {
                        **learning_snapshot,
                        **get_learning_journal().summary(),
                        **get_model_governance_ledger().snapshot(),
                        "research_only": True,
                        "live_execution_enabled": False,
                        "automatic_parameter_tuning": False,
                    })
                except Exception as exc:
                    self._send(503, {
                        "mode": "REALIZED_FORECAST_FEEDBACK_V1",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "research_only": True,
                        "live_execution_enabled": False,
                        "execution": "none",
                    })
            elif self.path == "/forecast-evaluation":
                try:
                    self._send(200, forecast_evaluation_payload())
                except Exception as exc:
                    self._send(503, {
                        "mode": "RESOLVED_MARKET_FORECAST_EVALUATION",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "research_only": True,
                        "execution": "none",
                    })
            elif self.path == "/matches":
                with _news_lock:
                    cached_matches = list(_news_matches_cache)
                    match_snapshot = {
                        "method": "market_corpus_idf_keyword_v2",
                        "research_only": True,
                        "execution": "none",
                        "total_recent": _news_state["market_matches_total_recent"],
                        "last_match_check_at": _news_state["last_match_check_at"],
                        "last_match_error": _news_state["last_match_error"],
                    }
                # Publication age changes while a candidate remains in the cache.
                # Refresh only the response payload; the append-only research log
                # keeps the timestamp/status as observed when the match was created.
                current_matches = refresh_match_freshness(cached_matches)
                match_snapshot.update({
                    "items": current_matches,
                    "source_corroboration": summarize_market_corroboration(current_matches),
                    "freshness_summary": summarize_match_freshness(current_matches),
                })
                self._send(200, match_snapshot)
            else:
                with _news_lock:
                    news_snapshot = dict(_news_state)
                    news_snapshot["feed_errors"] = list(_news_state["feed_errors"])
                    news_snapshot["freshness"] = summarize_news_freshness(_news_items_cache)
                    news_snapshot["gdelt"] = dict(_gdelt_state)
                with _lock:
                    stream_snapshot = dict(_market_stream_state)
                    resolution_snapshot = dict(_resolution_state)
                with _learning_lock:
                    learning_snapshot = dict(_learning_state)
                self._send(200, {
                    "service": "Polymarket Paper Research Bot",
                    **snapshot,
                    "news": news_snapshot,
                    "market_stream": stream_snapshot,
                    "resolutions": resolution_snapshot,
                    "model_learning": learning_snapshot,
                })
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        return


def main() -> None:
    threading.Thread(target=worker_loop, daemon=True, name="market-poller").start()
    threading.Thread(target=news_worker_loop, daemon=True, name="global-news-poller").start()
    threading.Thread(target=market_stream_worker_loop, daemon=True, name="market-stream-poller").start()
    threading.Thread(target=resolution_worker_loop, daemon=True, name="resolution-poller").start()
    threading.Thread(target=model_learning_worker_loop, daemon=True, name="model-learning-poller").start()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Paper research worker listening on port {port}; polling every {POLL_SECONDS}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
