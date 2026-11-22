"""File-versioned presentation indexes for frame-addressable media."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class PresentationIndex:
    """Display-order PTS ticks in the file's original video stream time base."""

    pts: tuple[int, ...]
    time_base: Fraction
    end_pts: int
    is_variable: bool

    def seconds_at(self, frame: int) -> Fraction:
        """Return time relative to the first displayed frame."""
        frame = max(0, min(frame, len(self.pts)))
        pts = self.pts[frame] if frame < len(self.pts) else self.end_pts
        return (pts - self.pts[0]) * self.time_base

    def frame_at(self, seconds: Fraction) -> int:
        """Use the last displayed frame at or before this time."""
        ticks = self.pts[0] + seconds / self.time_base
        return max(0, bisect_right(self.pts, ticks) - 1)

    def frame_for_pts(self, pts: int) -> int:
        """Map an absolute decoder timestamp to its display-order ordinal."""
        return max(0, bisect_right(self.pts, pts) - 1)


_LOCK = Lock()
_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-pts")
_INDEXES: OrderedDict[tuple[str, int, int, int], Future[PresentationIndex | None]] = OrderedDict()
_PATH_KEYS: dict[str, tuple[str, int, int, int]] = {}


def _read_variable_index(path: str) -> PresentationIndex | None:
    from .ffmpeg_source import read_presentation_index

    index = read_presentation_index(path)
    return index if index is not None and index.is_variable else None


def request_presentation_index(path: str) -> Future[PresentationIndex | None]:
    """Share one CPU demux job for each observed file version."""
    source = Path(path).resolve()
    info = source.stat()
    key = (str(source), info.st_ino, info.st_size, info.st_mtime_ns)
    with _LOCK:
        future = _INDEXES.get(key)
        if future is None:
            future = _WORKERS.submit(_read_variable_index, key[0])
            _INDEXES[key] = future
        _PATH_KEYS[path] = key
        _INDEXES.move_to_end(key)
        while len(_INDEXES) > 16:
            evictable = next((entry for entry, job in _INDEXES.items() if job.done()), None)
            if evictable is None:
                break
            _INDEXES.pop(evictable)
            for alias in [name for name, value in _PATH_KEYS.items() if value == evictable]:
                _PATH_KEYS.pop(alias)
        return future


def ready_presentation_index(path: str) -> PresentationIndex | None:
    """Read an already indexed asset without doing I/O on the caller's thread."""
    with _LOCK:
        key = _PATH_KEYS.get(path)
        future = _INDEXES.get(key) if key is not None else None
    if future is None or not future.done():
        return None
    try:
        return future.result()
    except (ImportError, IndexError, OSError, ValueError):
        return None
