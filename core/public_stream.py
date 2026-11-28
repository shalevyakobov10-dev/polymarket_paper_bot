"""Optional public market subscribe helper using the ``websockets`` package.

The long-running collector remains ``market_stream.py`` (websocket-client).
This module is the small adapter the decision loop can call for a one-shot
dump without replacing that collector.
"""
from __future__ import annotations

import json
from typing import Any

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def build_subscribe_message(token_ids: list[str]) -> dict[str, Any]:
    tokens = []
    seen = set()
    for token in token_ids:
        text = str(token or "").strip()
        if text and text not in seen:
            seen.add(text)
            tokens.append(text)
    return {"assets_ids": tokens, "type": "market", "initial_dump": True}


def decode_market_frame(raw: str) -> list[dict[str, Any]]:
    if not raw or raw in {"PONG", "PING"}:
        return []
    payload = json.loads(raw)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


async def recv_initial_dump(token_ids: list[str], *, timeout: float = 8.0) -> list[dict[str, Any]]:
    """Open one public socket, read the initial dump, and close. No credentials."""
    import asyncio

    import websockets

    message = build_subscribe_message(token_ids)
    if not message["assets_ids"]:
        return []
    events: list[dict[str, Any]] = []
    async with websockets.connect(MARKET_WS_URL, open_timeout=timeout) as socket:
        await socket.send(json.dumps(message))
        while len(events) < 20:
            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
            events.extend(decode_market_frame(str(raw)))
            if events:
                break
    return events
