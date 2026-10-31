"""SUB-P1-08 contract: subtitle drag/trim snapping.

The bug: subtitle drag/trim rounded pixels to frames and never used the
SnapManager, so aligning a cue to a video cut, marker, playhead or another
cue edge was guesswork.

Phase 1.4 makes subtitle cue edges first-class snap candidates and routes
subtitle drag/trim through the existing snap path (with Shift/Ctrl bypass and
visible indicator feedback).
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_ops import (
    SnapManager,
    SnapPointType,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineMarker,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _seq_with_video_and_subtitle() -> TimelineSequence:
    video_track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(
            ClipInstance(
                clip_id="v-clip", asset_id="asset-v", track_id="v1",
                timeline_start=0, timeline_duration=100,
                source_start=0, source_duration=100,
            ),
        ),
    )
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1", track_id="s1",
                timeline_start=50, timeline_duration=72, text="cue",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        video_tracks=(video_track,),
        subtitle_tracks=(sub_track,),
    )


def _seq_with_subtitle_only() -> TimelineSequence:
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1", track_id="s1",
                timeline_start=50, timeline_duration=72, text="cue",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(sub_track,),
    )


def test_subtitle_edges_are_snap_candidates() -> None:
    """update_from_timeline must add CLIP_START/END for subtitle cues."""
    seq = _seq_with_subtitle_only()
    mgr = SnapManager()
    mgr.update_from_timeline(seq, playhead=0)

    frames = set(mgr.get_all_frames())
    # The subtitle cue occupies [50, 122).
    assert 50 in frames
    assert 122 in frames


def test_subtitle_drag_snaps_to_nearby_clip_edge(qapp) -> None:
    """A subtitle dragged near a video cut snaps to it (view-side)."""
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_video_and_subtitle()
    view = TimelineView()
    view.load_timeline(seq)
    view._snap_enabled = True
    view._snap_manager.update_from_timeline(seq, playhead=0)

    # video cut at frame 100 (v-clip end). Drag subtitle start to 98 → snap to 100.
    snapped, did_snap = view._snap_frame(98, tuple(), show_indicator=False)
    assert did_snap is True
    assert snapped == 100


def test_subtitle_snap_excludes_dragged_clip_itself(qapp) -> None:
    """A cue must not snap to its own current edges."""
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_subtitle_only()
    view = TimelineView()
    view.load_timeline(seq)
    view._snap_enabled = True
    # Exclude the dragged cue so its own edges don't count.
    view._snap_manager.update_from_timeline(seq, playhead=0, exclude_clip_ids={"sub-1"})

    frames = view._snap_manager.get_all_frames()
    # 50 and 122 (the cue edges) are gone; only sequence start (0) remains.
    assert 50 not in frames
    assert 122 not in frames


def test_actual_subtitle_drag_handler_excludes_its_own_edges(qapp) -> None:
    """The TimelineView handler must pass the dragged cue ID to snapping."""
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_subtitle_only()
    view = TimelineView()
    view.load_timeline(seq)
    view._snap_enabled = True
    view._snap_manager.update_from_timeline(seq, playhead=0)

    # Frame 49 is within the snap threshold of this cue's own start at 50.
    # It must remain 49; otherwise the cue sticks to its original position.
    view._on_subtitle_clip_moved("sub-1", "s1", 49)

    item = view._items_map["sub-1"]
    assert item._start_frame == 49


def test_snap_bypassed_when_disabled(qapp) -> None:
    """When snapping is disabled, _snap_frame returns the raw frame."""
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_video_and_subtitle()
    view = TimelineView()
    view.load_timeline(seq)
    view._snap_enabled = False

    snapped, did_snap = view._snap_frame(98, tuple(), show_indicator=False)
    assert did_snap is False
    assert snapped == 98


def test_subtitle_edge_and_marker_coexist_at_same_frame() -> None:
    """A subtitle edge and a marker at the same frame coexist (no dedup loss)."""
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1", track_id="s1",
                timeline_start=50, timeline_duration=72, text="cue",
            ),
        ),
    )
    seq = TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(sub_track,),
        markers=(
            TimelineMarker(marker_id="m1", frame=50, name="M", color="#ff0000"),
        ),
    )
    mgr = SnapManager()
    mgr.update_from_timeline(seq, playhead=0)

    pts_at_50 = mgr.get_points_at_frame(50)
    types_at_50 = {p.type for p in pts_at_50}
    # Both the cue start and the marker live at frame 50.
    assert SnapPointType.CLIP_START in types_at_50
    assert SnapPointType.MARKER in types_at_50


# ── Keyboard nudge (SUB-P1-08) ─────────────────────────────────────────────


def test_nudge_moves_selected_subtitle_by_one_frame(qapp) -> None:
    """Alt+Arrow nudge moves a selected subtitle cue by ±1 frame, one undo each."""
    from PyQt6.QtCore import QEvent, Qt as QtConst
    from PyQt6.QtGui import QKeyEvent
    from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_subtitle_only()
    view = TimelineView()
    view.load_timeline(seq)

    # Select the subtitle cue.
    item = view._items_map.get("sub-1")
    assert isinstance(item, SubtitleClipItem)
    item.setSelected(True)

    start_before = view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start

    # Nudge directly via the method (Alt+Right path).
    assert view._nudge_selected_subtitle_clips(1) is True
    assert view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start == start_before + 1

    assert view._nudge_selected_subtitle_clips(-1) is True
    assert view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start == start_before

    # Each nudge is its own undo entry.
    assert view.can_undo() is True


def test_nudge_returns_false_when_no_subtitle_selected(qapp) -> None:
    """Without a selected subtitle, nudge falls through to playhead movement."""
    from src.features.edit.ui.timeline_view import TimelineView

    seq = _seq_with_subtitle_only()
    view = TimelineView()
    view.load_timeline(seq)
    # Nothing selected.
    assert view._nudge_selected_subtitle_clips(1) is False


def test_nudge_clamps_to_zero(qapp) -> None:
    """A cue at frame 0 nudged left stays at 0 (no negative start)."""
    from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
    from src.features.edit.ui.timeline_view import TimelineView

    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1", track_id="s1",
                timeline_start=0, timeline_duration=72, text="cue",
            ),
        ),
    )
    seq = TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(sub_track,),
    )
    view = TimelineView()
    view.load_timeline(seq)
    view._items_map.get("sub-1").setSelected(True)

    assert view._nudge_selected_subtitle_clips(-1) is False  # clamped, no move
    assert view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start == 0


def test_multi_nudge_is_atomic_and_one_undo(qapp) -> None:
    """Adjacent selected cues move together without transient overlap."""
    from src.features.edit.ui.timeline_view import TimelineView

    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="a", track_id="s1", timeline_start=10,
                timeline_duration=10, text="a",
            ),
            SubtitleClip(
                clip_id="b", track_id="s1", timeline_start=20,
                timeline_duration=10, text="b",
            ),
        ),
    )
    sequence = TimelineSequence(
        timeline_id="seq", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )
    view = TimelineView()
    view.load_timeline(sequence)
    view._items_map["a"].setSelected(True)
    view._items_map["b"].setSelected(True)

    assert view._nudge_selected_subtitle_clips(1) is True
    assert view._timeline.get_subtitle_clip_by_id("a").timeline_start == 11
    assert view._timeline.get_subtitle_clip_by_id("b").timeline_start == 21

    view.undo()
    assert view._timeline.get_subtitle_clip_by_id("a").timeline_start == 10
    assert view._timeline.get_subtitle_clip_by_id("b").timeline_start == 20


def test_multi_nudge_collision_is_rejected_without_partial_commit(qapp) -> None:
    """An unselected neighbour blocks the whole selection, not one cue."""
    from src.features.edit.ui.timeline_view import TimelineView

    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="a", track_id="s1", timeline_start=0,
                timeline_duration=10, text="a",
            ),
            SubtitleClip(
                clip_id="b", track_id="s1", timeline_start=10,
                timeline_duration=10, text="b",
            ),
            SubtitleClip(
                clip_id="blocker", track_id="s1", timeline_start=20,
                timeline_duration=10, text="blocker",
            ),
        ),
    )
    sequence = TimelineSequence(
        timeline_id="seq", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )
    view = TimelineView()
    view.load_timeline(sequence)
    view._items_map["a"].setSelected(True)
    view._items_map["b"].setSelected(True)

    assert view._nudge_selected_subtitle_clips(1) is False
    assert view._timeline.get_subtitle_clip_by_id("a").timeline_start == 0
    assert view._timeline.get_subtitle_clip_by_id("b").timeline_start == 10
    assert view.can_undo() is False
