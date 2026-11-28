"""Idempotent order lifecycle. It records intent; it does not place orders."""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INTENT = "INTENT"
RISK_CHECKED = "RISK_CHECKED"
CONSENSUS_CHECKED = "CONSENSUS_CHECKED"
SUBMITTED = "SUBMITTED"
PARTIAL = "PARTIAL"
FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"

TERMINAL = {FILLED, REJECTED, CANCELLED, DUPLICATE_BLOCKED}
ACTIVE = {INTENT, RISK_CHECKED, CONSENSUS_CHECKED, SUBMITTED, PARTIAL}
_TRANSITIONS = {
    INTENT: {CONSENSUS_CHECKED, REJECTED, CANCELLED},
    CONSENSUS_CHECKED: {RISK_CHECKED, REJECTED, CANCELLED},
    RISK_CHECKED: {SUBMITTED, REJECTED, CANCELLED},
    SUBMITTED: {PARTIAL, FILLED, REJECTED, CANCELLED},
    PARTIAL: {FILLED, CANCELLED, REJECTED},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OrderRecord:
    order_id: str
    idempotency_key: str
    token_id: str
    market_id: str
    side: str
    state: str
    reason: str
    created_at: str
    updated_at: str
    notional: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderStateError(ValueError):
    pass


class OrderStateMachine:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._orders: dict[str, OrderRecord] = {}
        self._by_key: dict[str, str] = {}
        if self.path is not None:
            self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise OrderStateError("order state file must be a list")
        for item in payload:
            record = OrderRecord(**item)
            self._orders[record.order_id] = record
            self._by_key[record.idempotency_key] = record.order_id

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [record.as_dict() for record in self._orders.values()]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, order_id: str) -> OrderRecord | None:
        with self._lock:
            return self._orders.get(order_id)

    def active_for_token(self, token_id: str) -> OrderRecord | None:
        with self._lock:
            for record in self._orders.values():
                if record.token_id == token_id and record.state in ACTIVE:
                    return record
            return None

    def begin(
        self,
        *,
        token_id: str,
        market_id: str,
        side: str,
        idempotency_key: str,
    ) -> tuple[OrderRecord, bool]:
        """Return ``(record, created)``. A false created flag means do not send another order."""
        token = str(token_id or "").strip()
        key = str(idempotency_key or "").strip()
        if not token or not key:
            raise OrderStateError("token_id and idempotency_key are required")
        with self._lock:
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                return self._orders[existing_id], False
            active = self.active_for_token(token)
            now = _now()
            if active is not None:
                record = OrderRecord(
                    order_id=uuid.uuid4().hex,
                    idempotency_key=key,
                    token_id=token,
                    market_id=str(market_id or ""),
                    side=str(side or "BUY"),
                    state=DUPLICATE_BLOCKED,
                    reason="active_order_exists",
                    created_at=now,
                    updated_at=now,
                    details={"blocking_order_id": active.order_id},
                )
                self._orders[record.order_id] = record
                self._by_key[key] = record.order_id
                self._save()
                return record, False
            record = OrderRecord(
                order_id=uuid.uuid4().hex,
                idempotency_key=key,
                token_id=token,
                market_id=str(market_id or ""),
                side=str(side or "BUY"),
                state=INTENT,
                reason="research_intent",
                created_at=now,
                updated_at=now,
            )
            self._orders[record.order_id] = record
            self._by_key[key] = record.order_id
            self._save()
            return record, True

    def transition(self, order_id: str, new_state: str, *, reason: str, **details: Any) -> OrderRecord:
        with self._lock:
            record = self._orders.get(order_id)
            if record is None:
                raise OrderStateError(f"unknown order {order_id}")
            allowed = _TRANSITIONS.get(record.state, set())
            if new_state not in allowed:
                raise OrderStateError(f"cannot move {record.state} to {new_state}")
            record.state = new_state
            record.reason = str(reason)[:240]
            record.updated_at = _now()
            if "notional" in details:
                record.notional = details.pop("notional")
            record.details.update(details)
            self._save()
            return record
