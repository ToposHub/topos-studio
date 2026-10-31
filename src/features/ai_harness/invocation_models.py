"""AI Harness 调用模型（plan §0.4 / §10.2 P0.1）。

定义统一的领域对象，取代 chat / workflow / developer 三套入口各自的散参数。
所有后续代码（runtime、scheduler_adapter、legacy_adapters、UI drawer、节点图
编译器）必须复用本模块的类型，禁止再造平行模型。

红线对照（plan §1.4 / §10.2 P0.1）：

- **不存 secret / API key / 绝对路径 / 未授权 prompt 全文。** ``sanitize_for_storage``
  在数据层强制脱敏；任何持久化前必须经过它。
- **source 必须显式。** ``chat`` / ``workflow`` / ``developer`` 三选一，方便审计
  与权限分流。
- **scope 必须可验证。** ``InvocationScope`` 把"分析哪些素材 / 写哪条时间线 /
  是否允许上传"显式化，避免"分析所有素材"悄悄变成扫描全库（plan §0.3 第 6 条）。
- **权限分级，不可一键越过。** ``PermissionLevel`` 是明确枚举；云上传、生成付费、
  模型下载仍是独立 consent，不能被 ``apply_with_each_approval`` 越权（plan §10.2
  P0.3）。
- **状态迁移受控。** ``RunStatus.transition`` 强制合法迁移路径，禁止从 ``SUCCEEDED``
  回到 ``RUNNING`` 这类非法翻转。
- **run_id 唯一关联。** TaskGraph、Job、artifact、审批、审计都以同一 ``run_id``
  串联；``RunRecord`` 是它们的聚合只读视图。

本模块纯逻辑、无 I/O、无 Qt / OpenGL 依赖；可被测试 / 节点编译器 / UI 共享。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal


# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #


InvocationSourceLiteral = Literal["chat", "workflow", "developer"]
PermissionLevelLiteral = Literal[
    "read_only", "propose", "apply_with_each_approval",
]


class InvocationSource:
    """发起入口的三类来源（plan §0.2 映射表）。

    用 ``str`` 常量 + 校验函数而非 ``Enum``，是为了让 ``source`` 字段可被
    JSON 序列化为原生 ``"chat"`` 字符串（便于落盘 / 审计 / 跨进程），同时仍
    在数据层拒绝未知值。
    """

    CHAT: InvocationSourceLiteral = "chat"
    WORKFLOW: InvocationSourceLiteral = "workflow"
    DEVELOPER: InvocationSourceLiteral = "developer"

    ALL: tuple[str, ...] = (CHAT, WORKFLOW, DEVELOPER)

    @staticmethod
    def is_valid(value: str) -> bool:
        return value in InvocationSource.ALL


class PermissionLevel:
    """权限等级（plan §10.2 P0.3）。

    - ``read_only``：仅读取 / 分析，不产编辑提议、不写。
    - ``propose``：可产 ``EditPlan`` / 字幕草稿等提议，但不写时间线。
    - ``apply_with_each_approval``：每次写入前都要一次性 ``ApprovalGrant``；
      **不存在**"完全访问"等级——云上传 / 生成付费 / 模型下载仍是独立 consent。

    顺序：``read_only < propose < apply_with_each_approval``，``at_least``
    用于校验"当前等级是否达到要求"。
    """

    READ_ONLY: PermissionLevelLiteral = "read_only"
    PROPOSE: PermissionLevelLiteral = "propose"
    APPLY_WITH_EACH_APPROVAL: PermissionLevelLiteral = "apply_with_each_approval"

    ALL: tuple[str, ...] = (READ_ONLY, PROPOSE, APPLY_WITH_EACH_APPROVAL)
    _ORDER: dict[str, int] = {
        READ_ONLY: 0,
        PROPOSE: 1,
        APPLY_WITH_EACH_APPROVAL: 2,
    }

    @staticmethod
    def is_valid(value: str) -> bool:
        return value in PermissionLevel.ALL

    @staticmethod
    def at_least(have: str, required: str) -> bool:
        """``have`` 是否达到 ``required`` 等级。未知值视为最低。"""
        return PermissionLevel._ORDER.get(have, -1) >= PermissionLevel._ORDER.get(
            required, 0
        )


class RunStatus:
    """``WorkflowRun`` 状态机（plan §6.1）。

    合法迁移路径由 :meth:`transition` 强制；非法迁移抛 ``ValueError``，避免
    UI / runtime 出现"SUCCEEDED 又回 RUNNING"这类不可解释状态。

    终态：``SUCCEEDED`` / ``SUCCEEDED_WITH_WARNINGS`` / ``FAILED`` /
    ``CANCELLED`` / ``SKIPPED``。
    """

    DRAFT = "draft"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    ACTIVE_STATES: frozenset[str] = frozenset(
        {QUEUED, RUNNING, WAITING_FOR_USER, VALIDATING}
    )
    TERMINAL_STATES: frozenset[str] = frozenset(
        {
            SUCCEEDED,
            SUCCEEDED_WITH_WARNINGS,
            FAILED,
            CANCELLED,
            SKIPPED,
        }
    )

    # 合法迁移：from -> {to, ...}
    _TRANSITIONS: dict[str, frozenset[str]] = {
        DRAFT: frozenset({VALIDATING, QUEUED, CANCELLED, SKIPPED}),
        VALIDATING: frozenset({QUEUED, FAILED, CANCELLED}),
        QUEUED: frozenset({RUNNING, CANCELLED, FAILED, SKIPPED}),
        RUNNING: frozenset(
            {WAITING_FOR_USER, SUCCEEDED, SUCCEEDED_WITH_WARNINGS, FAILED, CANCELLED}
        ),
        WAITING_FOR_USER: frozenset({RUNNING, CANCELLED, FAILED}),
        # 终态：retry 显式走 retry_run，不在此自动迁移
        SUCCEEDED: frozenset(),
        SUCCEEDED_WITH_WARNINGS: frozenset(),
        FAILED: frozenset(),
        CANCELLED: frozenset(),
        SKIPPED: frozenset(),
    }

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in RunStatus.TERMINAL_STATES

    @staticmethod
    def is_active(status: str) -> bool:
        return status in RunStatus.ACTIVE_STATES

    @staticmethod
    def can_transition(from_status: str, to_status: str) -> bool:
        return to_status in RunStatus._TRANSITIONS.get(from_status, frozenset())

    @staticmethod
    def transition(from_status: str, to_status: str) -> str:
        """返回合法的 ``to_status``；非法迁移抛 ``ValueError``。"""
        if not RunStatus.can_transition(from_status, to_status):
            raise ValueError(
                f"illegal run status transition: {from_status!r} -> {to_status!r}"
            )
        return to_status


class RunEventKind:
    """``RunEvent`` 的事件类别（plan §6.1 节点事件 + plan §10.2 P0.6 取消贯通）。

    事件是 UI / 审计 / 重放的唯一事实来源；``HarnessRuntime`` 把每次状态翻转、
    节点进出、artifact 产出、取消、审批都写成事件。
    """

    RUN_CREATED = "run.created"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_HANDOFF_REQUESTED = "run.handoff_requested"
    RUN_RETRY_REQUESTED = "run.retry_requested"
    RUN_RESUMED = "run.resumed"
    NODE_QUEUED = "node.queued"
    NODE_STARTED = "node.started"
    NODE_PROGRESS = "node.progress"
    NODE_WAITING_INPUT = "node.waiting_input"
    NODE_SUCCEEDED = "node.succeeded"
    NODE_FAILED = "node.failed"
    NODE_CANCELLED = "node.cancelled"
    NODE_CACHED = "node.cached"
    NODE_SKIPPED = "node.skipped"
    ARTIFACT_PRODUCED = "artifact.produced"
    JOB_ATTACHED = "job.attached"
    JOB_PROGRESS = "job.progress"
    JOB_FINISHED = "job.finished"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_CONSUMED = "approval.consumed"
    COST_RECORDED = "cost.recorded"
    DIAGNOSTIC = "diagnostic"


# ---------------------------------------------------------------------- #
# Scope / Policy
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvocationScope:
    """一次调用所作用的项目范围（plan §0.3 第 6 条 / §10.2 P0.3）。

    默认只允许"当前选择"，避免"分析所有素材"悄悄扫描全库。``analyze_all_assets``
    必须显式为 ``True``，且在 UI 侧显示素材数量 / 预计耗时后由用户确认写进
    invocation（plan §0.3 第 6 条）。

    Attributes:
        project_id: 工程绑定（跨工程 invocation 非法）。
        timeline_id: 时间线绑定；空表示纯分析、不写时间线。
        selected_asset_ids: 用户当前已选素材 ID（脱敏后的稳定 ID，非路径）。
        analyze_all_assets: 是否允许扫描整个媒体池（默认 False）。
        bin_id: 若仅分析某个 bin，可限定到 bin（空表示不限定）。
        network_allowed: 是否允许本次调用走云（独立于 permission；仍需逐次 consent）。
    """

    project_id: str = ""
    timeline_id: str = ""
    selected_asset_ids: tuple[str, ...] = ()
    analyze_all_assets: bool = False
    bin_id: str = ""
    network_allowed: bool = False
    # Workflow/plan provenance.  These are stable identifiers and revisions,
    # never paths or provider secrets; keeping them in scope makes stale-plan
    # checks and audit records independent of UI/controller state.
    brief_id: str = ""
    brief_revision: int = 0
    base_revision: int | None = None
    analysis_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("InvocationScope.project_id is required")
        if self.analyze_all_assets and self.network_allowed:
            # 全库 + 云同时开启需要更高警觉；数据层先挡一道，UI 再二次确认。
            # 不抛错（合法），但调用方应意识到这是高风险组合。
            pass
        if self.brief_revision < 0:
            raise ValueError("InvocationScope.brief_revision must be >= 0")
        if self.base_revision is not None and self.base_revision < 0:
            raise ValueError("InvocationScope.base_revision must be >= 0")

    @property
    def effective_asset_ids(self) -> tuple[str, ...]:
        """实际生效的素材范围：``analyze_all_assets`` 时返回空（表示全库占位），
        否则返回已选集合。调用方据此决定是否需要二次确认。"""
        if self.analyze_all_assets:
            return ()
        return self.selected_asset_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "selected_asset_ids": list(self.selected_asset_ids),
            "analyze_all_assets": self.analyze_all_assets,
            "bin_id": self.bin_id,
            "network_allowed": self.network_allowed,
            "brief_id": self.brief_id,
            "brief_revision": self.brief_revision,
            "base_revision": self.base_revision,
            "analysis_snapshot_hash": self.analysis_snapshot_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InvocationScope":
        """Restore a scope from a sanitized public snapshot."""
        return cls(
            project_id=str(data.get("project_id", "")),
            timeline_id=str(data.get("timeline_id", "")),
            selected_asset_ids=tuple(str(item) for item in data.get("selected_asset_ids", ())),
            analyze_all_assets=bool(data.get("analyze_all_assets", False)),
            bin_id=str(data.get("bin_id", "")),
            network_allowed=bool(data.get("network_allowed", False)),
            brief_id=str(data.get("brief_id", "")),
            brief_revision=int(data.get("brief_revision", 0) or 0),
            base_revision=(
                int(data["base_revision"])
                if data.get("base_revision") is not None else None
            ),
            analysis_snapshot_hash=str(data.get("analysis_snapshot_hash", "")),
        )


@dataclass(frozen=True)
class RunPolicy:
    """本次运行的权限 / 预算 / 网络策略（plan §10.2 P0.3 / §6.4）。

    独立于 :class:`InvocationScope`：scope 描述"作用在哪"，policy 描述"允许做
    什么、花多少"。两者都不可变；运行中改变 policy 必须新建 invocation。

    Attributes:
        permission: 权限等级（``read_only`` / ``propose`` /
            ``apply_with_each_approval``）。
        profile_id: 模型 profile ID（不含 secret；secret 由 CredentialStore 管）。
        model_id: 具体模型覆盖（空表示用 profile 默认）。
        cost_budget: 预计成本上限（货币单位由 provider 定义；None 表示不限）。
        timeout_ms: 单次运行超时（None 表示不限）。
        timeline_write: 时间线写入策略（``approval_required`` / ``forbidden``）。
        allow_model_download: 是否允许下载模型（独立 consent）。
        allow_generation: 是否允许付费生成（图片 / 视频 / TTS 等；独立 consent）。
        notes: 仅记录用的人类可读说明（脱敏后）。
    """

    permission: str = PermissionLevel.READ_ONLY
    profile_id: str = ""
    model_id: str = ""
    cost_budget: float | None = None
    timeout_ms: int | None = None
    timeline_write: str = "approval_required"
    allow_model_download: bool = False
    allow_generation: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not PermissionLevel.is_valid(self.permission):
            raise ValueError(f"unknown permission level: {self.permission!r}")
        if self.timeline_write not in ("approval_required", "forbidden"):
            raise ValueError(
                f"timeline_write must be 'approval_required' or 'forbidden', "
                f"got {self.timeline_write!r}"
            )

    def can_write_timeline(self) -> bool:
        return (
            PermissionLevel.at_least(self.permission, PermissionLevel.APPLY_WITH_EACH_APPROVAL)
            and self.timeline_write == "approval_required"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "permission": self.permission,
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "cost_budget": self.cost_budget,
            "timeout_ms": self.timeout_ms,
            "timeline_write": self.timeline_write,
            "allow_model_download": self.allow_model_download,
            "allow_generation": self.allow_generation,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunPolicy":
        """Restore policy metadata without accepting secrets or unknown flags."""
        return cls(
            permission=str(data.get("permission", PermissionLevel.READ_ONLY)),
            profile_id=str(data.get("profile_id", "")),
            model_id=str(data.get("model_id", "")),
            cost_budget=(
                float(data["cost_budget"])
                if data.get("cost_budget") is not None else None
            ),
            timeout_ms=(
                int(data["timeout_ms"])
                if data.get("timeout_ms") is not None else None
            ),
            timeline_write=str(data.get("timeline_write", "approval_required")),
            allow_model_download=bool(data.get("allow_model_download", False)),
            allow_generation=bool(data.get("allow_generation", False)),
            notes=str(data.get("notes", "")),
        )


# ---------------------------------------------------------------------- #
# Invocation
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class Invocation:
    """一次用户 / 图 / 开发者发起的请求（plan §0.4）。

    取代 chat / workflow 各自的散参数（``WorkflowInput`` / ``SkillInput``）。
    三类入口都构造本对象后交给 :class:`HarnessRuntime.start`。

    不可变；改 policy / scope / inputs 必须新建 invocation（得到新 run_id）。

    Attributes:
        invocation_id: 稳定唯一 ID（uuid4 hex）。
        source: 入口来源（chat / workflow / developer）。
        skill_id: 目标 Skill / workflow ID（空表示纯文本请求，由 router 解析）。
        scope: 作用范围。
        policy: 权限 / 预算策略。
        inputs: 类型化输入（artifact 引用、文本等；非文件路径 / Qt 对象）。
        parent_session_id: 父会话 ID（用于会话目录关联）。
        user_text: 用户原始输入（仅 source=chat 时非空；脱敏后存）。
        created_at: 创建时间戳（秒）。
    """

    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = InvocationSource.CHAT
    skill_id: str = ""
    scope: InvocationScope = field(default_factory=lambda: InvocationScope(project_id="default"))
    policy: RunPolicy = field(default_factory=RunPolicy)
    inputs: dict[str, Any] = field(default_factory=dict)
    parent_session_id: str = ""
    user_text: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not InvocationSource.is_valid(self.source):
            raise ValueError(f"unknown invocation source: {self.source!r}")
        # workflow / developer 必须指明 skill / workflow；chat 可由 router 后填。
        if self.source in (InvocationSource.WORKFLOW, InvocationSource.DEVELOPER):
            if not self.skill_id:
                raise ValueError(
                    f"invocation source={self.source!r} requires skill_id"
                )

    def with_skill(self, skill_id: str) -> "Invocation":
        """router 解析出 skill 后返回新 invocation（不可变更新）。"""
        return replace(self, skill_id=skill_id)

    def to_public_dict(self) -> dict[str, Any]:
        """对外 / 落盘用的字典（已脱敏）。"""
        return {
            "invocation_id": self.invocation_id,
            "source": self.source,
            "skill_id": self.skill_id,
            "scope": self.scope.to_dict(),
            "policy": self.policy.to_dict(),
            "inputs": sanitize_for_storage(self.inputs),
            "parent_session_id": self.parent_session_id,
            "user_text": sanitize_for_storage(self.user_text),
            "created_at": self.created_at,
        }

    @classmethod
    def from_public_dict(cls, data: dict[str, Any]) -> "Invocation":
        """Restore an invocation from a sanitized cross-process snapshot."""
        scope_data = data.get("scope")
        policy_data = data.get("policy")
        if not isinstance(scope_data, dict) or not isinstance(policy_data, dict):
            raise ValueError("invocation snapshot is missing scope or policy")
        inputs = data.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        return cls(
            invocation_id=str(data.get("invocation_id", "")) or uuid.uuid4().hex,
            source=str(data.get("source", InvocationSource.CHAT)),
            skill_id=str(data.get("skill_id", "")),
            scope=InvocationScope.from_dict(scope_data),
            policy=RunPolicy.from_dict(policy_data),
            inputs=sanitize_for_storage(inputs),
            parent_session_id=str(data.get("parent_session_id", "")),
            user_text=str(data.get("user_text", "")),
            created_at=float(data.get("created_at", time.time()) or time.time()),
        )


# ---------------------------------------------------------------------- #
# RunEvent / RunRecord
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunEvent:
    """运行事件（plan §6.1）。append-only；UI / 审计据此重建任意时刻状态。

    Attributes:
        event_id: uuid4 hex。
        run_id: 关联的 run。
        kind: :class:`RunEventKind`。
        timestamp: 秒。
        node_id: 相关节点 ID（可空）。
        payload: 结构化细节（脱敏后；不含 secret / 路径）。
    """

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    kind: str = ""
    timestamp: float = field(default_factory=time.time)
    node_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "payload": sanitize_for_storage(self.payload),
        }

    @classmethod
    def from_public_dict(cls, data: dict[str, Any]) -> "RunEvent":
        """Restore one sanitized event; payload is sanitized again defensively."""
        payload = data.get("payload", {})
        return cls(
            event_id=str(data.get("event_id", "")) or uuid.uuid4().hex,
            run_id=str(data.get("run_id", "")),
            kind=str(data.get("kind", "")),
            timestamp=float(data.get("timestamp", time.time()) or time.time()),
            node_id=str(data.get("node_id", "")),
            payload=sanitize_for_storage(payload if isinstance(payload, dict) else {}),
        )


@dataclass(frozen=True)
class JobRef:
    """``RunRecord`` 内对 Job 的只读引用（plan §0.4）。

    只存 ID / kind / 摘要；真实 ``JobHandle`` 由 ``scheduler_adapter`` 桥接，
    不在 run / artifact 之间传可变对象。
    """

    job_id: str = ""
    kind: str = ""
    label: str = ""
    status: str = ""
    progress: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRef":
        return cls(
            job_id=str(data.get("job_id", "")),
            kind=str(data.get("kind", "")),
            label=str(data.get("label", "")),
            status=str(data.get("status", "")),
            progress=float(data.get("progress", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class ArtifactRef:
    """``RunRecord`` 内对 artifact 的只读引用（plan §0.4 / §3.1）。

    小型、可验证、可追溯；不在聊天 / 节点间传文件路径 / Qt 对象。
    """

    artifact_id: str = ""
    artifact_type: str = ""
    producer_node_id: str = ""
    summary: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(data.get("artifact_id", "")),
            artifact_type=str(data.get("artifact_type", "")),
            producer_node_id=str(data.get("producer_node_id", "")),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True)
class RunRecord:
    """统一保存状态 / 节点事件 / Job / 成本 / artifact / 诊断的聚合只读视图。

    plan §0.4：``jobs.py`` + Artifact/Audit 的聚合视图；``AssistantStateSnapshot.jobs``
    不再固定空 jobs。

    本对象是 *快照*（frozen）；``HarnessRuntime`` 内部维护可变状态并通过
    ``RunRecord.updated`` 反映最新聚合。UI 订阅 runtime 的 event 流后据此刷新。

    Attributes:
        run_id: 唯一 run ID（与 invocation 关联）。
        invocation_id: 发起本次 run 的 invocation。
        parent_session_id: 父会话。
        source: 入口来源。
        skill_id: 目标 Skill。
        status: :class:`RunStatus`。
        created_at / started_at / finished_at: 生命周期时间戳。
        progress: 0.0–1.0。
        events: append-only 事件序列（脱敏）。
        jobs: 关联的 Job 引用（只读）。
        artifacts: 产出的 artifact 引用。
        cost: 累计成本。
        diagnostics: 诊断（错误码 / 建议 / debug detail，无 secret）。
        cancel_reason: 取消原因（可空）。
        error_code / error_message: 失败原因。
    """

    run_id: str = ""
    invocation_id: str = ""
    parent_session_id: str = ""
    source: str = InvocationSource.CHAT
    skill_id: str = ""
    status: str = RunStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0
    events: tuple[RunEvent, ...] = ()
    jobs: tuple[JobRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    cost: float = 0.0
    diagnostics: tuple[dict[str, Any], ...] = ()
    cancel_reason: str = ""
    error_code: str = ""
    error_message: str = ""

    @property
    def is_terminal(self) -> bool:
        return RunStatus.is_terminal(self.status)

    @property
    def is_active(self) -> bool:
        return RunStatus.is_active(self.status)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "invocation_id": self.invocation_id,
            "parent_session_id": self.parent_session_id,
            "source": self.source,
            "skill_id": self.skill_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "events": [e.to_public_dict() for e in self.events],
            "jobs": [
                {"job_id": j.job_id, "kind": j.kind, "label": j.label,
                 "status": j.status, "progress": j.progress}
                for j in self.jobs
            ],
            "artifacts": [
                {"artifact_id": a.artifact_id, "artifact_type": a.artifact_type,
                 "producer_node_id": a.producer_node_id, "summary": a.summary}
                for a in self.artifacts
            ],
            "cost": self.cost,
            "diagnostics": list(self.diagnostics),
            "cancel_reason": self.cancel_reason,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


# ---------------------------------------------------------------------- #
# 脱敏
# ---------------------------------------------------------------------- #


# 绝对路径：Unix / Windows / UNC。脱敏成 <absolute-path>。
_ABS_PATH_RE = re.compile(
    r"(?:(?:[A-Za-z]:[\\/])|"           # Windows 盘符 C:\ / C:/
    r"(?:[\\/][\\/][^\s\\/]+[\\/][^\s]+)|"  # UNC \\host\share 或 //host/share
    r"(?:[\/\\](?:Users|home|tmp|var|opt|etc|mnt|media|private)[\/\\][^\s\"']*))"
)

# 常见 secret 模式：key=... / token=... / Authorization: ... / sk-...
# （只做形状匹配，不追求覆盖一切；secret 应在源头不进入 invocation。）
_SECRET_RE = re.compile(
    r"(?i)"
    r"(?:api[_-]?key|secret|token|passwd|password|authorization|bearer)"
    r"\s*[:=]\s*"
    r"(?:[A-Za-z0-9_\-./+=]{12,})"
)
# OpenAI 风格 key 形状（sk-...）作为补充。
_OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}")


def sanitize_for_storage(value: Any) -> Any:
    """递归脱敏：去掉绝对路径与 secret 形状字符串（plan §1.4 红线 / §10.2 P0.1）。

    规则：

    - 字符串中的绝对路径 → ``<absolute-path>``。
    - 字符串中的 secret 形状 → ``<redacted-secret>``。
    - dict / list / tuple 递归处理（tuple 保持 tuple）。
    - 其它类型原样返回。

    注意：这是 *最后一道防线*。secret 不应在源头进入 invocation —— profile 的
    secret 由 :class:`CredentialStore` 单独管理，永远不传进 ``inputs``。
    """
    if isinstance(value, str):
        s = _ABS_PATH_RE.sub("<absolute-path>", value)
        s = _SECRET_RE.sub(
            lambda m: m.group(0).split("=", 1)[0].split(":", 1)[0].rstrip()
            + "=<redacted-secret>"
            if ("=" in m.group(0) or ":" in m.group(0))
            else "<redacted-secret>",
            s,
        )
        s = _OPENAI_KEY_RE.sub("<redacted-secret>", s)
        return s
    if isinstance(value, dict):
        return {sanitize_for_storage(k): sanitize_for_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_storage(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_storage(v) for v in value)
    return value


def collect_secret_free(values: Iterable[Any]) -> list[Any]:
    """便捷工具：对一组值脱敏后返回 list（用于诊断 / 审计打包）。"""
    return [sanitize_for_storage(v) for v in values]


__all__ = [
    "ArtifactRef",
    "Invocation",
    "InvocationScope",
    "InvocationSource",
    "JobRef",
    "PermissionLevel",
    "RunEvent",
    "RunEventKind",
    "RunPolicy",
    "RunRecord",
    "RunStatus",
    "collect_secret_free",
    "sanitize_for_storage",
]
