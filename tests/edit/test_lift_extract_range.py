"""P1-03: range Lift / Extract semantics.

Contract (from the professional-gap audit):
- Lift (ripple=False): removes the In/Out range content, keeps the gap.
- Extract (ripple=True): removes the content and closes the gap by shifting
  later clips left by exactly the range duration (internal gaps inside the
  range close too).
- Clips straddling a range boundary are split first, so only the in-range
  half is removed and out-of-range source frames survive.
- Linked A/V stay aligned: every scoped track gets the same trims and the
  same ripple shift.
- Everything is one MacroCommand — atomic and undoable, including
  clip-scoped markers.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import CommandStack, MacroCommand
from src.features.edit.core.timeline_ops import build_range_delete_commands
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineMarker,
    TimelineSequence,
)


def _clip(
    clip_id: str,
    track_id: str,
    start: int,
    duration: int,
    linked: str | None = None,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset-1",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=start,  # identity mapping: source frame == timeline frame
        source_duration=duration,
        linked_clip_id=linked,
    )


def _sequence(
    video_clips: tuple[ClipInstance, ...] = (),
    audio_clips: tuple[ClipInstance, ...] = (),
    markers: tuple[TimelineMarker, ...] = (),
    lock_v1: bool = False,
) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-lift",
        name="Lift",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=video_clips,
                is_locked=lock_v1,
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1",
                name="A1",
                track_type="audio",
                index=0,
                clips=audio_clips,
            ),
        ),
        markers=markers,
    )


def _run(sequence, commands):
    """Apply commands like CommandStack does and return the macro result."""
    macro = MacroCommand.create("range op", commands)
    stack = CommandStack()
    return stack.execute(macro, sequence), stack, macro


def _clips_on(sequence: TimelineSequence, track_id: str = "v1"):
    return {
        c.clip_id: c for c in sequence.get_track_by_id(track_id).clips
    }


class TestLift:
    def test_clip_fully_inside_is_removed_gap_kept(self):
        seq = _sequence(video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50), _clip("c", "v1", 100, 50)))

        commands = build_range_delete_commands(seq, ["v1"], 50, 100, ripple=False)
        new_seq, _, _ = _run(seq, commands)

        clips = _clips_on(new_seq)
        assert set(clips) == {"a", "c"}
        assert clips["a"].timeline_start == 0
        assert clips["c"].timeline_start == 100  # gap kept

    def test_clip_straddling_start_is_trimmed_at_tail(self):
        seq = _sequence(video_clips=(_clip("long", "v1", 0, 100),))

        commands = build_range_delete_commands(seq, ["v1"], 40, 80, ripple=False)
        new_seq, _, _ = _run(seq, commands)

        clips = _clips_on(new_seq)
        # Two survivors: [0,40) from the original head and [80,100) tail.
        survivors = sorted(clips.values(), key=lambda c: c.timeline_start)
        assert len(survivors) == 2
        head, tail = survivors
        assert (head.timeline_start, head.timeline_end) == (0, 40)
        assert (tail.timeline_start, tail.timeline_end) == (80, 100)
        # Source frames outside the range survive with identity mapping.
        assert head.source_duration == 40
        assert tail.source_start == 80

    def test_range_boundaries_align_with_clip_edges_no_split(self):
        seq = _sequence(video_clips=(_clip("a", "v1", 0, 40), _clip("b", "v1", 40, 40), _clip("c", "v1", 80, 40)))

        commands = build_range_delete_commands(seq, ["v1"], 40, 80, ripple=False)
        new_seq, _, _ = _run(seq, commands)

        assert set(_clips_on(new_seq)) == {"a", "c"}

    def test_empty_range_region_produces_no_commands_for_lift(self):
        seq = _sequence(video_clips=(_clip("a", "v1", 0, 50),))

        commands = build_range_delete_commands(seq, ["v1"], 200, 260, ripple=False)

        assert commands == []

    def test_locked_track_is_skipped(self):
        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 100),),
            lock_v1=True,
        )

        commands = build_range_delete_commands(seq, ["v1"], 0, 50, ripple=False)

        assert commands == []


class TestExtract:
    def test_later_clips_shift_left_by_range_duration(self):
        seq = _sequence(video_clips=(
            _clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50), _clip("c", "v1", 100, 50),
        ))

        commands = build_range_delete_commands(seq, ["v1"], 50, 100, ripple=True)
        new_seq, _, _ = _run(seq, commands)

        clips = _clips_on(new_seq)
        assert set(clips) == {"a", "c"}
        assert clips["c"].timeline_start == 50  # 100 - (100-50)

    def test_internal_gaps_close_completely(self):
        # Range [0, 100) with a 20-frame gap between the two in-range clips.
        # Sequential per-clip ripple deletes would only close 80 of the 100
        # frames; the explicit range shift must close all of it.
        seq = _sequence(video_clips=(
            _clip("a", "v1", 0, 40), _clip("b", "v1", 60, 40), _clip("c", "v1", 100, 50),
        ))

        commands = build_range_delete_commands(seq, ["v1"], 0, 100, ripple=True)
        new_seq, _, _ = _run(seq, commands)

        clips = _clips_on(new_seq)
        assert set(clips) == {"c"}
        assert clips["c"].timeline_start == 0  # 100 - 100

    def test_extract_of_empty_leading_region_shifts_content(self):
        seq = _sequence(video_clips=(_clip("a", "v1", 100, 50),))

        commands = build_range_delete_commands(seq, ["v1"], 0, 40, ripple=True)
        new_seq, _, _ = _run(seq, commands)

        clips = _clips_on(new_seq)
        assert clips["a"].timeline_start == 60

    def test_straddling_clip_extract_trims_and_shifts_tail(self):
        seq = _sequence(video_clips=(_clip("long", "v1", 0, 100), _clip("after", "v1", 100, 50)))

        commands = build_range_delete_commands(seq, ["v1"], 40, 80, ripple=True)
        new_seq, _, _ = _run(seq, commands)

        survivors = sorted(_clips_on(new_seq).values(), key=lambda c: c.timeline_start)
        assert len(survivors) == 3
        head, tail_half, after = survivors
        assert (head.timeline_start, head.timeline_end) == (0, 40)
        assert (tail_half.timeline_start, tail_half.timeline_end) == (40, 60)
        assert (after.timeline_start, after.timeline_end) == (60, 110)
        # Source frames of the surviving tail half are preserved (identity
        # mapping): it played source frames [80, 100).
        assert tail_half.source_start == 80


class TestLinkedAV:
    def test_linked_pair_extracts_in_sync(self):
        seq = _sequence(
            video_clips=(_clip("v-a", "v1", 0, 50), _clip("v-b", "v1", 50, 50, linked="a-b")),
            audio_clips=(_clip("a-a", "a1", 0, 50, linked="v-a"), _clip("a-b", "a1", 50, 50, linked="v-b")),
        )

        commands = build_range_delete_commands(
            seq, ["v1", "a1"], 50, 100, ripple=True
        )
        new_seq, _, _ = _run(seq, commands)

        v_clips = _clips_on(new_seq, "v1")
        a_clips = _clips_on(new_seq, "a1")
        assert set(v_clips) == {"v-a"} and set(a_clips) == {"a-a"}
        assert v_clips["v-a"].timeline_start == a_clips["a-a"].timeline_start == 0
        # Deleting the linked partners clears the reciprocal link (the
        # batch delete unlinks one-way residue) — never a stale reference.
        assert v_clips["v-a"].linked_clip_id is None


class TestUndoAndMarkers:
    def test_undo_restores_original_clips_and_sources(self):
        seq = _sequence(video_clips=(_clip("long", "v1", 0, 100), _clip("after", "v1", 100, 50)))

        commands = build_range_delete_commands(seq, ["v1"], 40, 80, ripple=True)
        new_seq, stack, _ = _run(seq, commands)

        assert len(_clips_on(new_seq)) == 3

        restored = stack.undo(new_seq)

        assert _clips_on(restored) == _clips_on(seq)

    def test_clip_scoped_markers_follow_their_clips(self):
        marker_on_after = TimelineMarker(frame=120, name="m", clip_id="after", track_id="v1")
        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("after", "v1", 100, 50)),
            markers=(marker_on_after,),
        )

        commands = build_range_delete_commands(seq, ["v1"], 50, 100, ripple=True)
        new_seq, _, _ = _run(seq, commands)

        # "after" moved from 50 to 0 → its marker moved by the same delta.
        moved = [m for m in new_seq.markers if m.clip_id == "after"]
        assert len(moved) == 1
        assert moved[0].frame == 70  # 120 - 50

    def test_marker_inside_deleted_clip_is_removed(self):
        marker_on_b = TimelineMarker(frame=60, name="m-b", clip_id="b", track_id="v1")
        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50)),
            markers=(marker_on_b,),
        )

        commands = build_range_delete_commands(seq, ["v1"], 50, 100, ripple=False)
        new_seq, _, _ = _run(seq, commands)

        assert [m for m in new_seq.markers if m.clip_id == "b"] == []

    def test_timeline_scoped_marker_is_untouched_by_lift(self):
        ruler_marker = TimelineMarker(frame=60, name="ruler")
        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50)),
            markers=(ruler_marker,),
        )

        commands = build_range_delete_commands(seq, ["v1"], 50, 100, ripple=False)
        new_seq, _, _ = _run(seq, commands)

        assert [m for m in new_seq.markers if m.clip_id is None] == [ruler_marker]


class TestValidation:
    def test_invalid_range_raises(self):
        seq = _sequence()
        with pytest.raises(ValueError):
            build_range_delete_commands(seq, ["v1"], 100, 100)
        with pytest.raises(ValueError):
            build_range_delete_commands(seq, ["v1"], 200, 100)

    def test_unknown_track_is_skipped(self):
        seq = _sequence(video_clips=(_clip("a", "v1", 0, 50),))

        commands = build_range_delete_commands(seq, ["ghost"], 0, 50, ripple=False)

        assert commands == []


class TestTimelineViewDispatcher:
    def test_range_lift_via_delete_key(self, qapp, qtbot):
        from src.features.edit.ui.timeline_view import TimelineView

        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50), _clip("c", "v1", 100, 50)),
        ).with_range(50, 99)  # In/Out inclusive → frames [50, 100)

        view = TimelineView()
        view.load_timeline(seq)
        view._clear_selection()

        view.handle_delete_request(ripple=False)

        clips = _clips_on(view._timeline)
        assert set(clips) == {"a", "c"}
        assert clips["c"].timeline_start == 100  # gap kept

    def test_range_extract_via_dispatcher(self, qapp, qtbot):
        from src.features.edit.ui.timeline_view import TimelineView

        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50), _clip("c", "v1", 100, 50)),
        ).with_range(50, 99)

        view = TimelineView()
        view.load_timeline(seq)
        view._clear_selection()

        view.handle_delete_request(ripple=True)

        clips = _clips_on(view._timeline)
        assert set(clips) == {"a", "c"}
        assert clips["c"].timeline_start == 50

    def test_selection_wins_over_range(self, qapp, qtbot):
        from src.features.edit.ui.timeline_view import TimelineView

        seq = _sequence(
            video_clips=(_clip("a", "v1", 0, 50), _clip("b", "v1", 50, 50), _clip("c", "v1", 100, 50)),
        ).with_range(50, 99)

        view = TimelineView()
        view.load_timeline(seq)
        view.set_selection(
            view._selection.with_clip_selected(
                "a", "v1", seq.timeline_id, replace=True
            )
        )

        view.handle_delete_request(ripple=False)

        # Selected clip deleted, range ignored.
        assert set(_clips_on(view._timeline)) == {"b", "c"}

    def test_no_range_no_selection_is_noop(self, qapp, qtbot):
        from src.features.edit.ui.timeline_view import TimelineView

        seq = _sequence(video_clips=(_clip("a", "v1", 0, 50),))
        view = TimelineView()
        view.load_timeline(seq)
        view._clear_selection()

        view.handle_delete_request(ripple=False)

        assert set(_clips_on(view._timeline)) == {"a"}
