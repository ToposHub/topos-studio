"""Multicam editing support tests.

Covers the multicam data model (angles / groups) and the switch commands
(UpdateClipSourceCommand, SwitchMulticamAngleCommand). The switch command
combines a split with a source re-point, recording a hard cut between angles.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    CommandStack,
    SwitchMulticamAngleCommand,
    UpdateClipSourceCommand,
)
from src.features.edit.core.multicam import (
    METADATA_MULTICAM_KEY,
    MulticamAngle,
    MulticamAngleGroup,
    attach_multicam_group,
    get_multicam_group_id,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ── Fixtures ──


@pytest.fixture
def clip() -> ClipInstance:
    return ClipInstance(
        clip_id="c1", asset_id="angle-wide", track_id="t1",
        timeline_start=0, timeline_duration=20, source_start=0, source_duration=20,
    )


@pytest.fixture
def sequence(clip: ClipInstance) -> TimelineSequence:
    track = EditTrack(track_id="t1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="seq1", name="S", fps=Rational(24, 1),
        width=1920, height=1080, video_tracks=(track,),
    )


@pytest.fixture
def angle_group() -> MulticamAngleGroup:
    return MulticamAngleGroup(
        group_id="g1", name="Interview",
        angles=(
            MulticamAngle(angle_id="1", asset_id="angle-wide", label="Wide"),
            MulticamAngle(angle_id="2", asset_id="angle-close", sync_offset=-3, label="Close"),
            MulticamAngle(angle_id="3", asset_id="angle-side", label="Side"),
        ),
        default_angle_id="1",
    )


# ── Data model ──


class TestMulticamAngleGroup:
    def test_default_angle(self, angle_group: MulticamAngleGroup):
        assert angle_group.default_angle.asset_id == "angle-wide"

    def test_default_falls_back_to_first(self):
        g = MulticamAngleGroup(angles=(MulticamAngle("1", "a"),))
        assert g.default_angle.asset_id == "a"

    def test_get_angle(self, angle_group: MulticamAngleGroup):
        assert angle_group.get_angle("2").sync_offset == -3
        assert angle_group.get_angle("nope") is None

    def test_angle_index(self, angle_group: MulticamAngleGroup):
        assert angle_group.angle_index("1") == 1
        assert angle_group.angle_index("3") == 3
        assert angle_group.angle_index("x") == -1

    def test_rejects_duplicate_angle_ids(self):
        with pytest.raises(ValueError):
            MulticamAngleGroup(angles=(
                MulticamAngle("1", "a"), MulticamAngle("1", "b"),
            ))

    def test_rejects_invalid_default(self):
        with pytest.raises(ValueError):
            MulticamAngleGroup(
                angles=(MulticamAngle("1", "a"),),
                default_angle_id="missing",
            )

    def test_serialization_roundtrip(self, angle_group: MulticamAngleGroup):
        d = angle_group.to_dict()
        restored = MulticamAngleGroup.from_dict(d)
        assert restored.group_id == "g1"
        assert len(restored.angles) == 3
        assert restored.angles[1].sync_offset == -3
        assert restored.default_angle_id == "1"


class TestMulticamMetadata:
    def test_attach_and_read(self):
        meta = attach_multicam_group({"foo": 1}, "g1")
        assert get_multicam_group_id(meta) == "g1"
        assert meta["foo"] == 1  # original keys preserved

    def test_read_none(self):
        assert get_multicam_group_id(None) is None
        assert get_multicam_group_id({}) is None
        assert get_multicam_group_id({METADATA_MULTICAM_KEY: ""}) is None


# ── UpdateClipSourceCommand ──


class TestUpdateClipSourceCommand:
    def test_swaps_asset_preserving_timeline(self, sequence: TimelineSequence, clip: ClipInstance):
        cmd = UpdateClipSourceCommand.create("t1", "c1", "angle-close", 5)
        new_seq, updated = cmd.apply(sequence)
        nc = new_seq.get_track_by_id("t1").get_clip_by_id("c1")
        assert nc.asset_id == "angle-close"
        assert nc.source_start == 5
        # timeline placement unchanged
        assert nc.timeline_start == 0
        assert nc.timeline_duration == 20

    def test_undo_restores_source(self, sequence: TimelineSequence):
        cmd = UpdateClipSourceCommand.create("t1", "c1", "angle-close", 5)
        new_seq, updated = cmd.apply(sequence)
        restored = updated.undo(new_seq)
        rc = restored.get_track_by_id("t1").get_clip_by_id("c1")
        assert rc.asset_id == "angle-wide"
        assert rc.source_start == 0

    def test_command_stack_undo(self, sequence: TimelineSequence):
        stack = CommandStack()
        cmd = UpdateClipSourceCommand.create("t1", "c1", "angle-side", 2)
        tl = stack.execute(cmd, sequence)
        assert tl.get_track_by_id("t1").get_clip_by_id("c1").asset_id == "angle-side"
        tl = stack.undo(tl)
        assert tl.get_track_by_id("t1").get_clip_by_id("c1").asset_id == "angle-wide"

    def test_serialization(self):
        cmd = UpdateClipSourceCommand.create("t1", "c1", "asset-x", 7)
        d = cmd.to_dict()
        assert d["command_type"] == "UpdateClipSourceCommand"
        assert d["new_asset_id"] == "asset-x"
        restored = UpdateClipSourceCommand.from_dict(d)
        assert restored.new_asset_id == "asset-x"
        assert restored.new_source_start == 7


# ── SwitchMulticamAngleCommand ──


class TestSwitchMulticamAngleCommand:
    def test_split_and_repoint(self, sequence: TimelineSequence):
        """Switching mid-clip splits it and re-points the right half."""
        cmd = SwitchMulticamAngleCommand.create(
            "t1", "c1", switch_frame=8, angle_asset_id="angle-close", angle_sync_offset=-2,
        )
        new_seq, _ = cmd.apply(sequence)
        clips = sorted(new_seq.get_track_by_id("t1").clips, key=lambda c: c.timeline_start)
        assert len(clips) == 2
        # Left half keeps original asset
        assert clips[0].asset_id == "angle-wide"
        assert clips[0].timeline_duration == 8
        # Right half switched, with sync offset applied.
        # After split at frame 8, the right segment's source_start is 8
        # (original clip source_start 0 + 8 frames). Then sync_offset -2 → 6.
        assert clips[1].asset_id == "angle-close"
        assert clips[1].timeline_start == 8
        assert clips[1].source_start == 6  # 8 + (-2)

    def test_positive_sync_offset(self, sequence: TimelineSequence):
        cmd = SwitchMulticamAngleCommand.create(
            "t1", "c1", switch_frame=5, angle_asset_id="cam", angle_sync_offset=10,
        )
        new_seq, _ = cmd.apply(sequence)
        clips = sorted(new_seq.get_track_by_id("t1").clips, key=lambda c: c.timeline_start)
        # base source_start at split = 5, + offset 10 = 15
        assert clips[1].source_start == 15

    def test_no_split_at_clip_start(self, sequence: TimelineSequence):
        """Switching exactly at the clip start re-points the whole clip (no split)."""
        cmd = SwitchMulticamAngleCommand.create(
            "t1", "c1", switch_frame=0, angle_asset_id="angle-close", angle_sync_offset=0,
        )
        new_seq, updated = cmd.apply(sequence)
        clips = new_seq.get_track_by_id("t1").clips
        assert len(clips) == 1  # no split
        assert clips[0].asset_id == "angle-close"
        assert updated._did_split is False

    def test_command_stack_undo_restores_single_clip(self, sequence: TimelineSequence):
        stack = CommandStack()
        cmd = SwitchMulticamAngleCommand.create("t1", "c1", 10, "cam2", 0)
        tl = stack.execute(cmd, sequence)
        assert len(tl.get_track_by_id("t1").clips) == 2
        tl = stack.undo(tl)
        assert tl is not None
        assert len(tl.get_track_by_id("t1").clips) == 1

    def test_rejects_frame_outside_clip(self, sequence: TimelineSequence):
        cmd = SwitchMulticamAngleCommand.create("t1", "c1", 100, "cam", 0)
        with pytest.raises(ValueError):
            cmd.apply(sequence)
