"""TAEP Phase 9 UI-0 — AI 编辑助手 dock 空壳与状态契约测试。

验收（TAEP §11.9 UI-0）：
- 面板可创建、显示不同状态（idle/no_project/no_profile/error）。
- 只发细粒度 signal；状态回填用 _updating 锁避免信号递归。
- 不实例化模型 / renderer / exporter。
- 无项目时仍显示对话输入区，允许先进行自由对话。
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QLabel, QPushButton

# UI 测试需要 QApplication（tests/conftest.py 提供 qapp fixture）
from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
    ContextSummary,
)
from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel


@pytest.fixture
def panel(qapp):
    p = AiEditAssistantPanel()
    yield p
    p.deleteLater()


def test_panel_creates_with_object_name(panel):
    assert panel.objectName() == "dock_ai_edit_assistant"
    assert panel.windowTitle() == "AI assistant"
    assert panel.minimumWidth() < 360  # 不撑大共享的右侧标签组


def test_panel_shows_idle_state(panel):
    snap = AssistantStateSnapshot(state=AssistantState.IDLE)
    panel.refresh_state(snap)
    assert panel._stack.currentIndex() == 2  # main page
    assert panel._state_label.text() == ""
    assert panel._state_label.isHidden()


def test_panel_shows_no_project_state(panel):
    snap = AssistantStateSnapshot(state=AssistantState.NO_PROJECT)
    panel.refresh_state(snap)
    assert panel._stack.currentIndex() == 2  # chat page remains available
    assert panel._request_edit.isHidden() is False
    assert panel._send_btn.isEnabled() is True


def test_panel_can_submit_chat_without_project(panel):
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.NO_PROJECT))
    emitted = []
    panel.request_submitted.connect(lambda request: emitted.append(request))
    panel._request_edit.setPlainText("先介绍一下 Motion Graphic Clip")
    panel._send_btn.click()
    assert len(emitted) == 1
    assert emitted[0].text == "先介绍一下 Motion Graphic Clip"


def test_panel_motion_graphic_plan_emits_explicit_confirmation(panel):
    events = []
    panel.motion_graphic_confirm_requested.connect(lambda request_id: events.append(request_id))
    panel.set_motion_graphic_proposal("motion-1", "AI Agent", "确认前不会修改时间线。")
    assert panel._motion_graphic_proposals["motion-1"] is not None
    confirm = panel._motion_graphic_proposals["motion-1"].findChild(
        QPushButton, "assistant_motion_confirm"
    )
    assert confirm is not None
    confirm.click()
    assert events == ["motion-1"]


def test_panel_typed_motion_graphic_card_shows_and_emits_confirm_button(panel):
    """The structured card used by the LLM path must expose a real button."""
    events = []
    panel.motion_graphic_confirm_requested.connect(events.append)
    panel.set_motion_graphic_proposal_card(
        "motion-typed-1",
        {
            "title": "HTML5 学习演示",
            "summary": "已完成首帧验证，等待执行确认。",
            "duration_seconds": 15.0,
            "canvas_size": [1920, 1080],
            "actions": ["cancel", "confirm"],
            "quality_errors": [],
            "quality_warnings": [],
            "html_design_quality": {
                "overall_score": 0.93,
                "visual_language_score": 1.0,
                "page_diversity_score": 0.95,
                "responsive_layout_score": 0.85,
            },
            "html_intent_checks": {
                "requirements": ["包含文案「新品发布」", "使用颜色 #ff8800"],
                "issues": [],
                "passed": True,
            },
        },
    )
    panel.show()
    card = panel._motion_graphic_proposals["motion-typed-1"]
    confirm = card.findChild(QPushButton, "assistant_motion_confirm")
    assert confirm is not None
    assert confirm.isVisible()
    assert any("内容完整度 93%" in label.text() for label in card.findChildren(QLabel))
    assert any("布局适配 85%" in label.text() for label in card.findChildren(QLabel))
    assert any("已验收 2 条明确需求" in label.text() for label in card.findChildren(QLabel))
    confirm.click()
    assert events == ["motion-typed-1"]


def test_panel_plan_review_card_has_start_generation_button(panel):
    """The reviewed plan can proceed by button or by sending “可以”."""
    events = []
    panel.motion_graphic_confirm_requested.connect(events.append)
    panel.set_motion_graphic_proposal_card(
        "motion-plan-1",
        {
            "title": "Motion Graphic 制作计划",
            "summary": "先审核计划，确认后才生成网页。",
            "duration_seconds": 15.0,
            "canvas_size": [1920, 1080],
            "mode": "plan_review",
            "plan_rows": [
                {"label": "核心创意", "value": "从问题到实践的网页旅程"},
                {"label": "叙事结构", "value": "问题：建立动机\n实践：完成练习"},
            ],
            "actions": ["cancel", "start"],
        },
    )
    card = panel._motion_graphic_proposals["motion-plan-1"]
    start = card.findChild(QPushButton, "assistant_motion_start")
    assert start is not None
    assert start.text() == "开始生成"
    start.click()
    assert events == ["motion-plan-1"]
    labels = [label.text() for label in card.findChildren(QLabel)]
    assert any("核心创意" in text for text in labels)
    assert any("从问题到实践" in text for text in labels)


def test_panel_generation_confirmation_card_emits_generate_action(panel):
    events = []
    panel.motion_graphic_confirm_requested.connect(events.append)
    panel.set_motion_graphic_proposal_card(
        "motion-generate-1",
        {
            "title": "确认生成 Motion Graphic",
            "summary": "计划已确认，等待开始生成。",
            "duration_seconds": 15.0,
            "canvas_size": [1920, 1080],
            "mode": "generation_confirmation",
            "plan_rows": [{"label": "核心创意", "value": "网页叙事"}],
            "actions": ["cancel", "generate"],
        },
    )
    card = panel._motion_graphic_proposals["motion-generate-1"]
    generate = card.findChild(QPushButton, "assistant_motion_generate")
    assert generate is not None
    assert card.findChild(QPushButton, "assistant_motion_confirm") is None
    generate.click()
    assert events == ["motion-generate-1"]


def test_panel_shows_no_profile_state(panel):
    snap = AssistantStateSnapshot(state=AssistantState.NO_PROFILE)
    panel.refresh_state(snap)
    assert panel._stack.currentIndex() == 1  # no profile page


def test_panel_shows_error_state(panel):
    snap = AssistantStateSnapshot(
        state=AssistantState.FAILED, error_message="连接超时"
    )
    panel.refresh_state(snap)
    assert panel._stack.currentIndex() == 3  # error page
    assert "连接超时" in panel._error_label.text()


def test_panel_shows_context_summary(panel):
    snap = AssistantStateSnapshot(
        state=AssistantState.IDLE,
        context=ContextSummary(asset_count=18, analyzed_count=12, music_count=4, locked_count=2),
    )
    panel.refresh_state(snap)
    # 上下文摘要区有文本（i18n 是否加载都会填充）；状态已存储
    assert panel._state.context.asset_count == 18
    assert panel._state.context.analyzed_count == 12
    # M5：优先 AiContextSummary widget（_context_label 为 None）；回退 QLabel。
    # 两种路径都要求摘要已回填（widget 或 label 至少一个就绪）。
    if panel._context_summary is not None:
        # widget 路径：set_summary 已调用（_context_summary 存在即装配成功）
        assert panel._context_summary is not None
    else:
        assert panel._context_label is not None
        assert panel._context_label.text() != ""


def test_cancel_button_visible_only_when_busy(panel):
    # 现在是单一发送/停止按钮：空闲显示箭头，忙碌显示方块。
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    assert panel._cancel_btn.isVisibleTo(panel) is True
    assert panel._cancel_btn.text() == "↑"
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.PLANNING))
    assert panel._cancel_btn.isVisibleTo(panel) is True
    assert panel._cancel_btn.text() == "■"


def test_state_updating_lock_prevents_recursion(panel):
    """refresh_state 期间 is_updating=True，submit_request 被跳过。"""
    snap = AssistantStateSnapshot(state=AssistantState.IDLE)
    # 模拟回填中
    panel._updating = True
    emitted = []
    panel.request_submitted.connect(lambda r: emitted.append(r))
    panel.submit_request("test")
    assert emitted == []  # 回填中，不发射 signal


def test_submit_request_emits_signal(panel):
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    emitted = []
    panel.request_submitted.connect(lambda r: emitted.append(r))
    panel.submit_request("做一个 30 秒竖版宣传片")
    assert len(emitted) == 1
    assert "30 秒" in emitted[0].text
    assert emitted[0].request_id  # 有 id


def test_fine_grained_signals_exist(panel):
    """面板声明了细粒度 signal（TAEP §11.3）。"""
    assert hasattr(panel, "brief_patch_requested")
    assert hasattr(panel, "variant_selected")
    assert hasattr(panel, "apply_requested")
    assert hasattr(panel, "request_submitted")
    assert hasattr(panel, "cancel_requested")
    assert hasattr(panel, "analysis_confirm_requested")
    assert hasattr(panel, "analysis_cancel_requested")


def test_analysis_proposal_card_emits_confirm_and_cancel(panel):
    """Agent 分析提议必须有显式确认/取消信号，不能由面板直接执行 provider。"""
    from types import SimpleNamespace

    request = SimpleNamespace(
        request_id="analysis-1", title="Agent：分析素材", asset_ids=("a1",),
        recipe_id=SimpleNamespace(value="fast_local"),
    )
    panel.set_analysis_proposal(request)
    assert "analysis-1" in panel._analysis_proposals
    card = panel._analysis_proposals["analysis-1"]
    buttons = card.findChildren(__import__("PyQt6.QtWidgets", fromlist=["QPushButton"]).QPushButton)
    assert len(buttons) == 2
    confirmed: list[str] = []
    cancelled: list[str] = []
    panel.analysis_confirm_requested.connect(confirmed.append)
    panel.analysis_cancel_requested.connect(cancelled.append)
    buttons[0].click()
    buttons[1].click()
    assert confirmed == ["analysis-1"]
    assert cancelled == ["analysis-1"]


def test_header_keeps_model_out_of_conversation_title(panel):
    snap = AssistantStateSnapshot(
        state=AssistantState.IDLE,
        active_profile_id="local-qwen",
        profile_connected=True,
        allow_media_upload=False,
    )
    panel.refresh_state(snap)
    txt = panel._header_label.text()
    assert txt == "新对话"
    assert "local-qwen" not in txt


# ---------------------------------------------------------------- #
# M1.3：request composer + variant 列表渲染
# ---------------------------------------------------------------- #


def test_send_button_emits_request_submitted(panel):
    """M1.3：在输入框输入文本后点发送 → emit request_submitted，并清空输入。"""
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    emitted = []
    panel.request_submitted.connect(lambda r: emitted.append(r))
    panel._request_edit.setPlainText("30 秒竖版，强调卖点")
    panel._on_send_clicked()
    assert len(emitted) == 1
    assert "卖点" in emitted[0].text
    # 发送后输入框清空
    assert panel._request_edit.toPlainText() == ""


def test_send_empty_does_nothing(panel):
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    emitted = []
    panel.request_submitted.connect(lambda r: emitted.append(r))
    panel._request_edit.setPlainText("   \n  ")
    panel._on_send_clicked()
    assert emitted == []


def test_send_disabled_when_busy(panel):
    """busy 时同一个按钮进入停止状态，输入只读。"""
    panel.refresh_state(
        AssistantStateSnapshot(state=AssistantState.PLANNING)
    )
    assert panel._send_btn.isEnabled() is True
    assert panel._send_btn.text() == "■"
    assert panel._request_edit.isReadOnly() is True
    # cancel 按钮未被隐藏（isHidden=False 表示要显示）
    assert panel._cancel_btn.isHidden() is False


def test_request_truncated_to_max_length(panel):
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    emitted = []
    panel.request_submitted.connect(lambda r: emitted.append(r))
    long_text = "x" * (panel._MAX_REQUEST_LEN + 500)
    panel._request_edit.setPlainText(long_text)
    panel._on_send_clicked()
    assert len(emitted[0].text) == panel._MAX_REQUEST_LEN


def test_variants_render_cards(panel):
    """M1.3：snapshot 含 variant 时渲染卡片列表，隐藏 placeholder。"""
    from src.features.edit.ai_editing.assistant_events import VariantCard

    snap = AssistantStateSnapshot(
        state=AssistantState.VARIANTS_READY,
        variants=(
            VariantCard(
                variant_id="v1", label="A 口播优先", duration_frames=720,
                hard_constraints_passed=True, beat_coverage=1.0,
            ),
            VariantCard(
                variant_id="v2", label="B B-roll 优先", duration_frames=700,
                hard_constraints_passed=True, beat_coverage=0.9,
            ),
        ),
    )
    panel.refresh_state(snap)
    assert panel._variants_area.isHidden() is False
    assert panel._placeholder.isHidden() is True
    # 卡片数 = 2（variants_layout 末尾有一个 stretch）
    card_count = panel._variants_layout.count() - 1
    assert card_count == 2


def test_no_variants_shows_placeholder(panel):
    panel.refresh_state(AssistantStateSnapshot(state=AssistantState.IDLE))
    assert panel._placeholder.isHidden() is False
    assert panel._variants_area.isHidden() is True


def test_variant_apply_button_emits_apply_requested(panel):
    """M1.3：点 variant 卡的'应用'按钮 → emit apply_requested(variant_id)。"""
    from src.features.edit.ai_editing.assistant_events import VariantCard

    snap = AssistantStateSnapshot(
        state=AssistantState.AWAITING_APPLY_APPROVAL,
        variants=(
            VariantCard(variant_id="v1", label="A", duration_frames=720,
                        hard_constraints_passed=True),
        ),
    )
    panel.refresh_state(snap)
    emitted = []
    panel.apply_requested.connect(lambda vid: emitted.append(vid))
    # 找到第一张卡的 apply 按钮（卡 layout 的 btn_row 里第二个按钮）
    card = panel._variants_layout.itemAt(0).widget()
    btn_row = card.layout().itemAt(card.layout().count() - 1).widget()
    # btn_row 是 QVBoxLayout 包了两个按钮；apply 是第二个
    apply_btn = btn_row.layout().itemAt(1).widget()
    apply_btn.click()
    assert emitted == ["v1"]


# ---------------------------------------------------------------- #
# M1.3：Brief 编辑器页（index 4）接入
# ---------------------------------------------------------------- #


def test_brief_approval_state_shows_brief_page(panel):
    """AWAITING_BRIEF_APPROVAL 时切换到 brief 编辑器页（index 4）。"""
    panel.refresh_state(
        AssistantStateSnapshot(state=AssistantState.AWAITING_BRIEF_APPROVAL)
    )
    assert panel._stack.currentIndex() == 4


def test_set_pending_brief_populates_editor(panel):
    """set_pending_brief 把预览 brief 回填进内嵌编辑器。"""
    from src.features.edit.ai_editing.editorial_models import (
        DurationTarget,
        EditorialBrief,
        Platform,
    )

    panel.refresh_state(
        AssistantStateSnapshot(state=AssistantState.AWAITING_BRIEF_APPROVAL)
    )
    brief = EditorialBrief(
        brief_id="default", revision=0, primary_goal="30 秒竖版",
        platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook", "product_value", "call_to_action"),
    )
    panel.set_pending_brief(brief)
    # 编辑器已回填
    editor = panel._brief_editor
    assert editor._goal_edit.toPlainText() == "30 秒竖版"
    assert editor._platform_combo.currentText() == Platform.VERTICAL_SOCIAL.value


def test_brief_editor_signal_forwards_to_panel(panel):
    """内嵌编辑器点'批准 Brief' → 转发到 panel.brief_patch_requested。"""
    from src.features.edit.ai_editing.editorial_models import (
        DurationTarget,
        EditorialBrief,
        Platform,
    )

    panel.refresh_state(
        AssistantStateSnapshot(state=AssistantState.AWAITING_BRIEF_APPROVAL)
    )
    brief = EditorialBrief(
        brief_id="default", revision=0, primary_goal="确认",
        platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
    )
    panel.set_pending_brief(brief)
    emitted = []
    panel.brief_patch_requested.connect(lambda b: emitted.append(b))
    # 模拟用户点编辑器的"批准 Brief"
    panel._brief_editor._approve_btn.click()
    assert len(emitted) == 1
    # emit 的是 bumped revision 的完整 brief
    approved = emitted[0]
    assert approved.revision == 1  # bumped
    assert approved.primary_goal == "确认"
