"""CPU frame adapters for handing scene output to preview/timeline clients."""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading

from ..core.contracts import RenderedFrame


_QT_APPLICATION = None
#: Serializes the QApplication check-then-create below.  ``_svg_to_rgba`` runs
#: on render worker threads (ThreadPoolExecutor workers, timeline preview and
#: export workers); an unlocked ``instance() or QApplication([])`` let two
#: worker threads each construct a QApplication (Qt fatal: only one may exist)
#: and could race the host main thread's own startup (AUD-29908).
_QT_APPLICATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class RgbaFrame:
    frame: int
    width: int
    height: int
    data: bytes
    pixel_format: str = "rgba8"

    def __post_init__(self) -> None:
        if self.pixel_format != "rgba8":
            raise ValueError("RgbaFrame pixel_format must be rgba8")
        if len(self.data) != self.width * self.height * 4:
            raise ValueError("RGBA frame data length does not match dimensions")


class FrameAdapter:
    """Convert deterministic SVG frames to RGBA without a browser dependency."""

    @staticmethod
    def from_rendered_frame(rendered: RenderedFrame) -> RgbaFrame:
        if rendered.pixel_format == "rgba8":
            return RgbaFrame(rendered.frame, rendered.width, rendered.height, rendered.data)
        if rendered.pixel_format != "svg":
            raise ValueError(f"unsupported frame format: {rendered.pixel_format}")
        return FrameAdapter._svg_to_rgba(rendered)

    @staticmethod
    def _svg_to_rgba(rendered: RenderedFrame) -> RgbaFrame:
        # Lazy Qt imports keep the render contract usable in headless workers
        # and avoid importing any QWidget/WebEngine UI classes.
        global _QT_APPLICATION
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import QByteArray, QRectF
        from PyQt6.QtGui import QColor, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtWidgets import QApplication

        # QSvgRenderer requires a GUI application. Keep one process-local and
        # headless; this is a CPU image adapter, never a QWidget/WebEngine
        # preview and never an OpenGL context. QApplication is intentional:
        # it remains compatible when a host opens the editor afterwards.
        # The check-then-create is serialized so concurrent worker threads can
        # never construct two instances (AUD-29908); an existing instance —
        # e.g. the host main thread's — is always preferred over creating one.
        with _QT_APPLICATION_LOCK:
            _QT_APPLICATION = QApplication.instance() or QApplication([])

        image = QImage(rendered.width, rendered.height, QImage.Format.Format_RGBA8888)
        image.fill(QColor(0, 0, 0, 0))
        renderer = QSvgRenderer(QByteArray(rendered.data))
        if not renderer.isValid():
            raise ValueError("SVG frame is invalid")
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0.0, 0.0, float(rendered.width), float(rendered.height)))
        painter.end()
        # QImage bytes may be backed by a temporary image; copy before return.
        data = bytes(image.bits().asstring(image.sizeInBytes()))
        return RgbaFrame(rendered.frame, rendered.width, rendered.height, data)
