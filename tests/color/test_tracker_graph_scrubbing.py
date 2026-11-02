"""Tracker ruler scrubs media before a window or tracking binding exists."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.app.controllers.color_controller import ColorControllerMixin
from src.features.color.ui.tracker_panel import TrackerPanel


def test_empty_tracker_ruler_drag_emits_preview_and_exact_release(qtbot) -> None:
    panel = TrackerPanel()
    qtbot.addWidget(panel)
    panel.resize(794, 418)
    panel.graph.set_frame_range(50, 149)
    panel.show()
    view = panel.graph.view
    qtbot.waitExposed(panel)
    events: list[tuple[str, int]] = []
    panel.graph.scrub_started.connect(lambda f: events.append(('start', f)))
    panel.graph.frame_clicked.connect(lambda f: events.append(('seek', f)))
    panel.graph.scrub_finished.connect(lambda f: events.append(('finish', f)))
    start = QPoint(round(view.frame_to_x(50)), 6)
    end = QPoint(round(view.frame_to_x(120)), 6)
    assert panel._bound_shape is None
    assert panel.graph.isEnabled()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)
    assert events[0] == ('start', 50)
    assert ('seek', 120) in events
    assert events[-2:] == [('finish', 120), ('seek', 120)]
    assert view._current_frame == 120
    assert not view._scrubbing


def test_single_frame_media_never_seeks_past_last_frame(qtbot) -> None:
    panel = TrackerPanel()
    qtbot.addWidget(panel)
    panel.graph.set_frame_range(37, 37)
    events: list[int] = []
    panel.graph.frame_clicked.connect(events.append)
    panel.graph.view._seek_from_x(10000)
    assert events == [37]


class _ScrubHost(ColorControllerMixin):
    def __init__(self) -> None:
        self.tracker_panel = MagicMock()
        self.on_video_scrub_started = MagicMock()
        self.on_video_scrub_moved = MagicMock()
        self.on_video_scrub_finished = MagicMock()
        self.on_video_seek = MagicMock()
        self._seek_tracker_frame = MagicMock()

    def _color_source_frame_to_local_frame(self, frame: int) -> int:
        return frame - 50


def test_tracker_drag_uses_existing_async_scrub_lane_and_exact_settle() -> None:
    host = _ScrubHost()
    host._on_tracker_graph_scrub_started(50)
    host._on_tracker_graph_frame_clicked(80)
    host._on_tracker_graph_scrub_finished(110)
    host._on_tracker_graph_frame_clicked(110)
    host.on_video_scrub_started.assert_called_once_with(0)
    host.on_video_scrub_moved.assert_called_once_with(30)
    host.on_video_scrub_finished.assert_called_once_with(60)
    host.on_video_seek.assert_called_once_with(60)
    host._seek_tracker_frame.assert_not_called()
    host.tracker_panel.set_current_frame.assert_called_once_with(110)


def test_clip_range_initializes_without_any_tracking_data() -> None:
    host = _ScrubHost()
    host.video_controller = SimpleNamespace(fps=25)
    host._tracker_frame_context = lambda: ('clip', 'media.mov', 70, 50, 149)
    host._sync_tracker_clip_range()
    host.tracker_panel.graph.set_frame_range.assert_called_once_with(50, 149)
    host.tracker_panel.graph.set_timebase.assert_called_once_with(25.0)
    host.tracker_panel.set_current_frame.assert_called_once_with(70)


def test_tracker_readout_dimensions_accept_real_canvas_qsizef() -> None:
    from PyQt6.QtCore import QSizeF
    host = _ScrubHost()
    host.video_controller = SimpleNamespace(fps=25)
    host.canvas = SimpleNamespace(logical_size=QSizeF(1920, 1080))
    host._tracker_frame_context = lambda: ('clip', 'media.mov', 0, 0, 123)
    host._sync_tracker_clip_range()
    host.tracker_panel.set_source_frame_size.assert_called_once_with(1920, 1080)
