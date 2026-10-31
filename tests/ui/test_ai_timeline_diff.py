"""TAEP Phase 9 UI-4 — TimelineDiff 面板 + 审批 dialog 测试。

验收（TAEP §11.9 UI-4）：
- diff 按视频/音频轨显示；高风险项（removed）置顶。
- has_errors / stale 时禁用应用按钮。
- 应用前弹确认 dialog，显示 undo group/影响数/删除数/warnings。
- 只读 diff；应用只发 signal，不直接 commit。
"""

from __future__ import annotations

import pytest

from src.features.edit.ui.ai_timeline_diff import (
    AiApplyConfirmationDialog,
    AiTimelineDiffPanel,
)


@pytest.fixture
def diff_panel(qapp):
    w = AiTimelineDiffPanel()
    yield w
    w.deleteLater()


def _sample_diff() -> dict:
    return {
        "added": [{"change": "added", "clip_id": "c1", "track_id": "v1",
                   "after": {"timeline": {"start": 0, "duration": 100}}}],
        "removed": [{"change": "removed", "clip_id": "c_old", "track_id": "v1",
                     "before": {"timeline": {"start": 0, "duration": 50}}}],
        "moved": [{"change": "moved", "clip_id": "c2", "track_id": "v1",
                   "before": {"timeline": {"start": 0}}, "after": {"timeline": {"start": 100}}}],
        "trimmed": [],
        "duration_before": 50,
        "duration_after": 200,
        "total_changes": 3,
        "warnings": ["将删除 1 个片段，提交前请确认"],
    }


def test_diff_panel_displays_entries(diff_panel):
    diff_panel.set_diff(_sample_diff(), variant_id="v1")
    # 摘要区有文本（i18n 是否加载都填充）
    assert diff_panel._summary_label.text() != ""
    # removed (高风险) 置顶 -> 第一张卡是 removed；共 3 条 + 1 条预检警告 + stretch
    # （AUD-19619：warnings 不再读入即弃，须渲染为高风险条目）
    assert diff_panel._container_layout.count() == 5
    assert diff_panel.current_warnings == ("将删除 1 个片段，提交前请确认",)


def test_diff_panel_disables_apply_on_errors(diff_panel):
    diff_panel.set_diff(_sample_diff(), has_errors=True)
    assert diff_panel._apply_btn.isEnabled() is False


def test_diff_panel_disables_apply_on_stale(diff_panel):
    diff_panel.set_diff(_sample_diff(), stale=True)
    assert diff_panel._apply_btn.isEnabled() is False
    assert diff_panel._stale_label.isVisibleTo(diff_panel) is True


def test_diff_panel_apply_enabled_when_clean(diff_panel):
    diff_panel.set_diff(_sample_diff(), variant_id="v1")
    assert diff_panel._apply_btn.isEnabled() is True


def test_diff_panel_emits_apply_signal(diff_panel):
    diff_panel.set_diff(_sample_diff(), variant_id="v9")
    emitted = []
    diff_panel.apply_requested.connect(lambda vid: emitted.append(vid))
    diff_panel._apply_btn.click()
    assert emitted == ["v9"]


def test_diff_panel_high_risk_entry_styled(diff_panel):
    diff_panel.set_diff(_sample_diff())
    # 第一张卡（removed，高风险）应有红色边框样式
    first_card = diff_panel._container_layout.itemAt(0).widget()
    style = first_card.styleSheet()
    assert "e74c3c" in style  # red border for high risk


def test_confirmation_dialog_content(qapp):
    dlg = AiApplyConfirmationDialog(
        undo_label="AI plan: rough cut", affected_clips=5, removed_clips=1,
        warnings=("字体缺失",),
    )
    from PyQt6.QtWidgets import QTextEdit
    body = dlg.findChild(QTextEdit)
    text = body.toPlainText()
    # 关键信息出现在正文里（i18n 是否加载都应包含传入的 label/warning）
    assert "rough cut" in text
    assert "字体缺失" in text


def test_confirmation_dialog_not_approved_by_default(qapp):
    dlg = AiApplyConfirmationDialog("label", 1, 0)
    assert dlg.approved is False
