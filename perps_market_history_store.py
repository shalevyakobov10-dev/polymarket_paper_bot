"""Append-only SQLite archive for public Polymarket Perps market snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PerpsMarketHistoryStore:
    """Persist validated public Perps snapshots without storing account data."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS perps_market_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                category TEXT NOT NULL,
                received_at TEXT NOT NULL,
                ticker_timestamp_ms INTEGER NOT NULL,
                book_timestamp_ms INTEGER NOT NULL,
                book_sequence INTEGER NOT NULL,
                index_price TEXT NOT NULL,
                mark_price TEXT NOT NULL,
                last_price TEXT NOT NULL,
                mid_price TEXT NOT NULL,
                open_interest TEXT NOT NULL,
                funding_rate TEXT NOT NULL,
                next_funding_ms INTEGER,
                best_bid TEXT NOT NULL,
                best_ask TEXT NOT NULL,
                bids_json TEXT NOT NULL,
                asks_json TEXT NOT NULL,
                instrument_json TEXT NOT NULL,
                ticker_json TEXT NOT NULL,
                UNIQUE(instrument_id, book_timestamp_ms, book_sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_perps_history_symbol_time
                ON perps_market_snapshots(symbol, received_at);
            CREATE INDEX IF NOT EXISTS idx_perps_history_instrument_time
                ON perps_market_snapshots(instrument_id, book_timestamp_ms);
            """
        )
        self.db.commit()

    @staticmethod
    def _validate(snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")
        instrument = snapshot.get("instrument")
        ticker = snapshot.get("ticker")
        book = snapshot.get("book")
        if not all(isinstance(item, dict) for item in (instrument, ticker, book)):
            raise ValueError("snapshot requires instrument, ticker and book objects")
        instrument_id = instrument.get("instrument_id")
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        if ticker.get("instrument_id") != instrument_id or book.get("instrument_id") != instrument_id:
            raise ValueError("snapshot instrument identifiers do not match")
        received_at = snapshot.get("received_at")
        if not isinstance(received_at, str) or not received_at.strip():
            raise ValueError("received_at is required")
        parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        required_ticker = (
            "timestamp_ms", "index_price", "mark_price", "last_price",
            "mid_price", "open_interest", "funding_rate",
        )
        if any(ticker.get(field) is None for field in required_ticker):
            raise ValueError("ticker is missing required fields")
        if not isinstance(book.get("timestamp_ms"), int) or not isinstance(book.get("sequence"), int):
            raise ValueError("book timestamp and sequence must be integers")
        if not book.get("bids") or not book.get("asks"):
            raise ValueError("book must contain both sides")
        if not isinstance(instrument.get("symbol"), str) or not instrument["symbol"]:
            raise ValueError("instrument symbol is required")
        return {
            "instrument": instrument,
            "ticker": ticker,
            "book": book,
            "received_at": parsed.astimezone(timezone.utc).isoformat(),
        }

    def record_snapshots(self, snapshots: list[dict[str, Any]]) -> dict[str, int | str | bool]:
        if not isinstance(snapshots, list):
            raise ValueError("snapshots must be a list")
        normalized = [self._validate(row) for row in snapshots]
        rows = []
        for item in normalized:
            instrument, ticker, book = item["instrument"], item["ticker"], item["book"]
            rows.append((
                instrument["instrument_id"], instrument["symbol"], instrument["category"],
                item["received_at"], ticker["timestamp_ms"], book["timestamp_ms"],
                book["sequence"], ticker["index_price"], ticker["mark_price"],
                ticker["last_price"], ticker["mid_price"], ticker["open_interest"],
                ticker["funding_rate"], ticker.get("next_funding_ms"), book["best_bid"],
                book["best_ask"],
                json.dumps(book["bids"], sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(book["asks"], sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(instrument, sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(ticker, sort_keys=True, separators=(",", ":"), allow_nan=False),
            ))
        before = self.db.total_changes
        with self.db:
            self.db.executemany(
                """
                INSERT OR IGNORE INTO perps_market_snapshots (
                    instrument_id, symbol, category, received_at,
                    ticker_timestamp_ms, book_timestamp_ms, book_sequence,
                    index_price, mark_price, last_price, mid_price, open_interest,
                    funding_rate, next_funding_ms, best_bid, best_ask,
                    bids_json, asks_json, instrument_json, ticker_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        inserted = self.db.total_changes - before
        return {
            "received": len(normalized),
            "inserted": inserted,
            "duplicates": len(normalized) - inserted,
            "total_snapshots": self.count(),
            "research_only": True,
            "execution": "none",
        }

    def latest(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(500, int(limit)))
        rows = self.db.execute(
            """
            SELECT * FROM perps_market_snapshots
            WHERE snapshot_id IN (
                SELECT MAX(snapshot_id) FROM perps_market_snapshots GROUP BY instrument_id
            )
            ORDER BY symbol LIMIT ?
            """,
            (bounded,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["bids"] = json.loads(item.pop("bids_json"))
            item["asks"] = json.loads(item.pop("asks_json"))
            item["instrument"] = json.loads(item.pop("instrument_json"))
            item["ticker"] = json.loads(item.pop("ticker_json"))
            result.append(item)
        return result

    def count(self) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM perps_market_snapshots"
        ).fetchone()[0])

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PerpsMarketHistoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
