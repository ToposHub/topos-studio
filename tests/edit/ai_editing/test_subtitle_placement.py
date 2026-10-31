from src.features.edit.ai_editing.analysis_models import TranscriptSegment, WordSegment
from src.features.edit.ai_editing.subtitle_placement import build_subtitle_placements
from src.features.edit.core.timeline_v2 import ClipInstance


def _clip() -> ClipInstance:
    return ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="audio-1",
        timeline_start=100,
        timeline_duration=80,
        source_start=10,
        source_duration=80,
    )


def test_word_aligned_transcript_maps_source_offset_to_timeline() -> None:
    transcript = (
        TranscriptSegment(
            start_frame=0,
            end_frame=30,
            text="你好 world",
            words=(
                WordSegment(0, 10, "你"),
                WordSegment(10, 20, "好"),
                WordSegment(20, 30, "world"),
            ),
        ),
    )

    placements = build_subtitle_placements(transcript, _clip(), max_chars=100)

    assert len(placements) == 1
    assert placements[0].start_frame == 100  # source_start=10 is mapped to clip start
    assert placements[0].duration_frames == 30
    assert placements[0].text == "你好 world"


def test_unaligned_text_is_split_and_clipped_to_trim() -> None:
    transcript = (
        TranscriptSegment(
            start_frame=0,
            end_frame=120,
            text="一二三四五六七八九十",
        ),
    )

    placements = build_subtitle_placements(transcript, _clip(), max_chars=5)

    assert [p.text for p in placements] == ["一二三四五", "六七八九十"]
    assert placements[0].start_frame == 100
    assert placements[-1].start_frame + placements[-1].duration_frames == 180
    assert all(p.start_frame >= 100 and p.start_frame + p.duration_frames <= 180 for p in placements)
