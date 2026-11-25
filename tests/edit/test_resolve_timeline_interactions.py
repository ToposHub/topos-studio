"""Resolve-style view gestures and presentation stay outside persistent models."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject, MediaAsset, probe_media_metadata
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineMarker, TimelineSequence, TransitionInstance,
)
from src.features.edit.ui.timeline_items import ClipItem, EditTool, MarkerItem
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


def test_active_trim_tool_is_inherited_after_timeline_reload(qtbot: Any) -> None:
    view = _view(qtbot)
    view.load_timeline(replace(_sequence(), timeline_id="another-timeline"))
    item = view._items_map["c1"]
    item._max_source_frames = 1000
    _gesture(view, QPointF(item._width / 2, 30))
    clip = view.get_timeline().get_clip_by_id("c1")
    assert clip.timeline_start == 100
    assert clip.source_start == 210


def test_normal_trim_clamps_to_adjacent_clip_in_preview_and_commit(qtbot: Any) -> None:
    view = _view(qtbot)
    original = _sequence()
    track = original.video_tracks[0]
    previous = replace(track.clips[0], timeline_duration=75, source_duration=75)
    updated = replace(track, clips=(previous, *track.clips[1:]))
    view.load_timeline(original.with_track_updated(updated))
    view.set_tool(EditTool.SELECT)
    view.set_snap_enabled(False)
    item = view._items_map["c1"]
    end = _gesture(view, QPointF(4, 30), delta=-35, release=False)
    assert item.x() == view._layout_engine.frame_to_x(75)
    assert item._duration_frames == 125
    assert view.get_timeline().get_clip_by_id("c1").timeline_start == 100
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    clip = view.get_timeline().get_clip_by_id("c1")
    assert (clip.timeline_start, clip.source_start, clip.timeline_duration) == (75, 175, 125)
    assert view._items_map["c1"].x() == view._layout_engine.frame_to_x(75)
    view.undo()
    assert view._items_map["c1"].x() == view._layout_engine.frame_to_x(100)


def test_ruler_snaps_seek_drag_and_release_without_deselecting(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_tool(EditTool.SELECT)
    view.set_snap_enabled(True)
    item = view._items_map["c1"]
    center = view.mapFromScene(item.mapToScene(QPointF(item._width / 2, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    selection = view._selection
    assert tuple(selection.selected_clip_ids) == ("c1",)
    start = QPoint(view.mapFromScene(QPointF(view._layout_engine.frame_to_x(99), 0)).x(), 10)
    end = QPoint(view.mapFromScene(QPointF(view._layout_engine.frame_to_x(199), 0)).x(), 10)
    started, moved, finished = [], [], []
    view.scrub_started.connect(started.append)
    view.scrub_moved.connect(moved.append)
    view.scrub_finished.connect(finished.append)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    assert started == [100]
    QTest.mouseMove(view.viewport(), end, 10)
    assert moved[-1] == 200
    QTest.keyClick(view, Qt.Key.Key_N)
    assert moved[-1] == 199
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert finished == [199]
    assert not view._snap_indicator.isVisible()
    assert view._selection == selection


@pytest.mark.parametrize("cancel", [True, False])
def test_held_clip_drag_autoscrolls_and_stops_after_release_or_escape(qtbot: Any, cancel: bool) -> None:
    view = _view(qtbot)
    view.set_tool(EditTool.SELECT)
    view.set_zoom(10.0)
    item = view._items_map["c0"]
    start = view.mapFromScene(item.mapToScene(QPointF(170, 30)))
    near_edge = QPoint(view.viewport().width() + (150 if not cancel else -4), start.y())
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), near_edge, 10)
    first_preview = item._start_frame
    qtbot.waitUntil(lambda: item._start_frame > first_preview, timeout=1000)
    assert view.horizontalScrollBar().value() > 0
    preview = item._start_frame
    if cancel:
        QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=near_edge)
    assert view.get_timeline().get_clip_by_id("c0").timeline_start == (0 if cancel else preview)
    assert not view._drag_scroll_timer.isActive()


def _linked_view(
    qtbot: Any, *, lock_video: bool = False, lock_audio: bool = False,
    project: EditProject | None = None,
) -> TimelineView:
    video = ClipInstance("video", "source", "v1", 100, 60, 50, 60, linked_clip_id="audio")
    audio = replace(video, clip_id="audio", track_id="a1", linked_clip_id="video")
    neighbor = ClipInstance("next", "source", "v1", 200, 60, 80, 60)
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    if project is not None:
        view.set_project(project)
    view.load_timeline(TimelineSequence(
        "drag", "Drag", video_tracks=(
            EditTrack("v1", "V1", "video", 0, clips=(video, neighbor)),
            EditTrack("v2", "V2", "video", 1, is_locked=lock_video),
        ),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,), is_locked=lock_audio),),
    ))
    view.set_zoom(3.0)
    view.show()
    qtbot.waitExposed(view)
    return view


def test_keyboard_track_nudge_moves_picture_keeps_audio_and_can_create_lane(qtbot: Any) -> None:
    view = _linked_view(qtbot)
    view.set_playhead(137)
    item = view._items_map["video"]
    spot = view.mapFromScene(item.mapToScene(QPointF(60, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=spot)
    assert set(view._selection.selected_clip_ids) == {"video", "audio"}
    QTest.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("video").track_id == "v2"
    assert timeline.get_clip_by_id("audio").track_id == "a1"
    assert view._playhead_frame == 137
    assert view._selection.primary_item.track_id == "v2"
    QTest.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    timeline = view.get_timeline()
    assert len(timeline.video_tracks) == 3
    assert timeline.get_clip_by_id("video").track_id == timeline.video_tracks[-1].track_id
    assert timeline.get_reciprocal_linked_clip("video").clip_id == "audio"
    assert (timeline.get_clip_by_id("video").timeline_start, timeline.get_clip_by_id("audio").timeline_start) == (100, 100)
    view.undo()
    assert len(view.get_timeline().video_tracks) == 2
    assert view.get_timeline().get_clip_by_id("video").track_id == "v2"


def test_keyboard_track_nudge_moves_independent_audio_down(qtbot: Any) -> None:
    view = _linked_view(qtbot)
    view.set_linked_selection_enabled(False)
    item = view._items_map["audio"]
    spot = view.mapFromScene(item.mapToScene(QPointF(60, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=spot)
    QTest.keyClick(view, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    timeline = view.get_timeline()
    assert len(timeline.audio_tracks) == 2
    assert timeline.get_clip_by_id("audio").track_id == timeline.audio_tracks[-1].track_id
    assert timeline.get_reciprocal_linked_clip("audio").track_id == "v1"


def test_track_move_with_scoped_marker_preserves_view_and_selection_on_history(qtbot: Any) -> None:
    view = _view(qtbot)
    clip = ClipInstance("shot", "source", "v1", 1000, 200, 100, 200)
    view.load_timeline(TimelineSequence(
        "far-shot", "Far shot",
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        markers=(TimelineMarker(marker_id="cue", frame=1040, clip_id="shot", track_id="v1"),),
    ))
    view.set_tool(EditTool.SELECT)
    view.set_zoom(10.0)
    QTest.qWait(1)
    bar = view.horizontalScrollBar()
    bar.setValue(9700)
    item = view._items_map["shot"]
    point = view.mapFromScene(item.mapToScene(QPointF(80, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.keyClick(view, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    QTest.qWait(1)
    moved = view.get_timeline().get_clip_by_id("shot")
    assert moved.track_id != "v1"
    assert view.get_timeline().markers[0].track_id == moved.track_id
    assert view._selection.primary_item.track_id == moved.track_id
    assert bar.value() == 9700

    view.undo()
    QTest.qWait(1)
    assert view._selection.primary_item.track_id == "v1"
    assert view._items_map["shot"]._is_selected
    assert bar.value() == 9700

    view.redo()
    assert view._selection.primary_item.track_id == moved.track_id
    assert view._items_map["shot"]._is_selected
    assert bar.value() == 9700


def test_linked_drag_snaps_tail_and_toggles_snapping_in_place(qtbot: Any) -> None:
    view = _linked_view(qtbot)
    before = view.get_timeline()
    video = view._items_map["video"]
    audio = view._items_map["audio"]
    start = view.mapFromScene(video.mapToScene(QPointF(80, 30)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), start + QPoint(6, 0), 10)
    assert (video._start_frame, audio._start_frame) == (102, 102)

    end = start + QPoint(38 * 3, 0)
    QTest.mouseMove(view.viewport(), end, 10)
    assert (video._start_frame, audio._start_frame) == (140, 140)
    QTest.keyClick(view, Qt.Key.Key_N)
    assert (video._start_frame, audio._start_frame) == (138, 138)
    QTest.keyClick(view, Qt.Key.Key_N)
    assert (video._start_frame, audio._start_frame) == (140, 140)
    assert view.get_timeline() is before

    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.get_timeline().get_clip_by_id("video").timeline_end == 200
    assert view.get_timeline().get_clip_by_id("audio").timeline_start == 140
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("video").timeline_start == 100
    view.redo()
    assert view.get_timeline().get_clip_by_id("video").timeline_start == 140


@pytest.mark.parametrize("locked_member", ["video_target", "audio_source"])
def test_blocked_cross_track_drag_restores_original_geometry(qtbot: Any, locked_member: str) -> None:
    view = _linked_view(
        qtbot, lock_video=locked_member == "video_target", lock_audio=locked_member == "audio_source",
    )
    view.set_snap_enabled(False)
    view._on_track_height_changed("v2", 146)
    before = view.get_timeline()
    video = view._items_map["video"]
    audio = view._items_map["audio"]
    geometry = [(item.pos(), item._track_height) for item in (video, audio)]
    start = view.mapFromScene(video.mapToScene(QPointF(80, 30)))
    y = view._layout_engine.track_top("v2") + 35
    end = view.mapFromScene(QPointF(video.x() + 80 + 30, y))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    assert video._track_height == 146
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.get_timeline() is before
    assert [(item.pos(), item._track_height) for item in (video, audio)] == geometry
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("linked_selection", [True, False])
def test_blade_splits_linked_selection_as_one_edit(qtbot: Any, linked_selection: bool) -> None:
    view = _linked_view(qtbot)
    view.set_linked_selection_enabled(linked_selection)
    video = view._items_map["video"]
    cut = view.mapFromScene(video.mapToScene(QPointF(90, 30)))
    QTest.keyClick(view, Qt.Key.Key_B)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=cut)
    sequence = view.get_timeline()
    assert sequence.get_clip_by_id("video").timeline_end == 130
    second_video = sequence.get_track_by_id("v1").get_clip_at(130)
    if linked_selection:
        assert sequence.get_reciprocal_linked_clip(second_video).timeline_start == 130
    else:
        assert sequence.get_clip_by_id("audio").timeline_end == 160
        assert second_video.linked_clip_id is None
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("video").timeline_end == 160
    assert view.get_timeline().get_clip_by_id("audio").timeline_end == 160


def _real_av_project() -> EditProject:
    sample = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
    if not sample.is_file():
        pytest.skip("2K H.264 fixture is unavailable")
    metadata = probe_media_metadata(str(sample), include_pts_index=False)
    asset = MediaAsset(
        asset_id="source", path=str(sample), display_name=sample.name,
        media_type="video", metadata=metadata,
    )
    assert metadata["audio_info"]["channels"] == 2
    return EditProject.create(name="Resolve timeline QA").with_media_asset_added(asset)


@pytest.mark.real_media
def test_linked_ripple_keeps_real_av_preview_and_result_in_sync(qtbot: Any) -> None:
    view = _linked_view(qtbot, project=_real_av_project())
    view.set_snap_enabled(False)
    view.set_tool(EditTool.TRIM)
    video, audio = view._items_map["video"], view._items_map["audio"]
    start = view.mapFromScene(video.mapToScene(QPointF(video._width - 4, 30)))
    end = start - QPoint(30, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    assert (video._duration_frames, audio._duration_frames) == (50, 50)
    assert view._items_map["next"]._start_frame == 190
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("video").timeline_duration == 50
    assert timeline.get_reciprocal_linked_clip("video").timeline_duration == 50
    assert timeline.get_clip_by_id("next").timeline_start == 190
    view.undo()
    assert view.get_timeline().get_clip_by_id("next").timeline_start == 200


@pytest.mark.real_media
def test_independent_cross_track_drag_keeps_link_on_real_media(qtbot: Any) -> None:
    view = _linked_view(qtbot, project=_real_av_project())
    view.set_snap_enabled(False)
    view.set_linked_selection_enabled(False)
    video = view._items_map["video"]
    start = view.mapFromScene(video.mapToScene(QPointF(80, 30)))
    y = view._layout_engine.track_top("v2") + 35
    end = view.mapFromScene(QPointF(video.x() + 80 + 30, y))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    sequence = view.get_timeline()
    moved = sequence.get_clip_by_id("video")
    assert (moved.track_id, moved.timeline_start) == ("v2", 110)
    partner = sequence.get_reciprocal_linked_clip(moved)
    assert (partner.track_id, partner.timeline_start) == ("a1", 100)


@pytest.mark.parametrize("lanes", [1, 2])
def test_multi_selection_moves_every_linked_pair_by_the_same_track_offset(qtbot: Any, lanes: int) -> None:
    view = _linked_view(qtbot)
    before = view.get_timeline()
    video2 = ClipInstance("video2", "source", "v2", 250, 40, 30, 40, linked_clip_id="audio2")
    audio2 = replace(video2, clip_id="audio2", track_id="a2", linked_clip_id="video2")
    audio_tracks = (
        before.audio_tracks[0], EditTrack("a2", "A2", "audio", 1, clips=(audio2,)),
    )
    if lanes == 1:
        audio_tracks += (EditTrack("a3", "A3", "audio", 2),)
    before = replace(
        before,
        video_tracks=(
            before.video_tracks[0], replace(before.video_tracks[1], clips=(video2,)),
            EditTrack("v3", "V3", "video", 2),
        ),
        audio_tracks=audio_tracks,
    )
    view.load_timeline(before)
    view.set_snap_enabled(False)
    primary = view._items_map["video"]
    secondary = view._items_map["video2"]
    start = view.mapFromScene(primary.mapToScene(QPointF(80, 30)))
    second = view.mapFromScene(secondary.mapToScene(QPointF(75, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=second,
    )
    target_track = f"v{1 + lanes}"
    end = view.mapFromScene(QPointF(primary.x() + 95, view._layout_engine.track_top(target_track) + 45))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    session = view._current_drag_session
    if lanes == 1:
        assert session.current_track_ids == {
            "video": "v2", "audio": "a2", "video2": "v3", "audio2": "a3",
        }
    else:
        assert session.current_track_ids["audio"] != session.current_track_ids["audio2"]
        assert view._items_map["audio"].y() < view._items_map["audio2"].y()
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    after = view.get_timeline()
    assert after.get_clip_by_id("video").track_id == target_track
    assert [clip.clip_id for clip in after.video_tracks[lanes + 1].clips] == ["video2"]
    assert [clip.clip_id for clip in after.audio_tracks[lanes].clips] == ["audio"]
    assert [clip.clip_id for clip in after.audio_tracks[lanes + 1].clips] == ["audio2"]
    assert after.get_reciprocal_linked_clip("video2").clip_id == "audio2"
    assert len(view._command_stack._undo_stack) == 1


def test_selected_pairs_share_one_new_video_and_audio_lane(qtbot: Any) -> None:
    view = _linked_view(qtbot)
    before = view.get_timeline()
    v1, a1 = before.video_tracks[0], before.audio_tracks[0]
    video2 = ClipInstance("video2", "source", "v1", 250, 40, 30, 40, linked_clip_id="audio2")
    audio2 = replace(video2, clip_id="audio2", track_id="a1", linked_clip_id="video2")
    fade = TransitionInstance(
        transition_id="fade", track_id="v1", right_clip_id="video2",
        center_frame=250, duration=12, alignment="start_on_cut",
    )
    view.load_timeline(replace(
        before, video_tracks=(replace(v1, clips=(*v1.clips[:-1], video2), transitions=(fade,)),),
        audio_tracks=(replace(a1, clips=(*a1.clips, audio2)),),
        markers=(TimelineMarker("moved_marker", 110, clip_id="video", track_id="v1"),),
    ))
    primary = view._items_map["video"]
    secondary = view._items_map["video2"]
    start = view.mapFromScene(primary.mapToScene(QPointF(80, 30)))
    second = view.mapFromScene(secondary.mapToScene(QPointF(75, 30)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=second,
    )
    end = view.mapFromScene(QPointF(primary.x() + 80, primary.y() - 10))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    assert view._items_map["video"].y() == view._items_map["video2"].y()
    assert view._items_map["audio"].y() == view._items_map["audio2"].y()
    marker = view._marker_items["moved_marker"]
    assert marker._target_y == primary.y() + MarkerItem.CLIP_MARKER_INSET_Y
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    after = view.get_timeline()
    assert len(after.video_tracks) == len(after.audio_tracks) == 2
    assert [clip.clip_id for clip in after.video_tracks[1].clips] == ["video", "video2"]
    assert [clip.clip_id for clip in after.audio_tracks[1].clips] == ["audio", "audio2"]
    assert after.video_tracks[1].transitions[0].track_id == after.video_tracks[1].track_id
    assert after.video_tracks[1].transitions[0].center_frame == 250
    assert after.markers[0].track_id == after.video_tracks[1].track_id
    assert after.get_clip_by_id("video2").timeline_start == 250
    assert after.get_reciprocal_linked_clip("video2").clip_id == "audio2"
    view.undo()
    assert len(view.get_timeline().video_tracks) == len(view.get_timeline().audio_tracks) == 1
    view.redo()
    assert view.get_timeline().video_tracks[1].track_id == after.video_tracks[1].track_id


def test_clip_scoped_marker_moves_without_snapping_to_its_old_frame(qtbot: Any) -> None:
    view = _linked_view(qtbot)
    before = view.get_timeline()
    view.load_timeline(replace(before, markers=(
        TimelineMarker("owned", 159, clip_id="video", track_id="v1"),
    )))
    video = view._items_map["video"]
    start = view.mapFromScene(video.mapToScene(QPointF(80, 30)))
    end = start + QPoint(6, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    assert video._start_frame == 102
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.get_timeline().markers[0].frame == 161
