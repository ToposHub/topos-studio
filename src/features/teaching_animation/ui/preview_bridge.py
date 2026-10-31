"""Lazy WebEngine preview bridge contract.

No Qt/WebEngine class is imported at module import time.  Phase 0 only needs
to prove that a local HTML preview can consume the exact SVG bytes produced by
the deterministic renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ..core.contracts import RenderedFrame


@dataclass
class PreviewBridge:
    """Transport-neutral bridge suitable for a later QWebChannel adapter."""

    _last_frame: RenderedFrame | None = None

    def update_frame(self, frame: RenderedFrame) -> str:
        if frame.pixel_format == "svg":
            self._last_frame = frame
            svg = frame.data.decode("utf-8")
            return f'<html><body style="margin:0">{svg}</body></html>'
        if frame.pixel_format == "rgba8":
            # An HTML scene renders to RGBA through the pinned browser for
            # delivery.  The editor's interactive HTML browser is a separate
            # surface; this transport still accepts rgba8 for compatibility
            # with callers that use the frame bridge instead of the editor.
            self._last_frame = frame
            return (
                '<html><body style="margin:0;background:#111827;color:#9ca3af;'
                'font-family:sans-serif;display:flex;align-items:center;'
                'justify-content:center;height:100vh">'
                '<p>HTML motion layer (rendered offscreen)</p></body></html>'
            )
        raise ValueError(f"preview bridge does not support {frame.pixel_format!r}")

    @property
    def last_frame(self) -> RenderedFrame | None:
        return self._last_frame


class WebEnginePreviewBridge:
    """Concrete QWebEngineView/QWebChannel adapter for local scene preview.

    Qt WebEngine is intentionally imported only when this class is
    instantiated.  Registering the feature module or importing this file
    therefore remains safe for products that do not ship WebEngine.
    """

    def __init__(self, parent: Any = None) -> None:
        from PyQt6.QtCore import QUrl, QObject, pyqtSignal, pyqtSlot
        from PyQt6.QtWebChannel import QWebChannel
        from PyQt6.QtWebEngineWidgets import QWebEngineView

        class _ChannelObject(QObject):
            ready = pyqtSignal()

            @pyqtSlot()
            def notifyReady(self) -> None:
                self.ready.emit()

        self._view = QWebEngineView(parent)
        self._channel = QWebChannel(self._view.page())
        self._channel_object = _ChannelObject(self._view)
        self._channel.registerObject("teachingAnimationBridge", self._channel_object)
        self._view.page().setWebChannel(self._channel)

        self._transport = PreviewBridge()
        self._pending_frame: RenderedFrame | None = None
        self._ready = False
        self._channel_object.ready.connect(self._on_channel_ready)
        self._view.setHtml(_PREVIEW_DOCUMENT, QUrl("about:blank"))

    @property
    def view(self) -> Any:
        """Return the owned QWebEngineView for host layout integration."""
        return self._view

    @property
    def channel(self) -> Any:
        """Return the owned QWebChannel for diagnostics/tests."""
        return self._channel

    @property
    def channel_ready(self) -> Any:
        """Return the Qt signal emitted after JavaScript connects to QWebChannel."""
        return self._channel_object.ready

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def last_frame(self) -> RenderedFrame | None:
        return self._transport.last_frame

    def update_frame(self, frame: RenderedFrame) -> str:
        """Send the exact deterministic SVG frame into the WebEngine DOM."""
        html = self._transport.update_frame(frame)
        self._pending_frame = frame
        if self._ready:
            self._send_frame(frame)
        return html

    def close(self) -> None:
        """Release the browser widget and channel-owned Qt resources."""
        self._view.close()
        self._view.deleteLater()

    def _on_channel_ready(self) -> None:
        self._ready = True
        if self._pending_frame is not None:
            self._send_frame(self._pending_frame)

    def _send_frame(self, frame: RenderedFrame) -> None:
        svg = frame.data.decode("utf-8")
        svg_literal = json.dumps(svg, ensure_ascii=False)
        script = (
            "window.toposTeachingAnimation.setFrame("
            f"{svg_literal}, {frame.frame});"
        )
        self._view.page().runJavaScript(script)


_PREVIEW_DOCUMENT = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body style="margin:0;background:#111827;overflow:hidden">
  <div id="scene" data-frame=""></div>
  <script>
    window.toposTeachingAnimation = {
      setFrame: function(svg, frame) {
        const scene = document.getElementById('scene');
        scene.innerHTML = svg;
        scene.dataset.frame = String(frame);
      }
    };
    new QWebChannel(qt.webChannelTransport, function(channel) {
      channel.objects.teachingAnimationBridge.notifyReady();
    });
  </script>
</body>
</html>"""
