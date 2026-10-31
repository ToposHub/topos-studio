"""SUB-P2-01 contract: split/merge carry caption style + metadata to the halves.

Phase 2A-5 fixes a known limitation: Split/MergeSubtitleClipsCommand used to
build their AddSubtitleClipCommand children with only text/timing, so
``style_override`` and ``caption_metadata`` (speaker/status/note/…) were
dropped. The fix adds ``AddSubtitleClipCommand.from_clip`` which stamps the new
cue from a template clip via ``SubtitleClip._replace``, copying every field.

These tests pin that:
1. Split halves inherit the original cue's style_override + caption_metadata.
2. Merge result inherits the chosen primary cue's style + metadata.
3. Each new cue gets a fresh clip_id (uniqueness invariant holds).
4. Undo restores the original clips exactly.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionStatus,
    CaptionStyle,
)
from src.features.edit.core.captions import commands as _cap_cmds
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


def _cue(cid: str, start: int, dur: int, text: str = "x",
         style: CaptionStyle = None, meta: CaptionCueMetadata = None) -> SubtitleClip:
    return SubtitleClip(
        clip_id=cid, track_id="s1",
        timeline_start=start, timeline_duration=dur, text=text,
        style_override=style,
        caption_metadata=meta or CaptionCueMetadata(),
    )


# ──────────────────────────────────────────────────────────────────────────
# Split
# ──────────────────────────────────────────────────────────────────────────

def test_split_carries_style_and_metadata_to_halves() -> None:
    style = CaptionStyle(font_family="Noto", font_size=140, fill_r=0.2)
    meta = CaptionCueMetadata(speaker="Alice", status=CaptionStatus.APPROVED,
                              note="reviewed")
    seq = _seq([_cue("c1", 0, 100, "hello", style=style, meta=meta)])

    cmd = _cap_cmds.SplitSubtitleClipCommand.create("c1", split_frame=40)
    nt, _ = cmd.apply(seq)

    clips = nt.subtitle_tracks[0].subtitle_clips
    assert len(clips) == 2
    left, right = sorted(clips, key=lambda c: c.timeline_start)
    # timing
    assert left.timeline_start == 0 and left.timeline_duration == 40
    assert right.timeline_start == 40 and right.timeline_duration == 60
    # text: left keeps original, right starts empty
    assert left.text == "hello"
    assert right.text == ""
    # style_override carried to BOTH halves
    assert left.style_override == style
    assert right.style_override == style
    # caption_metadata carried to BOTH halves
    assert left.caption_metadata.speaker == "Alice"
    assert right.caption_metadata.speaker == "Alice"
    assert left.caption_metadata.status == CaptionStatus.APPROVED
    assert right.caption_metadata.note == "reviewed"


def test_split_halves_get_fresh_ids() -> None:
    seq = _seq([_cue("c1", 0, 100)])
    cmd = _cap_cmds.SplitSubtitleClipCommand.create("c1", split_frame=50)
    nt, _ = cmd.apply(seq)
    ids = {c.clip_id for c in nt.subtitle_tracks[0].subtitle_clips}
    assert "c1" not in ids  # original deleted
    assert len(ids) == 2    # two distinct new ids


def test_split_undo_restores_original() -> None:
    style = CaptionStyle(font_family="Noto", font_size=140)
    meta = CaptionCueMetadata(speaker="Alice")
    seq = _seq([_cue("c1", 0, 100, "hi", style=style, meta=meta)])
    cmd = _cap_cmds.SplitSubtitleClipCommand.create("c1", split_frame=50)
    nt, _ = cmd.apply(seq)
    restored = cmd.undo(nt)
    clips = restored.subtitle_tracks[0].subtitle_clips
    assert len(clips) == 1
    assert clips[0].clip_id == "c1"
    assert clips[0].text == "hi"
    assert clips[0].style_override == style
    assert clips[0].caption_metadata.speaker == "Alice"


# ──────────────────────────────────────────────────────────────────────────
# Merge
# ──────────────────────────────────────────────────────────────────────────

def test_merge_carries_primary_style_and_metadata() -> None:
    style_first = CaptionStyle(font_family="Noto", font_size=140)
    style_second = CaptionStyle(font_family="Arial", font_size=100)
    meta_first = CaptionCueMetadata(speaker="Alice")
    meta_second = CaptionCueMetadata(speaker="Bob")
    seq = _seq([
        _cue("a", 0, 50, "hello", style=style_first, meta=meta_first),
        _cue("b", 50, 50, "world", style=style_second, meta=meta_second),
    ])

    cmd = _cap_cmds.MergeSubtitleClipsCommand.create("a", "b", primary="first")
    nt, _ = cmd.apply(seq)
    clips = nt.subtitle_tracks[0].subtitle_clips
    assert len(clips) == 1
    merged = clips[0]
    # timing spans both
    assert merged.timeline_start == 0
    assert merged.timeline_duration == 100
    # text concatenated
    assert merged.text == "hello world"
    # inherits the PRIMARY (first) cue's style + metadata
    assert merged.style_override == style_first
    assert merged.caption_metadata.speaker == "Alice"


def test_merge_primary_second_inherits_second_style() -> None:
    style_second = CaptionStyle(font_family="Arial")
    seq = _seq([
        _cue("a", 0, 50, "hello"),
        _cue("b", 50, 50, "world", style=style_second),
    ])
    cmd = _cap_cmds.MergeSubtitleClipsCommand.create("a", "b", primary="second")
    nt, _ = cmd.apply(seq)
    merged = nt.subtitle_tracks[0].subtitle_clips[0]
    assert merged.style_override == style_second


def test_merge_undo_restores_both_originals() -> None:
    seq = _seq([
        _cue("a", 0, 50, "hello"),
        _cue("b", 50, 50, "world"),
    ])
    cmd = _cap_cmds.MergeSubtitleClipsCommand.create("a", "b")
    nt, _ = cmd.apply(seq)
    restored = cmd.undo(nt)
    clips = restored.subtitle_tracks[0].subtitle_clips
    ids = {c.clip_id for c in clips}
    assert ids == {"a", "b"}
    by_id = {c.clip_id: c for c in clips}
    assert by_id["a"].text == "hello"
    assert by_id["b"].text == "world"


def test_merge_rejects_different_tracks() -> None:
    t1 = EditTrack(track_id="s1", track_type="subtitle", index=0,
                   subtitle_clips=(SubtitleClip(clip_id="a", track_id="s1",
                                                timeline_start=0,
                                                timeline_duration=50),))
    t2 = EditTrack(track_id="s2", track_type="subtitle", index=1,
                   subtitle_clips=(SubtitleClip(clip_id="b", track_id="s2",
                                                timeline_start=50,
                                                timeline_duration=50),))
    seq = TimelineSequence(timeline_id="seq", name="Seq", fps=24,
                           subtitle_tracks=(t1, t2))
    cmd = _cap_cmds.MergeSubtitleClipsCommand.create("a", "b")
    with pytest.raises(ValueError, match="different tracks"):
        cmd.apply(seq)
