"""Settings for the autonomous research loop.

PAPER_MODE defaults on and LIVE_MODE defaults off. Setting LIVE_MODE does not
authorize a Polymarket order. Live posting stays coupled to
``live_order_preflight.LIVE_ORDER_SUBMISSION_IMPLEMENTED``, which this
repository keeps false and which environment variables cannot flip.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

VERIFIED_NEWS_DOMAINS = (
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "npr.org",
    "economist.com",
)
LIVE_ACK_PHRASE = "I_ACCEPT_REAL_USDC_LOSS"
SECRET_FIELDS = ("openai_api_key", "openrouter_api_key", "polymarket_private_key")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _float_env(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
    if value != value or value in {float("inf"), float("-inf")} or not low < value <= high:
        raise ValueError(f"{name} must be in ({low}, {high}]")
    return value


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
    if value < low or value > high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class BotSettings:
    paper_mode: bool = True
    live_mode: bool = False
    mode_note: str = "paper"
    ai_offline: bool = False
    min_consensus_score: float = 0.67
    max_position_usdc: float = 2.0
    max_daily_drawdown_usdc: float = 5.0
    kelly_fraction: float = 0.25
    min_edge_bps: int = 100
    min_confidence: float = 0.55
    max_markets: int = 5
    openai_model: str = "gpt-4.1-mini"
    arena_models: tuple[str, ...] = ("openai/gpt-4.1-mini", "google/gemini-2.5-flash")
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    polymarket_private_key: str = ""
    live_trading_ack: str = ""
    data_dir: str = "data"
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    @property
    def analyst_offline(self) -> bool:
        return self.ai_offline or not self.openai_api_key

    @property
    def arena_offline(self) -> bool:
        return self.ai_offline or not self.openrouter_api_key

    def public_dict(self) -> dict:
        return {
            "paper_mode": self.paper_mode,
            "live_mode": self.live_mode,
            "mode_note": self.mode_note,
            "ai_offline": self.ai_offline,
            "analyst_offline": self.analyst_offline,
            "arena_offline": self.arena_offline,
            "min_consensus_score": self.min_consensus_score,
            "max_position_usdc": self.max_position_usdc,
            "max_daily_drawdown_usdc": self.max_daily_drawdown_usdc,
            "kelly_fraction": self.kelly_fraction,
            "min_edge_bps": self.min_edge_bps,
            "min_confidence": self.min_confidence,
            "max_markets": self.max_markets,
            "openai_model": self.openai_model,
            "arena_models": list(self.arena_models),
            "openai_api_key_present": bool(self.openai_api_key),
            "openrouter_api_key_present": bool(self.openrouter_api_key),
            "polymarket_private_key_present": bool(self.polymarket_private_key),
            "live_trading_ack_present": bool(self.live_trading_ack),
            "data_dir": self.data_dir,
            "clob_host": self.clob_host,
            "chain_id": self.chain_id,
            "live_submission_implemented": False,
        }


def load_settings() -> BotSettings:
    """Read the process environment. Secrets stay on the object and out of logs."""
    paper_mode = _bool_env("PAPER_MODE", True)
    live_mode = _bool_env("LIVE_MODE", False)
    if paper_mode and live_mode:
        live_mode = False
        mode_note = "paper_wins_conflicting_live_flag"
    elif paper_mode:
        mode_note = "paper"
    elif live_mode:
        mode_note = "live_requested_submission_still_blocked"
    else:
        mode_note = "idle"
    models = tuple(
        part.strip()
        for part in os.getenv(
            "ARENA_MODELS",
            "openai/gpt-4.1-mini,google/gemini-2.5-flash",
        ).split(",")
        if part.strip()
    )
    if len(models) < 2:
        raise ValueError("ARENA_MODELS must list at least two models")
    private_key = ""
    if live_mode and not paper_mode:
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    return BotSettings(
        paper_mode=paper_mode,
        live_mode=live_mode,
        mode_note=mode_note,
        ai_offline=_bool_env("AI_OFFLINE", False),
        min_consensus_score=_float_env("MIN_CONSENSUS_SCORE", 0.67, low=0.0, high=1.0),
        max_position_usdc=_float_env("MAX_POSITION_USDC", 2.0, low=0.0, high=1_000_000.0),
        max_daily_drawdown_usdc=_float_env("MAX_DAILY_DRAWDOWN", 5.0, low=0.0, high=1_000_000.0),
        kelly_fraction=_float_env("KELLY_FRACTION", 0.25, low=0.0, high=1.0),
        min_edge_bps=_int_env("MIN_EDGE_BPS", 100, low=0, high=10_000),
        min_confidence=_float_env("MIN_CONFIDENCE", 0.55, low=0.0, high=1.0),
        max_markets=_int_env("MAX_MARKETS", 5, low=1, high=50),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        arena_models=models,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        polymarket_private_key=private_key,
        live_trading_ack=os.getenv("LIVE_TRADING_ACK", "").strip(),
        data_dir=os.getenv("DATA_DIR", "data").strip() or "data",
        clob_host=os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").strip()
        or "https://clob.polymarket.com",
        chain_id=_int_env("POLYMARKET_CHAIN_ID", 137, low=1, high=1_000_000),
    )
