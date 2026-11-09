"""Color-specific chrome retains the shared transport and signal contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QRectF, QSize, QSizeF, QPointF, Qt
from PyQt6.QtWidgets import QDockWidget, QGraphicsScene, QGraphicsView, QMainWindow, QWidget

from src.features.color.ui.canvas import Canvas, VideoControlBar, ViewportMenuBar
from src.features.color.ui.color_viewport_chrome import ColorViewportChrome


class _CanvasHost(QGraphicsView):
    """Native QWidget viewport: no GL context, renderer or decoder in UI QA."""

    update_video_control_bar_position = Canvas.update_video_control_bar_position
    _on_color_monitor_toggled = Canvas._on_color_monitor_toggled

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._show_mask_overlays = False
        self.engine = SimpleNamespace(show_mask_mode=False, split_view_active=False)
        self.viewport_menu = ViewportMenuBar(self)
        self.video_control_bar = VideoControlBar(self)
        self.color_status_bar = QWidget(self.viewport())
        self.color_status_bar.setFixedSize(150, 20)
        self.color_status_bar.hide()

    def _update_lod(self) -> bool:
        return False


@pytest.fixture
def chrome(qapp):
    owner = QWidget()
    host = _CanvasHost(owner)
    host.setWindowFlag(Qt.WindowType.Window, True)
    host.resize(1000, 600)
    adapter = ColorViewportChrome(host)
    host.viewport_menu.resize(1000, 30)
    host.viewport_menu.show()
    host.show()
    qapp.processEvents()
    yield adapter
    host.hide()
    # Keep native ownership deterministic through the end of this fixture.
    owner.deleteLater()


def test_color_transport_keeps_original_objects_and_big_orange_playback(chrome, qapp) -> None:
    bar = chrome.transport
    slider, ruler, timecode = bar.slider, bar.ruler_slider, bar.timecode_label
    bar.show_for_video()
    qapp.processEvents()
    assert bar.parentWidget() is chrome.canvas
    assert bar.height() == 56
    assert bar.slider is slider and bar.ruler_slider is ruler
    assert bar.timecode_label is timecode
    assert slider.property("colorMonitorScrubber")
    assert slider.height() == 20
    for button in (bar.btn_first, bar.btn_prev, bar.btn_next, bar.btn_last, bar.btn_loop):
        assert button.size() == QSize(28, 28)
        assert button.iconSize() == QSize(15, 15)
    assert bar.btn_play.size() == QSize(32, 28)
    assert bar.btn_play.iconSize() == QSize(18, 18)
    assert bar.btn_first.parentWidget() is chrome.transport_center
    assert bar.label_frame.isHidden()


def test_color_fusion_image_transitions_restore_original_control_owners(chrome, qapp) -> None:
    bar = chrome.transport
    for _ in range(2):
        bar.show_for_video_with_io()
        qapp.processEvents()
        assert bar.height() == 74
        assert bar.slider.parentWidget() is chrome.row
        assert not bar.slider.property("colorMonitorScrubber")
        assert not bar.ruler_slider.isHidden()
        assert not bar.btn_mark_in.isHidden()
        assert bar.btn_play.parentWidget() is chrome.row
        bar.show_for_video()
        qapp.processEvents()
        assert bar.height() == 56
        assert bar.slider.parentWidget() is bar
        assert bar.btn_play.parentWidget() is chrome.transport_center
        assert bar.ruler_slider.isHidden()
        assert bar.btn_mark_in.isHidden()
        bar.hide_for_image()
        assert bar.isHidden()
        assert not bar.btn_play.isEnabled()
    assert chrome.toolbar.height() == 30


def test_composite_shared_bar_without_adapter_retains_legacy_height(qapp) -> None:
    bar = VideoControlBar()
    bar.show_for_video_with_io()
    assert bar.height() == 74
    bar.show_for_video()
    assert bar.height() == 36
    assert not hasattr(bar, "_color_chrome")
    bar.close()


def test_live_scrub_updates_both_timecodes_and_emits_one_final_seek(chrome) -> None:
    bar = chrome.transport
    bar.set_total_frames(250)
    bar.set_frame_rate(25.0)
    events: list[tuple[str, int]] = []
    bar.scrub_started.connect(lambda value: events.append(("start", value)))
    bar.scrub_moved.connect(lambda value: events.append(("move", value)))
    bar.scrub_finished.connect(lambda value: events.append(("finish", value)))
    bar.seek_requested.connect(lambda value: events.append(("seek", value)))
    bar.slider.setSliderDown(True)
    bar.slider.setValue(100)
    assert bar.timecode_label.text() == "00:00:04:00"
    assert chrome.header_timecode.text() == "00:00:04:00"
    bar.slider.setSliderDown(False)
    assert events == [("start", 0), ("move", 100), ("finish", 100), ("seek", 100)]
    bar.set_time_info("01:02:03:04")
    assert chrome.header_timecode.text() == "01:02:03:04"


def test_header_selector_stays_exactly_centered_without_overlapping_tools(chrome, qapp) -> None:
    toolbar = chrome.toolbar
    toolbar.set_timeline_choices({"one": SimpleNamespace(name="Timeline 1")}, "one")
    toolbar.resize(1000, 30)
    qapp.processEvents()
    chrome.position_title()
    selector = toolbar._timeline_selector
    assert not selector.isHidden()
    assert selector.geometry().center().x() == toolbar.rect().center().x()
    assert selector.geometry().left() > chrome.header_left.geometry().right()
    assert selector.geometry().right() < chrome.header_right.geometry().left()
    for width in (800, 520, 400):
        toolbar.resize(width, 30)
        qapp.processEvents()
        assert chrome.options_button.mapTo(toolbar, chrome.options_button.rect().topRight()).x() < toolbar.width()


@pytest.mark.parametrize("width", [1000, 700, 500])
def test_dark_theme_keeps_a_single_short_timeline_title_visible_and_centered(chrome, qapp, width) -> None:
    from src.shared.ui.style_sheet import DARK_THEME

    previous_style = qapp.styleSheet()
    try:
        qapp.setStyleSheet(DARK_THEME)
        toolbar = chrome.toolbar
        toolbar.set_timeline_choices({"short": SimpleNamespace(name="T1")}, "short")
        toolbar.resize(width, 30)
        qapp.processEvents()
        chrome.position_title()
        selector = toolbar._timeline_selector
        assert not selector.isHidden()
        assert selector.currentText() == "T1"
        assert selector.width() >= 80
        assert selector.geometry().center().x() == toolbar.rect().center().x()
    finally:
        qapp.setStyleSheet(previous_style)


def test_menu_mask_sync_uses_canvas_even_after_toolbar_reparent(chrome) -> None:
    toolbar = chrome.toolbar
    unrelated = QWidget(chrome.canvas)
    toolbar.setParent(unrelated)
    chrome.canvas.engine.show_mask_mode = True
    chrome.canvas.engine.split_view_active = True
    emitted: list[bool] = []
    toolbar.mask_toggled.connect(emitted.append)
    toolbar.sync_menu_state()
    assert toolbar.mask_btn.isChecked()
    assert toolbar.mask_act.isChecked()
    assert toolbar.split_btn.isChecked()
    assert emitted == []


def test_original_commands_remain_reachable_from_options_view_menu(chrome) -> None:
    toolbar = chrome.toolbar
    view_action = chrome.options_button.menu().actions()[0]
    assert view_action.menu() is toolbar.view_menu
    actions = toolbar.view_menu.actions()
    for action in (toolbar.mask_act, toolbar.split_act, toolbar.ab_toggle_act,
                   toolbar.capture_reference_act, toolbar.clear_reference_act,
                   toolbar.capture_still_act, toolbar.fullscreen_act, toolbar.focus_act):
        assert action in actions
    assert toolbar.workspace_menu in [action.menu() for action in actions]


def test_status_bar_stays_above_color_footer(chrome, qapp) -> None:
    bar = chrome.transport
    status = chrome.canvas.color_status_bar
    bar.show_for_video()
    chrome.toolbar.color_monitor_act.setChecked(True)
    status.show()
    qapp.processEvents()
    chrome.canvas.update_video_control_bar_position()
    status_bottom = status.mapTo(chrome.canvas, status.rect().bottomLeft()).y()
    assert status_bottom < bar.geometry().top()
    # Repeated metadata refreshes do not resize/show the already visible
    # status widget.  They must use the same reserved-footer positioning.
    Canvas.update_color_monitor(chrome.canvas)
    Canvas.update_color_monitor(chrome.canvas)
    status_bottom = status.mapTo(chrome.canvas, status.rect().bottomLeft()).y()
    assert status_bottom < bar.geometry().top()


def test_actual_size_and_zoom_menu_refresh_label_from_real_canvas_lod_hook(chrome) -> None:
    # Exercise the actual UI notification hook without supplying a GPU engine.
    # Its early guard must still let zoom reflect the native view transform.
    canvas = chrome.canvas
    canvas.color_viewport_chrome = chrome
    canvas.engine = None
    canvas._update_lod = lambda: Canvas._update_lod(canvas)
    canvas.viewport_menu._set_actual_size()
    assert chrome.zoom_button.text() == "100%"
    canvas.viewport_menu._zoom_by_factor(0.5)
    assert chrome.zoom_button.text() == "50%"


def test_enhanced_viewer_follows_canvas_host_while_dock_floats(chrome, qapp) -> None:
    calls: list[bool] = []
    assert not chrome.enhanced_button.isEnabled()
    assert not chrome.toolbar.focus_act.isEnabled()
    host = QMainWindow()
    host.toggle_focus_mode = lambda: calls.append(True)
    dock = QDockWidget("Viewport", host)
    host.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
    chrome.canvas.setParent(dock)
    dock.setWidget(chrome.canvas)
    dock.setFloating(True)
    chrome._sync_focus_capability()
    assert chrome.enhanced_button.isEnabled()
    assert chrome.toolbar.focus_act.isEnabled()
    chrome.enhanced_button.click()
    chrome.toolbar.focus_act.trigger()
    assert calls == [True, True]
    chrome.canvas.setParent(None)
    chrome._sync_focus_capability()
    assert not chrome.enhanced_button.isEnabled()
    assert not chrome.toolbar.focus_act.isEnabled()
    host.deleteLater()


def test_header_uses_right_chevron_and_transparent_transport_groups(chrome) -> None:
    assert chrome.zoom_button.width() == 62
    assert chrome.zoom_button.icon().isNull()
    for button in (chrome.toolbar.mask_btn, chrome.toolbar.split_btn,
                   chrome.toolbar.guides_menu_btn, chrome.options_button):
        assert button.size() == QSize(32, 28)
    for group in (chrome.left_tools, chrome.transport_center, chrome.right_timecode):
        assert "background: transparent" in group.styleSheet()


def test_color_display_toggles_keep_fine_vector_artwork(chrome) -> None:
    toolbar = chrome.toolbar
    for button in (toolbar.mask_btn, toolbar.split_btn):
        original = button.icon().pixmap(QSize(20, 20)).toImage()
        button.setChecked(True)
        button.setChecked(False)
        assert button.icon().pixmap(QSize(20, 20)).toImage() == original
    assert toolbar.guides_menu_btn.iconSize() == QSize(28, 20)
    assert chrome.proxy_button.iconSize() == QSize(28, 20)


def test_color_native_and_qt_backdrops_use_the_same_191919_surround(qapp) -> None:
    from src.features.color.ui.gl_viewport import VIEWER_BACKGROUND
    from PyQt6.QtGui import QColor

    assert QColor.fromRgbF(*VIEWER_BACKGROUND).name() == "#191919"
    # Construct the real Canvas with a native QWidget viewport instead of a
    # QOpenGLWidget so no renderer/context/decoder is created in this test.
    from unittest.mock import patch

    with patch("src.features.color.ui.canvas.GLCanvasViewport", QWidget):
        canvas = Canvas()
    assert canvas.backgroundBrush().color().name() == "#191919"
    canvas.close()


@pytest.mark.parametrize("video_visible", [True, False])
def test_actual_canvas_fit_keeps_whole_image_above_color_transport(chrome, qapp, video_visible) -> None:
    canvas = chrome.canvas
    scene = QGraphicsScene(canvas)
    canvas.setScene(scene)
    canvas.scene = scene
    canvas.setFrameStyle(0)
    canvas.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    canvas.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    canvas.logical_size = QSizeF(1920.0, 1080.0)
    scene.setSceneRect(QRectF(0.0, 0.0, 1920.0, 1080.0))
    canvas.engine = None
    canvas._update_scene_pan_bounds = lambda: Canvas._update_scene_pan_bounds(canvas)
    canvas._refresh_video_frame_view = lambda: False
    bar = chrome.transport
    if video_visible:
        bar.show_for_video()
    else:
        bar.hide_for_image()
    qapp.processEvents()
    Canvas.fit_to_window(canvas)
    reserved = bar.height() if video_visible else 0
    usable_height = canvas.viewport().height() - reserved
    bottom = canvas.mapFromScene(QPointF(1920.0, 1080.0)).y()
    center = canvas.mapFromScene(QPointF(960.0, 540.0)).y()
    assert bottom <= usable_height + 1
    assert center == pytest.approx(usable_height / 2.0, abs=1.5)
