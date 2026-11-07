"""Conservative, proposition-aware headline evidence for binary event markets.

This module does not estimate probabilities. It recognizes a small set of explicit
headline patterns and abstains when the question, headline, context, or publisher
consensus is ambiguous. General prose sentiment never selects YES versus NO.
"""
from __future__ import annotations

import math
import re
from typing import Any

_TOKEN = re.compile(r"won't|wouldn't|couldn't|shouldn't|didn't|doesn't|isn't|aren't|can't|cannot|[a-z]+")
_NEGATORS = {"not", "never", "no", "without", "neither", "nor", "cannot", "can't", "won't", "wouldn't", "couldn't", "shouldn't", "didn't", "doesn't", "isn't", "aren't"}
_UNCERTAINTY = {
    "may", "might", "could", "possibly", "possible", "potential", "expected",
    "expects", "expect", "forecast", "forecasts", "projected", "projects",
    "rumor", "rumour", "reportedly", "allegedly", "speculation", "speculates",
}
_RATE_DOWN = {"cut", "cuts", "cutting", "lower", "lowers", "lowered", "reduce", "reduces", "reduced", "reduction", "ease", "eases", "eased"}
_RATE_UP = {"raise", "raises", "raised", "hike", "hikes", "hiked", "increase", "increases", "increased", "tighten", "tightens", "tightened"}
_RATE_HOLD = {"hold", "holds", "held", "maintain", "maintains", "maintained", "steady", "unchanged", "pause", "pauses", "paused"}

_RULES: dict[str, dict[str, set[str]]] = {
    "bill_passage": {
        "question": {"pass", "passes", "passed", "approve", "approves", "approved", "adopt", "adopts", "adopted"},
        "yes": {"pass", "passes", "passed", "approve", "approves", "approved", "adopt", "adopts", "adopted", "enact", "enacts", "enacted"},
        "no": {"reject", "rejects", "rejected", "veto", "vetoes", "vetoed", "fail", "fails", "failed", "block", "blocks", "blocked", "defeat", "defeats", "defeated"},
        "context": {"bill", "law", "legislation", "measure", "senate", "parliament", "congress", "parliamentary"},
    },
    "election_win": {
        "question": {"win", "wins", "won", "elect", "elects", "elected", "reelect", "reelected"},
        "yes": {"win", "wins", "won", "elect", "elects", "elected", "reelect", "reelected"},
        "no": {"lose", "loses", "lost", "concede", "concedes", "conceded", "defeat", "defeats", "defeated"},
        "context": {"election", "elections", "president", "presidential", "candidate", "candidates", "vote", "votes", "ballot", "primary"},
    },
    "product_launch": {
        "question": {"launch", "launches", "launched", "release", "releases", "released", "introduce", "introduced"},
        "yes": {"launches", "launched", "release", "releases", "released", "introduce", "introduces", "introduced", "unveil", "unveils", "unveiled"},
        "no": {"delay", "delays", "delayed", "postpone", "postpones", "postponed", "cancel", "cancels", "cancelled", "canceled", "scrap", "scraps", "scrapped"},
        "context": {"product", "company", "device", "phone", "iphone", "smartphone", "rocket", "mission", "service", "platform", "app", "model"},
    },
    "agreement": {
        "question": {"sign", "signs", "signed", "agree", "agrees", "agreed", "reach", "reaches", "reached", "conclude", "concludes", "concluded"},
        "yes": {"sign", "signs", "signed", "agree", "agrees", "agreed", "reach", "reaches", "reached", "conclude", "concludes", "concluded"},
        "no": {"reject", "rejects", "rejected", "withdraw", "withdraws", "withdrew", "withdrawn", "abandon", "abandons", "abandoned", "collapse", "collapses", "collapsed", "fail", "fails", "failed"},
        "context": {"deal", "agreement", "treaty", "ceasefire", "accord", "contract", "pact"},
    },
}


def _tokens(value: Any) -> list[str]:
    return [token.lower() for token in _TOKEN.findall(str(value or "").casefold())]


def _has_negation(tokens: list[str], index: int) -> bool:
    preceding = tokens[max(0, index - 3):index]
    return any(token in _NEGATORS or token.endswith("n't") for token in preceding)


def _unique_question_family(question: str) -> tuple[str | None, str | None]:
    tokens = set(_tokens(question))
    if not tokens or tokens & {"not", "never"}:
        return None, "negated_or_empty_market_question"
    rate_context = bool(tokens & {"rate", "rates", "interest", "fed", "federal", "reserve", "central", "bank"})
    if rate_context and tokens & (_RATE_DOWN | _RATE_UP | _RATE_HOLD):
        targets = []
        if tokens & _RATE_DOWN:
            targets.append("down")
        if tokens & _RATE_UP:
            targets.append("up")
        if tokens & _RATE_HOLD:
            targets.append("hold")
        if len(set(targets)) != 1:
            return None, "ambiguous_rate_question"
        return f"rate_{targets[0]}", None
    found = [name for name, rule in _RULES.items() if tokens & rule["question"]]
    if len(found) == 1:
        return found[0], None
    if len(found) > 1:
        return None, "multiple_event_families_in_question"
    return None, "unsupported_market_question_type"


def classify_event_outcome(question: str, headline: str) -> dict[str, Any]:
    """Return explicit YES/NO headline evidence or abstain."""
    question_tokens, title_tokens = _tokens(question), _tokens(headline)
    if len(question_tokens) < 3 or len(title_tokens) < 3:
        return {"direction": None, "status": "unscored", "reason": "insufficient_text", "family": None}
    family, reason = _unique_question_family(question)
    if family is None:
        return {"direction": None, "status": "unsupported", "reason": reason, "family": None}
    title_set = set(title_tokens)
    if title_set & _UNCERTAINTY:
        return {"direction": None, "status": "ambiguous", "reason": "speculative_headline", "family": family}

    cues: list[tuple[str, str, bool]] = []
    if family.startswith("rate_"):
        context = {"rate", "rates", "interest", "fed", "federal", "reserve", "central", "bank"}
        if not title_set & context:
            return {"direction": None, "status": "ambiguous", "reason": "headline_lacks_rate_context", "family": family}
        target = family.removeprefix("rate_")
        groups = {token: "down" for token in _RATE_DOWN}
        groups.update({token: "up" for token in _RATE_UP})
        groups.update({token: "hold" for token in _RATE_HOLD})
        for index, token in enumerate(title_tokens):
            if token in groups:
                cues.append((token, "YES" if groups[token] == target else "NO", _has_negation(title_tokens, index)))
        if not cues:
            return {"direction": None, "status": "no_event_cue", "reason": "no_explicit_rate_action", "family": family}
    else:
        rule = _RULES[family]
        if not title_set & rule["context"]:
            return {"direction": None, "status": "ambiguous", "reason": "headline_lacks_event_context", "family": family}
        for index, token in enumerate(title_tokens):
            if token in rule["yes"]:
                cues.append((token, "YES", _has_negation(title_tokens, index)))
            elif token in rule["no"]:
                cues.append((token, "NO", _has_negation(title_tokens, index)))
        if not cues:
            return {"direction": None, "status": "no_event_cue", "reason": "no_explicit_outcome_action", "family": family}

    mapped: set[str] = set()
    matched_cues = []
    for token, observed, negated in cues:
        if family.startswith("rate_") and negated and observed == "NO":
            return {"direction": None, "status": "ambiguous", "reason": "negated_opposing_rate_action", "family": family}
        mapped.add(("NO" if observed == "YES" else "YES") if negated else observed)
        matched_cues.append(token)
    if len(mapped) != 1:
        return {"direction": None, "status": "ambiguous", "reason": "conflicting_headline_cues", "family": family, "matched_cues": sorted(set(matched_cues))}
    return {
        "direction": next(iter(mapped)),
        "status": "decisive",
        "reason": "explicit_headline_outcome_cue",
        "family": family,
        "matched_cues": sorted(set(matched_cues)),
        "interpretation": "directional_headline_evidence_not_probability_or_event_verification",
        "research_only": True,
        "execution": "none",
    }


def aggregate_event_outcome_evidence(
    matches: list[dict[str, Any]],
    *,
    now_epoch: float,
    max_age_hours: float = 24.0,
    minimum_publishers: int = 2,
    minimum_consensus_ratio: float = 0.75,
) -> dict[str, Any]:
    """Count one non-conflicting fresh vote per publisher; otherwise abstain."""
    if not math.isfinite(float(now_epoch)) or max_age_hours <= 0 or minimum_publishers < 1:
        raise ValueError("invalid event evidence aggregation settings")
    if not 0.5 < minimum_consensus_ratio <= 1.0:
        raise ValueError("minimum_consensus_ratio must be in (0.5, 1]")
    publisher_directions: dict[str, set[str]] = {}
    article_count = stale_count = invalid_timestamp_count = ignored_ambiguous_count = 0
    for item in matches:
        if not isinstance(item, dict) or item.get("event_evidence_status") != "decisive":
            ignored_ambiguous_count += 1
            continue
        direction = str(item.get("event_direction") or "").upper()
        if direction not in {"YES", "NO"}:
            ignored_ambiguous_count += 1
            continue
        try:
            published = float(item.get("published_at_epoch"))
        except (TypeError, ValueError, OverflowError):
            invalid_timestamp_count += 1
            continue
        if not math.isfinite(published):
            invalid_timestamp_count += 1
            continue
        age_hours = (float(now_epoch) - published) / 3600.0
        if age_hours < -0.1 or age_hours > max_age_hours:
            stale_count += 1
            continue
        publisher = str(item.get("publisher_id") or item.get("source") or "").strip().casefold()
        if not publisher:
            continue
        publisher_directions.setdefault(publisher, set()).add(direction)
        article_count += 1

    yes_publishers = sum(1 for values in publisher_directions.values() if values == {"YES"})
    no_publishers = sum(1 for values in publisher_directions.values() if values == {"NO"})
    conflict_publishers = sum(1 for values in publisher_directions.values() if len(values) > 1)
    decisive_publishers = yes_publishers + no_publishers
    ratio = max(yes_publishers, no_publishers) / decisive_publishers if decisive_publishers else 0.0
    leading_direction = "YES" if yes_publishers > no_publishers else "NO" if no_publishers > yes_publishers else None
    qualified = bool(
        leading_direction is not None
        and decisive_publishers >= minimum_publishers
        and ratio >= minimum_consensus_ratio
    )
    direction = leading_direction if qualified else None
    score = (ratio if direction == "YES" else -ratio) if direction else None
    return {
        "score": round(score, 6) if score is not None else None,
        "direction": direction,
        "qualified": qualified,
        "publisher_count": decisive_publishers,
        "yes_publisher_count": yes_publishers,
        "no_publisher_count": no_publishers,
        "conflict_publisher_count": conflict_publishers,
        "article_count": article_count,
        "consensus_ratio": round(ratio, 6),
        "minimum_consensus_ratio": minimum_consensus_ratio,
        "minimum_publishers": minimum_publishers,
        "stale_article_count": stale_count,
        "invalid_timestamp_count": invalid_timestamp_count,
        "ignored_ambiguous_article_count": ignored_ambiguous_count,
        "method": "explicit_event_cue_publisher_consensus_v1",
        "interpretation": "signed_consensus_strength_not_probability_or_expected_value",
        "research_only": True,
        "execution": "none",
    }
