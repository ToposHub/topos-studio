"""Regressions from the real editor screenshot: cursor, ticks and host theme."""
from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH, TimecodeRulerItem
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.shared.ui.style_sheet import DARK_THEME, PANEL_SLIDER_STYLE

pytestmark = pytest.mark.edit_ui


class TickPainter(QPainter):
    def __init__(self, image: QImage) -> None:
        super().__init__(image)
        self.tick_x: list[int] = []

    def drawLine(self, *args: Any) -> None:
        self.tick_x.append(int(args[0]))
        super().drawLine(*args)


@pytest.mark.parametrize("fps", [24.0, 25.0, 29.97, 60.0])
@pytest.mark.parametrize("zoom", [0.01, 0.35, 1.2, 3.0, 20.0])
def test_ruler_ticks_are_unique_and_evenly_subdivided(qapp: Any, fps: float, zoom: float) -> None:
    ruler = TimecodeRulerItem(zoom, fps, 10000)
    image = QImage(1400, 38, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = TickPainter(image)
    try:
        ruler._paint_dynamic_ticks(painter, 0, int(1100 / zoom))
    finally:
        painter.end()
    assert len(painter.tick_x) == len(set(painter.tick_x))
    assert min(b - a for a, b in zip(painter.tick_x, painter.tick_x[1:])) >= 9
    assert ruler._major_tick_interval % ruler._tick_interval == 0
    assert ruler._medium_tick_interval % ruler._tick_interval == 0


@pytest.fixture
def panel(qtbot: Any, qapp: Any) -> Any:
    previous = qapp.styleSheet()
    qapp.setStyleSheet(DARK_THEME)
    widget = TimelinePanel()
    qtbot.addWidget(widget)
    widget.resize(1300, 650)
    clip = ClipInstance("clip", "asset", "v1", 100, 200, 0, 200)
    widget.load_timeline(TimelineSequence("pointer", "Pointer", fps=25.0,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),)))
    widget.show()
    widget.activateWindow()
    QTest.qWait(20)
    widget.timeline_view.set_zoom(3.0)
    yield widget
    widget.close()
    qapp.setStyleSheet(previous)


def test_clip_hover_preserves_the_items_selection_arrow(panel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    view = panel.timeline_view
    item = view._items_map["clip"]
    start = view.mapFromScene(item.mapToScene(QPointF(80, 32)))
    QTest.mouseMove(view.viewport(), start)
    calls: list[Any] = []
    original = view.setCursor

    def record(cursor: Any) -> None:
        calls.append(cursor)
        original(cursor)

    monkeypatch.setattr(view, "setCursor", record)
    for offset in range(1, 12):
        point = view.mapFromScene(item.mapToScene(QPointF(80 + offset, 32)))
        QTest.mouseMove(view.viewport(), point)
        assert view.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    assert Qt.CursorShape.ArrowCursor not in calls


def test_toolbar_separators_are_one_pixel_without_native_bevel(panel: Any) -> None:
    from PyQt6.QtWidgets import QFrame

    assert all(line.width() == 1 for line in panel._toolbar_separators)
    assert all(line.frameShape() == QFrame.Shape.NoFrame for line in panel._toolbar_separators)


@pytest.mark.parametrize("empty", [False, True])
def test_timeline_uses_flat_canvas_below_the_ruler(panel: TimelinePanel, qapp: Any, empty: bool) -> None:
    view = panel.timeline_view
    if empty:
        view.clear()
    qapp.processEvents()

    viewport = view.viewport()
    canvas = viewport.grab()
    image = canvas.toImage()
    ratio = canvas.devicePixelRatio()
    y = round((viewport.height() - 18) * ratio)
    assert {
        image.pixelColor(x, y).name()
        for x in range(round((TRACK_HEADER_WIDTH + 10) * ratio), image.width() - round(12 * ratio))
    } == {"#27272b"}

    toolbar = panel._tools_bar.parentWidget().grab()
    ratio = toolbar.devicePixelRatio()
    assert toolbar.toImage().pixelColor(round(ratio), round(10 * ratio)).name() == "#202024"


@pytest.mark.parametrize("rect_method,shape", [
    ("_get_lock_button_rect", Qt.CursorShape.ArrowCursor),
    ("_get_mute_button_rect", Qt.CursorShape.ArrowCursor),
    ("_get_name_rect", Qt.CursorShape.ArrowCursor),
    ("_get_resize_handle_rect", Qt.CursorShape.SizeVerCursor),
])
def test_track_header_hover_matches_resolve_controls(
    panel: Any, rect_method: str, shape: Qt.CursorShape,
) -> None:
    view = panel.timeline_view
    header = view._track_headers["v1"]
    target = view.mapFromScene(header.mapToScene(getattr(header, rect_method)().center()))
    QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(600, 100)))
    QTest.mouseMove(view.viewport(), target)
    assert view.viewport().cursor().shape() == shape


def test_zoom_slider_inherits_shared_transparent_slider_subcontrols(panel: Any, qapp: Any) -> None:
    from PyQt6.QtWidgets import QSlider, QStyle, QStyleOptionSlider

    def groove_height(slider: QSlider) -> int:
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        return slider.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, slider,
        ).height()

    reference = QSlider(Qt.Orientation.Horizontal)
    reference.resize(panel.zoom_slider.size())
    reference.setStyleSheet(PANEL_SLIDER_STYLE)
    assert groove_height(panel.zoom_slider) == groove_height(reference) > 0
    assert not panel.zoom_slider.styleSheet()
    style = qapp.styleSheet()
    assert "QSlider::sub-page, QSlider::add-page { background: transparent; border: none; }" in style
