"""P1 最后一公里批次1：MediaPoolPanel 公共选择 API + bridge 对齐。

> 验证 selected_asset_ids 不再恒空：
> - MediaPoolPanel 暴露公共 get_selected_asset_ids() 方法
> - MainWindowAiEditHostBridge 探测 media_pool_panel（实际属性名）而非 media_pool_controller
"""
from __future__ import annotations

from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt, QItemSelectionModel
from PyQt6.QtWidgets import QTreeWidgetItem

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------- #
# MediaPoolPanel 公共选择 API
# ---------------------------------------------------------------------- #


def test_media_pool_panel_has_public_get_selected_asset_ids(qapp):
    """MediaPoolPanel 暴露公共 get_selected_asset_ids 方法（供 bridge 用）。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel
    assert hasattr(MediaPoolPanel, "get_selected_asset_ids"), (
        "MediaPoolPanel 必须有公共 get_selected_asset_ids 方法"
    )


def test_media_pool_get_selected_returns_tuple(qapp):
    """get_selected_asset_ids 返回 tuple（与 InvocationScope.selected_asset_ids 契约一致）。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel
    panel = MediaPoolPanel()
    try:
        result = panel.get_selected_asset_ids()
        assert isinstance(result, tuple), "应返回 tuple"
        assert result == (), "无选择时应返回空 tuple"
    finally:
        panel.deleteLater()


def test_media_pool_exposes_ai_attachment_candidates(qapp):
    """AI ``@`` picker can read the live media-pool asset list."""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel
    panel = MediaPoolPanel()
    try:
        result = panel.get_ai_attachment_candidates()
        assert isinstance(result, tuple)
        assert result == ()
    finally:
        panel.deleteLater()


def test_media_pool_exposes_virtual_motion_graphic_for_ai_targeting(qapp):
    """A reopened project still offers virtual Motion Graphic assets to @/drag."""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    panel = MediaPoolPanel()
    panel._project = SimpleNamespace(assets={
        "teaching_animation:scene-reopened": SimpleNamespace(
            asset_id="teaching_animation:scene-reopened",
            path="topos://motion_graphic/scene-reopened",
            display_name="重开后的网页 Clip",
            media_type="motion_graphic",
        ),
    })
    try:
        result = panel.get_ai_attachment_candidates()
        assert result == ({
            "asset_id": "teaching_animation:scene-reopened",
            "path": "topos://motion_graphic/scene-reopened",
            "display_name": "重开后的网页 Clip",
            "media_type": "motion_graphic",
        },)
    finally:
        panel.deleteLater()


def test_media_pool_context_menu_scopes_tree_selection_to_clicked_asset(qapp):
    """Right-click selection contract: an unselected target becomes the sole
    selection; a target already inside an extended selection preserves the
    whole group (batch-action contract, media_pool_panel._select_single_*)."""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    panel = MediaPoolPanel()
    try:
        first = QTreeWidgetItem(["Scene 001"])
        second = QTreeWidgetItem(["Scene 002"])
        for item, asset_id in ((first, "scene-001"), (second, "scene-002")):
            item.setData(0, Qt.ItemDataRole.UserRole, asset_id)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "asset")
            panel.tree.addTopLevelItem(item)

        # 分支 1：目标未选中 → 收敛为唯一选择
        first.setSelected(True)
        panel._select_single_context_tree_item(second)

        assert panel.tree.selectedItems() == [second]
        assert panel.tree.currentItem() is second

        # 分支 2：目标已在多选组内 → 保留整组，仅移动 current
        first.setSelected(True)
        second.setSelected(True)
        panel._select_single_context_tree_item(second)

        assert {item.text(0) for item in panel.tree.selectedItems()} == {
            "Scene 001", "Scene 002"}
        assert panel.tree.currentItem() is second
    finally:
        panel.deleteLater()


def test_media_pool_context_menu_scopes_grid_selection_to_clicked_subclip(qapp):
    """Grid selection contract mirrors the tree: unselected target collapses
    to a single selection; a selected target keeps the intentional group."""
    from src.features.edit.core.project import MediaAsset
    from src.features.edit.ui.media_pool_panel import GridItem, MediaPoolPanel

    panel = MediaPoolPanel()
    try:
        assets = [
            MediaAsset(
                asset_id=f"subclip-{index}",
                path="/fake/source.mp4",
                display_name=f"Source - Scene {index:03d}",
                media_type="video",
                metadata={"is_subclip": True, "parent_asset_id": "source"},
            )
            for index in (1, 2)
        ]
        assert panel.grid_view is not None
        assert panel.asset_model is not None
        panel.asset_model.set_items(
            [GridItem("asset", asset.asset_id, asset) for asset in assets]
        )

        selection_model = panel.grid_view.selectionModel()
        first_index = panel.asset_model.index(0, 0)
        second_index = panel.asset_model.index(1, 0)

        # 分支 1：目标未选中 → ClearAndSelect 目标
        selection_model.select(
            first_index,
            QItemSelectionModel.SelectionFlag.Select,
        )
        panel._select_single_context_grid_item(second_index)

        selected_indexes = panel.grid_view.selectedIndexes()
        assert selected_indexes == [second_index]
        assert panel.grid_view.currentIndex() == second_index

        # 分支 2：目标已在多选组内 → 保留整组，仅移动 current
        selection_model.select(
            first_index,
            QItemSelectionModel.SelectionFlag.Select,
        )
        panel._select_single_context_grid_item(second_index)

        assert {index.row() for index in panel.grid_view.selectedIndexes()} == {0, 1}
        assert panel.grid_view.currentIndex() == second_index
    finally:
        panel.deleteLater()


# ---------------------------------------------------------------------- #
# bridge 探测 media_pool_panel（实际属性名）
# ---------------------------------------------------------------------- #


def test_bridge_get_selected_asset_ids_reads_media_pool_panel(qapp):
    """MainWindowAiEditHostBridge.get_selected_asset_ids 探测 media_pool_panel。

    模拟 MainWindow 持有 media_pool_panel（真实属性名），bridge 应取到选中素材。
    """
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge

    # fake media_pool_panel with 2 selected assets
    fake_mp = MagicMock()
    fake_mp.get_selected_asset_ids.return_value = ("a1", "a2")

    # fake mainwindow 持有 media_pool_panel（真实属性名）
    fake_mw = MagicMock()
    fake_mw.media_pool_panel = fake_mp
    # 不设 media_pool_controller / media_pool / _media_pool（确保 bridge 探测对的名字）

    bridge = MainWindowAiEditHostBridge(fake_mw, MagicMock(), MagicMock())
    result = bridge.get_selected_asset_ids()

    assert result == ("a1", "a2"), "bridge 应从 media_pool_panel 取到选中素材"
    # 确认调的是 get_selected_asset_ids（公共方法）
    fake_mp.get_selected_asset_ids.assert_called_once()


def test_bridge_get_selected_asset_ids_empty_when_no_panel(qapp):
    """MainWindow 无 media_pool_panel 时返回空 tuple（不崩）。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge

    fake_mw = MagicMock(spec=[])  # 无任何属性
    bridge = MainWindowAiEditHostBridge(fake_mw, MagicMock(), MagicMock())
    result = bridge.get_selected_asset_ids()
    assert result == ()


def test_bridge_get_selected_asset_ids_falls_back_to_legacy_names(qapp):
    """bridge 兼容旧的 selected_asset_ids 属性（向后兼容，无 get_selected_asset_ids 方法）。"""
    from src.app.controllers.ai_edit_host_bridge_impl import MainWindowAiEditHostBridge

    # 旧 media_pool：只有 selected_asset_ids 属性，无 get_selected_asset_ids 方法
    class LegacyMediaPool:
        selected_asset_ids = ["legacy-1", "legacy-2"]
    fake_mp = LegacyMediaPool()

    fake_mw = MagicMock(spec=["media_pool"])  # 只有 media_pool，无 media_pool_panel
    fake_mw.media_pool = fake_mp

    bridge = MainWindowAiEditHostBridge(fake_mw, MagicMock(), MagicMock())
    result = bridge.get_selected_asset_ids()
    assert "legacy-1" in result and "legacy-2" in result
