"""SUB-P1-07 contract: CaptionEditMode placement strategies.

The bug: AddSubtitleClipCommand silently bumped every new cue past the next
overlapping clip, so the insert point never matched the click point and there
was no overwrite/ripple choice. Phase 1.4 introduces three explicit modes
(overwrite/insert/ripple) and auto-selects the created cue.

These tests pin each mode's placement, the displaced-clip undo, and the model
invariants (no overlap, owner track_id consistent) after every mode.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    AddSubtitleClipCommand,
    CaptionEditMode,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _seq(clips) -> TimelineSequence:
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=tuple(clips),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )


def _cue(cid: str, start: int, dur: int, text: str = "x") -> SubtitleClip:
    return SubtitleClip(
        clip_id=cid, track_id="s1",
        timeline_start=start, timeline_duration=dur, text=text,
    )


def _apply(seq, **kw):
    nt, cmd = AddSubtitleClipCommand.create("s1", **kw).apply(seq)
    return nt, cmd


# ── INSERT ─────────────────────────────────────────────────────────────────


def test_insert_no_overlap_places_at_request() -> None:
    seq = _seq([_cue("a", 0, 50)])
    nt, _ = _apply(seq, timeline_start=100, duration=30, mode=CaptionEditMode.INSERT)
    new = [c for c in nt.subtitle_tracks[0].subtitle_clips if c.clip_id != "a"]
    assert new[0].timeline_start == 100


def test_insert_overlap_places_after_overlap() -> None:
    seq = _seq([_cue("a", 0, 100)])
    nt, cmd = _apply(seq, timeline_start=50, duration=30, mode=CaptionEditMode.INSERT)
    placed = nt.get_subtitle_clip_by_id(cmd._created_clip_id)
    assert placed.timeline_start == 100  # pushed past a's end


# ── OVERWRITE ──────────────────────────────────────────────────────────────


def test_overwrite_no_overlap_places_at_request() -> None:
    seq = _seq([_cue("a", 0, 50)])
    nt, cmd = _apply(seq, timeline_start=100, duration=30, mode=CaptionEditMode.OVERWRITE)
    placed = nt.get_subtitle_clip_by_id(cmd._created_clip_id)
    assert placed.timeline_start == 100
    assert cmd._displaced_clips == ()


def test_overwrite_right_edge_trims_existing() -> None:
    """new cue [80,130) overlaps a[0,100) on a's right → a trimmed to [0,80)."""
    seq = _seq([_cue("a", 0, 100)])
    nt, cmd = _apply(seq, timeline_start=80, duration=50, mode=CaptionEditMode.OVERWRITE)
    a = nt.get_subtitle_clip_by_id("a")
    assert a.timeline_duration == 80
    assert a.timeline_start == 0
    assert len(cmd._displaced_clips) == 1


def test_overwrite_left_edge_advances_existing() -> None:
    """new cue [0,30) overlaps a[20,100) on a's left → a advanced to [30,100)."""
    seq = _seq([_cue("a", 20, 80)])
    nt, _ = _apply(seq, timeline_start=0, duration=30, mode=CaptionEditMode.OVERWRITE)
    a = nt.get_subtitle_clip_by_id("a")
    assert a.timeline_start == 30
    assert a.timeline_duration == 70  # end stays 100


def test_overwrite_fully_covered_removes_existing() -> None:
    """new cue [0,100) fully covers a[20,50) → a removed."""
    seq = _seq([_cue("a", 20, 30)])
    nt, cmd = _apply(seq, timeline_start=0, duration=100, mode=CaptionEditMode.OVERWRITE)
    assert nt.get_subtitle_clip_by_id("a") is None
    assert len(cmd._displaced_clips) == 1


def test_overwrite_undo_restores_displaced() -> None:
    seq = _seq([_cue("a", 0, 100)])
    nt, cmd = _apply(seq, timeline_start=80, duration=50, mode=CaptionEditMode.OVERWRITE)
    assert nt.get_subtitle_clip_by_id("a").timeline_duration == 80
    restored = cmd.undo(nt)
    a = restored.get_subtitle_clip_by_id("a")
    assert a.timeline_duration == 100
    assert a.timeline_start == 0
    # The new cue is gone too.
    assert restored.get_subtitle_clip_by_id(cmd._created_clip_id) is None


# ── RIPPLE ─────────────────────────────────────────────────────────────────


def test_ripple_shifts_clips_at_and_after_insertion() -> None:
    """Insert at 50: clips with start>=50 shift by duration; earlier ones stay."""
    seq = _seq([_cue("a", 0, 40), _cue("b", 50, 40), _cue("c", 100, 40)])
    nt, cmd = _apply(seq, timeline_start=50, duration=30, mode=CaptionEditMode.RIPPLE)
    a = nt.get_subtitle_clip_by_id("a")
    b = nt.get_subtitle_clip_by_id("b")
    c = nt.get_subtitle_clip_by_id("c")
    new = nt.get_subtitle_clip_by_id(cmd._created_clip_id)
    assert a.timeline_start == 0          # before insertion → unmoved
    assert b.timeline_start == 80         # 50 + 30
    assert c.timeline_start == 130        # 100 + 30
    assert new.timeline_start == 50
    assert len(cmd._displaced_clips) == 2  # b and c shifted


def test_ripple_undo_restores_all() -> None:
    seq = _seq([_cue("a", 0, 40), _cue("b", 50, 40)])
    nt, cmd = _apply(seq, timeline_start=50, duration=30, mode=CaptionEditMode.RIPPLE)
    restored = cmd.undo(nt)
    assert restored.get_subtitle_clip_by_id("b").timeline_start == 50
    assert restored.get_subtitle_clip_by_id(cmd._created_clip_id) is None


# ── Invariants ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [
    CaptionEditMode.OVERWRITE, CaptionEditMode.INSERT, CaptionEditMode.RIPPLE,
])
def test_every_mode_leaves_no_overlap_and_consistent_owner(mode) -> None:
    """Re-constructing the track from the result must succeed (invariants hold)."""
    seq = _seq([_cue("a", 0, 100), _cue("b", 100, 100)])
    nt, _ = _apply(seq, timeline_start=50, duration=30, mode=mode)
    track = nt.subtitle_tracks[0]
    # Rebuild enforces overlap + owner-track_id + uniqueness; if it raises,
    # the mode left the model inconsistent.
    EditTrack(
        track_id=track.track_id, name=track.name, track_type="subtitle",
        index=track.index, subtitle_clips=track.subtitle_clips,
    )
    for sub in track.subtitle_clips:
        assert sub.track_id == track.track_id


# ── Round-trip ─────────────────────────────────────────────────────────────


def test_to_dict_from_dict_preserves_mode() -> None:
    cmd = AddSubtitleClipCommand.create("s1", 100, mode=CaptionEditMode.RIPPLE)
    restored = AddSubtitleClipCommand.from_dict(cmd.to_dict())
    assert restored.mode == CaptionEditMode.RIPPLE


def test_from_dict_defaults_legacy_to_insert() -> None:
    """Old persisted commands without a mode field deserialize as INSERT
    (the pre-P1.7 behaviour), not the new OVERWRITE default."""
    legacy = AddSubtitleClipCommand.from_dict({
        "command_id": "x", "track_id": "s1", "timeline_start": 0,
    })
    assert legacy.mode == CaptionEditMode.INSERT


# ── Auto-select after add ──────────────────────────────────────────────────


def test_default_mode_is_overwrite() -> None:
    """create() with no mode defaults to OVERWRITE (the plan's named default)."""
    cmd = AddSubtitleClipCommand.create("s1", 0)
    assert cmd.mode == CaptionEditMode.OVERWRITE


def test_created_clip_id_is_available_for_auto_select(qapp) -> None:
    """After an add commit the new cue's id is exposed so the controller can
    auto-select it (SUB-P1-07). Driven through the unified TimelineView path."""
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    view.load_timeline(_seq([_cue("a", 0, 50)]))

    cmd = AddSubtitleClipCommand.create("s1", 100, duration=30)
    view.execute_command(cmd)

    created_id = cmd._created_clip_id
    assert created_id is not None
    item = view._items_map.get(created_id)
    # The created cue's item exists and is selectable (auto-select target).
    assert item is not None
    item.setSelected(True)
    assert item.isSelected() is True
