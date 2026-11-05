"""Read-only source thumbnail presentation for the Color graph.

Uses the existing asynchronous ThumbnailProvider. The GUI receives a source
still for node cards and emits it for LUT candidate preview preparation; graph
output thumbnails are evaluated by a separate backend task.
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap

from src.shared.i18n.i18n import I18N
from src.shared.media.thumbnail_provider import ThumbnailProvider


class ColorNodeSourcePreview(QObject):
    """Observe the active media identity and request one reusable source still."""

    source_thumbnail_changed = pyqtSignal(object)

    def __init__(self, host: Any, provider: Any = None) -> None:
        super().__init__(host)
        self._host = host
        self._provider = provider if provider is not None else ThumbnailProvider.instance()
        self._path = ""
        self._ready = False
        self._provider.thumbnail_ready.connect(self._thumbnail_ready)
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self) -> None:
        """Identity polling only; extraction runs in the provider's worker pool."""
        path = str(getattr(self._host, "current_file_path", "") or "")
        panel = self._host.color_node_panel
        if path != self._path:
            self._path = path
            self._ready = False
            panel.view._thumbnails.clear()
            panel.view.set_source_thumbnail(None)
            self.source_thumbnail_changed.emit(None)
        if not path or self._ready or not panel.isVisible():
            return
        cached = self._provider.get_thumbnail(path, 160)
        if cached is not None and not cached.isNull():
            self._thumbnail_ready(path, cached)

    def _thumbnail_ready(self, path: str, pixmap: QPixmap) -> None:
        if path != self._path or path != str(getattr(self._host, "current_file_path", "") or "") or pixmap.isNull():
            return
        self._ready = True
        description = I18N.tr(
            "color_nodes.source_preview_only",
            "Source preview — waiting for evaluated node output.",
        )
        self._host.color_node_panel.view.set_source_thumbnail(pixmap, description)
        self.source_thumbnail_changed.emit(pixmap)
