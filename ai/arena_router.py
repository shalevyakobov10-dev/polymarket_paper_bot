"""Multi-model consensus modeled on Arena-style blind comparison.

Automated mode queries two or more OpenRouter models in parallel on the same
prompt. Manual mode is the Arena bridge, which parses a pasted winning reply
into the same vote shape. Neither mode submits an order.
"""
from __future__ import annotations

import json
import statistics
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from ai.chatgpt_analyst import (
    SYSTEM_PROMPT,
    AnalystView,
    Transport,
    build_market_prompt,
    default_transport,
    edge_bps,
    extract_json_object,
    normalize_view,
    side_for,
)
from config import BotSettings


@dataclass(frozen=True)
class ModelVote:
    model: str
    estimated_probability: float
    confidence_score: float
    side: str
    reasoning: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "estimated_probability": self.estimated_probability,
            "confidence_score": self.confidence_score,
            "side": self.side,
            "reasoning": self.reasoning,
            "error": self.error,
        }


@dataclass(frozen=True)
class Consensus:
    approved: bool
    consensus_score: float
    mean_probability: float
    mean_confidence: float
    edge_bps: int
    reason: str
    votes: tuple[ModelVote, ...]
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "consensus_score": self.consensus_score,
            "mean_probability": self.mean_probability,
            "mean_confidence": self.mean_confidence,
            "edge_bps": self.edge_bps,
            "reason": self.reason,
            "votes": [vote.as_dict() for vote in self.votes],
            "source": self.source,
        }


def score_votes(votes: list[ModelVote], *, market_price: float, min_edge_bps: int) -> tuple[float, str]:
    """Blend side agreement with probability dispersion. 1.0 is full agreement."""
    usable = [vote for vote in votes if vote.error is None]
    if len(usable) < 2:
        return 0.0, "insufficient_model_votes"
    sides = [vote.side for vote in usable]
    majority = max(set(sides), key=sides.count)
    side_agreement = sides.count(majority) / len(sides)
    spread = statistics.pstdev(vote.estimated_probability for vote in usable)
    probability_agreement = 1.0 - min(1.0, spread / 0.15)
    score = round(0.5 * side_agreement + 0.5 * probability_agreement, 6)
    if majority != "BUY":
        return score, "majority_not_buy"
    if edge_bps(
        statistics.fmean(vote.estimated_probability for vote in usable),
        market_price,
    ) < min_edge_bps:
        return score, "consensus_edge_below_minimum"
    return score, "consensus_ok"


def _offline_votes(analyst: AnalystView, settings: BotSettings) -> list[ModelVote]:
    """Mirror a fixture analyst with two labeled offline voters. Not live models."""
    models = settings.arena_models[:2] or ("offline-a", "offline-b")
    return [
        ModelVote(
            model=f"offline:{name}",
            estimated_probability=analyst.estimated_probability,
            confidence_score=analyst.confidence_score,
            side=analyst.side,
            reasoning="offline mirror of the fixture analyst; not an Arena vote",
        )
        for name in models
    ]


def consensus_from_votes(
    votes: list[ModelVote],
    *,
    market_price: float,
    settings: BotSettings,
    source: str,
) -> Consensus:
    usable = [vote for vote in votes if vote.error is None]
    score, reason = score_votes(votes, market_price=market_price, min_edge_bps=settings.min_edge_bps)
    if usable:
        mean_probability = statistics.fmean(vote.estimated_probability for vote in usable)
        mean_confidence = statistics.fmean(vote.confidence_score for vote in usable)
    else:
        mean_probability = market_price
        mean_confidence = 0.0
    computed_edge = edge_bps(mean_probability, market_price) if usable else 0
    approved = (
        reason == "consensus_ok"
        and score + 1e-12 >= settings.min_consensus_score
        and mean_confidence + 1e-12 >= settings.min_confidence
        and computed_edge >= settings.min_edge_bps
    )
    if reason == "consensus_ok" and score + 1e-12 < settings.min_consensus_score:
        reason = "consensus_below_threshold"
    elif reason == "consensus_ok" and mean_confidence + 1e-12 < settings.min_confidence:
        reason = "consensus_confidence_below_minimum"
        approved = False
    return Consensus(
        approved=approved,
        consensus_score=score,
        mean_probability=round(mean_probability, 8),
        mean_confidence=round(mean_confidence, 8),
        edge_bps=computed_edge,
        reason=reason if not approved else "approved",
        votes=tuple(votes),
        source=source,
    )


class ArenaRouter:
    def __init__(self, settings: BotSettings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or default_transport

    def evaluate(self, market: dict[str, Any], analyst: AnalystView | None = None) -> tuple[Consensus, str]:
        prompt = build_market_prompt(market)
        try:
            market_price = float(market.get("observed_price"))
        except (TypeError, ValueError):
            market_price = 0.5
        if self.settings.arena_offline:
            seed = analyst or AnalystView(
                estimated_probability=market_price,
                confidence_score=0.0,
                edge_bps=0,
                reasoning="offline arena had no analyst seed",
                risk_flags=("offline_abstain",),
                side="ABSTAIN",
                source="offline",
            )
            votes = _offline_votes(seed, self.settings)
            return (
                consensus_from_votes(votes, market_price=market_price, settings=self.settings, source="offline_mirror"),
                prompt,
            )
        votes = self._query_models(prompt, market_price)
        return (
            consensus_from_votes(votes, market_price=market_price, settings=self.settings, source="openrouter"),
            prompt,
        )

    def _query_models(self, prompt: str, market_price: float) -> list[ModelVote]:
        models = list(self.settings.arena_models)

        def query(model: str) -> ModelVote:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
            headers = {
                "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                "Content-Type": "application/json",
            }
            try:
                response = self.transport("https://openrouter.ai/api/v1/chat/completions", payload, headers)
                view = normalize_view(
                    response if isinstance(response, dict) else {},
                    market_price=market_price,
                    min_edge_bps=self.settings.min_edge_bps,
                    source=model,
                )
                if "invalid_model_output" in view.risk_flags:
                    return ModelVote(model, market_price, 0.0, "ABSTAIN", view.reasoning, error="invalid_model_output")
                return ModelVote(
                    model=model,
                    estimated_probability=view.estimated_probability,
                    confidence_score=view.confidence_score,
                    side=side_for(view.estimated_probability, market_price, min_edge_bps=0),
                    reasoning=view.reasoning,
                )
            except (OSError, TimeoutError, urllib.error.URLError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
                return ModelVote(model, market_price, 0.0, "ABSTAIN", "", error=f"{type(exc).__name__}: {exc}"[:240])

        votes: list[ModelVote] = []
        with ThreadPoolExecutor(max_workers=min(4, len(models))) as pool:
            futures = [pool.submit(query, model) for model in models]
            for future in as_completed(futures):
                votes.append(future.result())
        order = {model: index for index, model in enumerate(models)}
        votes.sort(key=lambda vote: order.get(vote.model, 99))
        return votes


def vote_from_pasted_response(text: str, *, model: str, market_price: float, min_edge_bps: int) -> ModelVote:
    """Parse the winning Arena reply pasted back from the manual bridge."""
    parsed = extract_json_object(text) or {}
    view = normalize_view(parsed, market_price=market_price, min_edge_bps=min_edge_bps, source=model)
    if "invalid_model_output" in view.risk_flags:
        return ModelVote(model, market_price, 0.0, "ABSTAIN", view.reasoning, error="invalid_model_output")
    return ModelVote(
        model=model,
        estimated_probability=view.estimated_probability,
        confidence_score=view.confidence_score,
        side=view.side,
        reasoning=view.reasoning,
    )
