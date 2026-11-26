"""Timeline zoom and clipped-lane pointer regressions."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QAction, QKeySequence, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.selection import TimelineSelection
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui


def _long_sequence() -> TimelineSequence:
    clip = ClipInstance("clip", "asset", "v1", 100, 4500, 0, 4500)
    return TimelineSequence(
        "long-timeline", "Long timeline",
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
    )


def _show(widget: QWidget, qtbot: Any) -> None:
    qtbot.addWidget(widget)
    widget.resize(1200, 650)
    widget.show()
    widget.activateWindow()
    QTest.qWait(10)


def _set_detail(view: TimelineView) -> None:
    view.set_zoom(3.0)
    view.set_playhead(950)
    view.horizontalScrollBar().setValue(2400)


def _playhead_x(view: TimelineView) -> int:
    return view.mapFromScene(view._playhead_item.pos()).x()


@pytest.mark.parametrize("use_window_shortcut", [False, True])
def test_shift_z_restores_previous_view_and_keeps_selection(qtbot: Any, use_window_shortcut: bool) -> None:
    view = TimelineView()
    if use_window_shortcut:
        window = QMainWindow()
        content = QWidget(window)
        layout = QVBoxLayout(content)
        layout.addWidget(view)
        recipient = QPushButton("Outside the timeline", content)
        layout.addWidget(recipient)
        window.setCentralWidget(content)
        controller = EditControllerMixin.__new__(EditControllerMixin)
        controller.timeline_panel = SimpleNamespace(timeline_view=view)
        fit = QAction("Fit", window)
        fit.setShortcut(QKeySequence("Shift+Z"))
        fit.triggered.connect(controller.on_timeline_fit)
        window.addAction(fit)
        _show(window, qtbot)
    else:
        _show(view, qtbot)
        recipient = view

    timeline = _long_sequence()
    view.load_timeline(timeline)
    _set_detail(view)
    selection = TimelineSelection().with_clip_selected("clip", "v1", timeline.timeline_id)
    view.set_selection(selection)
    before = view._pixels_per_frame, view.horizontalScrollBar().value()
    recipient.setFocus()
    QTest.keyClick(recipient, Qt.Key.Key_Z, Qt.KeyboardModifier.ShiftModifier)
    assert view._pixels_per_frame < before[0]
    assert view.horizontalScrollBar().value() == 0
    QTest.keyClick(recipient, Qt.Key.Key_Z, Qt.KeyboardModifier.ShiftModifier)
    QTest.qWait(15)
    assert (view._pixels_per_frame, view.horizontalScrollBar().value()) == before
    assert view.get_playhead_frame() == 950
    assert view._selection == selection
    assert view.get_timeline() is timeline
    assert not view._command_stack.can_undo()


def test_zoom_shortcut_slider_and_menu_use_playhead_anchor(qtbot: Any) -> None:
    panel = TimelinePanel()
    _show(panel, qtbot)
    panel.load_timeline(_long_sequence())
    view = panel.timeline_view
    _set_detail(view)
    anchor = _playhead_x(view)
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.timeline_panel = panel

    for zoom in (
        lambda: QTest.keyClick(view, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier),
        controller.on_timeline_zoom_in,
        lambda: panel.zoom_slider.setValue(panel.zoom_slider.value() - 80),
        controller.on_timeline_zoom_out,
    ):
        before = view._pixels_per_frame
        zoom()
        assert view._pixels_per_frame != before
        assert abs(_playhead_x(view) - anchor) <= 1
        assert panel.export_presentation_state()["custom_scroll"] == view.horizontalScrollBar().value()


def test_zoom_brings_an_offscreen_playhead_into_the_content_area(qtbot: Any) -> None:
    view = TimelineView()
    _show(view, qtbot)
    view.load_timeline(_long_sequence())
    _set_detail(view)
    view.horizontalScrollBar().setValue(0)
    center_x = (view._track_headers["v1"].boundingRect().width() + view.viewport().width()) / 2
    assert _playhead_x(view) >= view.viewport().width()
    view.zoom_in()
    assert abs(_playhead_x(view) - center_x) <= 1


def test_clipped_audio_lane_cannot_take_clicks_or_marquee_selection(qtbot: Any) -> None:
    view = TimelineView()
    _show(view, qtbot)
    audio = ClipInstance("audio", "asset", "a1", 100, 80, 0, 80)
    tracks = tuple(
        EditTrack(f"a{index + 1}", f"A{index + 1}", "audio", index,
                  clips=(audio,) if index == 0 else ())
        for index in range(12)
    )
    view.load_timeline(TimelineSequence(
        "scrolled", "Scrolled", video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=tracks,
    ))
    view.set_zoom(3.0)
    view.set_layout_state({**view.layout_state(), "audio_scroll_offset": 150})
    item = view._items_map["audio"]
    rect = item.sceneBoundingRect()
    assert rect.bottom() < view._layout_engine.area_layout().audio_area.top()
    assert view._layout_engine.area_layout().video_area.contains(rect.center())
    center = view.mapFromScene(rect.center())
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    assert not view._selection.selected_clip_ids

    start = view.mapFromScene(QPointF(rect.left() - 12.0, rect.center().y() - 8.0))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), center)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    assert not view._selection.selected_clip_ids
    assert view._track_headers["a1"].shape().isEmpty()
    assert view._layout_engine.track_boundary_at_y(item.y()) is None

    view.set_layout_state({**view.layout_state(), "audio_scroll_offset": 0})
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=view.mapFromScene(item.sceneBoundingRect().center()))
    assert view._selection.selected_clip_ids == ("audio",)


def test_track_resize_uses_the_pinned_header_after_horizontal_scrolling(qtbot: Any) -> None:
    view = TimelineView()
    _show(view, qtbot)
    view.load_timeline(_long_sequence())
    _set_detail(view)
    top = view._layout_engine.track_top("v1")
    y = view.mapFromScene(QPointF(0.0, top)).y()
    start = QPoint(50, y)
    end = start - QPoint(0, 20)
    height = view._layout_engine.track_height("v1")
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view._layout_engine.track_height("v1") == height + 20


def _wheel_sequence() -> TimelineSequence:
    return replace(_long_sequence(), audio_tracks=tuple(
        EditTrack(f"a{index + 1}", f"A{index + 1}", "audio", index) for index in range(12)
    ))


def _wheel(
    view: TimelineView, pos: QPoint, modifiers: Qt.KeyboardModifier,
    *, pixels: QPoint | None = None,
) -> None:
    event = QWheelEvent(
        QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
        pixels if pixels is not None else QPoint(),
        QPoint(0, 120) if pixels is None else QPoint(),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(view.viewport(), event)


def test_edit_wheel_zooms_scrolls_and_resizes_the_expected_lane(qtbot: Any) -> None:
    view = TimelineView()
    _show(view, qtbot)
    sequence = _wheel_sequence()
    view.load_timeline(sequence)
    _set_detail(view)
    view.set_layout_state({**view.layout_state(), "audio_scroll_offset": 120,
                           "track_heights": {"a2": 86}})
    audio_mid = view._layout_engine.area_layout().audio_area.center().y()
    pos = view.mapFromScene(QPointF(view.mapToScene(400, 0).x(), audio_mid))
    anchor_x = _playhead_x(view)
    assert anchor_x != pos.x()

    initial_zoom = view._pixels_per_frame
    _wheel(view, pos, Qt.KeyboardModifier.AltModifier)
    assert view._pixels_per_frame > initial_zoom
    assert abs(_playhead_x(view) - anchor_x) <= 1

    zoom = view._pixels_per_frame
    bar = view.horizontalScrollBar()
    scroll_x = bar.value()
    _wheel(view, pos, Qt.KeyboardModifier.ControlModifier)
    assert bar.value() == scroll_x - 15
    assert view._pixels_per_frame == zoom

    heights = [view._layout_engine.track_height(track_id) for track_id in ("v1", "a1", "a2")]
    _wheel(view, pos, Qt.KeyboardModifier.ShiftModifier)
    assert [view._layout_engine.track_height(track_id) for track_id in ("v1", "a1", "a2")] == [
        heights[0], heights[1] + 15, heights[2] + 15,
    ]

    audio_scroll = view._audio_v_scroll.value()
    video_scroll = view._video_v_scroll.value()
    scroll_x = bar.value()
    _wheel(view, pos, Qt.KeyboardModifier.NoModifier, pixels=QPoint(2, 18))
    assert view._audio_v_scroll.value() == audio_scroll - 18
    assert view._video_v_scroll.value() == video_scroll
    assert bar.value() == scroll_x
    _wheel(view, pos, Qt.KeyboardModifier.NoModifier, pixels=QPoint(21, 3))
    assert bar.value() == scroll_x - 21
    assert view._audio_v_scroll.value() == audio_scroll - 18
    assert view.get_timeline() is sequence


@pytest.mark.parametrize("mode", ["mixer", "deliver"])
def test_other_workspaces_keep_control_wheel_zoom_and_shift_wheel_scroll(qtbot: Any, mode: str) -> None:
    panel = TimelinePanel()
    _show(panel, qtbot)
    timeline = _wheel_sequence()
    panel.load_timeline(timeline)
    panel.set_display_mode(mode)
    view = panel.timeline_view
    view.set_audio_only(mode == "mixer")
    _set_detail(view)
    view.set_layout_state({**view.layout_state(), "audio_scroll_offset": 120})

    audio_mid = view._layout_engine.area_layout().audio_area.center().y()
    pos = view.mapFromScene(QPointF(view.mapToScene(400, 0).x(), audio_mid))
    zoom = view._pixels_per_frame
    _wheel(view, pos, Qt.KeyboardModifier.ControlModifier)
    assert view._pixels_per_frame > zoom
    height = view._layout_engine.track_height("a1")
    scroll_y = view._audio_v_scroll.value()
    _wheel(view, pos, Qt.KeyboardModifier.ShiftModifier)
    assert view._audio_v_scroll.value() < scroll_y
    assert view._layout_engine.track_height("a1") == height
