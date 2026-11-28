"""Manual Arena bridge.

Formats the same research prompt used by the automated router, and parses a
pasted winning Arena reply back into a model vote. The optional webhook only
translates text. It cannot submit an order.

Examples:
  python arena_bridge.py prompt --markets-file tests/fixtures/ai_cycle_markets.json
  python arena_bridge.py parse --text-file winning.json --price 0.42
  python arena_bridge.py serve --port 8091
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai.arena_router import consensus_from_votes, vote_from_pasted_response
from ai.chatgpt_analyst import ANALYST_SCHEMA, build_market_prompt, edge_bps
from config import BotSettings


def load_markets(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        markets = payload.get("markets", [])
    else:
        markets = payload
    if not isinstance(markets, list):
        raise ValueError("markets file must be a list or an object with markets")
    return [item for item in markets if isinstance(item, dict)]


def prompt_document(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": build_market_prompt(market),
        "schema": ANALYST_SCHEMA,
        "instructions": (
            "Paste this prompt into Arena. Paste only the winning model's JSON "
            "back into `arena_bridge.py parse` or POST /v1/result."
        ),
        "execution": "none",
    }


def parse_result(text: str, *, market_price: float, min_edge_bps: int = 100, min_consensus_score: float = 0.67) -> dict[str, Any]:
    settings = BotSettings(min_edge_bps=min_edge_bps, min_consensus_score=min_consensus_score)
    vote = vote_from_pasted_response(
        text,
        model="arena-manual-winner",
        market_price=market_price,
        min_edge_bps=min_edge_bps,
    )
    vote_payload = vote.as_dict()
    vote_payload["edge_bps"] = edge_bps(vote.estimated_probability, market_price)
    # A single pasted winner cannot satisfy the two-model automated threshold.
    # Callers that want it to count must pair it with another vote. The bridge
    # reports the parsed vote and a one-vote consensus that stays unapproved.
    consensus = consensus_from_votes([vote], market_price=market_price, settings=settings, source="arena_manual")
    return {"vote": vote_payload, "consensus": consensus.as_dict(), "execution": "none"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok", "mode": "arena_bridge", "execution": "none"})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            self._send(400, {"error": "invalid_json"})
            return
        if path == "/v1/prompt":
            market = payload.get("market", payload)
            if not isinstance(market, dict):
                self._send(400, {"error": "market_object_required"})
                return
            self._send(200, prompt_document(market))
            return
        if path == "/v1/result":
            try:
                price = float(payload.get("market_price"))
            except (TypeError, ValueError):
                self._send(400, {"error": "market_price_required"})
                return
            text = payload.get("response", "")
            self._send(200, parse_result(str(text), market_price=price))
            return
        self._send(404, {"error": "not_found"})


def serve(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Format or parse an Arena AI research prompt")
    sub = parser.add_subparsers(dest="command", required=True)
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--markets-file", required=True)
    prompt.add_argument("--index", type=int, default=0)
    parsed = sub.add_parser("parse")
    parsed.add_argument("--text-file", required=True)
    parsed.add_argument("--price", type=float, required=True)
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8091)
    args = parser.parse_args(argv)
    if args.command == "prompt":
        markets = load_markets(args.markets_file)
        print(json.dumps(prompt_document(markets[args.index]), indent=2))
        return 0
    if args.command == "parse":
        text = Path(args.text_file).read_text(encoding="utf-8")
        print(json.dumps(parse_result(text, market_price=args.price), indent=2))
        return 0
    httpd = serve(args.host, args.port)
    print(f"Arena bridge listening on {args.host}:{args.port}; execution=none")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
