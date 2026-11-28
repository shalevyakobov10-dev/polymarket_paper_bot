"""ChatGPT research pass for one Polymarket outcome.

Calls the OpenAI Responses API with web search limited to verified news
domains. Edge is recomputed locally from the model probability and the
observed contract price so a model cannot invent the edge figure.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

from config import VERIFIED_NEWS_DOMAINS, BotSettings

ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "estimated_probability": {"type": "number"},
        "confidence_score": {"type": "number"},
        "edge_bps": {"type": "integer"},
        "reasoning": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "estimated_probability",
        "confidence_score",
        "edge_bps",
        "reasoning",
        "risk_flags",
    ],
}

SYSTEM_PROMPT = (
    "You are a prediction-market research analyst. Estimate the probability that "
    "the specified outcome token pays 1.00 USDC. Use only the supplied market "
    "context and, when web search is available, pages on the allowed news domains. "
    "Do not invent prices, citations, or a trading instruction. Return JSON only."
)

Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class AnalystView:
    estimated_probability: float
    confidence_score: float
    edge_bps: int
    reasoning: str
    risk_flags: tuple[str, ...]
    side: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_flags"] = list(self.risk_flags)
        return payload


def build_market_prompt(market: dict[str, Any]) -> str:
    book = market.get("orderbook") if isinstance(market.get("orderbook"), dict) else {}
    headlines = market.get("headlines") if isinstance(market.get("headlines"), list) else []
    lines = [
        f"Question: {str(market.get('question') or '')[:500]}",
        f"Outcome token: {str(market.get('outcome') or '')[:120]}",
        f"Market id: {str(market.get('market_id') or '')[:120]}",
        f"Rules: {str(market.get('rules') or market.get('description') or '')[:2000]}",
        f"Observed outcome price: {market.get('observed_price')}",
        f"Best bid: {book.get('best_bid')}",
        f"Best ask: {book.get('best_ask')}",
        f"Spread bps: {book.get('spread_bps')}",
        f"Top-5 ask depth USDC: {book.get('ask_depth_usd_top5')}",
        "Headlines:",
    ]
    if not headlines:
        lines.append("- none supplied")
    for item in headlines[:8]:
        lines.append(f"- {str(item)[:300]}")
    lines.append(
        "Return estimated_probability for THIS outcome token, confidence_score, "
        "edge_bps versus the observed price, reasoning, and risk_flags."
    )
    return "\n".join(lines)


def edge_bps(probability: float, market_price: float) -> int:
    return int(round((float(probability) - float(market_price)) * 10000))


def side_for(probability: float, market_price: float, *, min_edge_bps: int) -> str:
    if edge_bps(probability, market_price) >= int(min_edge_bps):
        return "BUY"
    return "ABSTAIN"


def _finite_unit(value: Any, *, allow_zero: bool) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    lower = 0.0 if allow_zero else 0.0
    if allow_zero:
        if 0.0 <= parsed <= 1.0:
            return parsed
        return None
    if lower < parsed < 1.0:
        return parsed
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    output = payload.get("output")
    chunks: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
            elif isinstance(item.get("text"), str):
                chunks.append(item["text"])
    if chunks:
        return "\n".join(chunks)
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def normalize_view(
    payload: dict[str, Any],
    *,
    market_price: float,
    min_edge_bps: int,
    source: str,
) -> AnalystView:
    parsed = payload if "estimated_probability" in payload else extract_json_object(_response_text(payload))
    if not isinstance(parsed, dict):
        parsed = extract_json_object(json.dumps(payload))
    probability = _finite_unit((parsed or {}).get("estimated_probability"), allow_zero=False)
    confidence = _finite_unit((parsed or {}).get("confidence_score"), allow_zero=True)
    if probability is None or confidence is None or not isinstance(parsed, dict):
        return AnalystView(
            estimated_probability=float(market_price),
            confidence_score=0.0,
            edge_bps=0,
            reasoning="model output was missing a usable probability",
            risk_flags=("invalid_model_output",),
            side="ABSTAIN",
            source=source,
        )
    flags = []
    for flag in parsed.get("risk_flags") or []:
        text = str(flag).strip()
        if text:
            flags.append(text[:120])
    reasoning = str(parsed.get("reasoning") or "").strip()[:2000]
    computed = edge_bps(probability, market_price)
    return AnalystView(
        estimated_probability=probability,
        confidence_score=confidence,
        edge_bps=computed,
        reasoning=reasoning,
        risk_flags=tuple(flags),
        side=side_for(probability, market_price, min_edge_bps=min_edge_bps),
        source=source,
    )


def offline_view(market: dict[str, Any], *, min_edge_bps: int) -> AnalystView:
    """Deterministic abstain unless the caller supplied a research fixture."""
    try:
        price = float(market.get("observed_price"))
    except (TypeError, ValueError):
        price = 0.5
    estimate = _finite_unit(market.get("research_estimate"), allow_zero=False)
    confidence = _finite_unit(market.get("research_confidence"), allow_zero=True)
    if estimate is None or confidence is None:
        return AnalystView(
            estimated_probability=price if 0.0 < price < 1.0 else 0.5,
            confidence_score=0.0,
            edge_bps=0,
            reasoning="offline mode has no research_estimate; abstaining",
            risk_flags=("offline_abstain",),
            side="ABSTAIN",
            source="offline",
        )
    return AnalystView(
        estimated_probability=estimate,
        confidence_score=confidence,
        edge_bps=edge_bps(estimate, price),
        reasoning="offline fixture research_estimate; not a live model call",
        risk_flags=("offline_fixture",),
        side=side_for(estimate, price, min_edge_bps=min_edge_bps),
        source="offline",
    )


def abstain_error(reason: str, market_price: float) -> AnalystView:
    return AnalystView(
        estimated_probability=market_price,
        confidence_score=0.0,
        edge_bps=0,
        reasoning=reason[:500],
        risk_flags=("model_request_failed",),
        side="ABSTAIN",
        source="error",
    )


def default_transport(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"http_{exc.code}:{detail}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("openai response was not an object")
    return parsed


class ChatGPTAnalyst:
    def __init__(self, settings: BotSettings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or default_transport

    def analyze(self, market: dict[str, Any]) -> tuple[AnalystView, str]:
        prompt = build_market_prompt(market)
        try:
            market_price = float(market.get("observed_price"))
        except (TypeError, ValueError):
            market_price = 0.5
        if self.settings.analyst_offline:
            return offline_view(market, min_edge_bps=self.settings.min_edge_bps), prompt
        request_payload = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "tools": [
                {
                    "type": "web_search",
                    "filters": {"allowed_domains": list(VERIFIED_NEWS_DOMAINS)},
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "polymarket_analyst",
                    "strict": True,
                    "schema": ANALYST_SCHEMA,
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        try:
            payload = self.transport("https://api.openai.com/v1/responses", request_payload, headers)
        except (OSError, TimeoutError, urllib.error.URLError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
            return abstain_error(f"{type(exc).__name__}: {exc}", market_price), prompt
        return (
            normalize_view(
                payload,
                market_price=market_price,
                min_edge_bps=self.settings.min_edge_bps,
                source="openai_responses",
            ),
            prompt,
        )
