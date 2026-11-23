"""Share a finished Color frame with photo cards and the Files thumbnail cache."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage

from src.features.files.repository.catalog import nfc
from src.features.files.services.thumbnail_cache import FileRevision, GradedThumbnailStamp

if TYPE_CHECKING:
    from numpy import ndarray
    from src.app.main_window import MainWindow


@dataclass
class _CacheRequest:
    key: tuple[int, int]
    stamp: GradedThumbnailStamp
    resume: Callable[[], object] | None
    wait_for_cache: bool
    write_id: str | None = None


class GradeSetPreviews(QObject):
    _written = pyqtSignal(str, str, bool)

    def __init__(self, host: MainWindow) -> None:
        super().__init__(host)
        self._host = host
        self._snapshot: tuple[tuple[int, int], ndarray] | None = None
        self._scheduled_key: tuple[int, int] | None = None
        self._completed: tuple[tuple[int, int], GradedThumbnailStamp] | None = None
        self._resuming: tuple[int, int] | None = None
        self._loaded_source: tuple[str, FileRevision] | None = None
        self._saved_stamp: GradedThumbnailStamp | None = None
        self._request: _CacheRequest | None = None
        self._cache_after_save: str | None = None
        self._closed = False
        self._idle = QTimer(self, singleShot=True, interval=300)
        self._idle.timeout.connect(self._capture_if_visible)
        self._deadline = QTimer(self, singleShot=True, interval=350)
        self._deadline.timeout.connect(self._finish)
        self._written.connect(self._write_finished)
        host.engine.frame_ready.connect(self.schedule)
        host.engine.grade_set_preview_ready.connect(self._accept)

    def schedule(self, _texture: int = 0) -> None:
        if self._needs_live_preview():
            key = self._host.engine.grade_set_preview_key
            if not self._idle.isActive() or self._scheduled_key != key:
                self._scheduled_key = key
                self._idle.start()

    def _needs_live_preview(self) -> bool:
        host = self._host
        return (not self._closed and (self._request is None or self._request.write_id is not None)
                and host.current_page == "Color"
                and host.grade_set_panel.isVisible() and host._active_grade_set_image() is not None
                and (self._snapshot is None or self._snapshot[0] != host.engine.grade_set_preview_key))

    def _capture_if_visible(self) -> None:
        if self._needs_live_preview():
            self._host.engine.request_grade_set_preview()

    @staticmethod
    def file_revision(path: str | None) -> FileRevision | None:
        try:
            return GradedThumbnailStamp.revision(path) if path else None
        except OSError:
            return None

    def photo_opened(self, path: str, source_revision: FileRevision | None,
                     sidecar: str | None, sidecar_revision: FileRevision | None) -> None:
        source = nfc(path)
        self._loaded_source = (source, source_revision) if source_revision is not None else None
        self._saved_stamp = None
        self._cache_after_save = None
        if sidecar is not None and sidecar_revision is not None and self.file_revision(sidecar) == sidecar_revision:
            self._record_saved_stamp(source, sidecar, sidecar_revision)

    def leave_page(self) -> None:
        self._idle.stop()
        host = self._host
        if not host._has_grade_set_photo():
            return
        host.raw_panel.end_interaction()
        if host.edit_project_dirty or host.edit_project_io.has_pending_archive_write():
            self._cache_after_save = nfc(host.current_file_path)
            host._schedule_autosave(0)
        else:
            self.flush()

    def _record_saved_stamp(self, source: str, sidecar: str, sidecar_revision: FileRevision) -> None:
        if self._loaded_source is not None and self._loaded_source[0] == source:
            self._saved_stamp = GradedThumbnailStamp(source, sidecar, self._loaded_source[1], sidecar_revision)

    def after_save(self, path: str, sidecar: str) -> None:
        source = nfc(path)
        revision = self.file_revision(sidecar)
        if revision is not None:
            self._record_saved_stamp(source, sidecar, revision)
        if self._cache_after_save == source:
            self._cache_after_save = None
            if self._host.current_page != "Color":
                self.flush()

    def flush(self, resume: Callable[[], object] | None = None, *, wait_for_cache: bool = False) -> bool:
        """Queue a final snapshot. Source teardown can wait on the bounded callback."""
        host = self._host
        key = host.engine.grade_set_preview_key
        stamp = self._saved_stamp
        if (self._closed or stamp is None or stamp.source != nfc(host.current_file_path)
                or stamp.sidecar != host._current_edit_project_filepath
                or (key, stamp) == self._completed or key == self._resuming):
            return True
        if self._request is not None and (self._request.key, self._request.stamp) == (key, stamp):
            if self._request.write_id is not None and not wait_for_cache:
                return True
            if resume is not None:
                self._request.resume = resume
                self._request.wait_for_cache = wait_for_cache
            return False
        self._idle.stop()
        pixels = self._snapshot[1] if self._snapshot is not None and self._snapshot[0] == key else None
        continuation = resume if wait_for_cache or pixels is None else None
        self._request = _CacheRequest(key, stamp, continuation, wait_for_cache)
        self._deadline.start()
        if pixels is not None:
            self._write(self._request, pixels)
            return not wait_for_cache
        elif not host.engine.request_grade_set_preview():
            self._deadline.stop()
            self._request = None
            return True
        return False

    def _accept(self, key: tuple[int, int], pixels: ndarray | None) -> None:
        if self._closed:
            return
        host = self._host
        if pixels is not None and key == host.engine.grade_set_preview_key and host._has_grade_set_photo():
            self._snapshot = (key, pixels)
            height, width = pixels.shape[:2]
            thumbnail = QImage(pixels.data, width, height, pixels.strides[0], QImage.Format.Format_RGB888)
            host.grade_set_panel.set_graded_thumbnail(host.current_file_path, thumbnail)

        request = self._request
        if request is not None and request.key == key and request.write_id is None:
            if pixels is None:
                self._finish()
            else:
                self._write(request, pixels)
                if not request.wait_for_cache and self._request is request:
                    self._resume(request)

    def _write(self, request: _CacheRequest, pixels: ndarray) -> None:
        facade = self._host.library_panel._native_transport._runtime()
        if facade is None:
            self._finish()
            return
        request.write_id = uuid4().hex
        stamp = request.stamp
        facade.run(
            key=f"grade-preview:{stamp.source}", request_id=request.write_id,
            work=lambda: facade.cache_graded_thumbnail(stamp, pixels),
            listener=lambda outcome: self._written.emit(
                outcome.request_id, stamp.source, outcome.ok and bool(outcome.result)))

    def _write_finished(self, request_id: str, path: str, success: bool) -> None:
        if self._closed:
            return
        if success:
            self._host.library_panel.invalidate_image_grade_badge(path)
        if self._request is not None and self._request.write_id == request_id:
            self._finish(success=success)

    def _finish(self, *, success: bool = False) -> None:
        self._deadline.stop()
        request, self._request = self._request, None
        if request is None:
            return
        if success:
            self._completed = (request.key, request.stamp)
        self._resume(request)
        self.schedule()

    def _resume(self, request: _CacheRequest) -> None:
        resume, request.resume = request.resume, None
        if resume is None:
            return

        def run() -> None:
            if not self._closed:
                self._resuming = request.key
                try:
                    resume()
                finally:
                    self._resuming = None

        QTimer.singleShot(0, run)

    def shutdown(self) -> None:
        self._closed = True
        self._idle.stop()
        self._deadline.stop()
        self._request = None
