"""M9 Skill Runtime 集成测试（§3 / §5 / §6 / §11.1 M9）。

覆盖：
- SkillInput / SkillResult 数据合同
- SkillRuntime.run：未注册/禁用 → 诊断
- 官方 Skill（roles=()）走 WorkflowBackend（向后兼容 M7）
- 多角色 Skill（roles 非空）走 TaskGraph + 确定性 executor
- 单写者冲突 → degradation
- 失败/取消可见
- DefaultGraphBuilder 4 角色线性图
- OfficialResult / WorkflowBackend 协议
- assistant_service.run_skill 端到端（官方 Skill variant 灌入 _variants，复用闭环）

红线对照：
- 官方 Skill 走 M7 单路径（roles=()，"应用仍只有一个 Undo 单元"）
- 多角色 Skill 只产 typed artifact（§6.1 子智能体无独立 approval）
- 单写者（§6.2 规则 2，同 timeline 至多一个写候选）
- 失败可见（§6.2 规则 5，不编造完成）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.editorial_models import (
    EditDecision,
    EditVariant,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.editorial_models import FrameRange
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillManifest,
    SkillRisk,
    SkillSource,
    ToolPolicy,
    with_signature,
)
from src.features.edit.ai_editing.skill_registry import SkillRegistry
from src.features.edit.ai_editing.skill_runtime import (
    DefaultGraphBuilder,
    DeterministicRoleExecutor,
    OfficialResult,
    SkillInput,
    SkillRuntime,
)
from src.features.edit.ai_editing.task_artifacts import (
    EvidenceSet,
    QaReport,
    ScriptDraft,
    StoryOutline,
)
from src.features.edit.ai_editing.task_graph import (
    ContextSlice,
    SingleWriterPolicy,
    TaskRole,
)
from src.features.edit.ai_editing.task_graph import TaskGraphExecutor


# ================================================================
# 辅助 fixtures
# ================================================================


def _transcript_seg(seg_id: str, asset_id: str = "a1", start: int = 0) -> SemanticSegment:
    return SemanticSegment(
        segment_id=seg_id, asset_id=asset_id, kind=SegmentKind.TRANSCRIPT,
        source_range=FrameRange(start, 10),
        payload={"text": "口播内容"},
        confidence=0.9,
    )


class _FakeAnalysisService:
    """满足 workflow._AnalysisReader 的 duck-typed 分析服务。"""

    def __init__(self, status: str = "ready", segs=()) -> None:
        self._status = status
        self._segs = tuple(segs)

    def get_status(self, asset_id: str) -> str:
        return self._status

    def get_segments(self, asset_id: str):
        return self._segs

    # AnalysisService 兼容方法（run_workflow 间接需要）
    def search_assets(self, *, text=None, kind=None, limit=None):
        return ()


class _StubWorkflowBackend:
    """WorkflowBackend 协议的 stub（官方 Skill 走这）。"""

    def __init__(self, result: OfficialResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def run_official(self, manifest, inp: SkillInput) -> OfficialResult:
        self.calls.append(inp.skill_id)
        return self._result


def _make_variant(vid: str = "v1") -> EditVariant:
    return EditVariant(
        variant_id=vid,
        brief_id="b1",
        brief_revision=0,
        label="测试方案",
        decisions=(EditDecision(
            decision_id="d1", action="select_source_range",
            evidence_refs=("seg-1",), confidence=0.9,
        ),),
    )


def _runtime(backend=None, registry=None) -> SkillRuntime:
    return SkillRuntime(
        registry=registry or SkillRegistry(),
        policy=SingleWriterPolicy(),
        workflow_backend=backend,
    )


# ================================================================
# SkillInput / SkillResult 合同
# ================================================================


class TestSkillInputResult:
    def test_skill_input_defaults(self):
        inp = SkillInput(
            skill_id="s1", asset_ids=("a1",), timeline_id="tl1",
            project_id="p1", base_revision=0, brief_id="b1",
        )
        assert inp.target_duration == 720
        assert inp.brief_summary == ""
        assert inp.evidence_refs == ()

    def test_skill_result_failed_tasks_property(self):
        from src.features.edit.ai_editing.skill_runtime import SkillResult
        from src.features.edit.ai_editing.task_graph import TaskResult, TaskState
        r = SkillResult(
            skill_id="s1",
            task_results=(
                TaskResult(task_id="t1", role=TaskRole.QA_REVIEWER, state=TaskState.FAILED,
                           error_reason="boom"),
                TaskResult(task_id="t2", role=TaskRole.QA_REVIEWER, state=TaskState.SUCCEEDED),
            ),
        )
        assert len(r.failed_tasks) == 1
        assert r.degraded is False  # degradation 字段为 None


# ================================================================
# SkillRuntime.run 校验路径
# ================================================================


class TestRuntimeValidation:
    def test_unregistered_skill_returns_diagnostics(self):
        rt = _runtime(registry=SkillRegistry(register_official=False))
        inp = SkillInput(
            skill_id="bogus", asset_ids=("a1",), timeline_id="tl1",
            project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert result.variants == ()
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0]["severity"] == "error"

    def test_disabled_skill_denied(self):
        reg = SkillRegistry()
        reg.disable("topos.voiceover_polish")
        rt = _runtime(registry=reg)
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert len(result.diagnostics) == 1
        assert "disabled" in result.diagnostics[0]["message"]

    def test_empty_asset_ids_rejected(self):
        rt = _runtime()
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=(),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert any(d["severity"] == "error" for d in result.diagnostics)


# ================================================================
# 官方 Skill → WorkflowBackend（向后兼容）
# ================================================================


class TestOfficialSkillPath:
    def test_official_skill_routes_to_backend(self):
        variant = _make_variant()
        backend = _StubWorkflowBackend(OfficialResult(variants=(variant,)))
        rt = _runtime(backend=backend)
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert backend.calls == ["topos.voiceover_polish"]
        assert result.variants == (variant,)
        assert result.writer_acquired is True

    def test_official_skill_degradation_passthrough(self):
        """M7 降级（缺证据）透传到 SkillResult。"""
        backend = _StubWorkflowBackend(OfficialResult(
            degradation="缺少转录证据", missing_evidence=("a1: transcript",),
        ))
        rt = _runtime(backend=backend)
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert result.degraded is True
        assert "转录证据" in result.degradation

    def test_no_backend_returns_degradation(self):
        rt = _runtime(backend=None)
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert result.degraded is True
        assert "WorkflowBackend" in result.degradation


# ================================================================
# 单写者 policy（端到端）
# ================================================================


class TestSingleWriterE2E:
    def test_writer_acquired_and_released(self):
        backend = _StubWorkflowBackend(OfficialResult())
        policy = SingleWriterPolicy()
        rt = SkillRuntime(
            registry=SkillRegistry(), policy=policy, workflow_backend=backend,
        )
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        rt.run(inp)
        # 执行后写候选已释放（finally 块）
        assert policy.holder("tl1") is None

    def test_concurrent_same_timeline_second_blocked(self):
        """红线：同一 timeline 第二个 Skill 被拒（§6.2 规则 2）。

        模拟：policy 已被别的 skill 占用 → run 返回 degradation。
        """
        policy = SingleWriterPolicy()
        policy.acquire("tl1", "other-skill")  # 已被占用
        rt = SkillRuntime(
            registry=SkillRegistry(), policy=policy,
            workflow_backend=_StubWorkflowBackend(OfficialResult()),
        )
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert result.writer_acquired is False
        assert result.degraded is True
        assert "单写者" in result.degradation


# ================================================================
# 多角色 Skill → TaskGraph
# ================================================================


def _multiorole_manifest(skill_id: str = "multi.skill") -> SkillManifest:
    return with_signature(SkillManifest(
        skill_id=skill_id, version="1.0.0", title="多角色测试",
        required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
        produces_plan=False,
        tool_policy=ToolPolicy(max_permission="read"),
        risk=SkillRisk.PROPOSAL_ONLY,
        roles=("creative_director", "story_editor", "qa_reviewer"),
    ))


class TestMultiRolePath:
    def test_multiorole_produces_typed_artifacts(self):
        reg = SkillRegistry(register_official=False)
        reg.register(_multiorole_manifest())
        rt = SkillRuntime(
            registry=reg, policy=SingleWriterPolicy(),
            role_executor=DeterministicRoleExecutor(skill_id="multi.skill"),
            graph_builder=DefaultGraphBuilder(),
        )
        inp = SkillInput(
            skill_id="multi.skill", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
            evidence_refs=("seg-1",),
        )
        result = rt.run(inp)
        assert result.degraded is False
        # 3 个角色 → 3 个 artifact
        assert len(result.artifacts) == 3
        kinds = {type(a).__name__ for a in result.artifacts}
        assert kinds == {"ScriptDraft", "StoryOutline", "QaReport"}
        # 所有 task 成功
        assert all(r.state.value == "succeeded" for r in result.task_results)

    def test_run_forwards_cancel_to_taskgraph(self):
        """红线修复（§0 第三条 / M9 取消传播）：cancel_requested 透传到 TaskGraphExecutor。

        cancel_requested=lambda:True → TaskGraphExecutor 在首个节点派发前标 CANCELLED，
        cancelled_tasks 非空，降级提示可见。run 默认 None 时不取消（向后兼容）。
        """
        from src.features.edit.ai_editing.task_graph import TaskState

        reg = SkillRegistry(register_official=False)
        reg.register(_multiorole_manifest())
        rt = SkillRuntime(
            registry=reg, policy=SingleWriterPolicy(),
            role_executor=DeterministicRoleExecutor(skill_id="multi.skill"),
            graph_builder=DefaultGraphBuilder(),
        )
        inp = SkillInput(
            skill_id="multi.skill", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
            evidence_refs=("seg-1",),
        )
        # cancel 立即为真 → 首节点前全部 CANCELLED
        result = rt.run(inp, cancel_requested=lambda: True)
        assert len(result.cancelled_tasks) >= 1
        assert all(r.state == TaskState.CANCELLED for r in result.cancelled_tasks)
        assert result.degraded  # 取消必须可见（不编造完成）
        assert "取消" in (result.degradation or "")

    def test_multiorole_failure_visible(self):
        """失败可见（§6.2 规则 5）。"""
        class FailingExecutor:
            def execute(self, role, context, upstream):
                if role == TaskRole.STORY_EDITOR:
                    raise RuntimeError("story offline")
                return DeterministicRoleExecutor().execute(role, context, upstream)

        reg = SkillRegistry(register_official=False)
        reg.register(_multiorole_manifest())
        rt = SkillRuntime(
            registry=reg, policy=SingleWriterPolicy(),
            role_executor=FailingExecutor(),
            graph_builder=DefaultGraphBuilder(),
        )
        inp = SkillInput(
            skill_id="multi.skill", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        result = rt.run(inp)
        assert result.degraded is True
        assert "失败" in result.degradation
        assert len(result.failed_tasks) >= 1


# ================================================================
# DefaultGraphBuilder
# ================================================================


class TestDefaultGraphBuilder:
    def test_builds_linear_dag_in_section_6_3_order(self):
        """§6.3 典型任务图顺序：creative → media → story → qa。"""
        m = with_signature(SkillManifest(
            skill_id="s1", version="1.0.0", title="T",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            roles=("creative_director", "media_librarian", "story_editor", "qa_reviewer"),
        ))
        inp = SkillInput(
            skill_id="s1", asset_ids=("a1",), timeline_id="tl1",
            project_id="p1", base_revision=0, brief_id="b1",
            evidence_refs=("seg-1",),
        )
        g = DefaultGraphBuilder().build(m, inp)
        assert g.validate() == []
        order = [n.role.value for n in g.topo_order()]
        assert order == ["creative_director", "media_librarian", "story_editor", "qa_reviewer"]

    def test_subset_roles(self):
        m = with_signature(SkillManifest(
            skill_id="s1", version="1.0.0", title="T",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            roles=("qa_reviewer",),  # 只要 QA
        ))
        inp = SkillInput(
            skill_id="s1", asset_ids=("a1",), timeline_id="tl1",
            project_id="p1", base_revision=0, brief_id="b1",
        )
        g = DefaultGraphBuilder().build(m, inp)
        assert len(g.nodes) == 1
        assert g.nodes[0].role == TaskRole.QA_REVIEWER

    def test_empty_roles_empty_graph(self):
        m = OFFICIAL_VOICEOVER_POLISH  # roles=()
        inp = SkillInput(
            skill_id="topos.voiceover_polish", asset_ids=("a1",),
            timeline_id="tl1", project_id="p1", base_revision=0, brief_id="b1",
        )
        g = DefaultGraphBuilder().build(m, inp)
        assert g.nodes == ()

    def test_context_minimal(self):
        """§6.2 规则 3：context 只含最小数据。"""
        m = with_signature(SkillManifest(
            skill_id="s1", version="1.0.0", title="标题提示",
            required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
            roles=("creative_director",),
        ))
        inp = SkillInput(
            skill_id="s1", asset_ids=("a1",), timeline_id="tl1",
            project_id="p1", base_revision=0, brief_id="b1",
            evidence_refs=("seg-1", "seg-2"),
            brief_summary="简要",
        )
        g = DefaultGraphBuilder().build(m, inp)
        ctx = g.nodes[0].context
        assert ctx.asset_ids == ("a1",)
        assert ctx.evidence_refs == ("seg-1", "seg-2")
        assert ctx.role_hint == "标题提示"  # 用 title 作角色提示
