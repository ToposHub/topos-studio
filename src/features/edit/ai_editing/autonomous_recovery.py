"""Bounded persistence for autonomous editing recovery.

Only the minimum information needed to decide whether a plan may be
re-planned is stored here.  Approval grants, prompts, file paths, compiled
commands, and provider state are intentionally absent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CHECKPOINTS = 32


class AutonomousCheckpointStatus:
    PENDING = "pending"
    NEEDS_REPLAN = "needs_replan"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    ALL = frozenset({PENDING, NEEDS_REPLAN, APPLIED, CANCELLED, EXPIRED})


@dataclass(frozen=True)
class AutonomousCheckpoint:
    """A sanitized reference to one autonomous plan attempt."""

    run_id: str
    plan_id: str
    project_id: str
    timeline_id: str
    base_revision: int
    asset_ids: tuple[str, ...] = ()
    goal_id: str = ""
    status: str = AutonomousCheckpointStatus.PENDING
    reason: str = ""
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.goal_id:
            object.__setattr__(self, "goal_id", self.run_id)
        for name in ("run_id", "plan_id", "project_id", "timeline_id", "goal_id"):
            value = str(getattr(self, name) or "")
            if not value or len(value) > 256 or "\x00" in value:
                raise ValueError(f"invalid checkpoint {name}")
        if int(self.base_revision) < 0:
            raise ValueError("checkpoint base_revision must be non-negative")
        if self.status not in AutonomousCheckpointStatus.ALL:
            raise ValueError(f"unknown checkpoint status: {self.status!r}")
        if len(self.asset_ids) > 128:
            raise ValueError("too many checkpoint assets")
        if any(not str(asset_id) or len(str(asset_id)) > 256 for asset_id in self.asset_ids):
            raise ValueError("invalid checkpoint asset id")

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_storage({
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "asset_ids": list(self.asset_ids),
            "goal_id": self.goal_id,
            "status": self.status,
            "reason": self.reason[:240],
            "updated_at": self.updated_at,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomousCheckpoint":
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        if int(data.get("schema_version", CHECKPOINT_SCHEMA_VERSION) or 0) != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("checkpoint schema unsupported")
        assets = data.get("asset_ids", ())
        if not isinstance(assets, (list, tuple)):
            raise ValueError("checkpoint asset_ids must be a list")
        return cls(
            run_id=str(data.get("run_id", "")),
            plan_id=str(data.get("plan_id", "")),
            project_id=str(data.get("project_id", "")),
            timeline_id=str(data.get("timeline_id", "")),
            base_revision=int(data.get("base_revision", -1)),
            asset_ids=tuple(str(item) for item in assets),
            goal_id=str(data.get("goal_id", data.get("run_id", ""))),
            status=str(data.get("status", AutonomousCheckpointStatus.PENDING)),
            reason=str(data.get("reason", ""))[:240],
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )


class AutonomousCheckpointStore:
    """Small in-memory store with bounded, sanitized project persistence."""

    def __init__(self, *, ttl_seconds: float = CHECKPOINT_TTL_SECONDS) -> None:
        self._items: dict[str, AutonomousCheckpoint] = {}
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._lock = RLock()

    def upsert(self, checkpoint: AutonomousCheckpoint) -> None:
        with self._lock:
            self._items[checkpoint.run_id] = checkpoint
            self._trim_locked()

    def get(self, run_id: str) -> AutonomousCheckpoint | None:
        with self._lock:
            return self._items.get(str(run_id))

    def list_pending(self) -> tuple[AutonomousCheckpoint, ...]:
        """Return non-expired checkpoints eligible for a recovery attempt."""
        now = time.time()
        with self._lock:
            result: list[AutonomousCheckpoint] = []
            for run_id, checkpoint in tuple(self._items.items()):
                if checkpoint.status != AutonomousCheckpointStatus.PENDING:
                    continue
                if now - checkpoint.updated_at > self._ttl_seconds:
                    self._items[run_id] = replace(
                        checkpoint,
                        status=AutonomousCheckpointStatus.EXPIRED,
                        reason="checkpoint expired; fresh planning required",
                        updated_at=now,
                    )
                    continue
                result.append(checkpoint)
            return tuple(result)

    def mark(self, run_id: str, status: str, reason: str = "") -> AutonomousCheckpoint | None:
        if status not in AutonomousCheckpointStatus.ALL:
            raise ValueError(f"unknown checkpoint status: {status!r}")
        with self._lock:
            old = self._items.get(str(run_id))
            if old is None:
                return None
            updated = replace(
                old, status=status, reason=str(reason)[:240], updated_at=time.time(),
            )
            self._items[str(run_id)] = updated
            return updated

    def export_all(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._items:
                return None
            items = sorted(
                self._items.values(), key=lambda item: item.updated_at, reverse=True,
            )[:MAX_CHECKPOINTS]
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoints": [item.to_dict() for item in items],
        }

    def import_all(self, data: Any) -> int:
        if not isinstance(data, dict):
            return 0
        if int(data.get("schema_version", 0) or 0) != CHECKPOINT_SCHEMA_VERSION:
            return 0
        rows = data.get("checkpoints", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        with self._lock:
            for raw in rows[:MAX_CHECKPOINTS]:
                try:
                    checkpoint = AutonomousCheckpoint.from_dict(raw)
                except (TypeError, ValueError, KeyError):
                    continue
                self._items[checkpoint.run_id] = checkpoint
                restored += 1
            self._trim_locked()
        return restored

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _trim_locked(self) -> None:
        if len(self._items) <= MAX_CHECKPOINTS:
            return
        keep = sorted(
            self._items.values(), key=lambda item: item.updated_at, reverse=True,
        )[:MAX_CHECKPOINTS]
        self._items = {item.run_id: item for item in keep}
