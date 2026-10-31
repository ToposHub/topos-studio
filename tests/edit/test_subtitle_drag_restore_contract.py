"""SUB-P0-04 UI contract: failed subtitle move restores the item geometry.

End-to-end (headless TimelineView) verification that when a release-time
subtitle move fails overlap validation, the SubtitleClipItem snaps back to its
pre-drag (start, duration). Without the fix the chip would visually sit on top
of a neighbour until the next full reload.

The test drives the public controller-facing restore helper against a real
SubtitleClipItem registered in the view's items map.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.captions.edit_policy import SubtitleEditPolicy
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _two_cue_sequence() -> TimelineSequence:
    track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-a", track_id="s1",
                timeline_start=0, timeline_duration=72, text="A",
            ),
            SubtitleClip(
                clip_id="sub-b", track_id="s1",
                timeline_start=72, timeline_duration=72, text="B",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )


def test_failed_move_restores_item_geometry(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())

    item = view._items_map.get("sub-a")
    assert isinstance(item, SubtitleClipItem)
    original_start = item._start_frame
    original_duration = item._duration_frames

    # Simulate a live drag that moved the chip into an illegal position
    # (overlapping sub-b which occupies [72, 144)).
    item.restore_geometry(original_start + 80, original_duration)
    assert item._start_frame == original_start + 80  # now overlapping

    # The release-time commit would fail; the controller restores via policy.
    SubtitleEditPolicy.restore_on_failure(item, original_start, original_duration)

    assert item._start_frame == original_start
    assert item._duration_frames == original_duration


def test_failed_trim_restores_item_duration(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())

    item = view._items_map.get("sub-a")
    assert isinstance(item, SubtitleClipItem)
    original_duration = item._duration_frames

    # Simulate a right-trim that grew sub-a to overlap sub-b.
    item.restore_geometry(item._start_frame, original_duration + 50)
    assert item._duration_frames == original_duration + 50

    SubtitleEditPolicy.restore_on_failure(
        item, item._start_frame, original_duration
    )
    assert item._duration_frames == original_duration


def test_restore_is_idempotent_when_already_at_target(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())
    item = view._items_map.get("sub-a")
    assert isinstance(item, SubtitleClipItem)
    start = item._start_frame
    duration = item._duration_frames

    # Already at the target geometry → restore is a no-op (no crash, no change).
    SubtitleEditPolicy.restore_on_failure(item, start, duration)
    assert item._start_frame == start
    assert item._duration_frames == duration
