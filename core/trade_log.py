"""CSV and JSONL audit log for prompts, probabilities, fills, and PnL."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


CSV_FIELDS = (
    "timestamp",
    "market_id",
    "token_id",
    "question",
    "mode",
    "analyst_probability",
    "analyst_confidence",
    "analyst_edge_bps",
    "consensus_score",
    "consensus_edge_bps",
    "kelly_notional",
    "risk_reason",
    "order_state",
    "order_reason",
    "fill_price",
    "pnl",
)


class TradeJournal:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.directory / "ai_decisions.jsonl"
        self.csv_path = self.directory / "ai_decisions.csv"
        self.summary_path = self.directory / "pnl_summary.csv"
        self._csv_ready = self.csv_path.exists() and self.csv_path.stat().st_size > 0

    def write_decision(self, record: dict[str, Any]) -> None:
        safe = _redact(record)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, allow_nan=False, default=str) + "\n")
        row = {field: safe.get(field, "") for field in CSV_FIELDS}
        with self.csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if not self._csv_ready:
                writer.writeheader()
                self._csv_ready = True
            writer.writerow(row)

    def write_summary(self, rows: list[dict[str, Any]]) -> None:
        """Write a one-row-per-decision PnL table. Uses pandas when it is installed."""
        safe_rows = [_redact(row) for row in rows]
        try:
            import pandas as pd
        except ImportError:
            with self.summary_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for row in safe_rows:
                    writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
            return
        frame = pd.DataFrame(safe_rows)
        if frame.empty:
            frame = pd.DataFrame(columns=list(CSV_FIELDS))
        else:
            for field in CSV_FIELDS:
                if field not in frame.columns:
                    frame[field] = ""
            frame = frame.loc[:, list(CSV_FIELDS)]
        frame.to_csv(self.summary_path, index=False)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"openai_api_key", "openrouter_api_key", "polymarket_private_key", "authorization"}:
                cleaned[key] = "present" if item else ""
            elif "private_key" in lowered or lowered.endswith("_api_key"):
                cleaned[key] = "present" if item else ""
            else:
                cleaned[key] = _redact(item)
        return cleaned
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value
