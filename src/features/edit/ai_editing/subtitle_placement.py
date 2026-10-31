"""Build timeline subtitle cues from timestamped transcript evidence.

The transcript provider reports frame ranges relative to the source asset.  A
timeline clip may use only a trimmed portion of that asset and may be placed at
an arbitrary timeline position, so subtitle creation must go through the
clip's immutable source/timeline mapping instead of doing seconds arithmetic in
the UI layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.features.edit.ai_editing.analysis_models import TranscriptSegment, WordSegment
from src.features.edit.core.captions.model import CaptionWordTiming
from src.features.edit.core.timeline_v2 import ClipInstance


@dataclass(frozen=True)
class SubtitlePlacement:
    """One subtitle cue in timeline-frame coordinates."""

    start_frame: int
    duration_frames: int
    text: str
    word_timings: tuple[CaptionWordTiming, ...] = ()


def _join_words(words: Sequence[WordSegment]) -> str:
    """Join words without inserting spaces between CJK characters."""
    result = ""
    for word in words:
        token = word.text.strip()
        if not token:
            continue
        # CJK↔CJK stays compact; Latin/numeric tokens get a separating space,
        # including a CJK↔Latin boundary for readable mixed-language captions.
        if result and (
            (result[-1].isascii() and result[-1].isalnum())
            or (token[0].isascii() and token[0].isalnum())
        ):
            result += " "
        result += token
    return result.strip()


def _source_to_timeline_range(
    clip: ClipInstance, local_start: int, local_end: int
) -> tuple[int, int] | None:
    """Map an asset-relative half-open range onto the clip's timeline range."""
    if local_end <= local_start:
        return None

    source_start = max(clip.source_start, clip.source_start + int(local_start))
    source_end_exclusive = min(
        clip.source_end + 1, clip.source_start + int(local_end)
    )
    if source_end_exclusive <= source_start:
        return None

    try:
        timeline_start = clip.source_to_timeline_frame(source_start)
        timeline_last = clip.source_to_timeline_frame(source_end_exclusive - 1)
    except ValueError:
        return None

    timeline_start = max(clip.timeline_start, min(clip.timeline_end - 1, timeline_start))
    timeline_end = max(timeline_start + 1, timeline_last + 1)
    timeline_end = min(clip.timeline_end, timeline_end)
    if timeline_end <= timeline_start:
        return None
    return timeline_start, timeline_end


def _split_text_segment(
    segment: TranscriptSegment, max_chars: int
) -> list[tuple[int, int, str]]:
    """Split a segment with no word alignment into proportional cue ranges."""
    text = " ".join(segment.text.split())
    if not text or segment.end_frame <= segment.start_frame:
        return []
    chunks = [text[i:i + max_chars].strip() for i in range(0, len(text), max_chars)]
    chunks = [chunk for chunk in chunks if chunk]
    if not chunks:
        return []
    total = segment.end_frame - segment.start_frame
    result: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        start = segment.start_frame + (total * index) // len(chunks)
        end = segment.start_frame + (total * (index + 1)) // len(chunks)
        if index == len(chunks) - 1:
            end = segment.end_frame
        if end > start:
            result.append((start, end, chunk))
    return result


def build_subtitle_placements(
    transcript: Sequence[TranscriptSegment],
    clip: ClipInstance,
    *,
    max_chars: int = 18,
    max_duration_frames: int = 90,
) -> tuple[SubtitlePlacement, ...]:
    """Convert transcript evidence into non-empty, timeline-aligned cues.

    Word-aligned transcripts are grouped by readable line length and maximum
    duration.  When alignment is unavailable, segment text is split and its
    duration is divided proportionally.  All ranges are clamped to the source
    trim and the timeline clip, so an ASR result that extends beyond a trimmed
    clip cannot create subtitles outside that clip.
    """
    max_chars = max(1, int(max_chars))
    max_duration_frames = max(1, int(max_duration_frames))
    raw: list[tuple[int, int, str, tuple[WordSegment, ...]]] = []

    for segment in transcript:
        words = tuple(word for word in segment.words if word.text.strip())
        if not words:
            raw.extend((start, end, text, ())
                       for start, end, text in _split_text_segment(segment, max_chars))
            continue

        group: list[WordSegment] = []
        for word in words:
            candidate = _join_words((*group, word))
            group_start = group[0].start_frame if group else word.start_frame
            group_end = max(word.end_frame, group_start + 1)
            should_flush = bool(group) and (
                len(candidate) > max_chars
                or group_end - group_start > max_duration_frames
            )
            if should_flush:
                text = _join_words(group)
                if text:
                    raw.append((
                        group[0].start_frame, group[-1].end_frame,
                        text, tuple(group),
                    ))
                group = []
            group.append(word)
        if group:
            text = _join_words(group)
            if text:
                raw.append((
                    group[0].start_frame, group[-1].end_frame,
                    text, tuple(group),
                ))

    placements: list[SubtitlePlacement] = []
    for local_start, local_end, text, source_words in raw:
        mapped = _source_to_timeline_range(clip, local_start, local_end)
        if mapped is None:
            continue
        timeline_start, timeline_end = mapped
        # ASR providers occasionally emit overlapping adjacent words. Keep the
        # command stream valid by making cues monotonic and non-overlapping.
        if placements and timeline_start < placements[-1].start_frame + placements[-1].duration_frames:
            timeline_start = placements[-1].start_frame + placements[-1].duration_frames
        if timeline_end <= timeline_start:
            continue
        word_timings: list[CaptionWordTiming] = []
        for word in source_words:
            word_mapped = _source_to_timeline_range(
                clip, word.start_frame, word.end_frame
            )
            if word_mapped is None:
                continue
            word_start, word_end = word_mapped
            word_start = max(timeline_start, word_start)
            word_end = min(timeline_end, word_end)
            if word_end <= word_start:
                continue
            word_timings.append(CaptionWordTiming(
                text=word.text.strip(),
                start_frame=word_start,
                end_frame=word_end,
                confidence=max(0.0, min(1.0, float(word.confidence))),
            ))
        placements.append(
            SubtitlePlacement(
                start_frame=timeline_start,
                duration_frames=timeline_end - timeline_start,
                text=text,
                word_timings=tuple(word_timings),
            )
        )
    return tuple(placements)


__all__ = ["SubtitlePlacement", "build_subtitle_placements"]
