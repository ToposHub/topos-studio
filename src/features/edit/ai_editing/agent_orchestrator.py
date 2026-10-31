"""Unified Agent run orchestrator (plan §A1 / §4.3).

A small, Qt-free coordinator that owns the lifecycle of the unified
:class:`AgentRun`: it creates the task + run, drives state transitions through
the validated :class:`AgentRunState` machine, records an immutable
:class:`AgentEvent` for every observable step and fans those events out to
subscribers (the Panel / Run Drawer / diagnostics).

Why a separate layer (plan §4.3):

- The current chat path drives ``_ChatWorker`` business state directly from
  the controller.  Wrapping every request in an ``AgentRun`` makes it
  observable, cancellable and recoverable without rewriting the worker.
- The orchestrator is intentionally a *thin* layer.  It does not know how to
  call a model, edit a timeline or upload media — those stay in injected
  collaborators (ChatModelAdapter, ToolFacade, ...).  This keeps the state
  machine testable without a real provider.

Concurrency:

- A single ``RLock`` guards the in-memory run / event / task maps because the
  model worker runs on a background thread while the UI thread subscribes.
- Subscriber notification happens *outside* the lock so a slow / throwing
  sink can never deadlock the orchestrator.

Run-id isolation (plan §A1 §7):

- Every event carries ``run_id``.  Subscribers are expected to drop callbacks
  whose ``run_id`` is no longer the active run for a session; the orchestrator
  exposes :meth:`active_run_id` so that check is a one-liner.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from src.features.edit.ai_editing.agent_models import (
    AgentEvent,
    AgentEventKind,
    AgentRun,
    AgentRunState,
    AgentTask,
    ApprovalRequest,
    ArtifactRef,
    ContextUsage,
)


#: Subscriber signature: receives one immutable :class:`AgentEvent`.
Subscriber = Callable[[AgentEvent], None]

#: AUD-12708：终态 run 保留上限（供 Run Drawer 查看最近历史）。超过后在
#: run 进入终态时自动淘汰最旧的终态 run（含其事件流与 checkpoint），
#: 避免进程级无界内存增长；非终态 run 永不淘汰。
_MAX_RETAINED_TERMINAL_RUNS: int = 32


class RunNotFoundError(KeyError):
    """Raised when an operation targets an unknown run id."""


class RunNotTransitionableError(RuntimeError):
    """Raised when a convenience transition cannot apply to the current state."""


class AgentOrchestrator:
    """Owns the lifecycle of unified Agent runs (plan §4.3 / §A1)."""

    def __init__(self, *, time_fn: Callable[[], float] | None = None) -> None:
        self._lock = threading.RLock()
        self._time = time_fn or time.time
        self._runs: dict[str, AgentRun] = {}
        self._tasks: dict[str, AgentTask] = {}
        self._events: dict[str, list[AgentEvent]] = {}
        self._subscribers: list[Subscriber] = []
        #: session_id -> active run_id, so subscribers can drop stale callbacks.
        self._active_run_by_session: dict[str, str] = {}
        #: run_id -> checkpoint payloads (for resume / recovery).
        self._checkpoints: dict[str, list[Any]] = {}

    # ------------------------------------------------------------------ #
    # Task + run creation
    # ------------------------------------------------------------------ #

    def start_task(
        self,
        task: AgentTask,
        *,
        mode: str = "chat",
    ) -> str:
        """Create a new run for *task* and emit the initial events.

        Returns the new ``run_id``.  The run starts in ``CREATED``, immediately
        advances to ``GATHERING_CONTEXT`` and emits ``run.created`` followed by
        ``run.context_started`` so the UI shows a phase within ~200ms (plan §9).
        """

        run_id = uuid.uuid4().hex
        run = AgentRun(run_id=run_id, task_id=task.task_id, mode=mode)
        pending_events: list[AgentEvent] = []
        with self._lock:
            self._tasks[task.task_id] = task
            self._runs[run_id] = run
            self._events[run_id] = []
            if task.session_id:
                self._active_run_by_session[task.session_id] = run_id
            created = self._emit_locked(
                run_id, AgentEventKind.RUN_CREATED, payload={"task_id": task.task_id}
            )
            pending_events.append(created)
            # Advance to GATHERING_CONTEXT (always legal from CREATED).
            advanced = run.with_state(AgentRunState.GATHERING_CONTEXT)
            self._runs[run_id] = advanced
            ctx_started = self._emit_locked(
                run_id, AgentEventKind.RUN_CONTEXT_STARTED
            )
            pending_events.append(ctx_started)
        self._notify_all(pending_events)
        return run_id

    # ------------------------------------------------------------------ #
    # Generic state transition
    # ------------------------------------------------------------------ #

    def transition(
        self,
        run_id: str,
        new_state: str,
        *,
        event_kind: str | None = None,
        payload: dict[str, Any] | None = None,
        reason: str = "",
    ) -> AgentRun:
        """Advance *run_id* to *new_state*, validating the transition.

        Optionally emits *event_kind* (useful when a state flip should also
        produce a dedicated event such as ``run.waiting_approval``).
        """

        pending: list[AgentEvent] = []
        with self._lock:
            run = self._require_run_locked(run_id)
            advanced = run.with_state(new_state, reason=reason)
            self._runs[run_id] = advanced
            if event_kind:
                ev = self._emit_locked(run_id, event_kind, payload=payload)
                pending.append(ev)
            if advanced.is_terminal:
                self._prune_locked()
        self._notify_all(pending)
        return advanced

    # ------------------------------------------------------------------ #
    # Convenience transitions (the common happy path)
    # ------------------------------------------------------------------ #

    def mark_context_ready(self, run_id: str) -> AgentRun:
        """GATHERING_CONTEXT → PLANNING, emitting context_ready + plan_started."""

        return self._advance(
            run_id,
            AgentRunState.GATHERING_CONTEXT,
            AgentRunState.PLANNING,
            pre_event=AgentEventKind.RUN_CONTEXT_READY,
            post_event=AgentEventKind.RUN_PLAN_STARTED,
        )

    def mark_plan_ready(self, run_id: str) -> AgentRun:
        """PLANNING → EXECUTING (no approval needed), emitting plan_ready."""

        return self._advance(
            run_id,
            AgentRunState.PLANNING,
            AgentRunState.EXECUTING,
            pre_event=AgentEventKind.RUN_PLAN_READY,
        )

    def request_user_input(self, run_id: str, *, message: str = "") -> AgentRun:
        """Move to WAITING_USER_INPUT so the UI can ask for clarification."""

        return self.transition(
            run_id,
            AgentRunState.WAITING_USER_INPUT,
            event_kind=AgentEventKind.RUN_WAITING_USER_INPUT,
            payload={"message": message} if message else None,
        )

    def request_approval(
        self, run_id: str, approval: ApprovalRequest
    ) -> AgentRun:
        """Move to WAITING_APPROVAL and surface the approval ask.

        Plan §A2: only one confirm action may correspond to a run at a time;
        the approval_id + run_id pair is the stable key subscribers match on.
        """

        return self.transition(
            run_id,
            AgentRunState.WAITING_APPROVAL,
            event_kind=AgentEventKind.RUN_WAITING_APPROVAL,
            payload={
                "approval_id": approval.approval_id,
                "tool_name": approval.tool_name,
                "risk": approval.risk,
            },
        )

    def grant_approval(self, run_id: str) -> AgentRun:
        """Leave WAITING_APPROVAL and begin executing the approved action."""

        return self._advance(
            run_id,
            AgentRunState.WAITING_APPROVAL,
            AgentRunState.EXECUTING,
        )

    def begin_executing(self, run_id: str) -> AgentRun:
        """Enter EXECUTING from PLANNING or WAITING_APPROVAL."""

        run = self.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if run.state not in (AgentRunState.PLANNING, AgentRunState.WAITING_APPROVAL):
            raise RunNotTransitionableError(
                f"cannot begin executing from {run.state!r}"
            )
        return self.transition(run_id, AgentRunState.EXECUTING)

    def begin_verification(self, run_id: str) -> AgentRun:
        """EXECUTING → VERIFYING, emitting verification_started."""

        return self._advance(
            run_id,
            AgentRunState.EXECUTING,
            AgentRunState.VERIFYING,
            post_event=AgentEventKind.RUN_VERIFICATION_STARTED,
        )

    def finish_verification(
        self,
        run_id: str,
        *,
        success: bool = True,
        partial: bool = False,
        reason: str = "",
    ) -> AgentRun:
        """Leave VERIFYING for a terminal state.

        ``success=True``  → SUCCEEDED, ``partial=True`` → PARTIAL_SUCCESS,
        otherwise FAILED.  Emits ``run.completed`` / ``run.failed``.
        """

        run = self.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if run.state != AgentRunState.VERIFYING:
            raise RunNotTransitionableError(
                f"can only finish verification from VERIFYING, not {run.state!r}"
            )
        if success and not partial:
            terminal = AgentRunState.SUCCEEDED
            event_kind = AgentEventKind.RUN_COMPLETED
        elif partial:
            terminal = AgentRunState.PARTIAL_SUCCESS
            event_kind = AgentEventKind.RUN_COMPLETED
        else:
            terminal = AgentRunState.FAILED
            event_kind = AgentEventKind.RUN_FAILED
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason
        if partial:
            # AUD-12703：指标收集器（agent_metrics）依赖 run.completed payload
            # 的 "partial" 键区分 PARTIAL_SUCCESS/SUCCEEDED——此前该键从未被
            # 生产，partial 回归被静默记为成功，发布门的 outcome 混合比失真。
            payload["partial"] = True
        return self.transition(
            run_id,
            terminal,
            event_kind=event_kind,
            payload=payload or None,
            reason=reason,
        )

    def fail(self, run_id: str, *, reason: str = "") -> AgentRun:
        """Move a run straight to FAILED from any non-terminal state.

        AUD-12707：PAUSED_FOR_RESUME 在冻结状态机（agent_models，不在本修复
        白名单）中没有 FAILED 出边（仅 GATHERING_CONTEXT/CANCELLED），此处经
        GATHERING_CONTEXT 合法中转后落 FAILED，使"恢复失败/校验失败"的
        paused run 也能兑现本方法"任意非终态可 fail"的契约，而不是抛
        ValueError 把原始错误掩盖掉。
        """

        run = self.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if run.is_terminal:
            return run
        pending: list[AgentEvent] = []
        with self._lock:
            current = self._require_run_locked(run_id)
            if current.is_terminal:
                return current
            if current.state == AgentRunState.PAUSED_FOR_RESUME:
                current = current.with_state(
                    AgentRunState.GATHERING_CONTEXT,
                    reason="resume attempt failed",
                )
                self._runs[run_id] = current
            advanced = current.with_state(AgentRunState.FAILED, reason=reason)
            self._runs[run_id] = advanced
            payload = {"reason": reason} if reason else None
            pending.append(self._emit_locked(
                run_id, AgentEventKind.RUN_FAILED, payload=payload,
            ))
            self._prune_locked()
        self._notify_all(pending)
        return advanced

    def request_cancel(self, run_id: str, *, reason: str = "user_requested") -> bool:
        """Cooperatively cancel a run.

        Emits ``run.cancel_requested`` then advances to ``CANCELLED``.  Returns
        ``False`` if the run is already terminal (no-op), ``True`` otherwise.
        """

        run = self.get_run(run_id)
        if run is None:
            return False
        if run.is_terminal:
            return False
        pending: list[AgentEvent] = []
        with self._lock:
            current = self._runs.get(run_id)
            if current is None or current.is_terminal:
                return False
            requested = self._emit_locked(
                run_id,
                AgentEventKind.RUN_CANCEL_REQUESTED,
                payload={"reason": reason},
            )
            pending.append(requested)
            cancelled = current.with_state(AgentRunState.CANCELLED, reason=reason)
            self._runs[run_id] = cancelled
            cancelled_event = self._emit_locked(
                run_id, AgentEventKind.RUN_CANCELLED, payload={"reason": reason}
            )
            pending.append(cancelled_event)
            self._prune_locked()
        self._notify_all(pending)
        return True

    def request_cancel_for_session(
        self, session_id: str, *, reason: str = "user_requested",
    ) -> bool:
        """Cancel the active (non-terminal) run for *session_id*.

        Convenience for hosts that track runs by session — the controller
        starts a chat without knowing the run id (it is minted inside
        :meth:`ChatRunDriver.run_chat`).  Returns ``True`` if a run was
        cancelled, ``False`` if the session has no active run.
        """

        run_id = self.active_run_id(session_id)
        if run_id is None:
            return False
        return self.request_cancel(run_id, reason=reason)

    def pause_for_resume(self, run_id: str, *, reason: str = "") -> AgentRun:
        """Park a verifying run so it can be resumed after a restart."""

        return self.transition(
            run_id,
            AgentRunState.PAUSED_FOR_RESUME,
            reason=reason or "paused for resume",
        )

    # ------------------------------------------------------------------ #
    # Side-channel updates (no state change)
    # ------------------------------------------------------------------ #

    def emit(
        self,
        run_id: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        parent_id: str = "",
    ) -> AgentEvent:
        """Record an event without changing run state (e.g. tool progress)."""

        with self._lock:
            event = self._emit_locked(run_id, kind, payload=payload, parent_id=parent_id)
        self._notify_all([event])
        return event

    def record_usage(self, run_id: str, usage: ContextUsage) -> None:
        """Attach token usage to a run and emit ``run.usage_recorded``.

        No-op on a terminal run: a cancelled/failed run must keep its final
        snapshot immutable so a late usage callback cannot retroactively
        rewrite the authoritative token accounting (plan §4.2 §P0).
        """

        with self._lock:
            run = self._require_run_locked(run_id)
            if run.is_terminal:
                return
            self._runs[run_id] = AgentRun(
                run_id=run.run_id,
                task_id=run.task_id,
                mode=run.mode,
                state=run.state,
                started_at=run.started_at,
                ended_at=run.ended_at,
                checkpoint_id=run.checkpoint_id,
                last_event_id=run.last_event_id,
                context_usage=usage,
                artifact_refs=run.artifact_refs,
                error_code=run.error_code,
                state_reason=run.state_reason,
            )
            event = self._emit_locked(
                run_id,
                AgentEventKind.RUN_USAGE_RECORDED,
                payload=usage.to_dict(),
            )
        self._notify_all([event])

    def stage_artifact(self, run_id: str, artifact: ArtifactRef) -> None:
        """Register a produced artifact and emit ``artifact.staged``.

        No-op on a terminal run so a late artifact callback cannot attach
        results to a run the user already cancelled/failed.
        """

        with self._lock:
            run = self._require_run_locked(run_id)
            if run.is_terminal:
                return
            refs = run.artifact_refs
            if artifact.artifact_id not in refs:
                refs = refs + (artifact.artifact_id,)
            self._runs[run_id] = AgentRun(
                run_id=run.run_id,
                task_id=run.task_id,
                mode=run.mode,
                state=run.state,
                started_at=run.started_at,
                ended_at=run.ended_at,
                checkpoint_id=run.checkpoint_id,
                last_event_id=run.last_event_id,
                context_usage=run.context_usage,
                artifact_refs=refs,
                error_code=run.error_code,
                state_reason=run.state_reason,
            )
            event = self._emit_locked(
                run_id,
                AgentEventKind.ARTIFACT_STAGED,
                payload={
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "payload_hash": artifact.payload_hash,
                },
            )
        self._notify_all([event])

    def save_checkpoint(
        self,
        run_id: str,
        checkpoint_id: str,
        *,
        completed_steps: tuple[str, ...] = (),
        messages_digest: str = "",
        context_hash: str = "",
        protected_context: tuple[str, ...] = (),
    ) -> None:
        """Stamp a checkpoint id on the run and emit ``run.checkpoint_saved``.

        No-op on a terminal run (a checkpoint for an already-finished run
        is meaningless and must not mutate its snapshot).
        """

        from src.features.edit.ai_editing.agent_models import AgentCheckpoint

        with self._lock:
            run = self._require_run_locked(run_id)
            if run.is_terminal:
                return
            checkpoint = AgentCheckpoint(
                checkpoint_id=checkpoint_id,
                run_id=run_id,
                state=run.state,
                completed_steps=completed_steps,
                messages_digest=messages_digest,
                context_hash=context_hash,
                protected_context=protected_context,
            )
            self._runs[run_id] = AgentRun(
                run_id=run.run_id,
                task_id=run.task_id,
                mode=run.mode,
                state=run.state,
                started_at=run.started_at,
                ended_at=run.ended_at,
                checkpoint_id=checkpoint_id,
                last_event_id=run.last_event_id,
                context_usage=run.context_usage,
                artifact_refs=run.artifact_refs,
                error_code=run.error_code,
                state_reason=run.state_reason,
            )
            event = self._emit_locked(
                run_id,
                AgentEventKind.RUN_CHECKPOINT_SAVED,
                payload={"checkpoint_id": checkpoint_id, "state": run.state},
            )
            # Stash the checkpoint payload for later retrieval (recovery).
            self._checkpoints.setdefault(run_id, []).append(checkpoint)
        self._notify_all([event])

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_task(self, task_id: str) -> AgentTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def get_events(self, run_id: str) -> tuple[AgentEvent, ...]:
        with self._lock:
            return tuple(self._events.get(run_id, ()))

    def list_runs(self) -> tuple[AgentRun, ...]:
        with self._lock:
            return tuple(self._runs.values())

    def active_run_id(self, session_id: str) -> str | None:
        """The run id subscribers should treat as current for *session_id*.

        Returns ``None`` if the session has no run or its run is terminal, so a
        stale callback whose run_id differs can be dropped (plan §A1 §7).
        """

        with self._lock:
            run_id = self._active_run_by_session.get(session_id)
            if run_id is None:
                return None
            run = self._runs.get(run_id)
            if run is None or run.is_terminal:
                return None
            return run_id

    def is_run_active(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        return run is not None and not run.is_terminal

    # ---------------- housekeeping (AUD-12708) ---------------- #

    def prune_terminal_runs(self, keep_last_n: int = _MAX_RETAINED_TERMINAL_RUNS) -> int:
        """Evict the oldest terminal runs and everything they own.

        Keeps the most recent *keep_last_n* terminal runs (by ``ended_at`` /
        ``started_at``) for the Run Drawer; non-terminal runs are never evicted.
        Removes the run's event stream, checkpoints and (when unreferenced) its
        task. Also drops session->run entries that pointed at evicted runs.
        Returns the number of runs evicted.
        """

        if keep_last_n < 0:
            raise ValueError("keep_last_n cannot be negative")
        with self._lock:
            return self._prune_locked(keep_last_n)

    def _prune_locked(self, keep_last_n: int = _MAX_RETAINED_TERMINAL_RUNS) -> int:
        """Lock-held eviction of excess terminal runs（AUD-12708）.

        Called automatically whenever a run reaches a terminal state so memory
        stays bounded even if the host never calls :meth:`prune_terminal_runs`.
        """

        terminal = [r for r in self._runs.values() if r.is_terminal]
        excess = len(terminal) - keep_last_n
        if excess <= 0:
            return 0
        terminal.sort(key=lambda r: (r.ended_at or r.started_at, r.started_at))
        evicted = {r.run_id for r in terminal[:excess]}
        for rid in evicted:
            run = self._runs.pop(rid, None)
            self._events.pop(rid, None)
            self._checkpoints.pop(rid, None)
            if run is not None and not any(
                r.task_id == run.task_id for r in self._runs.values()
            ):
                # task 仍被其他 run（如 retry）引用时保留
                self._tasks.pop(run.task_id, None)
        stale_sessions = [
            sid for sid, rid in self._active_run_by_session.items()
            if rid in evicted
        ]
        for sid in stale_sessions:
            del self._active_run_by_session[sid]
        return len(evicted)

    # ------------------------------------------------------------------ #
    # Persistence (plan §A5 §5.2 step 9-10: restart recovery)
    # ------------------------------------------------------------------ #

    def export_state(self) -> dict[str, Any]:
        """Serialise every run + task for persistence (plan §A5 §5.2 §9).

        Events are not exported (potentially large); each run's state +
        checkpoint id is the recoverable trail.  The returned dict is safe to
        persist once the caller has applied any session-scoped redaction.
        """

        with self._lock:
            return {
                "runs": [run.to_dict() for run in self._runs.values()],
                "tasks": [task.to_dict() for task in self._tasks.values()],
                "active_run_by_session": dict(self._active_run_by_session),
            }

    def restore_state(self, data: dict[str, Any]) -> int:
        """Restore runs + tasks from a snapshot (plan §A5 §5.2 §9-10).

        Restored entries merge into the in-memory state (a matching id
        overwrites the in-memory copy so a persisted terminal state wins).
        Corrupt entries are skipped individually rather than aborting the
        whole restore.  Returns the number of runs restored.

        A restored run that was ``PAUSED_FOR_RESUME`` stays paused; the host
        is expected to re-validate before resuming (plan §2.7 / §10.2 §7).
        """

        restored = 0
        with self._lock:
            for task_data in data.get("tasks", []):
                if not isinstance(task_data, dict):
                    continue
                try:
                    task = AgentTask.from_dict(task_data)
                    self._tasks[task.task_id] = task
                except Exception:  # pragma: no cover - skip corrupt entry
                    continue
            for run_data in data.get("runs", []):
                if not isinstance(run_data, dict):
                    continue
                try:
                    run = AgentRun.from_dict(run_data)
                    self._runs[run.run_id] = run
                    self._events.setdefault(run.run_id, [])
                    restored += 1
                except Exception:  # pragma: no cover - skip corrupt entry
                    continue
            for sid, rid in (data.get("active_run_by_session") or {}).items():
                if isinstance(sid, str) and isinstance(rid, str):
                    self._active_run_by_session[sid] = rid
        return restored

    # ------------------------------------------------------------------ #
    # Subscribers
    # ------------------------------------------------------------------ #

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Register an event sink; returns an unsubscribe callable."""

        with self._lock:
            self._subscribers.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsubscribe

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _require_run_locked(self, run_id: str) -> AgentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def _emit_locked(
        self,
        run_id: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        parent_id: str = "",
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=run_id,
            kind=kind,
            payload=dict(payload or {}),
            parent_id=parent_id,
            timestamp=self._time(),
        )
        self._events.setdefault(run_id, []).append(event)
        # Stamp last_event_id on the run so subscribers can order.
        run = self._runs.get(run_id)
        if run is not None:
            self._runs[run_id] = AgentRun(
                run_id=run.run_id,
                task_id=run.task_id,
                mode=run.mode,
                state=run.state,
                started_at=run.started_at,
                ended_at=run.ended_at,
                checkpoint_id=run.checkpoint_id,
                last_event_id=event.event_id,
                context_usage=run.context_usage,
                artifact_refs=run.artifact_refs,
                error_code=run.error_code,
                state_reason=run.state_reason,
            )
        return event

    def _advance(
        self,
        run_id: str,
        expected_from: str,
        to_state: str,
        *,
        pre_event: str | None = None,
        post_event: str | None = None,
    ) -> AgentRun:
        run = self.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if run.state != expected_from:
            raise RunNotTransitionableError(
                f"expected {expected_from!r} but run is {run.state!r}"
            )
        pending: list[AgentEvent] = []
        with self._lock:
            current = self._require_run_locked(run_id)
            if pre_event:
                pending.append(self._emit_locked(run_id, pre_event))
            advanced = current.with_state(to_state)
            self._runs[run_id] = advanced
            if post_event:
                pending.append(self._emit_locked(run_id, post_event))
        self._notify_all(pending)
        return advanced

    def _notify_all(self, events: list[AgentEvent]) -> None:
        if not events:
            return
        with self._lock:
            subscribers = list(self._subscribers)
        # Notify outside the lock: a slow / throwing sink must not block the
        # orchestrator or other subscribers.
        for event in events:
            for sink in subscribers:
                try:
                    sink(event)
                except Exception:  # pragma: no cover - defensive: never let a sink crash the run
                    pass


__all__ = [
    "AgentOrchestrator",
    "RunNotFoundError",
    "RunNotTransitionableError",
    "Subscriber",
]
