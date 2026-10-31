"""P1.5 批次2：结果卡渲染骨架接线测试（panel.set_result_card + controller 聚合）。

> 验证：
> - panel.set_result_card 把 ResultCardState 渲染成 AiResultCard 插进消息流
> - controller.result_card_ready signal 存在并连接到 panel.set_result_card
> - controller._make_artifact_resolver 从 service._artifact_store 构造闭包
> - run 终态后 controller 自动聚合 ResultCardState 并 emit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.invocation_models import RunRecord, RunStatus
from src.features.ai_harness.result_card import ResultCardState, error_result
from tests.ui.helpers import find_flow_widget

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# panel.set_result_card
# ---------------------------------------------------------------------- #


class TestPanelSetResultCard:
    def test_set_result_card_none_does_not_crash(self, qapp):
        """set_result_card(None) → 不崩，消息流不变。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        panel = AiEditAssistantPanel()
        before = panel._messages_layout.count()
        panel.set_result_card(None)
        assert panel._messages_layout.count() == before, "None 不应改变消息流"

    def test_set_result_card_adds_ai_result_card(self, qapp):
        """set_result_card(error_state) → 消息流多一张 AiResultCard。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from src.features.edit.ui.ai_result_card import AiResultCard
        panel = AiEditAssistantPanel()
        before = panel._messages_layout.count()
        state = error_result(
            result_id="r1", title="失败",
            error_code="E1", error_message="boom",
        )
        panel.set_result_card(state)
        after = panel._messages_layout.count()
        assert after == before + 1, "应多一张卡"
        # 最后一个非 stretch widget 应是 AiResultCard
        card = find_flow_widget(panel, AiResultCard)
        assert isinstance(card, AiResultCard), f"应是 AiResultCard，实际 {type(card)}"

    def test_panel_has_set_result_card_method(self, qapp):
        """AiEditAssistantPanel 必须有 set_result_card 方法。"""
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        assert hasattr(AiEditAssistantPanel, "set_result_card"), (
            "panel 必须有 set_result_card 方法"
        )


# ---------------------------------------------------------------------- #
# controller.result_card_ready signal
# ---------------------------------------------------------------------- #


def _make_controller(qapp, *, service=None):
    """构造带最小依赖的 AiEditController（fake service/bridge）。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    # 用 MagicMock 作 service（避免 AssistantService 的复杂依赖；_artifact_store
    # 默认 MagicMock 自动属性，测试需要时显式覆盖）。
    service = service or MagicMock()
    bridge = MagicMock()
    return AiEditController(panel=panel, service=service, bridge=bridge)


class TestControllerResultCardSignal:
    def test_controller_has_result_card_ready_signal(self, qapp):
        """controller 有 result_card_ready signal（pyqtSignal(object)）。"""
        controller = _make_controller(qapp)
        assert hasattr(controller, "result_card_ready"), (
            "controller 必须有 result_card_ready signal"
        )

    def test_result_card_ready_connected_to_panel_set_result_card(self, qapp):
        """result_card_ready signal 连接到 panel.set_result_card。"""
        controller = _make_controller(qapp)
        # emit state → panel 应出现一张卡
        state = error_result(
            result_id="r1", title="失败",
            error_code="E1", error_message="boom",
        )
        before = controller._panel._messages_layout.count()
        controller.result_card_ready.emit(state)
        after = controller._panel._messages_layout.count()
        assert after == before + 1, "emit 后 panel 应多一张卡（signal 已连接）"


# ---------------------------------------------------------------------- #
# _make_artifact_resolver
# ---------------------------------------------------------------------- #


class TestArtifactResolver:
    def test_no_artifact_store_returns_none(self, qapp):
        """service._artifact_store 为 None → _make_artifact_resolver 返回 None。"""
        controller = _make_controller(qapp)
        # 显式置 None（模拟 AssistantService 默认未注入 store）
        controller._service._artifact_store = None
        assert controller._make_artifact_resolver() is None

    def test_with_store_returns_callable(self, qapp):
        """service 有 store → 返回闭包，调它能取 typed artifact。"""
        from src.features.edit.ai_editing.task_artifacts import ScriptDraft

        @dataclass
        class _FakeStore:
            artifacts: dict = field(default_factory=lambda: {"a1": ScriptDraft(draft_id="a1")})
            def get(self, aid):
                return self.artifacts.get(aid)

        controller = _make_controller(qapp)
        controller._service._artifact_store = _FakeStore()
        resolver = controller._make_artifact_resolver()
        assert resolver is not None
        assert callable(resolver)
        result = resolver("a1")
        assert result is not None and result.draft_id == "a1"

    def test_resolver_swallows_exceptions(self, qapp):
        """resolver 内部异常被吞（返回 None，不崩）。"""
        @dataclass
        class _BadStore:
            def get(self, aid):
                raise RuntimeError("corrupted")

        controller = _make_controller(qapp)
        controller._service._artifact_store = _BadStore()
        resolver = controller._make_artifact_resolver()
        assert resolver("anything") is None, "异常应返回 None"


# ---------------------------------------------------------------------- #
# 端到端：run 终态 → panel 出现结果卡
# ---------------------------------------------------------------------- #


class TestRunFinalizationToResultCard:
    def test_failed_run_emits_result_card(self, qapp):
        """controller._record_run_finalization(FAILED run) → panel 出现 error 卡。

        用真实 runtime + 真实 builder，验证端到端聚合 → emit → 渲染。
        """
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.invocation_models import (
            Invocation, InvocationScope,
        )

        controller = _make_controller(qapp)
        runtime = HarnessRuntime()
        controller.runtime = runtime
        before = controller._panel._messages_layout.count()

        # 用 runtime 公共 API 创建一个 FAILED run（start → mark_running → finish）
        inv = Invocation(
            invocation_id="inv1", scope=InvocationScope(project_id="p1"),
            skill_id="topos.test",
        )
        run_id = runtime.start(inv)
        runtime.mark_running(run_id)
        runtime.finish(
            run_id, success=False,
            error_code="WORKFLOW_NO_OUTPUT", error_message="no output",
        )
        controller._active_chat_run_id = run_id
        controller._record_run_finalization(run_id, summary="测试失败")

        after = controller._panel._messages_layout.count()
        assert after == before + 1, "FAILED run 应触发结果卡渲染"
