"""P0-04 regression tests: cross-track move onto an existing track.

Covers the acceptance matrix from the 2026-08-27 edit-page audit:

- MoveClipToTrackCommand(create_new_track=False) previously raised
  NotImplementedError; it must now perform a real move with undo.
- Cross-type moves (video → audio) are rejected.
- Overlapping drops are rejected with an actionable message.
- Locked source/target tracks are rejected.
- Clip-scoped markers migrate with the clip and return on undo.
- MoveClipToExistingTrackCommand shares the same validation contract.
"""

from __future__ import annotations

import pytest
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TimelineMarker,
)
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    MoveClipToTrackCommand,
    MoveClipToExistingTrackCommand,
    CommandStack,
)


def _video_clip(clip_id: str, track_id: str, start: int, duration: int = 40) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset_{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=SpeedParams(speed=1.0),
    )


def _timeline() -> TimelineSequence:
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(
        _video_clip("clipA", "v1", 0),
        _video_clip("clipB", "v1", 60),
    ))
    v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1)
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    return TimelineSequence(
        timeline_id="seq",
        name="Test",
        video_tracks=(v1, v2),
        audio_tracks=(a1,),
    )


class TestMoveClipToTrackCommandExistingTrack:
    """MoveClipToTrackCommand with create_new_track=False (P0-04)."""

    def test_moves_clip_to_existing_track(self) -> None:
        tl = _timeline()
        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=10,
            old_start=0,
        )
        result = cmd.apply(tl)

        assert result.get_track_by_id("v2").get_clip_by_id("clipA") is not None
        assert result.get_track_by_id("v2").get_clip_by_id("clipA").timeline_start == 10
        assert result.get_track_by_id("v1").get_clip_by_id("clipA") is None
        # No track created or destroyed
        assert len(result.video_tracks) == 2

    def test_undo_restores_clip_and_markers(self) -> None:
        tl = _timeline()
        marker = TimelineMarker(
            marker_id="m1", frame=5, name="scoped", clip_id="clipA", track_id="v1",
        )
        tl = tl._rebuild(markers=(marker,))

        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=20,
            old_start=0,
        )
        moved = cmd.apply(tl)

        moved_marker = next(m for m in moved.markers if m.marker_id == "m1")
        assert moved_marker.frame == 25  # 5 + delta(20)
        assert moved_marker.track_id == "v2"

        restored = cmd.undo(moved)
        assert restored.get_track_by_id("v1").get_clip_by_id("clipA") is not None
        assert restored.get_track_by_id("v1").get_clip_by_id("clipA").timeline_start == 0
        assert restored.get_track_by_id("v2").get_clip_by_id("clipA") is None
        restored_marker = next(m for m in restored.markers if m.marker_id == "m1")
        assert restored_marker.frame == 5
        assert restored_marker.track_id == "v1"

    def test_cross_type_move_rejected(self) -> None:
        tl = _timeline()
        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="a1",
            new_start=0,
            old_start=0,
        )
        with pytest.raises(ValueError, match="Cannot move video clip to audio"):
            cmd.apply(tl)

    def test_overlap_rejected(self) -> None:
        tl = _timeline()
        v2 = tl.get_track_by_id("v2").with_clip_added(
            _video_clip("blocker", "v2", 10, duration=30)
        )
        tl = tl.with_track_updated(v2)

        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=20,  # overlaps blocker [10, 40)
            old_start=0,
        )
        with pytest.raises(ValueError, match="overlap clip 'blocker'"):
            cmd.apply(tl)

    def test_locked_target_rejected(self) -> None:
        tl = _timeline()
        from dataclasses import replace
        tl = tl.with_track_updated(replace(tl.get_track_by_id("v2"), is_locked=True))

        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=0,
            old_start=0,
        )
        with pytest.raises(ValueError, match="Target track is locked"):
            cmd.apply(tl)

    def test_locked_source_rejected(self) -> None:
        tl = _timeline()
        from dataclasses import replace
        tl = tl.with_track_updated(replace(tl.get_track_by_id("v1"), is_locked=True))

        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=0,
            old_start=0,
        )
        with pytest.raises(ValueError, match="Source track is locked"):
            cmd.apply(tl)

    def test_missing_target_track_id_rejected(self) -> None:
        tl = _timeline()
        cmd = MoveClipToTrackCommand(
            command_id=str(uuid.uuid4()),
            label="bad",
            source_track_id="v1",
            clip_id="clipA",
            target_track_type="video",
            create_new_track=False,
            new_timeline_start=0,
        )
        with pytest.raises(ValueError, match="target_track_id"):
            cmd.apply(tl)

    def test_non_overlapping_drop_on_occupied_track_succeeds(self) -> None:
        tl = _timeline()
        v2 = tl.get_track_by_id("v2").with_clip_added(
            _video_clip("blocker", "v2", 0, duration=30)
        )
        tl = tl.with_track_updated(v2)

        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=50,  # clipA spans [50, 90); blocker spans [0, 30)
            old_start=0,
        )
        result = cmd.apply(tl)
        assert result.get_track_by_id("v2").get_clip_by_id("clipA") is not None
        assert result.get_track_by_id("v2").get_clip_by_id("blocker") is not None

    def test_command_stack_undo_redo_roundtrip(self) -> None:
        tl = _timeline()
        stack = CommandStack()
        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=15,
            old_start=0,
        )
        moved = stack.execute(cmd, tl)
        undone = stack.undo(moved)
        assert undone.get_track_by_id("v1").get_clip_by_id("clipA") is not None
        redone = stack.redo(undone)
        assert redone.get_track_by_id("v2").get_clip_by_id("clipA") is not None

    def test_serialization_roundtrip(self) -> None:
        cmd = MoveClipToTrackCommand.create_for_existing_track(
            source_track_id="v1",
            clip_id="clipA",
            target_track_id="v2",
            new_start=5,
            old_start=0,
        )
        data = cmd.to_dict()
        assert data["target_track_id"] == "v2"
        restored = MoveClipToTrackCommand.from_dict(data)
        assert restored.target_track_id == "v2"
        assert restored.create_new_track is False


class TestMoveClipToExistingTrackValidation:
    """MoveClipToExistingTrackCommand shares the P0-04 contract."""

    def test_cross_type_rejected(self) -> None:
        tl = _timeline()
        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="a1",
            clip_id="clipA",
            new_start=0,
            old_start=0,
        )
        with pytest.raises(ValueError, match="Cannot move video clip to audio"):
            cmd.apply(tl)

    def test_overlap_rejected(self) -> None:
        tl = _timeline()
        v2 = tl.get_track_by_id("v2").with_clip_added(
            _video_clip("blocker", "v2", 0, duration=100)
        )
        tl = tl.with_track_updated(v2)

        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="v2",
            clip_id="clipA",
            new_start=10,
            old_start=0,
        )
        with pytest.raises(ValueError, match="overlap clip 'blocker'"):
            cmd.apply(tl)

    def test_locked_target_rejected(self) -> None:
        tl = _timeline()
        from dataclasses import replace
        tl = tl.with_track_updated(replace(tl.get_track_by_id("v2"), is_locked=True))
        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="v2",
            clip_id="clipA",
            new_start=0,
            old_start=0,
        )
        with pytest.raises(ValueError, match="locked"):
            cmd.apply(tl)

    def test_valid_move_still_works(self) -> None:
        tl = _timeline()
        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="v2",
            clip_id="clipA",
            new_start=50,
            old_start=0,
        )
        result = cmd.apply(tl)
        assert result.get_track_by_id("v2").get_clip_by_id("clipA") is not None


def test_move_preserves_track_fields():
    """审计修复回归：跨轨移动/撤销不丢源/目标轨的扩展字段。"""
    from src.features.edit.core.commands import MoveClipToExistingTrackCommand
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )
    from fractions import Fraction
    from src.features.edit.core.timeline_v2 import Rational

    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="src",
        timeline_start=0, timeline_duration=40,
        source_start=0, source_duration=40, speed=1.0,
    )
    source = EditTrack(
        track_id="src", track_type="video", name="V1",
        output_bus_id="bus1", stem_group="dialogue",
        is_visible=False, is_sync_locked=False, track_role="graphics",
        clips=(clip,),
    )
    target = EditTrack(track_id="dst", track_type="video", name="V2")
    timeline = TimelineSequence(
        timeline_id="tl", name="TL", fps=Rational(25, 1),
        video_tracks=(source, target),
    )

    cmd = MoveClipToExistingTrackCommand.create("src", "dst", "c1", 100)
    moved = cmd.apply(timeline)
    moved_source = moved.get_track_by_id("src")
    assert moved_source.output_bus_id == "bus1"          # 旧代码被重置为 None
    assert moved_source.stem_group == "dialogue"
    assert moved_source.is_visible is False
    assert moved_source.is_sync_locked is False

    undone = cmd.undo(moved)
    assert undone.get_track_by_id("dst").clips == ()  # clip 已移回
    assert undone.get_track_by_id("src").get_clip_by_id("c1") is not None
