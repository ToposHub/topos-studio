"""Plan linked A/V rolling edits before publishing any track change."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .timeline_ops import roll_trim_delta_bounds
from .timeline_v2 import ClipInstance, TimelineSequence

RollPair = tuple[ClipInstance, ClipInstance]
SourceLimit = Callable[[ClipInstance], int | None]


def linked_roll_pairs(
    sequence: TimelineSequence, left_id: str, right_id: str,
    linked: bool = True,
) -> tuple[RollPair, ...]:
    """Return matching seams; mismatched or locked links reject the group."""
    left, right = sequence.get_clip_by_id(left_id), sequence.get_clip_by_id(right_id)
    if left is None or right is None:
        raise ValueError("Roll clips no longer exist")
    pairs = [(left, right)]
    if linked:
        audio_left = sequence.get_reciprocal_linked_clip(left_id)
        audio_right = sequence.get_reciprocal_linked_clip(right_id)
        if audio_left is not None or audio_right is not None:
            if audio_left is None or audio_right is None:
                raise ValueError("Linked Roll requires both linked clip boundaries")
            if (audio_left.timeline_end != left.timeline_end
                    or audio_right.timeline_start != right.timeline_start
                    or audio_left.clip_id in {left_id, right_id}
                    or audio_right.clip_id in {left_id, right_id}):
                raise ValueError("Linked clip boundaries differ; disable linked selection to roll independently")
            pairs.append((audio_left, audio_right))
    for first, second in pairs:
        track = sequence.get_track_by_id(first.track_id)
        if (track is None or first.track_id != second.track_id
                or first.timeline_end != second.timeline_start):
            raise ValueError("Roll requires adjacent clips on the same track")
        if track.is_locked:
            raise ValueError(f"Track is locked: {track.track_id}")
    return tuple(pairs)


def linked_roll_bounds(pairs: tuple[RollPair, ...], source_limit: SourceLimit) -> tuple[int, int]:
    """Intersect timeline, direction, speed and source limits across seams."""
    minimum, maximum = None, None
    for left, right in pairs:
        lo, hi = roll_trim_delta_bounds(left, right, source_limit(left), source_limit(right))
        minimum = lo if minimum is None else max(minimum, lo)
        maximum = hi if maximum is None else min(maximum, hi)
    if minimum is None or maximum is None or minimum > maximum:
        return 0, 0
    return minimum, maximum


def build_linked_roll_command(
    sequence: TimelineSequence, left_id: str, right_id: str, delta: int,
    source_limit: SourceLimit, linked: bool = True,
) -> tuple[Any | None, int]:
    """One command and one common delta, with rejection before any mutation."""
    from .commands import MacroCommand, RollTrimCommand

    pairs = linked_roll_pairs(sequence, left_id, right_id, linked)
    minimum, maximum = linked_roll_bounds(pairs, source_limit)
    clamped = max(minimum, min(maximum, int(delta)))
    if clamped == 0:
        return None, 0
    commands = tuple(RollTrimCommand.create(
        left.track_id, left.clip_id, right.clip_id, clamped,
        left_max_source_frames=source_limit(left),
        right_max_source_frames=source_limit(right),
    ) for left, right in pairs)
    return (commands[0] if len(commands) == 1 else
            MacroCommand.create("Roll linked clips", commands)), clamped
