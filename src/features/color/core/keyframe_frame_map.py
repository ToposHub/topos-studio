"""Source ruler coordinates and clip transport mapping for Color animation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KeyframeFrameMap:
    clip: Any
    total: int
    effective_source_start: int | None = None
    timeline_domain: bool = False
    _signature: tuple = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Also capture mutable playback adapters: an edit made during a drag
        # must invalidate the gesture rather than mutate its old identity.
        object.__setattr__(self, "_signature", (
            self.total, self.effective_source_start, self.timeline_domain,
            *(repr(getattr(self.clip, name, None)) for name in (
                "clip_id", "timeline_start", "timeline_duration", "source_start", "source_duration",
                "speed", "freeze_source_frame", "_source_fps_num", "_source_fps_den",
                "_timeline_fps_num", "_timeline_fps_den", "_source_frame_phase_num", "_source_frame_phase_den")),
            id(getattr(self.clip, "_source_pts_index", None))))

    @property
    def available(self) -> bool:
        if self.timeline_domain or callable(getattr(self.clip, "clip_local_to_source_frame", None)):
            return True
        speed = getattr(self.clip, "speed", 1.0)
        ratio = getattr(self.clip, "_source_over_timeline_fps_ratio", None)
        return (getattr(speed, "speed", speed) == 1.0
                and not getattr(speed, "reverse", False)
                and not getattr(speed, "speed_points", ())
                and getattr(self.clip, "freeze_source_frame", None) is None
                and (not callable(ratio) or ratio() == 1))

    def source_frame(self, local: int) -> int | None:
        if not self.available or not 0 <= local < self.total:
            return None
        if self.timeline_domain:
            return int(getattr(self.clip, "timeline_start", 0)) + local
        mapper = getattr(self.clip, "clip_local_to_source_frame", None)
        source = (int(mapper(local)) if callable(mapper)
                  else int(getattr(self.clip, "source_start", 0)) + local)
        if self.effective_source_start is not None:
            source += self.effective_source_start - int(getattr(self.clip, "source_start", 0))
        return source

    def local_frame(self, source: int) -> int | None:
        """First exact sampled location. Unvisited source keys stay in the project."""
        if not self.total:
            return None
        first, last = self.source_frame(0), self.source_frame(self.total - 1)
        if first is None or last is None:
            return None
        reverse = last < first
        target = -source if reverse else source
        lo, hi = 0, self.total
        while lo < hi:
            mid = (lo + hi) // 2
            value = self.source_frame(mid)
            assert value is not None
            if (-value if reverse else value) < target:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < self.total and self.source_frame(lo) == source else None

    @property
    def panel_origin(self) -> int:
        if self.timeline_domain:
            return int(getattr(self.clip, "timeline_start", 0))
        return (self.effective_source_start if self.effective_source_start is not None
                else int(getattr(self.clip, "source_start", 0)))

    @property
    def panel_total(self) -> int:
        return max(0, int(getattr(self.clip, "source_duration", 0) or self.total))

    def panel_frame(self, source: int) -> int | None:
        """Every source key remains visible, including frames skipped by fast playback."""
        frame = source - self.panel_origin
        return frame if 0 <= frame < self.panel_total else None

    def panel_source_frame(self, frame: int) -> int | None:
        return self.panel_origin + frame if self.available and 0 <= frame < self.panel_total else None

    def panel_to_local(self, frame: int) -> int | None:
        """Seek the closest sampled source frame (ties choose the later source frame).

        The ruler retains source precision; transport quantizes to a timeline
        frame, matching Resolve's Maintain Timing behavior on a retimed clip.
        """
        source = self.panel_source_frame(frame)
        if source is None or not self.total:
            return None
        first, last = self.source_frame(0), self.source_frame(self.total - 1)
        if first is None or last is None:
            return None
        reverse = last < first
        target = -source if reverse else source
        lo, hi = 0, self.total
        while lo < hi:
            mid = (lo + hi) // 2
            value = self.source_frame(mid)
            assert value is not None
            if (-value if reverse else value) < target:
                lo = mid + 1
            else:
                hi = mid
        candidates = {max(0, lo - 1), min(self.total - 1, lo)}
        return min(candidates, key=lambda local: (
            abs(self.source_frame(local) - source), -self.source_frame(local), local))

    @property
    def signature(self) -> tuple:
        return self._signature
