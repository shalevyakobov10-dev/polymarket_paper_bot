"""Read-only Polymarket CLOB market WebSocket collector.

Subscribes only to public market data. No account credentials, orders, or execution.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websocket

from paper_trading import rotate_jsonl_file

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def normalize_market_message(message: Any, received_at: str | None = None) -> list[dict[str, Any]]:
    """Normalize known public market-channel event shapes; ignore malformed messages."""
    if isinstance(message, list):
        payloads = message
    elif isinstance(message, dict):
        payloads = [message]
    else:
        return []
    observed_at = received_at or datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
        if event_type == "book":
            token_id = str(payload.get("asset_id") or "").strip()
            if not token_id:
                continue
            bids = payload.get("bids") if isinstance(payload.get("bids"), list) else []
            asks = payload.get("asks") if isinstance(payload.get("asks"), list) else []
            bid_prices = [_number(level.get("price")) for level in bids if isinstance(level, dict)]
            ask_prices = [_number(level.get("price")) for level in asks if isinstance(level, dict)]
            bid_prices = [value for value in bid_prices if value is not None and 0 < value < 1]
            ask_prices = [value for value in ask_prices if value is not None and 0 < value < 1]
            best_bid = max(bid_prices) if bid_prices else None
            best_ask = min(ask_prices) if ask_prices else None
            crossed = best_bid is not None and best_ask is not None and best_bid >= best_ask
            records.append({
                "event_type": "book",
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None and not crossed else None,
                "spread": best_ask - best_bid if best_bid is not None and best_ask is not None and not crossed else None,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "crossed_book": crossed,
                "observed_at": observed_at,
                "exchange_timestamp": payload.get("timestamp"),
                "source": "public_clob_market_websocket",
                "research_only": True,
                "execution": "none",
            })
        elif event_type == "price_change":
            changes = payload.get("price_changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                token_id = str(change.get("asset_id") or "").strip()
                if not token_id:
                    continue
                bid, ask = _number(change.get("best_bid")), _number(change.get("best_ask"))
                valid_bid = bid if bid is not None and 0 < bid < 1 else None
                valid_ask = ask if ask is not None and 0 < ask < 1 else None
                records.append({
                    "event_type": "price_change",
                    "token_id": token_id,
                    "price": _number(change.get("price")),
                    "size": _number(change.get("size")),
                    "side": str(change.get("side") or "")[:10],
                    "best_bid": valid_bid,
                    "best_ask": valid_ask,
                    "spread": valid_ask - valid_bid if valid_bid is not None and valid_ask is not None and valid_ask >= valid_bid else None,
                    "observed_at": observed_at,
                    "exchange_timestamp": payload.get("timestamp"),
                    "source": "public_clob_market_websocket",
                    "research_only": True,
                    "execution": "none",
                })
        elif event_type in {"last_trade_price", "best_bid_ask", "tick_size_change", "new_market", "market_resolved"}:
            record = {
                "event_type": event_type,
                "token_id": str(payload.get("asset_id") or "").strip(),
                "observed_at": observed_at,
                "exchange_timestamp": payload.get("timestamp"),
                "source": "public_clob_market_websocket",
                "research_only": True,
                "execution": "none",
            }
            for key in ("price", "size", "side", "best_bid", "best_ask", "spread", "old_tick_size", "new_tick_size", "question", "slug", "winning_asset_id", "winning_outcome"):
                if key in payload:
                    record[key] = payload[key]
            if event_type == "last_trade_price" and _number(record.get("price")) is None:
                continue
            records.append(record)
    return records


class MarketStreamCollector:
    """Collect a bounded public CLOB stream session and persist normalized events."""

    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.last_message_at: str | None = None
        self.messages_received = 0
        self.events_persisted = 0

    @staticmethod
    def select_tokens(rows: list[dict[str, Any]]) -> list[str]:
        try:
            limit = int(os.getenv("MARKET_WS_MAX_TOKENS", "80"))
        except (TypeError, ValueError):
            limit = 80
        limit = min(200, max(0, limit))
        tokens: list[str] = []
        seen: set[str] = set()
        for row in rows:
            token = str(row.get("token_id") or "").strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
            if len(tokens) >= limit:
                break
        return tokens

    def _persist(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_jsonl_file(self.log_path, 10 * 1024 * 1024, 5)
        with self.log_path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.events_persisted += len(records)

    def run_session(self, token_ids: list[str], max_seconds: int = 45) -> dict[str, Any]:
        tokens = list(dict.fromkeys(str(token).strip() for token in token_ids if str(token).strip()))
        if not tokens:
            return {"connected": False, "reason": "no_tokens", "events_persisted": 0}
        tokens = tokens[:200]
        started = time.monotonic()
        session_events = 0
        connection = None
        try:
            connection = websocket.create_connection(MARKET_WS_URL, timeout=12, enable_multithread=True)
            connection.send(json.dumps({"assets_ids": tokens, "type": "market", "initial_dump": True, "custom_feature_enabled": True}))
            last_heartbeat = time.monotonic()
            connection.settimeout(1.0)
            while time.monotonic() - started < max(5, min(120, int(max_seconds))):
                now = time.monotonic()
                if now - last_heartbeat >= 10:
                    connection.send("PING")
                    last_heartbeat = now
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw or raw == "PONG":
                    continue
                try:
                    decoded = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                records = normalize_market_message(decoded)
                if records:
                    self._persist(records)
                    session_events += len(records)
                    self.last_message_at = records[-1]["observed_at"]
                    self.messages_received += 1
            return {
                "connected": True,
                "reason": None,
                "subscribed_tokens": len(tokens),
                "events_persisted": session_events,
                "last_message_at": self.last_message_at,
            }
        except Exception as exc:
            return {
                "connected": False,
                "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
                "subscribed_tokens": len(tokens),
                "events_persisted": session_events,
                "last_message_at": self.last_message_at,
            }
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
