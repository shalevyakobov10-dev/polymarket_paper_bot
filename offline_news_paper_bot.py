"""Offline news-driven paper simulator.

Market prices are supplied by a local JSON snapshot/replay file. This module
fetches third-party RSS headlines only; it contains no Polymarket API client,
wallet integration, signer, or order-submission code.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from adaptive_paper_risk import (
    daily_drawdown_breached,
    dynamic_exit_levels,
    size_position_by_risk,
)
from market_snapshot_provider import JsonMarketSnapshotProvider
from polymarket_public_market_feed import PolymarketPublicMarketSnapshotProvider
from news_monitor import DEFAULT_FEEDS, NewsCollector, match_news_to_markets
from news_sentiment import aggregate_market_news_sentiment, analyze_news_sentiment
from paper_parameter_tuner import tune_sentiment_threshold

DRY_RUN = True
LIVE_EXECUTION_ENABLED = False
ENGINE_MODE = "OFFLINE_NEWS_PAPER_ONLY"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_published_epoch(value: Any) -> float | None:
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
    return parsed.astimezone(timezone.utc).timestamp()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


class PaperConfig:
    def __init__(self) -> None:
        if not _bool_env("DRY_RUN", True):
            raise ValueError("This research build requires DRY_RUN=true")
        if _bool_env("LIVE_EXECUTION_ENABLED", False):
            raise ValueError("Live execution is not part of this offline research build")
        self.starting_cash = _float_env("PAPER_STARTING_CASH", 100.0, 1.0, 1_000_000.0)
        self.max_position_usd = _float_env("PAPER_MAX_POSITION_USD", 5.0, 0.1, 100_000.0)
        self.max_open_exposure_usd = _float_env("PAPER_MAX_OPEN_EXPOSURE_USD", 20.0, 0.1, 1_000_000.0)
        self.max_open_positions = _int_env("PAPER_MAX_OPEN_POSITIONS", 5, 1, 100)
        self.risk_per_trade_pct = _float_env("PAPER_RISK_PER_TRADE_PCT", 0.01, 0.001, 0.05)
        self.max_daily_drawdown_pct = _float_env("PAPER_MAX_DAILY_DRAWDOWN_PCT", 0.05, 0.005, 0.25)
        self.max_portfolio_drawdown_pct = _float_env("PAPER_MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20, 0.01, 0.75)
        self.fee_rate = _float_env("PAPER_FEE_RATE", 0.01, 0.0, 0.20)
        self.min_sentiment_threshold = _float_env("NEWS_SENTIMENT_THRESHOLD", 0.35, 0.05, 1.0)
        self.minimum_publishers = _int_env("NEWS_MINIMUM_PUBLISHERS", 2, 1, 20)
        self.maximum_news_age_hours = _float_env("NEWS_MAXIMUM_AGE_HOURS", 24.0, 1.0, 168.0)
        self.half_life_hours = _float_env("NEWS_SENTIMENT_HALF_LIFE_HOURS", 6.0, 0.5, 72.0)
        self.maximum_spread_bps = _float_env("PAPER_MAX_SPREAD_BPS", 500.0, 10.0, 5000.0)
        self.maximum_market_age_seconds = _float_env("MARKET_SNAPSHOT_MAX_AGE_SECONDS", 180.0, 10.0, 3600.0)
        self.fallback_volatility = _float_env("PAPER_FALLBACK_VOLATILITY", 0.02, 0.001, 0.20)
        self.stop_volatility_multiplier = _float_env("PAPER_STOP_VOLATILITY_MULTIPLIER", 2.0, 0.5, 5.0)
        self.take_profit_r_multiple = _float_env("PAPER_TAKE_PROFIT_R_MULTIPLE", 2.0, 0.5, 5.0)
        self.minimum_stop_distance = _float_env("PAPER_MINIMUM_STOP_DISTANCE", 0.02, 0.005, 0.20)
        self.maximum_stop_distance = _float_env("PAPER_MAXIMUM_STOP_DISTANCE", 0.15, 0.02, 0.40)
        if self.maximum_stop_distance < self.minimum_stop_distance:
            raise ValueError("PAPER_MAXIMUM_STOP_DISTANCE must be >= PAPER_MINIMUM_STOP_DISTANCE")
        self.max_hold_hours = _float_env("PAPER_MAX_HOLD_HOURS", 24.0, 1.0, 720.0)
        self.auto_tune_enabled = _bool_env("PAPER_AUTO_TUNE_ENABLED", True)
        self.auto_tune_minimum_trades = _int_env("PAPER_AUTO_TUNE_MINIMUM_TRADES", 60, 30, 10_000)
        self.auto_tune_interval_hours = _float_env("PAPER_AUTO_TUNE_INTERVAL_HOURS", 24.0, 1.0, 720.0)
        self.market_snapshot_file = Path(os.getenv("MARKET_SNAPSHOT_FILE", "./data/market_snapshots.json"))
        self.market_data_source = os.getenv("MARKET_DATA_SOURCE", "local_json").strip().lower()
        if self.market_data_source not in {"local_json", "polymarket_public"}:
            raise ValueError("MARKET_DATA_SOURCE must be local_json or polymarket_public")
        self.public_markets_to_track = _int_env("PUBLIC_MARKETS_TO_TRACK", 10, 1, 10)
        self.database_path = Path(os.getenv("PAPER_DATABASE_PATH", "./data/offline_news_paper.sqlite3"))
        self.news_log_path = Path(os.getenv("NEWS_LOG_PATH", "./data/offline_news_events.jsonl"))
        self.poll_seconds = _int_env("PAPER_POLL_SECONDS", 60, 30, 86_400)


class OfflineNewsPaperBot:
    def __init__(
        self,
        config: PaperConfig | None = None,
        *,
        news_collector: NewsCollector | None = None,
        snapshot_provider: JsonMarketSnapshotProvider | None = None,
    ) -> None:
        self.config = config or PaperConfig()
        self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.news_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.config.database_path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self.news_collector = news_collector or NewsCollector(self.config.news_log_path, DEFAULT_FEEDS)
        if snapshot_provider is not None:
            self.snapshot_provider = snapshot_provider
        elif self.config.market_data_source == "polymarket_public":
            self.snapshot_provider = PolymarketPublicMarketSnapshotProvider(
                max_age_seconds=self.config.maximum_market_age_seconds,
                max_markets=self.config.public_markets_to_track,
                websocket_enabled=_bool_env("PUBLIC_MARKET_WS_ENABLED", True),
                websocket_session_seconds=_int_env("PUBLIC_MARKET_WS_SESSION_SECONDS", 15, 5, 120),
                stream_log_path=Path(os.getenv("MARKET_STREAM_LOG_PATH", "./data/market_stream_events.jsonl")),
            )
        else:
            self.snapshot_provider = JsonMarketSnapshotProvider(
                self.config.market_snapshot_file,
                max_age_seconds=self.config.maximum_market_age_seconds,
            )
        self._ensure_account()

    def close(self) -> None:
        self.db.close()

    def _create_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account_state (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                cash REAL NOT NULL,
                starting_cash REAL NOT NULL,
                equity_peak REAL NOT NULL,
                day_key TEXT NOT NULL,
                day_start_equity REAL NOT NULL,
                active_sentiment_threshold REAL NOT NULL,
                last_tune_at TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                midpoint REAL NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                UNIQUE(token_id, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_token_time
                ON observations(token_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                question TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('YES','NO')),
                token_id TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                entry_price REAL NOT NULL,
                shares REAL NOT NULL,
                notional REAL NOT NULL,
                entry_fee REAL NOT NULL,
                stop_loss_price REAL NOT NULL,
                take_profit_price REAL NOT NULL,
                signal_score REAL NOT NULL,
                signal_strength REAL NOT NULL,
                sentiment_confidence REAL NOT NULL,
                publisher_count INTEGER NOT NULL,
                entry_threshold REAL NOT NULL,
                last_mark_price REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                closed_at TEXT,
                exit_price REAL,
                exit_fee REAL,
                pnl REAL,
                net_roi REAL,
                exit_reason TEXT,
                exit_reason_placeholder TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status, market_id);
            CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                market_id TEXT NOT NULL,
                sentiment_score REAL,
                sentiment_confidence REAL,
                publisher_count INTEGER NOT NULL,
                article_count INTEGER NOT NULL,
                selected_direction TEXT,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tuning_runs (
                tuning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
        """)
        self.db.commit()

    def _ensure_account(self, now: datetime | None = None) -> None:
        current = now or _utc_now()
        row = self.db.execute("SELECT singleton_id FROM account_state WHERE singleton_id=1").fetchone()
        if row:
            return
        self.db.execute(
            "INSERT INTO account_state VALUES (1, ?, ?, ?, ?, ?, ?, NULL)",
            (
                self.config.starting_cash, self.config.starting_cash,
                self.config.starting_cash, current.date().isoformat(),
                self.config.starting_cash, self.config.min_sentiment_threshold,
            ),
        )
        self.db.commit()

    def _account(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM account_state WHERE singleton_id=1").fetchone()

    def _open_trades(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY trade_id").fetchall()

    def _equity(self) -> float:
        account = self._account()
        marked = self.db.execute(
            "SELECT COALESCE(SUM(shares * last_mark_price),0) AS value FROM trades WHERE status='OPEN'"
        ).fetchone()["value"]
        return float(account["cash"]) + float(marked or 0.0)

    def _roll_day(self, now: datetime) -> None:
        account = self._account()
        today = now.date().isoformat()
        if account["day_key"] != today:
            equity = self._equity()
            self.db.execute(
                "UPDATE account_state SET day_key=?, day_start_equity=? WHERE singleton_id=1",
                (today, equity),
            )

    def _history(self, token_id: str, *, limit: int = 30) -> list[float]:
        rows = self.db.execute(
            "SELECT midpoint FROM observations WHERE token_id=? ORDER BY observed_at DESC LIMIT ?",
            (token_id, limit),
        ).fetchall()
        return [float(row["midpoint"]) for row in reversed(rows)]

    @staticmethod
    def _book_values(snapshot: dict[str, Any], direction: str) -> tuple[str, float, float, float]:
        suffix = "yes" if direction == "YES" else "no"
        token_id = str(snapshot[f"{suffix}_token_id"])
        bid = float(snapshot[f"{suffix}_bid"])
        ask = float(snapshot[f"{suffix}_ask"])
        mid = float(snapshot[f"{suffix}_price"])
        return token_id, bid, ask, mid

    def _insert_observations(self, snapshots: list[dict[str, Any]]) -> None:
        for snapshot in snapshots:
            for direction in ("YES", "NO"):
                token_id, bid, ask, mid = self._book_values(snapshot, direction)
                self.db.execute(
                    "INSERT OR IGNORE INTO observations(token_id,market_id,observed_at,midpoint,bid,ask) "
                    "VALUES(?,?,?,?,?,?)",
                    (token_id, snapshot["market_id"], snapshot["observed_at"], mid, bid, ask),
                )

    def _close_due_trades(self, snapshots: dict[str, dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for trade in self._open_trades():
            snapshot = snapshots.get(str(trade["market_id"]))
            if snapshot is None:
                continue
            _token, bid, _ask, _mid = self._book_values(snapshot, str(trade["direction"]))
            self.db.execute(
                "UPDATE trades SET last_mark_price=? WHERE trade_id=?",
                (bid, trade["trade_id"]),
            )
            opened = datetime.fromisoformat(str(trade["opened_at"]).replace("Z", "+00:00"))
            age_hours = (now - opened.astimezone(timezone.utc)).total_seconds() / 3600.0
            reason = None
            if bid <= float(trade["stop_loss_price"]):
                reason = "dynamic_stop_loss"
            elif bid >= float(trade["take_profit_price"]):
                reason = "dynamic_take_profit"
            elif age_hours >= self.config.max_hold_hours:
                reason = "time_limit"
            if reason is None:
                continue
            shares = float(trade["shares"])
            exit_fee = shares * bid * self.config.fee_rate
            net_proceeds = shares * bid - exit_fee
            pnl = net_proceeds - float(trade["notional"]) - float(trade["entry_fee"])
            cost = float(trade["notional"]) + float(trade["entry_fee"])
            net_roi = pnl / cost if cost > 0 else 0.0
            self.db.execute(
                "UPDATE trades SET status='CLOSED',closed_at=?,exit_price=?,exit_fee=?,pnl=?,"
                "net_roi=?,exit_reason=?,last_mark_price=? WHERE trade_id=?",
                (_iso(now), bid, exit_fee, pnl, net_roi, reason, bid, trade["trade_id"]),
            )
            account = self._account()
            self.db.execute(
                "UPDATE account_state SET cash=cash+? WHERE singleton_id=1",
                (net_proceeds,),
            )
            closed.append({
                "trade_id": trade["trade_id"], "market_id": trade["market_id"],
                "direction": trade["direction"], "pnl": round(pnl, 8),
                "net_roi": round(net_roi, 8), "exit_reason": reason,
                "execution": "simulated_only",
            })
        return closed

    def _open_trade(
        self,
        snapshot: dict[str, Any],
        signal: dict[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any] | None, str]:
        if len(self._open_trades()) >= self.config.max_open_positions:
            return None, "maximum_open_positions_reached"
        if self.db.execute(
            "SELECT 1 FROM trades WHERE status='OPEN' AND market_id=?",
            (snapshot["market_id"],),
        ).fetchone():
            return None, "market_already_has_open_position"
        direction = "YES" if float(signal["score"]) > 0 else "NO"
        token_id, bid, ask, mid = self._book_values(snapshot, direction)
        spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else float("inf")
        if spread_bps > self.config.maximum_spread_bps:
            return None, "spread_above_limit"
        history = self._history(token_id)
        levels = dynamic_exit_levels(
            entry_price=ask,
            price_history=history,
            volatility_multiplier=self.config.stop_volatility_multiplier,
            reward_to_risk=self.config.take_profit_r_multiple,
            fallback_volatility=self.config.fallback_volatility,
            minimum_stop_distance=self.config.minimum_stop_distance,
            maximum_stop_distance=self.config.maximum_stop_distance,
        )
        account = self._account()
        equity = self._equity()
        exposure = sum(float(row["notional"]) for row in self._open_trades())
        remaining_capacity = max(0.0, self.config.max_open_exposure_usd - exposure)
        sizing = size_position_by_risk(
            equity=equity,
            available_cash=float(account["cash"]),
            remaining_exposure_capacity=remaining_capacity,
            entry_price=ask,
            stop_distance=levels.stop_distance,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            max_position_usd=self.config.max_position_usd,
        )
        if not sizing["approved"]:
            return None, "risk_sizing_rejected"
        notional = float(sizing["notional_usd"])
        shares = float(sizing["shares"])
        entry_fee = notional * self.config.fee_rate
        if notional + entry_fee > float(account["cash"]) + 1e-9:
            return None, "insufficient_paper_cash_after_fees"

        self.db.execute(
            "INSERT INTO trades(market_id,question,direction,token_id,opened_at,entry_price,shares,"
            "notional,entry_fee,stop_loss_price,take_profit_price,signal_score,signal_strength,"
            "sentiment_confidence,publisher_count,entry_threshold,last_mark_price,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
            (
                snapshot["market_id"], snapshot["question"], direction, token_id, _iso(now),
                ask, shares, notional, entry_fee, levels.stop_loss_price,
                levels.take_profit_price, float(signal["score"]), abs(float(signal["score"])),
                float(signal["confidence"]), int(signal["publisher_count"]),
                float(signal["threshold"]), bid,
            ),
        )
        self.db.execute(
            "UPDATE account_state SET cash=cash-? WHERE singleton_id=1",
            (notional + entry_fee,),
        )
        return {
            "market_id": snapshot["market_id"], "direction": direction,
            "entry_price": ask, "notional": notional, "shares": shares,
            "stop_loss_price": levels.stop_loss_price,
            "take_profit_price": levels.take_profit_price,
            "estimated_loss_at_stop_usd": sizing["estimated_loss_at_stop_usd"],
            "execution": "simulated_only",
        }, ""

    def _update_equity_and_gates(self, now: datetime) -> dict[str, Any]:
        self._roll_day(now)
        equity = self._equity()
        account = self._account()
        peak = max(float(account["equity_peak"]), equity)
        self.db.execute("UPDATE account_state SET equity_peak=? WHERE singleton_id=1", (peak,))
        daily_loss = daily_drawdown_breached(
            day_start_equity=float(account["day_start_equity"]),
            current_equity=equity,
            max_daily_drawdown_pct=self.config.max_daily_drawdown_pct,
        )
        portfolio_dd = max(0.0, (peak - equity) / peak) if peak > 0 else 1.0
        portfolio_loss = portfolio_dd >= self.config.max_portfolio_drawdown_pct
        return {
            "equity": round(equity, 8), "cash": round(float(account["cash"]), 8),
            "equity_peak": round(peak, 8), "portfolio_drawdown_pct": round(portfolio_dd * 100.0, 4),
            "daily_drawdown_breached": daily_loss, "portfolio_drawdown_breached": portfolio_loss,
            "new_entries_allowed": not daily_loss and not portfolio_loss,
        }

    def _build_signals(
        self,
        news_items: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        markets = [
            {"market_id": row["market_id"], "question": row["question"], "volume_24h": row.get("volume_24h")}
            for row in snapshots
        ]
        by_market: dict[str, list[dict[str, Any]]] = {}
        for news in news_items:
            candidates = match_news_to_markets(news, markets, max_matches=3)
            sentiment = analyze_news_sentiment(
                str(news.get("title") or "") + ". " + str(news.get("summary") or "")
            )
            published_epoch = _parse_published_epoch(news.get("published_at"))
            for candidate in candidates:
                by_market.setdefault(str(candidate["market_id"]), []).append({
                    **candidate,
                    "sentiment_score": sentiment.get("score"),
                    "sentiment_supported": sentiment.get("supported") is True,
                    "publisher_id": str(news.get("publisher_id") or news.get("source") or ""),
                    "source": str(news.get("source") or ""),
                    "published_at_epoch": published_epoch,
                })

        results: dict[str, dict[str, Any]] = {}
        now_epoch = now.timestamp()
        for market_id, matches in by_market.items():
            aggregate = aggregate_market_news_sentiment(
                matches,
                now_epoch=now_epoch,
                max_age_hours=self.config.maximum_news_age_hours,
                half_life_hours=self.config.half_life_hours,
                minimum_publishers=self.config.minimum_publishers,
            )
            aggregate["matches"] = len(matches)
            results[market_id] = aggregate
        return results

    def maybe_tune(self, now: datetime) -> dict[str, Any] | None:
        if not self.config.auto_tune_enabled:
            return None
        account = self._account()
        last = account["last_tune_at"]
        if last:
            parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if (now - parsed.astimezone(timezone.utc)) < timedelta(hours=self.config.auto_tune_interval_hours):
                return None
        rows = self.db.execute(
            "SELECT status,signal_strength,entry_threshold,pnl,net_roi,closed_at,exit_reason "
            "FROM trades WHERE status='CLOSED' ORDER BY closed_at,trade_id"
        ).fetchall()
        trades = [dict(row) for row in rows]
        result = tune_sentiment_threshold(
            trades,
            current_threshold=float(account["active_sentiment_threshold"]),
            minimum_closed_trades=self.config.auto_tune_minimum_trades,
        )
        self.db.execute(
            "INSERT INTO tuning_runs(ran_at,result_json) VALUES(?,?)",
            (_iso(now), json.dumps(result, ensure_ascii=False, allow_nan=False)),
        )
        self.db.execute(
            "UPDATE account_state SET last_tune_at=?,active_sentiment_threshold=? WHERE singleton_id=1",
            (_iso(now), float(result["new_threshold"])),
        )
        return result

    def run_once(self, *, now: datetime | None = None, fetch_news: bool = True) -> dict[str, Any]:
        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        snapshots = self.snapshot_provider.load(now=current)
        market_diagnostics = getattr(self.snapshot_provider, "last_diagnostics", None)
        if not isinstance(market_diagnostics, dict):
            market_diagnostics = {
                "source": self.config.market_data_source,
                "snapshots_returned": len(snapshots),
            }
        news_result = (
            self.news_collector.fetch_once()
            if fetch_news else {"feeds_configured": 0, "feeds_succeeded": 0, "feeds_failed": 0}
        )
        news_items = list(self.news_collector.items[-500:])
        signals = self._build_signals(news_items, snapshots, current)
        by_market = {row["market_id"]: row for row in snapshots}

        with self.db:
            closed = self._close_due_trades(by_market, current)
            gates = self._update_equity_and_gates(current)
            opened = []
            decisions = []
            account = self._account()
            threshold = float(account["active_sentiment_threshold"])
            for snapshot in snapshots:
                signal = signals.get(snapshot["market_id"])
                if not signal or not signal.get("qualified") or signal.get("score") is None:
                    continue
                score = float(signal["score"])
                threshold_now = float(self._account()["active_sentiment_threshold"])
                direction = "YES" if score > 0 else "NO"
                eligible = abs(score) >= threshold_now
                reason = "signal_threshold_not_met"
                if eligible:
                    gates = self._update_equity_and_gates(current)
                if eligible and not gates["new_entries_allowed"]:
                    eligible = False
                    reason = "daily_or_portfolio_drawdown_limit"
                elif eligible:
                    result, skip_reason = self._open_trade(
                        snapshot,
                        {**signal, "threshold": threshold_now},
                        current,
                    )
                    if result:
                        opened.append(result)
                        reason = "paper_position_opened"
                    else:
                        eligible = False
                        reason = skip_reason
                self.db.execute(
                    "INSERT INTO decisions(observed_at,market_id,sentiment_score,sentiment_confidence,"
                    "publisher_count,article_count,selected_direction,decision,reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        _iso(current), snapshot["market_id"], score, float(signal["confidence"]),
                        int(signal["publisher_count"]), int(signal["article_count"]),
                        direction, "ENTERED" if reason == "paper_position_opened" else "SKIPPED",
                        reason,
                    ),
                )
            self._insert_observations(snapshots)
            tuning = self.maybe_tune(current)
            final_gates = self._update_equity_and_gates(current)
            self.db.commit()

        summary = self.summary()
        return {
            "mode": ENGINE_MODE, "dry_run": DRY_RUN, "live_execution_enabled": LIVE_EXECUTION_ENABLED,
            "market_snapshots_loaded": len(snapshots),
            "market_data_source": self.config.market_data_source,
            "market_data_diagnostics": market_diagnostics,
            "news_feeds_configured": int(news_result.get("feeds_configured") or 0),
            "news_feeds_succeeded": int(news_result.get("feeds_succeeded") or 0),
            "news_feeds_failed": int(news_result.get("feeds_failed") or 0),
            "market_signals": len(signals), "opened": opened, "closed": closed,
            "tuning": tuning, "risk_gates": final_gates, "portfolio": summary,
            "research_only": True, "execution": "none",
        }

    def summary(self) -> dict[str, Any]:
        account = self._account()
        equity = self._equity()
        open_count = self.db.execute("SELECT COUNT(*) AS n FROM trades WHERE status='OPEN'").fetchone()["n"]
        closed_count = self.db.execute("SELECT COUNT(*) AS n FROM trades WHERE status='CLOSED'").fetchone()["n"]
        win_count = self.db.execute("SELECT COUNT(*) AS n FROM trades WHERE status='CLOSED' AND pnl>0").fetchone()["n"]
        realized = self.db.execute("SELECT COALESCE(SUM(pnl),0) AS pnl FROM trades WHERE status='CLOSED'").fetchone()["pnl"]
        return {
            "cash": round(float(account["cash"]), 8), "equity": round(equity, 8),
            "realized_pnl": round(float(realized or 0.0), 8),
            "open_positions": int(open_count), "closed_positions": int(closed_count),
            "win_rate_pct": round(100.0 * int(win_count) / int(closed_count), 4) if closed_count else None,
            "active_sentiment_threshold": float(account["active_sentiment_threshold"]),
            "mode": ENGINE_MODE, "execution": "none",
        }


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Offline news-driven Polymarket-style paper simulator")
    parser.add_argument("--once", action="store_true", help="Run one snapshot/news cycle and exit")
    args = parser.parse_args()
    bot = OfflineNewsPaperBot()
    try:
        while True:
            result = bot.run_once()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.once:
                break
            time.sleep(bot.config.poll_seconds)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
