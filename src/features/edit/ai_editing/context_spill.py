"""Bounded references for context omitted by compression or budget pruning.

Spill records intentionally contain only a hash, counts and a short sanitized
preview.  They make omitted context auditable without turning project files
into a second full prompt archive.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterable, Mapping

from src.features.ai_harness.invocation_models import sanitize_for_storage


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value[:128]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


@dataclass(frozen=True)
class ContextSpillRef:
    spill_id: str
    scope: str
    kind: str
    content_hash: str
    item_count: int
    token_count: int
    preview: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spill_id": self.spill_id,
            "scope": self.scope,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "item_count": self.item_count,
            "token_count": self.token_count,
            "preview": sanitize_for_storage(self.preview[:400]),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextSpillRef":
        if not isinstance(data, dict):
            raise ValueError("context spill ref must be an object")
        spill_id = str(data.get("spill_id", ""))
        content_hash = str(data.get("content_hash", ""))
        if not spill_id or not content_hash:
            raise ValueError("context spill ref is incomplete")
        return cls(
            spill_id=spill_id[:64],
            scope=str(data.get("scope", ""))[:128],
            kind=str(data.get("kind", "context"))[:64],
            content_hash=content_hash[:128],
            item_count=max(0, int(data.get("item_count", 0) or 0)),
            token_count=max(0, int(data.get("token_count", 0) or 0)),
            preview=str(data.get("preview", ""))[:400],
            created_at=float(data.get("created_at", time.time()) or time.time()),
        )


class ContextSpillStore:
    """Thread-safe bounded metadata store for omitted context."""

    SCHEMA_VERSION = "topos-context-spill/1.0"

    def __init__(self, *, max_items: int = 128) -> None:
        self._max_items = max(1, int(max_items))
        self._items: dict[str, ContextSpillRef] = {}
        self._lock = RLock()

    def record(
        self,
        *,
        scope: str,
        kind: str,
        items: Iterable[Any],
        token_count: int,
        preview: str = "",
    ) -> ContextSpillRef:
        values = tuple(items)
        encoded = json.dumps(_canonical(values), ensure_ascii=False, sort_keys=True, default=str)
        content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        key = f"{str(scope)[:128]}:{str(kind)[:64]}:{content_hash}"
        with self._lock:
            existing = self._items.get(key)
            if existing is not None:
                return existing
            ref = ContextSpillRef(
                spill_id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
                scope=str(scope)[:128],
                kind=str(kind)[:64],
                content_hash=content_hash,
                item_count=len(values),
                token_count=max(0, int(token_count)),
                preview=str(sanitize_for_storage(preview or ""))[:400],
            )
            self._items[key] = ref
            self._trim_locked()
            return ref

    def get(self, spill_id: str) -> ContextSpillRef | None:
        with self._lock:
            return next((item for item in self._items.values() if item.spill_id == spill_id), None)

    def records(self) -> tuple[ContextSpillRef, ...]:
        with self._lock:
            return tuple(sorted(self._items.values(), key=lambda item: item.created_at))

    def export_all(self) -> dict[str, Any] | None:
        rows = self.records()
        if not rows:
            return None
        return {
            "schema_version": self.SCHEMA_VERSION,
            "spills": [row.to_dict() for row in rows[-self._max_items:]],
        }

    def import_all(self, data: Any) -> int:
        if not isinstance(data, dict) or data.get("schema_version") != self.SCHEMA_VERSION:
            return 0
        rows = data.get("spills", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        with self._lock:
            for raw in rows[-self._max_items:]:
                try:
                    ref = ContextSpillRef.from_dict(raw)
                except (TypeError, ValueError, KeyError):
                    continue
                key = f"{ref.scope}:{ref.kind}:{ref.content_hash}"
                self._items[key] = ref
                restored += 1
            self._trim_locked()
        return restored

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _trim_locked(self) -> None:
        if len(self._items) <= self._max_items:
            return
        keep = sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)[: self._max_items]
        self._items = {
            f"{item.scope}:{item.kind}:{item.content_hash}": item for item in keep
        }


__all__ = ["ContextSpillRef", "ContextSpillStore"]
