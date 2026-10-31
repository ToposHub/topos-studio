"""SUB-P0-04: subtitle edit policy — legal-range computation & rollback.

The problem this module solves: subtitle move/trim was only validated for
overlap on mouse release. When validation failed the controller swallowed the
exception but the ``SubtitleClipItem`` kept its transient (illegal) geometry,
so the chip visually sat on top of a neighbour until the next full reload —
the user could believe the edit succeeded.

``SubtitleEditPolicy`` computes the *legal* placement range for a cue given its
neighbours and the track lock state, so the drag can clamp in real time. It is
pure logic (no Qt) so it can be unit-tested headlessly and shared by the drag
handler, the item, and (later) a caption workspace.

Conventions
-----------
* Frames are inclusive-interval integers: a cue occupies
  ``[timeline_start, timeline_start + timeline_duration)``.
* Neighbours are the other cues on the same track. A cue may not overlap a
  neighbour; it may become adjacent (``end == next_start`` is allowed).
* A locked track forbids every edit (range is empty).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SubtitleLegalRange:
    """The set of legal (start, duration) outcomes for a drag.

    ``min_start``/``max_start`` bound the move range for the body. Trim
    operations shrink these further via ``min_duration``/``max_duration`` and
    the left-trim ``min_start`` (the body cannot cross the left neighbour).
    ``editable`` is False when the track is locked, in which case all fields
    are zero and any edit must be rejected.
    """

    editable: bool
    min_start: int
    max_start: int
    min_duration: int = 1
    max_duration: int = 1

    def clamp_start(self, start: int) -> int:
        """Clamp a proposed move start to the legal move range."""
        if not self.editable:
            return start
        return max(self.min_start, min(self.max_start, start))

    def clamp_duration(self, duration: int) -> int:
        """Clamp a proposed duration to the legal duration range (>=1)."""
        if not self.editable:
            return duration
        return max(self.min_duration, min(self.max_duration, max(1, duration)))


class SubtitleEditPolicy:
    """Compute legal placement ranges for subtitle move/trim operations.

    All methods are pure functions over cue timing data; they take either
    ``SubtitleClip``/``EditTrack`` instances or lightweight dicts so the policy
    stays decoupled from the Qt/UI layer.
    """

    @staticmethod
    def _cue_start(cue: Any) -> int:
        if hasattr(cue, "timeline_start"):
            return int(cue.timeline_start)
        return int(cue["timeline_start"])

    @staticmethod
    def _cue_end(cue: Any) -> int:
        if hasattr(cue, "timeline_end"):
            return int(cue.timeline_end)
        start = int(cue["timeline_start"])
        duration = int(cue["timeline_duration"])
        return start + duration

    @staticmethod
    def _cue_id(cue: Any) -> str:
        if hasattr(cue, "clip_id"):
            return cue.clip_id
        return cue["clip_id"]

    @classmethod
    def compute_move_range(
        cls,
        clip_id: str,
        duration: int,
        neighbours: tuple[Any, ...],
        track_locked: bool = False,
        current_start: Optional[int] = None,
    ) -> SubtitleLegalRange:
        """Legal ``[min_start, max_start]`` for moving a cue of ``duration``.

        The cue may not overlap any neighbour. The cue lives in a "gap"
        delimited by its left neighbour (greatest end among cues ending at or
        before ``current_start``) and its right neighbour (smallest start
        among cues starting at or after ``current_start + duration``). It may
        move freely within that gap. With no right neighbour the max start is
        unbounded. Locked tracks return a non-editable range.

        ``current_start`` is optional but recommended: when provided it
        identifies the gap the cue currently occupies so the move is clamped
        to that gap (preventing a drag from jumping over a neighbour). When
        omitted, the cue is assumed to start at frame 0.
        """
        if track_locked:
            return SubtitleLegalRange(editable=False, min_start=0, max_start=0)

        duration = max(1, int(duration))
        cur_start = 0 if current_start is None else int(current_start)
        cur_end = cur_start + duration

        # Left neighbour: greatest end among cues ending <= cur_start.
        left_bound = 0
        # Right neighbour: smallest start among cues starting >= cur_end.
        right_bound: Optional[int] = None

        for nb in neighbours:
            if cls._cue_id(nb) == clip_id:
                continue
            nb_start = cls._cue_start(nb)
            nb_end = cls._cue_end(nb)
            if nb_end <= cur_start:
                left_bound = max(left_bound, nb_end)
            if nb_start >= cur_end:
                if right_bound is None or nb_start < right_bound:
                    right_bound = nb_start

        max_start = (right_bound - duration) if right_bound is not None else (2 ** 31 - 1)
        if max_start < left_bound:
            # No legal slot of this duration exists (cue too wide for the gap).
            return SubtitleLegalRange(
                editable=False, min_start=left_bound, max_start=left_bound
            )
        return SubtitleLegalRange(
            editable=True,
            min_start=left_bound,
            max_start=max_start,
            min_duration=1,
            max_duration=duration,
        )

    @classmethod
    def compute_trim_range(
        cls,
        clip_id: str,
        current_start: int,
        current_duration: int,
        neighbours: tuple[Any, ...],
        track_locked: bool = False,
    ) -> SubtitleLegalRange:
        """Legal range covering both left-trim and right-trim.

        * Left-trim changes (start, duration) keeping end fixed: start is
          bounded below by the left neighbour's end and above by end-1
          (AUD-15807: 上限不再混入整体平移约束——右邻恰好与本 cue 相接
          （密集字幕轨常态）时，旧实现给出 max_start == current_start，
          左缘完全无法向内拉；右邻对右缘方向的约束由 max_duration 表达).
        * Right-trim changes duration keeping start fixed: duration is bounded
          above by the right neighbour's start - current_start.

        The returned range expresses the union so the caller can clamp.
        """
        if track_locked:
            return SubtitleLegalRange(
                editable=False, min_start=0, max_start=0
            )

        current_start = int(current_start)
        current_duration = max(1, int(current_duration))
        end = current_start + current_duration

        # Left bound for the body start: the left neighbour's end (>=0).
        left_bound = 0
        # Right bound for the body end: the right neighbour's start.
        right_neighbour_start: Optional[int] = None

        for nb in neighbours:
            if cls._cue_id(nb) == clip_id:
                continue
            nb_start = cls._cue_start(nb)
            nb_end = cls._cue_end(nb)
            if nb_end <= current_start:
                left_bound = max(left_bound, nb_end)
            if nb_start >= end:
                if right_neighbour_start is None or nb_start < right_neighbour_start:
                    right_neighbour_start = nb_start

        # AUD-15807: 左缘裁剪保持 end 不变、仅缩短时长，start 上限只受
        # "duration >= 1"（即 end-1）与左邻约束。旧实现把整体平移约束
        # ``right_neighbour_start - current_duration`` 混入上限：右邻与本
        # cue 相接时 max_start == current_start，左缘向内拉被完全钳回原位。
        max_start = end - 1
        if max_start < left_bound:
            return SubtitleLegalRange(
                editable=False, min_start=left_bound, max_start=left_bound
            )

        # Duration range for right-trim (start fixed): [1, right_neighbour_start - start].
        min_duration = 1
        if right_neighbour_start is not None:
            max_duration = max(1, right_neighbour_start - current_start)
        else:
            max_duration = 2 ** 31 - 1

        return SubtitleLegalRange(
            editable=True,
            min_start=left_bound,
            max_start=max_start,
            min_duration=min_duration,
            max_duration=max_duration,
        )

    @staticmethod
    def restore_on_failure(
        item: Any,
        original_start: int,
        original_duration: int,
    ) -> None:
        """Restore a SubtitleClipItem to its pre-drag geometry after a failed commit.

        Called by the controller when the release-time command raised (e.g.
        overlap). Without this the item keeps its transient illegal position
        until the next reload. The item is expected to expose
        ``restore_geometry(start, duration)`` (added in Phase 0.4); if it does
        not, the call is a no-op so older items don't crash.
        """
        restore = getattr(item, "restore_geometry", None)
        if callable(restore):
            restore(int(original_start), int(original_duration))
