"""One research cycle: analyst, Arena consensus, Kelly, then the paper engine.

Live mode reaches the CLOB gateway only after the same gates. The gateway
refuses to post while live submission is unimplemented.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from ai.arena_router import ArenaRouter
from ai.chatgpt_analyst import ChatGPTAnalyst
from config import LIVE_ACK_PHRASE, BotSettings
from core.live_gateway import LiveOrderGateway
from core.order_state_machine import (
    CONSENSUS_CHECKED,
    FILLED,
    REJECTED,
    RISK_CHECKED,
    SUBMITTED,
    OrderStateMachine,
)
from core.risk_manager import evaluate_sized_entry
from core.trade_log import TradeJournal
from live_order_preflight import REQUIRED_RELEASE_GATES
from paper_trading import PaperTrader


def _now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _stamp_book(market: dict[str, Any], now: datetime) -> dict[str, Any]:
    stamped = copy.deepcopy(market)
    book = stamped.get("orderbook")
    if isinstance(book, dict):
        book["observed_at"] = now.isoformat()
        book.setdefault("token_id", stamped.get("token_id"))
    return stamped


def _price(market: dict[str, Any]) -> float | None:
    try:
        price = float(market.get("observed_price"))
    except (TypeError, ValueError):
        return None
    if price != price or not 0.0 < price < 1.0:
        return None
    return price


def _opposing(snapshot: dict[str, Any], market: dict[str, Any]) -> bool:
    market_id = str(market.get("market_id") or "").strip()
    outcome = str(market.get("outcome") or "").strip()
    if not market_id or not outcome:
        return False
    for position in snapshot.get("positions_list") or []:
        same_market = str(position.get("market_id") or "").strip() == market_id
        other = str(position.get("outcome") or "").strip()
        if same_market and other and other != outcome:
            return True
    return False


def decide_and_execute(
    market: dict[str, Any],
    *,
    settings: BotSettings,
    trader: PaperTrader,
    machine: OrderStateMachine,
    analyst: ChatGPTAnalyst,
    arena: ArenaRouter,
    journal: TradeJournal,
    gateway: LiveOrderGateway | None = None,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    current = _now(now)
    stamped = _stamp_book(market, current)
    token_id = str(stamped.get("token_id") or "").strip()
    price = _price(stamped)
    key = idempotency_key or f"{token_id}:BUY:{current.strftime('%Y-%m-%dT%H')}"
    base = {
        "timestamp": current.isoformat(),
        "market_id": str(stamped.get("market_id") or ""),
        "token_id": token_id,
        "question": str(stamped.get("question") or "")[:240],
        "mode": "paper" if settings.paper_mode else ("live_blocked" if settings.live_mode else "idle"),
        "fill_price": "",
        "pnl": "",
    }
    if not token_id or price is None:
        result = {**base, "order_state": REJECTED, "order_reason": "invalid_market_row", "risk_reason": "invalid_market_row"}
        journal.write_decision(result)
        return result

    record, created = machine.begin(
        token_id=token_id,
        market_id=base["market_id"],
        side="BUY",
        idempotency_key=key,
    )
    if not created:
        result = {
            **base,
            "order_state": record.state,
            "order_reason": record.reason or "duplicate_order",
            "risk_reason": "duplicate_order",
            "order_id": record.order_id,
        }
        journal.write_decision(result)
        return result

    view, prompt = analyst.analyze(stamped)
    consensus, arena_prompt = arena.evaluate(stamped, view)
    result: dict[str, Any] = {
        **base,
        "order_id": record.order_id,
        "analyst_probability": view.estimated_probability,
        "analyst_confidence": view.confidence_score,
        "analyst_edge_bps": view.edge_bps,
        "analyst_side": view.side,
        "analyst_source": view.source,
        "reasoning": view.reasoning,
        "risk_flags": list(view.risk_flags),
        "consensus_score": consensus.consensus_score,
        "consensus_edge_bps": consensus.edge_bps,
        "consensus_reason": consensus.reason,
        "votes": consensus.as_dict()["votes"],
        "prompt": prompt[:4000],
        "arena_prompt": arena_prompt[:4000],
        "kelly_notional": 0.0,
        "risk_reason": "",
    }
    if not consensus.approved:
        machine.transition(record.order_id, REJECTED, reason=consensus.reason)
        result.update(order_state=REJECTED, order_reason=consensus.reason, risk_reason=consensus.reason)
        journal.write_decision(result)
        return result
    machine.transition(
        record.order_id,
        CONSENSUS_CHECKED,
        reason="consensus_approved",
        consensus_score=consensus.consensus_score,
    )

    snapshot = trader.snapshot()
    metrics = snapshot["metrics"]
    decision, notional = evaluate_sized_entry(
        probability=consensus.mean_probability,
        market_price=price,
        bankroll=float(metrics["equity"]),
        kelly_fraction=settings.kelly_fraction,
        max_position_usdc=settings.max_position_usdc,
        cash=float(metrics["cash"]),
        open_exposure=float(metrics["open_exposure"]),
        open_positions=int(metrics["open_positions"]),
        max_open_exposure_usd=trader.config.max_open_exposure_usd,
        max_open_positions=trader.config.max_open_positions,
        daily_realized_pnl=float(metrics["daily_realized_pnl"]),
        open_unrealized_pnl=float(metrics["unrealized_pnl"]),
        max_daily_drawdown_usdc=settings.max_daily_drawdown_usdc,
        fee_rate=trader.config.fee_rate,
        opposing_market_outcome_open=_opposing(snapshot, stamped),
        current_drawdown_pct=float(metrics["current_drawdown_pct"]),
        max_portfolio_drawdown_pct=trader.config.max_portfolio_drawdown_pct,
    )
    result["kelly_notional"] = notional
    result["risk_reason"] = decision.reason
    if not decision.approved:
        machine.transition(record.order_id, REJECTED, reason=decision.reason, notional=notional)
        result.update(order_state=REJECTED, order_reason=decision.reason)
        journal.write_decision(result)
        return result
    machine.transition(record.order_id, RISK_CHECKED, reason=decision.reason, notional=decision.approved_notional)

    if not settings.paper_mode:
        live = gateway or LiveOrderGateway(settings)
        book = stamped.get("orderbook") if isinstance(stamped.get("orderbook"), dict) else {}

        def _finite_book_number(key: str) -> float | None:
            try:
                value = float(book.get(key))
            except (TypeError, ValueError):
                return None
            if value != value or value in {float("inf"), float("-inf")}:
                return None
            return value

        best_ask = _finite_book_number("best_ask")
        best_bid = _finite_book_number("best_bid")
        tick_size = _finite_book_number("tick_size")
        minimum_order_size = _finite_book_number("min_order_size")
        limit_price = best_ask if best_ask is not None and 0.0 < best_ask < 1.0 else price
        size = float(
            (Decimal(str(decision.approved_notional)) / Decimal(str(limit_price))).quantize(
                Decimal("0.0001"), rounding=ROUND_DOWN
            )
        )
        kill_switch = not (
            settings.live_mode
            and not settings.paper_mode
            and settings.live_trading_ack == LIVE_ACK_PHRASE
        )
        try:
            observed_at = datetime.fromisoformat(str(book.get("observed_at")))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            book_age_seconds = max(0.0, (current - observed_at.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            book_age_seconds = None
        market_active = (
            best_bid is not None
            and best_ask is not None
            and 0.0 < best_bid <= best_ask < 1.0
        )
        # Every release gate is reported as unverified: the decision loop has no
        # evidence for any of them, so the preflight trail must not claim approvals.
        release_gates = {gate: False for gate in REQUIRED_RELEASE_GATES}
        preflight_args = {
            "token_id": token_id,
            "side": "BUY",
            "limit_price": limit_price,
            "size": size,
            "best_ask": best_ask,
            "tick_size": tick_size,
            "minimum_order_size": minimum_order_size,
            "book_age_seconds": book_age_seconds,
            "max_book_age_seconds": 30.0,
            "max_price_impact_bps": 100.0,
            "max_order_notional_usd": settings.max_position_usdc,
            "current_open_exposure_usd": float(metrics["open_exposure"]),
            "max_open_exposure_usd": trader.config.max_open_exposure_usd,
            "daily_realized_pnl_usd": float(metrics["daily_realized_pnl"]),
            "open_unrealized_pnl_usd": float(metrics["unrealized_pnl"]),
            "daily_loss_limit_usd": settings.max_daily_drawdown_usdc,
            "current_drawdown_pct": float(metrics["current_drawdown_pct"]),
            "max_drawdown_pct": trader.config.max_portfolio_drawdown_pct,
            "market_active": market_active,
            "kill_switch_engaged": kill_switch,
            "release_gates": release_gates,
        }
        live_result = live.execute(
            token_id=token_id,
            limit_price=limit_price,
            size=size,
            preflight_args=preflight_args,
        )
        machine.transition(record.order_id, REJECTED, reason=str(live_result.get("reason")), notional=decision.approved_notional)
        result.update(
            order_state=REJECTED,
            order_reason=live_result.get("reason"),
            live_execution=live_result.get("execution"),
            submitted=False,
            # Keep the honest preflight artifact in the audit trail.
            preflight=live_result.get("preflight"),
        )
        journal.write_decision(result)
        return result

    machine.transition(record.order_id, SUBMITTED, reason="paper_engine", notional=decision.approved_notional)
    opened, skip = trader.open_from_external_signal(
        stamped,
        now=current,
        requested_notional=decision.approved_notional,
        signal_label="ai_consensus",
    )
    if not opened:
        machine.transition(record.order_id, REJECTED, reason=skip or "paper_entry_skipped")
        result.update(order_state=REJECTED, order_reason=skip or "paper_entry_skipped")
        journal.write_decision(result)
        return result
    machine.transition(record.order_id, FILLED, reason="paper_fill", notional=float(opened.get("gross_cost") or 0.0))
    result.update(
        order_state=FILLED,
        order_reason="paper_fill",
        fill_price=opened.get("entry_price"),
        pnl=opened.get("unrealized_pnl"),
        paper_event="position_opened",
    )
    journal.write_decision(result)
    return result


def mark_open_positions(trader: PaperTrader, markets: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Let the existing paper engine update stops on tokens it already holds."""
    current = _now(now)
    snapshot = trader.snapshot()
    held = {
        str(position.get("token_id") or "")
        for position in snapshot.get("positions_list") or []
    }
    rows = [_stamp_book(market, current) for market in markets if str(market.get("token_id") or "") in held]
    if not rows:
        return {"opened": [], "closed": [], "candidates": []}
    return trader.process_snapshot(rows, current)
