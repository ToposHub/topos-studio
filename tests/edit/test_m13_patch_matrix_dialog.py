"""M13.1 尾片 · 源 patch 布线矩阵对话框入口回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.source_patch import SourcePatchSpec
from src.features.edit.ui.source_patch_dialog import SourcePatchDialog


def _dialog(qtbot, n_channels, current):
    dialog = SourcePatchDialog(n_channels, current)
    qtbot.addWidget(dialog)
    return dialog


class TestMatrixView:
    def test_passthrough_identity_marks(self, qtbot):
        dlg = _dialog(qtbot, 4, SourcePatchSpec())
        assert dlg._matrix_table.rowCount() == 4
        assert dlg._matrix_table.columnCount() == 4
        # 恒等直通：对角线 ●
        assert dlg._matrix_table.item(0, 0).text() == "●"
        assert dlg._matrix_table.item(0, 1).text() == ""
        assert dlg._matrix_table.item(3, 3).text() == "●"

    def test_mono_single_wired_no_mute_row(self, qtbot):
        dlg = _dialog(qtbot, 4, SourcePatchSpec(
            audio_channel_map=((2, 0),)))
        assert dlg._matrix_table.rowCount() == 1
        assert dlg._matrix_table.item(0, 2).text() == "●"
        header = dlg._matrix_table.verticalHeaderItem(0).text()
        assert "静音" not in header

    def test_multi_target_marks_and_mute_via_explicit_width(self, qtbot):
        """多目标映射逐格打 ●；显式目标宽度的静音行标记（模型直测
        +对话框行标记逻辑共用 channel_matrix）。"""
        # 稀疏 dst {0,2}：目标宽 = max(dst)+1 = 3，中间行显式静音
        dlg = _dialog(qtbot, 4, SourcePatchSpec(
            audio_channel_map=((0, 0), (1, 2))))
        assert dlg._matrix_table.rowCount() == 3
        assert dlg._matrix_table.item(0, 0).text() == "●"
        assert dlg._matrix_table.item(2, 1).text() == "●"
        assert "静音" in dlg._matrix_table.verticalHeaderItem(1).text()
        assert "静音" not in dlg._matrix_table.verticalHeaderItem(0).text()

    def test_no_audio_channels_matrix_hidden(self, qtbot):
        dlg = _dialog(qtbot, 0, SourcePatchSpec())
        assert dlg._matrix_visible is False
