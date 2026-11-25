"""Keep exact VideoToolbox frames across seeks during an Edit scrub gesture."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Hashable

from src.features.edit.core.playback_debug import playback_trace

if TYPE_CHECKING:
    from src.shared.media.macos_vt.packet_demux import ScrubMediaIdentity
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource, VtPixelBufferFrame, _ScrubRewindBuffer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrubClipRange:
    generation: int
    clip_id: str
    timeline_start: int
    timeline_stop: int
    source_start: int

    def source_at(self, timeline_frame: int) -> int | None:
        if self.timeline_start <= timeline_frame < self.timeline_stop:
            return self.source_start + timeline_frame - self.timeline_start
        return None

    @property
    def source_stop(self) -> int:
        return self.source_start + self.timeline_stop - self.timeline_start


@dataclass(frozen=True)
class _Playhead:
    generation: int
    frame: int
    direction: int
    velocity: float


@dataclass(frozen=True)
class _Route:
    key: Hashable
    path: str
    clip: ScrubClipRange
    media: ScrubMediaIdentity
    owner: object


class ReverseScrubFrames:
    """One independent hardware cursor and one exact frame window per manager."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._playhead: _Playhead | None = None
        self._route: _Route | None = None
        self._thread: threading.Thread | None = None
        self._motion = 0
        self._closed = False
        self._unavailable: tuple[Hashable, ScrubClipRange] | None = None

    @staticmethod
    def _buffer() -> _ScrubRewindBuffer:
        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

        return MacOSVtZeroCopySource._scrub_rewind_buffer

    def move(self, generation: int, frame: int, direction: int, velocity: float) -> None:
        with self._condition:
            old = self._playhead
            if self._closed or (old is not None and old.generation > generation):
                return
            route = self._route
            if route is not None and (
                route.clip.generation != generation or route.clip.source_at(frame) is None
            ):
                self._drop_route_locked()
            head = _Playhead(generation, frame, direction, velocity)
            if old == head:
                return
            if old is None or old.generation != generation or old.direction != direction:
                self._motion += 1
            self._playhead = head
            if self._route is not None:
                self._condition.notify()

    def cancel(self, *, close: bool = False) -> None:
        with self._condition:
            self._closed = self._closed or close
            self._playhead = None
            self._unavailable = None
            self._drop_route_locked()
            self._condition.notify()

    def _drop_route_locked(self) -> None:
        route, self._route = self._route, None
        if route is not None:
            self._motion += 1
            self._buffer().discard(route.owner)

    def engaged(self, generation: int) -> bool:
        with self._condition:
            return self._route is not None and self._route.clip.generation == generation

    def _start_worker_locked(self) -> None:
        thread = threading.Thread(target=self._run, name="EditReverseScrub", daemon=True)
        self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            self._thread = None
            self._drop_route_locked()

    def borrow(self, key: Hashable, clip: ScrubClipRange, target: int) -> VtPixelBufferFrame | None:
        with self._condition:
            route, head = self._route, self._playhead
            if (
                route is None or head is None or head.direction != -1
                or route.key != key or route.clip != clip
            ):
                return None
            frame = self._buffer().borrow_preview(route.owner, target)
        if frame is not None:
            playback_trace("decode_manager.reverse_scrub_hit", frame=target, asset=route.path)
        return frame

    def observe(
        self, key: Hashable, path: str, clip: ScrubClipRange,
        source: MacOSVtZeroCopySource, frame: VtPixelBufferFrame,
    ) -> None:
        """Register the validated foreground route and retain recent forward frames."""
        identity = source.scrub_media_identity
        if identity is None or not source.h264_scrub_frame_matches(frame):
            return
        with self._condition:
            head = self._playhead
            if (self._closed or head is None or clip.generation != head.generation
                    or (key, clip) == self._unavailable):
                return
            source_head = clip.source_at(head.frame)
            if source_head is None:
                return
            route = self._route
            if route is None or route.key != key or route.clip != clip or route.media != identity:
                self._drop_route_locked()
                route = self._route = _Route(key, path, clip, identity, object())
                if self._thread is None:
                    self._start_worker_locked()
                    if self._thread is None:
                        return
                self._condition.notify()
            if head.direction <= 0 or frame.frame_index < source_head - 30:
                return
            size = frame.allocated_bytes()
            if not 0 < size <= 16 << 20:
                return
            lease = frame.acquire_lease()
            if lease is not None and not self._buffer().store_preview(
                route.owner, lease, size, head=source_head, reverse=False,
            ):
                lease.release()

    def _run(self) -> None:
        from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

        cursor: MacOSVtZeroCopySource | None = None
        opened_route: _Route | None = None
        last: tuple[_Route, _Playhead, int] | None = None
        prepared: tuple[_Route, int, int] | None = None
        route: _Route | None = None
        try:
            while True:
                with self._condition:
                    route, head, motion = self._route, self._playhead, self._motion
                    if route is None or head is None or self._closed:
                        return
                    current = route, head, motion
                    if current == last:
                        self._condition.wait()
                        continue
                    last = current
                if opened_route is not route:
                    if cursor is not None:
                        cursor.close()
                    opened_route = route
                    spare = MacOSVtZeroCopySource()
                    if (spare.open(route.path, require_hardware=True)
                            and spare.scrub_media_identity == route.media and spare.supports_h264_scrub_gops()):
                        cursor = spare
                    else:
                        spare.close()
                        cursor = None
                        playback_trace("decode_manager.reverse_scrub_unavailable", asset=route.path)
                        with self._condition:
                            if self._route is route:
                                self._unavailable = route.key, route.clip
                                self._drop_route_locked()
                        return
                if cursor is None or head.direction != -1 or head.velocity > 180.0:
                    continue
                source_head = route.clip.source_at(head.frame)
                if source_head is None:
                    continue
                gop = cursor.previous_h264_scrub_gop(source_head)
                lead = max(5, min(18, round(head.velocity / 6.0)))
                if gop is None or source_head > gop.stop_frame + lead:
                    continue
                marker = route, motion, gop.first_frame
                if marker == prepared:
                    continue
                begin = max(route.clip.source_start, gop.first_frame)
                end = min(route.clip.source_stop, gop.stop_frame)
                if begin >= end:
                    continue
                prepared = marker

                def stale() -> bool:
                    with self._condition:
                        latest = self._playhead
                        return (
                            self._route is not route or self._motion != motion or latest is None
                            or latest.frame < route.clip.timeline_start + gop.first_frame - route.clip.source_start
                        )

                def store(frame: VtPixelBufferFrame, size: int) -> bool:
                    with self._condition:
                        latest = self._playhead
                        if self._route is not route or self._motion != motion or latest is None:
                            return False
                        latest_source = route.clip.source_at(latest.frame)
                        if latest_source is None or latest_source < gop.first_frame:
                            return False
                        return self._buffer().store_preview(
                            route.owner, frame, size, head=latest_source, reverse=True,
                        )

                started = time.perf_counter()
                count = cursor.decode_h264_scrub_gop(
                    gop, first_output_frame=begin, stop_output_frame=end,
                    is_stale=stale, deadline_at=started + 0.150, sink=store,
                )
                retained_bytes, rewind_bytes = self._buffer().usage(route.owner)
                playback_trace(
                    "decode_manager.reverse_scrub_prepared", asset=route.path, frame=gop.first_frame,
                    stop=gop.stop_frame, head=source_head, count=count,
                    retained_bytes=retained_bytes, rewind_bytes=rewind_bytes,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.debug("Reverse scrub preparation failed: %s", exc)
            with self._condition:
                if route is not None and self._route is route:
                    self._unavailable = route.key, route.clip
                    self._drop_route_locked()
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            finally:
                with self._condition:
                    if self._thread is threading.current_thread():
                        self._thread = None
                        if self._route is route and route is not None:
                            self._unavailable = route.key, route.clip
                            self._drop_route_locked()
                        elif self._route is not None and not self._closed:
                            self._start_worker_locked()
