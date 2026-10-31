"""
Tests for marker functionality.

Verifies:
- TimelineMarker creation and validation
- AddMarkerCommand / DeleteMarkerCommand / MoveMarkerCommand / EditMarkerCommand
- Marker navigation helpers
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineMarker,
    EditTrack,
    ClipInstance,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    AddMarkerCommand,
    BatchDeleteCommand,
    DeleteClipCommand,
    DeleteMarkerCommand,
    MoveClipCommand,
    MoveMarkerCommand,
    EditMarkerCommand,
)
from src.shared.media.metadata import Rational


def _make_timeline(markers=None) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=(track,),
        markers=tuple(markers) if markers else (),
    )


class TestTimelineMarker:
    """Verify TimelineMarker data model."""

    def test_create_marker(self):
        m = TimelineMarker(frame=10, name="Test")
        assert m.frame == 10
        assert m.name == "Test"
        assert m.color == "#ff0000"

    def test_invalid_frame_raises(self):
        with pytest.raises(ValueError):
            TimelineMarker(frame=-1)

    def test_invalid_color_raises(self):
        with pytest.raises(ValueError):
            TimelineMarker(frame=0, color="red")

    def test_to_dict_roundtrip(self):
        m = TimelineMarker(
            frame=42,
            name="Mark",
            color="#00ff00",
            comment="Note",
            clip_id="c1",
            track_id="v1",
        )
        d = m.to_dict()
        m2 = TimelineMarker.from_dict(d)
        assert m2.frame == m.frame
        assert m2.name == m.name
        assert m2.color == m.color
        assert m2.comment == m.comment
        assert m2.clip_id == "c1"
        assert m2.track_id == "v1"


class TestAddMarkerCommand:
    """Verify AddMarkerCommand."""

    def test_add_marker(self):
        tl = _make_timeline()
        marker = TimelineMarker(frame=50, name="M1")
        cmd = AddMarkerCommand.create(marker)
        new_tl = cmd.apply(tl)
        assert len(new_tl.markers) == 1
        assert new_tl.markers[0].frame == 50

    def test_marker_command_preserves_other_timeline_state(self):
        tl = TimelineSequence(
            timeline_id="tl1",
            name="Test",
            fps=Rational(24, 1),
            video_tracks=_make_timeline().video_tracks,
            range_start=4,
            range_end=80,
            master_volume=0.5,
            master_muted=True,
        )
        new_tl = AddMarkerCommand.create(
            TimelineMarker(frame=20, clip_id="c1", track_id="v1")
        ).apply(tl)
        assert new_tl.range_start == 4
        assert new_tl.range_end == 80
        assert new_tl.master_volume == 0.5
        assert new_tl.master_muted is True
        assert new_tl.markers[0].clip_id == "c1"

    def test_undo_add(self):
        tl = _make_timeline()
        marker = TimelineMarker(frame=50, name="M1")
        cmd = AddMarkerCommand.create(marker)
        new_tl = cmd.apply(tl)
        restored = cmd.undo(new_tl)
        assert len(restored.markers) == 0


class TestDeleteMarkerCommand:
    """Verify DeleteMarkerCommand."""

    def test_delete_marker(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="M1")
        tl = _make_timeline(markers=[m])
        cmd = DeleteMarkerCommand.create("m1")
        new_tl = cmd.apply(tl)
        assert len(new_tl.markers) == 0

    def test_undo_delete(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="M1")
        tl = _make_timeline(markers=[m])
        cmd = DeleteMarkerCommand.create("m1")
        new_tl = cmd.apply(tl)
        restored = cmd.undo(new_tl)
        assert len(restored.markers) == 1


class TestClipScopedMarkerLifecycle:
    """Clip-scoped markers follow moves and are removed with their clip."""

    def test_move_clip_translates_scoped_marker(self):
        marker = TimelineMarker(
            marker_id="m1",
            frame=25,
            name="On clip",
            clip_id="c1",
            track_id="v1",
        )
        timeline = _make_timeline(markers=[marker])
        command = MoveClipCommand.create("v1", "c1", 40, old_start=0)

        moved = command.apply(timeline)
        assert moved.markers[0].frame == 65
        assert moved.markers[0].track_id == "v1"

        restored = command.undo(moved)
        assert restored.markers[0].frame == 25

    def test_delete_clip_removes_scoped_markers_and_undo_restores(self):
        scoped = TimelineMarker(
            marker_id="scoped",
            frame=25,
            clip_id="c1",
            track_id="v1",
        )
        timeline_marker = TimelineMarker(marker_id="timeline", frame=25)
        timeline = _make_timeline(markers=[scoped, timeline_marker])
        command = DeleteClipCommand.create("v1", "c1")

        deleted, applied_command = command.apply(timeline)
        assert [marker.marker_id for marker in deleted.markers] == ["timeline"]

        restored = applied_command.undo(deleted)
        assert {marker.marker_id for marker in restored.markers} == {
            "scoped",
            "timeline",
        }

    def test_batch_delete_removes_scoped_markers(self):
        marker = TimelineMarker(
            marker_id="m1",
            frame=25,
            clip_id="c1",
            track_id="v1",
        )
        timeline = _make_timeline(markers=[marker])
        command = BatchDeleteCommand.create(timeline, ["c1"])

        deleted, applied_command = command.apply(timeline)
        assert deleted.markers == ()

        restored = applied_command.undo(deleted)
        assert restored.markers[0].marker_id == "m1"


class TestMoveMarkerCommand:
    """Verify MoveMarkerCommand."""

    def test_move_marker(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="M1")
        tl = _make_timeline(markers=[m])
        cmd = MoveMarkerCommand.create("m1", 100)
        new_tl = cmd.apply(tl)
        assert new_tl.markers[0].frame == 100

    def test_undo_move(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="M1")
        tl = _make_timeline(markers=[m])
        cmd = MoveMarkerCommand.create("m1", 100)
        new_tl = cmd.apply(tl)
        restored = cmd.undo(new_tl)
        assert restored.markers[0].frame == 50


class TestEditMarkerCommand:
    """Verify EditMarkerCommand."""

    def test_edit_name_and_color(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="Old")
        tl = _make_timeline(markers=[m])
        cmd = EditMarkerCommand(
            command_id="cmd1", label="edit", marker_id="m1",
            new_name="New", new_color="#0000ff",
        )
        new_tl = cmd.apply(tl)
        assert new_tl.markers[0].name == "New"
        assert new_tl.markers[0].color == "#0000ff"

    def test_undo_edit(self):
        m = TimelineMarker(marker_id="m1", frame=50, name="Old", color="#ff0000")
        tl = _make_timeline(markers=[m])
        cmd = EditMarkerCommand(
            command_id="cmd1", label="edit", marker_id="m1",
            new_name="New", new_color="#0000ff",
        )
        new_tl = cmd.apply(tl)
        restored = cmd.undo(new_tl)
        assert restored.markers[0].name == "Old"
        assert restored.markers[0].color == "#ff0000"


class TestMarkerNavigationHelpers:
    """Verify marker navigation logic."""

    def test_sorted_markers(self):
        markers = [
            TimelineMarker(frame=100, name="M3"),
            TimelineMarker(frame=30, name="M1"),
            TimelineMarker(frame=60, name="M2"),
        ]
        tl = _make_timeline(markers=markers)
        frames = sorted(m.frame for m in tl.markers)
        assert frames == [30, 60, 100]

    def test_empty_markers(self):
        tl = _make_timeline()
        assert len(tl.markers) == 0
