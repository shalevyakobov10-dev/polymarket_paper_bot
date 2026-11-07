"""Append-only SQLite archive for observed public Polymarket market snapshots.

The archive is research data only. It stores observed bid/ask prices and the
visible top-five book levels; it never submits orders or treats midpoint as a
guaranteed fill price.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


def _timestamp(value: Any, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _price(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} must be a finite probability price") from None
    if not math.isfinite(parsed) or not 0.0 < parsed < 1.0:
        raise ValueError(f"{field} must be between 0 and 1 (exclusive)")
    return parsed


def _optional_nonnegative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _book_payload(book: Any) -> str:
    if not isinstance(book, dict):
        book = {}
    allowed = (
        "observed_at", "best_bid", "best_ask", "bids_top5", "asks_top5",
        "bid_depth_usd_top5", "ask_depth_usd_top5", "depth_imbalance_top5",
        "source", "book_hash", "tick_size", "min_order_size",
    )
    compact = {key: book[key] for key in allowed if key in book}
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, allow_nan=False)


class MarketHistoryStore:
    """Versioned, append-only local market history with idempotent snapshot writes."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 20.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=max(1.0, float(timeout_seconds)))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self._migrate()

    def _migrate(self) -> None:
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Market history schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            with self.db:
                self.db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS market_snapshots (
                        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        market_id TEXT NOT NULL,
                        question TEXT NOT NULL,
                        yes_token_id TEXT NOT NULL,
                        no_token_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        yes_bid REAL NOT NULL,
                        yes_ask REAL NOT NULL,
                        no_bid REAL NOT NULL,
                        no_ask REAL NOT NULL,
                        yes_mid REAL NOT NULL,
                        no_mid REAL NOT NULL,
                        volume_24h REAL,
                        yes_book_json TEXT NOT NULL,
                        no_book_json TEXT NOT NULL,
                        market_source TEXT NOT NULL,
                        book_source TEXT NOT NULL,
                        UNIQUE(market_id, observed_at, yes_bid, yes_ask, no_bid, no_ask)
                    );
                    CREATE INDEX IF NOT EXISTS idx_market_history_market_time
                        ON market_snapshots(market_id, observed_at, snapshot_id);
                    CREATE INDEX IF NOT EXISTS idx_market_history_observed
                        ON market_snapshots(observed_at, snapshot_id);
                    """
                )
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _normalize(snapshot: Any, captured_at: str) -> tuple[Any, ...]:
        if not isinstance(snapshot, dict):
            raise ValueError("Every market snapshot must be an object")
        required = ("market_id", "question", "yes_token_id", "no_token_id")
        values = {key: str(snapshot.get(key) or "").strip() for key in required}
        if any(not value for value in values.values()):
            raise ValueError("Snapshot market_id, question, and YES/NO token IDs are required")
        if values["yes_token_id"] == values["no_token_id"]:
            raise ValueError("YES and NO token IDs must be different")

        observed_at = _timestamp(snapshot.get("observed_at"), "observed_at")
        yes_bid = _price(snapshot.get("yes_bid"), "yes_bid")
        yes_ask = _price(snapshot.get("yes_ask"), "yes_ask")
        no_bid = _price(snapshot.get("no_bid"), "no_bid")
        no_ask = _price(snapshot.get("no_ask"), "no_ask")
        if yes_bid > yes_ask or no_bid > no_ask:
            raise ValueError("Crossed outcome books cannot be archived")
        yes_mid = (yes_bid + yes_ask) / 2.0
        no_mid = (no_bid + no_ask) / 2.0

        return (
            values["market_id"], values["question"][:500],
            values["yes_token_id"], values["no_token_id"],
            observed_at, captured_at, yes_bid, yes_ask, no_bid, no_ask,
            yes_mid, no_mid, _optional_nonnegative(snapshot.get("volume_24h")),
            _book_payload(snapshot.get("yes_book")),
            _book_payload(snapshot.get("no_book")),
            str(snapshot.get("market_source") or "unknown")[:80],
            str(snapshot.get("book_source") or "unknown")[:80],
        )

    def append_snapshots(
        self,
        snapshots: Iterable[dict[str, Any]],
        *,
        captured_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        if isinstance(captured_at, datetime):
            capture_value = captured_at
            if capture_value.tzinfo is None:
                raise ValueError("captured_at datetime must include a timezone")
            capture_text = capture_value.astimezone(timezone.utc).isoformat()
        elif captured_at is None:
            capture_text = datetime.now(timezone.utc).isoformat()
        else:
            capture_text = _timestamp(captured_at, "captured_at")
        capture_text = _timestamp(capture_text, "captured_at")

        normalized = [self._normalize(row, capture_text) for row in snapshots]
        before = self.db.total_changes
        with self.db:
            self.db.executemany(
                """
                INSERT OR IGNORE INTO market_snapshots (
                    market_id, question, yes_token_id, no_token_id,
                    observed_at, captured_at, yes_bid, yes_ask, no_bid, no_ask,
                    yes_mid, no_mid, volume_24h, yes_book_json, no_book_json,
                    market_source, book_source
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                normalized,
            )
        inserted = self.db.total_changes - before
        return {
            "inserted": inserted,
            "duplicates": len(normalized) - inserted,
            "received": len(normalized),
            "total_snapshots": self.count(),
            "captured_at": capture_text,
            "research_only": True,
            "execution": "none",
        }

    def history(
        self,
        market_id: str,
        *,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        market = str(market_id or "").strip()
        if not market:
            raise ValueError("market_id is required")
        bounded_limit = max(1, min(100_000, int(limit)))
        clauses = ["market_id = ?"]
        params: list[Any] = [market]
        if start_at is not None:
            clauses.append("observed_at >= ?")
            params.append(_timestamp(start_at, "start_at"))
        if end_at is not None:
            clauses.append("observed_at <= ?")
            params.append(_timestamp(end_at, "end_at"))
        params.append(bounded_limit)
        rows = self.db.execute(
            f"SELECT * FROM market_snapshots WHERE {' AND '.join(clauses)} "
            "ORDER BY observed_at, snapshot_id LIMIT ?",
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["yes_book"] = json.loads(item.pop("yes_book_json"))
            item["no_book"] = json.loads(item.pop("no_book_json"))
            result.append(item)
        return result

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0])

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "MarketHistoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
