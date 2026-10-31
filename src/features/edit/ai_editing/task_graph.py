"""TAEP M9 — TaskGraph：受控子智能体的有界角色任务图（§6 / §10.1 / §11.1 M9 step 3-4）。

本模块实现固定角色的 ``TaskGraph``：父任务分解为若干有界子任务，每个子任务有明确
角色、最小上下文、预算/超时、artifact 类型、取消传播与失败策略（§6.2）。

红线（§6 / §11.1 M9 / .clinerules）：

- **子智能体是只读 worker，不是第二个用户**（§6.1）：子任务只能产出 typed artifact
  （``task_artifacts.SubagentArtifact``），**不能**直接写 timeline / 调 commit /
  拿独立 approval。
- **单写者 policy**（§6.2 规则 2 / §11.1 M9 step 4）：同一 timeline 同时至多一个提交
  候选；``SingleWriterPolicy`` 强制此约束，子智能体永远没有独立 approval。
- **最小上下文**（§6.2 规则 3）：子任务的 ``ContextSlice`` 只含完成其角色所需的最小
  数据（不继承父任务的所有工具或密钥）。
- **取消传播**（§6.2 规则 6）：取消父任务 → 取消未开始的子任务 + 请求安全取消进行中
  的子任务；已成功的 artifact 可保留。
- **失败可见**（§6.2 规则 5）：子任务失败必须报告"未完成什么 + 影响"，不得编造完成。
- **预算/超时/循环检测**（§6.2 规则 7）：每个 task 有 budget/timeout/max_tool_calls；
  超限进入 ``NEEDS_USER_INPUT`` 或 ``FAILED``，不无限自我反思。

首版设计（§6.3："首版以确定角色和固定图实现"）：

- 4 个固定角色（``TaskRole``）：``creative_director`` / ``media_librarian`` /
  ``story_editor`` / ``qa_reviewer``（§6.1 全量角色留后续）。
- ``TaskGraph`` 是**声明式 DAG**（``TaskNode`` + ``depends_on``），由 SkillManifest
  的 ``roles`` 派生或手动构造。``TaskGraphExecutor`` 按拓扑序执行，收集 typed artifact。
- **确定性 stub**（v1 决策）：``RoleExecutor`` 协议 + ``DeterministicRoleExecutor``
  产出固定 artifact（不接 LLM）；真实 LLM 角色留 M10。

本模块**不**：自己写 timeline / 调 VariantPlanner（那是 SkillRuntime 调 WorkflowRunner
的事）/ 接 approval / 持久化（artifact 由 SkillRuntime 收集后交给宿主）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.task_artifacts import SubagentArtifact


# ---------------------------------------------------------------------- #
# 固定角色（§6.1）
# ---------------------------------------------------------------------- #


class TaskRole(str, Enum):
    """受控子智能体的固定角色（§6.1）。

    v1 实现 M9 step 3 指名的 4 个：创意 / 素材检索 / 故事 / QA。
    其余角色（researcher / rough_cut_planner / caption_editor / audio_director /
    visual_style_advisor / help_guide）留后续。
    """

    CREATIVE_DIRECTOR = "creative_director"   # Brief/受众/hook
    MEDIA_LIBRARIAN = "media_librarian"        # 素材检索/引用/缺口/授权
    STORY_EDITOR = "story_editor"              # 大纲/叙事/分镜建议
    QA_REVIEWER = "qa_reviewer"                # 约束/版权/时长/字幕/安全区/能力

    @property
    def produces_kind(self) -> str:
        """该角色产出的 artifact 类型标识（typed artifact contract）。"""
        return _ROLE_ARTIFACT_KIND[self]


_ROLE_ARTIFACT_KIND: dict[TaskRole, str] = {
    TaskRole.CREATIVE_DIRECTOR: "script_draft",
    TaskRole.MEDIA_LIBRARIAN: "evidence_set",
    TaskRole.STORY_EDITOR: "story_outline",
    TaskRole.QA_REVIEWER: "qa_report",
}


# ---------------------------------------------------------------------- #
# Task 状态
# ---------------------------------------------------------------------- #


class TaskState(str, Enum):
    """子任务状态机（§6.2 规则 7）。"""

    PENDING = "pending"            # 已声明，未开始
    RUNNING = "running"            # 执行中
    SUCCEEDED = "succeeded"        # 完成，产 artifact
    FAILED = "failed"              # 失败（error_reason 非空）
    CANCELLED = "cancelled"        # 被父任务取消（cancel_reason 非空）
    NEEDS_USER_INPUT = "needs_user_input"  # 超预算/超时，需用户介入（§6.2 规则 7）

    @property
    def terminal(self) -> bool:
        return self in (TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.CANCELLED, TaskState.NEEDS_USER_INPUT)


# ---------------------------------------------------------------------- #
# 预算 / 超时 / 资源限额（§6.2 规则 7）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskBudget:
    """子任务的资源限额（§6.2 规则 7：超时/token/成本/最大工具调用数）。

    v1 用 ``max_tool_calls`` + ``timeout_ms`` 做硬上限；``max_tokens`` / ``max_cost``
    字段预留（真实 LLM 计费留 M10）。``None`` 表示不限（仅官方内置角色允许）。
    """

    max_tool_calls: int | None = 8        # 单子任务最大工具调用数
    timeout_ms: int | None = 30_000       # 单子任务超时（毫秒）
    max_tokens: int | None = None         # token 上限（v1 预留）
    max_cost: float | None = None         # 成本上限（v1 预留）

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "timeout_ms": self.timeout_ms,
            "max_tokens": self.max_tokens,
            "max_cost": self.max_cost,
        }


# ---------------------------------------------------------------------- #
# ContextSlice —— 最小上下文（§6.2 规则 3）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContextSlice:
    """子任务的最小上下文（§6.2 规则 3："不继承父任务的所有工具或密钥"）。

    只含完成角色所需的数据：``asset_ids``（作用域）/ ``evidence_refs``（已收集证据）/
    ``brief_summary``（brief 摘要）/ ``upstream_artifacts``（依赖子任务的产物 id）。
    **不含**：完整 brief / 全部 timeline / API key / 其他角色的上下文。
    """

    asset_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()     # SemanticSegment.segment_id（已就绪证据）
    brief_summary: str = ""                  # brief 摘要（非全文）
    upstream_artifact_ids: tuple[str, ...] = ()  # 依赖子任务的 artifact id
    role_hint: str = ""                      # 角色特定的提示（如受众/平台）

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_ids": list(self.asset_ids),
            "evidence_refs": list(self.evidence_refs),
            "brief_summary": self.brief_summary,
            "upstream_artifact_ids": list(self.upstream_artifact_ids),
            "role_hint": self.role_hint,
        }


# ---------------------------------------------------------------------- #
# TaskNode —— 单个子任务
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskNode:
    """TaskGraph 的单个节点（§6.2 / §10.1 "Task"）。

    ``depends_on`` 声明前置依赖（DAG）；``expected_artifact_kind`` 是 typed artifact
    contract（执行后产物必须匹配，否则 FAILED）。
    """

    task_id: str
    role: TaskRole
    context: ContextSlice = field(default_factory=lambda: ContextSlice())
    depends_on: tuple[str, ...] = ()         # 前置 task_id（DAG 边）
    budget: TaskBudget = field(default_factory=lambda: TaskBudget())
    expected_artifact_kind: str = ""         # = role.produces_kind（默认）
    can_write_timeline: bool = False         # 红线：子任务永远不能写（单写者 policy）

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        # 默认 expected_artifact_kind = role 产物类型
        if not self.expected_artifact_kind:
            object.__setattr__(self, "expected_artifact_kind", self.role.produces_kind)
        # 红线校验：子任务 can_write_timeline 必须为 False（单写者）
        # （构造时允许 True 仅供测试反例；SingleWriterPolicy 会在执行期拒绝）
        # depends_on 不含自身（自环）
        if self.task_id in self.depends_on:
            raise ValueError(f"task {self.task_id} cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role.value,
            "context": self.context.to_dict(),
            "depends_on": list(self.depends_on),
            "budget": self.budget.to_dict(),
            "expected_artifact_kind": self.expected_artifact_kind,
            "can_write_timeline": self.can_write_timeline,
        }


# ---------------------------------------------------------------------- #
# TaskGraph —— 声明式 DAG
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskGraph:
    """受控子智能体的任务图（§6.2 规则 1 / §6.3）。

    由 ``TaskNode`` 集合 + 依赖边组成。``validate()`` 检查：无环 / 无悬空依赖 /
    角色合法 / 单写者。``topo_order()`` 返回拓扑序（执行顺序）。
    """

    skill_id: str
    nodes: tuple[TaskNode, ...] = ()
    timeline_id: str = ""                     # 单写者作用域 key

    def __post_init__(self) -> None:
        if not self.skill_id:
            raise ValueError("skill_id cannot be empty")
        # task_id 唯一
        ids = [n.task_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate task_id in graph: {ids}")

    # ---------------- 校验 ---------------- #

    def validate(self) -> list[str]:
        """返回错误列表（空 = 合法）。检查：悬空依赖 / 环 / 角色合法。"""
        errors: list[str] = []
        node_map = {n.task_id: n for n in self.nodes}

        # 悬空依赖
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in node_map:
                    errors.append(f"task {n.task_id} depends on unknown {dep}")

        # 环检测（DFS）
        if not errors:  # 悬空依赖会干扰环检测，先确认无悬空
            cycle = self._detect_cycle()
            if cycle:
                errors.append(f"cycle detected: {' -> '.join(cycle)}")

        return errors

    def _detect_cycle(self) -> list[str] | None:
        """DFS 环检测，返回环路径或 None。"""
        node_map = {n.task_id: n for n in self.nodes}
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {tid: WHITE for tid in node_map}
        stack: list[str] = []

        def dfs(tid: str) -> list[str] | None:
            color[tid] = GRAY
            stack.append(tid)
            for dep in node_map[tid].depends_on:
                if color.get(dep) == GRAY:
                    # 找到环：从 dep 在 stack 中的位置到末尾
                    idx = stack.index(dep)
                    return stack[idx:] + [dep]
                if color.get(dep) == WHITE:
                    found = dfs(dep)
                    if found:
                        return found
            stack.pop()
            color[tid] = BLACK
            return None

        for tid in node_map:
            if color[tid] == WHITE:
                found = dfs(tid)
                if found:
                    return found
        return None

    # ---------------- 拓扑序 ---------------- #

    def topo_order(self) -> list[TaskNode]:
        """返回拓扑序节点列表（依赖在前）。有环时抛 ValueError。"""
        if self.validate():
            raise ValueError(f"invalid graph: {self.validate()}")
        node_map = {n.task_id: n for n in self.nodes}
        visited: set[str] = set()
        order: list[TaskNode] = []

        def visit(tid: str) -> None:
            if tid in visited:
                return
            visited.add(tid)
            for dep in node_map[tid].depends_on:
                visit(dep)
            order.append(node_map[tid])

        for n in self.nodes:
            visit(n.task_id)
        return order

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "timeline_id": self.timeline_id,
            "nodes": [n.to_dict() for n in self.nodes],
        }


# ---------------------------------------------------------------------- #
# SingleWriterPolicy —— 同一 timeline 至多一个提交候选（§6.2 规则 2 / §11.1 M9）
# ---------------------------------------------------------------------- #


class SingleWriterPolicy:
    """单写者 policy（§6.2 规则 2 / §11.1 M9 step 4："同一 timeline 同时最多一个
    提交候选，子智能体永远没有独立 approval"）。

    作用域 key = ``timeline_id``。同一 timeline 同时至多一个 active graph（持有写候选）；
    子智能体（TaskNode）的 ``can_write_timeline`` 即使被误设为 True，执行期也拒绝。

    只读检索/分析可并行（多 graph 可同时活跃），但写候选互斥。
    """

    def __init__(self) -> None:
        # timeline_id -> skill_id（当前持有写候选的 graph）
        self._writers: dict[str, str] = {}
        # AUD-14511：acquire 是无锁 check-then-set（TOCTOU）——本类 docstring 设想
        # "多 graph 可同时活跃"，两个线程可同时通过检查后各自写入而双双获得写候选。
        # 一把 Lock 把读-改-写收成原子操作（成本可忽略）。
        self._lock = threading.Lock()

    def acquire(self, timeline_id: str, skill_id: str) -> bool:
        """尝试为 graph 获取 timeline 的写候选权。成功返回 True。

        同一 timeline 已被**别的** skill 持有时拒绝（返回 False）；同一 skill 重入允许。
        """
        if not timeline_id:
            return True  # 无 timeline 作用域（纯分析 graph）不占写候选
        with self._lock:
            current = self._writers.get(timeline_id)
            if current is not None and current != skill_id:
                return False
            self._writers[timeline_id] = skill_id
            return True

    def release(self, timeline_id: str, skill_id: str) -> None:
        """释放写候选权（仅当持有者是该 skill 时）。"""
        if not timeline_id:
            return
        with self._lock:
            if self._writers.get(timeline_id) == skill_id:
                del self._writers[timeline_id]

    def holder(self, timeline_id: str) -> str | None:
        """当前持有 timeline 写候选的 skill_id（None = 空闲）。"""
        with self._lock:
            return self._writers.get(timeline_id)

    def assert_subagent_no_write(self, node: TaskNode) -> None:
        """红线断言：子任务永远不能写 timeline（§6.1 / §6.2 规则 4）。

        ``can_write_timeline=True`` 的子任务在执行期被拒（抛 ``PermissionError``）。
        """
        if node.can_write_timeline:
            raise PermissionError(
                f"subagent task {node.task_id} ({node.role.value}) cannot write timeline: "
                "subagents never have independent approval (§6.2 rule 4)"
            )


# ---------------------------------------------------------------------- #
# RoleExecutor Protocol + 确定性 stub（v1）
# ---------------------------------------------------------------------- #


class RoleExecutor(Protocol):
    """角色执行器协议（注入点）。

    v1：``DeterministicRoleExecutor``（确定性 stub，不接 LLM）。
    M10+：``LlmRoleExecutor``（真实 LLM 角色注入）。
    """

    def execute(
        self, role: TaskRole, context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
    ) -> SubagentArtifact: ...


# 子任务执行结果（artifact + 状态）。
@dataclass(frozen=True)
class TaskResult:
    """单个子任务的执行结果。"""

    task_id: str
    role: TaskRole
    state: TaskState
    artifact: SubagentArtifact | None = None
    error_reason: str = ""
    cancel_reason: str = ""
    execution_source: str = ""       # llm / deterministic_fallback / custom
    degradation_reason: str = ""     # fallback or other visible degradation

    @property
    def succeeded(self) -> bool:
        return self.state == TaskState.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "role": self.role.value,
            "state": self.state.value,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "error_reason": self.error_reason, "cancel_reason": self.cancel_reason,
            "execution_source": self.execution_source,
            "degradation_reason": self.degradation_reason,
        }


# ---------------------------------------------------------------------- #
# TaskGraphExecutor —— 按拓扑序执行 + 取消传播 + 单写者
# ---------------------------------------------------------------------- #


@dataclass
class TaskGraphExecutor:
    """TaskGraph 执行器（§6.2 调度规则）。

    按拓扑序执行节点，收集 typed artifact；实现取消传播、单写者 policy、失败可见。
    **不**自己写 timeline / 调 approval（artifact 由调用方收集后交给宿主闭环）。
    """

    role_executor: RoleExecutor
    policy: SingleWriterPolicy = field(default_factory=lambda: SingleWriterPolicy())

    def execute(
        self, graph: TaskGraph, *, cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[TaskResult, ...]:
        """执行整个 graph。返回每个节点的 ``TaskResult``（拓扑序）。

        - 取消传播（§6.2 规则 6）：``cancel_requested`` 返回 True 时，未开始的节点
          标 CANCELLED，进行中的不再派发。
        - 失败可见（§6.2 规则 5）：失败节点标 FAILED + error_reason，依赖它的节点
          标 CANCELLED（cancel_reason 注明上游失败）。
        - 单写者（§6.2 规则 4）：每个节点经 ``policy.assert_subagent_no_write``。
        """
        # 1. graph 合法性
        errors = graph.validate()
        if errors:
            # graph 非法：全部节点直接 FAILED（不执行）
            return tuple(
                TaskResult(
                    task_id=n.task_id, role=n.role, state=TaskState.FAILED,
                    error_reason=f"invalid graph: {errors[0]}",
                ) for n in graph.nodes
            )

        order = graph.topo_order()
        results: dict[str, TaskResult] = {}
        # upstream artifacts 按依赖收集（供下游 context）
        artifact_by_task: dict[str, SubagentArtifact] = {}

        for node in order:
            # 取消传播：父任务取消 → 未开始的子任务标 CANCELLED
            if cancel_requested is not None and cancel_requested():
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role, state=TaskState.CANCELLED,
                    cancel_reason="parent cancelled before start",
                )
                continue

            # 上游有 FAILED / CANCELLED → 本节点不执行（标 CANCELLED，注明原因）
            upstream_failed = [
                d for d in node.depends_on
                if results.get(d) is not None and results[d].state in (
                    TaskState.FAILED, TaskState.CANCELLED, TaskState.NEEDS_USER_INPUT,
                )
            ]
            if upstream_failed:
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role, state=TaskState.CANCELLED,
                    cancel_reason=f"upstream not succeeded: {upstream_failed}",
                )
                continue

            # 单写者红线：子任务永远不能写
            try:
                self.policy.assert_subagent_no_write(node)
            except PermissionError as exc:
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role, state=TaskState.FAILED,
                    error_reason=str(exc),
                )
                continue

            # deadline 是任务图的硬边界。已经过期的节点不派发；执行器返回后还会
            # 再检查一次，避免把超时产物当成成功交给宿主。
            if node.budget.timeout_ms is not None and node.budget.timeout_ms <= 0:
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role,
                    state=TaskState.NEEDS_USER_INPUT,
                    error_reason="task deadline exceeded before start",
                )
                continue

            # 组装上游 artifact（供 role_executor）
            upstream_artifacts = tuple(
                artifact_by_task[d] for d in node.depends_on
                if d in artifact_by_task
            )

            # 执行
            started_at = time.monotonic()
            try:
                execute_with_budget = getattr(self.role_executor, "execute_with_budget", None)
                if callable(execute_with_budget):
                    artifact = execute_with_budget(
                        node.role, node.context, upstream_artifacts, node.budget,
                    )
                else:
                    artifact = self.role_executor.execute(node.role, node.context, upstream_artifacts)
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                usage = getattr(self.role_executor, "last_usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                budget_reason = _budget_violation(node.budget, usage, elapsed_ms)
                if budget_reason:
                    results[node.task_id] = TaskResult(
                        task_id=node.task_id, role=node.role,
                        state=TaskState.NEEDS_USER_INPUT,
                        error_reason=budget_reason,
                    )
                    continue
                # typed artifact contract 校验：产物 kind 必须匹配 role
                kind_ok = _artifact_kind_matches(node.role, artifact)
                if not kind_ok:
                    results[node.task_id] = TaskResult(
                        task_id=node.task_id, role=node.role, state=TaskState.FAILED,
                        error_reason=(
                            f"artifact kind mismatch: expected {node.expected_artifact_kind}, "
                            f"got {type(artifact).__name__}"
                        ),
                    )
                    continue
                artifact_by_task[node.task_id] = artifact
                execution_source = str(
                    getattr(self.role_executor, "last_execution_source", "")
                )
                degradation_reason = str(
                    getattr(self.role_executor, "last_degradation_reason", "")
                )
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role, state=TaskState.SUCCEEDED,
                    artifact=artifact, execution_source=execution_source,
                    degradation_reason=degradation_reason,
                )
            except Exception as exc:  # noqa: BLE001 — 子任务失败必须可见，不能吞
                results[node.task_id] = TaskResult(
                    task_id=node.task_id, role=node.role, state=TaskState.FAILED,
                    error_reason=f"{type(exc).__name__}: {exc}",
                )

        return tuple(results[n.task_id] for n in order)


def _artifact_kind_matches(role: TaskRole, artifact: Any) -> bool:
    """typed artifact contract：role 产物类型必须匹配（§6.2 规则 4）。"""
    from src.features.edit.ai_editing.task_artifacts import (
        EvidenceSet, QaReport, ScriptDraft, StoryOutline,
    )
    _EXPECTED = {
        TaskRole.CREATIVE_DIRECTOR: ScriptDraft,
        TaskRole.MEDIA_LIBRARIAN: EvidenceSet,
        TaskRole.STORY_EDITOR: StoryOutline,
        TaskRole.QA_REVIEWER: QaReport,
    }
    expected_type = _EXPECTED.get(role)
    return expected_type is not None and isinstance(artifact, expected_type)


def _budget_violation(budget: TaskBudget, usage: dict[str, Any], elapsed_ms: int) -> str:
    """把 executor 报告的 usage 与节点预算收口为可见的用户介入状态。"""

    if budget.timeout_ms is not None and elapsed_ms > budget.timeout_ms:
        return f"task deadline exceeded: {elapsed_ms}ms > {budget.timeout_ms}ms"
    checks = (
        ("tokens", budget.max_tokens, "max_tokens"),
        ("cost_units", budget.max_cost, "max_cost"),
        ("tool_calls", budget.max_tool_calls, "max_tool_calls"),
    )
    for usage_key, limit, label in checks:
        if limit is None or usage_key not in usage:
            continue
        try:
            actual = float(usage[usage_key])
            if actual > float(limit):
                return f"task budget exceeded: {label}={actual:g} > {limit:g}"
        except (TypeError, ValueError):
            return f"invalid executor usage for {usage_key}: {usage[usage_key]!r}"
    return ""


__all__ = [
    "TaskRole",
    "TaskState",
    "TaskBudget",
    "ContextSlice",
    "TaskNode",
    "TaskGraph",
    "SingleWriterPolicy",
    "RoleExecutor",
    "TaskResult",
    "TaskGraphExecutor",
]
