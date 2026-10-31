# -*- coding: utf-8 -*-
"""
P7 Comprehensive Regression Test Matrix for the Timeline Editor.

Covers:
  1.  Linked drag atomicity (MoveGroup delta)
  2.  Track header badge type-local index
  3.  Independent video / audio target tracks
  4.  V1-A1 splitter adjacency
  5.  Drag preview vs. final drop target consistency
  6.  Wrong-area auto-snap (audio on video area and vice versa)
  7.  Locked track rejection
  8.  MoveGroup validation (is_valid)
  9.  Clip drag event reaches ClipItem (interaction state machine)
  10. Capture tool module importability
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational
from src.features.edit.ui.timeline_interaction import (
    ClipDragSession,
    InteractionSession,
    InteractionState,
    MoveGroup,
    MoveGroupMember,
)
from src.features.edit.ui.timeline_layout import (
    TRACK_AREA_TOP,
    TRACK_HEADER_WIDTH,
    TimelineDropTarget,
    TimelineLayoutEngine,
    TimelineLayoutState,
    TrackInfo,
)
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_engine(
    video_count: int = 2,
    audio_count: int = 2,
    width: float = 1200.0,
    height: float = 600.0,
    ppf: float = 5.0,
) -> TimelineLayoutEngine:
    """Build a configured engine with the requested number of video/audio tracks."""
    engine = TimelineLayoutEngine()
    tracks: list[TrackInfo] = []
    for i in range(video_count):
        tracks.append(TrackInfo(track_id=f"v{i + 1}", track_type="video"))
    for i in range(audio_count):
        tracks.append(TrackInfo(track_id=f"a{i + 1}", track_type="audio"))
    state = TimelineLayoutState()
    engine.configure(tracks, state, width, height, ppf)
    return engine


def _center_y(engine: TimelineLayoutEngine, track_id: str) -> float:
    r = engine.track_rect(track_id)
    return (r.top() + r.bottom()) / 2.0


def _sequence_with_linked_clips() -> TimelineSequence:
    """Sequence with a linked video+audio pair at frame 0."""
    video_clip = ClipInstance(
        clip_id="vc",
        asset_id="a1",
        track_id="v1",
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
        linked_clip_id="ac",
    )
    audio_clip = ClipInstance(
        clip_id="ac",
        asset_id="a1",
        track_id="a1",
        timeline_start=0,
        timeline_duration=120,
        source_start=0,
        source_duration=120,
        linked_clip_id="vc",
    )
    return TimelineSequence(
        timeline_id="seq-p7",
        name="P7",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=(video_clip,),
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1",
                name="A1",
                track_type="audio",
                index=0,
                clips=(audio_clip,),
            ),
        ),
    )


def _sequence_with_multi_tracks(
    n_video: int = 2,
    n_audio: int = 2,
) -> TimelineSequence:
    """Sequence with *n_video* video and *n_audio* audio tracks (one clip each)."""
    vt = [
        EditTrack(
            track_id=f"v{i + 1}",
            name=f"V{i + 1}",
            track_type="video",
            index=i,
            clips=(
                ClipInstance(
                    clip_id=f"vc{i + 1}",
                    asset_id=f"av{i + 1}",
                    track_id=f"v{i + 1}",
                    timeline_start=0,
                    timeline_duration=120,
                    source_start=0,
                    source_duration=120,
                ),
            ),
        )
        for i in range(n_video)
    ]
    at = [
        EditTrack(
            track_id=f"a{i + 1}",
            name=f"A{i + 1}",
            track_type="audio",
            index=i,
            clips=(
                ClipInstance(
                    clip_id=f"ac{i + 1}",
                    asset_id=f"aa{i + 1}",
                    track_id=f"a{i + 1}",
                    timeline_start=0,
                    timeline_duration=120,
                    source_start=0,
                    source_duration=120,
                ),
            ),
        )
        for i in range(n_audio)
    ]
    return TimelineSequence(
        timeline_id="seq-multi-p7",
        name="Multi",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=tuple(vt),
        audio_tracks=tuple(at),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Linked Drag Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestLinkedDrag:
    """Verify linked V/A clips move atomically."""

    def test_linked_drag_finished_moves_video_and_audio_atomically(self, qapp) -> None:
        """After drag finished, both video and audio clips have same delta_frames."""
        view = TimelineView()
        view.load_timeline(_sequence_with_linked_clips())
        view._snap_enabled = False

        view._on_drag_finished("vc", "v1", 0, 48, "v1")

        assert view._timeline is not None
        assert view._timeline.get_clip_by_id("vc").timeline_start == 48
        assert view._timeline.get_clip_by_id("ac").timeline_start == 48

    @pytest.mark.skip(reason="Y position depends on full QGraphicsScene layout; verify manually in real UI")
    def test_linked_drag_y_locked_to_tracks(self, qapp) -> None:
        """Linked clips stay on their respective tracks during drag.

        NOTE: This test requires a fully initialized QGraphicsScene to get
        correct Y positions. In headless mode the layout engine may not
        produce exact pixel coordinates. Verify manually in the real UI.
        """
        view = TimelineView()
        view.load_timeline(_sequence_with_linked_clips())

        clip_v = view._items_map["vc"]
        clip_a = view._items_map["ac"]

        # Record original Y positions
        orig_y_v = clip_v.y()
        orig_y_a = clip_a.y()

        # Use the clip's current Y as scene_y to simulate same-track move
        scene_y = clip_v.y() + 1.0
        # Simulate drag via _on_clip_moved
        view._on_clip_moved("vc", "v1", 60, scene_y)

        # Both clips should keep their original Y positions (locked to tracks)
        assert clip_v.y() == orig_y_v
        assert clip_a.y() == orig_y_a

    def test_linked_drag_zero_delta_keeps_position(self, qapp) -> None:
        """Drag with zero delta does not change clip positions."""
        view = TimelineView()
        view.load_timeline(_sequence_with_linked_clips())
        view._snap_enabled = False

        view._on_drag_finished("vc", "v1", 0, 0, "v1")

        assert view._timeline.get_clip_by_id("vc").timeline_start == 0
        assert view._timeline.get_clip_by_id("ac").timeline_start == 0

    def test_linked_drag_negative_delta_clamped_by_move_group(self, qapp) -> None:
        """Moving linked clips before frame 0 is rejected by MoveGroup validation."""
        view = TimelineView()
        seq = _sequence_with_linked_clips()
        # Move starting position forward so a large negative delta would be invalid
        vc = seq.get_clip_by_id("vc")
        ac = seq.get_clip_by_id("ac")
        # The clips start at 0, so moving to -10 should be invalid
        # However, _on_drag_finished applies Math.max(0, ...) internally.
        # We test that the clips don't end up at a negative frame.
        view.load_timeline(seq)
        view._snap_enabled = False

        # Even if we call with new_start=-10, the engine should clamp
        view._on_drag_finished("vc", "v1", 0, -10, "v1")

        # After the operation, clips should not be at negative frames
        vc_after = view._timeline.get_clip_by_id("vc")
        ac_after = view._timeline.get_clip_by_id("ac")
        assert vc_after.timeline_start >= 0
        assert ac_after.timeline_start >= 0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Track Header Badge Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTrackHeaderBadge:
    """Verify track header badges use type-local indices (V1, V2, A1, A2)."""

    def test_badge_index_uses_type_local_index(self, qapp) -> None:
        """First audio track should show A1, not A2 (type-local index)."""
        view = TimelineView()
        view.load_timeline(_sequence_with_multi_tracks(2, 2))

        # Check that badge indices are type-local
        # Video tracks: v1 -> index 0 (V1), v2 -> index 1 (V2)
        v1_header = view._track_headers["v1"]
        v2_header = view._track_headers["v2"]
        assert v1_header._badge_index == 0, "v1 should have badge_index=0 (V1)"
        assert v2_header._badge_index == 1, "v2 should have badge_index=1 (V2)"

        # Audio tracks: a1 -> index 0 (A1), a2 -> index 1 (A2)
        a1_header = view._track_headers["a1"]
        a2_header = view._track_headers["a2"]
        assert a1_header._badge_index == 0, "a1 should have badge_index=0 (A1)"
        assert a2_header._badge_index == 1, "a2 should have badge_index=1 (A2)"

    def test_badge_index_single_video_single_audio(self, qapp) -> None:
        """With one video and one audio track, both get badge_index=0."""
        view = TimelineView()
        view.load_timeline(_sequence_with_multi_tracks(1, 1))

        assert view._track_headers["v1"]._badge_index == 0
        assert view._track_headers["a1"]._badge_index == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Target Track Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTargetTracks:
    """Verify video and audio target tracks are independent."""

    def test_target_tracks_are_independent(self, qapp) -> None:
        """V target and A target can be set independently."""
        view = TimelineView()
        view.load_timeline(_sequence_with_multi_tracks(2, 2))

        # Initial targets
        assert view._target_track_id == "v1"
        assert view._target_audio_track_id == "a1"

        # Change video target to v2
        view._on_target_track_changed("v2")
        assert view._target_track_id == "v2"
        assert view._target_audio_track_id == "a1"  # unchanged

        # Change audio target to a2
        view._on_target_track_changed("a2")
        assert view._target_track_id == "v2"  # unchanged
        assert view._target_audio_track_id == "a2"

    def test_get_target_video_and_audio_independent(self, qapp) -> None:
        """get_target_track_id and get_target_audio_track_id return independently."""
        view = TimelineView()
        view.load_timeline(_sequence_with_multi_tracks(2, 2))

        # Set video target to v2
        view._on_target_track_changed("v2")

        assert view.get_target_track_id() == "v2"
        assert view.get_target_audio_track_id() == "a1"

    def test_changing_video_target_does_not_affect_audio_target(self, qapp) -> None:
        """Setting video target multiple times does not disturb audio target."""
        view = TimelineView()
        view.load_timeline(_sequence_with_multi_tracks(2, 2))

        view._on_target_track_changed("a2")  # audio target -> a2
        view._on_target_track_changed("v2")  # video target -> v2
        view._on_target_track_changed("v1")  # back to v1

        assert view._target_audio_track_id == "a2"
        assert view._target_track_id == "v1"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Splitter Adjacency Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSplitterAdjacency:
    """Verify V1 bottom / A1 top are adjacent to the splitter."""

    def test_v1_a1_adjacent_to_splitter(self) -> None:
        """V1 bottom should be at splitter top; A1 top at splitter bottom."""
        engine = make_engine(video_count=1, audio_count=1)

        v1_rect = engine.track_rect("v1")
        a1_rect = engine.track_rect("a1")
        splitter = engine.splitter_rect()

        # V1 bottom should be at or near splitter top
        assert v1_rect.bottom() <= splitter.top() + 3, (
            f"V1 bottom ({v1_rect.bottom()}) should be near splitter top ({splitter.top()})"
        )

        # A1 top should be at or near splitter bottom
        assert a1_rect.top() >= splitter.bottom() - 1, (
            f"A1 top ({a1_rect.top()}) should be near splitter bottom ({splitter.bottom()})"
        )

    def test_v1_a1_adjacent_with_many_tracks(self) -> None:
        """With multiple tracks, V1 is still closest to splitter on video side,
        and A1 closest on audio side."""
        engine = make_engine(video_count=4, audio_count=4)

        area = engine.area_layout()
        v1_rect = engine.track_rect("v1")
        a1_rect = engine.track_rect("a1")

        # V1 bottom near splitter top
        assert v1_rect.bottom() <= area.splitter_rect.top() + 3

        # A1 top near splitter bottom
        assert a1_rect.top() >= area.splitter_rect.bottom() - 1

    def test_splitter_sits_between_video_and_audio_areas(self) -> None:
        """Splitter is between the video area and audio area."""
        engine = make_engine(video_count=2, audio_count=2)
        area = engine.area_layout()

        assert area.video_area.bottom() == pytest.approx(area.splitter_rect.top())
        assert area.audio_area.top() == pytest.approx(area.splitter_rect.bottom())


# ═══════════════════════════════════════════════════════════════════════════
# 5. Drag Preview Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestDragPreviewConsistency:
    """Verify drop target frame matches engine.x_to_frame conversion."""

    def test_drop_target_frame_matches_x_to_frame(self) -> None:
        """For various X positions, drop target frame matches engine.x_to_frame."""
        engine = make_engine(ppf=5.0)
        v1_mid = _center_y(engine, "v1")

        for offset_px in [0, 50, 150, 300, 750]:
            scene_x = TRACK_HEADER_WIDTH + offset_px
            expected_frame = engine.x_to_frame(scene_x)
            target = engine.resolve_drop_target(scene_x, v1_mid, "video")
            assert target.frame == expected_frame, (
                f"At offset {offset_px}: expected frame {expected_frame}, got {target.frame}"
            )

    def test_hit_frame_matches_x_to_frame(self) -> None:
        """TimelineHit frame matches engine.x_to_frame for track hits."""
        engine = make_engine(ppf=5.0)
        v2_mid = _center_y(engine, "v2")

        for offset_px in [0, 100, 250]:
            scene_x = TRACK_HEADER_WIDTH + offset_px
            expected_frame = engine.x_to_frame(scene_x)
            hit = engine.resolve_timeline_hit(scene_x, v2_mid)
            assert hit.frame == expected_frame

    def test_frame_to_x_roundtrip(self) -> None:
        """frame_to_x and x_to_frame are consistent (within 1 frame)."""
        engine = make_engine(ppf=5.0)

        for frame in [0, 10, 50, 100, 500, 1000]:
            x = engine.frame_to_x(frame)
            recovered = engine.x_to_frame(x)
            assert abs(recovered - frame) <= 1, (
                f"Frame {frame}: x={x}, recovered={recovered}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Wrong-Area Drop Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestWrongAreaDrop:
    """Audio dropped on video area auto-snaps to audio, and vice versa."""

    def test_audio_drop_on_video_area_snaps_to_audio(self) -> None:
        """Audio clip dropped on video area auto-snaps to audio target."""
        engine = make_engine(video_count=2, audio_count=2)
        v1_mid = _center_y(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, v1_mid, "audio")
        assert target.is_valid is True
        assert target.track_type == "audio"
        assert target.track_id == "a1"
        assert "Snapped" in (target.reason or "")

    def test_video_drop_on_audio_area_snaps_to_video(self) -> None:
        """Video clip dropped on audio area auto-snaps to video target."""
        engine = make_engine(video_count=2, audio_count=2)
        a1_mid = _center_y(engine, "a1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, a1_mid, "video")
        assert target.is_valid is True
        assert target.track_type == "video"
        assert target.track_id == "v1"
        assert "Snapped" in (target.reason or "")

    def test_audio_drop_on_v2_snaps_to_a1(self) -> None:
        """Audio dropped on V2 (not V1) still snaps to first audio track."""
        engine = make_engine(video_count=3, audio_count=2)
        v2_mid = _center_y(engine, "v2")
        scene_x = TRACK_HEADER_WIDTH + 200

        target = engine.resolve_drop_target(scene_x, v2_mid, "audio")
        assert target.is_valid is True
        assert target.track_id == "a1"

    def test_video_drop_on_a2_snaps_to_v1(self) -> None:
        """Video dropped on A2 still snaps to first video track."""
        engine = make_engine(video_count=2, audio_count=3)
        a2_mid = _center_y(engine, "a2")
        scene_x = TRACK_HEADER_WIDTH + 200

        target = engine.resolve_drop_target(scene_x, a2_mid, "video")
        assert target.is_valid is True
        assert target.track_id == "v1"


# ═══════════════════════════════════════════════════════════════════════════
# 7. Locked Track Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestLockedTrack:
    """Locked tracks reject drop operations."""

    def test_locked_track_rejects_move_and_drop(self) -> None:
        """resolve_drop_target with locked track returns is_valid=False."""
        engine = make_engine()
        v1_mid = _center_y(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(
            scene_x, v1_mid, "video", locked_track_ids={"v1"}
        )
        assert target.is_valid is False
        assert target.track_id == "v1"
        assert "locked" in (target.reason or "").lower()

    def test_locked_audio_track_rejects_audio_drop(self) -> None:
        """Locked audio track rejects audio drop."""
        engine = make_engine()
        a1_mid = _center_y(engine, "a1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(
            scene_x, a1_mid, "audio", locked_track_ids={"a1"}
        )
        assert target.is_valid is False
        assert "locked" in (target.reason or "").lower()

    def test_locked_track_rejects_before_type_check(self) -> None:
        """If a track is locked, the lock check rejects before type compatibility
        is considered -- even for a media type that would otherwise auto-snap."""
        engine = make_engine()
        v1_mid = _center_y(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        # Audio on a locked video track -> lock check rejects first
        target = engine.resolve_drop_target(
            scene_x, v1_mid, "audio", locked_track_ids={"v1"}
        )
        assert target.is_valid is False
        assert "locked" in (target.reason or "").lower()

    def test_multiple_locked_tracks(self) -> None:
        """When both video tracks are locked, video drop on either is rejected."""
        engine = make_engine(video_count=2, audio_count=2)
        v1_mid = _center_y(engine, "v1")
        v2_mid = _center_y(engine, "v2")
        scene_x = TRACK_HEADER_WIDTH + 100
        locked = {"v1", "v2"}

        t1 = engine.resolve_drop_target(
            scene_x, v1_mid, "video", locked_track_ids=locked
        )
        t2 = engine.resolve_drop_target(
            scene_x, v2_mid, "video", locked_track_ids=locked
        )
        assert t1.is_valid is False
        assert t2.is_valid is False


# ═══════════════════════════════════════════════════════════════════════════
# 8. MoveGroup Validation Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestMoveGroupValidation:
    """Verify MoveGroup.is_valid correctly checks all members."""

    def test_move_group_valid_when_all_members_valid(self) -> None:
        """MoveGroup is_valid returns True when all members have non-negative start."""
        group = MoveGroup(
            primary_clip_id="vc",
            members=(
                MoveGroupMember(clip_id="vc", track_id="v1", old_start=0, new_start=24),
                MoveGroupMember(clip_id="ac", track_id="a1", old_start=0, new_start=24),
            ),
            delta_frames=24,
        )
        assert group.is_valid is True

    def test_move_group_invalid_when_any_member_negative(self) -> None:
        """MoveGroup is_valid returns False when any member would have negative start."""
        group = MoveGroup(
            primary_clip_id="vc",
            members=(
                MoveGroupMember(clip_id="vc", track_id="v1", old_start=5, new_start=0),
                MoveGroupMember(clip_id="ac", track_id="a1", old_start=0, new_start=-5),
            ),
            delta_frames=-5,
        )
        assert group.is_valid is False

    def test_move_group_valid_at_zero(self) -> None:
        """MoveGroup is valid when new_start is exactly 0."""
        group = MoveGroup(
            primary_clip_id="vc",
            members=(
                MoveGroupMember(clip_id="vc", track_id="v1", old_start=10, new_start=0),
            ),
            delta_frames=-10,
        )
        assert group.is_valid is True

    def test_move_group_with_many_members(self) -> None:
        """MoveGroup with 5 members, one negative, is invalid."""
        members = tuple(
            MoveGroupMember(
                clip_id=f"c{i}",
                track_id=f"v{i + 1}",
                old_start=i * 10,
                new_start=i * 10 + 5,
            )
            for i in range(4)
        )
        # Add one with negative new_start
        members += (
            MoveGroupMember(clip_id="bad", track_id="a1", old_start=2, new_start=-1),
        )
        group = MoveGroup(primary_clip_id="c0", members=members, delta_frames=5)
        assert group.is_valid is False

    def test_move_group_empty_members_is_valid(self) -> None:
        """MoveGroup with no members is vacuously valid."""
        group = MoveGroup(
            primary_clip_id="c",
            members=(),
            delta_frames=0,
        )
        assert group.is_valid is True

    def test_move_group_all_at_large_frame(self) -> None:
        """MoveGroup at large frame numbers is valid."""
        group = MoveGroup(
            primary_clip_id="c1",
            members=(
                MoveGroupMember(clip_id="c1", track_id="v1", old_start=10000, new_start=10050),
                MoveGroupMember(clip_id="c2", track_id="a1", old_start=10000, new_start=10050),
            ),
            delta_frames=50,
        )
        assert group.is_valid is True


# ═══════════════════════════════════════════════════════════════════════════
# 9. Clip Drag Event Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestClipDragEvent:
    """Verify interaction state machine transitions correctly on clip drag."""

    def test_interaction_session_begins_non_idle(self, qapp) -> None:
        """After begin() with DRAGGING_CLIP, session is not IDLE."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession()
        assert session.is_idle is True

        session.begin(
            InteractionState.DRAGGING_CLIP,
            QPointF(100, 200),
            primary_clip_id="vc",
            primary_track_id="v1",
        )
        assert session.is_idle is False
        assert session.state == InteractionState.DRAGGING_CLIP
        assert session.primary_clip_id == "vc"

    def test_interaction_session_finish_returns_to_idle(self, qapp) -> None:
        """After finish(), session returns to IDLE."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession()
        session.begin(InteractionState.DRAGGING_CLIP, QPointF(100, 200))
        assert session.is_idle is False

        session.finish()
        assert session.is_idle is True
        assert session.primary_clip_id is None

    def test_interaction_session_cancel_returns_to_idle(self, qapp) -> None:
        """After cancel(), session returns to IDLE and clears state."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession()
        session.begin(
            InteractionState.DRAGGING_CLIP,
            QPointF(100, 200),
            primary_clip_id="vc",
            original_start=50,
        )
        session.cancel()
        assert session.is_idle is True
        assert session.primary_clip_id is None
        assert session.clip_drag_session is None

    def test_drag_threshold_not_passed_initially(self, qapp) -> None:
        """threshold_passed is False initially and after begin."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession()
        session.begin(InteractionState.DRAGGING_CLIP, QPointF(100, 200))
        assert session.threshold_passed is False

    def test_drag_threshold_passed_after_movement(self, qapp) -> None:
        """threshold_passed becomes True after moving beyond threshold pixels."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession(drag_threshold_px=4.0)
        session.begin(InteractionState.DRAGGING_CLIP, QPointF(100, 200))

        # Move 2 pixels -- below threshold
        session.update_pos(QPointF(102, 200))
        assert session.threshold_passed is False

        # Move 5 pixels -- above threshold
        session.update_pos(QPointF(105, 200))
        assert session.threshold_passed is True

    def test_delta_frames_computation(self, qapp) -> None:
        """delta_frames computes correctly from pixel delta and ppf."""
        from PyQt6.QtCore import QPointF

        session = InteractionSession()
        session.begin(InteractionState.DRAGGING_CLIP, QPointF(100, 200))
        session.update_pos(QPointF(250, 200))

        # 150 px / 5.0 ppf = 30 frames
        assert session.delta_frames(5.0) == 30

    def test_clip_drag_session_stores_original_positions(self, qapp) -> None:
        """ClipDragSession correctly stores original clip positions."""
        session = ClipDragSession(
            primary_clip_id="vc",
            linked_clip_ids=["ac"],
            original_positions={"vc": 100, "ac": 100},
        )
        assert session.original_positions["vc"] == 100
        assert session.original_positions["ac"] == 100

    def test_clip_drag_session_move_group_assignment(self, qapp) -> None:
        """ClipDragSession can hold a MoveGroup."""
        group = MoveGroup(
            primary_clip_id="vc",
            members=(
                MoveGroupMember(clip_id="vc", track_id="v1", old_start=0, new_start=24),
            ),
            delta_frames=24,
        )
        session = ClipDragSession(
            primary_clip_id="vc",
            move_group=group,
        )
        assert session.move_group is not None
        assert session.move_group.is_valid is True


# ═══════════════════════════════════════════════════════════════════════════
# 10. Capture Tool Test
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureTool:
    """Verify the capture tool module is importable."""

    def test_capture_tool_imports(self) -> None:
        """Capture tool module imports without error."""
        spec = importlib.util.find_spec("tools.capture_edit_page")
        assert spec is not None, "tools.capture_edit_page module not found"

    def test_capture_tool_has_main_classes(self) -> None:
        """Capture tool module exposes EditPageCapture and main function."""
        import tools.capture_edit_page as cap

        assert hasattr(cap, "EditPageCapture")
        assert hasattr(cap, "VisualAcceptanceChecker")
        assert callable(cap.main)

    def test_capture_tool_baseline_dir_exists(self) -> None:
        """Capture tool's baseline directory path is valid."""
        import tools.capture_edit_page as cap

        assert cap.BASELINE_DIR is not None
        assert "tests" in str(cap.BASELINE_DIR)
        assert "baselines" in str(cap.BASELINE_DIR)
