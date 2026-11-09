"""Track output controls retain editable gray clips in the timeline."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QStyleOptionGraphicsItem

from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import ClipItem
from src.features.edit.ui.timeline_view import TimelineView


pytestmark = pytest.mark.edit_ui


@pytest.fixture
def view(qtbot: Any) -> TimelineView:
    widget = TimelineView()
    qtbot.addWidget(widget)
    widget.resize(1200, 650)
    widget.show()
    widget.load_timeline(TimelineSequence(
        "track-output", "Track output",
        video_tracks=(EditTrack("v1", "Video", "video", 0, clips=(
            ClipInstance("video", "asset", "v1", 0, 1000, 0, 1000),
        )),),
        audio_tracks=(EditTrack("a1", "Audio", "audio", 0, clips=(
            ClipInstance("audio", "asset", "a1", 0, 1000, 0, 1000),
        )),),
    ))
    widget.set_zoom(1.0)
    QTest.qWait(10)
    return widget


def test_eye_disables_output_but_keeps_clips_visible_and_editable(view: TimelineView) -> None:
    header = view._track_headers["v1"]
    point = view.mapFromScene(header.mapToScene(header._get_mute_button_rect().center()))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    timeline = view.get_timeline()
    assert not timeline.video_tracks[0].is_visible
    video = view._items_map["video"]
    assert video.isVisible()
    assert not video._is_track_output_enabled
    assert video._is_enabled  # Clip enable state is independent of track output.
    assert view._items_map["audio"]._is_track_output_enabled
    assert not TimelineRenderPlan(timeline).get_frame_plan(0).layers
    body = view.mapFromScene(video.mapToScene(QPointF(50, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=body)
    assert "video" in view._selection.selected_clip_ids
    view.undo()
    video = view._items_map["video"]
    assert video.isVisible() and video._is_track_output_enabled
    assert TimelineRenderPlan(view.get_timeline()).get_frame_plan(0).layers
    view.redo()
    video = view._items_map["video"]
    assert video.isVisible() and not video._is_track_output_enabled


def test_disabled_output_survives_reload_zoom_and_viewport_culling(view: TimelineView) -> None:
    timeline = view.get_timeline()
    hidden = replace(timeline.video_tracks[0], is_visible=False)
    view.load_timeline(replace(timeline, video_tracks=(hidden,)))
    item = view._items_map["video"]
    assert item.isVisible() and not item._is_track_output_enabled
    for zoom in (0.5, 2.0, 1.0):
        view.set_zoom(zoom)
        view.horizontalScrollBar().setValue(40)
        view._update_clip_visibility()
        assert item.isVisible() and not item._is_track_output_enabled
    view.horizontalScrollBar().setValue(0)
    item.update_visibility(2000, 3000)
    assert not item.isVisible()
    view._update_clip_visibility()
    assert item.isVisible() and not item._is_track_output_enabled


def test_output_disabled_clip_uses_gray_body_and_retains_its_geometry(qapp: Any) -> None:
    item = ClipItem("paint", "v1", "Picture", 0, 200, 1.0, 0, track_height=64)
    bounds = item.boundingRect()

    def draw() -> QImage:
        image = QImage(210, 70, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        option = QStyleOptionGraphicsItem()
        option.exposedRect = bounds
        item.paint(painter, option)
        painter.end()
        return image

    normal = draw().pixelColor(50, 20)
    item.set_track_output_enabled(False)
    disabled = draw().pixelColor(50, 20)
    assert max(normal.red(), normal.green(), normal.blue()) - min(normal.red(), normal.green(), normal.blue()) > 30
    assert max(disabled.red(), disabled.green(), disabled.blue()) - min(disabled.red(), disabled.green(), disabled.blue()) < 12
    assert disabled.alpha() == 255
    assert item.boundingRect() == bounds and item.isVisible()


def test_cross_track_move_projects_destination_output_state_immediately(view: TimelineView) -> None:
    from src.features.edit.core.commands import MoveClipToExistingTrackCommand

    timeline = view.get_timeline().with_track_added(
        EditTrack("v2", "Disabled output", "video", 1, is_visible=False)
    )
    view.load_timeline(timeline)
    for source, target, enabled in (("v1", "v2", False), ("v2", "v1", True)):
        view._execute_command(MoveClipToExistingTrackCommand.create(
            source_track_id=source, target_track_id=target,
            clip_id="video", new_start=0, old_start=0,
        ))
        item = view._items_map["video"]
        assert item.track_id == target
        assert item._is_track_output_enabled is enabled
        assert item.isVisible()
