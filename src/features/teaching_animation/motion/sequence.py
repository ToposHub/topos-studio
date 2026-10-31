"""MT-02: ``SeriesGroup`` (sequential) and ``SequenceGroup`` (parallel) time graphs.

A ``SeriesGroup`` lays its steps out back-to-back with a per-step ``gap_after``
(negative for overlap); editing one step's duration ripples to every later step
automatically, because ``layout()`` recomputes offsets from scratch.  A
``SequenceGroup`` places children in parallel with explicit local ranges so
they can overlap freely (Remotion ``<Sequence>`` + ``<Series>`` equivalents).

Both resolve to absolute ``TimeRange`` windows so the existing element/animation
layer can consume them without knowing whether a range came from a series or a
sequence.  Local-frame resolution is a pure function of the group frame.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .time_range import TimeRange


ChildLayout = tuple  # (child_id: str, range: TimeRange)


@dataclass(frozen=True)
class SeriesStep:
    """One sequential step.  ``gap_after`` < 0 overlaps the next step."""

    step_id: str
    duration: int
    gap_after: int = 0
    trim_in: int = 0
    freeze: bool = False

    def __post_init__(self) -> None:
        if not self.step_id:
            raise ValueError("SeriesStep step_id cannot be empty")
        if self.duration <= 0:
            raise ValueError("SeriesStep duration must be positive")
        if self.trim_in < 0:
            raise ValueError("SeriesStep trim_in must be non-negative")


@dataclass(frozen=True)
class SeriesGroup:
    """Sequential children laid out by duration + per-step gap/overlap."""

    steps: tuple[SeriesStep, ...] = ()
    start: int = 0

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("SeriesGroup start must be non-negative")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("SeriesGroup step ids must be unique")
        # AUD-28200: an over-large negative ``gap_after`` would otherwise only
        # blow up deep inside ``layout()`` when a TimeRange is constructed
        # with a negative start.  Validate the ripple at edit time (this
        # constructor also runs for ``with_step_gap``) and name the offending
        # step, so the error surfaces where the gap was authored.
        cursor = self.start
        for step in self.steps:
            if cursor < 0:
                raise ValueError(
                    f"SeriesStep {step.step_id!r} would start at frame {cursor}; "
                    "gap_after overlap is too large")
            cursor += step.duration + step.gap_after

    def layout(self) -> tuple[ChildLayout, ...]:
        """Resolve each step to its absolute ``TimeRange``, rippling offsets."""
        result: list[ChildLayout] = []
        cursor = self.start
        for step in self.steps:
            time_range = TimeRange(
                start=cursor,
                duration=step.duration,
                trim_in=step.trim_in,
                freeze=step.freeze,
            )
            result.append((step.step_id, time_range))
            cursor += step.duration + step.gap_after
        return tuple(result)

    @property
    def end(self) -> int:
        layout = self.layout()
        return layout[-1][1].end if layout else self.start

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)

    def step_range(self, step_id: str) -> TimeRange | None:
        for sid, time_range in self.layout():
            if sid == step_id:
                return time_range
        return None

    def local_frame(self, step_id: str, group_frame: int, *, content_duration: int | None = None) -> int | None:
        time_range = self.step_range(step_id)
        if time_range is None:
            return None
        return time_range.local_frame(group_frame, content_duration=content_duration)

    def visible_steps(self, group_frame: int) -> tuple[str, ...]:
        """Ids of steps whose window contains ``group_frame`` (overlap-aware)."""
        return tuple(sid for sid, tr in self.layout() if tr.contains(group_frame))

    def with_step_duration(self, step_id: str, duration: int) -> "SeriesGroup":
        """Change one step's duration; later steps ripple automatically."""
        if duration <= 0:
            raise ValueError("duration must be positive")
        new_steps = tuple(
            replace(step, duration=duration) if step.step_id == step_id else step
            for step in self.steps
        )
        return replace(self, steps=new_steps)

    def with_step_gap(self, step_id: str, gap_after: int) -> "SeriesGroup":
        new_steps = tuple(
            replace(step, gap_after=gap_after) if step.step_id == step_id else step
            for step in self.steps
        )
        return replace(self, steps=new_steps)


@dataclass(frozen=True)
class SequenceChild:
    """A parallel child with an explicit local ``TimeRange`` (may overlap siblings)."""

    child_id: str
    time_range: TimeRange

    def __post_init__(self) -> None:
        if not self.child_id:
            raise ValueError("SequenceChild child_id cannot be empty")


@dataclass(frozen=True)
class SequenceGroup:
    """Parallel children positioned by their own local ranges."""

    children: tuple[SequenceChild, ...] = ()
    start: int = 0

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("SequenceGroup start must be non-negative")
        ids = [child.child_id for child in self.children]
        if len(ids) != len(set(ids)):
            raise ValueError("SequenceGroup child ids must be unique")

    def layout(self) -> tuple[ChildLayout, ...]:
        """Resolve each child's local range to an absolute ``TimeRange``."""
        result: list[ChildLayout] = []
        for child in self.children:
            local = child.time_range
            result.append((child.child_id, TimeRange(
                start=self.start + local.start,
                duration=local.duration,
                trim_in=local.trim_in,
                freeze=local.freeze,
                enabled=local.enabled,
            )))
        return tuple(result)

    @property
    def end(self) -> int:
        layout = self.layout()
        return max((tr.end for _, tr in layout), default=self.start)

    def child_range(self, child_id: str) -> TimeRange | None:
        for cid, time_range in self.layout():
            if cid == child_id:
                return time_range
        return None

    def local_frame(self, child_id: str, group_frame: int, *, content_duration: int | None = None) -> int | None:
        time_range = self.child_range(child_id)
        if time_range is None:
            return None
        return time_range.local_frame(group_frame, content_duration=content_duration)

    def visible_children(self, group_frame: int) -> tuple[str, ...]:
        return tuple(cid for cid, tr in self.layout() if tr.contains(group_frame))


def stagger_starts(
    child_ids: tuple[str, ...] | list[str],
    *,
    interval: int,
    duration: int,
    start: int = 0,
) -> SequenceGroup:
    """Build a SequenceGroup whose children enter on a fixed stagger.

    Convenience for the S1 "four cards pop in sequence" pattern: each child
    starts ``interval`` frames after the previous, all with the same duration.
    """
    if interval < 0:
        raise ValueError("stagger interval must be non-negative")
    if duration <= 0:
        raise ValueError("stagger duration must be positive")
    children = tuple(
        SequenceChild(child_id=cid, time_range=TimeRange(start=index * interval, duration=duration))
        for index, cid in enumerate(child_ids)
    )
    return SequenceGroup(children=children, start=start)


__all__ = [
    "ChildLayout",
    "SequenceChild",
    "SequenceGroup",
    "SeriesGroup",
    "SeriesStep",
    "stagger_starts",
]
