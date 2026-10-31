"""SUB-P0-05 contract: subtitle changes invalidate the preview signature.

The bug: ``_build_timeline_preview_signature`` read ``track.clips`` (empty on
subtitle tracks) and ``clip.params`` (no such field on SubtitleClip), so the
subtitle signature was always empty. Edits to subtitle text/style/transform/
timing therefore did not invalidate the "no-change → skip re-render" path.

These tests assert the signature changes for every visual subtitle edit and
stays stable for unrelated (e.g. audio) changes.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.edit_page import EditPage

pytestmark = pytest.mark.edit_core


def _sub(
    clip_id: str = "sub-1",
    text: str = "hello",
    start: int = 0,
    duration: int = 72,
    **overrides,
) -> SubtitleClip:
    fields = dict(
        clip_id=clip_id,
        track_id="s1",
        timeline_start=start,
        timeline_duration=duration,
        text=text,
    )
    fields.update(overrides)
    return SubtitleClip(**fields)


def _seq(subtitle_clips, *, audio_volume=1.0) -> TimelineSequence:
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=tuple(subtitle_clips),
    )
    audio_track = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        clips=(
            ClipInstance(
                clip_id="a-clip", asset_id="asset-a", track_id="a1",
                timeline_start=0, timeline_duration=120,
                source_start=0, source_duration=120,
            ),
        ),
        volume=audio_volume,
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        audio_tracks=(audio_track,),
        subtitle_tracks=(sub_track,),
    )


def _signature(timeline: TimelineSequence):
    page = EditPage()
    return page._build_timeline_preview_signature(timeline)


def test_subtitle_text_change_changes_signature(qapp) -> None:
    base = _signature(_seq([_sub(text="A")]))
    changed = _signature(_seq([_sub(text="B")]))
    assert base != changed


def test_subtitle_timing_change_changes_signature(qapp) -> None:
    base = _signature(_seq([_sub(start=0, duration=72)]))
    changed = _signature(_seq([_sub(start=10, duration=50)]))
    assert base != changed


def test_subtitle_transform_change_changes_signature(qapp) -> None:
    base = _signature(_seq([_sub(translate_x=0.0)]))
    changed = _signature(_seq([_sub(translate_x=100.0)]))
    assert base != changed


def test_subtitle_style_change_changes_signature(qapp) -> None:
    base = _signature(_seq([_sub(fill_r=1.0)]))
    changed = _signature(_seq([_sub(fill_r=0.2)]))
    assert base != changed


def test_subtitle_enabled_change_changes_signature(qapp) -> None:
    base = _signature(_seq([_sub(is_enabled=True)]))
    changed = _signature(_seq([_sub(is_enabled=False)]))
    assert base != changed


def test_track_visibility_change_changes_signature(qapp) -> None:
    page = EditPage()
    seq_on = _seq([_sub()])
    # Toggle the subtitle track visibility via the public mutator.
    sub_off = seq_on.subtitle_tracks[0].with_visibility(False)
    seq_off = TimelineSequence(
        timeline_id=seq_on.timeline_id, name=seq_on.name,
        width=seq_on.width, height=seq_on.height, fps=seq_on.fps,
        audio_tracks=seq_on.audio_tracks, subtitle_tracks=(sub_off,),
    )
    assert page._build_timeline_preview_signature(seq_on) != \
        page._build_timeline_preview_signature(seq_off)


def test_audio_only_change_does_not_change_subtitle_signature(qapp) -> None:
    # Changing an audio track volume must NOT be attributed to subtitle changes.
    # The overall signature differs (audio is in video/audio sections), but the
    # subtitle sub-signature portion must be identical.
    page = EditPage()
    sig_a = page._build_timeline_preview_signature(_seq([_sub()], audio_volume=1.0))
    sig_b = page._build_timeline_preview_signature(_seq([_sub()], audio_volume=0.5))
    # The subtitle tracks entry (index 4) is unchanged.
    assert sig_a[4] == sig_b[4]


def test_identical_timelines_have_identical_signatures(qapp) -> None:
    assert _signature(_seq([_sub(text="same")])) == _signature(_seq([_sub(text="same")]))


def test_old_bug_empty_subtitle_signature_is_fixed(qapp) -> None:
    """Regression: previously the subtitle section was always () regardless of
    cues because it read track.clips/clip.params. Now it must reflect content."""
    empty = _signature(_seq([]))
    populated = _signature(_seq([_sub(text="x")]))
    assert empty != populated
