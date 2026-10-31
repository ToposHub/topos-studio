"""M9 TaskGraph 单元测试（§6 / §10.1 / §11.1 M9 step 3-4）。

覆盖：
- TaskRole 4 角色 + produces_kind 映射
- TaskBudget / ContextSlice（最小上下文）
- TaskGraph DAG 校验（环 / 悬空依赖 / 自环）
- topo_order 拓扑序
- SingleWriterPolicy 单写者（同 timeline 互斥 / 不同 timeline 并行 / 子任务禁写）
- TaskGraphExecutor：成功路径 / 失败可见 / 取消传播 / 上游失败级联取消
- typed artifact contract（role 产物类型不匹配 → FAILED）
- DeterministicRoleExecutor 各角色产固定 artifact

红线对照：
- 子智能体是只读 worker（§6.1，can_write_timeline=True → PermissionError）
- 单写者（§6.2 规则 2 / §11.1 M9 step 4，同 timeline 至多一个）
- 最小上下文（§6.2 规则 3，ContextSlice 不继承父任务全量）
- 取消传播（§6.2 规则 6，父取消 → 子取消）
- 失败可见（§6.2 规则 5，不编造完成）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.task_artifacts import (
    EvidenceSet,
    QaReport,
    ScriptDraft,
    StoryOutline,
)
from src.features.edit.ai_editing.task_graph import (
    ContextSlice,
    SingleWriterPolicy,
    TaskBudget,
    TaskGraph,
    TaskGraphExecutor,
    TaskNode,
    TaskResult,
    TaskRole,
    TaskState,
)
from src.features.edit.ai_editing.skill_runtime import DeterministicRoleExecutor


# ================================================================
# TaskRole
# ================================================================


class TestTaskRole:
    def test_four_roles(self):
        roles = {r.value for r in TaskRole}
        assert roles == {
            "creative_director", "media_librarian",
            "story_editor", "qa_reviewer",
        }

    def test_produces_kind_mapping(self):
        assert TaskRole.CREATIVE_DIRECTOR.produces_kind == "script_draft"
        assert TaskRole.MEDIA_LIBRARIAN.produces_kind == "evidence_set"
        assert TaskRole.STORY_EDITOR.produces_kind == "story_outline"
        assert TaskRole.QA_REVIEWER.produces_kind == "qa_report"


# ================================================================
# TaskBudget / ContextSlice
# ================================================================


class TestBudgetAndContext:
    def test_budget_defaults(self):
        b = TaskBudget()
        assert b.max_tool_calls == 8
        assert b.timeout_ms == 30_000
        assert b.max_tokens is None  # v1 预留

    def test_context_minimal(self):
        c = ContextSlice(asset_ids=("a1",), evidence_refs=("seg-1",))
        assert c.asset_ids == ("a1",)
        assert c.brief_summary == ""  # 非完整 brief


# ================================================================
# TaskNode
# ================================================================


class TestTaskNode:
    def test_default_artifact_kind_from_role(self):
        n = TaskNode(task_id="t1", role=TaskRole.CREATIVE_DIRECTOR)
        assert n.expected_artifact_kind == "script_draft"

    def test_can_write_timeline_default_false(self):
        n = TaskNode(task_id="t1", role=TaskRole.QA_REVIEWER)
        assert n.can_write_timeline is False  # 红线默认

    def test_self_dependency_rejected(self):
        with pytest.raises(ValueError):
            TaskNode(task_id="t1", role=TaskRole.QA_REVIEWER, depends_on=("t1",))

    def test_empty_task_id_rejected(self):
        with pytest.raises(ValueError):
            TaskNode(task_id="", role=TaskRole.QA_REVIEWER)


# ================================================================
# TaskGraph DAG 校验 + topo_order
# ================================================================


def _node(tid: str, role: TaskRole = TaskRole.QA_REVIEWER, deps=()) -> TaskNode:
    return TaskNode(task_id=tid, role=role, depends_on=tuple(deps))


class TestTaskGraphValidation:
    def test_empty_graph_valid(self):
        g = TaskGraph(skill_id="s1")
        assert g.validate() == []
        assert g.topo_order() == []

    def test_linear_deps_valid(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", TaskRole.CREATIVE_DIRECTOR),
            _node("b", TaskRole.STORY_EDITOR, deps=("a",)),
            _node("c", TaskRole.QA_REVIEWER, deps=("b",)),
        ))
        assert g.validate() == []

    def test_topo_order_linear(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("c", TaskRole.QA_REVIEWER, deps=("b",)),
            _node("a", TaskRole.CREATIVE_DIRECTOR),
            _node("b", TaskRole.STORY_EDITOR, deps=("a",)),
        ))
        order = [n.task_id for n in g.topo_order()]
        assert order == ["a", "b", "c"]

    def test_dangling_dependency_detected(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", deps=("nonexistent",)),
        ))
        errors = g.validate()
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_cycle_detected(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", deps=("b",)),
            _node("b", deps=("a",)),
        ))
        errors = g.validate()
        assert len(errors) == 1
        assert "cycle" in errors[0]

    def test_topo_order_raises_on_invalid(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", deps=("b",)),
            _node("b", deps=("a",)),
        ))
        with pytest.raises(ValueError):
            g.topo_order()

    def test_duplicate_task_id_rejected(self):
        with pytest.raises(ValueError):
            TaskGraph(skill_id="s1", nodes=(
                _node("a"), _node("a"),
            ))


# ================================================================
# SingleWriterPolicy
# ================================================================


class TestSingleWriterPolicy:
    def test_acquire_release(self):
        p = SingleWriterPolicy()
        assert p.acquire("tl-1", "skill-A") is True
        assert p.holder("tl-1") == "skill-A"
        p.release("tl-1", "skill-A")
        assert p.holder("tl-1") is None

    def test_same_skill_reentrant(self):
        p = SingleWriterPolicy()
        assert p.acquire("tl-1", "skill-A") is True
        # 同 skill 再次 acquire 允许（重入）
        assert p.acquire("tl-1", "skill-A") is True

    def test_different_skill_blocked(self):
        """红线：同一 timeline 同时至多一个写候选（§6.2 规则 2）。"""
        p = SingleWriterPolicy()
        assert p.acquire("tl-1", "skill-A") is True
        assert p.acquire("tl-1", "skill-B") is False
        assert p.holder("tl-1") == "skill-A"  # 仍是 A

    def test_different_timelines_parallel(self):
        """不同 timeline 可并行（只读检索/分析并行）。"""
        p = SingleWriterPolicy()
        assert p.acquire("tl-1", "skill-A") is True
        assert p.acquire("tl-2", "skill-B") is True

    def test_release_only_by_holder(self):
        p = SingleWriterPolicy()
        p.acquire("tl-1", "skill-A")
        p.release("tl-1", "skill-B")  # 非 holder，无效
        assert p.holder("tl-1") == "skill-A"

    def test_empty_timeline_no_op(self):
        p = SingleWriterPolicy()
        assert p.acquire("", "skill-A") is True  # 无 timeline 作用域
        p.release("", "skill-A")

    def test_assert_subagent_no_write_raises(self):
        """红线：子任务 can_write_timeline=True → PermissionError（§6.1）。"""
        p = SingleWriterPolicy()
        bad_node = TaskNode(
            task_id="t1", role=TaskRole.QA_REVIEWER, can_write_timeline=True,
        )
        with pytest.raises(PermissionError):
            p.assert_subagent_no_write(bad_node)

    def test_assert_subagent_no_write_passes_for_default(self):
        p = SingleWriterPolicy()
        good_node = TaskNode(task_id="t1", role=TaskRole.QA_REVIEWER)
        p.assert_subagent_no_write(good_node)  # 不抛


# ================================================================
# TaskGraphExecutor
# ================================================================


def _exec(role_executor=None) -> TaskGraphExecutor:
    return TaskGraphExecutor(
        role_executor=role_executor or DeterministicRoleExecutor(),
        policy=SingleWriterPolicy(),
    )


class TestTaskGraphExecutor:
    def test_successful_linear_execution(self):
        g = TaskGraph(skill_id="s1", nodes=(
            TaskNode(task_id="creative", role=TaskRole.CREATIVE_DIRECTOR,
                     context=ContextSlice(asset_ids=("a1",))),
            TaskNode(task_id="story", role=TaskRole.STORY_EDITOR,
                     context=ContextSlice(asset_ids=("a1",)),
                     depends_on=("creative",)),
            TaskNode(task_id="qa", role=TaskRole.QA_REVIEWER,
                     context=ContextSlice(asset_ids=("a1",)),
                     depends_on=("story",)),
        ))
        results = _exec().execute(g)
        assert all(r.state == TaskState.SUCCEEDED for r in results)
        # 每个 task 产了 artifact
        artifacts = [r.artifact for r in results if r.artifact]
        assert len(artifacts) == 3
        # 类型匹配 role
        from src.features.edit.ai_editing.task_graph import _artifact_kind_matches
        assert isinstance(artifacts[0], ScriptDraft)
        assert isinstance(artifacts[1], StoryOutline)
        assert isinstance(artifacts[2], QaReport)

    def test_invalid_graph_all_failed(self):
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", deps=("nonexistent",)),
        ))
        results = _exec().execute(g)
        assert all(r.state == TaskState.FAILED for r in results)
        assert "invalid graph" in results[0].error_reason

    def test_cancel_propagation_before_start(self):
        """§6.2 规则 6：父取消 → 未开始的子任务标 CANCELLED。"""
        g = TaskGraph(skill_id="s1", nodes=(
            _node("a", TaskRole.CREATIVE_DIRECTOR),
            _node("b", TaskRole.QA_REVIEWER, deps=("a",)),
        ))
        # 立即取消（任何时刻都返回 True）
        results = _exec().execute(g, cancel_requested=lambda: True)
        assert all(r.state == TaskState.CANCELLED for r in results)

    def test_upstream_failure_cascades_cancel(self):
        """§6.2 规则 5/6：上游失败 → 下游标 CANCELLED（不编造完成）。"""
        class FailingExecutor:
            def execute(self, role, context, upstream):
                if role == TaskRole.STORY_EDITOR:
                    raise RuntimeError("story LLM offline")
                # 用确定性执行器跑其它角色
                return DeterministicRoleExecutor().execute(role, context, upstream)

        g = TaskGraph(skill_id="s1", nodes=(
            TaskNode(task_id="creative", role=TaskRole.CREATIVE_DIRECTOR,
                     context=ContextSlice(asset_ids=("a1",))),
            TaskNode(task_id="story", role=TaskRole.STORY_EDITOR,
                     context=ContextSlice(asset_ids=("a1",)),
                     depends_on=("creative",)),
            TaskNode(task_id="qa", role=TaskRole.QA_REVIEWER,
                     context=ContextSlice(asset_ids=("a1",)),
                     depends_on=("story",)),
        ))
        results = _exec(FailingExecutor()).execute(g)
        by_id = {r.task_id: r for r in results}
        assert by_id["creative"].state == TaskState.SUCCEEDED
        assert by_id["story"].state == TaskState.FAILED
        assert "story LLM offline" in by_id["story"].error_reason
        # qa 因 story 失败被取消
        assert by_id["qa"].state == TaskState.CANCELLED
        assert "upstream" in by_id["qa"].cancel_reason

    def test_artifact_kind_mismatch_fails(self):
        """typed artifact contract（§6.2 规则 4）：role 产物类型不匹配 → FAILED。"""
        class WrongArtifactExecutor:
            def execute(self, role, context, upstream):
                # creative 角色返回 EvidenceSet（类型错）
                return EvidenceSet(set_id="wrong")

        g = TaskGraph(skill_id="s1", nodes=(
            TaskNode(task_id="c", role=TaskRole.CREATIVE_DIRECTOR,
                     context=ContextSlice(asset_ids=("a1",))),
        ))
        results = _exec(WrongArtifactExecutor()).execute(g)
        assert results[0].state == TaskState.FAILED
        assert "kind mismatch" in results[0].error_reason

    def test_subagent_write_rejected(self):
        """红线：can_write_timeline=True 的子任务执行期被拒（§6.1）。"""
        g = TaskGraph(skill_id="s1", nodes=(
            TaskNode(task_id="bad", role=TaskRole.QA_REVIEWER, can_write_timeline=True),
        ))
        results = _exec().execute(g)
        assert results[0].state == TaskState.FAILED
        assert "cannot write timeline" in results[0].error_reason


# ================================================================
# DeterministicRoleExecutor
# ================================================================


class TestDeterministicRoleExecutor:
    def test_creative_director_produces_hooks(self):
        ex = DeterministicRoleExecutor(skill_id="s1")
        result = ex.execute(
            TaskRole.CREATIVE_DIRECTOR,
            ContextSlice(asset_ids=("a1",), role_hint="年轻创作者"),
            (),
        )
        assert isinstance(result, ScriptDraft)
        assert len(result.hooks) >= 1
        assert result.audience == "年轻创作者"
        assert result.produced_by == "creative_director"

    def test_media_librarian_with_evidence(self):
        ex = DeterministicRoleExecutor()
        result = ex.execute(
            TaskRole.MEDIA_LIBRARIAN,
            ContextSlice(asset_ids=("a1",), evidence_refs=("seg-1", "seg-2")),
            (),
        )
        assert isinstance(result, EvidenceSet)
        assert len(result.citations) == 2
        assert all(c.trust == "trusted" for c in result.citations)
        assert result.gaps == ()  # 有证据无 blocking gap

    def test_media_librarian_no_evidence_blocking_gap(self):
        ex = DeterministicRoleExecutor()
        result = ex.execute(
            TaskRole.MEDIA_LIBRARIAN,
            ContextSlice(asset_ids=("a1",)),  # 无 evidence_refs
            (),
        )
        assert isinstance(result, EvidenceSet)
        assert len(result.gaps) == 1
        assert result.gaps[0].severity == "blocking"

    def test_story_editor_consumes_upstream_draft(self):
        ex = DeterministicRoleExecutor()
        upstream = (ScriptDraft(
            draft_id="d1", hooks=(
                __import__("src.features.edit.ai_editing.task_artifacts", fromlist=["HookIdea"]).HookIdea(
                    hook_id="h1", draft_text="定制 hook 文案",
                ),
            ),
            call_to_action="立即下载",
        ),)
        result = ex.execute(
            TaskRole.STORY_EDITOR,
            ContextSlice(asset_ids=("a1",), evidence_refs=("seg-1",)),
            upstream,
        )
        assert isinstance(result, StoryOutline)
        hook_beat = [b for b in result.beats if b.label == "hook"][0]
        assert hook_beat.description == "定制 hook 文案"
        # 有证据的 beat confidence=1.0，无证据的 <1.0
        cta_beat = [b for b in result.beats if b.label == "call_to_action"][0]
        assert cta_beat.confidence < 1.0

    def test_qa_reviewer_flags_blocking_gap(self):
        ex = DeterministicRoleExecutor()
        upstream = (EvidenceSet(
            set_id="e1", gaps=(
                __import__("src.features.edit.ai_editing.task_artifacts", fromlist=["EvidenceGap"]).EvidenceGap(
                    gap_id="g1", severity="blocking", description="缺素材",
                ),
            ),
        ),)
        result = ex.execute(
            TaskRole.QA_REVIEWER,
            ContextSlice(asset_ids=("a1",)),
            upstream,
        )
        assert isinstance(result, QaReport)
        assert result.passes is False  # 有 blocking finding
        assert len(result.blocking_findings) >= 1

    def test_qa_reviewer_passes_no_blocking(self):
        ex = DeterministicRoleExecutor()
        upstream = (EvidenceSet(set_id="e1", citations=(
            __import__("src.features.edit.ai_editing.task_artifacts", fromlist=["EvidenceCitation"]).EvidenceCitation(
                segment_id="s1", trust="trusted",
            ),
        )),)
        result = ex.execute(
            TaskRole.QA_REVIEWER,
            ContextSlice(asset_ids=("a1",)),
            upstream,
        )
        assert isinstance(result, QaReport)
        assert result.passes is True  # 无 blocking

    def test_deterministic_same_input_same_output(self):
        """确定性：相同输入 → 相同输出（便于 eval）。"""
        ex = DeterministicRoleExecutor(skill_id="s1", skill_version="1.0.0")
        ctx = ContextSlice(asset_ids=("a1",), evidence_refs=("seg-1",))
        r1 = ex.execute(TaskRole.MEDIA_LIBRARIAN, ctx, ())
        r2 = ex.execute(TaskRole.MEDIA_LIBRARIAN, ctx, ())
        assert r1.to_dict() == r2.to_dict()

    def test_unknown_role_raises(self):
        ex = DeterministicRoleExecutor()
        # 构造一个非法 role（绕过枚举）：用 monkey 不便，直接测 NotImplement 路径
        # 4 个角色都有实现，故用一个伪造的 role 值
        class FakeRole:
            value = "fake_role"
            produces_kind = "fake"

        with pytest.raises(NotImplementedError):
            ex.execute(FakeRole(), ContextSlice(), ())  # type: ignore[arg-type]
