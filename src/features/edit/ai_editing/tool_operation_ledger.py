"""Idempotency ledger for tool calls that may have external side effects.

The ledger is intentionally small and transport agnostic.  It records a
fingerprint and the terminal result, never the raw arguments, so a retry can
replay a known result while an interrupted operation is held for explicit
reconciliation instead of being executed twice.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, Callable, Iterable

from src.features.edit.ai_editing.capability_catalog import (
    ALL_IDEMPOTENCY_STRATEGIES,
    IDEMPOTENCY_CONTENT_KEY,
    IDEMPOTENCY_NONE,
    IDEMPOTENCY_REQUEST_KEY,
    create_default_capability_catalog,
)
from src.features.edit.ai_editing.tool_result import Diagnostic, ToolResult


class OperationStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class LedgerDecision(str, Enum):
    EXECUTE = "execute"
    REPLAY = "replay"
    IN_FLIGHT = "in_flight"
    RECONCILE = "reconcile"


# Fingerprint bounds (AUD-14800).  Each bound folds a sha256 digest of the
# material it drops into the canonical shape, so arguments that merely share
# a prefix can no longer collide on the same fingerprint.
_FINGERPRINT_STR_LIMIT = 2000
_FINGERPRINT_CONTAINER_LIMIT = 64
_FINGERPRINT_DEPTH_LIMIT = 6
# Marker key used whenever a bound dropped material.  Namespaced so it cannot
# be confused with ordinary argument keys in practice; if the input dict itself
# carries this key, an escaping suffix is appended instead of overwriting the
# user's value (which would itself reintroduce a collision).
_FINGERPRINT_TRUNCATION_KEY = "$topos.fingerprint.truncated"


def _sha256_text(text: str) -> str:
    """Full-content digest for material dropped by a fingerprint bound.

    ``surrogatepass`` keeps hashing total even for strings that cannot be
    UTF-8 encoded (the canonical payload would reject them anyway).
    """
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _sha256_items(items: Iterable[Any]) -> str:
    """Digest a separator-framed repr stream of dropped container items."""
    digest = hashlib.sha256()
    for item in items:
        digest.update(repr(item).encode("utf-8", "surrogatepass"))
        digest.update(b"\x1e")  # record separator: adjacent items cannot merge
    return digest.hexdigest()


def _bounded_text(text: str, reason: str) -> Any:
    """Return ``text`` unchanged, or a marker carrying a digest of the whole."""
    if len(text) <= _FINGERPRINT_STR_LIMIT:
        return text
    return {
        _FINGERPRINT_TRUNCATION_KEY: {
            "reason": reason,
            "prefix": text[:_FINGERPRINT_STR_LIMIT],
            "full_length": len(text),
            "dropped_sha256": _sha256_text(text),
        },
    }


def _canonical(value: Any, *, depth: int = 0) -> Any:
    """Build a stable, bounded JSON shape for a fingerprint.

    AUD-14800: previously the bounds silently truncated (``[:64]`` containers,
    ``[:2000]`` strings, a constant at the depth limit), so different arguments
    sharing a prefix collided on one fingerprint and could trigger a wrong
    REPLAY in ``ToolOperationLedger.begin``.  Every bound now keeps the same
    kept-prefix behaviour but folds in ``dropped_count``/``full_length`` and a
    sha256 over the dropped material, making accidental collisions impossible
    (only a deliberate forgery of the exact marker shape, or a sha256 break,
    could still alias two distinct argument trees).
    """

    if depth >= _FINGERPRINT_DEPTH_LIMIT:
        return {
            _FINGERPRINT_TRUNCATION_KEY: {
                "reason": "depth-limit",
                "dropped_sha256": _sha256_text(str(value)),
            },
        }
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        canonical = {
            str(key): _canonical(item, depth=depth + 1)
            for key, item in items[:_FINGERPRINT_CONTAINER_LIMIT]
        }
        dropped = items[_FINGERPRINT_CONTAINER_LIMIT:]
        if dropped:
            marker_key = _FINGERPRINT_TRUNCATION_KEY
            while marker_key in canonical:  # never overwrite user material
                marker_key += "*"
            canonical[marker_key] = {
                "reason": "dict-overflow",
                "dropped_count": len(dropped),
                "dropped_sha256": _sha256_items(dropped),
            }
        return canonical
    if isinstance(value, (list, tuple)):
        canonical = [
            _canonical(item, depth=depth + 1)
            for item in value[:_FINGERPRINT_CONTAINER_LIMIT]
        ]
        if len(value) > _FINGERPRINT_CONTAINER_LIMIT:
            canonical.append({
                _FINGERPRINT_TRUNCATION_KEY: {
                    "reason": "sequence-overflow",
                    "dropped_count": len(value) - _FINGERPRINT_CONTAINER_LIMIT,
                    "dropped_sha256": _sha256_items(
                        value[_FINGERPRINT_CONTAINER_LIMIT:]
                    ),
                },
            })
        return canonical
    if isinstance(value, str):
        return _bounded_text(value, "string-overflow")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(str(value), "value-overflow")


def fingerprint_args(args: Any) -> str:
    payload = json.dumps(_canonical(args or {}), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolOperationKey:
    """Stable lookup key; the raw arguments are deliberately excluded."""

    tool_name: str
    fingerprint: str
    scope: str
    strategy: str

    @property
    def value(self) -> str:
        return f"{self.tool_name}:{self.strategy}:{self.scope}:{self.fingerprint}"


@dataclass(frozen=True)
class ToolOperationRecord:
    operation_id: str
    key: ToolOperationKey
    status: OperationStatus
    created_at: float
    updated_at: float
    attempt: int = 1
    result: ToolResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "tool_name": self.key.tool_name,
            "fingerprint": self.key.fingerprint,
            "scope": self.key.scope,
            "strategy": self.key.strategy,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempt": self.attempt,
            "result": self.result.to_dict() if self.result is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolOperationRecord":
        if not isinstance(data, dict):
            raise ValueError("tool operation record must be an object")
        strategy = str(data.get("strategy", IDEMPOTENCY_NONE))
        if strategy not in ALL_IDEMPOTENCY_STRATEGIES:
            raise ValueError("unsupported operation idempotency strategy")
        result_data = data.get("result")
        result = None
        if isinstance(result_data, dict):
            diagnostics = tuple(
                Diagnostic.from_dict(item)
                for item in result_data.get("diagnostics", ())
                if isinstance(item, dict)
            )
            result = ToolResult(
                tool_name=str(result_data.get("tool_name", data.get("tool_name", "unknown"))),
                outcome=str(result_data.get("outcome", "failure")),
                data=dict(result_data.get("data", {})) if isinstance(result_data.get("data"), dict) else {},
                diagnostics=diagnostics,
                artifact_refs=tuple(str(item) for item in result_data.get("artifact_refs", ()) or ()),
                error_code=str(result_data.get("error_code", "")),
                error_message=str(result_data.get("error_message", "")),
                duration_ms=float(result_data.get("duration_ms", 0.0) or 0.0),
            )
        key = ToolOperationKey(
            tool_name=str(data.get("tool_name", "")),
            fingerprint=str(data.get("fingerprint", "")),
            scope=str(data.get("scope", "")),
            strategy=strategy,
        )
        if not key.tool_name or not key.fingerprint:
            raise ValueError("tool operation key is incomplete")
        return cls(
            operation_id=str(data.get("operation_id", key.value)),
            key=key,
            status=OperationStatus(str(data.get("status", OperationStatus.UNKNOWN.value))),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            attempt=max(1, int(data.get("attempt", 1) or 1)),
            result=result,
        )


@dataclass(frozen=True)
class LedgerAdmission:
    decision: LedgerDecision
    record: ToolOperationRecord


class ToolOperationLedger:
    """Thread-safe, bounded ledger for side-effecting tool operations."""

    SCHEMA_VERSION = "topos-tool-operation-ledger/1.0"

    def __init__(
        self,
        *,
        max_items: int = 256,
        strategy_for: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_items = max(1, int(max_items))
        self._clock = clock
        catalog = create_default_capability_catalog()
        self._strategy_for = strategy_for or (
            lambda tool_name: (
                getattr(catalog.get(tool_name), "idempotency_strategy", IDEMPOTENCY_NONE)
            )
        )
        self._records: dict[str, ToolOperationRecord] = {}
        self._lock = RLock()

    def _key(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        args: Any,
        strategy: str | None,
    ) -> ToolOperationKey:
        resolved = str(strategy or self._strategy_for(tool_name) or IDEMPOTENCY_NONE)
        if resolved not in ALL_IDEMPOTENCY_STRATEGIES:
            resolved = IDEMPOTENCY_NONE
        args_hash = fingerprint_args(args)
        if resolved == IDEMPOTENCY_CONTENT_KEY:
            scope = "content"
        elif resolved == IDEMPOTENCY_REQUEST_KEY:
            scope = f"request:{str(run_id)[:128]}:{str(call_id)[:128]}"
        else:
            # Even tools without a declared cross-run strategy must reject a
            # repeated model call id within one run.
            scope = f"call:{str(run_id)[:128]}:{str(call_id)[:128]}"
        return ToolOperationKey(str(tool_name), args_hash, scope, resolved)

    def begin(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        args: Any = None,
        *,
        strategy: str | None = None,
    ) -> LedgerAdmission:
        key = self._key(run_id, call_id, tool_name, args, strategy)
        now = self._clock()
        with self._lock:
            old = self._records.get(key.value)
            if old is not None:
                if old.status == OperationStatus.SUCCEEDED and old.result is not None:
                    return LedgerAdmission(LedgerDecision.REPLAY, old)
                if old.status == OperationStatus.RUNNING:
                    return LedgerAdmission(LedgerDecision.IN_FLIGHT, old)
                if old.status == OperationStatus.UNKNOWN:
                    return LedgerAdmission(LedgerDecision.RECONCILE, old)
                # Failed/denied calls may be retried. Keep their attempt count
                # so diagnostics can expose repeated attempts without replaying
                # a transient error forever.
                attempt = old.attempt + 1
                created_at = old.created_at
            else:
                attempt = 1
                created_at = now
            record = ToolOperationRecord(
                operation_id=hashlib.sha256(key.value.encode("utf-8")).hexdigest()[:24],
                key=key,
                status=OperationStatus.RUNNING,
                created_at=created_at,
                updated_at=now,
                attempt=attempt,
            )
            self._records[key.value] = record
            self._trim_locked()
            return LedgerAdmission(LedgerDecision.EXECUTE, record)

    def finish(self, record: ToolOperationRecord, result: ToolResult) -> ToolOperationRecord:
        status = OperationStatus.SUCCEEDED if result.succeeded else OperationStatus.FAILED
        with self._lock:
            current = self._records.get(record.key.value)
            if current is None or current.operation_id != record.operation_id:
                return record
            updated = ToolOperationRecord(
                operation_id=current.operation_id,
                key=current.key,
                status=status,
                created_at=current.created_at,
                updated_at=self._clock(),
                attempt=current.attempt,
                result=result,
            )
            self._records[current.key.value] = updated
            return updated

    def mark_unknown(self, record: ToolOperationRecord) -> ToolOperationRecord:
        with self._lock:
            current = self._records.get(record.key.value)
            if current is None or current.operation_id != record.operation_id:
                return record
            updated = ToolOperationRecord(
                operation_id=current.operation_id,
                key=current.key,
                status=OperationStatus.UNKNOWN,
                created_at=current.created_at,
                updated_at=self._clock(),
                attempt=current.attempt,
            )
            self._records[current.key.value] = updated
            return updated

    def get(self, operation_id: str) -> ToolOperationRecord | None:
        with self._lock:
            return next(
                (record for record in self._records.values() if record.operation_id == operation_id),
                None,
            )

    def records(self) -> tuple[ToolOperationRecord, ...]:
        with self._lock:
            return tuple(sorted(self._records.values(), key=lambda item: item.updated_at))

    def export_all(self) -> dict[str, Any] | None:
        rows = self.records()
        if not rows:
            return None
        return {
            "schema_version": self.SCHEMA_VERSION,
            "operations": [record.to_dict() for record in rows[-self._max_items:]],
        }

    def import_all(self, data: Any) -> int:
        if not isinstance(data, dict) or data.get("schema_version") != self.SCHEMA_VERSION:
            return 0
        rows = data.get("operations", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        with self._lock:
            for raw in rows[-self._max_items:]:
                try:
                    record = ToolOperationRecord.from_dict(raw)
                except (TypeError, ValueError, KeyError):
                    continue
                # A process restart invalidates RUNNING ownership. Unknown is
                # deliberately not replayable without an explicit reconciliation.
                if record.status == OperationStatus.RUNNING:
                    record = ToolOperationRecord(
                        operation_id=record.operation_id,
                        key=record.key,
                        status=OperationStatus.UNKNOWN,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        attempt=record.attempt,
                    )
                self._records[record.key.value] = record
                restored += 1
            self._trim_locked()
        return restored

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def _trim_locked(self) -> None:
        if len(self._records) <= self._max_items:
            return
        keep = sorted(
            self._records.values(), key=lambda item: item.updated_at, reverse=True
        )[: self._max_items]
        self._records = {record.key.value: record for record in keep}


__all__ = [
    "LedgerAdmission",
    "LedgerDecision",
    "OperationStatus",
    "ToolOperationKey",
    "ToolOperationLedger",
    "ToolOperationRecord",
    "fingerprint_args",
]
