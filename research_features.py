"""Transparent, point-in-time market/news feature snapshots for research.

This module deliberately creates descriptive features only. It does not produce
forecasts, rankings, trade signals, or execution instructions.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable


def _finite_nonnegative(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def build_market_feature_rows(
    outcome_rows: list[dict[str, Any]],
    news_matches: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    news_window_hours: float = 24.0,
    max_headlines_per_market: int = 5,
) -> list[dict[str, Any]]:
    """Join Yes/No Gamma prices, Yes-token CLOB metrics and lexical news matches.

    News counts include only matches with a parseable publication timestamp inside
    the requested window. Missing/future timestamps are counted separately.
    Distinct source labels are not proof of independent reporting.
    """
    if news_window_hours <= 0:
        raise ValueError("news_window_hours must be positive")
    if max_headlines_per_market < 0:
        raise ValueError("max_headlines_per_market cannot be negative")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in outcome_rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get("market_id") or "").strip()
        outcome = str(row.get("outcome") or "").strip().casefold()
        if not market_id or outcome not in {"yes", "no"}:
            continue
        grouped.setdefault(market_id, {})[outcome] = row

    matches_by_market: dict[str, list[dict[str, Any]]] = {}
    for match in news_matches:
        if not isinstance(match, dict):
            continue
        market_id = str(match.get("market_id") or "").strip()
        if market_id:
            matches_by_market.setdefault(market_id, []).append(match)

    output: list[dict[str, Any]] = []
    for market_id, outcomes in grouped.items():
        yes = outcomes.get("yes")
        if not yes:
            continue
        no = outcomes.get("no")
        yes_price = _finite_nonnegative(yes.get("observed_price"))
        no_price = _finite_nonnegative(no.get("observed_price")) if no else None
        if yes_price is None or yes_price > 1.0:
            continue
        if no_price is not None and no_price > 1.0:
            no_price = None

        book = yes.get("orderbook") if isinstance(yes.get("orderbook"), dict) else {}
        quality = yes.get("market_quality") if isinstance(yes.get("market_quality"), dict) else {}
        book_time = _parse_timestamp(book.get("observed_at"))
        book_age = (current - book_time).total_seconds() if book_time else None
        if book_age is not None:
            book_age = round(book_age, 3)

        fresh_matches: list[tuple[datetime, dict[str, Any]]] = []
        unknown_time = 0
        future_time = 0
        for match in matches_by_market.get(market_id, []):
            published = _parse_timestamp(match.get("news_published_at"))
            if published is None:
                unknown_time += 1
                continue
            age = (current - published).total_seconds()
            if age < -300:
                future_time += 1
                continue
            if age <= news_window_hours * 3600:
                fresh_matches.append((published, match))
        fresh_matches.sort(key=lambda item: item[0], reverse=True)

        source_labels = {
            str(match.get("source") or "").strip().casefold()
            for _, match in fresh_matches
            if str(match.get("source") or "").strip()
        }
        headline_rows = []
        for published, match in fresh_matches[:max_headlines_per_market]:
            headline_rows.append({
                "source": str(match.get("source") or "")[:100],
                "title": str(match.get("news_title") or "")[:400],
                "published_at": published.isoformat(),
                "lexical_overlap": _finite_nonnegative(match.get("lexical_overlap")),
                "shared_term_count": int(_finite_nonnegative(match.get("shared_term_count")) or 0),
            })

        fee_schedule = yes.get("fee_schedule") if isinstance(yes.get("fee_schedule"), dict) else {}
        fee_rate = _finite_nonnegative(fee_schedule.get("rate"))
        fee_exponent = _finite_nonnegative(fee_schedule.get("exponent"))
        output.append({
            "schema_version": 1,
            "observed_at": current.isoformat(),
            "market_id": market_id,
            "token_id_yes": str(yes.get("token_id") or ""),
            "token_id_no": str(no.get("token_id") or "") if no else "",
            "question": str(yes.get("question") or "")[:500],
            "gamma_yes_probability": round(yes_price, 8),
            "gamma_no_probability": round(no_price, 8) if no_price is not None else None,
            "gamma_binary_sum": round(yes_price + no_price, 8) if no_price is not None else None,
            "volume_24h": _finite_nonnegative(yes.get("volume_24h")),
            "fees_enabled": yes.get("fees_enabled"),
            "fee_type": str(yes.get("fee_type") or "")[:80] or None,
            "fee_schedule_rate": fee_rate,
            "fee_schedule_exponent": fee_exponent,
            "fee_schedule_source": "gamma_fee_schedule" if fee_rate is not None else None,
            "clob_book_present": bool(book),
            "clob_book_usable": bool(book.get("book_usable")),
            "clob_book_quality_eligible": quality.get("eligible"),
            "clob_book_quality_reason": str(quality.get("reason") or "")[:80] or None,
            "clob_book_age_seconds": book_age,
            "clob_best_bid": _finite_nonnegative(book.get("best_bid")),
            "clob_best_ask": _finite_nonnegative(book.get("best_ask")),
            "clob_midpoint": _finite_nonnegative(book.get("midpoint")),
            "clob_spread_bps": _finite_nonnegative(book.get("spread_bps")),
            "clob_bid_depth_usd_top5": _finite_nonnegative(book.get("bid_depth_usd_top5")),
            "clob_ask_depth_usd_top5": _finite_nonnegative(book.get("ask_depth_usd_top5")),
            "clob_depth_imbalance_top5": book.get("depth_imbalance_top5"),
            "news_match_count_24h": len(fresh_matches),
            "news_unique_source_label_count_24h": len(source_labels),
            "news_matches_without_publish_time": unknown_time,
            "news_matches_with_future_publish_time": future_time,
            "news_window_hours": float(news_window_hours),
            "news_matching_method": "existing_lexical_keyword_overlap",
            "news_source_count_interpretation": "distinct_source_labels_not_verified_independent_reporting",
            "news_headlines": headline_rows,
            "research_only": True,
            "forecast": None,
            "execution": "none",
        })

    output.sort(key=lambda row: (row["volume_24h"] is not None, row["volume_24h"] or 0.0), reverse=True)
    return output


def summarize_feature_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize coverage and observed inputs; intentionally no score or forecast."""
    spreads = [
        float(value) for row in rows
        if (value := _finite_nonnegative(row.get("clob_spread_bps"))) is not None
    ]
    depths = [
        float(row["clob_bid_depth_usd_top5"]) + float(row["clob_ask_depth_usd_top5"])
        for row in rows
        if _finite_nonnegative(row.get("clob_bid_depth_usd_top5")) is not None
        and _finite_nonnegative(row.get("clob_ask_depth_usd_top5")) is not None
    ]
    return {
        "markets_in_snapshot": len(rows),
        "markets_with_usable_clob_book": sum(bool(row.get("clob_book_usable")) for row in rows),
        "markets_with_quality_eligible_book": sum(row.get("clob_book_quality_eligible") is True for row in rows),
        "markets_with_24h_news_matches": sum((row.get("news_match_count_24h") or 0) > 0 for row in rows),
        "news_matches_24h_total": sum(int(row.get("news_match_count_24h") or 0) for row in rows),
        "mean_observed_spread_bps": _mean(spreads),
        "mean_visible_top5_depth_usd_both_sides": _mean(depths),
        "interpretation": "descriptive_data_coverage_only_not_a_probability_forecast_or_trading_edge",
        "research_only": True,
        "execution": "none",
    }


def read_recent_jsonl(paths: Iterable[str | Path], *, limit: int = 100) -> list[dict[str, Any]]:
    """Read bounded recent records from rotated JSONL files, oldest file first."""
    if limit <= 0:
        return []
    records: list[dict[str, Any]] = []
    for path_value in reversed([Path(path) for path in paths]):
        try:
            with path_value.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            continue
    return records[-limit:]
