"""P1.5 批次3：结果卡集成验收（RunRecord → 渲染端到端）。

> 验证 §10.3 P1.5 完成定义的核心场景：
>   skill 执行完 → 消息流出现结构化结果卡（kind 徽标/信任/证据/诊断/错误码）
> 不再是纯文本一行；红线（untrusted 禁 apply / 未审批显示"提议"）UI 真实可见。

覆盖：
- FAILED run → error 卡（error_code 可见）
- SUCCEEDED + EvidenceSet 含 untrusted → evidence 卡，trust=unverified
- SUCCEEDED 无 artifact → 通用完成卡
- CANCELLED run → error 卡（CANCELLED）
- resolver 异常降级 → 不崩
- RUNNING run → 不渲染终态卡
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, RunStatus,
)
from src.features.ai_harness.result_card import ResultCardState
from src.features.ai_harness.runtime import HarnessRuntime
from tests.ui.helpers import find_flow_widget

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# fake typed artifact（与 test_result_card_builder 同形状）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FakeEvidenceCitation:
    segment_id: str = "seg-1"
    trust: str = "trusted"


@dataclass(frozen=True)
class _FakeEvidenceSet:
    set_id: str = "es-1"
    citations: tuple = ()
    evidence_refs: tuple[str, ...] = ()
    summary: str = "证据集"

    @property
    def untrusted_count(self) -> int:
        return sum(1 for c in self.citations if getattr(c, "trust", "") == "untrusted")


@dataclass
class _FakeStore:
    """模拟 ArtifactStore.get。"""
    artifacts: dict = field(default_factory=lambda: {})

    def get(self, aid):
        return self.artifacts.get(aid)


def _make_controller(qapp, *, store=None):
    """构造带真实 runtime 的 controller，store 可选注入。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    service = MagicMock()
    if store is not None:
        service._artifact_store = store
    else:
        service._artifact_store = None
    controller = AiEditController(
        panel=panel, service=service, bridge=MagicMock(),
    )
    runtime = HarnessRuntime()
    controller.runtime = runtime
    return controller, runtime


def _run_to_terminal(runtime, *, success=True, artifacts=(), skill_id="topos.test"):
    """用 runtime 公共 API 创建一个终态 run。"""
    inv = Invocation(
        invocation_id=f"inv-{skill_id}",
        scope=InvocationScope(project_id="p1"),
        skill_id=skill_id,
    )
    run_id = runtime.start(inv)
    runtime.mark_running(run_id)
    for art in artifacts:
        runtime.record_artifact(run_id, art)
    runtime.finish(run_id, success=success, error_code="E1" if not success else "")
    return run_id


# ---------------------------------------------------------------------- #
# 验收1：FAILED run → error 卡
# ---------------------------------------------------------------------- #


class TestFailedRunAcceptance:
    def test_failed_run_shows_error_card(self, qapp):
        """FAILED run → panel 出现 error 卡，error_code 可见。"""
        from src.features.ai_harness.invocation_models import ArtifactRef
        from src.features.edit.ui.ai_result_card import AiResultCard

        controller, runtime = _make_controller(qapp)
        run_id = _run_to_terminal(runtime, success=False)
        before = controller._panel._messages_layout.count()

        controller._record_run_finalization(run_id, summary="失败")

        after = controller._panel._messages_layout.count()
        assert after == before + 1, "应渲染 error 卡"
        # 取最后插入的卡（stretch 前一个）
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)
        # error_code 应体现在卡上（state.error_code）
        assert card._state.error_code == "E1"


# ---------------------------------------------------------------------- #
# 验收2：SUCCEEDED + EvidenceSet 含 untrusted → evidence 卡
# ---------------------------------------------------------------------- #


class TestEvidenceSetAcceptance:
    def test_untrusted_evidence_shows_warning(self, qapp):
        """SUCCEEDED + EvidenceSet 含 OCR untrusted → evidence 卡，trust=unverified。"""
        from src.features.ai_harness.invocation_models import ArtifactRef
        from src.features.edit.ui.ai_result_card import AiResultCard

        store = _FakeStore()
        store.artifacts["es-1"] = _FakeEvidenceSet(
            citations=(_FakeEvidenceCitation("seg-1", trust="untrusted"),),
            evidence_refs=("seg-1",),
        )
        controller, runtime = _make_controller(qapp, store=store)
        run_id = _run_to_terminal(
            runtime, success=True,
            artifacts=(ArtifactRef(
                artifact_id="es-1", artifact_type="evidence",
                summary="证据集", producer_node_id="media_librarian",
            ),),
        )
        before = controller._panel._messages_layout.count()
        controller._record_run_finalization(run_id, summary="完成")
        after = controller._panel._messages_layout.count()

        assert after == before + 1, "应渲染 evidence 卡"
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)
        assert card._state.kind == "evidence"
        assert card._state.trust == "unverified", "含 untrusted → 整体 unverified"


# ---------------------------------------------------------------------- #
# 验收3：SUCCEEDED 无 artifact → 通用完成卡
# ---------------------------------------------------------------------- #


class TestNoArtifactAcceptance:
    def test_succeeded_no_artifact_shows_generic_card(self, qapp):
        """SUCCEEDED 无 artifact → 通用完成卡（不崩）。"""
        from src.features.edit.ui.ai_result_card import AiResultCard

        controller, runtime = _make_controller(qapp)
        run_id = _run_to_terminal(runtime, success=True, artifacts=())
        before = controller._panel._messages_layout.count()
        controller._record_run_finalization(run_id, summary="完成")
        after = controller._panel._messages_layout.count()
        assert after == before + 1
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)
        assert card._state.kind in ("variant", "info")


# ---------------------------------------------------------------------- #
# 验收4：CANCELLED run → error 卡
# ---------------------------------------------------------------------- #


class TestCancelledAcceptance:
    def test_cancelled_run_shows_error_card(self, qapp):
        """CANCELLED run → error 卡（CANCELLED）。"""
        from src.features.edit.ui.ai_result_card import AiResultCard

        controller, runtime = _make_controller(qapp)
        inv = Invocation(
            invocation_id="inv-cancel",
            scope=InvocationScope(project_id="p1"),
            skill_id="topos.test",
        )
        run_id = runtime.start(inv)
        runtime.mark_running(run_id)
        runtime.cancel(run_id)  # 设 cancel_requested
        runtime.finish(run_id, success=False)  # cancel_requested=True → CANCELLED 终态

        before = controller._panel._messages_layout.count()
        controller._record_run_finalization(run_id, summary="已取消")
        after = controller._panel._messages_layout.count()
        assert after == before + 1
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)
        assert card._state.kind == "error"
        assert card._state.error_code == "CANCELLED"


# ---------------------------------------------------------------------- #
# 验收5：resolver 异常降级
# ---------------------------------------------------------------------- #


class TestResolverExceptionAcceptance:
    def test_bad_resolver_does_not_crash(self, qapp):
        """resolver 抛异常 → 不崩，仍渲染（error 或通用兜底）。"""
        from src.features.ai_harness.invocation_models import ArtifactRef
        from src.features.edit.ui.ai_result_card import AiResultCard

        @dataclass
        class _BadStore:
            def get(self, aid):
                raise RuntimeError("corrupted")

        controller, runtime = _make_controller(qapp, store=_BadStore())
        run_id = _run_to_terminal(
            runtime, success=True,
            artifacts=(ArtifactRef(artifact_id="x", artifact_type="script"),),
        )
        before = controller._panel._messages_layout.count()
        # 不应抛异常
        controller._record_run_finalization(run_id, summary="完成")
        after = controller._panel._messages_layout.count()
        assert after == before + 1, "resolver 异常应降级渲染，不崩"
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)


# ---------------------------------------------------------------------- #
# 验收6：RUNNING run 不渲染终态卡
# ---------------------------------------------------------------------- #


class TestRunningNotRendered:
    def test_running_run_does_not_render_card(self, qapp):
        """RUNNING run → 不渲染终态卡（避免误导）。"""
        controller, runtime = _make_controller(qapp)
        inv = Invocation(
            invocation_id="inv-run",
            scope=InvocationScope(project_id="p1"),
            skill_id="topos.test",
        )
        run_id = runtime.start(inv)
        runtime.mark_running(run_id)  # 停在 RUNNING

        before = controller._panel._messages_layout.count()
        controller._record_run_finalization(run_id, summary="运行中")
        after = controller._panel._messages_layout.count()
        assert after == before, "RUNNING 不应渲染终态卡"


# ---------------------------------------------------------------------- #
# 验收7：红线——未审批显示"提议"，apply 按钮 disabled
# ---------------------------------------------------------------------- #


class TestRedLineApprovalGate:
    def test_unapproved_variant_apply_disabled(self, qapp):
        """未审批 variant 卡 → apply 按钮 disabled（红线：不显示成已应用）。"""
        from src.features.ai_harness.invocation_models import ArtifactRef
        from src.features.edit.ui.ai_result_card import AiResultCard
        from src.features.edit.ai_editing.task_artifacts import ScriptDraft

        store = _FakeStore()
        store.artifacts["d1"] = ScriptDraft(draft_id="d1", audience="测试")
        controller, runtime = _make_controller(qapp, store=store)
        run_id = _run_to_terminal(
            runtime, success=True,
            artifacts=(ArtifactRef(
                artifact_id="d1", artifact_type="script", summary="创意方向",
            ),),
        )
        controller._record_run_finalization(run_id, summary="完成")
        card = find_flow_widget(controller, AiResultCard)
        assert isinstance(card, AiResultCard)
        # 默认 approved=False/applied=False（红线）
        assert card._state.approved is False
        assert card._state.applied is False
        # status_label 应体现"未审批"
        assert "未审批" in card._state.status_label or "提议" in card._state.status_label
        # apply 动作应 disabled（approved=False → 禁用）
        apply_action = next(
            (a for a in card._state.available_actions if a.kind == "apply"), None
        )
        assert apply_action is not None, "应有 apply 动作"
        assert apply_action.enabled is False, "未审批 → apply 必须禁用（红线）"
