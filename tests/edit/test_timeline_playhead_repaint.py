"""Foreground overlays must invalidate pixels outside scene item bounds."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt6.QtGui import QPaintEvent, QRegion, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QGraphicsView

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import TRACK_HEADER_WIDTH
from src.features.edit.ui.timeline_view import TimelineView


pytestmark = pytest.mark.edit_ui


class _PaintRegions(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.region = QRegion()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QPaintEvent):
            self.region |= event.region()
        return False


@pytest.fixture
def view(qtbot: Any) -> TimelineView:
    widget = TimelineView()
    qtbot.addWidget(widget)
    widget.resize(1200, 650)
    widget.show()
    widget.load_timeline(TimelineSequence(
        "playhead-repaint", "Repaint",
        video_tracks=(EditTrack("v1", "Video", "video", 0, clips=(
            ClipInstance("clip", "asset", "v1", 0, 4000, 0, 4000),
        )),),
        audio_tracks=(EditTrack("a1", "Audio", "audio", 0),),
    ))
    widget.set_playhead_follow_enabled(False)
    widget.set_zoom(1.0)
    widget.horizontalScrollBar().setValue(0)
    widget.set_playhead(300)
    QTest.qWait(30)
    return widget


@pytest.mark.parametrize("wheel", [False, True], ids=["slider", "command-wheel"])
def test_zoom_erases_old_playhead_in_empty_track_areas(view: TimelineView, wheel: bool) -> None:
    recorder = _PaintRegions(view.viewport())
    view.viewport().installEventFilter(recorder)
    # A point below the clips used to retain the stem: no scene item there
    # becomes dirty when the detached playhead changes its X coordinate.
    y = view.viewport().height() - 30
    for zoom in (1.25, 1.5, 0.8, 1.0):
        old_x = view.mapFromScene(view._playhead_item.scenePos()).x()
        recorder.region = QRegion()
        if wheel:
            # Qt maps macOS Command to ControlModifier. Anchor at frame zero
            # so the scrollbar stays still and cannot mask a missing repaint.
            event = QWheelEvent(
                QPointF(TRACK_HEADER_WIDTH, 20), QPointF(TRACK_HEADER_WIDTH, 20),
                QPoint(), QPoint(0, 120 if zoom > view._pixels_per_frame else -120),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.NoScrollPhase, False,
            )
            QApplication.sendEvent(view.viewport(), event)
        else:
            view.set_zoom(zoom)
        QTest.qWait(15)
        new_x = view.mapFromScene(view._playhead_item.scenePos()).x()
        assert new_x != old_x
        assert view.horizontalScrollBar().value() == 0
        assert recorder.region.contains(QPoint(old_x, y))
        assert recorder.region.contains(QPoint(new_x, y))


def test_playback_keeps_repainting_only_narrow_playhead_strips(view: TimelineView) -> None:
    recorder = _PaintRegions(view.viewport())
    view.viewport().installEventFilter(recorder)
    old_x = view.mapFromScene(view._playhead_item.scenePos()).x()
    view.set_playhead(305)
    QTest.qWait(15)
    y = view.viewport().height() - 30
    assert recorder.region.contains(QPoint(old_x, y))
    assert not recorder.region.contains(QPoint(view.viewport().width() - 30, y))
    assert view.viewportUpdateMode() == QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate
