"""TAEP M9 — Skill Runtime：把 Skill manifest 接到 TaskGraph + 宿主闭环（§3 / §5 / §6 / §11.1 M9）。

``SkillRuntime`` 是 M9 的中央编排器，把三个抽象接起来：

    SkillManifest（声明：做什么 + 用什么工具 + 哪些角色）
        ↓ ``run()``
    TaskGraph（受控子智能体 DAG：最小上下文 + 预算 + 取消 + 单写者）
        ↓ ``TaskGraphExecutor`` + ``DeterministicRoleExecutor``
    SkillResult（typed artifact 聚合 + 可选 plan/artifact 产物）
        ↓ 交给宿主 ``AssistantService`` 的既有 preview/approve/commit 闭环

红线（§11.1 M9 / §6 / §5 / .clinerules / §0）：

- **子智能体无独立 approval**（§6.1 / §6.2 规则 4 / §11.1 M9 step 4）：TaskGraph 只产
  typed artifact；任何 timeline 写入仍经宿主 ``ApprovalService`` + ``CommandStack``。
- **单写者**（§6.2 规则 2）：``run()`` 经 ``SingleWriterPolicy`` 获取 timeline 写候选；
  同一 timeline 同时至多一个 active graph。
- **确定性 v1**（§6.3："首版以确定角色和固定图实现"）：``DeterministicRoleExecutor``
  产固定 artifact，不接 LLM；真实 LLM 角色留 M10。
- **官方 Skill 向后兼容**（M7 红线："应用仍只有一个 Undo 单元"）：``roles=()`` 的 Skill
  （三个官方工作流）走 M7 既有 ``WorkflowRunner`` 单路径，**不**引入 TaskGraph——保证
  ``run_workflow`` 行为完全不变。``roles`` 非空的 Skill 才走 TaskGraph 多角色路径。
- **失败可见 + 取消传播**（§6.2 规则 5/6）：``SkillResult`` 携带 ``failed_tasks`` /
  ``cancelled_tasks`` / ``degradation``；绝不编造完成。

本模块**不**：自己写 timeline / 调 approval / 持久化 artifact（落地由宿主闭环负责）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.editorial_models import EditVariant
from src.features.edit.ai_editing.skill_manifest import SkillManifest
from src.features.edit.ai_editing.task_artifacts import (
    EvidenceCitation,
    EvidenceGap,
    EvidenceSet,
    HookIdea,
    QaFinding,
    QaReport,
    ScriptDraft,
    StoryBeat,
    StoryOutline,
    SubagentArtifact,
)
from src.features.edit.ai_editing.task_graph import (
    ContextSlice,
    RoleExecutor,
    SingleWriterPolicy,
    TaskBudget,
    TaskGraph,
    TaskGraphExecutor,
    TaskNode,
    TaskResult,
    TaskRole,
    TaskState,
)


# ---------------------------------------------------------------------- #
# SkillInput / SkillResult
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillInput:
    """Skill 运行的统一输入（镜像 M7 ``WorkflowInput`` 语义）。

    ``asset_ids`` 必须来自用户确认的 selection（§0 红线）。``brief_summary`` 是给
    子任务的最小上下文（非完整 brief，§6.2 规则 3）。
    """

    skill_id: str
    asset_ids: tuple[str, ...]
    timeline_id: str
    project_id: str
    base_revision: int
    brief_id: str
    brief_summary: str = ""           # 最小上下文（非完整 brief）
    evidence_refs: tuple[str, ...] = ()  # 已就绪证据 segment_id
    target_duration: int = 720


@dataclass(frozen=True)
class SkillResult:
    """Skill 运行产物（只读）。

    - ``variants`` / ``artifact_id``：与 M7 ``WorkflowResult`` 同语义（向后兼容）。
    - ``artifacts``：TaskGraph 各角色的 typed artifact（``roles`` 非空的 Skill 才有）。
    - ``task_results``：每个子任务的状态（失败/取消可见）。
    - ``degradation``：降级说明（缺证据 / TaskGraph 失败 / 单写者冲突）。
    - ``writer_acquired``：是否成功获取单写者候选权。
    """

    skill_id: str
    variants: tuple[EditVariant, ...] = ()
    artifact_id: str | None = None
    artifacts: tuple[SubagentArtifact, ...] = ()
    task_results: tuple[TaskResult, ...] = ()
    degradation: str | None = None
    writer_acquired: bool = False
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def degraded(self) -> bool:
        return self.degradation is not None

    @property
    def failed_tasks(self) -> tuple[TaskResult, ...]:
        """失败的子任务（失败可见，§6.2 规则 5）。"""
        return tuple(r for r in self.task_results if r.state == TaskState.FAILED)

    @property
    def cancelled_tasks(self) -> tuple[TaskResult, ...]:
        """被取消的子任务（取消传播，§6.2 规则 6）。"""
        return tuple(r for r in self.task_results if r.state == TaskState.CANCELLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "variants": [getattr(v, "variant_id", str(v)) for v in self.variants],
            "artifact_id": self.artifact_id,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "task_results": [r.to_dict() for r in self.task_results],
            "degradation": self.degradation,
            "writer_acquired": self.writer_acquired,
            "diagnostics": list(self.diagnostics),
            "failed_count": len(self.failed_tasks),
            "cancelled_count": len(self.cancelled_tasks),
        }


# ---------------------------------------------------------------------- #
# WorkflowBackend Protocol —— 官方 Skill 走 M7 WorkflowRunner 的注入点
# ---------------------------------------------------------------------- #


class WorkflowBackend(Protocol):
    """官方 Skill（roles=()）走 M7 既有 WorkflowRunner 的注入点。

    SkillRuntime 不直接依赖 ``WorkflowRunner``（避免循环 + 保持可测）：宿主注入一个
    接受 ``workflow-like input`` 的回调。返回 ``(variants, artifact_id, degradation, missing_evidence)``。
    """

    def run_official(
        self, manifest: SkillManifest, inp: SkillInput,
    ) -> "OfficialResult": ...


@dataclass(frozen=True)
class OfficialResult:
    """WorkflowBackend.run_official 的返回（M7 WorkflowResult 的精简镜像）。"""

    variants: tuple[EditVariant, ...] = ()
    artifact_id: str | None = None
    degradation: str | None = None
    missing_evidence: tuple[str, ...] = ()


# ---------------------------------------------------------------------- #
# DeterministicRoleExecutor —— 确定性 stub（v1，§6.3）
# ---------------------------------------------------------------------- #


@dataclass
class DeterministicRoleExecutor:
    """固定角色的确定性执行器（v1 / §6.3："首版以确定角色和固定图实现"）。

    不接 LLM：每个角色按 ``context`` 产出**固定的、确定性的** typed artifact。
    符合 M7 planner 的"可解释/可验证/可撤销"模式。

    确定性原则：相同输入（context + upstream）→ 相同输出（无随机性，便于 eval）。
    """

    skill_id: str = ""
    skill_version: str = ""

    def execute(
        self, role: TaskRole, context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
    ) -> SubagentArtifact:
        """按角色产确定性 typed artifact。"""
        skill_id = self.skill_id or "skill"
        skill_ver = self.skill_version or "1.0.0"
        ev_refs = context.evidence_refs
        primary = context.asset_ids[0] if context.asset_ids else "asset"

        if role == TaskRole.CREATIVE_DIRECTOR:
            return self._creative_director(skill_id, skill_ver, context, primary)
        if role == TaskRole.MEDIA_LIBRARIAN:
            return self._media_librarian(skill_id, skill_ver, context, primary, ev_refs)
        if role == TaskRole.STORY_EDITOR:
            return self._story_editor(skill_id, skill_ver, context, primary, upstream, ev_refs)
        if role == TaskRole.QA_REVIEWER:
            return self._qa_reviewer(skill_id, skill_ver, context, upstream)
        # 未实现角色：明确失败（不编造）
        raise NotImplementedError(f"role {role} not implemented in DeterministicRoleExecutor")

    # ---------------- 创意总监 ---------------- #

    def _creative_director(
        self, skill_id: str, skill_ver: str, context: ContextSlice, primary: str,
    ) -> ScriptDraft:
        audience = context.role_hint or "目标受众"
        return ScriptDraft(
            draft_id=f"draft-{primary}-{skill_id}",
            audience=audience,
            tone="清晰、有行动力",
            hooks=(
                HookIdea(
                    hook_id="hook-1",
                    angle="痛点前置",
                    draft_text=f"开场抛出 {audience} 的核心痛点",
                    rationale="痛点前置提升前 3 秒留存",
                ),
                HookIdea(
                    hook_id="hook-2",
                    angle="价值承诺",
                    draft_text=f"直接承诺本片给 {audience} 的价值",
                    rationale="价值承诺适合决策型受众",
                ),
            ),
            call_to_action="结尾给出明确的下一步行动",
            produced_by="creative_director",
            skill_id=skill_id, skill_version=skill_ver,
            evidence_refs=context.evidence_refs,
        )

    # ---------------- 媒体管理员 ---------------- #

    def _media_librarian(
        self, skill_id: str, skill_ver: str, context: ContextSlice,
        primary: str, ev_refs: tuple[str, ...],
    ) -> EvidenceSet:
        # 把已就绪证据包成 citation（全部 trusted——转录/人工标注；OCR 若进来自有 trust 标记）
        citations = tuple(
            EvidenceCitation(segment_id=ref, asset_id=primary, trust="trusted")
            for ref in ev_refs
        )
        # 缺口：无证据时标 blocking 缺口
        gaps: tuple[EvidenceGap, ...] = ()
        if not ev_refs:
            gaps = (EvidenceGap(
                gap_id=f"gap-{primary}",
                description="未找到已就绪的分析证据",
                severity="blocking",
                suggested_action="先对所选素材运行 editorial_local 分析",
            ),)
        return EvidenceSet(
            set_id=f"evidence-{primary}-{skill_id}",
            citations=citations,
            gaps=gaps,
            licensing_notes="授权状态待用户确认（首版不自动判定）",
            produced_by="media_librarian",
            skill_id=skill_id, skill_version=skill_ver,
            evidence_refs=ev_refs,
        )

    # ---------------- 故事编辑 ---------------- #

    def _story_editor(
        self, skill_id: str, skill_ver: str, context: ContextSlice, primary: str,
        upstream: tuple[SubagentArtifact, ...], ev_refs: tuple[str, ...],
    ) -> StoryOutline:
        # 从上游 creative_director 的 ScriptDraft 提取 hook 作为开场 beat
        hook_text = "开场 hook（来自创意总监）"
        cta_text = "行动号召"
        for a in upstream:
            if isinstance(a, ScriptDraft) and a.hooks:
                hook_text = a.hooks[0].draft_text or hook_text
                cta_text = a.call_to_action or cta_text

        # beat 序列：hook → product_value → call_to_action（与官方工作流 target_beats 对齐）
        beats = []
        # hook beat：有证据则引用第一条，无则低置信
        hook_ref = ev_refs[0] if ev_refs else ""
        beats.append(StoryBeat(
            beat_id="beat-hook", label="hook", description=hook_text,
            evidence_ref=hook_ref, confidence=1.0 if hook_ref else 0.4,
        ))
        # product_value beat：引用第二条证据（若有）
        pv_ref = ev_refs[1] if len(ev_refs) > 1 else ""
        beats.append(StoryBeat(
            beat_id="beat-value", label="product_value", description="产品价值展示",
            evidence_ref=pv_ref, confidence=1.0 if pv_ref else 0.4,
        ))
        beats.append(StoryBeat(
            beat_id="beat-cta", label="call_to_action", description=cta_text,
            evidence_ref="", confidence=0.4,  # CTA 通常是文案，无强证据
        ))
        return StoryOutline(
            outline_id=f"outline-{primary}-{skill_id}",
            beats=tuple(beats),
            storyboard_hints=("开场特写", "产品价值中景", "行动号召结尾卡"),
            produced_by="story_editor",
            skill_id=skill_id, skill_version=skill_ver,
            evidence_refs=ev_refs,
        )

    # ---------------- QA 审查 ---------------- #

    def _qa_reviewer(
        self, skill_id: str, skill_ver: str, context: ContextSlice,
        upstream: tuple[SubagentArtifact, ...],
    ) -> QaReport:
        findings: list[QaFinding] = []

        # 检查上游 evidence_set 的 blocking gap
        for a in upstream:
            if isinstance(a, EvidenceSet):
                for gap in a.gaps:
                    if gap.severity == "blocking":
                        findings.append(QaFinding(
                            finding_id=f"qa-gap-{gap.gap_id}",
                            category="constraint", severity="blocking",
                            message=f"证据缺口：{gap.description}",
                            suggested_action=gap.suggested_action,
                        ))
                # 授权备注 → QA finding
                if a.licensing_notes:
                    findings.append(QaFinding(
                        finding_id="qa-license", category="licensing",
                        severity="warning", message=a.licensing_notes,
                        suggested_action="确认素材授权后继续",
                    ))
                # untrusted 比例高 → 提示
                if a.untrusted_count > 0:
                    findings.append(QaFinding(
                        finding_id="qa-untrusted", category="constraint",
                        severity="warning",
                        message=f"{a.untrusted_count} 条不可信引用（OCR/网页），不进字幕",
                        suggested_action="untrusted 数据不进字幕与权限决策",
                    ))

        # 时长检查（首版固定：target_duration）
        if context.asset_ids:
            findings.append(QaFinding(
                finding_id="qa-duration", category="duration",
                severity="info",
                message=f"目标时长需用户确认（首版默认 {context.role_hint or '30s'}）",
                suggested_action="在预览阶段核对时长",
            ))

        # blocking 优先级排序
        sev_order = {"blocking": 0, "warning": 1, "info": 2}
        findings.sort(key=lambda f: sev_order.get(f.severity, 3))

        return QaReport(
            report_id=f"qa-{skill_id}",
            findings=tuple(findings),
            approved=not any(f.severity == "blocking" for f in findings),
            summary=(
                "QA 通过" if not any(f.severity == "blocking" for f in findings)
                else "QA 发现 blocking 问题，需先解决"
            ),
            produced_by="qa_reviewer",
            skill_id=skill_id, skill_version=skill_ver,
        )


# ---------------------------------------------------------------------- #
# SkillRuntime —— 中央编排器
# ---------------------------------------------------------------------- #


class SkillRuntime:
    """Skill Runtime（§3 / §5 / §6 / §11.1 M9）。

    用法::

        runtime = SkillRuntime(registry, single_writer_policy,
                               workflow_backend=M7_runner_adapter)
        result = runtime.run(SkillInput(skill_id="topos.voiceover_polish", ...))
        # 官方 Skill（roles=()）→ workflow_backend（M7 闭环，向后兼容）
        # 多角色 Skill（roles 非空）→ TaskGraph + DeterministicRoleExecutor

    红线：``run()`` 经 ``SingleWriterPolicy`` 获取 timeline 写候选；TaskGraph 只产
    typed artifact；任何 timeline 写入仍由宿主经 approval/commit 闭环。
    """

    def __init__(
        self,
        registry: Any,  # SkillRegistry（duck-typed: get/is_enabled/require）
        policy: SingleWriterPolicy,
        *,
        workflow_backend: WorkflowBackend | None = None,
        role_executor: RoleExecutor | None = None,
        graph_builder: "GraphBuilder | None" = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._workflow_backend = workflow_backend
        self._role_executor = role_executor
        self._graph_builder = graph_builder

    # ---------------- 查询 ---------------- #

    def manifest(self, skill_id: str) -> SkillManifest | None:
        return self._registry.get(skill_id)

    # ---------------- 主入口 ---------------- #

    def run(
        self, inp: SkillInput,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SkillResult:
        """执行 Skill。官方 Skill 走 M7 闭环；多角色 Skill 走 TaskGraph。

        ``cancel_requested``：取消信号回调（§0 第三条）。非 None 时透传给
        ``TaskGraphExecutor.execute``，在每节点派发前检查。官方 Skill（roles=()）
        不透传——其取消由宿主在 M7 workflow 层处理（既有闭环语义）。
        """
        diag_dicts: list[dict[str, Any]] = []

        # 1. Skill 已注册 + 启用？
        manifest = self._registry.get(inp.skill_id)
        if manifest is None:
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
                op_kind=inp.skill_id,
                details={"reason": "skill not registered"},
            ).to_dict())
            return SkillResult(skill_id=inp.skill_id, diagnostics=tuple(diag_dicts))
        if not self._registry.is_enabled(inp.skill_id):
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=f"skill {inp.skill_id} is disabled",
            ).to_dict())
            return SkillResult(skill_id=inp.skill_id, diagnostics=tuple(diag_dicts))

        # 2. 输入校验
        for d in self._validate_input(manifest, inp):
            diag_dicts.append(d.to_dict())
        if any(d.get("severity") == "error" for d in diag_dicts):
            return SkillResult(skill_id=inp.skill_id, diagnostics=tuple(diag_dicts))

        # 3. 单写者：获取 timeline 写候选（§6.2 规则 2）
        writer_acquired = self._policy.acquire(inp.timeline_id, inp.skill_id)
        if not writer_acquired:
            holder = self._policy.holder(inp.timeline_id)
            return SkillResult(
                skill_id=inp.skill_id,
                degradation=(
                    f"单写者冲突：timeline {inp.timeline_id} 已被 skill {holder} 占用写候选。"
                    "同一 timeline 同时至多一个提交候选（§6.2）。请等其完成或取消后再试。"
                ),
                writer_acquired=False,
            )

        try:
            # 4. 分流：官方（roles=()）→ M7 WorkflowBackend；多角色 → TaskGraph
            if not manifest.roles:
                return self._run_official(manifest, inp, writer_acquired, diag_dicts)
            return self._run_taskgraph(
                manifest, inp, writer_acquired, diag_dicts, cancel_requested=cancel_requested,
            )
        finally:
            # 释放写候选（无论成功/失败/降级）
            self._policy.release(inp.timeline_id, inp.skill_id)

    # ---------------- 官方 Skill：M7 闭环（向后兼容）---------------- #

    def _run_official(
        self, manifest: SkillManifest, inp: SkillInput,
        writer_acquired: bool, diag_dicts: list[dict[str, Any]],
    ) -> SkillResult:
        """官方 Skill（roles=()）走 M7 既有 WorkflowRunner（经 workflow_backend 注入）。"""
        if self._workflow_backend is None:
            return SkillResult(
                skill_id=inp.skill_id,
                degradation="官方 Skill 需要 WorkflowBackend 注入（宿主未提供）",
                writer_acquired=writer_acquired,
                diagnostics=tuple(diag_dicts),
            )
        result = self._workflow_backend.run_official(manifest, inp)
        # M7 降级（缺证据）透传
        degradation = result.degradation
        return SkillResult(
            skill_id=inp.skill_id,
            variants=result.variants,
            artifact_id=result.artifact_id,
            degradation=degradation,
            writer_acquired=writer_acquired,
            diagnostics=tuple(diag_dicts),
        )

    # ---------------- 多角色 Skill：TaskGraph ---------------- #

    def _run_taskgraph(
        self, manifest: SkillManifest, inp: SkillInput,
        writer_acquired: bool, diag_dicts: list[dict[str, Any]],
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SkillResult:
        """多角色 Skill（roles 非空）走 TaskGraph + 确定性 role executor。"""
        # 1. 构造 TaskGraph（由 graph_builder 或默认 4 角色线性图）
        builder = self._graph_builder or DefaultGraphBuilder()
        try:
            graph = builder.build(manifest, inp)
        except ValueError as exc:
            # AUD-14413：图构造失败（如 roles 全部无法映射 TaskRole）必须可见——
            # 不静默产出"成功"的空图，也不让异常击穿 run() 调用方。
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                detail=f"TaskGraph build failed for skill {inp.skill_id}: {exc}",
            ).to_dict())
            return SkillResult(
                skill_id=inp.skill_id,
                degradation=f"TaskGraph 构造失败：{exc}",
                writer_acquired=writer_acquired,
                diagnostics=tuple(diag_dicts),
            )
        graph_errors = graph.validate()
        if graph_errors:
            return SkillResult(
                skill_id=inp.skill_id,
                degradation=f"TaskGraph 非法：{graph_errors[0]}",
                writer_acquired=writer_acquired,
                diagnostics=tuple(diag_dicts),
            )

        # AUD-14413：manifest 声明了但没进图的角色（部分非法被丢弃）必须可见，
        # 不静默少跑角色后仍按"完整成功"上报。
        node_roles = {n.role.value for n in graph.nodes}
        dropped_roles = [r for r in manifest.roles if r not in node_roles]
        if dropped_roles:
            diag_dicts.append(make_diagnostic(
                DiagnosticSeverity.WARNING, TaepDiagnosticCode.SCHEMA_INVALID,
                detail=(
                    f"skill {manifest.skill_id} declares roles not runnable by this "
                    f"runtime (dropped from graph): {dropped_roles}"
                ),
            ).to_dict())

        # 2. 执行（确定性 role executor）
        executor = TaskGraphExecutor(
            role_executor=self._role_executor or DeterministicRoleExecutor(
                skill_id=manifest.skill_id, skill_version=manifest.version,
            ),
            policy=self._policy,
        )
        task_results = executor.execute(graph, cancel_requested=cancel_requested)

        # 3. 收集 artifact + 失败/取消可见
        artifacts = tuple(r.artifact for r in task_results if r.artifact is not None)
        failed = [r for r in task_results if r.state == TaskState.FAILED]
        cancelled = [r for r in task_results if r.state == TaskState.CANCELLED]
        needs_input = [r for r in task_results if r.state == TaskState.NEEDS_USER_INPUT]
        degraded_tasks = [r for r in task_results if r.degradation_reason]

        degradation: str | None = None
        if failed:
            degradation = (
                f"TaskGraph 有 {len(failed)} 个子任务失败："
                + "; ".join(f"{r.task_id}({r.error_reason})" for r in failed[:2])
            )
        elif cancelled:
            degradation = (
                f"TaskGraph 有 {len(cancelled)} 个子任务被取消（上游失败或父任务取消）"
            )
        elif needs_input:
            degradation = (
                f"TaskGraph 有 {len(needs_input)} 个子任务超过预算/截止时间，需用户介入："
                + "; ".join(f"{r.task_id}({r.error_reason})" for r in needs_input[:2])
            )
        elif degraded_tasks:
            degradation = (
                "部分角色使用了显式 deterministic fallback："
                + "; ".join(
                    f"{r.task_id}({r.degradation_reason})" for r in degraded_tasks[:3]
                )
            )
        elif dropped_roles:
            # AUD-14413：无任务级失败/取消时的兜底降级说明（角色被丢弃 → 结果不完整）
            degradation = (
                f"Skill 声明的角色未全部进图（被丢弃：{dropped_roles}）；"
                "本次运行只覆盖图中角色，产物不完整。"
            )

        return SkillResult(
            skill_id=inp.skill_id,
            artifacts=artifacts,
            task_results=task_results,
            degradation=degradation,
            writer_acquired=writer_acquired,
            diagnostics=tuple(diag_dicts),
        )

    # ---------------- 输入校验 ---------------- #

    def _validate_input(self, manifest: SkillManifest, inp: SkillInput) -> list[Diagnostic]:
        """纯校验。返回诊断列表（空 = 通过）。"""
        diags: list[Diagnostic] = []
        if not inp.asset_ids:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "asset_ids must be non-empty"},
            ))
        if not inp.timeline_id or not inp.project_id:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                details={"reason": "timeline_id/project_id required"},
            ))
        if inp.target_duration <= 0:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.SOURCE_RANGE_INVALID,
                details={"reason": "target_duration must be > 0"},
            ))
        return diags


# ---------------------------------------------------------------------- #
# GraphBuilder —— manifest.roles → TaskGraph
# ---------------------------------------------------------------------- #


class GraphBuilder(Protocol):
    """从 manifest.roles + SkillInput 构造 TaskGraph（注入点）。"""

    def build(self, manifest: SkillManifest, inp: SkillInput) -> TaskGraph: ...


@dataclass
class DefaultGraphBuilder:
    """默认图构造器：4 角色线性 DAG（§6.3 典型任务图）。

    创意总监 → 媒体管理员 → 故事编辑 → QA 审查（线性依赖）。每个角色产 typed
    artifact，QA 最后审查。``roles`` 决定包含哪些节点（按 manifest 声明）。
    """

    def build(self, manifest: SkillManifest, inp: SkillInput) -> TaskGraph:
        known_roles = {rr.value for rr in TaskRole}
        declared = [TaskRole(r) for r in manifest.roles if r in known_roles]
        if not declared:
            if manifest.roles:
                # AUD-14413：声明了角色但全部无法映射 TaskRole → 显式失败。
                # 注册期不校验 roles（skill_registry 不在本模块），这里是最后防线：
                # 静默返回空图会让运行"成功"却什么都没做，违反"失败可见"红线。
                raise ValueError(
                    f"skill {manifest.skill_id} declares roles {list(manifest.roles)} "
                    "but none map to a known TaskRole"
                )
            # 空角色声明 → 空 graph（SkillRuntime 会走 official 分支，不应到这）
            return TaskGraph(skill_id=manifest.skill_id, timeline_id=inp.timeline_id)

        # 默认顺序（§6.3）：creative → media → story → qa
        order_pref = (
            TaskRole.CREATIVE_DIRECTOR,
            TaskRole.MEDIA_LIBRARIAN,
            TaskRole.STORY_EDITOR,
            TaskRole.QA_REVIEWER,
        )
        ordered = [r for r in order_pref if r in declared]
        # 声明里有但不在 order_pref 的角色追加到末尾（保留声明）
        for r in declared:
            if r not in ordered:
                ordered.append(r)

        nodes: list[TaskNode] = []
        prev_id: str | None = None
        for i, role in enumerate(ordered):
            task_id = f"task-{i:02d}-{role.value}"
            context = ContextSlice(
                asset_ids=inp.asset_ids,
                evidence_refs=inp.evidence_refs,
                brief_summary=inp.brief_summary,
                role_hint=manifest.title,  # 用 Skill title 作为角色提示
            )
            deps = (prev_id,) if prev_id else ()
            nodes.append(TaskNode(
                task_id=task_id, role=role, context=context,
                depends_on=deps, budget=TaskBudget(),
            ))
            prev_id = task_id

        return TaskGraph(
            skill_id=manifest.skill_id,
            nodes=tuple(nodes),
            timeline_id=inp.timeline_id,
        )


__all__ = [
    "SkillInput",
    "SkillResult",
    "WorkflowBackend",
    "OfficialResult",
    "DeterministicRoleExecutor",
    "SkillRuntime",
    "GraphBuilder",
    "DefaultGraphBuilder",
]
