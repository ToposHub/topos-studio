"""P1.1 测试：panel 渲染入口（help/clarify/skill 推荐卡）+ bridge scope inputs。

> 验证 §10.3 P1 在 UI 侧的契约：
> - AiEditAssistantPanel 加 set_help_answer / set_clarification / set_skill_recommendation
> - panel 加 skill_start_requested signal（转发 AiSkillGallery.start_requested）
> - AiEditHostBridge 协议加 get_selected_asset_ids / get_project_id（供 scope 构造）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QLabel

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# bridge 协议新方法
# ---------------------------------------------------------------------- #


def test_bridge_protocol_has_get_selected_asset_ids():
    """AiEditHostBridge 协议声明 get_selected_asset_ids（供 scope 构造）。"""
    from src.app.controllers.ai_edit_host_bridge import AiEditHostBridge
    assert hasattr(AiEditHostBridge, "get_selected_asset_ids"), (
        "AiEditHostBridge 协议必须声明 get_selected_asset_ids"
    )


def test_bridge_protocol_has_get_project_id():
    """AiEditHostBridge 协议声明 get_project_id。"""
    from src.app.controllers.ai_edit_host_bridge import AiEditHostBridge
    assert hasattr(AiEditHostBridge, "get_project_id"), (
        "AiEditHostBridge 协议必须声明 get_project_id"
    )


def test_null_bridge_get_selected_asset_ids_returns_empty():
    """NullAiEditHostBridge.get_selected_asset_ids 返回空 tuple（确定状态）。"""
    from src.app.controllers.ai_edit_host_bridge import NullAiEditHostBridge
    bridge = NullAiEditHostBridge()
    assert bridge.get_selected_asset_ids() == ()


def test_null_bridge_get_project_id_returns_empty():
    """NullAiEditHostBridge.get_project_id 返回空串（确定状态）。"""
    from src.app.controllers.ai_edit_host_bridge import NullAiEditHostBridge
    bridge = NullAiEditHostBridge()
    assert bridge.get_project_id() == ""


# ---------------------------------------------------------------------- #
# panel 渲染入口
# ---------------------------------------------------------------------- #


def test_panel_has_skill_start_requested_signal(qapp):
    """panel 声明 skill_start_requested signal（转发 AiSkillGallery.start_requested）。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    assert hasattr(AiEditAssistantPanel, "skill_start_requested"), (
        "panel 必须声明 skill_start_requested signal"
    )


def test_panel_set_help_answer_renders_card(qapp):
    """set_help_answer 后 messages_layout 出现 help answer card。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    from src.features.ai_harness.help_assistant import HelpAnswer, HelpAnswerItem

    panel = AiEditAssistantPanel()
    try:
        answer = HelpAnswer(
            query="怎么做关键帧",
            items=(HelpAnswerItem(
                entry_id="kf-1", title="关键帧动画",
                body="在时间线上双击添加关键帧", page_ref="animation",
            ),),
            found=True, index_empty=False,
        )
        panel.set_help_answer(answer)
        # messages_layout 应含 help card（layout count 增加）
        assert panel._messages_layout.count() > 1
    finally:
        panel.deleteLater()


def test_panel_set_help_answer_empty_no_crash(qapp):
    """set_help_answer 空/None 时不抛错。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    from src.features.ai_harness.help_assistant import HelpAnswer

    panel = AiEditAssistantPanel()
    try:
        panel.set_help_answer(HelpAnswer(query="x", items=(), found=False, index_empty=True))
        panel.set_help_answer(None)  # type: ignore[arg-type]
    except Exception:
        pytest.fail("set_help_answer 空/None 不应抛错")
    finally:
        panel.deleteLater()


def test_panel_component_failure_is_visible_and_logged(qapp, monkeypatch, caplog):
    """Optional card failures must not look like a successful no-op."""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    from src.features.edit.ui.ai_help_answer import AiHelpAnswerCard
    from src.features.ai_harness.help_assistant import HelpAnswer

    def fail_set_answer(self, answer):  # noqa: ANN001 - deliberate failure stub
        raise RuntimeError("synthetic card failure")

    monkeypatch.setattr(AiHelpAnswerCard, "set_answer", fail_set_answer)
    panel = AiEditAssistantPanel()
    try:
        with caplog.at_level("WARNING"):
            panel.set_help_answer(
                HelpAnswer(query="x", items=(), found=False, index_empty=True)
            )
        error_cards = panel.findChildren(QLabel, "assistant_component_error")
        assert error_cards, "组件装配失败必须在消息流显示错误卡"
        assert "帮助答案" in error_cards[0].text()
        assert "AI assistant component 帮助答案 failed" in caplog.text
    finally:
        panel.deleteLater()


def test_panel_set_skill_recommendation_renders_gallery(qapp):
    """set_skill_recommendation 后出现 AiSkillGallery 含卡片。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel
    from src.features.edit.ui.ai_skill_card import AiSkillGallery
    from src.features.ai_harness.skill_catalog import SkillCatalogView
    from src.features.edit.ai_editing.skill_registry import SkillRegistry
    from src.features.ai_harness.editorial_skills import register_p2_skills

    reg = SkillRegistry()
    register_p2_skills(reg)
    catalog = SkillCatalogView(reg)
    cards = catalog.cards()

    panel = AiEditAssistantPanel()
    try:
        panel.set_skill_recommendation(cards)
        # 应能在 panel 找到 AiSkillGallery
        galleries = panel.findChildren(AiSkillGallery)
        assert len(galleries) >= 1
        assert galleries[0].cards  # 含卡片
    finally:
        panel.deleteLater()


def test_panel_set_skill_recommendation_empty_no_crash(qapp):
    """set_skill_recommendation 空列表不抛错。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    try:
        panel.set_skill_recommendation([])
    except Exception:
        pytest.fail("set_skill_recommendation 空列表不应抛错")
    finally:
        panel.deleteLater()


def test_panel_set_clarification_no_crash(qapp):
    """set_clarification 不抛错（最小可用：显示问题文本）。"""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    panel = AiEditAssistantPanel()
    try:
        panel.set_clarification("请选择目标语言：", "target_language")
        # messages_layout 应有内容（问题显示）
        assert panel._messages_layout.count() > 1
    finally:
        panel.deleteLater()
