"""Regression tests for batch-A UI fixes (2026-09-28 audit, machine w3-g7).

Covers:
* AUD-20508 — RelinkMediaDialog 各入口必须用
  ``item.data(Qt.ItemDataRole.UserRole)`` 取 asset_id；修复前传入枚举
  类型 ``Qt.ItemDataRole`` 会在第一行即抛 TypeError，重链接整体不可用。
* AUD-20600 — SceneCutDialog.closeEvent 对 wait() 超时后仍在运行的
  PreviewLoadWorker 不得 deleteLater（销毁运行中的 QThread 属未定义
  行为）；超时线程转 zombie 列表保引用，等其自然退出后清理。
* AUD-21100 — ClipItem._show_retime_segment_speed_menu 引用 I18N，
  修复前未导入会抛 NameError，段速度/Smooth Ramp 菜单不可用。

All Qt tests use the offscreen platform so they run headless.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QFileDialog

from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog
from src.features.edit.ui.scene_cut_dialog import (
    PreviewLoadWorker,
    SceneCutDialog,
)
from src.features.edit.ui.timeline_items import ClipItem
from src.shared.i18n.i18n import I18N


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ================================================================
# AUD-20508: RelinkMediaDialog asset_id 读取使用 UserRole
# ================================================================

def _make_relink_dialog(qapp) -> RelinkMediaDialog:
    assets = [
        {
            "asset_id": "asset-1",
            "display_name": "clip.mp4",
            "path": "/old/location/clip.mp4",
        }
    ]
    return RelinkMediaDialog(assets=assets)  # type: ignore[arg-type]


def test_relink_table_stores_asset_id_in_user_role(qapp) -> None:
    """写入侧 setData(UserRole) 与读取侧 data(UserRole) 必须对称。"""
    dlg = _make_relink_dialog(qapp)

    item = dlg._table.item(0, 0)
    assert item is not None
    # 修复前：dlg._update_relink_path(0, ...) 在
    # item.data(Qt.ItemDataRole) 处抛 TypeError。
    assert item.data(Qt.ItemDataRole.UserRole) == "asset-1"


def test_relink_update_relink_path_populates_map(qapp, tmp_path) -> None:
    """:499 — _update_relink_path 不再抛 TypeError 且 relink_map 正确。"""
    dlg = _make_relink_dialog(qapp)
    new_path = str(tmp_path / "clip.mp4")

    dlg._update_relink_path(0, new_path)

    assert dlg.get_relink_map() == {"asset-1": new_path}


def test_relink_by_directory_reads_asset_id(qapp, monkeypatch, tmp_path) -> None:
    """:381 — 目录搜索入口取 asset_id 不抛 TypeError 并能写入映射。"""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"stub")
    dlg = _make_relink_dialog(qapp)

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        lambda *args, **kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **kwargs: None
    )

    dlg._on_relink_by_directory()

    assert dlg.get_relink_map() == {"asset-1": str(media)}


def test_relink_selected_reads_asset_id(qapp, monkeypatch, tmp_path) -> None:
    """:347 — Relink Selected 入口取 asset_id 不抛 TypeError。"""
    new_path = str(tmp_path / "clip.mp4")
    new_path_file = tmp_path / "clip.mp4"
    new_path_file.write_bytes(b"stub")
    dlg = _make_relink_dialog(qapp)
    dlg._table.selectRow(0)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *args, **kwargs: (new_path, ""),
    )

    dlg._on_relink_selected()

    assert dlg.get_relink_map() == {"asset-1": new_path}


# ================================================================
# AUD-20600: closeEvent 不得销毁运行中的预览线程
# ================================================================

def _make_scene_cut_dialog(qapp) -> SceneCutDialog:
    # 不带 source_path：跳过 frame provider 初始化，仅验证线程清理策略。
    return SceneCutDialog()


def test_close_event_defers_cleanup_for_stuck_worker(qapp, monkeypatch) -> None:
    """wait() 超时（线程仍在解码）时：保引用、不 deleteLater。"""
    dlg = _make_scene_cut_dialog(qapp)
    worker = PreviewLoadWorker(provider=None, candidate_index=0, frame_number=0)
    delete_later_calls: list[bool] = []
    monkeypatch.setattr(worker, "deleteLater", lambda: delete_later_calls.append(True))
    monkeypatch.setattr(worker, "isRunning", lambda: True)
    monkeypatch.setattr(worker, "wait", lambda timeout: False)  # 模拟慢解码超时

    dlg._preview_workers.append(worker)
    dlg.closeEvent(QCloseEvent())

    # 超时线程不得被 deleteLater（销毁运行中的 QThread 会崩溃）
    assert delete_later_calls == []
    # 保引用：转入 zombie 列表等待自然退出
    assert worker in dlg._zombie_preview_workers
    # 仍留在 _preview_workers，供其 finished 信号回调清理
    assert worker in dlg._preview_workers

    # 模拟线程最终自然退出：finished 回调应完成清理并只在此刻 deleteLater
    monkeypatch.setattr(worker, "isRunning", lambda: False)
    dlg._on_preview_worker_finished(worker)
    assert worker not in dlg._preview_workers
    assert worker not in dlg._zombie_preview_workers
    assert delete_later_calls == [True]


def test_close_event_cleans_up_finished_worker(qapp, monkeypatch) -> None:
    """已退出（或 wait 成功返回）的线程仍走即时清理路径。"""
    dlg = _make_scene_cut_dialog(qapp)
    worker = PreviewLoadWorker(provider=None, candidate_index=0, frame_number=0)
    delete_later_calls: list[bool] = []
    monkeypatch.setattr(worker, "deleteLater", lambda: delete_later_calls.append(True))
    monkeypatch.setattr(worker, "isRunning", lambda: False)

    dlg._preview_workers.append(worker)
    dlg.closeEvent(QCloseEvent())

    assert worker not in dlg._preview_workers
    assert dlg._zombie_preview_workers == []
    assert delete_later_calls == [True]


# ================================================================
# AUD-21100: Retime 段速度菜单不再引用未导入的 I18N
# ================================================================

def test_retime_segment_speed_menu_builds_without_name_error(qapp, monkeypatch) -> None:
    """菜单构建（含 Smooth Ramp 项）不得抛 NameError。

    QMenu.exec 是模态阻塞调用，测试中替换为立即返回 None。
    """
    item = ClipItem(
        clip_id="clip-1",
        track_id="v1",
        asset_name="A.mp4",
        start_frame=0,
        duration_frames=48,
        pixels_per_frame=5.0,
        track_index=0,
        source_duration=48,
        retime_points=((0, 1.0), (24, 0.5)),
        retime_controls_visible=True,
    )
    menus: list[QMenu] = []

    def fake_exec(self: QMenu, *args: object, **kwargs: object) -> None:
        menus.append(self)
        return None

    monkeypatch.setattr(QMenu, "exec", fake_exec)

    # 修复前：方法内 I18N 未导入，此处抛 NameError。
    item._show_retime_segment_speed_menu(QPointF(10.0, 5.0), 10.0)

    assert len(menus) == 1
    texts = [action.text() for action in menus[0].actions()]
    # 速度百分比项 + Smooth Ramp（经 I18N 解析的实际文案）
    assert "100%" in texts
    assert I18N.tr("edit.clip.smooth_ramp", "Smooth Ramp") in texts
    # Smooth Ramp 是可勾选的切换项
    assert menus[0].actions()[-1].isCheckable()
