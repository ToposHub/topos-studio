"""LlmRoleExecutor 单元测试（plan §10.5 P3.1）。

覆盖：

- 无 model_client → 降级到 DeterministicRoleExecutor + 记录降级。
- 模型返回合法 JSON → 校验成 typed artifact（4 角色各一）；满足
  _artifact_kind_matches 硬类型契约。
- 成本 / token 累计；超 budget 降级。
- 模型异常 / 超时 / 错误 → 降级。
- 坏 JSON / 缺字段 → 校验失败 → 降级。
- fallback_to_deterministic=False 时失败抛错（不静默）。
- 降级计数可见。
- 上下文最小化（不传完整 brief / secret / 绝对路径）。

红线（plan §10.5 P3.1 / §1.3）：

- 子智能体无独立 approval（执行器不持有 grant）。
- 模型输出经校验才接受（外部输入不可信）。
- 失败降级不静默（记录 degradation_counts）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.llm_role_executor import (
    LlmRoleExecutor,
    ModelResponse,
    RoleBudget,
)
from src.features.edit.ai_editing.task_artifacts import (
    EvidenceSet,
    QaReport,
    ScriptDraft,
    StoryOutline,
)
from src.features.edit.ai_editing.task_graph import ContextSlice, TaskRole


# ================================================================
# 测试用 ModelClient
# ================================================================


class _Script:
    def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):
        return ModelResponse(
            text='{"audience":"年轻人","tone":"活泼",'
                 '"hooks":[{"angle":"痛点","draft_text":"hook1","rationale":"r"}],'
                 '"call_to_action":"立即购买"}',
            finish_reason="stop", tokens_used=120, cost_units=0.02,
        )


class _Evidence:
    def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):
        return ModelResponse(
            text='{"citations":[{"segment_id":"s1","asset_id":"a1","kind":"transcript",'
                 '"trust":"trusted","note":"n"}],'
                 '"gaps":[{"description":"缺卖点证据","severity":"warning"}],'
                 '"licensing_notes":"unknown"}',
            finish_reason="stop", tokens_used=80, cost_units=0.01,
        )


class _Story:
    def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):
        return ModelResponse(
            text='{"beats":[{"label":"hook","description":"d","evidence_ref":"s1",'
                 '"confidence":0.9}],"storyboard_hints":"hint"}',
            finish_reason="stop", tokens_used=100, cost_units=0.015,
        )


class _Qa:
    def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):
        return ModelResponse(
            text='{"findings":[{"category":"duration","severity":"info",'
                 '"message":"ok","suggested_action":""}],'
                 '"approved":true,"summary":"通过"}',
            finish_reason="stop", tokens_used=60, cost_units=0.005,
        )


class _Bad:
    def generate(self, **kw):
        return ModelResponse(text="not json {{{", finish_reason="stop")


class _Crash:
    def generate(self, **kw):
        raise RuntimeError("network down")


class _Timeout:
    def generate(self, *, system, user, role, max_tokens=None, timeout_ms=None):
        return ModelResponse(text="", finish_reason="timeout", error="timeout")


class _Markdown:
    """模型输出带 markdown fence。"""
    def generate(self, **kw):
        return ModelResponse(
            text='```json\n{"audience":"x","tone":"y","hooks":[],'
                 '"call_to_action":"z"}\n```',
            finish_reason="stop",
        )


# ================================================================
# 降级路径
# ================================================================


class TestFallback:
    def test_no_model_client_falls_back(self):
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(brief_summary="b"), ())
        assert isinstance(art, ScriptDraft)
        assert "no_model_client" in list(ex.degradation_counts.keys())[0]

    def test_model_exception_falls_back(self):
        ex = LlmRoleExecutor(model_client=_Crash(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.STORY_EDITOR, ContextSlice(), ())
        assert isinstance(art, StoryOutline)
        assert any("model_exception" in k for k in ex.degradation_counts)

    def test_model_timeout_falls_back(self):
        ex = LlmRoleExecutor(model_client=_Timeout(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())
        assert isinstance(art, QaReport)
        assert any("timeout" in k for k in ex.degradation_counts)

    def test_bad_json_falls_back(self):
        ex = LlmRoleExecutor(model_client=_Bad(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())
        assert isinstance(art, QaReport)
        assert "output_validation_failed" in list(ex.degradation_counts.keys())[0]

    def test_fallback_disabled_raises(self):
        """fallback_to_deterministic=False → 失败抛错（不静默）。"""
        ex = LlmRoleExecutor(
            model_client=_Crash(), skill_id="t", skill_version="1",
            fallback_to_deterministic=False,
        )
        with pytest.raises(RuntimeError):
            ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(), ())


# ================================================================
# 模型成功路径 + 类型校验
# ================================================================


class TestModelSuccess:
    def test_creative_director_produces_script_draft(self):
        ex = LlmRoleExecutor(model_client=_Script(), skill_id="t", skill_version="1")
        art = ex.execute(
            TaskRole.CREATIVE_DIRECTOR,
            ContextSlice(brief_summary="b", evidence_refs=("e1",)),
            (),
        )
        assert isinstance(art, ScriptDraft)
        assert art.audience == "年轻人"
        assert art.call_to_action == "立即购买"
        assert len(art.hooks) == 1
        assert art.evidence_refs == ("e1",)
        assert ex.total_cost_units == 0.02
        assert ex.total_tokens == 120

    def test_media_librarian_produces_evidence_set(self):
        ex = LlmRoleExecutor(model_client=_Evidence(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.MEDIA_LIBRARIAN, ContextSlice(), ())
        assert isinstance(art, EvidenceSet)
        assert len(art.citations) == 1
        assert len(art.gaps) == 1

    def test_story_editor_produces_outline(self):
        ex = LlmRoleExecutor(model_client=_Story(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.STORY_EDITOR, ContextSlice(), ())
        assert isinstance(art, StoryOutline)
        assert len(art.beats) == 1
        assert art.beats[0].confidence == 0.9

    def test_qa_reviewer_produces_report(self):
        ex = LlmRoleExecutor(model_client=_Qa(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())
        assert isinstance(art, QaReport)
        assert art.approved is True
        assert len(art.findings) == 1

    def test_markdown_fence_parsed(self):
        """模型输出带 ```json fence 也能解析。"""
        ex = LlmRoleExecutor(model_client=_Markdown(), skill_id="t", skill_version="1")
        art = ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(), ())
        assert isinstance(art, ScriptDraft)
        assert art.audience == "x"


# ================================================================
# 预算守门
# ================================================================


class TestBudgetEnforcement:
    def test_cost_budget_exceeded_falls_back(self):
        ex = LlmRoleExecutor(
            model_client=_Script(), skill_id="t", skill_version="1",
            role_budgets={TaskRole.CREATIVE_DIRECTOR: RoleBudget(max_cost_units=0.01)},
        )
        # _Script cost=0.02 > 0.01 budget → 降级
        art = ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(), ())
        assert isinstance(art, ScriptDraft)  # 降级产物仍是 ScriptDraft
        assert any("budget_cost_exceeded" in k for k in ex.degradation_counts)

    def test_budget_not_exceeded_succeeds(self):
        ex = LlmRoleExecutor(
            model_client=_Script(), skill_id="t", skill_version="1",
            role_budgets={TaskRole.CREATIVE_DIRECTOR: RoleBudget(max_cost_units=1.0)},
        )
        art = ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(), ())
        assert isinstance(art, ScriptDraft)
        assert art.audience == "年轻人"  # 模型产物（非降级）
        assert ex.degradation_counts == {}


# ================================================================
# 红线：执行器不持有 approval / 不写时间线
# ================================================================


class TestRedLines:
    def test_executor_has_no_approval_field(self):
        """plan §10.5：子智能体无独立 approval。"""
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
        # 无 approval / grant / approval_service 字段
        for forbidden in ("approval", "grant", "approval_service", "approval_token"):
            assert not hasattr(ex, forbidden), f"LlmRoleExecutor 不应有 {forbidden}"

    def test_executor_has_no_timeline_write_method(self):
        """执行器不写时间线。"""
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
        for forbidden in ("write_timeline", "commit", "apply", "insert_clip"):
            assert not callable(getattr(ex, forbidden, None)), \
                f"LlmRoleExecutor 不应有 {forbidden} 方法"

    def test_minimal_context_no_secret_passthrough(self):
        """最小 ContextSlice 不含 secret / 绝对路径（ContextSlice 本就不带这些）。"""
        from src.features.edit.ai_editing.task_graph import ContextSlice
        fields = {f for f in dir(ContextSlice) if not f.startswith("_")}
        for forbidden in ("secret", "api_key", "password", "absolute_path"):
            assert not any(forbidden in f.lower() for f in fields), \
                f"ContextSlice 不应含 {forbidden}"


# ================================================================
# 降级计数可见（plan §10.5：失败可见，不静默）
# ================================================================


class TestDegradationVisibility:
    def test_degradation_counts_accumulate(self):
        ex = LlmRoleExecutor(model_client=_Bad(), skill_id="t", skill_version="1")
        ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())
        ex.execute(TaskRole.QA_REVIEWER, ContextSlice(), ())
        assert sum(ex.degradation_counts.values()) == 2

    def test_successful_run_no_degradation(self):
        ex = LlmRoleExecutor(model_client=_Script(), skill_id="t", skill_version="1")
        ex.execute(TaskRole.CREATIVE_DIRECTOR, ContextSlice(), ())
        assert ex.degradation_counts == {}
