"""M12.3b · auto search 按钮可靠匹配接线回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
from PyQt6.QtCore import Qt

pytestmark = pytest.mark.edit_core


class _FakeDialog:
    """relink 对话框替身（__new__ 注入——只挂 _on_auto_search 依赖）。"""

    from src.features.edit.ui.relink_media_dialog import (  # noqa: E402
        RelinkMediaDialog,
    )

    def __init__(self, rows, tmp_path, monkeypatch):
        self._table = QTableWidget(len(rows), 3)
        for r, (asset_id, path) in enumerate(rows):
            item = QTableWidgetItem(asset_id)
            item.setData(Qt.ItemDataRole.UserRole, asset_id)
            self._table.setItem(r, 0, item)
            self._table.setItem(r, 2, QTableWidgetItem(""))
        self._assets = {aid: NS(path=path) for aid, path in rows}
        self.filled = []
        self._tmp = tmp_path
        # home 指到空目录——排除 Desktop/Documents 等真实目录干扰
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        monkeypatch.setattr(
            "src.features.edit.ui.relink_media_dialog.os.path.expanduser",
            lambda _: str(empty_home))
        import src.features.edit.ui.relink_media_dialog as dlg_mod
        self._msg = []
        monkeypatch.setattr(
            dlg_mod.QMessageBox, "information",
            lambda *a, **k: self._msg.append(a))
        self._reliable_match = (
            type(self).RelinkMediaDialog._reliable_match.__get__(self))
        self._build_directory_index = (
            type(self).RelinkMediaDialog._build_directory_index.__get__(self))

    def _get_asset_by_id(self, asset_id):
        return self._assets.get(asset_id)

    def _get_asset_attr(self, asset, attr):
        return getattr(asset, attr, "")

    def _update_relink_path(self, row, path):
        self.filled.append((row, path))
        self._table.item(row, 2).setText(path)

    def run(self):
        type(self).RelinkMediaDialog._on_auto_search(self)
        return self


class TestAutoSearchReliable:
    def test_moved_file_unique_name_filled_with_review_flag(self,
                                                             qtbot,
                                                             tmp_path,
                                                             monkeypatch):
        """文件移到父目录（原位缺失）：唯一同名候选填入并明示人工核验
        ——不再静默 basename 直填。"""
        media = tmp_path / "media"
        media.mkdir()
        (media / "clip.mov").write_bytes(b"moved-content")
        # 原路径缺失（已移动）：tmp_path 是 media 的父目录
        rows = [("a1", str(media / "gone_original.mov"))]
        # 原路径为 gone_original.mov 而候选是 clip.mov——名字不同不命中；
        # 直接用同名场景：原 media/clip.mov 已移走，父目录放同名副本
        (tmp_path / "clip.mov").write_bytes(b"moved-content-2")
        rows = [("a1", str(media / "clip.mov"))]
        dialog = _FakeDialog(rows, tmp_path, monkeypatch)
        qtbot.addWidget(dialog._table)
        dialog.run()
        assert dialog.filled, "唯一同名候选应填入（人工核验计数）"
        assert dialog.filled[0][1] == str(tmp_path / "clip.mov")
        assert any("人工核验" in str(m) or "核验" in str(m)
                   for m in dialog._msg)

    def test_no_candidate_no_fill(self, qtbot, tmp_path, monkeypatch):
        media = tmp_path / "media"
        media.mkdir()
        rows = [("a1", str(media / "nothing.mov"))]
        dialog = _FakeDialog(rows, tmp_path, monkeypatch)
        qtbot.addWidget(dialog._table)
        dialog.run()
        assert dialog.filled == []
        assert any("Could not" in str(m) for m in dialog._msg)
