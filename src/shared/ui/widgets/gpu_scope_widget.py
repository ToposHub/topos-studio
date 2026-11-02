"""GPU scope presenter widget.

This is a *display-only* ``QOpenGLWidget``.  It never runs the scope
analysis shader, never creates an analysis FBO, and never calls
``read()``.  It only blits a texture produced by ``AsyncRenderer`` (which
lives in the shared render-thread context) into a framebuffer it owns,
then lets Qt composite the result.

Safety model (see docs/gpu_scope_implementation_plan_2026-08-05.md §7
"Presenter 的实现决策"):

1. No analysis FBO, no ``read()``, no scope shader on the UI thread.
2. Uses the global GL mutex with ``tryLock``: if the renderer is busy,
   keep the last frame and return immediately — never block the GUI or
   spin a repaint loop.
3. Uses PyOpenGL (``OpenGL.GL``) for the blit path, exactly like the
   production-verified ``ProgramViewer``.  ``QOpenGLFunctions_4_1_Core``
   is intentionally avoided because the PyQt6 binding omits several
   framebuffer entry points (notably ``glGenFramebuffers``), which made
   the first ``paintGL`` raise ``AttributeError`` and silently never
   present a frame.
4. The renderer publishes a ``ScopeGpuFrame`` carrying only a texture id;
   the presenter validates it with ``glIsTexture`` before use.
5. Any sharing failure degrades to "keep last frame / empty"; it never
   copies pixels back to the CPU during playback.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QSurfaceFormat, QPaintEvent, QResizeEvent
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QWidget

# PyOpenGL provides a complete, driver-resolved binding for the framebuffer
# / blit entry points this presenter needs.  It operates on whatever GL
# context is current when the calls are made (the widget's own context
# inside initializeGL/paintGL/teardownGL), matching the ProgramViewer model.
from OpenGL.GL import (
    GL_TEXTURE_2D, GL_READ_FRAMEBUFFER, GL_DRAW_FRAMEBUFFER,
    GL_COLOR_ATTACHMENT0, GL_COLOR_BUFFER_BIT, GL_LINEAR,
    GL_FRAMEBUFFER_COMPLETE, GL_NO_ERROR,
    glGenFramebuffers, glDeleteFramebuffers, glBindFramebuffer,
    glFramebufferTexture2D, glCheckFramebufferStatus, glIsTexture,
    glBlitFramebuffer, glGetError,
)

logger = logging.getLogger(__name__)


def _scope_diag():
    """S01 呈现段诊断（可关闭；纯计数，不在 GL 路径加锁外开销）。"""
    from src.features.color.core.scope_diagnostics import scope_diagnostics
    return scope_diagnostics


# Default format: compatibility with the rest of the app (4.1 core surface).
_DEFAULT_FMT = QSurfaceFormat.defaultFormat()

# Target marker colours (CPU VectorScopeWidget._get_target_color parity).
_VS_TARGET_COLORS = {
    'R': QColor(255, 80, 80),
    'G': QColor(80, 255, 80),
    'B': QColor(80, 80, 255),
    'C': QColor(80, 255, 255),
    'M': QColor(255, 80, 255),
    'Y': QColor(255, 255, 80),
}


def draw_vectorscope_targets(painter: QPainter, cx: float, cy: float,
                             radius: float, *, reference_style: bool = False) -> None:
    """Draw the six 75%/100% saturation target boxes + labels.

    Pure QPainter annotation so it can render into a QImage in tests.
    Mapping matches the CPU widget: ``x = cx + cb * 2r``、
    ``y = cy - cr * 2r``(100% 饱和度落在 ``radius`` 圆上)。

    与 CPU 版的差异:GPU 烘焙刻度没有外边距(100% 环贴视图边缘),
    因此文字标签朝中心方向偏移,避免画出控件外被裁掉。
    """
    from src.shared.ui.widgets.vectorscope_widget import COLOR_TARGETS

    box_75_size, box_100_size = 6, 8
    label_offset = 12
    painter.setFont(QFont("Arial", 7))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for color_name, (cb, cr) in COLOR_TARGETS.items():
        x_100 = cx + cb * radius * 2.0
        y_100 = cy - cr * radius * 2.0
        x_75 = cx + cb * radius * 2.0 * 0.75
        y_75 = cy - cr * radius * 2.0 * 0.75
        if reference_style:
            # Broadcast-style 75% targets: open corners and a single amber
            # label leave the signal legible, including near the rim.
            painter.setPen(QPen(QColor(150, 122, 46), 1.0))
            x, y = round(x_75), round(y_75)
            for sx in (-1, 1):
                for sy in (-1, 1):
                    painter.drawLine(x + sx * 7, y + sy * 7,
                                     x + sx * 3, y + sy * 7)
                    painter.drawLine(x + sx * 7, y + sy * 7,
                                     x + sx * 7, y + sy * 3)
            painter.drawText(x + 9, y + 13, color_name)
            continue
        target_color = _VS_TARGET_COLORS.get(color_name, QColor(150, 150, 150))

        painter.setPen(QPen(target_color, 1.5))
        painter.drawRect(int(x_100 - box_100_size / 2),
                         int(y_100 - box_100_size / 2),
                         int(box_100_size), int(box_100_size))
        painter.setPen(QPen(target_color.darker(120), 1.0))
        painter.drawRect(int(x_75 - box_75_size / 2),
                         int(y_75 - box_75_size / 2),
                         int(box_75_size), int(box_75_size))

        # 标签朝中心偏移:边缘目标的文字永远留在视图内。
        to_cx, to_cy = cx - x_100, cy - y_100
        dist = math.hypot(to_cx, to_cy) or 1.0
        painter.setPen(target_color)
        painter.drawText(
            int(x_100 + to_cx / dist * label_offset),
            int(y_100 + to_cy / dist * label_offset + 4),
            color_name,
        )


class _VectorscopeTargetsOverlay(QWidget):
    """Static annotations composited by Qt above the shared GL texture."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.zoom = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = max(1.0, (min(self.width(), self.height()) - 24) / 2.0)
        draw_vectorscope_targets(painter, self.width() / 2.0,
                                 self.height() / 2.0, radius * self.zoom,
                                 reference_style=True)
        painter.end()


class GpuScopeWidget(QOpenGLWidget):
    """Presents a renderer-produced RGBA scope texture.

    The widget is given the latest ``ScopeGpuFrame`` via
    :meth:`set_scope_frame`.  ``paintGL`` blits the shared texture into the
    Qt-owned draw framebuffer (the widget's internal offscreen FBO) so Qt
    composites it.  The waveform graticule is baked into the source texture;
    the presenter adds only the small 10-bit axis labels as UI chrome.
    """

    def __init__(
        self,
        parent=None,
        *,
        preserve_aspect: bool = False,
        axis_left_px: int = 0,
        flip_y: bool = False,
        show_value_axis: bool = True,
        legend: str = '',
        show_vectorscope_targets: bool = False,
        show_histogram_axis: bool = False,
    ) -> None:
        super().__init__(parent)
        # tryLock may intentionally skip a blit during a seek/render. Qt's
        # default NoPartialUpdate invalidates the FBO before paintGL, making
        # that early return flash black. Keep the last presented pixels.
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.PartialUpdate)
        # Request a double-buffered core-profile surface matching the app.
        fmt = QSurfaceFormat(_DEFAULT_FMT)
        fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
        # Scope output textures are single-sample. The application default
        # requests MSAA, but scaling from a single-sample read FBO into a
        # multisample QOpenGLWidget target is invalid on several macOS drivers
        # and leaves the presenter black. Density displays gain nothing from
        # MSAA, so request a single-sample target explicitly.
        fmt.setSamples(0)
        self.setFormat(fmt)
        # NOTE: do NOT set WA_PaintOnScreen — QOpenGLWidget always renders
        # into an internal offscreen FBO that Qt composites; painting
        # directly to the screen is undefined for this class.
        self.setStyleSheet("background-color: #050505;")
        # Waveform displays intentionally fill the panel, matching the CPU
        # widget's drawImage(rect, ...) behavior. Vectorscope callers opt into
        # aspect preservation so its circular graticule stays circular.
        self._preserve_aspect = bool(preserve_aspect)
        self._axis_left_px = max(0, int(axis_left_px))
        self._flip_y = bool(flip_y)
        # Value-axis annotation (10-bit code labels). Histograms bake their
        # own 0/50/100% graticule into the shader, and CIE / False Color are
        # not value plots, so those presenters disable it.
        self._show_value_axis = bool(show_value_axis)
        # Optional static legend strip ('falsecolor_sdr' / 'falsecolor_hdr' /
        # 'falsecolor_zebra') drawn as UI chrome under the blitted texture.
        self._legend = str(legend)
        # Vectorscope 6-colour target boxes + labels (CPU widget parity).
        # The baked GPU graticule only has rings / crosshair / skin-tone
        # line; the targets are UI chrome drawn by this presenter.
        self._show_vectorscope_targets = bool(show_vectorscope_targets)
        self._show_histogram_axis = bool(show_histogram_axis)
        self._targets_overlay = (
            _VectorscopeTargetsOverlay(self) if show_vectorscope_targets else None
        )
        # Latest descriptor received from the renderer.  Only the texture id
        # + size are used here.
        self._frame_tex_id: Optional[int] = None
        self._frame_w: int = 0
        self._frame_h: int = 0
        # Read FBO that wraps the shared source texture (created lazily).
        self._read_fbo: Optional[int] = None
        # GL is ready after the first initializeGL; paint is a no-op before.
        self._gl_ready: bool = False
        self._mutex = None
        # P1-COLOR-06: waveform axis mode — 'code10' (full-range 1023..0,
        # legacy default), 'code10_video' (64..940 legal range), 'nits'
        # (PQ/HLG absolute luminance marks).
        self._scale_mode = 'code10'
        self._transfer = 'srgb'

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_vectorscope_zoom(self, zoom: float) -> None:
        """Keep chroma reference positions aligned with magnified samples."""
        if self._targets_overlay is not None:
            self._targets_overlay.zoom = max(1.0, float(zoom))
            self._targets_overlay.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._targets_overlay is not None:
            self._targets_overlay.setGeometry(self.rect())

    def set_scope_frame(self, tex_id: Optional[int], width: int, height: int) -> None:
        """Publish a new GPU scope texture descriptor (thread-safe via signal)."""
        self._frame_tex_id = tex_id
        self._frame_w = max(1, int(width))
        self._frame_h = max(1, int(height))
        self.update()

    def set_scale_mode(self, mode: str, transfer: str = 'srgb') -> None:
        """Choose the waveform axis annotation.

        ``mode``: 'code10' (full range 0..1023), 'code10_video' (64..940
        legal range), or 'nits' (HDR luminance marks for ``transfer``).
        """
        if mode not in ('code10', 'code10_video', 'nits'):
            return
        if mode != self._scale_mode or transfer != self._transfer:
            self._scale_mode = mode
            self._transfer = transfer
            self.update()

    def set_legend(self, legend: str) -> None:
        """Choose the legend strip ('', 'falsecolor_sdr'/'_hdr'/'_zebra')."""
        legend = str(legend)
        if legend != self._legend:
            self._legend = legend
            self.update()

    def clear(self) -> None:
        """Drop the current frame (e.g. when the panel hides)."""
        self._frame_tex_id = None
        self._frame_w = 0
        self._frame_h = 0
        self.update()

    # ------------------------------------------------------------------
    # QOpenGLWidget lifecycle
    # ------------------------------------------------------------------
    def initializeGL(self) -> None:  # pragma: no cover - GL context required
        """Mark the presenter ready and pick up the global GL mutex.

        PyOpenGL resolves functions against the current context lazily, so
        unlike ``QOpenGLFunctions`` there is no ``initializeOpenGLFunctions``
        step that can fail and leave the presenter disabled.  Scope
        rendering is optional; a failed presenter must never escape an
        exception from Qt's ``initializeGL`` callback and abort the process.
        """
        try:
            from src.shared.gpu.gl_backend import GLContextManager

            self._mutex = GLContextManager.instance().mutex
        except (ImportError, AttributeError, RuntimeError):
            self._mutex = None
        self._gl_ready = True

    def teardownGL(self) -> None:  # pragma: no cover - GL required
        """Release the widget-owned read FBO on context teardown."""
        if self._read_fbo is not None:
            try:
                glDeleteFramebuffers(1, [self._read_fbo])
            except Exception:
                pass
        self._read_fbo = None
        self._gl_ready = False

    def paintGL(self) -> None:  # pragma: no cover - GL required
        if not self._gl_ready:
            return
        if self._frame_tex_id is None:
            # PartialUpdate retains pixels deliberately on a busy-renderer
            # skip, but an explicit clear must drop the previous source.
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(5, 5, 5))
            painter.end()
            return
        # tryLock: never block the GUI. If the renderer holds the mutex,
        # keep showing the previous frame.
        mutex = getattr(self, '_mutex', None)
        if mutex is not None and not mutex.tryLock():
            _scope_diag().record("widget", "mutex_busy",
                                 tex_id=self._frame_tex_id)
            return
        try:
            # Qt's own GL compositing can leave a stale error flag in this
            # context; a stale flag makes the next PyOpenGL call raise
            # (e.g. glIsTexture reporting GL_INVALID_ENUM after returning
            # GL_TRUE) and would blank the whole frame. Purge it first.
            while glGetError() != GL_NO_ERROR:
                pass
            try:
                self._blit_shared_texture()
            except Exception as exc:
                _scope_diag().record("widget", "blit_failed",
                                     tex_id=self._frame_tex_id,
                                     detail=str(exc)[:120])
                logger.debug("GpuScopeWidget blit failed: %s", exc)
            try:
                self._paint_scope_chrome()
            except Exception as exc:
                logger.debug("GpuScopeWidget paint failed: %s", exc)
        finally:
            if mutex is not None:
                mutex.unlock()

    def _paint_scope_chrome(self) -> None:  # pragma: no cover - Qt/GL required
        """Paint UI annotations (value axis labels / legend) after the blit."""
        if self._show_value_axis and not self._preserve_aspect:
            self._paint_waveform_scale()
        if self._legend:
            self._paint_legend()
        if self._show_histogram_axis:
            self._paint_histogram_axis()

    def _paint_histogram_axis(self) -> None:
        """Draw the histogram's top 10-bit ruler in logical pixels."""
        painter = QPainter(self)
        painter.setFont(QFont("Arial", 8))
        painter.setPen(QColor(150, 122, 46))
        left, right = 20, max(21, self.width() - 10)
        for step in range(33):
            x = round(left + (right - left) * step / 32.0)
            painter.drawLine(x, 22, x, 27 if step % 4 else 30)
            if step % 4 == 0:
                code = min(1023, step * 32)
                painter.drawText(x - (12 if step == 32 else 5), 16, str(code))
        painter.end()

    def _paint_legend(self) -> None:  # pragma: no cover - Qt required
        """Draw the false color exposure-stop legend as UI chrome.

        Mirrors the CPU FalseColorWidget legend: a horizontal strip of the
        active LUT with reference marks. Pure annotation; the scope pixels
        stay in the renderer-owned GPU texture. The strip QImage is cached
        per (legend, transfer) — paintGL runs at display rate.
        """
        import numpy as np
        from PyQt6.QtGui import QImage
        from src.shared.ui.widgets.vectorscope_widget import (
            build_false_color_lut,
        )
        painter = QPainter()
        if not painter.begin(self):
            return
        w, h = self.width(), self.height()
        painter.setPen(QColor(200, 200, 200, 180))
        painter.setFont(QFont("Arial", 8))
        if self._legend == 'falsecolor_zebra':
            painter.drawText(8, 16, "Zebra")
            painter.end()
            return
        mode = 'hdr' if self._legend == 'falsecolor_hdr' else 'sdr'
        cache_key = (mode, self._transfer)
        cached = getattr(self, '_legend_strip', None)
        if cached is None or cached[0] != cache_key:
            lut = build_false_color_lut(mode, self._transfer)
            strip = np.ascontiguousarray(lut[np.newaxis, :, :])
            strip_img = QImage(
                strip.data, strip.shape[1], 1, strip.strides[0],
                QImage.Format.Format_RGB888,
            ).copy()
            self._legend_strip = (cache_key, strip_img)
        strip_img = self._legend_strip[1]
        legend_w = min(w - 16, 420)
        lx = (w - legend_w) // 2
        ly = h - 18
        painter.drawImage(QRectF(lx, ly, legend_w, 8), strip_img)
        painter.setPen(QColor(190, 190, 190, 200))
        if mode == 'hdr' and self._transfer == 'pq':
            from src.shared.ui.widgets.vectorscope_widget import (
                pq_nits_to_code,
            )
            marks = [("100", float(pq_nits_to_code(100.0))),
                     ("1k", float(pq_nits_to_code(1000.0))),
                     ("10k", 1.0)]
        elif mode == 'hdr':
            marks = [("203", 0.75), ("1000", 1.0)]
        else:
            marks = [("25", 0.25), ("50", 0.50), ("75", 0.75), ("100+", 1.0)]
        for label, code in marks:
            mx = lx + int(float(code) * legend_w)
            painter.drawLine(mx, ly - 3, mx, ly + 11)
            painter.drawText(mx - 8, ly + 22, label)
        painter.end()

    def _paint_waveform_scale(self) -> None:  # pragma: no cover - Qt/GL required
        """Draw 10-bit scale labels into the current QOpenGLWidget FBO.

        This is UI annotation only; the waveform pixels and all analysis stay
        in the renderer-owned GPU texture. Painting from ``paintGL`` ensures
        the labels land in the same Qt backing FBO as the blit.
        """
        if self._preserve_aspect or self._frame_tex_id is None:
            return
        h = self.height()
        if h <= 0:
            return
        painter = QPainter()
        if not painter.begin(self):
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setFont(QFont("Arial", 8))
        if self._scale_mode == 'nits':
            from src.shared.ui.widgets.vectorscope_widget import (
                waveform_scale_marks,
            )
            painter.setPen(QColor(255, 220, 120, 220))
            for value, label in waveform_scale_marks('nits', self._transfer):
                t = (float(value) + 0.1) / 1.3
                y = max(9, min(h - 3, round((1.0 - t) * h)))
                painter.drawText(3, y + 3, label)
                painter.drawLine(max(0, self._axis_left_px - 5), y,
                                 self._axis_left_px, y)
            painter.end()
            return
        painter.setPen(QColor(188, 149, 0, 220))
        if self._scale_mode == 'code10_video':
            # Legal range: value 0..1 maps to 64..940 on the same waveform.
            from src.shared.ui.widgets.vectorscope_widget import (
                waveform_scale_marks,
            )
            for value, label in waveform_scale_marks('code10', self._transfer):
                t = (float(value) + 0.1) / 1.3
                y = max(9, min(h - 3, round((1.0 - t) * h)))
                painter.drawText(3, y + 3, label)
                painter.drawLine(max(0, self._axis_left_px - 5), y,
                                 self._axis_left_px, y)
            painter.end()
            return
        # Code values mirror the 10-bit scale used by the reference scope.
        for code in (1023, 896, 768, 640, 512, 384, 256, 128, 0):
            value = float(code) / 1023.0
            t = (value + 0.1) / 1.3
            y = max(9, min(h - 3, round((1.0 - t) * h)))
            painter.drawText(3, y + 3, str(code))
            painter.drawLine(max(0, self._axis_left_px - 5), y,
                             self._axis_left_px, y)
        painter.end()

    def _blit_src_rect(self, fw: int, fh: int) -> tuple[int, int, int, int]:
        """Source texel rectangle (x0, y0, x1, y1, GL bottom-up) to present.

        Subclasses may present a sub-region of the frame — e.g. the CIE
        presenter zooms by blitting only the visible plot window. The
        destination letterbox is computed from the *full* frame size, so a
        subclass rect must keep the full-frame aspect ratio to avoid
        distortion.
        """
        return 0, 0, fw, fh

    def _blit_shared_texture(self) -> None:  # pragma: no cover - GL required
        """Blit the renderer's shared texture into Qt's bound framebuffer.

        QOpenGLWidget.paintGL is invoked with the widget's internal offscreen
        FBO already bound as the draw framebuffer; Qt composites it
        afterwards.  We re-bind it explicitly via ``defaultFramebufferObject``
        as the blit destination (more reliable than querying
        GL_DRAW_FRAMEBUFFER_BINDING through PyQt6's incomplete binding).
        """
        src = self._frame_tex_id
        # Validate the shared texture name is still a live GL texture.
        if not glIsTexture(src):
            _scope_diag().record("widget", "texture_not_shared", tex_id=src)
            return

        # Lazily allocate the read FBO that wraps the shared source texture.
        # PyOpenGL's glGenFramebuffers(n) returns the name directly for n == 1.
        if self._read_fbo is None:
            self._read_fbo = glGenFramebuffers(1)

        glBindFramebuffer(GL_READ_FRAMEBUFFER, self._read_fbo)
        glFramebufferTexture2D(
            GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
            GL_TEXTURE_2D, src, 0,
        )
        status = glCheckFramebufferStatus(GL_READ_FRAMEBUFFER)
        if status != GL_FRAMEBUFFER_COMPLETE:
            _scope_diag().record("widget", "fbo_incomplete",
                                 tex_id=src, detail=f"0x{status:X}")
            logger.debug("GpuScopeWidget read FBO incomplete: 0x%X", status)
            glBindFramebuffer(GL_READ_FRAMEBUFFER, 0)
            return

        # Re-bind Qt's draw framebuffer (the widget offscreen FBO) as the
        # blit destination.  defaultFramebufferObject is the robust way to
        # identify it for a QOpenGLWidget.
        draw_fbo = self.defaultFramebufferObject()
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, draw_fbo)

        # The framebuffer is expressed in device pixels, not Qt logical
        # pixels. Without this scaling, Retina displays render the scope into
        # only a quarter of the target FBO.
        dpr = self.devicePixelRatioF()
        w = max(1, round(self.width() * dpr))
        h = max(1, round(self.height() * dpr))
        fw, fh = self._frame_w, self._frame_h
        if self._preserve_aspect:
            # Vectorscope is square in data space; letterbox it so circles do
            # not become ellipses in a wide/tall dock or split view.
            inset = round(12 * dpr) if self._show_vectorscope_targets else 0
            scale = min(max(1, w - 2 * inset) / max(1, fw),
                        max(1, h - 2 * inset) / max(1, fh))
            dw = int(fw * scale)
            dh = int(fh * scale)
            dx0 = (w - dw) // 2
            dy0 = (h - dh) // 2
        else:
            # Waveform axes are semantic value/time axes, not a photograph;
            # stretch the complete texture to the available panel exactly as
            # the legacy CPU widget does. This removes the large black bars
            # that previously compressed the visible waveform into a strip.
            dx0 = min(round(self._axis_left_px * dpr), max(0, w - 1))
            dy0, dw, dh = 0, max(1, w - dx0), h
            if self._show_histogram_axis:
                dx0 = min(round(20 * dpr), max(0, w - 1))
                dy0 = round(8 * dpr)
                dw = max(1, w - dx0 - round(10 * dpr))
                dh = max(1, h - dy0 - round(30 * dpr))
        src_x0, src_y0, src_x1, src_y1 = self._blit_src_rect(fw, fh)
        if self._flip_y:
            src_y0, src_y1 = src_y1, src_y0
        glBlitFramebuffer(
            # The waveform shader emits 0% at the GL/FBO bottom and 100% at
            # the top, which is already the screen-space value orientation
            # expected by the 1023..0 labels.  Only callers that explicitly
            # request flip_y (currently none of the built-in scopes) reverse
            # the source rectangle.
            src_x0, src_y0, src_x1, src_y1,
            dx0, dy0, dx0 + dw, dy0 + dh,
            GL_COLOR_BUFFER_BIT,
            GL_LINEAR,
        )
        # Restore bindings so other paint code is unaffected.  Qt expects the
        # default framebuffer to remain bound as both read and draw target.
        glBindFramebuffer(GL_READ_FRAMEBUFFER, draw_fbo)
