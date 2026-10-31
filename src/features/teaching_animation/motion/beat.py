"""MT-08: beat grid + timecode — beat-level time semantics.

Beats let a scene express timing musically (BPM, time signature) and align
narration cues to the timeline.  A ``BeatGrid`` maps beat indices to frames at
a given BPM/fps; ``timecode`` formats a frame as ``HH:MM:SS:FF``.  Everything is
a pure function of (frame, fps, grid) so preview/export and the editor timeline
agree on where every beat lands.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BeatGrid:
    """Maps musical beats to scene frames."""

    bpm: float = 120.0
    time_signature_num: int = 4
    time_signature_den: int = 4
    fps_num: int = 24
    fps_den: int = 1
    anchor_frame: int = 0       # the frame at which beat 0 (downbeat) lands

    def __post_init__(self) -> None:
        if self.bpm <= 0.0:
            raise ValueError("bpm must be positive")
        if self.time_signature_num <= 0 or self.time_signature_den <= 0:
            raise ValueError("time signature must be positive")
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise ValueError("fps must be positive")
        if self.anchor_frame < 0:
            raise ValueError("anchor_frame must be non-negative")

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    @property
    def frames_per_beat(self) -> float:
        """Frames per single beat at this BPM/fps (denominator-relative)."""
        # A beat is a 1/4-note by definition; if signature denominator != 4,
        # the "beat" unit scales so bars line up.
        seconds_per_beat = 60.0 / self.bpm
        return seconds_per_beat * self.fps

    def frame_at_beat(self, beat_index: float) -> int:
        return self.anchor_frame + int(round(beat_index * self.frames_per_beat))

    def beat_at_frame(self, frame: int) -> float:
        return (frame - self.anchor_frame) / self.frames_per_beat

    def frames_per_bar(self) -> float:
        # A bar = time_signature_num beats of (4/den) — denominator scales the beat.
        beat_unit_scale = 4.0 / self.time_signature_den
        return self.time_signature_num * self.frames_per_beat * beat_unit_scale

    def to_dict(self) -> dict[str, object]:
        return {"bpm": self.bpm, "time_signature_num": self.time_signature_num,
                "time_signature_den": self.time_signature_den,
                "fps_num": self.fps_num, "fps_den": self.fps_den,
                "anchor_frame": self.anchor_frame}


@dataclass(frozen=True)
class Beat:
    """A named beat marker on the beat grid (for narration/cue alignment)."""

    beat_id: str
    beat_index: float
    label: str = ""

    def __post_init__(self) -> None:
        if not self.beat_id:
            raise ValueError("beat_id cannot be empty")


def format_timecode(frame: int, fps_num: int = 24, fps_den: int = 1) -> str:
    """SMPTE-style ``HH:MM:SS:FF`` timecode for an integer frame.

    Rational frame rates (e.g. NTSC ``30000/1001``) are converted with exact
    integer arithmetic on ``frame * fps_den`` so seconds/frames fields never
    drift from the true frame position (rounding the rate to an integer fps
    accumulates ~3.6 frames of error per minute at 29.97).
    """
    if frame < 0:
        raise ValueError("frame must be non-negative")
    if fps_num <= 0 or fps_den <= 0:
        raise ValueError("fps must be positive")
    fps_int = max(1, int(round(fps_num / fps_den)))   # nominal display rate (e.g. 30 for 30000/1001)
    ticks = frame * fps_den                           # exact seconds numerator: ticks / fps_num
    total_seconds_floor = ticks // fps_num
    fields = (ticks % fps_num) * fps_int // fps_num   # sub-second remainder, scaled to display rate
    hours = total_seconds_floor // 3600
    minutes = (total_seconds_floor % 3600) // 60
    seconds = total_seconds_floor % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{fields:02d}"


def narration_alignment(cue_frame: int, grid: BeatGrid, beats: tuple[Beat, ...]) -> tuple[Beat | None, float]:
    """Find the nearest beat to a narration cue and its distance in beats.

    Returns (nearest_beat_or_None, beat_distance) — distance is signed so the
    editor can nudge the cue left/right onto the beat.
    """
    cue_beat = grid.beat_at_frame(cue_frame)
    nearest: Beat | None = None
    nearest_dist = math.inf
    for beat in beats:
        dist = beat.beat_index - cue_beat
        if abs(dist) < abs(nearest_dist):
            nearest_dist = dist
            nearest = beat
    return nearest, (0.0 if nearest is None else nearest_dist)


__all__ = ["Beat", "BeatGrid", "format_timecode", "narration_alignment"]
