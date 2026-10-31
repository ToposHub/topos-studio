"""Tests for importing common subtitle files into timeline cues."""

from __future__ import annotations

from src.features.edit.core.captions.importer import (
    parse_subtitle_text,
    parse_subtitle_file_language,
    partition_subtitle_cues,
)
from src.shared.media.metadata import Rational
from src.shared.media.format_registry import get_media_type, is_media
from src.shared.project.topos_project import MediaAssetRef


def test_srt_is_converted_to_sorted_timeline_frames() -> None:
    content = (
        "2\n00:00:02,000 --> 00:00:03,000\nWorld\n\n"
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    )

    cues = parse_subtitle_text(content, ".srt", Rational(24, 1))

    assert [(cue.start_frame, cue.duration_frames, cue.text) for cue in cues] == [
        (0, 24, "Hello"),
        (48, 24, "World"),
    ]


def test_vtt_accepts_mm_ss_timestamps_and_ignores_header() -> None:
    content = "WEBVTT\n\n00:01.500 --> 00:02.250\nCaption\n"

    cues = parse_subtitle_text(content, "vtt", 24.0)

    assert len(cues) == 1
    assert cues[0].start_frame == 36
    assert cues[0].duration_frames == 18


def test_ass_removes_override_tags_and_restores_line_breaks() -> None:
    content = (
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\an8}Top\\NLine\n"
    )

    cues = parse_subtitle_text(content, ".ass", Rational(24, 1))

    assert [(cue.start_frame, cue.duration_frames, cue.text) for cue in cues] == [
        (24, 36, "Top\nLine"),
    ]


def test_ass_timestamp_keeps_minutes_from_being_counted_twice() -> None:
    content = "  Dialogue: 0,1:02:03.45,1:02:04.45,Default,,0,0,0,,Caption\n"

    cues = parse_subtitle_text(content, ".ass", 24.0)

    assert cues[0].start_frame == round(3_723.45 * 24)
    assert cues[0].duration_frames == round(1_000 / 1000 * 24)


def test_overlapping_cues_are_partitioned_without_losing_time_ranges() -> None:
    cues = parse_subtitle_text(
        "1\n00:00:00,000 --> 00:00:02,000\nA\n\n"
        "2\n00:00:01,000 --> 00:00:03,000\nB\n",
        ".srt", 24.0,
    )

    groups = partition_subtitle_cues(cues)

    assert [[cue.text for cue in group] for group in groups] == [["A"], ["B"]]
    assert [(cue.start_frame, cue.duration_frames)
            for group in groups for cue in group] == [(0, 48), (24, 48)]


def test_ttml_import_preserves_line_breaks_and_parent_offsets() -> None:
    content = (
        '<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="zh-CN">'
        '<body begin="1s"><div><p begin="500ms" dur="1.5s">第一行'
        '<br/>第二行</p></div></body></tt>'
    )

    cues = parse_subtitle_text(content, ".ttml", 24.0)

    assert [(cue.start_frame, cue.duration_frames, cue.text) for cue in cues] == [
        (36, 36, "第一行\n第二行"),
    ]


def test_ttml_language_is_available_for_track_metadata(tmp_path) -> None:
    path = tmp_path / "captions.ttml"
    path.write_text(
        '<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="en-US"/>',
        encoding="utf-8",
    )

    assert parse_subtitle_file_language(path) == "en-US"


def test_subtitle_files_are_registered_as_media_pool_assets() -> None:
    assert is_media("/tmp/captions.srt")
    assert get_media_type("/tmp/captions.vtt") == "subtitle"
    assert get_media_type("/tmp/captions.ass") == "subtitle"


def test_subtitle_asset_ref_can_be_serialized_for_project_archives() -> None:
    ref = MediaAssetRef(
        asset_id="subtitle-1",
        path="/tmp/captions.srt",
        media_type="subtitle",
        display_name="captions.srt",
    )

    restored = MediaAssetRef.from_dict(ref.to_dict())

    assert restored.media_type == "subtitle"
    assert restored.path == ref.path
