"""Autonomous paper cycle.

Default mode is paper. The existing ``app.py`` research service is unchanged.
This entrypoint asks ChatGPT and the Arena router for a probability, sizes with
fractional Kelly, and simulates the fill through PaperTrader.

Live CLOB submission stays disabled. See core/live_gateway.py.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ai.arena_router import ArenaRouter
from ai.chatgpt_analyst import ChatGPTAnalyst
from config import load_settings
from core.decision_loop import decide_and_execute, mark_open_positions
from core.live_gateway import LiveOrderGateway
from core.order_state_machine import OrderStateMachine
from core.trade_log import TradeJournal
from paper_trading import PaperConfig, PaperTrader


def load_markets(path: str) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    markets = payload.get("markets", []) if isinstance(payload, dict) else payload
    if not isinstance(markets, list):
        raise ValueError("markets file must be a list or an object with markets")
    return [item for item in markets if isinstance(item, dict)]


def run_once(markets: list[dict], *, data_dir: str | None = None) -> dict:
    settings = load_settings()
    directory = Path(data_dir or settings.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    config = PaperConfig(
        max_position_usd=settings.max_position_usdc,
        daily_loss_limit_usd=settings.max_daily_drawdown_usdc,
        require_orderbook=True,
    )
    trader = PaperTrader(directory / "paper_state.json", config)
    machine = OrderStateMachine(directory / "order_state.json")
    journal = TradeJournal(directory)
    analyst = ChatGPTAnalyst(settings)
    arena = ArenaRouter(settings)
    gateway = LiveOrderGateway(settings)
    now = datetime.now(timezone.utc)
    exits = mark_open_positions(trader, markets, now)
    decisions = []
    for market in markets[: settings.max_markets]:
        decisions.append(
            decide_and_execute(
                market,
                settings=settings,
                trader=trader,
                machine=machine,
                analyst=analyst,
                arena=arena,
                journal=journal,
                gateway=gateway,
                now=now,
            )
        )
    journal.write_summary(decisions)
    snapshot = trader.snapshot()
    summary = {
        "mode": settings.mode_note,
        "paper_mode": settings.paper_mode,
        "live_mode": settings.live_mode,
        "live_submission_implemented": False,
        "markets": len(decisions),
        "filled": sum(1 for item in decisions if item.get("order_state") == "FILLED"),
        "rejected": sum(1 for item in decisions if item.get("order_state") == "REJECTED"),
        "closed": len(exits.get("closed") or []),
        "equity": snapshot["metrics"]["equity"],
        "open_positions": snapshot["metrics"]["open_positions"],
        "log_dir": str(directory),
    }
    (directory / "cycle_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one autonomous Polymarket paper cycle")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--markets-file", required=True)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--loop-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    markets = load_markets(args.markets_file)
    if args.loop_seconds > 0 and not args.once:
        import time

        while True:
            summary = run_once(markets, data_dir=args.data_dir)
            print(json.dumps(summary), flush=True)
            time.sleep(args.loop_seconds)
    summary = run_once(markets, data_dir=args.data_dir)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
