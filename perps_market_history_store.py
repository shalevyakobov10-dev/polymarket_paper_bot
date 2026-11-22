"""Retained SQLite archive for public Perps snapshots and published funding settlements.

Snapshot and funding rows are append-only during the retention window; expiry is
handled by the worker's explicit retention policy.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _finite_decimal(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be nonnegative")
    return parsed


def _aware_iso(value: str | datetime, field: str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


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
                fees_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(instrument_id, book_timestamp_ms, book_sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_perps_history_symbol_time
                ON perps_market_snapshots(symbol, received_at);
            CREATE INDEX IF NOT EXISTS idx_perps_history_instrument_time
                ON perps_market_snapshots(instrument_id, book_timestamp_ms);
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(perps_market_snapshots)").fetchall()}
        if "fees_json" not in columns:
            self.db.execute("ALTER TABLE perps_market_snapshots ADD COLUMN fees_json TEXT NOT NULL DEFAULT '{}'")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS perps_funding_history (
                instrument_id INTEGER NOT NULL,
                funding_timestamp_ms INTEGER NOT NULL,
                funding_rate TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(instrument_id, funding_timestamp_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_perps_funding_time
                ON perps_funding_history(instrument_id, funding_timestamp_ms);
            CREATE TABLE IF NOT EXISTS perps_funding_conflicts (
                instrument_id INTEGER NOT NULL,
                funding_timestamp_ms INTEGER NOT NULL,
                observed_rates_json TEXT NOT NULL,
                first_detected_at TEXT NOT NULL,
                last_detected_at TEXT NOT NULL,
                PRIMARY KEY(instrument_id, funding_timestamp_ms)
            );
            CREATE INDEX IF NOT EXISTS idx_perps_funding_conflicts_time
                ON perps_funding_conflicts(last_detected_at);
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
        if isinstance(ticker["timestamp_ms"], bool) or not isinstance(ticker["timestamp_ms"], int) or ticker["timestamp_ms"] <= 0:
            raise ValueError("ticker timestamp_ms must be a positive integer")
        for field in ("index_price", "mark_price", "last_price", "mid_price"):
            _finite_decimal(ticker[field], f"ticker.{field}", positive=True)
        _finite_decimal(ticker["open_interest"], "ticker.open_interest", nonnegative=True)
        _finite_decimal(ticker["funding_rate"], "ticker.funding_rate")
        if ticker.get("next_funding_ms") is not None and (
            isinstance(ticker["next_funding_ms"], bool)
            or not isinstance(ticker["next_funding_ms"], int)
            or ticker["next_funding_ms"] <= 0
        ):
            raise ValueError("ticker next_funding_ms must be a positive integer or null")
        if (
            isinstance(book.get("timestamp_ms"), bool)
            or not isinstance(book.get("timestamp_ms"), int)
            or book["timestamp_ms"] <= 0
            or isinstance(book.get("sequence"), bool)
            or not isinstance(book.get("sequence"), int)
            or book["sequence"] < 0
        ):
            raise ValueError("book timestamp and sequence must be integers in valid ranges")
        bids, asks = book.get("bids"), book.get("asks")
        if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
            raise ValueError("book must contain both sides")
        parsed_sides = []
        for side_name, levels, reverse in (("bids", bids, True), ("asks", asks, False)):
            prices: list[Decimal] = []
            for level in levels[:10]:
                if not isinstance(level, dict):
                    raise ValueError(f"book {side_name} levels must be objects")
                prices.append(_finite_decimal(level.get("price"), f"{side_name}.price", positive=True))
                _finite_decimal(level.get("quantity"), f"{side_name}.quantity", positive=True)
            if prices != sorted(prices, reverse=reverse):
                raise ValueError(f"book {side_name} levels must be price ordered")
            parsed_sides.append(prices[0])
        best_bid = _finite_decimal(book.get("best_bid"), "book.best_bid", positive=True)
        best_ask = _finite_decimal(book.get("best_ask"), "book.best_ask", positive=True)
        if best_bid != parsed_sides[0] or best_ask != parsed_sides[1] or best_bid >= best_ask:
            raise ValueError("book best bid/ask do not match valid non-crossed depth")
        if not isinstance(instrument.get("symbol"), str) or not instrument["symbol"]:
            raise ValueError("instrument symbol is required")
        if not isinstance(instrument.get("category"), str) or not instrument["category"]:
            raise ValueError("instrument category is required")
        fees = snapshot.get("fees")
        if fees is not None:
            if not isinstance(fees, dict):
                raise ValueError("snapshot fees must be an object")
            if fees:
                _finite_decimal(fees.get("taker_fee_rate"), "fees.taker_fee_rate", nonnegative=True)
                _finite_decimal(fees.get("maker_fee_rate"), "fees.maker_fee_rate", nonnegative=True)
                if fees.get("source") != "public_perps_fee_schedule":
                    raise ValueError("fees must come from the public Perps fee schedule")
        return {
            "instrument": instrument,
            "ticker": ticker,
            "book": book,
            "fees": fees or {},
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
                json.dumps(item["fees"], sort_keys=True, separators=(",", ":"), allow_nan=False),
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
                    bids_json, asks_json, instrument_json, ticker_json, fees_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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

    @staticmethod
    def _decode_snapshot_rows(rows: Any) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            item["bids"] = json.loads(item.pop("bids_json"))
            item["asks"] = json.loads(item.pop("asks_json"))
            item["instrument"] = json.loads(item.pop("instrument_json"))
            item["ticker"] = json.loads(item.pop("ticker_json"))
            item["fees"] = json.loads(item.pop("fees_json"))
            result.append(item)
        return result

    def snapshots(
        self,
        *,
        start_received_at: str | datetime | None = None,
        end_received_at: str | datetime | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return a bounded chronological multi-instrument replay window."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be an integer between 1 and 100000")
        start = _aware_iso(start_received_at, "start_received_at") if start_received_at is not None else None
        end = _aware_iso(end_received_at, "end_received_at") if end_received_at is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("start_received_at must not exceed end_received_at")
        clauses: list[str] = []
        params: list[Any] = []
        if start is not None:
            clauses.append("received_at >= ?")
            params.append(start)
        if end is not None:
            clauses.append("received_at <= ?")
            params.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM perps_market_snapshots{where} ORDER BY received_at ASC, snapshot_id ASC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        if len(rows) > limit:
            raise ValueError(f"replay window exceeds the {limit}-snapshot safety limit; narrow the time range")
        return self._decode_snapshot_rows(rows)

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
            item["fees"] = json.loads(item.pop("fees_json"))
            result.append(item)
        return result

    def history(
        self,
        instrument_id: int,
        limit: int = 100,
        *,
        as_of_received_at: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded, chronological snapshots for one instrument.

        The newest eligible rows are selected first so a bounded query still
        preserves the requested most recent window, then returned in ascending
        time order. When supplied, as_of_received_at is applied in SQL before
        LIMIT, preventing later snapshots from leaking into historical decisions.
        """
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        if isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        try:
            bounded = max(1, min(5000, int(limit)))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("limit must be an integer") from None
        as_of = _aware_iso(as_of_received_at, "as_of_received_at") if as_of_received_at is not None else None
        if as_of is None:
            rows = self.db.execute(
                """
                SELECT * FROM perps_market_snapshots
                WHERE instrument_id = ?
                ORDER BY book_timestamp_ms DESC, snapshot_id DESC
                LIMIT ?
                """,
                (instrument_id, bounded),
            ).fetchall()
        else:
            rows = self.db.execute(
                """
                SELECT * FROM perps_market_snapshots
                WHERE instrument_id = ? AND received_at <= ?
                ORDER BY book_timestamp_ms DESC, snapshot_id DESC
                LIMIT ?
                """,
                (instrument_id, as_of, bounded),
            ).fetchall()
        rows.reverse()
        return self._decode_snapshot_rows(rows)


    def record_funding_history(
        self,
        instrument_id: int,
        entries: list[dict[str, Any]],
        *,
        received_at: str | datetime | None = None,
    ) -> dict[str, int]:
        """Persist funding settlements and quarantine conflicting revisions.

        Repeated identical observations are idempotent. A different rate for an
        already-published instrument/timestamp pair is retained as a conflict;
        the first value is not silently treated as authoritative.
        """
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        if not isinstance(entries, list):
            raise ValueError("funding entries must be a list")
        capture = received_at or datetime.now(timezone.utc)
        parsed_capture = capture if isinstance(capture, datetime) else datetime.fromisoformat(str(capture).replace("Z", "+00:00"))
        if parsed_capture.tzinfo is None or parsed_capture.utcoffset() is None:
            raise ValueError("funding received_at must be timezone-aware")
        captured = parsed_capture.astimezone(timezone.utc).isoformat()
        rows = []
        seen: set[int] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("funding history row must be an object")
            if entry.get("instrument_id") != instrument_id:
                raise ValueError("funding instrument ID mismatch")
            timestamp_ms = entry.get("timestamp_ms")
            if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
                raise ValueError("funding timestamp_ms must be a positive integer")
            if timestamp_ms in seen:
                raise ValueError("duplicate funding timestamp in response")
            seen.add(timestamp_ms)
            rate = _finite_decimal(entry.get("funding_rate"), "funding_rate")
            rows.append((instrument_id, timestamp_ms, str(rate), captured))

        inserted = 0
        duplicates = 0
        conflicts = 0
        with self.db:
            for row_instrument_id, timestamp_ms, rate_text, received_text in rows:
                existing = self.db.execute(
                    """SELECT funding_rate FROM perps_funding_history
                       WHERE instrument_id=? AND funding_timestamp_ms=?""",
                    (row_instrument_id, timestamp_ms),
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        """INSERT INTO perps_funding_history
                           (instrument_id, funding_timestamp_ms, funding_rate, received_at)
                           VALUES (?,?,?,?)""",
                        (row_instrument_id, timestamp_ms, rate_text, received_text),
                    )
                    inserted += 1
                    continue

                original_rate = _finite_decimal(existing["funding_rate"], "stored funding_rate")
                observed_rate = _finite_decimal(rate_text, "observed funding_rate")
                if original_rate == observed_rate:
                    duplicates += 1
                    continue

                conflict = self.db.execute(
                    """SELECT observed_rates_json FROM perps_funding_conflicts
                       WHERE instrument_id=? AND funding_timestamp_ms=?""",
                    (row_instrument_id, timestamp_ms),
                ).fetchone()
                if conflict is None:
                    observed_rates = [str(original_rate), str(observed_rate)]
                    self.db.execute(
                        """INSERT INTO perps_funding_conflicts
                           (instrument_id, funding_timestamp_ms, observed_rates_json,
                            first_detected_at, last_detected_at)
                           VALUES (?,?,?,?,?)""",
                        (
                            row_instrument_id, timestamp_ms,
                            json.dumps(observed_rates, separators=(",", ":"), allow_nan=False),
                            received_text, received_text,
                        ),
                    )
                else:
                    try:
                        observed_rates = json.loads(conflict["observed_rates_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raise ValueError("stored funding conflict record is corrupt") from None
                    if not isinstance(observed_rates, list) or any(
                        not isinstance(value, str) for value in observed_rates
                    ):
                        raise ValueError("stored funding conflict record is corrupt")
                    if not any(
                        _finite_decimal(value, "stored conflicting funding_rate") == observed_rate
                        for value in observed_rates
                    ):
                        observed_rates.append(str(observed_rate))
                    self.db.execute(
                        """UPDATE perps_funding_conflicts
                           SET observed_rates_json=?, last_detected_at=?
                           WHERE instrument_id=? AND funding_timestamp_ms=?""",
                        (
                            json.dumps(observed_rates, separators=(",", ":"), allow_nan=False),
                            received_text, row_instrument_id, timestamp_ms,
                        ),
                    )
                conflicts += 1

        return {
            "received": len(rows),
            "inserted": inserted,
            "duplicates": duplicates,
            "conflicts": conflicts,
        }

    def funding_rate_at(
        self,
        instrument_id: int,
        timestamp_ms: int,
        *,
        tolerance_ms: int = 1000,
        as_of_received_at: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return one unambiguous published settlement nearest the scheduled time."""
        if isinstance(instrument_id, bool) or not isinstance(instrument_id, int) or instrument_id < 0:
            raise ValueError("instrument_id must be a nonnegative integer")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
            raise ValueError("timestamp_ms must be a positive integer")
        if isinstance(tolerance_ms, bool) or not isinstance(tolerance_ms, int) or not 0 <= tolerance_ms <= 5000:
            raise ValueError("tolerance_ms must be between 0 and 5000")
        as_of = _aware_iso(as_of_received_at, "as_of_received_at") if as_of_received_at is not None else None
        conflict = self.db.execute(
            """SELECT 1 FROM perps_funding_conflicts
               WHERE instrument_id=? AND funding_timestamp_ms BETWEEN ? AND ?
               LIMIT 1""",
            (instrument_id, timestamp_ms - tolerance_ms, timestamp_ms + tolerance_ms),
        ).fetchone()
        if conflict is not None:
            return None
        if as_of is None:
            rows = self.db.execute(
                """SELECT funding_timestamp_ms, funding_rate FROM perps_funding_history
                   WHERE instrument_id=? AND funding_timestamp_ms BETWEEN ? AND ?
                   ORDER BY ABS(funding_timestamp_ms - ?) LIMIT 2""",
                (instrument_id, timestamp_ms - tolerance_ms, timestamp_ms + tolerance_ms, timestamp_ms),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT funding_timestamp_ms, funding_rate FROM perps_funding_history
                   WHERE instrument_id=? AND funding_timestamp_ms BETWEEN ? AND ?
                     AND received_at <= ?
                   ORDER BY ABS(funding_timestamp_ms - ?) LIMIT 2""",
                (instrument_id, timestamp_ms - tolerance_ms, timestamp_ms + tolerance_ms, as_of, timestamp_ms),
            ).fetchall()
        if len(rows) != 1:
            return None
        return {
            "instrument_id": instrument_id,
            "timestamp_ms": int(rows[0]["funding_timestamp_ms"]),
            "funding_rate": str(rows[0]["funding_rate"]),
        }

    def count(self) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM perps_market_snapshots"
        ).fetchone()[0])

    def prune_before(self, cutoff: str | datetime) -> dict[str, int]:
        """Remove expired observations while preserving the configured retention window."""
        parsed = (
            cutoff if isinstance(cutoff, datetime)
            else datetime.fromisoformat(str(cutoff).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        normalized = parsed.astimezone(timezone.utc).isoformat()
        with self.db:
            snapshot_cursor = self.db.execute(
                "DELETE FROM perps_market_snapshots WHERE received_at < ?",
                (normalized,),
            )
            funding_cursor = self.db.execute(
                "DELETE FROM perps_funding_history WHERE received_at < ?",
                (normalized,),
            )
            conflict_cursor = self.db.execute(
                "DELETE FROM perps_funding_conflicts WHERE last_detected_at < ?",
                (normalized,),
            )
        snapshot_deleted = max(0, int(snapshot_cursor.rowcount))
        funding_deleted = max(0, int(funding_cursor.rowcount))
        conflicts_deleted = max(0, int(conflict_cursor.rowcount))
        return {
            "deleted": snapshot_deleted + funding_deleted + conflicts_deleted,
            "snapshot_rows_deleted": snapshot_deleted,
            "funding_rows_deleted": funding_deleted,
            "funding_conflicts_deleted": conflicts_deleted,
            "remaining": self.count(),
        }

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "PerpsMarketHistoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
