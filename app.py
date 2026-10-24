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
from urllib.request import Request, urlopen

from paper_trading import PaperTrader

GAMMA_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "60")))
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
    "outcomes_seen": 0,
    "signals_total": 0,
    "mode": "paper_only",
    "worker_running": False,
    "paper_trading": None,
}
_previous: dict[str, float] = {}
_paper_trader: PaperTrader | None = None
_paper_state_path: Path | None = None


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


def fetch_markets() -> list[dict]:
    request = Request(GAMMA_URL, headers={"User-Agent": "PolymarketPaperResearchBot/0.9"})
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Gamma API returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Gamma response was not a list")
    return [item for item in payload if isinstance(item, dict)]


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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def get_paper_trader() -> PaperTrader:
    global _paper_trader, _paper_state_path
    expected_path = (DATA_DIR / "paper_state.json").resolve()
    if _paper_trader is None or _paper_state_path != expected_path:
        _paper_trader = PaperTrader(expected_path)
        _paper_state_path = expected_path
    return _paper_trader


def poll_once() -> dict:
    with _lock:
        _state["last_poll_at"] = utc_now()
    markets = fetch_markets()
    rows = parse_outcomes(markets)
    paper_result = get_paper_trader().process_snapshot(rows)
    found = []
    for row in rows:
        key = row["token_id"] or f'{row["market_id"]}:{row["outcome"]}'
        old_price = _previous.get(key)
        _previous[key] = row["observed_price"]
        append_jsonl(SNAPSHOTS, row)
        if old_price is not None:
            delta = row["observed_price"] - old_price
            if abs(delta) >= 0.03:
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
            "outcomes_seen": len(rows),
            "signals_total": _state["signals_total"] + len(found),
            "paper_trading": paper_status,
        })
    return {
        "markets": len(markets),
        "outcomes": len(rows),
        "candidates": len(found),
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
        if self.path in ("/", "/status", "/health", "/paper"):
            with _lock:
                snapshot = dict(_state)
            if self.path == "/health":
                self._send(200, {"ok": True, "mode": "paper_only", "worker_running": snapshot["worker_running"]})
            elif self.path == "/paper":
                self._send(200, get_paper_trader().snapshot())
            else:
                self._send(200, {"service": "Polymarket Paper Research Bot", **snapshot})
        else:
            self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        return


def main() -> None:
    threading.Thread(target=worker_loop, daemon=True, name="market-poller").start()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Paper research worker listening on port {port}; polling every {POLL_SECONDS}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
