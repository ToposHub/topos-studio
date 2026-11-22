"""Asynchronously resolve adjacent image projects and their cached card previews."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QImage

from src.features.files.repository.catalog import nfc
from src.features.files.services.facade import FacadeOutcome, FilesFacade
from src.features.files.transactions import CancelToken
from src.shared.project.image_sidecar import resolve_image_sidecar


@dataclass
class _BadgeRead:
    request_id: str
    paths: set[str]
    cancel: CancelToken


class LibraryGradeBadges(QObject):
    changed = pyqtSignal()
    _finished = pyqtSignal(object)

    def __init__(self, get_facade: Callable[[], FilesFacade | None],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._get_facade = get_facade
        self._results: dict[str, bool] = {}
        self._previews: OrderedDict[str, QImage] = OrderedDict()
        self._queue: dict[str, None] = {}
        self._running: _BadgeRead | None = None
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start)
        self._finished.connect(self._accept, Qt.ConnectionType.QueuedConnection)

    def is_graded(self, path: str) -> bool:
        """Return cached status and request a check for newly painted cards."""
        if self._closed:
            return False
        path = nfc(path)
        if path not in self._results and (self._running is None or path not in self._running.paths):
            self._queue[path] = None
            if self._running is None and not self._timer.isActive():
                self._timer.start(0)
        return self._results.get(path, False)

    def invalidate(self, path: str) -> None:
        """Re-resolve one source after the host confirms a save."""
        path = nfc(path)
        known = self._results.pop(path, None) is not None
        self._previews.pop(path, None)
        if self._running is not None and path in self._running.paths:
            self._running.paths.discard(path)
            known = True
        if known:
            self.changed.emit()

    def preview(self, path: str) -> QImage | None:
        path = nfc(path)
        image = self._previews.get(path)
        if image is not None:
            self._previews.move_to_end(path)
        return image

    def reset(self) -> None:
        self._timer.stop()
        self._results.clear()
        self._previews.clear()
        self._queue.clear()
        if self._running is not None:
            self._running.cancel.request()
            self._running = None

    def shutdown(self) -> None:
        self._closed = True
        self.reset()

    def _start(self) -> None:
        if self._closed or self._running is not None or not self._queue:
            return
        facade = self._get_facade()
        if facade is None:
            self._results.update(dict.fromkeys(self._queue, False))
            self._queue.clear()
            return
        paths = tuple(list(self._queue)[:64])
        for path in paths:
            self._queue.pop(path)
        request_id = uuid4().hex
        cancel = CancelToken()
        self._running = _BadgeRead(request_id, set(paths), cancel)

        def work() -> dict[str, tuple[bool, QImage | None]]:
            marks: dict[str, tuple[bool, QImage | None]] = {}
            for path in paths:
                if cancel.requested:
                    break
                sidecar, owned = resolve_image_sidecar(path)
                jpeg = facade.graded_thumbnail(path, verified_sidecar=sidecar) if owned else None
                image = QImage.fromData(jpeg, "JPEG") if jpeg is not None else None
                marks[path] = (owned, image)
            return marks

        def deliver(outcome: FacadeOutcome) -> None:
            if not cancel.requested:
                self._finished.emit(outcome)

        facade.run(key="library-grade-badges", request_id=request_id, work=work, listener=deliver)

    def _accept(self, outcome: FacadeOutcome) -> None:
        if self._running is None or self._running.request_id != outcome.request_id:
            return
        paths = self._running.paths
        self._running = None
        result = outcome.result if outcome.error is None else {}
        for path in paths:
            marked, image = result.get(path, (False, None))
            self._results[path] = marked
            if image is not None and not image.isNull():
                self._previews[path] = image
                if len(self._previews) > 256:
                    evicted, _image = self._previews.popitem(last=False)
                    self._results.pop(evicted, None)
        if any(self._results[path] for path in paths):
            self.changed.emit()
        self._start()
