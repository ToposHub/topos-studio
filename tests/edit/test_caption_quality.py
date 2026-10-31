"""Tests for professional subtitle QC reports."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.edit.core.captions.quality import inspect_subtitle_track
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip


def test_quality_report_points_to_specific_cues() -> None:
    track = EditTrack(
        track_id="s1", track_type="subtitle",
        subtitle_clips=(
            SubtitleClip(
                clip_id="empty", track_id="s1", timeline_start=0,
                timeline_duration=10, text="",
            ),
            SubtitleClip(
                clip_id="fast", track_id="s1", timeline_start=10,
                timeline_duration=1, text="This line is much too long to read comfortably",
            ),
        ),
    )

    report = inspect_subtitle_track(
        track, 24.0, max_chars_per_line=20, min_duration_seconds=0.8,
    )

    assert not report.passed
    assert {issue.code for issue in report.issues} >= {
        "empty_text", "too_short", "line_too_long", "reading_speed",
    }
    assert any(issue.cue_id == "empty" for issue in report.issues)


def test_quality_report_detects_overlapping_cues() -> None:
    # The immutable timeline normally rejects overlaps at construction time;
    # QC must still diagnose legacy/malformed data loaded from older projects.
    track = SimpleNamespace(
        track_id="s1", track_type="subtitle",
        subtitle_clips=(
            SimpleNamespace(clip_id="a", timeline_start=0,
                            timeline_duration=24, text="A", timeline_end=24),
            SimpleNamespace(clip_id="b", timeline_start=12,
                            timeline_duration=24, text="B", timeline_end=36),
        ),
    )

    report = inspect_subtitle_track(track, 24.0)

    assert any(issue.code == "overlap" and issue.cue_id == "b"
               for issue in report.issues)
