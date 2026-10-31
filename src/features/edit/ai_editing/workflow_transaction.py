"""T-P2-21: WorkflowTransaction —— 跨域事务、显式状态机与失败策略.

卡要求：
1. 冻结 project revision 与 capability snapshot，记录每一步
   输入/输出/undo token/job reference；
2. 所有 write 先产生 proposal/diff，宿主签发 ApprovalGrant（模型不得
   自签——由 ToolFacade/TransactionService 强制，本模块只承载记录）；
3. 长任务统一状态机 ``QUEUED → PREFLIGHT → RUNNING → VERIFYING →
   SUCCEEDED|DEGRADED|FAILED|CANCELLED``；每次状态转移携带进度、阶段、
   日志引用、可取消性与补偿建议；
4. 失败策略只允许显式三种：``retry_safe`` / ``rollback`` /
   ``pause_for_user``；
5. 统一入口：ToolFacade / MCP / CLI / chat driver 都经同一 transaction
   service —— 本模块即该 service 的工作流层记录与守卫，任何绕开都是
   架构缺陷（由各入口的 audit/replay 契约负责拦截）。

设计要点：
* 不可变锚点：open() 时冻结 (project_id, timeline_id, base_revision,
  capability_snapshot)；转移历史 append-only；
* 终态不可再转移；cancel 仅在可取消状态允许（cancellable per state）；
* 补偿建议是失败策略的**显式函数**（不允许第四种隐式策略）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import copy


class WorkflowState(str, Enum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailurePolicy(str, Enum):
    """卡步骤 4：只允许显式三种。"""

    RETRY_SAFE = "retry_safe"
    ROLLBACK = "rollback"
    PAUSE_FOR_USER = "pause_for_user"


#: 失败策略全集的冻结快照——新增第四种策略即破坏卡点契约（测试强制）。
FAILURE_POLICIES_EXACT = frozenset({
    FailurePolicy.RETRY_SAFE, FailurePolicy.ROLLBACK,
    FailurePolicy.PAUSE_FOR_USER,
})


#: 合法转移表（终态无出边）。
LEGAL_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.QUEUED: frozenset({WorkflowState.PREFLIGHT,
                                     WorkflowState.CANCELLED}),
    WorkflowState.PREFLIGHT: frozenset({WorkflowState.RUNNING,
                                        WorkflowState.FAILED,
                                        WorkflowState.CANCELLED}),
    WorkflowState.RUNNING: frozenset({WorkflowState.VERIFYING,
                                      WorkflowState.FAILED,
                                      WorkflowState.CANCELLED}),
    WorkflowState.VERIFYING: frozenset({WorkflowState.SUCCEEDED,
                                        WorkflowState.DEGRADED,
                                        WorkflowState.FAILED,
                                        WorkflowState.CANCELLED}),
    # 终态：空出边
    WorkflowState.SUCCEEDED: frozenset(),
    WorkflowState.DEGRADED: frozenset(),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({WorkflowState.SUCCEEDED, WorkflowState.DEGRADED,
                             WorkflowState.FAILED, WorkflowState.CANCELLED})

#: 各状态可取消性（卡：每次状态带可取消性元数据）。
CANCELLABLE_STATES = frozenset({WorkflowState.QUEUED, WorkflowState.PREFLIGHT,
                                WorkflowState.RUNNING, WorkflowState.VERIFYING})

#: 终态也是"离开运行"的状态——全部不可再取消。
assert not (TERMINAL_STATES & CANCELLABLE_STATES)


class IllegalTransitionError(RuntimeError):
    """非法状态转移（含终态再转移）。"""


class WorkflowTransaction:
    """一个跨域工作流事务：冻结锚点 + append-only 事件/步骤历史。"""

    def __init__(
        self,
        *,
        workflow_id: str,
        project_id: str,
        timeline_id: str,
        base_revision: int,
        capability_snapshot: dict[str, Any],
        failure_policy: FailurePolicy,
        created_by: str = "host",
    ) -> None:
        if not isinstance(failure_policy, FailurePolicy):
            raise TypeError("failure_policy must be a FailurePolicy")
        self._workflow_id = workflow_id
        # 不可变锚点（卡步骤 1）：open 时冻结——深拷贝快照，调用方
        # 事后的嵌套修改不得穿透（审计回放要求锚点逐字节可信）。
        self._anchor = {
            "project_id": project_id,
            "timeline_id": timeline_id,
            "base_revision": base_revision,
            "capability_snapshot": copy.deepcopy(capability_snapshot or {}),
        }
        self._failure_policy = failure_policy
        self._created_by = created_by
        self._state = WorkflowState.QUEUED
        self._events: list[WorkflowEvent] = []
        self._steps: list[WorkflowStep] = []

    # ── 只读锚点/状态 ──
    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def anchor(self) -> dict[str, Any]:
        return copy.deepcopy(self._anchor)

    @property
    def failure_policy(self) -> FailurePolicy:
        return self._failure_policy

    @property
    def state(self) -> WorkflowState:
        return self._state

    @property
    def cancellable(self) -> bool:
        return self._state in CANCELLABLE_STATES

    @property
    def events(self) -> tuple["WorkflowEvent", ...]:
        return tuple(self._events)

    @property
    def steps(self) -> tuple["WorkflowStep", ...]:
        return tuple(self._steps)

    # ── 状态机 ──
    def transition(
        self, to_state: WorkflowState, *, progress: float = 0.0,
        stage: str = "", log_ref: str = "", reason: str = "",
    ) -> "WorkflowEvent":
        """合法转移；终态再转移/越边抛 IllegalTransitionError。"""
        if not isinstance(to_state, WorkflowState):
            raise TypeError("to_state must be a WorkflowState")
        if self._state in TERMINAL_STATES:
            raise IllegalTransitionError(
                f"terminal state {self._state.value} cannot transition")
        if to_state not in LEGAL_TRANSITIONS[self._state]:
            raise IllegalTransitionError(
                f"illegal transition {self._state.value} → {to_state.value}")
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be within [0, 1]")
        event = WorkflowEvent(
            seq=len(self._events),
            from_state=self._state, to_state=to_state,
            progress=float(progress), stage=stage, log_ref=log_ref,
            reason=reason,
            cancellable_after=to_state in CANCELLABLE_STATES,
            compensation=self.compensation_advice(to_state),
        )
        self._state = to_state
        self._events.append(event)
        return event

    def cancel(self, *, reason: str = "user requested") -> "WorkflowEvent":
        """仅在可取消状态允许；终态/非法路径抛错。"""
        if not self.cancellable:
            raise IllegalTransitionError(
                f"state {self._state.value} is not cancellable")
        return self.transition(WorkflowState.CANCELLED, reason=reason)

    # ── 步骤记录（卡：每步输入/输出/undo token/job ref）──
    def add_step(
        self, name: str, *, operation_id: str = "",
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        undo_token: str = "", job_ref: str = "",
    ) -> "WorkflowStep":
        """记录一个已执行步骤；运行态之外记录是契约违规。"""
        if self._state is not WorkflowState.RUNNING:
            raise IllegalTransitionError(
                f"steps can only be recorded while RUNNING "
                f"(state={self._state.value})")
        step = WorkflowStep(
            index=len(self._steps), name=name,
            operation_id=operation_id,
            inputs=dict(inputs or {}), outputs=dict(outputs or {}),
            undo_token=undo_token, job_ref=job_ref,
        )
        self._steps.append(step)
        return step

    # ── 失败策略 → 补偿建议（显式函数，不允许第四种策略）──
    def compensation_advice(self, state: WorkflowState) -> str:
        if state is not WorkflowState.FAILED:
            return ""
        if self._failure_policy is FailurePolicy.RETRY_SAFE:
            return ("retry_safe: 操作幂等，可在 preconditions 复核后重新入队；"
                    "无需回滚已记录步骤。")
        if self._failure_policy is FailurePolicy.ROLLBACK:
            undo_tokens = [s.undo_token for s in self._steps if s.undo_token]
            if undo_tokens:
                return ("rollback: 按记录的 undo tokens 逆序回滚："
                        + " ← ".join(reversed(undo_tokens)))
            return "rollback: 无已提交步骤，无需回滚。"
        return ("pause_for_user: 暂停并等待用户裁决；不可逆操作"
                "（export/network/delete）需要特定授权与输出范围确认。")

    # ── 快照（审计/replay 消费同一形状）──
    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self._workflow_id,
            "anchor": self.anchor,
            "failure_policy": self._failure_policy.value,
            "created_by": self._created_by,
            "state": self._state.value,
            "cancellable": self.cancellable,
            "events": [e.to_dict() for e in self._events],
            "steps": [s.to_dict() for s in self._steps],
        }


@dataclass(frozen=True)
class WorkflowEvent:
    """一次状态转移：携带进度/阶段/日志引用/可取消性/补偿建议。"""

    seq: int
    from_state: WorkflowState
    to_state: WorkflowState
    progress: float
    stage: str
    log_ref: str
    reason: str
    cancellable_after: bool
    compensation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "progress": self.progress,
            "stage": self.stage,
            "log_ref": self.log_ref,
            "reason": self.reason,
            "cancellable_after": self.cancellable_after,
            "compensation": self.compensation,
        }


@dataclass(frozen=True)
class WorkflowStep:
    """卡步骤 1：每一步的输入/输出/undo token/job reference。"""

    index: int
    name: str
    operation_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    undo_token: str = ""
    job_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "name": self.name,
            "operation_id": self.operation_id,
            "inputs": self.inputs, "outputs": self.outputs,
            "undo_token": self.undo_token, "job_ref": self.job_ref,
        }
