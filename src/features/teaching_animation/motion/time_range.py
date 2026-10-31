"""MT-01: ``TimeRange`` — the local-time-domain primitive.

A ``TimeRange`` places a child (element/group/instance) on its parent's
timeline with ``[start, start+duration)`` half-open semantics, plus ``trim_in``
(read offset into the child's content) and ``freeze`` (hold the last content
frame after the content ends).  Every local-frame computation is a pure
function of the parent frame, so arbitrarily nested objects resolve
deterministically with no off-by-one at the first/last frame.

This replaces the flat keyframe-only time model with one that can express
"appear at frame 12, play 24 frames of content, then hold" as a single value.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TimeRange:
    """Half-open ``[start, start+duration)`` window on the parent timeline.

    ``trim_in`` is the frame offset into the child's own content; ``freeze``
    holds the last content frame once content is exhausted (instead of hiding
    the child).  ``enabled`` lets a beat/sequence be muted without deletion.
    """

    start: int = 0
    duration: int = 1
    trim_in: int = 0
    freeze: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("TimeRange start must be non-negative")
        if self.duration <= 0:
            raise ValueError("TimeRange duration must be positive")
        if self.trim_in < 0:
            raise ValueError("TimeRange trim_in must be non-negative")

    @property
    def end(self) -> int:
        return self.start + self.duration

    def contains(self, parent_frame: int) -> bool:
        """True iff the parent frame falls inside this enabled range."""
        if not self.enabled:
            return False
        return self.start <= parent_frame < self.end

    def local_frame(self, parent_frame: int, *, content_duration: int | None = None) -> int | None:
        """Resolve a parent frame to the child's content frame.

        Returns ``None`` when the child is not visible at this parent frame
        (outside the range, or content exhausted without ``freeze``).  With
        ``freeze`` the child holds its last content frame once content ends.
        """
        if not self.contains(parent_frame):
            return None
        local = (parent_frame - self.start) + self.trim_in
        if local < 0:
            return None
        if content_duration is not None and content_duration > 0 and local >= content_duration:
            return max(0, content_duration - 1) if self.freeze else None
        return local

    def shifted(self, delta: int) -> "TimeRange":
        """Return a copy translated by ``delta`` parent frames (rippling).

        AUD-28206: translation must stay a translation — clamping a
        negative shift to 0 would silently shrink sibling spacing and break
        group-relative geometry (and any undo/redo replay of the shift).
        Overflows past frame 0 raise instead; callers shifting a whole
        group should clamp ``delta`` to the group's common lower bound
        before shifting each member.
        """
        if self.start + delta < 0:
            raise ValueError(
                f"shift by {delta} would move start before frame 0 "
                f"(start={self.start}); clamp the shift at group level instead")
        return replace(self, start=self.start + delta)

    def with_duration(self, duration: int) -> "TimeRange":
        if duration <= 0:
            raise ValueError("duration must be positive")
        return replace(self, duration=duration)


def transform_to_parent(child: "TimeRange", parent: "TimeRange") -> "TimeRange":
    """Express a child range (given in parent-local frames) in the grandparent frame.

    Used by Series/Sequence composition: a child placed at local ``start``
    inside its parent ends up at ``parent.start + child.start`` on the
    grandparent timeline.  ``trim_in`` composes so nested trims accumulate.
    """
    if not parent.enabled or not child.enabled:
        return replace(child, enabled=False)
    return TimeRange(
        start=parent.start + child.start,
        duration=child.duration,
        trim_in=child.trim_in + parent.trim_in,
        freeze=child.freeze,
        enabled=True,
    )


def compose_local_frame(
    parent_frame: int,
    parent: "TimeRange",
    child: "TimeRange",
    *,
    content_duration: int | None = None,
) -> int | None:
    """Resolve a grandparent frame through a parent then a child range."""
    parent_local = parent.local_frame(parent_frame)
    if parent_local is None:
        return None
    return child.local_frame(parent_local, content_duration=content_duration)


__all__ = ["TimeRange", "compose_local_frame", "transform_to_parent"]
