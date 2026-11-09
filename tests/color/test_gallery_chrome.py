"""Gallery geometry, selection and comparison setup use the existing signal routes."""
from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QImage, QColor, QIcon
from PyQt6.QtWidgets import QDialogButtonBox, QStyleOptionViewItem
from src.features.color.ui import presets_panel
from src.features.color.ui.gallery_compare import GalleryCompareDialog
from src.features.color.ui.gallery_icons import GALLERY_ICON_NAMES, gallery_icon
from src.shared.ui.style_sheet import DARK_THEME


@pytest.fixture
def panel(qtbot, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(presets_panel, "_resolve_presets_root", lambda: str(tmp_path / "Presets"))
    widget = presets_panel.PresetsPanel()
    qtbot.addWidget(widget)
    for name in ("Still_01", "Still_02", "Still_03"):
        path = tmp_path / "Presets" / "User" / (name + ".topos")
        path.write_bytes(b"fixture")
        image = QImage(320, 180, QImage.Format.Format_RGB32)
        image.fill(QColor("#486977"))
        image.save(str(path.with_suffix(".thumb.jpg")))
    widget.scan_presets()
    widget.resize(504, 440)
    widget.show()
    qtbot.wait(20)
    yield widget


def test_gallery_is_single_toolbar_with_fixed_aspect_stills(panel, qtbot):
    assert panel._gallery.toolbar.height() == 28
    assert panel.search_edit.parentWidget().isHidden()
    assert panel.folder_combo.isHidden()
    assert panel.list_widget.iconSize() == QSize(108, 61)
    assert panel.list_widget.visualItemRect(panel.list_widget.item(2)).top() == 0
    panel.zoom_slider.setValue(160)
    panel.resize(650, 440)
    qtbot.wait(20)
    assert panel.list_widget.iconSize() == QSize(160, 90)
    assert panel.list_widget.gridSize().height() > 90


def test_grid_list_search_preserve_model_and_selected_identity(panel, qtbot):
    selected = panel.list_widget.item(1)
    panel.list_widget.setCurrentItem(selected)
    path = selected.data(Qt.ItemDataRole.UserRole)
    view = panel.list_widget
    panel._gallery.set_view_mode("list")
    qtbot.wait(20)
    assert panel.list_widget is view
    assert selected in view.selectedItems()
    assert panel._gallery.grid.header.height() == 20
    assert view.visualItemRect(selected).height() == 27
    panel._gallery.set_search_visible(True)
    panel.search_edit.setText("02")
    assert not selected.isHidden()
    panel._gallery.set_view_mode("grid")
    qtbot.wait(20)
    assert view.currentItem().data(Qt.ItemDataRole.UserRole) == path
    qtbot.keyClick(panel.search_edit, Qt.Key.Key_Escape)
    assert panel.search_edit.text() == ""
    assert panel._gallery.search_row.isHidden()


def test_selection_does_not_apply_and_double_click_applies_once(panel, qtbot):
    calls = []
    panel.grade_applied.connect(calls.append)
    item = panel.list_widget.item(0)
    position = panel.list_widget.visualItemRect(item).center()
    qtbot.mouseClick(panel.list_widget.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert calls == []
    qtbot.mouseDClick(panel.list_widget.viewport(), Qt.MouseButton.LeftButton, pos=position)
    assert calls == [item.data(Qt.ItemDataRole.UserRole)]


def test_narrow_dark_theme_keeps_core_tools_inside_panel(panel, qtbot):
    panel.setStyleSheet(DARK_THEME + panel.styleSheet())
    panel.resize(280, 360)
    qtbot.wait(20)
    for button in (panel._gallery.sidebar_button, panel._gallery.grid_button,
                   panel._gallery.list_button, panel._gallery.search_button,
                   panel._gallery.more_button):
        assert not button.isHidden()
        assert button.mapTo(panel, button.rect().topRight()).x() < panel.width()
    assert not panel._gallery.import_button.isEnabled()
    panel.zoom_slider.setValue(256)
    panel._gallery.sidebar_button.setChecked(True)
    qtbot.wait(20)
    assert panel.list_widget.iconSize().width() <= panel.list_widget.viewport().width()


def test_viewport_split_setup_routes_without_claiming_grid_is_enabled(qapp):
    from tests.color.test_color_viewport_chrome import _CanvasHost
    from src.features.color.ui.color_viewport_chrome import ColorViewportChrome
    from PyQt6.QtWidgets import QWidget
    owner = QWidget()
    canvas = _CanvasHost(owner)
    chrome = ColorViewportChrome(canvas)
    requests = []
    chrome.gallery_compare_requested.connect(requests.append)
    chrome.split_screen_button.click()
    assert requests == ["split"]
    assert not chrome._options_stub_actions["compare.split_screen"].isEnabled()
    owner.deleteLater()


def test_paint_no_longer_opens_grade_packages(panel, monkeypatch):
    def fail(*_args):
        raise AssertionError("Paint must not open .topos metadata")
    monkeypatch.setattr(panel, "_get_preset_meta", fail)
    assert not panel.grab().isNull()


def test_scrollbar_only_appears_for_overflow_and_hides_after_filtering(panel, qtbot):
    bar = panel.list_widget.verticalScrollBar()
    panel.list_widget.clear()
    panel._column_grid.pin_items()
    qtbot.wait(30)
    assert not bar.isVisible()
    from PyQt6.QtWidgets import QListWidgetItem
    for i in range(50):
        panel.list_widget.addItem(QListWidgetItem(f"Still {i}"))
    panel._column_grid.pin_items()
    qtbot.wait(30)
    assert bar.isVisible()
    assert bar.maximum() > 0
    panel._gallery.set_search_visible(True)
    panel.search_edit.setText("no matches")
    qtbot.wait(30)
    assert not bar.isVisible()
    panel.search_edit.clear()
    qtbot.wait(30)
    assert bar.isVisible()
    panel.resize(700, 2400)
    qtbot.wait(30)
    assert not bar.isVisible()


def test_zoom_slider_inherits_global_handle_and_has_no_panel_background(panel):
    assert panel.zoom_slider.styleSheet() == "QSlider { background: transparent; }"
    assert "QSlider::handle:horizontal" in DARK_THEME
    assert "background: #cc6600" in DARK_THEME


def test_wipe_setup_emits_original_reference_path_once(panel, qtbot):
    path = panel.list_widget.item(0).data(Qt.ItemDataRole.UserRole)
    dialog = GalleryCompareDialog([("Still_01", path)], [path], "wipe", panel)
    qtbot.addWidget(dialog)
    calls = []
    dialog.wipe_requested.connect(calls.append)
    button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert button.isEnabled()
    button.click()
    assert calls == [path]


def test_split_setup_reviews_layout_without_enabling_renderer(panel, qtbot):
    entries = [(panel.list_widget.item(i).text(), panel.list_widget.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(3)]
    dialog = GalleryCompareDialog(entries, [entry[1] for entry in entries], "split", panel)
    qtbot.addWidget(dialog)
    assert len(dialog.references.selectedItems()) == 3
    assert dialog.layout_choice.isEnabled()
    dialog.layout_choice.setCurrentIndex(1)
    assert dialog.layout_choice.currentData() == "grid_2x2"
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert dialog.status.text()


@pytest.mark.parametrize("name", sorted(GALLERY_ICON_NAMES))
def test_gallery_vectors_render_at_hidpi(qapp, name):
    pixmap = gallery_icon(name).pixmap(QSize(20, 20), 2.0, QIcon.Mode.Normal)
    assert not pixmap.isNull()
    assert pixmap.devicePixelRatio() == 2.0
