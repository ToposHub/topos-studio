"""P1-16 — FCPXML/OTIO 互操作 UI 接线冒烟测试。

覆盖：
- InterchangeReportDialog 渲染（分类统计/条目表/素材行/confirm 按钮）
- EditModule 注册的 File 菜单互操作动作与 FileOpsMixin 回调名一致
  （MenuBuilder 用 getattr(main_window, callback_name) 解析，名字错则菜单项
  会被静默跳过，所以此处必须显式对齐）。
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.edit_ui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from src.features.edit.adapters.interchange_types import (
    InterchangeEntry,
    InterchangeEntryCategory,
    InterchangeReport,
)
from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

_REPORT = InterchangeReport(
    format_id="fcpxml",
    entries=(
        InterchangeEntry(
            category=InterchangeEntryCategory.PRESERVED,
            element="asset-clip",
            description="clip imp-0 <- asset r2",
        ),
        InterchangeEntry(
            category=InterchangeEntryCategory.DROPPED,
            element="multicam",
            description="FCPXML 含 multicam，首版不支持，将被忽略",
        ),
        InterchangeEntry(
            category=InterchangeEntryCategory.REQUIRES_USER_ACTION,
            element="subtitle",
            description="字幕样式未保证",
        ),
    ),
    diagnostics=(
        {"severity": "warning", "code": "TAEP_INTERCHANGE_LOSSY", "message": "m"},
    ),
)


def test_report_dialog_renders_entries_and_rows(qapp):
    dlg = InterchangeReportDialog(
        _REPORT,
        title="预览",
        asset_rows=[("clip_a", "/x/clip_a.mp4", True), ("clip_b", "/gone.mp4", False)],
        confirm_mode=True,
    )
    # 条目表：3 行（preserved/dropped/requires_action）
    from PyQt6.QtWidgets import QTableWidget
    tables = dlg.findChildren(QTableWidget)
    assert len(tables) == 2  # 映射条目表 + 素材状态表
    assert tables[0].rowCount() == 3
    assert tables[1].rowCount() == 2
    # confirm_mode 提供 Cancel
    from PyQt6.QtWidgets import QDialogButtonBox
    boxes = dlg.findChildren(QDialogButtonBox)
    assert any(
        b.button(QDialogButtonBox.StandardButton.Cancel) is not None for b in boxes
    )


def test_report_dialog_plain_ok_only(qapp):
    dlg = InterchangeReportDialog(_REPORT)
    from PyQt6.QtWidgets import QDialogButtonBox
    boxes = dlg.findChildren(QDialogButtonBox)
    assert boxes
    assert all(
        b.button(QDialogButtonBox.StandardButton.Cancel) is None for b in boxes
    )


def test_menu_actions_match_controller_callbacks():
    """菜单 descriptor 的 callback_name 必须在 FileOpsMixin 上存在。"""
    from src.app.controllers.file_controller import FileOpsMixin
    from src.features.edit.module import EditModule
    from src.shared.actions import ActionRegistry

    registry = ActionRegistry()
    EditModule().register_menu_actions(registry)

    actions = {
        a.action_id: a
        for a in registry.get_actions(enabled_features={"edit"})
        if a.menu_path == "File"
    }
    assert "edit.interchange.import" in actions
    assert "edit.interchange.export.fcpxml" in actions
    assert "edit.interchange.export.otio" in actions

    for action_id in (
        "edit.interchange.import",
        "edit.interchange.export.fcpxml",
        "edit.interchange.export.otio",
    ):
        callback = actions[action_id].callback_name
        assert callable(
            getattr(FileOpsMixin, callback, None)
        ), f"{action_id} callback {callback} missing on FileOpsMixin"


def test_otio_public_availability_probe():
    from src.features.edit.adapters.otio_adapter import otio_available

    assert isinstance(otio_available(), bool)
