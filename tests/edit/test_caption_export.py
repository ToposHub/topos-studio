"""Tests for standard subtitle track exports."""

from __future__ import annotations

from pathlib import Path

from src.features.edit.core.captions.export import (
    render_subtitle_track,
    write_subtitle_track,
)
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip
from src.shared.media.metadata import Rational


def _track() -> EditTrack:
    return EditTrack(
        track_id="sub-1",
        name="English",
        track_type="subtitle",
        subtitle_clips=(
            SubtitleClip(
                clip_id="cue-2",
                track_id="sub-1",
                timeline_start=48,
                timeline_duration=48,
                text="Second line\r\nwith a break",
            ),
            SubtitleClip(
                clip_id="cue-1",
                track_id="sub-1",
                timeline_start=0,
                timeline_duration=24,
                text="Hello",
            ),
        ),
    )


def test_srt_uses_sorted_cues_and_millisecond_timebase() -> None:
    output = render_subtitle_track(_track(), "srt", Rational(24, 1))

    assert output == (
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nSecond line\nwith a break\n"
    )


def test_vtt_has_header_and_dot_milliseconds() -> None:
    output = render_subtitle_track(_track(), ".vtt", 24.0)

    assert output.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n")
    assert "Second line\nwith a break" in output


def test_ass_uses_centiseconds_and_escapes_text_controls() -> None:
    track = EditTrack(
        track_id="sub-1",
        track_type="subtitle",
        subtitle_clips=(SubtitleClip(
            clip_id="cue-1",
            track_id="sub-1",
            timeline_start=1,
            timeline_duration=23,
            text="{speaker}\nHello",
        ),),
    )

    output = render_subtitle_track(track, "ass", Rational(24, 1))

    assert "Dialogue: 0,0:00:00.04,0:00:01.00,Default,,0,0,0,,\\{speaker\\}\\NHello" in output


def test_write_subtitle_track_writes_utf8(tmp_path: Path) -> None:
    destination = tmp_path / "captions.vtt"

    result = write_subtitle_track(_track(), "vtt", Rational(24, 1), destination)

    assert result == destination
    assert destination.read_text(encoding="utf-8").startswith("WEBVTT")


def test_ttml_exports_language_and_line_breaks() -> None:
    from src.features.edit.core.captions.model import CaptionTrackMetadata

    track = _track().with_caption_metadata(CaptionTrackMetadata(language="zh-CN"))

    output = render_subtitle_track(track, "ttml", Rational(24, 1))

    assert 'xml:lang="zh-CN"' in output
    assert "Second line<br/>with a break" in output
    assert '<p begin="00:00:00.000" end="00:00:01.000">' in output
