"""Durable fail-closed idempotency ledger for a future order adapter.

This module does not connect to Polymarket, sign, or submit orders. The caller
must persist an intent before any network submission and must reconcile
ambiguous outcomes instead of blindly retrying. The JSON ledger is suitable
for a single process only; production multi-worker deployments need a
transactional shared store with equivalent uniqueness guarantees.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TERMINAL_STATUSES = {
    "accepted", "rejected", "reconciled_open", "reconciled_filled",
    "reconciled_canceled", "reconciled_rejected", "reconciled_not_found",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_text(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite decimal") from None
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_order(order: dict[str, Any]) -> dict[str, str]:
    if not isinstance(order, dict):
        raise ValueError("order must be an object")
    token_id = order.get("token_id")
    side = order.get("side")
    if not isinstance(token_id, str) or not token_id.strip():
        raise ValueError("token_id must be a non-empty string")
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    price = _decimal_text(order.get("price"), "price")
    if not Decimal(price) < Decimal("1"):
        raise ValueError("price must be less than 1")
    size = _decimal_text(order.get("size"), "size")
    market_id = order.get("market_id", "")
    if not isinstance(market_id, str):
        raise ValueError("market_id must be a string")
    return {
        "market_id": market_id.strip(),
        "token_id": token_id.strip(),
        "side": side,
        "price": price,
        "size": size,
    }


class OrderIntentLedger:
    """Persist one logical order intent and prevent blind duplicate submission."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "intents": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read order-intent ledger; refusing to reset: {exc}") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("schema_version") != SCHEMA_VERSION
            or not isinstance(loaded.get("intents"), dict)
        ):
            raise ValueError("Order-intent ledger has an unsupported or malformed schema")
        for key, record in loaded["intents"].items():
            if (
                not isinstance(key, str)
                or not isinstance(record, dict)
                or record.get("status") not in {
                    "prepared", "submission_started", "unknown", *TERMINAL_STATUSES
                }
                or not isinstance(record.get("payload_hash"), str)
                or not isinstance(record.get("order"), dict)
            ):
                raise ValueError("Order-intent ledger contains a malformed record; refusing to continue")
        return loaded

    def _save(self) -> None:
        encoded = json.dumps(self._state, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    @staticmethod
    def _key(intent_id: str) -> str:
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise ValueError("intent_id must be a non-empty stable identifier")
        return hashlib.sha256(intent_id.strip().encode("utf-8")).hexdigest()

    def prepare(self, intent_id: str, order: dict[str, Any]) -> dict[str, Any]:
        """Create or retrieve an intent; reusing its ID with changed terms is rejected."""
        key = self._key(intent_id)
        canonical = _canonical_order(order)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock:
            existing = self._state["intents"].get(key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ValueError("intent_id already exists with different order terms")
                return copy.deepcopy(existing)
            record = {
                "intent_id": intent_id.strip(),
                "order": canonical,
                "payload_hash": payload_hash,
                "status": "prepared",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "submission_attempts": 0,
                "result": None,
            }
            self._state["intents"][key] = record
            self._save()
            return copy.deepcopy(record)

    def begin_submission(self, intent_id: str) -> bool:
        """Durably claim the one permitted attempt before making a network call."""
        key = self._key(intent_id)
        with self._lock:
            record = self._state["intents"].get(key)
            if record is None:
                raise KeyError("intent must be prepared before submission")
            if record["status"] != "prepared":
                return False
            record["status"] = "submission_started"
            record["submission_attempts"] += 1
            record["updated_at"] = _utc_now()
            self._save()
            return True

    def record_submission_result(self, intent_id: str, result: str) -> dict[str, Any]:
        """Record accepted/rejected/unknown; unknown is never safe to blindly retry."""
        if result not in {"accepted", "rejected", "unknown"}:
            raise ValueError("result must be accepted, rejected, or unknown")
        key = self._key(intent_id)
        with self._lock:
            record = self._state["intents"].get(key)
            if record is None:
                raise KeyError("unknown order intent")
            if record["status"] != "submission_started":
                raise ValueError("submission result requires submission_started status")
            record["status"] = result
            record["result"] = result
            record["updated_at"] = _utc_now()
            self._save()
            return copy.deepcopy(record)

    def reconcile(self, intent_id: str, outcome: str, evidence: str) -> dict[str, Any]:
        """Record an external lookup result; this never re-enables submission."""
        allowed = {"open", "filled", "canceled", "rejected", "not_found"}
        if outcome not in allowed:
            raise ValueError(f"outcome must be one of {sorted(allowed)}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("evidence must identify the reconciliation source")
        key = self._key(intent_id)
        with self._lock:
            record = self._state["intents"].get(key)
            if record is None:
                raise KeyError("unknown order intent")
            if record["status"] not in {"submission_started", "unknown"}:
                raise ValueError("only ambiguous submissions can be reconciled")
            record["status"] = f"reconciled_{outcome}"
            record["result"] = {
                "outcome": outcome,
                "evidence": evidence.strip()[:500],
            }
            record["updated_at"] = _utc_now()
            self._save()
            return copy.deepcopy(record)

    def get(self, intent_id: str) -> dict[str, Any] | None:
        key = self._key(intent_id)
        with self._lock:
            record = self._state["intents"].get(key)
            return copy.deepcopy(record) if record is not None else None
