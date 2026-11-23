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
    paths: dict[str, bool]
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
        self._queue: dict[str, bool] = {}
        self._preview_checked: set[str] = set()
        self._running: _BadgeRead | None = None
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start)
        self._finished.connect(self._accept, Qt.ConnectionType.QueuedConnection)

    def is_graded(self, path: str) -> bool:
        """Request the status and preview for an on-screen card."""
        return self._request(path, preview=True)

    def has_grade(self, path: str) -> bool:
        """Filtering needs project ownership only, even for off-screen cards."""
        return self._request(path, preview=False)

    def _request(self, path: str, *, preview: bool) -> bool:
        if self._closed:
            return False
        path = nfc(path)
        marked = self._results.get(path)
        needs_preview = preview and marked is not False and path not in self._preview_checked
        if (marked is None or needs_preview) and (self._running is None or path not in self._running.paths):
            self._queue[path] = needs_preview or self._queue.get(path, False)
            if self._running is None and not self._timer.isActive():
                self._timer.start(0)
        return bool(marked)

    def invalidate(self, path: str) -> None:
        """Re-resolve one source after the host confirms a save."""
        path = nfc(path)
        known = self._results.pop(path, None) is not None
        self._previews.pop(path, None)
        self._preview_checked.discard(path)
        if self._running is not None and path in self._running.paths:
            self._running.paths.pop(path)
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
        self._preview_checked.clear()
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
        batch = {path: self._queue.pop(path) for path in tuple(self._queue)[:64]}
        request_id = uuid4().hex
        cancel = CancelToken()
        self._running = _BadgeRead(request_id, dict(batch), cancel)

        def work() -> dict[str, tuple[bool, bool, QImage | None]]:
            marks: dict[str, tuple[bool, bool, QImage | None]] = {}
            for path, preview in batch.items():
                if cancel.requested:
                    break
                sidecar, owned = resolve_image_sidecar(path)
                cached = facade.graded_thumbnail(path, verified_sidecar=sidecar) if owned and preview else None
                image = (QImage.fromData(cached).convertToFormat(QImage.Format.Format_RGB888)
                         if cached is not None else None)
                marks[path] = (owned, preview, image)
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
        if outcome.error is not None and outcome.error.code == "busy":
            for path, preview in paths.items():
                self._queue[path] = preview or self._queue.get(path, False)
            if self._queue:
                self._timer.start(250)
            return
        result = outcome.result if outcome.error is None else {}
        for path in paths:
            marked, preview, image = result.get(path, (False, False, None))
            if preview:
                self._preview_checked.add(path)
            self._results[path] = marked
            if image is not None and not image.isNull():
                self._previews[path] = image
                if len(self._previews) > 256:
                    evicted, _image = self._previews.popitem(last=False)
                    self._preview_checked.discard(evicted)
        if any(self._results[path] for path in paths):
            self.changed.emit()
        self._start()
