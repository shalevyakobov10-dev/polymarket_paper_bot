"""Fast local news sentiment scoring for offline paper research.

VADER is English-oriented. Non-English headlines are explicitly marked
unsupported rather than silently assigned a misleading score.
"""
from __future__ import annotations

import math
import re
from typing import Any

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:  # The runner remains importable; scoring fails closed.
    SentimentIntensityAnalyzer = None  # type: ignore[assignment,misc]

_ANALYZER = SentimentIntensityAnalyzer() if SentimentIntensityAnalyzer else None
_HEBREW_OR_ARABIC = re.compile(r"[\u0590-\u08FF]")
_WORD = re.compile(r"[A-Za-z]{2,}")


def analyze_news_sentiment(text: str) -> dict[str, Any]:
    """Return a bounded VADER score and explicit language/model diagnostics."""
    source = str(text or "").strip()
    if not source:
        return {
            "score": None, "label": "unscored", "confidence": 0.0,
            "supported": False, "reason": "empty_text", "model": "vader_lexicon",
        }
    if _ANALYZER is None:
        return {
            "score": None, "label": "unscored", "confidence": 0.0,
            "supported": False, "reason": "vader_dependency_missing", "model": "vader_lexicon",
        }
    letters = [char for char in source if char.isalpha()]
    non_latin = sum(1 for char in letters if _HEBREW_OR_ARABIC.match(char))
    latin_words = _WORD.findall(source)
    if not latin_words or (letters and non_latin / max(1, len(letters)) > 0.10):
        return {
            "score": None, "label": "unscored", "confidence": 0.0,
            "supported": False, "reason": "unsupported_language", "model": "vader_lexicon",
        }

    values = _ANALYZER.polarity_scores(source[:6000])
    compound = float(values.get("compound", 0.0))
    if not math.isfinite(compound):
        return {
            "score": None, "label": "unscored", "confidence": 0.0,
            "supported": False, "reason": "non_finite_model_output", "model": "vader_lexicon",
        }
    compound = max(-1.0, min(1.0, compound))
    label = "positive" if compound >= 0.15 else "negative" if compound <= -0.15 else "neutral"
    return {
        "score": round(compound, 6),
        "label": label,
        "confidence": round(abs(compound), 6),
        "positive": round(float(values.get("pos", 0.0)), 6),
        "negative": round(float(values.get("neg", 0.0)), 6),
        "neutral": round(float(values.get("neu", 0.0)), 6),
        "supported": True,
        "reason": None,
        "model": "vader_lexicon",
    }


def aggregate_market_news_sentiment(
    matches: list[dict[str, Any]],
    *,
    now_epoch: float,
    max_age_hours: float = 24.0,
    half_life_hours: float = 6.0,
    minimum_publishers: int = 2,
) -> dict[str, Any]:
    """Aggregate fresh matched headlines with publisher-level weighting.

    Each publisher contributes at most one weighted mean, reducing repeated
    headlines from a single outlet. This is a feature, not a probability.
    """
    if max_age_hours <= 0 or half_life_hours <= 0 or minimum_publishers < 1:
        raise ValueError("invalid sentiment aggregation policy")
    by_publisher: dict[str, list[tuple[float, float]]] = {}
    article_count = 0
    for item in matches:
        if not isinstance(item, dict) or item.get("sentiment_supported") is not True:
            continue
        try:
            score = float(item.get("sentiment_score"))
            relevance = float(item.get("relevance_score"))
            published = float(item.get("published_at_epoch"))
        except (TypeError, ValueError, OverflowError):
            continue
        age_hours = (now_epoch - published) / 3600.0
        if not all(math.isfinite(x) for x in (score, relevance, published, age_hours)):
            continue
        if not -1.0 <= score <= 1.0 or not 0.0 <= relevance <= 1.0:
            continue
        if age_hours < -0.1 or age_hours > max_age_hours:
            continue
        publisher = str(item.get("publisher_id") or item.get("source") or "").strip().casefold()
        if not publisher:
            continue
        weight = max(0.01, relevance) * math.exp(-max(0.0, age_hours) / half_life_hours)
        by_publisher.setdefault(publisher, []).append((score, weight))
        article_count += 1

    publisher_scores: list[float] = []
    for items in by_publisher.values():
        total_weight = sum(weight for _, weight in items)
        if total_weight > 0:
            publisher_scores.append(sum(score * weight for score, weight in items) / total_weight)

    publisher_count = len(publisher_scores)
    aggregate = (
        sum(publisher_scores) / publisher_count if publisher_count else None
    )
    confidence = (
        min(1.0, abs(aggregate) * math.sqrt(publisher_count / minimum_publishers))
        if aggregate is not None else 0.0
    )
    return {
        "score": round(aggregate, 6) if aggregate is not None else None,
        "confidence": round(confidence, 6),
        "publisher_count": publisher_count,
        "article_count": article_count,
        "qualified": publisher_count >= minimum_publishers and aggregate is not None,
        "method": "vader_publisher_weighted_time_decay_v1",
        "interpretation": "experimental_sentiment_feature_not_event_probability",
        "research_only": True,
        "execution": "none",
    }
