"""Decode requested stills without blocking the Color workspace."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
from pathlib import Path

import cv2
from numpy import ndarray
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from src.features.color.core.image_engine import ImageEngine
from src.shared.io.file_io import FileIO


logger = logging.getLogger(__name__)
FileRevision = tuple[int, int, int, int, int]


def _revision(path: str) -> FileRevision | None:
    try:
        return FileIO._file_revision(path)
    except OSError:
        return None


def _is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in ImageEngine.RAW_EXTENSIONS


@dataclass(frozen=True)
class DecodedPhoto:
    path: str
    revision: FileRevision
    pixels: tuple[ndarray, ndarray, ndarray]
    camera_white_balance: tuple[float, ...] | None

    def matches(self, path: str) -> bool:
        return self.path == path and _revision(path) == self.revision

    @property
    def nbytes(self) -> int:
        full, high, low = self.pixels
        high_bytes = high.nbytes if high is not full else 0
        low_bytes = low.nbytes if low is not full else 0
        return full.nbytes + high_bytes + low_bytes


class _DecodeSignals(QObject):
    finished = pyqtSignal(object, object)


class _DecodePhoto(QRunnable):
    def __init__(self, path: str, revision: FileRevision, *, prefetch: bool = False) -> None:
        super().__init__()
        self.path = path
        self.revision = revision
        self.prefetch = prefetch
        self.signals = _DecodeSignals()

    def run(self) -> None:
        result = None
        try:
            full, high, low = FileIO.load_image(self.path, use_preview_cache=False)
            if (full is not None and high is not None and low is not None
                    and _revision(self.path) == self.revision):
                wb = None
                if _is_raw(self.path):
                    wb = tuple(FileIO.read_camera_white_balance(self.path))
                for pixels in (full, high, low):
                    pixels.setflags(write=False)
                result = DecodedPhoto(self.path, self.revision, (full, high, low), wb)
        except (OSError, ValueError, RuntimeError, MemoryError, cv2.error) as exc:
            logger.warning("Grade set image decode failed for %s: %s", self.path, exc)
        finally:
            self.signals.finished.emit(self, result)


class GradeSetImageLoader(QObject):
    """Coalesce clicks and let a regular still open during a RAW decode."""

    ready = pyqtSignal(int, str, object)
    CACHE_BUDGET = 512 * 1024 * 1024
    CACHE_LIMIT = 8

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._serial = 0
        self._pending: tuple[int, str] | None = None
        self._workers: dict[bool | str, _DecodePhoto] = {}
        self._cache: OrderedDict[str, DecodedPhoto] = OrderedDict()
        self._cache_bytes = 0

    def request(self, path: str) -> int:
        self._serial += 1
        serial = self._serial
        self._pending = serial, path
        self._start_next()
        return serial

    def prefetch(self, path: str) -> None:
        """Warm one adjacent still without reserving a foreground RAW lane."""
        if self._pending is not None or self._workers or _is_raw(path):
            return
        revision = _revision(path)
        if revision is None:
            return
        cached = self._cache.get(path)
        if cached is not None and cached.revision == revision:
            return
        self._start_worker(path, revision, prefetch=True)

    def is_current(self, serial: int) -> bool:
        return self._serial == serial

    def cancel(self) -> None:
        self._serial += 1
        self._pending = None

    def shutdown(self) -> None:
        self.cancel()
        self._cache.clear()
        self._cache_bytes = 0
        for worker in self._workers.values():
            worker.signals.finished.disconnect(self._decoded)
        self._workers.clear()

    def _start_next(self) -> None:
        if self._pending is None:
            return
        serial, path = self._pending
        revision = _revision(path)
        cached = self._cache.get(path)
        if cached is not None and cached.revision != revision:
            self._cache_bytes -= self._cache.pop(path).nbytes
            cached = None
        if cached is not None or revision is None:
            if cached is not None:
                self._cache.move_to_end(path)
            self._pending = None
            QTimer.singleShot(0, lambda: self._deliver(serial, path, cached))
        elif (warming := self._workers.get("prefetch")) is not None and warming.path == path and warming.revision == revision:
            return  # The foreground request can adopt this exact decode.
        elif _is_raw(path) not in self._workers:
            self._start_worker(path, revision)

    def _start_worker(self, path: str, revision: FileRevision, *, prefetch: bool = False) -> None:
        worker = _DecodePhoto(path, revision, prefetch=prefetch)
        worker.signals.finished.connect(self._decoded)
        self._workers["prefetch" if prefetch else _is_raw(path)] = worker
        QThreadPool.globalInstance().start(worker)

    def _deliver(self, serial: int, path: str, photo: DecodedPhoto | None) -> None:
        if self.is_current(serial):
            self.ready.emit(serial, path, photo)

    def _decoded(self, worker: _DecodePhoto, photo: DecodedPhoto | None) -> None:
        path = worker.path
        lane = "prefetch" if worker.prefetch else _is_raw(path)
        if self._workers.get(lane) is not worker:
            return
        del self._workers[lane]
        if worker.revision != _revision(path):
            self._start_next()
            return
        if self._pending is None or self._pending[1] != path:
            if worker.prefetch and photo is not None and self._pending is None:
                self._cache_photo(photo)
            self._start_next()
            return
        serial, _ = self._pending
        self._pending = None
        if photo is not None:
            self._cache_photo(photo)
        self._deliver(serial, path, photo)

    def _cache_photo(self, photo: DecodedPhoto) -> None:
        if photo.nbytes > self.CACHE_BUDGET:
            return
        previous = self._cache.pop(photo.path, None)
        if previous is not None:
            self._cache_bytes -= previous.nbytes
        while (len(self._cache) >= self.CACHE_LIMIT
               or self._cache_bytes + photo.nbytes > self.CACHE_BUDGET):
            _, oldest = self._cache.popitem(last=False)
            self._cache_bytes -= oldest.nbytes
        self._cache[photo.path] = photo
        self._cache_bytes += photo.nbytes
