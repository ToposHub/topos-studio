"""
Tests for fps metadata propagation from asset to ClipInstance via insert chain.

Validates:
- insert_clip_at_playhead creates ClipInstance with fps metadata
- ClipInstance uses mapper path when fps metadata present
- Fallback to old formula when no fps metadata
- with_* methods preserve fps fields
- InsertClipCommand serialization preserves fps
"""

import pytest
import uuid
from fractions import Fraction


def _make_sequence(fps_num=24, fps_den=1):
    """Create a minimal TimelineSequence for testing."""
    from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack
    from src.shared.media.metadata import Rational
    return TimelineSequence(
        fps=Rational(fps_num, fps_den),
        video_tracks=(EditTrack(track_id="track-1", track_type="video"),),
    )


# ===================================================================
# Test: insert_clip_at_playhead with fps metadata
# ===================================================================

class TestInsertClipFpsMetadata:
    """insert_clip_at_playhead propagates fps metadata to ClipInstance."""

    def test_insert_clip_sets_fps_metadata(self):
        """insert_clip_at_playhead creates clip with fps metadata."""
        from src.features.edit.core.timeline_ops import insert_clip_at_playhead
        seq = _make_sequence(fps_num=30, fps_den=1)
        result = insert_clip_at_playhead(
            seq, "asset-1", 0, 99, 0, "track-1",
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=30, timeline_fps_den=1,
        )
        clip = result.video_tracks[0].clips[0]
        assert clip._source_fps_num == 24
        assert clip._source_fps_den == 1
        assert clip._timeline_fps_num == 30
        assert clip._timeline_fps_den == 1

    def test_inserted_clip_uses_mapper(self):
        """Clip with fps metadata uses mapper for frame mapping."""
        from src.features.edit.core.timeline_ops import insert_clip_at_playhead
        seq = _make_sequence(fps_num=30, fps_den=1)
        # source 24 frames, timeline_duration = int(24/1) = 24
        # But we want to test 24fps source in 30fps timeline cadence
        # Use longer clip: source_duration=240, timeline=240 frames
        result = insert_clip_at_playhead(
            seq, "asset-1", 0, 239, 0, "track-1",
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=30, timeline_fps_den=1,
        )
        clip = result.video_tracks[0].clips[0]
        # mapper: 30fps timeline → 24fps source
        frames = [clip.timeline_to_source_frame(i) for i in range(clip.timeline_duration)]
        # Cadence should be uniform: max gap <= 1
        diffs = [frames[i+1] - frames[i] for i in range(len(frames)-1)]
        assert max(diffs) <= 1, f"Non-uniform cadence: diffs={diffs}"

    def test_insert_without_fps_falls_back(self):
        """No fps metadata → all fields None, old formula works."""
        from src.features.edit.core.timeline_ops import insert_clip_at_playhead
        seq = _make_sequence()
        result = insert_clip_at_playhead(seq, "asset-1", 0, 99, 0, "track-1")
        clip = result.video_tracks[0].clips[0]
        assert clip._source_fps_num is None
        assert clip._timeline_fps_num is None
        # Old formula still works
        assert clip.timeline_to_source_frame(5) == 5

    def test_overwrite_clip_sets_fps_metadata(self):
        """overwrite_clip_at_playhead also propagates fps metadata."""
        from src.features.edit.core.timeline_ops import overwrite_clip_at_playhead
        seq = _make_sequence(fps_num=30, fps_den=1)
        result = overwrite_clip_at_playhead(
            seq, "asset-1", 0, 99, 0, "track-1",
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=30, timeline_fps_den=1,
        )
        clip = result.video_tracks[0].clips[0]
        assert clip._source_fps_num == 24
        assert clip._timeline_fps_num == 30


# ===================================================================
# Test: with_* methods preserve fps
# ===================================================================

class TestWithMethodsPreserveFps:
    """All with_* methods should preserve fps fields."""

    def test_with_timeline_start(self):
        from src.features.edit.core.timeline_v2 import ClipInstance
        clip = ClipInstance(
            asset_id="a", track_id="t",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
            _source_fps_num=24, _source_fps_den=1,
            _timeline_fps_num=30, _timeline_fps_den=1,
        )
        moved = clip.with_timeline_start(10)
        assert moved._source_fps_num == 24
        assert moved._source_fps_den == 1
        assert moved._timeline_fps_num == 30
        assert moved._timeline_fps_den == 1

    def test_with_trim_left(self):
        from src.features.edit.core.timeline_v2 import ClipInstance
        clip = ClipInstance(
            asset_id="a", track_id="t",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
            _source_fps_num=24, _source_fps_den=1,
            _timeline_fps_num=30, _timeline_fps_den=1,
        )
        trimmed = clip.with_trim_left(5, 5)
        assert trimmed._source_fps_num == 24
        assert trimmed._timeline_fps_num == 30

    def test_with_speed(self):
        from src.features.edit.core.timeline_v2 import ClipInstance
        clip = ClipInstance(
            asset_id="a", track_id="t",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100,
            _source_fps_num=24, _source_fps_den=1,
            _timeline_fps_num=30, _timeline_fps_den=1,
        )
        sped = clip.with_speed(2.0)
        assert sped._source_fps_num == 24
        assert sped._timeline_fps_num == 30


class TestCommandsPreserveFps:
    """Command apply paths should not strip precise frame-mapping metadata."""

    def test_add_clip_command_preserves_fps_metadata(self):
        from src.features.edit.core.commands import AddClipCommand
        from src.features.edit.core.timeline_v2 import ClipInstance

        seq = _make_sequence(fps_num=30, fps_den=1)
        clip = ClipInstance(
            asset_id="a",
            track_id="source-track",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=80,
            _source_fps_num=24,
            _source_fps_den=1,
            _timeline_fps_num=30,
            _timeline_fps_den=1,
        )

        new_seq = AddClipCommand.create("track-1", clip).apply(seq)
        added = new_seq.video_tracks[0].clips[0]

        assert added.track_id == "track-1"
        assert added._source_fps_num == 24
        assert added._source_fps_den == 1
        assert added._timeline_fps_num == 30
        assert added._timeline_fps_den == 1


# ===================================================================
# Test: InsertClipCommand serialization
# ===================================================================

class TestInsertClipCommandFpsSerialization:
    """InsertClipCommand preserves fps through to_dict/from_dict."""

    def test_round_trip_with_fps(self):
        from src.features.edit.core.commands import InsertClipCommand
        cmd = InsertClipCommand.create(
            asset_id="a1",
            in_frame=0, out_frame=99,
            playhead=10, track_id="t1",
            source_fps_num=24000, source_fps_den=1001,
            timeline_fps_num=25, timeline_fps_den=1,
        )
        d = cmd.to_dict()
        assert d["source_fps_num"] == 24000
        assert d["source_fps_den"] == 1001
        assert d["timeline_fps_num"] == 25
        assert d["timeline_fps_den"] == 1

        restored = InsertClipCommand.from_dict(d)
        assert restored._source_fps_num == 24000
        assert restored._source_fps_den == 1001
        assert restored._timeline_fps_num == 25
        assert restored._timeline_fps_den == 1

    def test_round_trip_without_fps(self):
        from src.features.edit.core.commands import InsertClipCommand
        cmd = InsertClipCommand.create(
            asset_id="a1",
            in_frame=0, out_frame=99,
            playhead=10, track_id="t1",
        )
        d = cmd.to_dict()
        assert d["source_fps_num"] is None

        restored = InsertClipCommand.from_dict(d)
        assert restored._source_fps_num is None
        assert restored._timeline_fps_num is None
