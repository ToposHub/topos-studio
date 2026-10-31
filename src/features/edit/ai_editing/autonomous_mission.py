"""Bounded mission ledger for autonomous timeline editing.

The autonomous write boundary is intentionally still owned by
``AutonomousAgent``.  This module records the finite stages around that
boundary so a long-running editing request is observable and restart-safe:

    plan -> preview -> commit -> verify

It is a ledger, not a second execution loop.  A failed quality gate stops the
mission and requires a fresh plan; stages cannot be skipped or repeated after
they have completed.  Persisted rows contain only IDs, stage names and short
redacted diagnostics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.edit.ai_editing.privacy_guard import redact_text


MISSION_SCHEMA_VERSION = 1
MAX_MISSIONS = 32


class MissionStage(str, Enum):
    PLAN = "plan"
    PREVIEW = "preview"
    COMMIT = "commit"
    VERIFY = "verify"


class MissionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_REPLAN = "needs_replan"
    CANCELLED = "cancelled"


_STAGE_ORDER = (MissionStage.PLAN, MissionStage.PREVIEW, MissionStage.COMMIT, MissionStage.VERIFY)


@dataclass(frozen=True)
class MissionStageResult:
    stage: MissionStage
    passed: bool
    reason: str = ""
    diagnostics: tuple[str, ...] = ()
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_storage({
            "stage": self.stage.value,
            "passed": self.passed,
            "reason": _safe_reason(self.reason),
            "diagnostics": [str(item)[:160] for item in self.diagnostics[:8]],
            "updated_at": self.updated_at,
        })


@dataclass(frozen=True)
class AutonomousMission:
    run_id: str
    plan_id: str
    status: MissionStatus = MissionStatus.RUNNING
    current_stage: MissionStage = MissionStage.PLAN
    completed_stages: tuple[MissionStage, ...] = ()
    stage_results: tuple[MissionStageResult, ...] = ()
    reason: str = ""
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name in ("run_id", "plan_id"):
            value = str(getattr(self, name) or "")
            if not value or len(value) > 256 or "\x00" in value:
                raise ValueError(f"invalid mission {name}")
        completed = tuple(self.completed_stages)
        if len(completed) > len(_STAGE_ORDER):
            raise ValueError("too many completed mission stages")
        if completed != _STAGE_ORDER[:len(completed)]:
            raise ValueError("mission stages must complete in order")

    @property
    def terminal(self) -> bool:
        return self.status in {
            MissionStatus.SUCCEEDED,
            MissionStatus.NEEDS_REPLAN,
            MissionStatus.CANCELLED,
        }

    @property
    def quality_passed(self) -> bool:
        return bool(self.stage_results) and all(item.passed for item in self.stage_results)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_storage({
            "schema_version": MISSION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "status": self.status.value,
            "current_stage": self.current_stage.value,
            "completed_stages": [stage.value for stage in self.completed_stages],
            "stage_results": [item.to_dict() for item in self.stage_results[-8:]],
            "reason": _safe_reason(self.reason),
            "updated_at": self.updated_at,
        })


class AutonomousMissionStore:
    """Thread-safe bounded store for mission state and its quality gates."""

    def __init__(self, *, max_items: int = MAX_MISSIONS) -> None:
        self._max_items = max(1, min(int(max_items), MAX_MISSIONS))
        self._items: dict[str, AutonomousMission] = {}
        self._lock = RLock()

    def start(self, run_id: str, plan_id: str) -> AutonomousMission:
        mission = AutonomousMission(run_id=str(run_id), plan_id=str(plan_id))
        with self._lock:
            old = self._items.get(mission.run_id)
            if old is not None and old.plan_id == mission.plan_id:
                return old
            self._items[mission.run_id] = mission
            self._trim_locked()
        return mission

    def get(self, run_id: str) -> AutonomousMission | None:
        with self._lock:
            return self._items.get(str(run_id))

    def record(
        self,
        run_id: str,
        stage: MissionStage,
        *,
        passed: bool,
        reason: str = "",
        diagnostics: tuple[str, ...] = (),
    ) -> AutonomousMission | None:
        """Advance one stage through a quality gate, failing closed on misuse."""
        with self._lock:
            old = self._items.get(str(run_id))
            if old is None or old.terminal:
                return old
            expected_index = len(old.completed_stages)
            if expected_index >= len(_STAGE_ORDER) or stage != _STAGE_ORDER[expected_index]:
                return self._fail_locked(old, "mission stage order violation")
            result = MissionStageResult(
                stage=stage,
                passed=bool(passed),
                reason=str(reason)[:240],
                diagnostics=tuple(str(item)[:160] for item in diagnostics[:8]),
            )
            results = old.stage_results + (result,)
            if not passed:
                updated = AutonomousMission(
                    run_id=old.run_id,
                    plan_id=old.plan_id,
                    status=MissionStatus.NEEDS_REPLAN,
                    current_stage=stage,
                    completed_stages=old.completed_stages,
                    stage_results=results,
                    reason=str(reason)[:240] or f"{stage.value} quality gate failed",
                )
            else:
                completed = old.completed_stages + (stage,)
                terminal_status = (
                    MissionStatus.SUCCEEDED
                    if len(completed) == len(_STAGE_ORDER)
                    else MissionStatus.RUNNING
                )
                next_stage = (
                    _STAGE_ORDER[len(completed)]
                    if len(completed) < len(_STAGE_ORDER) else stage
                )
                updated = AutonomousMission(
                    run_id=old.run_id,
                    plan_id=old.plan_id,
                    status=terminal_status,
                    current_stage=next_stage,
                    completed_stages=completed,
                    stage_results=results,
                    reason=str(reason)[:240],
                )
            self._items[old.run_id] = updated
            return updated

    def mark_cancelled(self, run_id: str, reason: str = "mission cancelled") -> AutonomousMission | None:
        with self._lock:
            old = self._items.get(str(run_id))
            if old is None or old.terminal:
                return old
            updated = AutonomousMission(
                run_id=old.run_id,
                plan_id=old.plan_id,
                status=MissionStatus.CANCELLED,
                current_stage=old.current_stage,
                completed_stages=old.completed_stages,
                stage_results=old.stage_results,
                reason=str(reason)[:240],
            )
            self._items[old.run_id] = updated
            return updated

    def export_all(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._items:
                return None
            items = sorted(self._items.values(), key=lambda item: item.updated_at, reverse=True)
        return {
            "schema_version": MISSION_SCHEMA_VERSION,
            "missions": [item.to_dict() for item in items[:self._max_items]],
        }

    def import_all(self, data: Any) -> int:
        if not isinstance(data, dict) or int(data.get("schema_version", 0) or 0) != MISSION_SCHEMA_VERSION:
            return 0
        rows = data.get("missions", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        with self._lock:
            for raw in rows[:self._max_items]:
                try:
                    mission = _mission_from_dict(raw)
                except (TypeError, ValueError, KeyError):
                    continue
                self._items[mission.run_id] = mission
                restored += 1
            self._trim_locked()
        return restored

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _fail_locked(self, old: AutonomousMission, reason: str) -> AutonomousMission:
        updated = AutonomousMission(
            run_id=old.run_id,
            plan_id=old.plan_id,
            status=MissionStatus.NEEDS_REPLAN,
            current_stage=old.current_stage,
            completed_stages=old.completed_stages,
            stage_results=old.stage_results,
            reason=reason,
        )
        self._items[old.run_id] = updated
        return updated

    def _trim_locked(self) -> None:
        if len(self._items) <= self._max_items:
            return
        keep = sorted(self._items.values(), key=lambda item: item.updated_at, reverse=True)[:self._max_items]
        self._items = {item.run_id: item for item in keep}


def _mission_from_dict(data: Any) -> AutonomousMission:
    if not isinstance(data, dict):
        raise ValueError("mission must be an object")
    if int(data.get("schema_version", MISSION_SCHEMA_VERSION) or 0) != MISSION_SCHEMA_VERSION:
        raise ValueError("mission schema unsupported")
    completed = tuple(MissionStage(str(item)) for item in data.get("completed_stages", ()))
    results: list[MissionStageResult] = []
    for raw in data.get("stage_results", ()):
        if not isinstance(raw, dict):
            continue
        results.append(MissionStageResult(
            stage=MissionStage(str(raw["stage"])),
            passed=bool(raw.get("passed", False)),
            reason=str(raw.get("reason", ""))[:240],
            diagnostics=tuple(str(item)[:160] for item in raw.get("diagnostics", ())[:8]),
            updated_at=float(raw.get("updated_at", time.time())),
        ))
    return AutonomousMission(
        run_id=str(data.get("run_id", "")),
        plan_id=str(data.get("plan_id", "")),
        status=MissionStatus(str(data.get("status", MissionStatus.RUNNING.value))),
        current_stage=MissionStage(str(data.get("current_stage", MissionStage.PLAN.value))),
        completed_stages=completed,
        stage_results=tuple(results),
        reason=str(data.get("reason", ""))[:240],
        updated_at=float(data.get("updated_at", time.time())),
    )


def _safe_reason(value: str) -> str:
    """Keep diagnostics useful while excluding prompt-like detail fields."""
    redacted = redact_text(str(value))[:240]
    lowered = redacted.lower()
    if any(marker in lowered for marker in ("prompt", "api_key", "authorization", "secret")):
        return "[detail redacted]"
    return redacted


__all__ = [
    "AutonomousMission",
    "AutonomousMissionStore",
    "MISSION_SCHEMA_VERSION",
    "MissionStage",
    "MissionStageResult",
    "MissionStatus",
]
