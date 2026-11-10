"""Focused UI contracts for the Resolve-style media-pool presentations."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QModelIndex, QPointF, Qt, QItemSelection, QItemSelectionModel
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QPushButton

from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import (
    AssetListModel, BinTreeWidget, MediaPoolPanel, SearchFilterMode, ViewMode,
    build_media_pool_drag_mime,
)
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.ui.style_sheet import C


def _refresh(panel: MediaPoolPanel) -> None:
    panel._refresh_timer.stop()
    panel._delayed_refresh()


def _index(panel: MediaPoolPanel, kind: str, identity: str) -> QModelIndex:
    for row in range(panel.asset_model.rowCount()):
        index = panel.asset_model.index(row)
        if (index.data(AssetListModel.ITEM_TYPE_ROLE),
                index.data(AssetListModel.ITEM_ID_ROLE)) == (kind, identity):
            return index
    raise AssertionError(f"Missing {kind}: {identity}")


def _selected(panel: MediaPoolPanel) -> set[tuple[str, str]]:
    return set(panel._capture_pool_selection()[0])


@pytest.fixture
def pool(qapp, tmp_path: Path, monkeypatch):
    """Use existing models with all decode/proxy side effects neutralized."""
    provider = ThumbnailProvider.instance()
    monkeypatch.setattr(provider, "get_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(provider, "get_frame_thumbnail", lambda *args, **kwargs: None)
    monkeypatch.setattr(MediaPoolPanel, "_request_timeline_previews", lambda *args: None)
    project = EditProject.create("Resolve presentation")
    asset_ids = []
    for number in range(3):
        path = tmp_path / f"clip-{number}.mp4"
        path.write_bytes(b"test")
        project = project.import_asset(
            str(path), "video", {"duration_seconds": 2.0, "fps": 25.0,
                                 "width": 1920, "height": 1080, "codec": "h264"})
        asset_ids.append(project.get_asset_by_path(str(path)).asset_id)
    project = project.create_bin("Footage", project.root_bin_id)
    footage = next(b.bin_id for b in project.bins.values() if b.name == "Footage")
    project = project.create_bin("Day 01", footage)
    day = next(b.bin_id for b in project.bins.values() if b.name == "Day 01")
    project = project.create_timeline("Sequence A").create_timeline("Sequence B")
    timeline_ids = list(project.timelines)
    panel = MediaPoolPanel()
    panel.resize(840, 600)
    panel._offline_refresh_timer.stop()
    panel.set_project(project)
    _refresh(panel)
    panel.show()
    qapp.processEvents()
    yield panel, asset_ids, footage, day, timeline_ids
    panel._refresh_timer.stop()
    panel._offline_refresh_timer.stop()
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("mixed", [False, True])
def test_three_views_preserve_multiselection_without_preview(pool, mixed: bool) -> None:
    panel, assets, footage, _, timelines = pool
    expected = {("asset", assets[0]), ("asset", assets[1])}
    if mixed:
        expected.update({("bin", footage), ("timeline", timelines[0])})
    selection = QItemSelection()
    for kind, identity in expected:
        index = _index(panel, kind, identity)
        selection.select(index, index)
    panel.grid_view.selectionModel().select(
        selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    previews = []
    panel.asset_selected.connect(previews.append)
    panel.timeline_selected.connect(previews.append)
    for mode in (ViewMode.METADATA, ViewMode.LIST, ViewMode.GRID, ViewMode.METADATA):
        panel.set_view_mode(mode)
        assert _selected(panel) == expected
    assert previews == []


def test_metadata_shelf_shares_selection_and_separates_visible_rows(pool) -> None:
    panel, assets, footage, _, timelines = pool
    panel.set_view_mode(ViewMode.METADATA)
    assert panel.metadata_shelf.model() is panel.grid_view.model()
    assert panel.metadata_shelf.selectionModel() is panel.grid_view.selectionModel()
    for kind, identity in (("asset", assets[0]), ("bin", footage),
                           ("timeline", timelines[0])):
        index = _index(panel, kind, identity)
        assert panel.metadata_shelf.isRowHidden(index.row()) == (kind == "asset")
        assert panel.grid_view.isRowHidden(index.row()) == (kind != "asset")
    index = _index(panel, "timeline", timelines[0])
    panel.metadata_shelf.selectionModel().select(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert _selected(panel) == {("timeline", timelines[0])}
    switches = []
    panel.timeline_switch_requested.connect(switches.append)
    panel.metadata_shelf.doubleClicked.emit(index)
    assert switches == [timelines[0]]


def test_metadata_bin_drop_uses_shelf_coordinates(pool, qapp, monkeypatch) -> None:
    panel, assets, footage, _, _ = pool
    panel.set_view_mode(ViewMode.METADATA)
    panel.metadata_shelf.doItemsLayout()
    qapp.processEvents()
    index = _index(panel, "bin", footage)
    position = panel.metadata_shelf.visualRect(index).center()
    assert panel._target_bin_at(position, panel.metadata_shelf) == footage
    handle_drop = MagicMock(return_value=True)
    monkeypatch.setattr(panel, "_handle_drop_into_bin", handle_drop)
    mime = build_media_pool_drag_mime(asset_ids=[assets[0]])
    event = QDropEvent(QPointF(position), Qt.DropAction.MoveAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    panel.metadata_shelf.dropEvent(event)
    handle_drop.assert_called_once_with(footage, [assets[0]], [], copy=False)


def test_metadata_shelf_delete_routes_to_shared_selection(pool, monkeypatch) -> None:
    panel, _, _, _, timelines = pool
    panel.set_view_mode(ViewMode.METADATA)
    index = _index(panel, "timeline", timelines[0])
    panel.metadata_shelf.selectionModel().select(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    delete = MagicMock(return_value=True)
    monkeypatch.setattr(panel, "_delete_selected_pool_items", delete)
    assert panel.handle_delete_key_from(panel.metadata_shelf)
    assert delete.call_count == 1


def test_metadata_bin_rename_edits_visible_shelf(pool, monkeypatch) -> None:
    panel, _, footage, _, _ = pool
    panel.set_view_mode(ViewMode.METADATA)
    shelf_edit = MagicMock()
    grid_edit = MagicMock()
    monkeypatch.setattr(panel.metadata_shelf, "edit", shelf_edit)
    monkeypatch.setattr(panel.grid_view, "edit", grid_edit)
    panel._start_grid_bin_rename(footage)
    explicit_edits = [call for call in shelf_edit.call_args_list if len(call.args) == 1]
    assert len(explicit_edits) == 1
    assert explicit_edits[0].args[0].data(AssetListModel.ITEM_ID_ROLE) == footage
    assert not [call for call in grid_edit.call_args_list if len(call.args) == 1]


def test_master_sidebar_and_breadcrumb_follow_navigation(pool) -> None:
    panel, _, footage, day, _ = pool
    panel._on_bin_selected(day, False)
    panel.bin_tree.select_bin(day, False)
    ancestor = next(button for button in
                    panel.breadcrumb_path_container.findChildren(QPushButton)
                    if button.text() == "Footage")
    ancestor.click()
    assert panel._current_bin_id == footage
    assert panel._navigation_stack == [panel._project.root_bin_id]
    panel._navigate_up()
    assert panel._current_bin_id == ""
    assert panel._navigation_stack == []
    current = panel.bin_tree.currentItem()
    assert current.data(0, BinTreeWidget.BIN_ID_ROLE) == panel._project.root_bin_id
    panel._on_toggle_sidebar(False)
    panel._enter_bin(footage)
    panel._on_toggle_sidebar(True)
    assert panel.bin_tree.currentItem().data(0, BinTreeWidget.BIN_ID_ROLE) == footage


def test_all_bins_search_finds_filed_asset_without_changing_current_bin(pool) -> None:
    panel, assets, footage, _, _ = pool
    project = panel._project.move_asset_to_bin(assets[2], footage)
    panel.set_project(project)
    panel._navigate_to_root()
    _refresh(panel)
    assert assets[2] not in {a.asset_id for a in panel._get_current_assets()}
    panel.search_box.line_edit.setText("clip-2")
    panel._set_search_scope(True)
    _refresh(panel)
    assert _index(panel, "asset", assets[2]).isValid()
    assert panel._current_bin_id == ""
    panel._set_search_scope(False)
    _refresh(panel)
    assert panel.asset_model.rowCount() == 0


def test_list_timeline_activation_updates_existing_rows_without_rebuild(pool) -> None:
    panel, _, _, _, timelines = pool
    panel.set_view_mode(ViewMode.LIST)
    rows = {item.data(0, Qt.ItemDataRole.UserRole): item
            for item in (panel.tree.topLevelItem(i)
                         for i in range(panel.tree.topLevelItemCount()))
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == "timeline"}
    panel.set_current_timeline(timelines[0])
    assert rows[timelines[0]].foreground(0).color().name() == C.ORANGE.lower()
    panel.set_current_timeline(timelines[1])
    assert rows[timelines[1]].foreground(0).color().name() == C.ORANGE.lower()
    assert rows[timelines[0]].foreground(0).color().name() != C.ORANGE.lower()
    assert _index(panel, "timeline", timelines[1]).data(AssetListModel.IS_ACTIVE_ROLE)


def test_folder_label_is_plain_name_in_every_presentation(pool) -> None:
    panel, _, footage, _, _ = pool
    index = _index(panel, "bin", footage)
    assert index.data(Qt.ItemDataRole.DisplayRole) == "Footage"
    panel.bin_tree.select_bin(footage, False)
    assert panel.bin_tree.currentItem().text(0) == "Footage"
    item = next(panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount())
                if panel.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == footage)
    assert item.text(0) == "Footage"


def test_clear_project_hides_metadata_shelf_and_clears_selection(pool) -> None:
    panel, _, _, _, timelines = pool
    panel.set_view_mode(ViewMode.METADATA)
    index = _index(panel, "timeline", timelines[0])
    panel.metadata_shelf.selectionModel().select(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert not panel.metadata_shelf.isHidden()
    panel.clear_project()
    assert panel.asset_model.rowCount() == 0
    assert panel.metadata_shelf.isHidden()
    assert panel.grid_view.selectionModel().selectedIndexes() == []


def test_all_bins_search_does_not_expand_ai_current_bin_scope(pool) -> None:
    panel, assets, footage, _, _ = pool
    panel.set_project(panel._project.move_asset_to_bin(assets[2], footage))
    panel._navigate_to_sidebar_bin(footage)
    panel.search_box.line_edit.setText("clip")
    panel._set_search_scope(True)
    captured = []
    panel.analysis_requested.connect(lambda identities, scope: captured.append((set(identities), scope)))
    panel._emit_analysis_scope("bin")
    assert captured == [({assets[2]}, "bin")]


def test_color_label_action_uses_existing_undoable_command(pool) -> None:
    panel, assets, _, _, _ = pool
    selection = panel.grid_view.selectionModel()
    selection.select(_index(panel, "asset", assets[0]), QItemSelectionModel.SelectionFlag.ClearAndSelect)
    selection.select(_index(panel, "asset", assets[1]), QItemSelectionModel.SelectionFlag.Select)
    panel._apply_pool_color_label("orange")
    assert all(panel._project.assets[identity].color_label == "orange" for identity in assets[:2])
    assert panel._project.assets[assets[2]].color_label == "none"
    panel.undo()
    assert all(panel._project.assets[identity].color_label == "none" for identity in assets)


def test_header_sort_and_menu_sort_share_indicator(pool) -> None:
    panel, _, _, _, _ = pool
    panel._on_header_clicked(4)
    assert panel._sort_by == "fps"
    assert panel.sort_combo.currentData() == "fps"
    assert panel.tree.header().sortIndicatorSection() == 4
    panel._set_sort_order("desc")
    assert panel.tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData("date"))
    assert not panel.tree.header().isSortIndicatorShown()


def test_unknown_asset_rename_is_a_noop(pool) -> None:
    panel, _, _, _, _ = pool
    panel._start_grid_asset_rename("no-such-asset")


def test_numeric_sort_includes_timelines_and_keeps_unknown_folders_last(pool) -> None:
    panel, _, footage, _, _ = pool
    panel._on_header_clicked(4)
    ordered = panel._current_pool_items()
    known = [getattr(item.data, "fps", None) for item in ordered if getattr(item.data, "fps", None) is not None]
    assert known == sorted(known)
    assert ordered[-1].item_type == "bin"
    panel._set_sort_order("desc")
    ordered = panel._current_pool_items()
    known = [getattr(item.data, "fps", None) for item in ordered if getattr(item.data, "fps", None) is not None]
    assert known == sorted(known, reverse=True)
    assert ordered[-1].item_type == "bin"


@pytest.mark.parametrize("mode,kind", [(ViewMode.GRID, "asset"), (ViewMode.METADATA, "asset"), (ViewMode.METADATA, "timeline")])
def test_info_badge_click_preserves_selection_and_does_not_open_media(pool, qapp, mode, kind) -> None:
    from PyQt6.QtTest import QTest
    panel, assets, _, _, timelines = pool
    panel.set_view_mode(mode)
    model_selection = panel.grid_view.selectionModel()
    for number, identity in enumerate(assets[:2]):
        model_selection.select(_index(panel, "asset", identity), QItemSelectionModel.SelectionFlag.ClearAndSelect if number == 0 else QItemSelectionModel.SelectionFlag.Select)
    before = _selected(panel)
    opened = []
    panel.asset_selected.connect(opened.append)
    panel.asset_double_clicked.connect(opened.append)
    panel.timeline_switch_requested.connect(opened.append)
    identity = assets[2] if kind == "asset" else timelines[0]
    index = _index(panel, kind, identity)
    view = panel.metadata_shelf if kind == "timeline" and mode == ViewMode.METADATA else panel.grid_view
    view.doItemsLayout()
    qapp.processEvents()
    position = view.itemDelegate().info_button_rect(view.visualRect(index)).center()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert panel._pool_info_popup is not None and panel._pool_info_popup.isVisible()
    assert panel._pool_info_key == (kind, identity)
    assert _selected(panel) == before
    assert opened == []
    QTest.keyClick(panel._pool_info_popup, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert panel._pool_info_popup is None or not panel._pool_info_popup.isVisible()


def test_info_badge_drag_out_cancels_without_starting_selection(pool, qapp) -> None:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    panel, assets, _, _, _ = pool
    index = _index(panel, "asset", assets[0])
    position = panel.grid_delegate.info_button_rect(panel.grid_view.visualRect(index)).center()
    QTest.mousePress(panel.grid_view.viewport(), Qt.MouseButton.LeftButton, pos=position)
    QTest.mouseMove(panel.grid_view.viewport(), position + QPoint(40, -25))
    QTest.mouseRelease(panel.grid_view.viewport(), Qt.MouseButton.LeftButton, pos=position + QPoint(40, -25))
    assert panel._pool_info_popup is None
    assert panel.grid_view._rubber_origin is None


def test_info_popup_replacement_and_project_clear_release_references(pool, qapp) -> None:
    from PyQt6.QtCore import QCoreApplication, QEvent
    panel, assets, _, _, _ = pool
    panel._show_pool_item_info(_index(panel, "asset", assets[0]), panel.grid_view)
    first = panel._pool_info_popup
    panel._show_pool_item_info(_index(panel, "asset", assets[1]), panel.grid_view)
    second = panel._pool_info_popup
    assert second is not first
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert panel._pool_info_popup is second
    panel.clear_project()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert panel._pool_info_popup is None


def test_blank_sidebar_new_bin_targets_master_even_when_nested_bin_selected(pool) -> None:
    from PyQt6.QtCore import QPoint
    panel, _, _, day, _ = pool
    panel._navigate_to_sidebar_bin(day)
    before = set(panel._project.bins)
    blank = QPoint(20, panel.bin_tree.viewport().height() - 8)
    assert panel.bin_tree.itemAt(blank) is None
    menu = panel.bin_tree._build_context_menu(blank)
    assert menu.actions()
    menu.actions()[0].trigger()
    created = set(panel._project.bins) - before
    assert len(created) == 1
    assert panel._project.bins[created.pop()].parent_id == panel._project.root_bin_id


def test_master_node_context_new_bin_uses_clicked_parent(pool) -> None:
    panel, _, _, day, _ = pool
    panel._navigate_to_sidebar_bin(day)
    master = panel.bin_tree._find_bin_item(panel._project.root_bin_id)
    position = panel.bin_tree.visualItemRect(master).center()
    requested = []
    panel.bin_tree.create_bin_in_requested.disconnect()
    panel.bin_tree.create_bin_in_requested.connect(requested.append)
    menu = panel.bin_tree._build_context_menu(position)
    menu.actions()[0].trigger()
    assert requested == [panel._project.root_bin_id]


def test_info_badge_hover_does_not_request_live_preview(pool, monkeypatch) -> None:
    from PyQt6.QtTest import QTest
    panel, assets, _, _, _ = pool
    index = _index(panel, "asset", assets[0])
    position = panel.grid_delegate.info_button_rect(panel.grid_view.visualRect(index)).center()
    scrub = MagicMock()
    monkeypatch.setattr(panel.grid_view, "_handle_hover_scrub", scrub)
    QTest.mouseMove(panel.grid_view.viewport(), position)
    assert not scrub.called


def test_slider_surface_blends_into_toolbar_with_global_theme(pool, qapp) -> None:
    from PyQt6.QtCore import QPoint
    from src.shared.ui.style_sheet import DARK_THEME
    panel, *_ = pool
    old_style = qapp.styleSheet()
    try:
        qapp.setStyleSheet(DARK_THEME)
        qapp.processEvents()
        image = panel.grab().toImage()
        corner = panel.thumb_slider.mapTo(panel, QPoint(2, 2))
        assert image.pixelColor(corner).name() == C.BG_DARK
    finally:
        qapp.setStyleSheet(old_style)


@pytest.mark.real_media
def test_info_popup_shows_probed_real_media_without_project_mutation(qapp) -> None:
    from PyQt6.QtTest import QTest
    from src.features.edit.core.project import probe_media_metadata
    from tests.real_media import require_video
    media = require_video("2K_h264.mp4")
    metadata = probe_media_metadata(str(media))
    project = EditProject.create("Real clip information").import_asset(str(media), "video", metadata)
    panel = MediaPoolPanel()
    panel._offline_refresh_timer.stop()
    panel.set_project(project)
    _refresh(panel)
    asset_id = next(iter(project.assets))
    index = _index(panel, "asset", asset_id)
    panel._show_pool_item_info(index, panel.grid_view)
    assert panel._pool_info_popup is not None
    from PyQt6.QtWidgets import QLabel
    text = " ".join(label.text() for label in panel._pool_info_popup.findChildren(QLabel))
    assert "1920 × 1080" in text
    assert "H264" in text
    assert "25.0" in text
    assert "48000 Hz" in text
    assert panel._project is project
    assert not panel.can_undo()
    panel.clear_project()
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_list_info_anchors_to_selected_item_when_current_item_is_unselected(pool, monkeypatch) -> None:
    panel, assets, _, _, _ = pool
    panel.set_view_mode(ViewMode.LIST)
    rows = {panel.tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole): panel.tree.topLevelItem(row) for row in range(panel.tree.topLevelItemCount())}
    panel.tree.clearSelection()
    panel.tree.setCurrentItem(rows[assets[0]], 0, QItemSelectionModel.SelectionFlag.NoUpdate)
    rows[assets[1]].setSelected(True)
    shown = MagicMock()
    monkeypatch.setattr(panel, "_show_pool_item_info", shown)
    panel._show_selected_pool_info()
    index, view, anchor = shown.call_args.args
    assert index.data(AssetListModel.ITEM_ID_ROLE) == assets[1]
    assert view is panel.tree
    assert anchor == panel.tree.visualItemRect(rows[assets[1]])


def test_new_bin_from_collapsed_master_expands_parent_for_inline_name(pool) -> None:
    from PyQt6.QtCore import QPoint
    panel, *_ = pool
    panel.bin_tree._find_bin_item(panel._project.root_bin_id).setExpanded(False)
    blank = QPoint(20, panel.bin_tree.viewport().height() - 8)
    panel.bin_tree._build_context_menu(blank).actions()[0].trigger()
    assert panel.bin_tree._find_bin_item(panel._project.root_bin_id).isExpanded()


def test_info_badge_is_hover_only_and_on_poster_right(pool, qapp) -> None:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QStyleOptionViewItem
    panel, assets, *_ = pool
    view = panel.grid_view
    index = _index(panel, "asset", assets[0])
    cell = view.visualRect(index)
    poster, _ = panel.grid_delegate._item_rects(cell)
    badge = panel.grid_delegate.info_button_rect(cell)
    assert badge.center().x() > poster.center().x()
    option = QStyleOptionViewItem()
    view._update_info_hover(None)
    assert not panel.grid_delegate.info_button_visible(index, option)
    QTest.mouseMove(view.viewport(), poster.center())
    qapp.processEvents()
    assert panel.grid_delegate.info_button_visible(index, option)
    QTest.mouseMove(view.viewport(), QPoint(view.viewport().width() - 2, view.viewport().height() - 2))
    qapp.processEvents()
    assert not panel.grid_delegate.info_button_visible(index, option)


def test_posters_clip_real_pixels_to_round_corners(pool) -> None:
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QColor, QPainter, QPixmap
    from PyQt6.QtWidgets import QStyleOptionViewItem
    panel, assets, _, _, timelines = pool
    for kind, identity in (("asset", assets[0]), ("timeline", timelines[0])):
        index = _index(panel, kind, identity)
        source = QPixmap(300, 169)
        source.fill(QColor("#fb2323"))
        if kind == "asset":
            panel.asset_model.set_asset_thumbnail(identity, source)
        else:
            panel.asset_model.set_timeline_preview(identity, source)
        image = QPixmap(180, 150)
        image.fill(Qt.GlobalColor.transparent)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 180, 150)
        panel.grid_delegate.set_thumb_size((140, 79))
        painter = QPainter(image)
        panel.grid_delegate.paint(painter, option, index)
        painter.end()
        poster, _ = panel.grid_delegate._item_rects(option.rect)
        pixels = image.toImage()
        assert pixels.pixelColor(poster.topLeft()).alpha() < 80
        assert pixels.pixelColor(poster.center()).alpha() == 255


def test_information_popup_opens_to_right_with_screen_space(pool) -> None:
    from PyQt6.QtCore import QRect
    panel, assets, *_ = pool
    panel._show_pool_item_info(_index(panel, "asset", assets[0]), panel.grid_view)
    popup = panel._pool_info_popup
    anchor = QRect(100, 100, 90, 50)
    screen = QRect(0, 0, 1600, 1000)
    popup.show_at(anchor, screen)
    assert popup.x() > anchor.right()
    assert popup.y() == anchor.top()
    popup.show_at(QRect(1540, 950, 40, 40), screen)
    assert screen.contains(popup.geometry())
    panel._close_pool_info()


def test_scrollbar_appears_only_on_overflow_and_retile_settles(pool, qapp) -> None:
    from PyQt6.QtTest import QTest
    from src.features.edit.ui.media_pool_panel import GridItem
    panel, *_ = pool
    panel.resize(600, 500)
    QTest.qWait(30)
    panel._column_grid.apply_columns(3)
    QTest.qWait(30)
    assert panel.grid_view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert not panel.grid_view.verticalScrollBar().isVisible()
    one = next(item for item in panel.asset_model._items if item.item_type == "asset")
    many = [GridItem("asset", f"layout-{i}", one.data) for i in range(90)]
    panel.asset_model.set_items(many)
    panel._sync_metadata_shelf()
    panel.grid_view.doItemsLayout()
    panel._column_grid.schedule_retile()
    QTest.qWait(40)
    assert panel.grid_view.verticalScrollBar().isVisible()
    grid = panel.grid_view.gridSize()
    QTest.qWait(40)
    assert panel.grid_view.gridSize() == grid
    assert not panel._column_grid._retile_timer.isActive()
    panel.asset_model.set_items([one])
    panel._sync_metadata_shelf()
    panel.grid_view.doItemsLayout()
    panel._column_grid.schedule_retile()
    QTest.qWait(40)
    assert not panel.grid_view.verticalScrollBar().isVisible()
    assert not panel._column_grid._retile_timer.isActive()


def test_master_path_back_button_stays_visible_and_enabled_for_nested_bin(pool) -> None:
    panel, _, footage, day, _ = pool
    assert not panel.btn_breadcrumb_back.isHidden()
    assert not panel.btn_breadcrumb_back.isEnabled()
    panel._navigate_to_sidebar_bin(day)
    assert panel.btn_breadcrumb_back.isEnabled()
    texts = [widget.text() for widget in panel.breadcrumb_path_container.findChildren(QPushButton)]
    assert texts == ["Master", "Footage"]
    panel.btn_breadcrumb_back.click()
    assert panel._current_bin_id == footage
    panel._navigate_to_root()
    assert not panel.btn_breadcrumb_back.isEnabled()
