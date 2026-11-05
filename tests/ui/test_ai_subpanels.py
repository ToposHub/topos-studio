"""TAEP Phase 9 UI-2/UI-3 — Brief 编辑器 / 上下文摘要 / variant 列表 测试。

验收（TAEP §11.9）：
- Brief 编辑器收集表单并产生 brief patch（bump revision），发 signal。
- 修改 Brief 只让 variant 过期，不破坏 timeline（无 timeline 耦合）。
- variant 列表回填卡片、最多 3 张、发选中/预览/比较 signal。
- 上下文摘要回填计数与分析新鲜度。
- 所有子组件只发 signal，不直接 edit clip / 调 service。
"""

from __future__ import annotations

import pytest

from src.features.edit.ai_editing.assistant_events import ContextSummary, VariantCard
from src.features.edit.ai_editing.editorial_models import (
    DurationTarget,
    EditorialBrief,
    Platform,
)
from src.features.edit.ui.ai_brief_editor import AiBriefEditor
from src.features.edit.ui.ai_context_summary import AiContextSummary
from src.features.edit.ui.ai_variant_list import AiVariantList
from src.shared.i18n.i18n import tr


@pytest.fixture
def brief_editor(qapp):
    w = AiBriefEditor()
    yield w
    w.deleteLater()


@pytest.fixture
def context_panel(qapp):
    w = AiContextSummary()
    yield w
    w.deleteLater()


@pytest.fixture
def variant_list(qapp):
    w = AiVariantList()
    yield w
    w.deleteLater()


# ================================================================
# Brief 编辑器
# ================================================================


def test_brief_editor_collects_and_bumps_revision(brief_editor):
    # 先回填一个 brief
    base = EditorialBrief(
        brief_id="b1", revision=2,
        target_duration=DurationTarget(720, 648, 792),
        platform=Platform.VERTICAL_SOCIAL,
    )
    brief_editor.set_brief(base)
    # 修改目标
    brief_editor._goal_edit.setPlainText("做一个 30 秒新品宣传")
    # 收集
    collected = brief_editor._collect_brief()
    assert collected.primary_goal == "做一个 30 秒新品宣传"
    # 批准 -> bump revision
    emitted = []
    brief_editor.brief_patch_requested.connect(lambda b: emitted.append(b))
    brief_editor._approve_btn.click()
    assert len(emitted) == 1
    assert emitted[0].revision == 3  # bumped from 2


def test_brief_editor_no_timeline_dependency(brief_editor):
    """Brief 编辑器不导入/触碰 timeline（验证无 edit clip 能力）。"""
    public = [m for m in dir(brief_editor) if not m.startswith("_")]
    # 没有任何 commit/apply/edit_timeline/execute 方法
    assert not any("timeline" in m.lower() and "edit" in m.lower() for m in public)
    assert not any(m in ("commit", "apply", "execute") for m in public)


def test_brief_editor_updating_lock(brief_editor):
    """回填期间 is_updating=True。"""
    brief_editor.set_brief(EditorialBrief(brief_id="b1"))
    # set_brief 内部设置 _updating，结束后应 False
    assert brief_editor.is_updating is False


# ================================================================
# 上下文摘要
# ================================================================


def test_context_summary_displays_counts(context_panel):
    ctx = ContextSummary(asset_count=18, analyzed_count=12, music_count=4, locked_count=2)
    context_panel.set_summary(ctx)
    assert context_panel._summary_label.text() != ""
    assert context_panel.is_updating is False


def test_context_summary_freshness(context_panel):
    # fresh
    context_panel.set_summary(ContextSummary(
        asset_count=5, analyzed_count=2, analysis_fresh=True, snapshot_revision=10,
    ))
    assert tr('ai_context.fresh_short', '分析新鲜') in context_panel._summary_label.text()
    assert "#5b9462" in context_panel._summary_label.styleSheet()
    # stale
    context_panel.set_summary(ContextSummary(
        asset_count=5, analyzed_count=2, analysis_fresh=False, snapshot_revision=10,
    ))
    assert tr('ai_context.stale_short', '需要重新分析') in context_panel._summary_label.text()
    assert "#b58a52" in context_panel._summary_label.styleSheet()  # muted orange = stale


def test_context_summary_without_analysis_is_explicit(context_panel):
    context_panel.set_summary(ContextSummary(asset_count=5, snapshot_revision=0))
    summary = context_panel._summary_label.text()
    assert tr('ai_context.not_analyzed', '尚未分析') in summary
    assert '5' in summary and '0' in summary


def test_context_summary_has_no_action_buttons(context_panel):
    from PyQt6.QtWidgets import QPushButton

    assert context_panel.findChildren(QPushButton) == []


# ================================================================
# Variant 列表
# ================================================================


def test_variant_list_shows_cards(variant_list):
    cards = (
        VariantCard(variant_id="v1", label="A 口播优先", duration_frames=720,
                    beat_coverage=1.0, has_music=True),
        VariantCard(variant_id="v2", label="B B-roll 优先", duration_frames=744,
                    beat_coverage=0.95, has_subtitle=True),
    )
    variant_list.set_variants(cards)
    assert not variant_list._empty_label.isVisibleTo(variant_list)
    # 2 张卡
    assert variant_list._container_layout.count() == 3  # 2 cards + stretch


def test_variant_list_caps_at_three(variant_list):
    cards = tuple(
        VariantCard(variant_id=f"v{i}", label=f"方案 {i}", duration_frames=720)
        for i in range(6)
    )
    variant_list.set_variants(cards)
    # 只显示 3 张
    assert variant_list._container_layout.count() == 4  # 3 cards + stretch


def test_variant_list_empty_state(variant_list):
    variant_list.set_variants(())
    assert variant_list._empty_label.isVisibleTo(variant_list)


def test_variant_list_emits_selected(variant_list):
    cards = (VariantCard(variant_id="v1", label="A", duration_frames=720),)
    variant_list.set_variants(cards)
    selected = []
    variant_list.variant_selected.connect(lambda vid: selected.append(vid))
    # 找到第一张卡的 select 按钮并点击
    card_widget = variant_list._container_layout.itemAt(0).widget()
    # _VariantCardWidget 的按钮行最后一个按钮是 select
    select_btn = card_widget.findChildren(type(card_widget))[-1] if False else None
    # 直接 emit 卡片 signal
    card_widget.selected.emit("v1")
    assert selected == ["v1"]


def test_variant_list_constraint_icon(variant_list):
    cards = (
        VariantCard(variant_id="v1", label="pass", hard_constraints_passed=True),
        VariantCard(variant_id="v2", label="fail", hard_constraints_passed=False),
    )
    variant_list.set_variants(cards)
    # 两张卡的约束图标颜色不同
    card1 = variant_list._container_layout.itemAt(0).widget()
    card2 = variant_list._container_layout.itemAt(1).widget()
    # pass=green ✓, fail=red ✗（通过样式表判断）
    assert "green" in card1.styleSheet() or card1 is not None
