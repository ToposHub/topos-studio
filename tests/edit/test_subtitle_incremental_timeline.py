"""SUB-P1-02 contract: subtitle incremental timeline diff & update.

Phase 0.1 bridged subtitle edits to a full reload. Phase 1.1 introduces a real
incremental diff so a single cue edit updates only that SubtitleClipItem.

These tests verify:
* ``_clip_change_detector`` populates the new subtitle fields and does NOT set
  ``full_reload_needed`` for ordinary cue edits.
* Structural subtitle-track changes (count/order) still force a full reload.
* ``update_timeline_incremental`` creates / updates / removes only the affected
  SubtitleClipItem without rebuilding unrelated video items.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    AddSubtitleClipCommand,
    CopySubtitleClipCommand,
    DeleteSubtitleClipCommand,
    MoveSubtitleClipCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_items import ClipItem
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _video_clip(clip_id: str = "v-clip", track_id: str = "v1") -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="asset-v", track_id=track_id,
        timeline_start=0, timeline_duration=120,
        source_start=0, source_duration=120,
    )


def _sub(clip_id: str, track_id: str = "s1", start: int = 0,
         duration: int = 72, text: str = "x") -> SubtitleClip:
    return SubtitleClip(
        clip_id=clip_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration, text=text,
    )


def _seq(subs=(), with_video: bool = True) -> TimelineSequence:
    video_tracks = ()
    if with_video:
        video_tracks = (
            EditTrack(
                track_id="v1", name="V1", track_type="video", index=0,
                clips=(_video_clip(),),
            ),
        )
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=tuple(subs),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        video_tracks=video_tracks,
        subtitle_tracks=(sub_track,),
    )


# ── Diff detection ─────────────────────────────────────────────────────────


def test_text_edit_does_not_force_full_reload(qapp) -> None:
    view = TimelineView()
    seq = _seq([_sub("sub-1", text="before")])
    view.load_timeline(seq)

    new_seq = UpdateSubtitleClipCommand.create(
        clip_id="sub-1", updates={"text": "after"}
    ).apply(seq)

    changes = view._clip_change_detector(seq, new_seq)
    assert changes.full_reload_needed is False
    assert len(changes.modified_subtitle_clips) == 1
    assert changes.modified_subtitle_clips[0].clip_id == "sub-1"
    assert changes.added_subtitle_clips == ()
    assert changes.removed_subtitle_clip_ids == ()


def test_add_cue_detected_as_added(qapp) -> None:
    view = TimelineView()
    seq = _seq([_sub("sub-1", start=0, duration=72)])
    view.load_timeline(seq)

    new_seq = AddSubtitleClipCommand.create(
        track_id="s1", timeline_start=200
    ).apply(seq)
    # AddSubtitleClipCommand.apply returns (timeline, updated_cmd)
    if isinstance(new_seq, tuple):
        new_seq = new_seq[0]

    changes = view._clip_change_detector(seq, new_seq)
    assert changes.full_reload_needed is False
    assert len(changes.added_subtitle_clips) == 1
    assert changes.removed_subtitle_clip_ids == ()


def test_delete_cue_detected_as_removed(qapp) -> None:
    view = TimelineView()
    seq = _seq([_sub("sub-1"), _sub("sub-2", start=72)])
    view.load_timeline(seq)

    new_seq = DeleteSubtitleClipCommand.create(clip_id="sub-2").apply(seq)

    changes = view._clip_change_detector(seq, new_seq)
    assert changes.full_reload_needed is False
    assert changes.removed_subtitle_clip_ids == ("sub-2",)
    assert changes.modified_subtitle_clips == ()


def test_subtitle_track_count_change_forces_full_reload(qapp) -> None:
    view = TimelineView()
    one_track = _seq([_sub("sub-1")])
    view.load_timeline(one_track)

    two_tracks = TimelineSequence(
        timeline_id=one_track.timeline_id, name=one_track.name,
        width=one_track.width, height=one_track.height, fps=one_track.fps,
        video_tracks=one_track.video_tracks,
        subtitle_tracks=(
            one_track.subtitle_tracks[0],
            EditTrack(
                track_id="s2", name="S2", track_type="subtitle", index=1,
                subtitle_clips=(),
            ),
        ),
    )

    changes = view._clip_change_detector(one_track, two_tracks)
    assert changes.full_reload_needed is True


# ── Incremental application ────────────────────────────────────────────────


def test_text_edit_updates_only_the_affected_subtitle_item(qapp) -> None:
    """A single cue edit must not recreate unrelated video clips."""
    view = TimelineView()
    seq = _seq([_sub("sub-1", text="before")])
    view.load_timeline(seq)

    # Snapshot identity of the video ClipItem; it must survive a subtitle edit.
    video_item_before = view._items_map.get("v-clip")
    assert isinstance(video_item_before, ClipItem)

    view.execute_command(UpdateSubtitleClipCommand.create(
        clip_id="sub-1", updates={"text": "after"}
    ))

    # The same video ClipItem object is retained (no rebuild).
    assert view._items_map.get("v-clip") is video_item_before
    # The subtitle item reflects the new text.
    sub_item = view._items_map.get("sub-1")
    assert isinstance(sub_item, SubtitleClipItem)
    assert sub_item._text == "after"


def test_add_cue_creates_subtitle_item_without_rebuilding_video(qapp) -> None:
    view = TimelineView()
    seq = _seq([_sub("sub-1", start=0, duration=72)])
    view.load_timeline(seq)
    video_item_before = view._items_map.get("v-clip")

    cmd = AddSubtitleClipCommand.create(track_id="s1", timeline_start=200)
    view.execute_command(cmd)

    assert view._items_map.get("v-clip") is video_item_before
    new_id = cmd._created_clip_id
    assert isinstance(view._items_map.get(new_id), SubtitleClipItem)


def test_delete_cue_removes_subtitle_item_without_rebuilding_video(qapp) -> None:
    view = TimelineView()
    seq = _seq([_sub("sub-1"), _sub("sub-2", start=72)])
    view.load_timeline(seq)
    video_item_before = view._items_map.get("v-clip")

    view.execute_command(DeleteSubtitleClipCommand.create(clip_id="sub-2"))

    assert view._items_map.get("v-clip") is video_item_before
    assert "sub-2" not in view._items_map
    assert isinstance(view._items_map.get("sub-1"), SubtitleClipItem)


def test_cross_track_move_rebinds_subtitle_item_to_destination_lane(qapp) -> None:
    """A move is rendered as remove/add, so the item gets its new track id."""
    sequence = _seq([_sub("sub-1", start=0, duration=20)])
    destination = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
    )
    sequence = TimelineSequence(
        timeline_id=sequence.timeline_id, name=sequence.name,
        width=sequence.width, height=sequence.height, fps=sequence.fps,
        video_tracks=sequence.video_tracks,
        subtitle_tracks=(sequence.subtitle_tracks[0], destination),
    )
    view = TimelineView()
    view.load_timeline(sequence)

    view.execute_command(MoveSubtitleClipCommand.create("sub-1", "s2", 96))

    item = view._items_map.get("sub-1")
    assert isinstance(item, SubtitleClipItem)
    assert item.track_id == "s2"
    assert item.start_frame == 96
    assert item.y() == view._layout_engine.track_top("s2")

    destination_center = view._layout_engine.track_rect("s2").center().y()
    assert view._subtitle_target_track_at_y(destination_center) == "s2"
    video_center = view._layout_engine.track_rect("v1").center().y()
    assert view._subtitle_target_track_at_y(video_center) is None


def test_copy_subtitle_clip_creates_a_second_item_without_removing_source(qapp) -> None:
    sequence = _seq([_sub("sub-1", start=0, duration=20)])
    view = TimelineView()
    view.load_timeline(sequence)
    command = CopySubtitleClipCommand.create("sub-1", "s1", 96)

    view.execute_command(command)

    assert isinstance(view._items_map.get("sub-1"), SubtitleClipItem)
    assert command._copied_clip_id is not None
    assert isinstance(view._items_map.get(command._copied_clip_id), SubtitleClipItem)


def test_same_track_alt_drag_preview_keeps_source_in_collision_resolution(qapp) -> None:
    """Alt-copy shows a ghost after the source instead of moving the source."""
    sequence = _seq([_sub("sub-1", start=0, duration=20)])
    view = TimelineView()
    view.load_timeline(sequence)
    item = view._items_map["sub-1"]
    assert isinstance(item, SubtitleClipItem)
    item._drag_copy_requested = True
    lane_y = view._layout_engine.track_rect("s1").center().y()

    view._on_subtitle_clip_drag_preview("sub-1", "s1", 5, lane_y)

    assert item.start_frame == 0
    preview = view._subtitle_copy_drag_preview_item
    assert isinstance(preview, SubtitleClipItem)
    assert preview.start_frame == 20
    assert preview.pos().y() == view._layout_engine.track_top("s1")


def test_alt_copy_drag_preview_tracks_destination_lane_and_cleans_up(qapp) -> None:
    """The visible ghost follows a cross-track Alt drag until release."""
    sequence = _seq([_sub("sub-1", start=0, duration=20)])
    destination = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
    )
    sequence = TimelineSequence(
        timeline_id=sequence.timeline_id, name=sequence.name,
        width=sequence.width, height=sequence.height, fps=sequence.fps,
        video_tracks=sequence.video_tracks,
        subtitle_tracks=(sequence.subtitle_tracks[0], destination),
    )
    view = TimelineView()
    view.load_timeline(sequence)
    item = view._items_map["sub-1"]
    assert isinstance(item, SubtitleClipItem)
    item._drag_copy_requested = True
    destination_y = view._layout_engine.track_rect("s2").center().y()

    view._on_subtitle_clip_drag_preview("sub-1", "s1", 96, destination_y)

    preview = view._subtitle_copy_drag_preview_item
    assert isinstance(preview, SubtitleClipItem)
    assert item.start_frame == 0
    assert preview.start_frame == 96
    assert preview.pos().y() == view._layout_engine.track_top("s2")

    view._on_subtitle_clip_drag_finished("sub-1", "s1", 0, 96, destination_y, True)
    assert view._subtitle_copy_drag_preview_item is None


def test_subtitle_drag_commit_is_queued_until_mouse_release_returns(qapp) -> None:
    """A cross-track commit must not delete the active graphics event sender."""
    sequence = _seq([_sub("sub-1", start=0, duration=20)])
    destination = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
    )
    sequence = TimelineSequence(
        timeline_id=sequence.timeline_id, name=sequence.name,
        width=sequence.width, height=sequence.height, fps=sequence.fps,
        video_tracks=sequence.video_tracks,
        subtitle_tracks=(sequence.subtitle_tracks[0], destination),
    )
    view = TimelineView()
    view.load_timeline(sequence)
    emitted: list[tuple] = []
    view.subtitle_clip_drag_finished.connect(
        lambda *payload: emitted.append(payload)
    )
    item = view._items_map["sub-1"]
    assert isinstance(item, SubtitleClipItem)
    item._start_frame = 96  # mirrors the completed visual drag preview
    destination_y = view._layout_engine.track_rect("s2").center().y()

    view._on_subtitle_clip_drag_finished(
        "sub-1", "s1", 0, 96, destination_y, False,
    )

    assert emitted == []
    qapp.processEvents()
    assert emitted == [("sub-1", "s1", 0, 96, "s2", False)]
