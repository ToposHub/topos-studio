"""Resolve-style view gestures and presentation stay outside persistent models."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import ClipItem, EditTool
from src.features.edit.ui.timeline_view import TimelineView


def _sequence() -> TimelineSequence:
    video = EditTrack(
        track_id="v1", name="Interview — Main Camera", track_type="video", index=0,
        clips=tuple(ClipInstance(
            clip_id=f"c{i}", asset_id="source", track_id="v1",
            timeline_start=i * 100, timeline_duration=100,
            source_start=100 + i * 100, source_duration=100,
        ) for i in range(3)),
    )
    return TimelineSequence(
        timeline_id="resolve-ui", name="Review", video_tracks=(video, EditTrack(
            track_id="v2", name="B Roll", track_type="video", index=1,
        )), audio_tracks=(EditTrack(
            track_id="a1", name="Dialogue", track_type="audio", index=0,
        ),),
    )


def _view(qtbot: Any) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    view.show()
    view.load_timeline(_sequence())
    view.set_zoom(3.0)
    view.set_snap_enabled(False)
    view.set_tool(EditTool.TRIM)
    for item in view._items_map.values():
        if isinstance(item, ClipItem):
            item._max_source_frames = 1000
    QTest.qWait(5)
    return view


def _gesture(view: TimelineView, local: QPointF, delta: int = 10, release: bool = True) -> QPoint:
    item = view._items_map["c1"]
    start = view.mapFromScene(item.mapToScene(local))
    end = start + QPoint(delta * 3, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    if release:
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    return end


@pytest.mark.parametrize("zone", ["picture", "label", "edge", "seam"])
def test_trim_mode_routes_real_gestures_and_undo(qtbot: Any, zone: str) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    item = view._items_map["c1"]
    point = {
        "picture": QPointF(150, 30),
        "label": QPointF(150, item._track_height - 8),
        "edge": QPointF(item._width - 5, 30),
        "seam": QPointF(item._width, 30),
    }[zone]
    _gesture(view, point)
    timeline = view.get_timeline()
    clip = timeline.get_clip_by_id("c1")
    if zone == "picture":
        assert clip.source_start == 210
        assert clip.timeline_start == 100 and clip.timeline_duration == 100
    elif zone == "label":
        assert clip.timeline_start == 110 and clip.timeline_duration == 100
        assert timeline.get_clip_by_id("c0").timeline_duration == 110
        assert timeline.get_clip_by_id("c2").timeline_start == 210
    elif zone == "edge":
        assert clip.timeline_duration == 110
        assert timeline.get_clip_by_id("c2").timeline_start == 210
    else:
        assert clip.timeline_duration == 110
        assert timeline.get_clip_by_id("c2").timeline_start == 210
        assert timeline.get_clip_by_id("c2").timeline_duration == 90
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().to_dict() == replace(original, revision=view.get_timeline().revision).to_dict()


@pytest.mark.parametrize("zone", ["picture", "label", "edge", "seam"])
def test_escape_cancels_smart_trim_without_late_commit(qtbot: Any, zone: str) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    item = view._items_map["c1"]
    point = {
        "picture": QPointF(150, 30), "label": QPointF(150, item._track_height - 8),
        "edge": QPointF(item._width - 5, 30), "seam": QPointF(item._width, 30),
    }[zone]
    end = _gesture(view, point, release=False)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.get_timeline().to_dict() == original.to_dict()
    assert not view._command_stack._undo_stack
    assert view._interaction.is_idle


def test_source_patch_badges_are_independent_from_target_and_model(qtbot: Any) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    requests: list[tuple[str, str, bool]] = []
    view.source_patch_changed.connect(lambda *args: requests.append(args))
    header = view._track_headers["v2"]
    point = view.mapFromScene(header.mapToScene(header._get_source_patch_rect().center()))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view.source_patch_track_id("video") == "v2"
    assert header._source_patch_enabled
    assert not view._track_headers["v1"]._source_patch_enabled
    assert requests == [("video", "v2", True)]
    assert view.get_target_track_id() != "v2"
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view.source_patch_track_id("video") is None


def test_display_preferences_are_per_view_and_survive_reload(qtbot: Any) -> None:
    first, second = _view(qtbot), _view(qtbot)
    original = first.get_timeline()
    first.set_thumbnail_display_mode("none")
    first.set_waveforms_visible(False)
    first.load_timeline(original)
    assert first.thumbnail_display_mode() == "none"
    assert not first.waveforms_visible()
    assert first._items_map["c1"]._thumbnail_display_mode == "none"
    assert not first._items_map["c1"]._waveforms_visible
    assert second.thumbnail_display_mode() == "auto"
    assert second.waveforms_visible()
    assert not first._command_stack._undo_stack


def test_position_lock_blocks_move_and_slide_but_allows_slip(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_position_lock_enabled(True)
    original = view.get_timeline()
    item = view._items_map["c1"]
    _gesture(view, QPointF(150, item._track_height - 8))
    assert view.get_timeline() is original
    view.set_tool(EditTool.SELECT)
    _gesture(view, QPointF(150, 30))
    assert view.get_timeline() is original
    view.set_tool(EditTool.TRIM)
    _gesture(view, QPointF(150, 30))
    assert view.get_timeline().get_clip_by_id("c1").source_start == 210


def test_group_height_slider_uses_shared_layout_without_commands(qtbot: Any) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    view.set_track_height_for_type("video", 114)
    for track_id in ("v1", "v2"):
        assert view._layout_engine.track_height(track_id) == 114
        assert view._track_headers[track_id].track_height == 114
    assert view._items_map["c1"]._track_height == 114
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack


def test_deleting_patch_destination_refreshes_remaining_badges(qtbot: Any) -> None:
    from src.features.edit.core.commands import DeleteTrackCommand
    view = _view(qtbot)
    view._on_source_patch_requested("v2", True)
    view._execute_command(DeleteTrackCommand.create("v2"))
    assert view.source_patch_track_id("video") == "v1"
    assert view._track_headers["v1"]._source_patch_enabled


@pytest.mark.parametrize("height", [40, 42])
def test_invisible_audio_slider_cannot_capture_name_clicks(qapp: Any, height: int) -> None:
    from src.features.edit.ui.timeline_items import TrackHeaderItem
    header = TrackHeaderItem("a1", "Dialogue", 0, "audio", track_height=height)
    assert header._get_button_at_pos(header._get_volume_slider_rect().center()) != "volume"


def test_selection_tool_rolls_center_seam_without_rippling(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_tool(EditTool.SELECT)
    item = view._items_map["c1"]
    _gesture(view, QPointF(item._width, 30))
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("c1").timeline_duration == 110
    assert timeline.get_clip_by_id("c2").timeline_duration == 90
    assert timeline.duration_frames == 300
    assert len(view._command_stack._undo_stack) == 1
