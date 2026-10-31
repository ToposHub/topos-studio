"""P1.5 动作接线批次3：adjust_params（预填表单重跑）。

> 验证：
> - AiSkillCard.set_form_values 预填 SpinBox/ComboBox/LineEdit
> - panel.set_skill_form(card_view, values) → 消息流出现预填表单卡
> - controller._on_result_action_adjust_params：反查 invocation + catalog.card → 预填卡
> - invocation/catalog 缺失 → 不崩
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tests.ui.helpers import find_flow_widget

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# AiSkillCard.set_form_values
# ---------------------------------------------------------------------- #


def _make_card_view(fields):
    """构造含 form_fields 的 SkillCardView fake。"""
    from src.features.ai_harness.skill_catalog import SkillCardView
    return SkillCardView(
        skill_id="topos.test", title="测试 Skill", description="测试",
        form_fields=tuple(fields),
    )


class TestSetFormValues:
    def test_prefill_integer_field(self, qapp):
        """set_form_values 预填 integer SpinBox。"""
        from PyQt6.QtWidgets import QSpinBox
        from src.features.ai_harness.skill_catalog import FormField
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        card = AiSkillCard(_make_card_view([
            FormField(name="duration", label="时长", field_type="integer", default=10),
        ]))
        card.set_form_values({"duration": 30})
        w = card._field_widgets["duration"]
        assert isinstance(w, QSpinBox)
        assert w.value() == 30

    def test_prefill_string_field(self, qapp):
        """set_form_values 预填 string LineEdit。"""
        from PyQt6.QtWidgets import QLineEdit
        from src.features.ai_harness.skill_catalog import FormField
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        card = AiSkillCard(_make_card_view([
            FormField(name="lang", label="语言", field_type="string", default="en"),
        ]))
        card.set_form_values({"lang": "zh"})
        w = card._field_widgets["lang"]
        assert isinstance(w, QLineEdit)
        assert w.text() == "zh"

    def test_prefill_enum_field(self, qapp):
        """set_form_values 预填 enum ComboBox。"""
        from PyQt6.QtWidgets import QComboBox
        from src.features.ai_harness.skill_catalog import FormField
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        card = AiSkillCard(_make_card_view([
            FormField(
                name="style", label="风格", field_type="enum",
                default="formal", options=("formal", "casual"),
            ),
        ]))
        card.set_form_values({"style": "casual"})
        w = card._field_widgets["style"]
        assert isinstance(w, QComboBox)
        assert w.currentText() == "casual"

    def test_prefill_ignores_unknown_keys(self, qapp):
        """values 含未知 key → 不崩，已知字段正常预填。"""
        from src.features.ai_harness.skill_catalog import FormField
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        card = AiSkillCard(_make_card_view([
            FormField(name="x", label="X", field_type="integer", default=1),
        ]))
        # 不应抛异常
        card.set_form_values({"unknown_key": 99, "x": 5})
        assert card._field_widgets["x"].value() == 5

    def test_prefill_none_value_skipped(self, qapp):
        """values 中某 key 为 None → 跳过该字段（保留 default）。"""
        from src.features.ai_harness.skill_catalog import FormField
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        card = AiSkillCard(_make_card_view([
            FormField(name="x", label="X", field_type="integer", default=7),
        ]))
        card.set_form_values({"x": None})
        assert card._field_widgets["x"].value() == 7, "None 值应跳过，保留 default"


# ---------------------------------------------------------------------- #
# panel.set_skill_form
# ---------------------------------------------------------------------- #


class TestPanelSetSkillForm:
    def test_set_skill_form_adds_card(self, qapp):
        """panel.set_skill_form(card_view, values) → 消息流出现一张 AiSkillCard。"""
        from src.features.ai_harness.skill_catalog import (
            FormField, SkillCardView,
        )
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        panel = AiEditAssistantPanel()
        before = panel._messages_layout.count()
        card_view = SkillCardView(
            skill_id="topos.test", title="测试", description="",
            form_fields=(FormField(name="lang", label="语言", default="en"),),
        )
        panel.set_skill_form(card_view, {"lang": "zh"})
        after = panel._messages_layout.count()
        assert after == before + 1
        card = find_flow_widget(panel, AiSkillCard)
        assert isinstance(card, AiSkillCard)
        # 预填值生效
        assert card._field_widgets["lang"].text() == "zh"

    def test_set_skill_form_without_values(self, qapp):
        """set_skill_form 不传 values → 用 default（不崩）。"""
        from src.features.ai_harness.skill_catalog import (
            FormField, SkillCardView,
        )
        from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
        from src.features.edit.ui.ai_skill_card import AiSkillCard

        panel = AiEditAssistantPanel()
        card_view = SkillCardView(
            skill_id="topos.test", title="测试", description="",
            form_fields=(FormField(name="lang", label="语言", default="en"),),
        )
        panel.set_skill_form(card_view)  # 不传 values
        card = find_flow_widget(panel, AiSkillCard)
        assert isinstance(card, AiSkillCard)


# ---------------------------------------------------------------------- #
# controller._on_result_action_adjust_params 端到端
# ---------------------------------------------------------------------- #


def _make_controller(qapp, *, runtime=None, catalog=None):
    from src.app.controllers.ai_edit_controller import AiEditController
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    controller = AiEditController(
        panel=panel, service=MagicMock(), bridge=MagicMock(),
    )
    if runtime is not None:
        controller.runtime = runtime
    if catalog is not None:
        controller.catalog = catalog
    return controller


class TestControllerAdjustParams:
    def test_adjust_params_renders_prefilled_form(self, qapp):
        """_on_result_action_adjust_params → panel 出现预填表单卡。"""
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.invocation_models import (
            Invocation, InvocationScope,
        )
        from src.features.ai_harness.skill_catalog import (
            FormField, SkillCardView,
        )

        # fake runtime 装一个 invocation（含 skill_id + inputs）
        runtime = HarnessRuntime()
        inv = Invocation(
            invocation_id="inv1", scope=InvocationScope(project_id="p1"),
            skill_id="topos.test", inputs={"lang": "zh"},
        )
        # 用 start 让 runtime 缓存 invocation
        run_id = runtime.start(inv)

        # fake catalog.card(skill_id) 返回带 form_fields 的 SkillCardView
        fake_catalog = MagicMock()
        fake_catalog.card = MagicMock(return_value=SkillCardView(
            skill_id="topos.test", title="测试", description="",
            form_fields=(FormField(name="lang", label="语言", default="en"),),
        ))

        controller = _make_controller(qapp, runtime=runtime, catalog=fake_catalog)
        before = controller._panel._messages_layout.count()
        controller._on_result_action_adjust_params(run_id)
        after = controller._panel._messages_layout.count()

        assert after == before + 1, "应渲染预填表单卡"
        # 预填值来自 invocation.inputs
        from src.features.edit.ui.ai_skill_card import AiSkillCard
        card = find_flow_widget(controller, AiSkillCard)
        assert isinstance(card, AiSkillCard)
        assert card._field_widgets["lang"].text() == "zh"

    def test_adjust_params_invocation_not_found_no_crash(self, qapp):
        """invocation 找不到 → 不崩（no-op）。"""
        from src.features.ai_harness.runtime import HarnessRuntime

        runtime = HarnessRuntime()
        controller = _make_controller(qapp, runtime=runtime, catalog=MagicMock())
        # run_id 不存在 → get_invocation 返回 None
        controller._on_result_action_adjust_params("nonexistent-run")
        # 不崩即通过

    def test_adjust_params_no_runtime_no_crash(self, qapp):
        """runtime 未装配 → 不崩（no-op）。"""
        controller = _make_controller(qapp, catalog=MagicMock())
        # runtime 默认 None（_make_controller 没传 runtime）
        controller._on_result_action_adjust_params("run-1")

    def test_adjust_params_card_not_found_no_crash(self, qapp):
        """catalog.card 返回 None → 不崩（no-op）。"""
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.invocation_models import (
            Invocation, InvocationScope,
        )

        runtime = HarnessRuntime()
        inv = Invocation(
            invocation_id="inv1", scope=InvocationScope(project_id="p1"),
            skill_id="topos.unknown",
        )
        run_id = runtime.start(inv)

        fake_catalog = MagicMock()
        fake_catalog.card = MagicMock(return_value=None)  # skill 不存在

        controller = _make_controller(qapp, runtime=runtime, catalog=fake_catalog)
        controller._on_result_action_adjust_params(run_id)
        # 不崩即通过
