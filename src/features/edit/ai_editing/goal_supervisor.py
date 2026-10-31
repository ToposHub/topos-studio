"""Generic long-running goal supervisor for typed Agent cycles.

This layer is deliberately domain agnostic.  A runner can be an editing
cycle, an analysis cycle, a generation cycle, or an adapter around
``AgentLoop``.  The supervisor owns only lifecycle, budgets, cancellation and
restart-safe checkpoints; it never invents tools or bypasses host policy.

"Unlimited" autonomy is intentionally not represented here.  Long-running
goals must have a finite budget and an explicit stop boundary so a kill switch,
user cancellation, or a project close can always regain control.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Callable, Protocol

from src.features.ai_harness.invocation_models import sanitize_for_storage
from src.features.edit.ai_editing.privacy_guard import redact_text


class GoalStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    WAITING_FOR_USER = "waiting_for_user"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


class GoalControl(str, Enum):
    CONTINUE = "continue"
    PAUSE = "pause"
    CANCEL = "cancel"


@dataclass(frozen=True)
class GoalLimits:
    """Finite global limits for one long-running goal."""

    max_cycles: int = 8
    max_tool_calls: int = 128
    max_cost_units: float | None = None
    max_wall_time_seconds: float | None = 3600.0

    def __post_init__(self) -> None:
        if self.max_cycles <= 0 or self.max_tool_calls <= 0:
            raise ValueError("goal limits must be positive")
        if self.max_cost_units is not None and self.max_cost_units <= 0:
            raise ValueError("max_cost_units must be positive")
        if self.max_wall_time_seconds is not None and self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive")


@dataclass(frozen=True)
class GoalSpec:
    """Privacy-safe identity and scope for one general goal."""

    goal_id: str
    project_id: str
    timeline_id: str = ""
    objective_ref: str = ""
    constraint_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("goal_id", "project_id"):
            value = str(getattr(self, name) or "")
            if not value or len(value) > 256 or "\x00" in value:
                raise ValueError(f"invalid goal {name}")
        if len(str(self.timeline_id)) > 256:
            raise ValueError("goal timeline_id is too long")
        if len(str(self.objective_ref)) > 256:
            raise ValueError("goal objective_ref is too long")
        if len(self.constraint_refs) > 32:
            raise ValueError("too many goal constraints")

    @property
    def scope_hash(self) -> str:
        payload = {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "objective_ref": self.objective_ref,
            "constraint_refs": list(self.constraint_refs),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Persist only typed goal metadata, never the user's raw request."""
        return sanitize_for_storage({
            "schema_version": "topos-goal-spec/1.0",
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "objective_ref": self.objective_ref,
            "constraint_refs": list(self.constraint_refs),
            "scope_hash": self.scope_hash,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalSpec":
        if not isinstance(data, dict):
            raise ValueError("goal spec must be an object")
        if data.get("schema_version", "topos-goal-spec/1.0") != "topos-goal-spec/1.0":
            raise ValueError("goal spec schema unsupported")
        constraints = data.get("constraint_refs", ())
        if not isinstance(constraints, (list, tuple)):
            raise ValueError("goal constraint_refs must be a list")
        spec = cls(
            goal_id=str(data.get("goal_id", "")),
            project_id=str(data.get("project_id", "")),
            timeline_id=str(data.get("timeline_id", "")),
            objective_ref=str(data.get("objective_ref", "")),
            constraint_refs=tuple(str(item) for item in constraints[:32]),
        )
        if data.get("scope_hash") and str(data["scope_hash"]) != spec.scope_hash:
            raise ValueError("goal scope hash mismatch")
        return spec


@dataclass(frozen=True)
class GoalCycleResult:
    """Typed result returned by one injected observe/plan/act/verify cycle."""

    completed: bool = False
    retryable: bool = False
    needs_user_input: bool = False
    tool_calls: int = 0
    cost_units: float = 0.0
    completed_step_ids: tuple[str, ...] = ()
    reason: str = ""
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tool_calls < 0 or self.cost_units < 0:
            raise ValueError("goal cycle counters cannot be negative")


@dataclass(frozen=True)
class GoalCheckpoint:
    """Restart-safe goal state; raw prompts and provider state are excluded."""

    goal_id: str
    status: GoalStatus
    cycle: int = 0
    tool_calls: int = 0
    cost_units: float = 0.0
    completed_step_ids: tuple[str, ...] = ()
    scope_hash: str = ""
    reason: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Monotonic optimistic-concurrency token.  Kept at the end so legacy
    # positional construction remains source-compatible.
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return sanitize_for_storage({
            "schema_version": "topos-goal-checkpoint/1.0",
            "goal_id": self.goal_id,
            "status": self.status.value,
            "cycle": self.cycle,
            "tool_calls": self.tool_calls,
            "cost_units": self.cost_units,
            "completed_step_ids": list(self.completed_step_ids[:256]),
            "scope_hash": self.scope_hash,
            "reason": _safe_reason(self.reason),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalCheckpoint":
        if not isinstance(data, dict):
            raise ValueError("goal checkpoint must be an object")
        if data.get("schema_version", "topos-goal-checkpoint/1.0") != "topos-goal-checkpoint/1.0":
            raise ValueError("goal checkpoint schema unsupported")
        steps = data.get("completed_step_ids", ())
        if not isinstance(steps, (list, tuple)):
            raise ValueError("completed_step_ids must be a list")
        return cls(
            goal_id=str(data.get("goal_id", "")),
            status=GoalStatus(str(data.get("status", GoalStatus.RUNNING.value))),
            cycle=int(data.get("cycle", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            cost_units=float(data.get("cost_units", 0.0)),
            completed_step_ids=tuple(str(item) for item in steps[:256]),
            scope_hash=str(data.get("scope_hash", "")),
            reason=str(data.get("reason", ""))[:500],
            started_at=float(data.get("started_at", data.get("updated_at", time.time()))),
            updated_at=float(data.get("updated_at", time.time())),
            revision=max(0, int(data.get("revision", 0) or 0)),
        )


@dataclass(frozen=True)
class GoalRunResult:
    status: GoalStatus
    checkpoint: GoalCheckpoint
    diagnostics: tuple[str, ...] = ()


class GoalCycleRunner(Protocol):
    def run_cycle(
        self, goal: GoalSpec, checkpoint: GoalCheckpoint,
    ) -> GoalCycleResult: ...


class GoalCheckpointStore(Protocol):
    def save(self, checkpoint: GoalCheckpoint) -> bool: ...
    def load(self, goal_id: str) -> GoalCheckpoint | None: ...

    def compare_and_save(
        self, goal_id: str, expected_revision: int, checkpoint: GoalCheckpoint,
    ) -> bool: ...


class GoalRevisionConflict(RuntimeError):
    """Raised when an async goal continuation tries to commit a stale revision."""


class InMemoryGoalCheckpointStore:
    """Bounded store useful for controller state and deterministic tests."""

    def __init__(self, *, max_items: int = 64) -> None:
        self._max_items = max(1, int(max_items))
        self._items: dict[str, GoalCheckpoint] = {}
        self._lock = RLock()

    def save(self, checkpoint: GoalCheckpoint) -> bool:
        with self._lock:
            self._items[checkpoint.goal_id] = checkpoint
            if len(self._items) > self._max_items:
                keep = sorted(
                    self._items.values(), key=lambda item: item.updated_at, reverse=True,
                )[:self._max_items]
                self._items = {item.goal_id: item for item in keep}
        return True

    def compare_and_save(
        self, goal_id: str, expected_revision: int, checkpoint: GoalCheckpoint,
    ) -> bool:
        """Atomically commit exactly one successor of ``expected_revision``."""

        with self._lock:
            current = self._items.get(str(goal_id))
            actual = current.revision if current is not None else 0
            if actual != int(expected_revision):
                return False
            if checkpoint.goal_id != str(goal_id):
                return False
            if checkpoint.revision != actual + 1:
                return False
            self._items[str(goal_id)] = checkpoint
        return True

    def load(self, goal_id: str) -> GoalCheckpoint | None:
        with self._lock:
            return self._items.get(str(goal_id))

    def export_all(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._items:
                return None
            items = sorted(
                self._items.values(), key=lambda item: item.updated_at, reverse=True,
            )[:self._max_items]
        return {
            "schema_version": "topos-goal-checkpoint/1.0",
            "checkpoints": [item.to_dict() for item in items],
        }

    def import_all(self, data: Any) -> int:
        if not isinstance(data, dict) or data.get("schema_version") != "topos-goal-checkpoint/1.0":
            return 0
        rows = data.get("checkpoints", ())
        if not isinstance(rows, (list, tuple)):
            return 0
        restored = 0
        with self._lock:
            for raw in rows[:self._max_items]:
                try:
                    checkpoint = GoalCheckpoint.from_dict(raw)
                except (TypeError, ValueError, KeyError):
                    continue
                self._items[checkpoint.goal_id] = checkpoint
                restored += 1
        return restored


class GoalSupervisor:
    """Run finite cycles until success, safe pause, failure or cancellation."""

    def __init__(
        self,
        runner: GoalCycleRunner | None = None,
        *,
        limits: GoalLimits | None = None,
        checkpoints: GoalCheckpointStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._limits = limits or GoalLimits()
        self._checkpoints = checkpoints
        self._clock = clock
        self._wall_clock = wall_clock

    def run(
        self,
        goal: GoalSpec,
        *,
        control: Callable[[], GoalControl] | None = None,
        resume: bool = False,
    ) -> GoalRunResult:
        control = control or (lambda: GoalControl.CONTINUE)
        started_at = self._clock()
        previous = self._checkpoints.load(goal.goal_id) if self._checkpoints else None
        if previous is not None and not resume:
            return GoalRunResult(
                previous.status,
                previous,
                ("goal checkpoint exists; explicit resume is required",),
            )
        if previous is not None:
            if previous.scope_hash and previous.scope_hash != goal.scope_hash:
                # 与 record_cycle 对称：并发修订冲突时返回 stale 结果而不是
                # 让 GoalRevisionConflict 冒泡破坏 run() -> GoalRunResult 契约。
                try:
                    checkpoint = self._save(GoalCheckpoint(
                        goal_id=goal.goal_id,
                        status=GoalStatus.WAITING_FOR_USER,
                        scope_hash=goal.scope_hash,
                        reason="goal scope changed; fresh planning is required",
                        started_at=previous.started_at,
                    ), expected_revision=previous.revision)
                except GoalRevisionConflict:
                    return self._stale_result(goal)
                return GoalRunResult(checkpoint.status, checkpoint, (checkpoint.reason,))
            if previous.status == GoalStatus.SUCCEEDED:
                return GoalRunResult(previous.status, previous, ("goal already succeeded",))
        checkpoint = previous or GoalCheckpoint(
            goal_id=goal.goal_id,
            status=GoalStatus.RUNNING,
            scope_hash=goal.scope_hash,
            started_at=self._wall_clock(),
        )
        goal_started_at = checkpoint.started_at
        cycle = checkpoint.cycle
        tool_calls = checkpoint.tool_calls
        cost_units = checkpoint.cost_units
        completed = set(checkpoint.completed_step_ids)

        while cycle < self._limits.max_cycles:
            decision = control()
            if decision == GoalControl.CANCEL:
                return self._finish(
                    goal, GoalStatus.CANCELLED, cycle, tool_calls, cost_units,
                    completed, "cancel requested",
                    started_at=goal_started_at,
                )
            if decision == GoalControl.PAUSE:
                return self._finish(
                    goal, GoalStatus.PAUSED, cycle, tool_calls, cost_units,
                    completed, "pause requested",
                    started_at=goal_started_at,
                )
            if self._over_budget(
                started_at,
                tool_calls,
                cost_units,
                persistent_started_at=goal_started_at,
            ):
                return self._finish(
                    goal, GoalStatus.BUDGET_EXCEEDED, cycle, tool_calls, cost_units,
                    completed, "goal budget exceeded",
                    started_at=goal_started_at,
                )
            cycle += 1
            cycle_checkpoint = GoalCheckpoint(
                goal_id=goal.goal_id, status=GoalStatus.RUNNING,
                cycle=cycle, tool_calls=tool_calls, cost_units=cost_units,
                completed_step_ids=tuple(sorted(completed)), scope_hash=goal.scope_hash,
                started_at=goal_started_at,
            )
            try:
                if self._runner is None:
                    raise RuntimeError("goal cycle runner is not configured")
                result = self._runner.run_cycle(goal, cycle_checkpoint)
            except Exception as exc:  # noqa: BLE001 — fail closed at goal boundary
                return self._finish(
                    goal, GoalStatus.FAILED, cycle, tool_calls, cost_units,
                    completed, f"cycle failed: {type(exc).__name__}: {redact_text(str(exc))}",
                    started_at=goal_started_at,
                )
            tool_calls += result.tool_calls
            cost_units += result.cost_units
            completed.update(str(item) for item in result.completed_step_ids if str(item))
            if result.completed:
                return self._finish(
                    goal, GoalStatus.SUCCEEDED, cycle, tool_calls, cost_units,
                    completed, result.reason, result.diagnostics,
                    started_at=goal_started_at,
                )
            if result.needs_user_input:
                return self._finish(
                    goal, GoalStatus.WAITING_FOR_USER, cycle, tool_calls, cost_units,
                    completed, result.reason, result.diagnostics,
                    started_at=goal_started_at,
                )
            if not result.retryable:
                return self._finish(
                    goal, GoalStatus.FAILED, cycle, tool_calls, cost_units,
                    completed, result.reason or "goal cycle failed", result.diagnostics,
                    started_at=goal_started_at,
                )

        return self._finish(
            goal, GoalStatus.BUDGET_EXCEEDED, cycle, tool_calls, cost_units,
            completed, "maximum goal cycles exceeded",
            started_at=goal_started_at,
        )

    def record_cycle(
        self,
        goal: GoalSpec,
        result: GoalCycleResult,
        *,
        resume: bool = True,
        resume_interactive: bool = False,
        started_at: float | None = None,
    ) -> GoalRunResult:
        """Record a cycle performed by an external/async execution adapter.

        This is the bridge for hosts whose actual cycle is asynchronous (for
        example a Qt worker or a skill runtime).  The adapter executes the
        cycle, then hands its typed result here; the supervisor still owns the
        authoritative lifecycle and budget decision.  A paused or
        user-blocked goal rejects late results until the caller explicitly
        opts into ``resume_interactive``.
        """
        previous = self._checkpoints.load(goal.goal_id) if self._checkpoints else None
        if previous is not None and not resume:
            return GoalRunResult(
                previous.status,
                previous,
                ("goal checkpoint exists; explicit resume is required",),
            )
        if previous is not None:
            if previous.scope_hash and previous.scope_hash != goal.scope_hash:
                try:
                    checkpoint = self._save(GoalCheckpoint(
                        goal_id=goal.goal_id,
                        status=GoalStatus.WAITING_FOR_USER,
                        scope_hash=goal.scope_hash,
                        reason="goal scope changed; fresh planning is required",
                        started_at=previous.started_at,
                    ), expected_revision=previous.revision)
                except GoalRevisionConflict:
                    return self._stale_result(goal)
                return GoalRunResult(checkpoint.status, checkpoint, (checkpoint.reason,))
            if previous.status in {
                GoalStatus.SUCCEEDED,
                GoalStatus.CANCELLED,
                GoalStatus.FAILED,
                GoalStatus.BUDGET_EXCEEDED,
            }:
                return GoalRunResult(previous.status, previous, ("goal is terminal",))
            if previous.status in {GoalStatus.PAUSED, GoalStatus.WAITING_FOR_USER}:
                if not resume_interactive:
                    return GoalRunResult(
                        previous.status,
                        previous,
                        ("goal requires explicit resume before accepting a cycle",),
                    )
        old_cycle = previous.cycle if previous is not None else 0
        old_tools = previous.tool_calls if previous is not None else 0
        old_cost = previous.cost_units if previous is not None else 0.0
        goal_started_at = (
            previous.started_at
            if previous is not None
            else float(started_at if started_at is not None else self._wall_clock())
        )
        if self._wall_time_exceeded(goal_started_at):
            try:
                checkpoint = self._save(GoalCheckpoint(
                goal_id=goal.goal_id,
                status=GoalStatus.BUDGET_EXCEEDED,
                cycle=old_cycle,
                tool_calls=old_tools,
                cost_units=old_cost,
                completed_step_ids=tuple(sorted(previous.completed_step_ids if previous else ()))[:256],
                scope_hash=goal.scope_hash,
                reason="goal wall-time budget exceeded",
                started_at=goal_started_at,
                ), expected_revision=previous.revision if previous is not None else 0)
            except GoalRevisionConflict:
                return self._stale_result(goal)
            return GoalRunResult(
                checkpoint.status, checkpoint, (checkpoint.reason,),
            )
        completed = set(previous.completed_step_ids if previous is not None else ())
        completed.update(str(item) for item in result.completed_step_ids if str(item))
        cycle = old_cycle + 1
        tool_calls = old_tools + result.tool_calls
        cost_units = old_cost + result.cost_units
        if result.completed:
            status = GoalStatus.SUCCEEDED
        elif result.needs_user_input:
            status = GoalStatus.WAITING_FOR_USER
        elif not result.retryable:
            status = GoalStatus.FAILED
        elif (
            cycle >= self._limits.max_cycles
            or tool_calls >= self._limits.max_tool_calls
            or (
                self._limits.max_cost_units is not None
                and cost_units >= self._limits.max_cost_units
            )
        ):
            status = GoalStatus.BUDGET_EXCEEDED
        else:
            status = GoalStatus.RUNNING
        try:
            checkpoint = self._save(GoalCheckpoint(
            goal_id=goal.goal_id,
            status=status,
            cycle=cycle,
            tool_calls=tool_calls,
            cost_units=cost_units,
            completed_step_ids=tuple(sorted(completed))[:256],
            scope_hash=goal.scope_hash,
            reason=result.reason,
            started_at=goal_started_at,
            ), expected_revision=previous.revision if previous is not None else 0)
        except GoalRevisionConflict:
            return self._stale_result(goal)
        return GoalRunResult(
            status, checkpoint,
            tuple(result.diagnostics) or ((result.reason,) if result.reason else ()),
        )

    def _over_budget(
        self,
        started_at: float,
        tool_calls: int,
        cost_units: float,
        *,
        persistent_started_at: float | None = None,
    ) -> bool:
        if tool_calls >= self._limits.max_tool_calls:
            return True
        if self._limits.max_cost_units is not None and cost_units >= self._limits.max_cost_units:
            return True
        max_wall = self._limits.max_wall_time_seconds
        if max_wall is None:
            return False
        monotonic_elapsed = self._clock() - started_at
        wall_elapsed = (
            self._wall_clock() - persistent_started_at
            if persistent_started_at is not None else 0.0
        )
        return max(monotonic_elapsed, wall_elapsed) >= max_wall

    def _wall_time_exceeded(self, started_at: float) -> bool:
        max_wall = self._limits.max_wall_time_seconds
        return max_wall is not None and self._wall_clock() - started_at >= max_wall

    def _finish(
        self,
        goal: GoalSpec,
        status: GoalStatus,
        cycle: int,
        tool_calls: int,
        cost_units: float,
        completed: set[str],
        reason: str,
        diagnostics: tuple[str, ...] = (),
        started_at: float | None = None,
    ) -> GoalRunResult:
        # run() 的所有收尾路径都经过这里；提交冲突与 record_cycle 一致地折叠为
        # stale 结果（GoalRunResult），绝不向宿主冒泡 GoalRevisionConflict。
        try:
            checkpoint = self._save(GoalCheckpoint(
                goal_id=goal.goal_id,
                status=status,
                cycle=cycle,
                tool_calls=tool_calls,
                cost_units=cost_units,
                completed_step_ids=tuple(sorted(completed))[:256],
                scope_hash=goal.scope_hash,
                reason=reason,
                started_at=float(started_at if started_at is not None else self._wall_clock()),
            ))
        except GoalRevisionConflict:
            return self._stale_result(goal)
        return GoalRunResult(status, checkpoint, tuple(diagnostics) or ((reason,) if reason else ()))

    def _stale_result(self, goal: GoalSpec) -> GoalRunResult:
        current = self._checkpoints.load(goal.goal_id) if self._checkpoints else None
        if current is None:
            current = GoalCheckpoint(
                goal_id=goal.goal_id,
                status=GoalStatus.WAITING_FOR_USER,
                scope_hash=goal.scope_hash,
                reason="goal revision became stale before commit",
            )
        return GoalRunResult(
            current.status,
            current,
            ("stale goal revision; continuation was not committed",),
        )

    def _save(
        self,
        checkpoint: GoalCheckpoint,
        *,
        expected_revision: int | None = None,
    ) -> GoalCheckpoint:
        if self._checkpoints is not None:
            compare_and_save = getattr(self._checkpoints, "compare_and_save", None)
            if callable(compare_and_save):
                if expected_revision is None:
                    current = self._checkpoints.load(checkpoint.goal_id)
                    expected_revision = current.revision if current is not None else 0
                candidate = GoalCheckpoint(
                    **{
                        **checkpoint.__dict__,
                        "revision": int(expected_revision) + 1,
                        "updated_at": self._wall_clock(),
                    }
                )
                if not compare_and_save(
                    checkpoint.goal_id, int(expected_revision), candidate,
                ):
                    raise GoalRevisionConflict(checkpoint.goal_id)
                return candidate
            self._checkpoints.save(checkpoint)
        return checkpoint


def _safe_reason(value: str) -> str:
    redacted = redact_text(str(value))[:500]
    if any(marker in redacted.lower() for marker in ("prompt", "api_key", "authorization", "secret")):
        return "[detail redacted]"
    return redacted


__all__ = [
    "GoalCheckpoint",
    "GoalCheckpointStore",
    "GoalControl",
    "GoalCycleResult",
    "GoalCycleRunner",
    "GoalLimits",
    "GoalRunResult",
    "GoalRevisionConflict",
    "GoalSpec",
    "GoalStatus",
    "GoalSupervisor",
    "InMemoryGoalCheckpointStore",
]
