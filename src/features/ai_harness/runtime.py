"""HarnessRuntime — 唯一 start/cancel/retry/resume/get_run 入口（plan §0.2 / §10.2 P0.2）。

P0 提供的是 **fake 版本**：不接 Qt / OpenGL / 网络 / 真实模型。它只做：

1. 接受 :class:`Invocation`，分配 run_id，发布 ``run.created`` 事件。
2. 维护一个可变内部状态 + append-only 事件流；提供 ``get_run`` 返回聚合只读
   ``RunRecord``。
3. ``cancel`` 通过协作 cancel token 传播（plan §10.2 P0.6）；下游通过
   ``is_cancelled(run_id)`` 轮询。
4. ``retry`` / ``resume`` 各自显式，且 retry 总是分配新 run_id（避免与旧 run
   的状态混淆）。
5. 事件订阅（``add_subscriber``）：UI / 审计据此订阅，**且不阻塞 runtime**。
   subscriber 抛错只记日志、不影响 run。

真实 provider / scheduler 接入由 :mod:`scheduler_adapter`（桥接
``JobRegistry`` / ``AnalysisJobScheduler``）和 :mod:`legacy_adapters`（包装
``AssistantService.run_workflow`` / ``run_skill``）完成，runtime 本身保持薄。

红线（plan §10.2 P0.6 / §1.4）：

- 取消是真实 cancel token 传播，不是仅变更 UI 状态文案。
- 工程关闭（``detach_session``）后，run 不再回写已关闭的项目。
- 旧 approval 不可复用（由 ``ApprovalService`` 保证；runtime 只持有 grant_id
  引用，不持有 grant 本身）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from src.features.ai_harness.invocation_models import (
    ArtifactRef,
    Invocation,
    JobRef,
    RunEvent,
    RunEventKind,
    RunRecord,
    RunStatus,
    sanitize_for_storage,
)

if TYPE_CHECKING:
    from src.features.ai_harness.rollout_metrics import RunMetricsSnapshot


_LOG = logging.getLogger(__name__)

#: subscriber 签名：(event: RunEvent) -> None。抛错被吞掉并记日志。
Subscriber = Callable[[RunEvent], None]


@dataclass
class _RunState:
    """runtime 内部对单个 run 的可变状态。``RunRecord`` 是它的不可变快照。"""

    run_id: str
    invocation: Invocation
    status: str = RunStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0
    events: list[RunEvent] = field(default_factory=list)
    jobs: list[JobRef] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    cost: float = 0.0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    cancel_reason: str = ""
    error_code: str = ""
    error_message: str = ""
    #: 协作 cancel token：True 表示已请求取消。下游在检查点轮询。
    cancel_requested: bool = False
    #: 本 run 所属的 session（工程）ID；detach 后该 session 的 run 不再被改写。
    session_id: str = ""
    session_detached: bool = False

    def to_record(self) -> RunRecord:
        return RunRecord(
            run_id=self.run_id,
            invocation_id=self.invocation.invocation_id,
            parent_session_id=self.invocation.parent_session_id or self.session_id,
            source=self.invocation.source,
            skill_id=self.invocation.skill_id,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            progress=self.progress,
            events=tuple(self.events),
            jobs=tuple(self.jobs),
            artifacts=tuple(self.artifacts),
            cost=self.cost,
            diagnostics=tuple(self.diagnostics),
            cancel_reason=self.cancel_reason,
            error_code=self.error_code,
            error_message=self.error_message,
        )


@dataclass(frozen=True)
class RuntimeRestoreReport:
    """Result of restoring a cross-process runtime snapshot."""

    restored_run_ids: tuple[str, ...] = ()
    interrupted_run_ids: tuple[str, ...] = ()
    skipped_entries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.skipped_entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored_run_ids": list(self.restored_run_ids),
            "interrupted_run_ids": list(self.interrupted_run_ids),
            "skipped_entries": list(self.skipped_entries),
            "ok": self.ok,
        }


class HarnessRuntime:
    """统一运行时（plan §0.2）。

    线程安全：内部用 ``RLock`` 保护 ``_runs`` 与订阅者列表。subscriber 在锁外
    调用（避免慢 subscriber 阻塞 runtime）。
    """

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}
        self._invocations: dict[str, Invocation] = {}
        self._subscribers: list[Subscriber] = []
        self._lock = threading.RLock()
        # 已 detach 的 session 集合：这些工程已关闭，其下的 run 不再被回写。
        self._detached_sessions: set[str] = set()

    # ------------------------------------------------------------------ #
    # 跨进程快照：只恢复可审计状态，不恢复孤儿 worker
    # ------------------------------------------------------------------ #

    def export_snapshot(self) -> dict[str, Any]:
        """Export a sanitized run/event snapshot for project persistence.

        Provider threads and cancel callbacks are intentionally excluded.  A
        later process can restore the history and explicitly retry through
        the provider's idempotency boundary.
        """
        with self._lock:
            runs = []
            for state in self._runs.values():
                record = state.to_record().to_public_dict()
                record.update({
                    "invocation": state.invocation.to_public_dict(),
                    "session_id": state.session_id,
                    "session_detached": state.session_detached,
                    "cancel_requested": state.cancel_requested,
                })
                runs.append(record)
            snapshot = {
                "schema_version": 1,
                "detached_sessions": sorted(self._detached_sessions),
                "runs": runs,
            }
        return sanitize_for_storage(snapshot)

    def restore_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        terminate_active: bool = True,
    ) -> RuntimeRestoreReport:
        """Restore run history across processes with fail-closed interruption.

        Active runs are marked ``RUN_INTERRUPTED`` by default.  No provider
        worker is revived, so callers must use the explicit retry path after
        re-establishing current project context and credentials.
        """
        if not isinstance(snapshot, dict):
            return RuntimeRestoreReport(skipped_entries=("snapshot_not_object",))
        if int(snapshot.get("schema_version", 0) or 0) != 1:
            return RuntimeRestoreReport(skipped_entries=("snapshot_schema_unsupported",))
        detached = snapshot.get("detached_sessions", ())
        rows = snapshot.get("runs", ())
        if not isinstance(detached, (list, tuple)) or not isinstance(rows, (list, tuple)):
            return RuntimeRestoreReport(skipped_entries=("snapshot_shape_invalid",))

        restored: list[str] = []
        interrupted: list[str] = []
        skipped: list[str] = []
        with self._lock:
            self._detached_sessions.update(str(item) for item in detached if item)
            for index, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    skipped.append(f"entry_{index}:not_object")
                    continue
                run_id = str(raw.get("run_id", ""))
                if not run_id:
                    skipped.append(f"entry_{index}:missing_run_id")
                    continue
                if run_id in self._runs:
                    skipped.append(f"{run_id}:duplicate")
                    continue
                invocation_data = raw.get("invocation")
                if not isinstance(invocation_data, dict):
                    skipped.append(f"{run_id}:missing_invocation")
                    continue
                try:
                    invocation = Invocation.from_public_dict(invocation_data)
                    status = str(raw.get("status", RunStatus.DRAFT))
                    if status not in (RunStatus.ACTIVE_STATES | RunStatus.TERMINAL_STATES | {RunStatus.DRAFT}):
                        raise ValueError("unknown run status")
                    events = tuple(
                        RunEvent.from_public_dict(item)
                        for item in raw.get("events", ())
                        if isinstance(item, dict)
                    )
                    jobs = tuple(
                        JobRef.from_dict(item)
                        for item in raw.get("jobs", ())
                        if isinstance(item, dict)
                    )
                    artifacts = tuple(
                        ArtifactRef.from_dict(item)
                        for item in raw.get("artifacts", ())
                        if isinstance(item, dict)
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    skipped.append(f"{run_id}:invalid:{type(exc).__name__}")
                    continue

                session_id = str(raw.get("session_id", "")) or invocation.parent_session_id
                session_detached = bool(raw.get("session_detached", False))
                session_detached = session_detached or session_id in self._detached_sessions
                diagnostics = [
                    sanitize_for_storage(item)
                    for item in raw.get("diagnostics", ())
                    if isinstance(item, dict)
                ]
                cancel_requested = bool(raw.get("cancel_requested", False))
                error_code = str(raw.get("error_code", ""))
                error_message = str(raw.get("error_message", ""))
                finished_at = raw.get("finished_at")
                if terminate_active and RunStatus.is_active(status):
                    previous_status = status
                    status = RunStatus.FAILED
                    error_code = "RUN_INTERRUPTED"
                    error_message = (
                        "run safely interrupted during application restart; "
                        f"previous status: {previous_status}"
                    )
                    finished_at = finished_at or time.time()
                    cancel_requested = False
                    diagnostics.append({
                        "code": "RUN_INTERRUPTED",
                        "previous_status": previous_status,
                        "recovery": "explicit_retry_required",
                    })
                    events = events + (RunEvent(
                        run_id=run_id,
                        kind=RunEventKind.DIAGNOSTIC,
                        payload={
                            "code": "RUN_INTERRUPTED",
                            "previous_status": previous_status,
                        },
                    ),)
                    interrupted.append(run_id)
                state = _RunState(
                    run_id=run_id,
                    invocation=invocation,
                    status=status,
                    created_at=float(raw.get("created_at", time.time()) or time.time()),
                    started_at=raw.get("started_at"),
                    finished_at=finished_at,
                    progress=float(raw.get("progress", 0.0) or 0.0),
                    events=list(events),
                    jobs=list(jobs),
                    artifacts=list(artifacts),
                    cost=float(raw.get("cost", 0.0) or 0.0),
                    diagnostics=diagnostics,
                    cancel_reason=("application_restart" if run_id in interrupted
                                   else str(raw.get("cancel_reason", ""))),
                    error_code=error_code,
                    error_message=error_message,
                    cancel_requested=cancel_requested,
                    session_id=session_id,
                    session_detached=session_detached,
                )
                self._runs[run_id] = state
                self._invocations[invocation.invocation_id] = invocation
                restored.append(run_id)
        return RuntimeRestoreReport(
            restored_run_ids=tuple(restored),
            interrupted_run_ids=tuple(interrupted),
            skipped_entries=tuple(skipped),
        )

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def add_subscriber(self, fn: Subscriber) -> Callable[[], None]:
        """注册事件订阅者。返回取消订阅的 callable。

        subscriber 在每次事件发布时被调用（在 runtime 锁外）。subscriber 抛错
        只记日志、不影响 run 或其它订阅者。
        """
        with self._lock:
            self._subscribers.append(fn)

        def _unsub() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return _unsub

    # ------------------------------------------------------------------ #
    # 生命周期：start / cancel / retry / resume
    # ------------------------------------------------------------------ #

    def start(self, invocation: Invocation) -> str:
        """接受 invocation，分配 run_id，发布 ``run.created``。

        P0 不立即执行 provider（fake）；真实执行由 legacy adapter / scheduler
        adapter 调用 :meth:`mark_running` / :meth:`attach_job` / :meth:`finish`
        等显式方法驱动。这样 runtime 与 provider 解耦，便于先 fake 后真。

        Returns:
            run_id。
        """
        run_id = uuid.uuid4().hex
        with self._lock:
            state = _RunState(
                run_id=run_id,
                invocation=invocation,
                status=RunStatus.DRAFT,
                session_id=invocation.parent_session_id,
                # detach 传染：若父 session 已关闭，新建 run 也标 detached（防御）。
                # 判定必须在锁内与 detach_session 的"加集合 + 遍历标记"互斥，
                # 否则并发窗口会漏标 detached（AUD-22310）。
                session_detached=invocation.parent_session_id in self._detached_sessions,
            )
            self._runs[run_id] = state
            self._invocations[invocation.invocation_id] = invocation
        self._emit(state, RunEventKind.RUN_CREATED, payload={
            "source": invocation.source,
            "skill_id": invocation.skill_id,
            "scope": invocation.scope.to_dict(),
            "policy_permission": invocation.policy.permission,
        })
        return run_id

    def cancel(self, run_id: str, *, reason: str = "user_requested") -> bool:
        """请求取消一个 run（协作取消，plan §10.2 P0.6）。

        设置 ``cancel_requested=True``；下游 provider / executor 在检查点轮询
        :meth:`is_cancelled` 后自行终止。**不**强杀线程。重复取消幂等。

        Returns:
            是否成功标记（run 不存在、已 detach 或已终态返回 False）。
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return False
            if state.session_detached:
                # 已关闭工程的 run 不再响应取消（避免回写状态）。
                _LOG.debug(
                    "cancel ignored for detached session run %s (session %s)",
                    run_id, state.session_id,
                )
                return False
            if RunStatus.is_terminal(state.status):
                # 终态 run 不再响应取消：不置 cancel token、不发 cancel 事件
                # （AUD-22311；避免终态 run 的投影状态被 run.cancel_requested
                # 翻成 cancelling）。注意 DRAFT→CANCELLED 是合法迁移，DRAFT
                # 仍可取消（与 _TRANSITIONS 一致）。
                _LOG.debug(
                    "cancel ignored for terminal run %s (status %s)",
                    run_id, state.status,
                )
                return False
            if state.cancel_requested:
                return True  # 幂等
            state.cancel_requested = True
            state.cancel_reason = reason
        self._emit(state, RunEventKind.RUN_CANCEL_REQUESTED, payload={"reason": reason})
        return True

    def handoff(self, run_id: str, *, reason: str = "manual_handoff") -> bool:
        """Request a cooperative stop and hand the run to a human operator.

        Handoff never force-kills a provider thread and never invents a paused
        result.  It records a distinct audit event, then uses the same cancel
        token as normal cancellation so the adapter can converge to a real
        ``CANCELLED`` terminal state.
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            if not RunStatus.is_active(state.status):
                return False
            if state.cancel_requested:
                return True
            state.cancel_requested = True
            state.cancel_reason = reason
        self._emit(state, RunEventKind.RUN_HANDOFF_REQUESTED, payload={
            "reason": reason,
            "status": state.status,
        })
        self._emit(state, RunEventKind.RUN_CANCEL_REQUESTED, payload={
            "reason": reason,
            "handoff": True,
        })
        return True

    def retry(self, run_id: str) -> str | None:
        """重试一个 run：基于其 invocation 新建一个 run（新 run_id）。

        旧 run 不被修改（保持终态）。新 run 状态从 ``DRAFT`` 开始。如果旧 run
        所属 session 已 detach，拒绝 retry（plan §1.4：工程关闭后不回写）。
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            if state.session_detached:
                _LOG.warning(
                    "retry rejected for detached session run %s (session %s)",
                    run_id, state.session_id,
                )
                return None
            invocation = state.invocation
        new_run_id = self.start(invocation)
        # AUD-22313：原此处有一个空锁块（锁内取值后无任何作用即离开），
        # 属死代码，已删除；run.created 事件由 start() 内部发布，retry 事件
        # 在下方 _emit_by_id 中发布，两者均自带锁保护。
        self._emit_by_id(new_run_id, RunEventKind.RUN_RETRY_REQUESTED, payload={
            "retried_from": run_id,
        })
        return new_run_id

    def resume(self, run_id: str) -> bool:
        """从 ``WAITING_FOR_USER`` 恢复到 ``RUNNING``（plan §6.1）。

        非 ``WAITING_FOR_USER`` 状态或已 detach 时拒绝。
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return False
            if state.session_detached:
                return False
            if not RunStatus.can_transition(state.status, RunStatus.RUNNING):
                return False
            self._set_status_unlocked(state, RunStatus.RUNNING)
        self._emit(state, RunEventKind.RUN_RESUMED, payload={})
        return True

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            state = self._runs.get(run_id)
            return state.to_record() if state else None

    def get_invocation(self, run_id: str) -> Invocation | None:
        with self._lock:
            state = self._runs.get(run_id)
            return state.invocation if state else None

    def list_runs(
        self, *, session_id: str | None = None, parent_session_id: str | None = None,
    ) -> list[RunRecord]:
        """列出 run（按创建时间升序）。可按 session 过滤。"""
        with self._lock:
            states = list(self._runs.values())
        records = [s.to_record() for s in states]
        if session_id is not None:
            records = [r for r in records if _session_of(r, self) == session_id]
        if parent_session_id is not None:
            records = [r for r in records if r.parent_session_id == parent_session_id]
        records.sort(key=lambda r: r.created_at)
        return records

    def get_metrics(self) -> RunMetricsSnapshot:
        """Return privacy-safe rollout metrics from current run snapshots."""
        from src.features.ai_harness.rollout_metrics import aggregate_run_metrics

        return aggregate_run_metrics(self.list_runs())

    def is_cancelled(self, run_id: str) -> bool:
        """供下游 provider / executor 在检查点轮询的 cancel token。"""
        with self._lock:
            state = self._runs.get(run_id)
            return bool(state and state.cancel_requested)

    # ------------------------------------------------------------------ #
    # 状态推进（由 adapter / legacy 包装在 provider 完成回调里调用）
    # ------------------------------------------------------------------ #

    def set_status(self, run_id: str, status: str) -> bool:
        """显式推进状态；非法迁移返回 False。"""
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return False
            if state.session_detached:
                return False
            if not RunStatus.can_transition(state.status, status):
                return False
            self._set_status_unlocked(state, status)
        self._emit(state, RunEventKind.RUN_STATE_CHANGED, payload={"status": status})
        return True

    def mark_running(self, run_id: str) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            # DRAFT -> VALIDATING -> QUEUED -> RUNNING 一次性推进到 RUNNING。
            for target in (RunStatus.VALIDATING, RunStatus.QUEUED, RunStatus.RUNNING):
                if RunStatus.can_transition(state.status, target):
                    self._set_status_unlocked(state, target)
                    if target == RunStatus.RUNNING:
                        state.started_at = state.started_at or time.time()
                        break
            else:
                if state.status != RunStatus.RUNNING:
                    return False
        self._emit(state, RunEventKind.RUN_STATE_CHANGED, payload={"status": state.status})
        return True

    def mark_waiting_for_user(self, run_id: str, *, message: str = "") -> bool:
        ok = self.set_status(run_id, RunStatus.WAITING_FOR_USER)
        if ok:
            self._emit_by_id(run_id, RunEventKind.APPROVAL_REQUIRED, payload={"message": message})
        return ok

    def finish(
        self, run_id: str, *, success: bool = True, with_warnings: bool = False,
        error_code: str = "", error_message: str = "",
    ) -> bool:
        """把 run 推进到终态。已 detach 的 session 不再回写（plan §1.4 红线）。"""
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            if success:
                target = (
                    RunStatus.SUCCEEDED_WITH_WARNINGS if with_warnings else RunStatus.SUCCEEDED
                )
            elif state.cancel_requested:
                target = RunStatus.CANCELLED
            else:
                target = RunStatus.FAILED
            if not RunStatus.can_transition(state.status, target):
                if not RunStatus.is_terminal(state.status):
                    # 非法迁移（如 DRAFT→SUCCEEDED）：不写任何字段，避免留下
                    # "带 finished_at 的活跃 run" 脏状态（AUD-22309）。
                    return False
                # 已终态：允许把补充的失败信息记入 diagnostics，但不改状态、
                # 不覆盖已终态 run 的 error 字段、不发伪 RUN_STATE_CHANGED 事件
                # （AUD-22309）。
                if error_code or error_message:
                    state.diagnostics.append(sanitize_for_storage({
                        "code": str(error_code or "FINISH_AFTER_TERMINAL"),
                        "message": str(error_message)[:500],
                        "suppressed": "run already terminal; fields not overwritten",
                    }))
                return True
            # 迁移校验通过后才写入任何字段（AUD-22309）。
            state.finished_at = time.time()
            if target == RunStatus.FAILED:
                state.error_code = error_code
                state.error_message = error_message
            self._set_status_unlocked(state, target)
            state.progress = 1.0
        self._emit(state, RunEventKind.RUN_STATE_CHANGED, payload={"status": state.status})
        return True

    # ------------------------------------------------------------------ #
    # Job / artifact / cost / progress（由 scheduler adapter / provider 调用）
    # ------------------------------------------------------------------ #

    def attach_job(self, run_id: str, job_ref: JobRef) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            # 去重（同一 job_id 不重复追加）。
            if any(j.job_id == job_ref.job_id for j in state.jobs):
                # 已存在则更新。
                state.jobs = [
                    job_ref if j.job_id == job_ref.job_id else j for j in state.jobs
                ]
            else:
                state.jobs.append(job_ref)
        self._emit_by_id(run_id, RunEventKind.JOB_ATTACHED, payload={
            "job_id": job_ref.job_id, "kind": job_ref.kind, "label": job_ref.label,
        })
        return True

    def update_job_progress(
        self, run_id: str, job_id: str, status: str, progress: float,
    ) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            updated = False
            new_jobs: list[JobRef] = []
            for j in state.jobs:
                if j.job_id == job_id:
                    new_jobs.append(replace(j, status=status, progress=progress))
                    updated = True
                else:
                    new_jobs.append(j)
            if not updated:
                return False
            state.jobs = new_jobs
        self._emit_by_id(run_id, RunEventKind.JOB_PROGRESS, payload={
            "job_id": job_id, "status": status, "progress": progress,
        })
        return True

    def record_artifact(self, run_id: str, artifact: ArtifactRef) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            state.artifacts.append(artifact)
        self._emit_by_id(run_id, RunEventKind.ARTIFACT_PRODUCED, payload={
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "producer_node_id": artifact.producer_node_id,
        })
        return True

    def record_cost(self, run_id: str, cost: float) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            state.cost += cost
        self._emit_by_id(run_id, RunEventKind.COST_RECORDED, payload={"cost_delta": cost})
        return True

    def record_progress(self, run_id: str, progress: float) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            state.progress = max(state.progress, min(1.0, max(0.0, progress)))
        return True

    def record_diagnostic(self, run_id: str, diagnostic: dict[str, Any]) -> bool:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
            state.diagnostics.append(sanitize_for_storage(diagnostic))
        self._emit_by_id(run_id, RunEventKind.DIAGNOSTIC, payload=diagnostic)
        return True

    # ------------------------------------------------------------------ #
    # 节点级事件（P9：补齐 RunEventKind.NODE_* 词汇）
    # ------------------------------------------------------------------ #

    # kind 字符串 → RunEventKind.NODE_* 映射（plan §6.1）。
    _NODE_KIND_MAP = {
        "queued": RunEventKind.NODE_QUEUED,
        "started": RunEventKind.NODE_STARTED,
        "progress": RunEventKind.NODE_PROGRESS,
        "waiting_input": RunEventKind.NODE_WAITING_INPUT,
        "succeeded": RunEventKind.NODE_SUCCEEDED,
        "failed": RunEventKind.NODE_FAILED,
        "cancelled": RunEventKind.NODE_CANCELLED,
        "cached": RunEventKind.NODE_CACHED,
        "skipped": RunEventKind.NODE_SKIPPED,
    }

    def emit_node_event(
        self, run_id: str, kind: str, node_id: str,
        *, payload: dict[str, Any] | None = None,
    ) -> bool:
        """发布节点级事件（plan §10.7 P9；补齐既有 NODE_* 词汇）。

        与 run 级 :meth:`_emit` 的区别：本方法**显式填** :attr:`RunEvent.node_id`，
        使订阅者能按节点过滤事件流（既有 ``_emit`` 永远 ``node_id=""``，保持不变）。

        Args:
            run_id: 目标 run。
            kind: 节点事件短名（``"started"`` / ``"succeeded"`` / ``"failed"``
                / ``"cancelled"`` / ``"queued"`` / ``"progress"`` /
                ``"waiting_input"`` / ``"cached"`` / ``"skipped"``）。未知 kind
                一律归到 ``node.started``（宽容）。
            node_id: 节点 ID（非空才有意义；空串仍允许但等价于 run 级事件）。
            payload: 额外结构化细节（脱敏后；不含 secret/路径）。``node_id`` 会
                自动并入 payload（便于不支持 ``RunEvent.node_id`` 的旧消费方）。

        Returns:
            True 表示事件已发布；False 表示 run 不存在或已 detached（no-op）。
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.session_detached:
                return False
        event_kind = self._NODE_KIND_MAP.get(kind, RunEventKind.NODE_STARTED)
        merged = dict(payload or {})
        merged.setdefault("node_id", node_id)
        self._emit_node(state, event_kind, node_id=node_id, payload=merged)
        return True

    # ------------------------------------------------------------------ #
    # 工程生命周期
    # ------------------------------------------------------------------ #

    def detach_session(self, session_id: str) -> int:
        """工程关闭：把该 session 下所有活跃 run 标 detached（plan §1.4 红线）。

        Returns:
            被 detach 的活跃 run 数。
        """
        n = 0
        with self._lock:
            self._detached_sessions.add(session_id)
            for state in self._runs.values():
                sid = state.session_id or state.invocation.parent_session_id
                if sid == session_id:
                    if not state.session_detached:
                        state.session_detached = True
                        if RunStatus.is_active(state.status):
                            n += 1
        _LOG.info(
            "detached session %s: %d active runs frozen", session_id, n,
        )
        return n

    def is_session_detached(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._detached_sessions

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _set_status_unlocked(self, state: _RunState, status: str) -> None:
        # 走 transition 校验（非法抛 ValueError）；调用方已在锁内。
        RunStatus.transition(state.status, status)
        state.status = status

    def _emit(self, state: _RunState, kind: str, *, payload: dict[str, Any]) -> None:
        event = RunEvent(
            run_id=state.run_id,
            kind=kind,
            payload=sanitize_for_storage(payload),
        )
        # AUD-22312：事件流 append 与其他状态写入一样受 RLock 保护（类文档
        # 声明的线程契约）。RLock 可重入：_emit 在持锁路径（如 start/cancel）
        # 与锁外路径都会进入。_dispatch 仍在锁外（慢 subscriber 不阻塞）。
        with self._lock:
            state.events.append(event)
        self._dispatch(event)

    def _emit_by_id(self, run_id: str, kind: str, *, payload: dict[str, Any]) -> None:
        with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            return
        self._emit(state, kind, payload=payload)

    def _emit_node(
        self, state: _RunState, kind: str, *,
        node_id: str, payload: dict[str, Any],
    ) -> None:
        """发布节点级事件（显式填 ``RunEvent.node_id``）。

        与 :meth:`_emit` 的唯一区别：``RunEvent.node_id`` 非空。订阅者据此按节点
        过滤（节点状态色实时刷新）。
        """
        event = RunEvent(
            run_id=state.run_id,
            kind=kind,
            node_id=node_id,
            payload=sanitize_for_storage(payload),
        )
        # AUD-22312：与 _emit 相同——append 受 RLock 保护，_dispatch 锁外。
        with self._lock:
            state.events.append(event)
        self._dispatch(event)

    def _dispatch(self, event: RunEvent) -> None:
        """在 runtime 锁外通知订阅者；subscriber 抛错被吞。"""
        with self._lock:
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — subscriber 不应影响 run
                _LOG.exception("run subscriber raised; ignoring (event=%s)", event.kind)


def _session_of(record: RunRecord, runtime: "HarnessRuntime") -> str:
    with runtime._lock:  # noqa: SLF001 — internal helper
        state = runtime._runs.get(record.run_id)  # noqa: SLF001
        return state.session_id if state else record.parent_session_id


__all__ = ["HarnessRuntime", "RuntimeRestoreReport", "Subscriber"]
