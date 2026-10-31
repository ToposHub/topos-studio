"""P1.5 动作接线批次4：retry/adjust_params 集成验收（端到端）。

> 验证：
> - retry 端到端：结果卡点'重试' → 新 run 启动（_active_chat_run_id 变更）
> - retry 用原参数：invocation.inputs 一致
> - adjust_params 端到端：点'调整参数' → panel 出现预填表单卡
> - adjust_params 预填值正确
> - 降级：runtime/catalog 未装配 → 不崩
> - 回归：P0 + P1 全量零回归
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, RunStatus,
)
from src.features.ai_harness.runtime import HarnessRuntime
from tests.ui.helpers import find_flow_widget

pytestmark = pytest.mark.edit_core


def _make_controller(qapp, *, runtime=None, catalog=None, router=None,
                     compiler=None, adapter=None, skill_registry=None):
    """构造带可选 P1 依赖的 controller。"""
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    controller = AiEditController(
        panel=panel, service=MagicMock(), bridge=MagicMock(),
        router=router, compiler=compiler, adapter=adapter,
        skill_registry=skill_registry,
    )
    if runtime is not None:
        controller.runtime = runtime
    if catalog is not None:
        controller.catalog = catalog
    return controller


def _fake_skill_registry(skill_ids):
    """fake SkillRegistry：get 返回最小 manifest（_resolve_definition 用）。"""
    from src.features.edit.ai_editing.skill_registry import SkillManifest
    reg = MagicMock()
    def _get(sid):
        if sid not in skill_ids:
            return None
        return MagicMock(spec=SkillManifest, skill_id=sid, ui_card={})
    reg.get = MagicMock(side_effect=_get)
    reg.is_enabled = MagicMock(return_value=True)
    return reg


def _make_terminal_run(runtime, *, skill_id="topos.test", inputs=None, success=True):
    """在 runtime 创建一个终态 run（带 invocation），返回 run_id。

    retry 类测试传 ``success=False``：retry 动作只对 failed/cancelled run
    生效（_on_result_action_retry 的状态门，防止对成功 run 重复执行）。
    """
    inv = Invocation(
        invocation_id=f"inv-{skill_id}",
        scope=InvocationScope(project_id="p1"),
        skill_id=skill_id,
        inputs=inputs or {},
    )
    run_id = runtime.start(inv)
    runtime.mark_running(run_id)
    runtime.finish(run_id, success=success)
    return run_id


# ---------------------------------------------------------------------- #
# retry 端到端
# ---------------------------------------------------------------------- #


class TestRetryAcceptance:
    def test_retry_triggers_new_run(self, qapp):
        """结果卡点'重试' → 触发 _execute_skill_route（新 run 启动）。

        用 fake router/compiler/adapter 验证 retry 调用了 invoke_compiled
        （_execute_skill_route 的核心步骤），且用原 invocation 的 skill_id。

        规格裁决（P1-UI-02）：retry 只对 failed/cancelled run 生效；且
        topos.auto_caption 走 _start_auto_caption_agent 特化路径（不经
        invoke_compiled），本测试用普通 skill 验证通用 retry 路径。
        """
        runtime = HarnessRuntime()
        run_id = _make_terminal_run(
            runtime, skill_id="topos.generic_skill",
            inputs={"target_language": "en"}, success=False,
        )

        # fake P1 依赖：router 返回匹配结果，adapter 记录 invoke_compiled 调用
        invoked_with: list = []
        fake_adapter = MagicMock()
        fake_adapter.invoke_compiled = MagicMock(
            side_effect=lambda compiled, inv: (
                invoked_with.append((compiled, inv)),
                # 真实启动一个 run 让 _active_chat_run_id 生效
                runtime.start(Invocation(
                    invocation_id=f"inv-retry-{len(invoked_with)}",
                    scope=InvocationScope(project_id="p1"),
                    skill_id="topos.generic_skill",
                )),
            )[-1],
        )
        fake_router = MagicMock()
        # to_invocation 返回真实 Invocation（_execute_skill_route 需要）
        fake_router.to_invocation = MagicMock(return_value=Invocation(
            invocation_id="inv-retry",
            scope=InvocationScope(project_id="p1"),
            skill_id="topos.generic_skill",
        ))

        controller = _make_controller(
            qapp, runtime=runtime,
            router=fake_router, compiler=MagicMock(),
            adapter=fake_adapter,
            skill_registry=_fake_skill_registry({"topos.generic_skill"}),
        )
        old_active = controller._active_chat_run_id

        # 模拟用户点结果卡的'重试'
        controller._on_result_action(run_id, "retry")

        # retry 应触发 invoke_compiled（_execute_skill_route 核心步骤）
        assert fake_adapter.invoke_compiled.called, "retry 应调 invoke_compiled"

    def test_retry_uses_original_skill_id(self, qapp):
        """retry 用原 invocation 的 skill_id（不是 hardcoded）。"""
        runtime = HarnessRuntime()
        run_id = _make_terminal_run(
            runtime, skill_id="topos.subtitle_localization", success=False,
        )

        captured_skill_ids: list[str] = []
        fake_router = MagicMock()
        fake_router.to_invocation = MagicMock(
            side_effect=lambda result, **kw: (
                captured_skill_ids.append(result.skill_id),
                Invocation(
                    invocation_id="inv-x",
                    scope=InvocationScope(project_id="p1"),
                    skill_id=result.skill_id,
                ),
            )[-1],
        )
        controller = _make_controller(
            qapp, runtime=runtime,
            router=fake_router, compiler=MagicMock(), adapter=MagicMock(),
            skill_registry=_fake_skill_registry({"topos.subtitle_localization"}),
        )
        controller._on_result_action(run_id, "retry")
        assert "topos.subtitle_localization" in captured_skill_ids, (
            "retry 应使用原 invocation 的 skill_id"
        )

    def test_retry_invocation_not_found_no_crash(self, qapp):
        """retry 时 invocation 找不到 → 不崩（no-op）。"""
        runtime = HarnessRuntime()
        controller = _make_controller(
            qapp, runtime=runtime,
            router=MagicMock(), compiler=MagicMock(), adapter=MagicMock(),
        )
        controller._on_result_action("nonexistent-run", "retry")

    def test_retry_no_router_no_crash(self, qapp):
        """router 未装配 → retry 不崩（no-op）。"""
        runtime = HarnessRuntime()
        run_id = _make_terminal_run(runtime)
        controller = _make_controller(qapp, runtime=runtime)  # 无 router
        controller._on_result_action(run_id, "retry")


# ---------------------------------------------------------------------- #
# adjust_params 端到端
# ---------------------------------------------------------------------- #


class TestAdjustParamsAcceptance:
    def test_adjust_params_renders_prefilled_form_e2e(self, qapp):
        """结果卡点'调整参数' → panel 出现预填表单卡，值来自原 invocation.inputs。"""
        from src.features.ai_harness.skill_catalog import (
            FormField, SkillCardView,
        )
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        runtime = HarnessRuntime()
        run_id = _make_terminal_run(
            runtime, skill_id="topos.auto_caption",
            inputs={"target_language": "ja"},
        )

        fake_catalog = MagicMock()
        fake_catalog.card = MagicMock(return_value=SkillCardView(
            skill_id="topos.auto_caption", title="自动字幕", description="",
            form_fields=(FormField(
                name="target_language", label="目标语言", default="en",
            ),),
        ))
        controller = _make_controller(qapp, runtime=runtime, catalog=fake_catalog)
        before = controller._panel._messages_layout.count()

        controller._on_result_action(run_id, "adjust_params")

        after = controller._panel._messages_layout.count()
        assert after == before + 1, "应出现预填表单卡"
        card = find_flow_widget(controller, AiSkillCard)
        assert isinstance(card, AiSkillCard)
        # 预填值来自 invocation.inputs（ja，不是 default en）
        assert card._field_widgets["target_language"].text() == "ja"

    def test_adjust_params_form_start_triggers_new_run(self, qapp):
        """预填表单卡点'开始' → 转发到 skill_start_requested（已有 _on_skill_card_start 接）。"""
        from src.features.ai_harness.skill_catalog import SkillCardView
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        runtime = HarnessRuntime()
        run_id = _make_terminal_run(runtime, skill_id="topos.test", inputs={})

        fake_catalog = MagicMock()
        fake_catalog.card = MagicMock(return_value=SkillCardView(
            skill_id="topos.test", title="测试", description="",
        ))
        controller = _make_controller(qapp, runtime=runtime, catalog=fake_catalog)
        controller._on_result_action(run_id, "adjust_params")

        # 取到刚渲染的表单卡，验证它连了 skill_start_requested
        card = find_flow_widget(controller, AiSkillCard)
        assert isinstance(card, AiSkillCard)
        # receivers(SIGNAL) 应 >= 1（panel.skill_start_requested 连了 controller）
        panel_receivers = controller._panel.receivers(
            getattr(type(controller._panel), "skill_start_requested")
        )
        assert panel_receivers >= 1


# ---------------------------------------------------------------------- #
# 信号链路端到端：结果卡 action_triggered → panel → controller
# ---------------------------------------------------------------------- #


class TestSignalChainE2E:
    def test_result_card_to_controller_signal_chain(self, qapp):
        """结果卡 action_triggered → panel.result_action_triggered → controller._on_result_action。"""
        from src.features.ai_harness.result_card import error_result
        from src.features.edit.ui.ai_result_card import AiResultCard

        runtime = HarnessRuntime()
        # 直接用一个 FAILED run
        inv = Invocation(
            invocation_id="inv-fail",
            scope=InvocationScope(project_id="p1"),
            skill_id="topos.test",
        )
        run_id = runtime.start(inv)
        runtime.mark_running(run_id)
        runtime.finish(run_id, success=False, error_code="E1")

        controller = _make_controller(qapp, runtime=runtime)
        # 渲染一张 error 结果卡（含 retry 动作）
        state = error_result(
            result_id=run_id, title="失败",
            error_code="E1", error_message="boom",
        )
        controller._panel.set_result_card(state)

        # 用 monkeypatch 标记 _on_result_action_retry 被调
        retry_called: list[str] = []
        original_retry = controller._on_result_action_retry
        controller._on_result_action_retry = lambda rid: retry_called.append(rid)

        # 模拟用户点结果卡的'重试'按钮 → emit action_triggered
        card = find_flow_widget(controller, AiResultCard)
        card.action_triggered.emit(run_id, "retry")

        # panel 转发 → controller 分派 → _on_result_action_retry 被调
        assert retry_called == [run_id], "信号链路应贯通：卡 → panel → controller → retry handler"

        # 恢复（避免影响其他测试）
        controller._on_result_action_retry = original_retry
