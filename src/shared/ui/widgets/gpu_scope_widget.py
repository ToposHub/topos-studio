"""GPU scope presenter widget.

This is a *display-only* ``QOpenGLWidget``.  It never runs the scope
analysis shader, never creates an analysis FBO, and never calls
``read()``.  It copies the texture produced by ``AsyncRenderer`` in the
shared render-thread context. The widget presents its local snapshot with
an optional display backdrop and lets Qt composite the result.

Safety model:

1. No analysis FBO, pixel readback, or statistical shader on the UI thread.
2. Copy new shared output only under the global GL mutex (tryLock), using a
   revocable publication and server-side GPU producer/consumer fences.
3. Redraw the presenter-owned GPU snapshot without the renderer mutex. It
   survives producer allocation changes and Qt framebuffer recreation.
4. Invalid/unfinished publications and copy failures keep the previous
   snapshot; an OpenGL numeric texture name alone is never a lifetime check.
5. PyOpenGL supplies the framebuffer entry points missing from PyQt bindings.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QSurfaceFormat, QPaintEvent, QResizeEvent, QVector4D
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QWidget
from src.features.color.core.scope_types import waveform_plot_range
from src.shared.gpu.scope_publication import ScopePublication

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
    GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE, GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_MAG_FILTER, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    GL_CLAMP_TO_EDGE, GL_NEAREST,
    glGenTextures, glDeleteTextures, glBindTexture, glTexImage2D, glTexParameteri,
    GL_SCISSOR_TEST, glIsEnabled, glDisable, glEnable,
    GL_BLEND, GL_TEXTURE0, GL_TRIANGLE_STRIP, glActiveTexture, glDrawArrays, glViewport,
)

logger = logging.getLogger(__name__)

_BACKDROP_VERTEX = """#version 330 core
uniform vec4 uv_rect;
out vec2 uv;
void main() {
    vec2 corner = vec2(float(gl_VertexID % 2), float(gl_VertexID / 2));
    uv = mix(uv_rect.xy, uv_rect.zw, corner);
    gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}
"""
_BACKDROP_FRAGMENT = """#version 330 core
uniform sampler2D plot;
uniform float background;
uniform float plot_floor;
in vec2 uv;
out vec4 pixel;
void main() {
    vec3 signal = max(texture(plot, uv).rgb - vec3(plot_floor), vec3(0.0))
                  / (1.0 - plot_floor);
    pixel = vec4(mix(vec3(background), vec3(1.0), signal), 1.0);
}
"""


def _blit_scope_pixels(source: tuple[int, int, int, int],
                       target: tuple[int, int, int, int], filtering: int) -> None:
    """Qt paint state must not clip a complete scope snapshot or redraw."""
    clipped = glIsEnabled(GL_SCISSOR_TEST)
    if clipped:
        glDisable(GL_SCISSOR_TEST)
    try:
        glBlitFramebuffer(*source, *target, GL_COLOR_BUFFER_BIT, filtering)
    finally:
        if clipped:
            glEnable(GL_SCISSOR_TEST)


def _scope_ruler_font() -> QFont:
    font = QFont("Arial")
    font.setPixelSize(12)
    font.setWeight(QFont.Weight.Medium)
    return font


def draw_waveform_ruler(
    painter: QPainter, height: int, axis_left_px: int,
    scale_mode: str = 'code10', transfer: str = 'srgb',
    *, plot_width: int = 0, show_excursions: bool = True,
) -> list[tuple[str, QRectF, int]]:
    """Paint readable value labels; return their bounds and exact tick rows."""
    if height <= 0 or axis_left_px <= 8:
        return []
    painter.save()
    painter.setFont(_scope_ruler_font())
    painter.setPen(QColor(190, 160, 70))
    metrics = painter.fontMetrics()
    if scale_mode == 'percent':
        marks = [(value / 100.0, f"{value}%") for value in range(0, 101, 25)]
    elif scale_mode in ('nits', 'code10_video'):
        from src.shared.ui.widgets.vectorscope_widget import waveform_scale_marks
        marks = waveform_scale_marks('nits' if scale_mode == 'nits' else 'code10', transfer)
        if scale_mode == 'nits':
            marks = [(0.0, '0'), *marks]
    else:
        marks = [(code / 1023.0, str(code))
                 for code in (1023, 896, 768, 640, 512, 384, 256, 128, 0)]
    marks = sorted(marks, reverse=True)
    # Reserve both endpoints before filling intermediate rows. A short plot
    # drops labels rather than moving ticks away from the measured values.
    priority = marks[:1] + marks[-1:] + marks[1:-1] if len(marks) > 1 else marks
    labels: list[tuple[str, QRectF, int]] = []
    text_height = float(metrics.height())
    range_min, range_max = waveform_plot_range(show_excursions)
    for value, label in priority:
        label = label.removesuffix(' nits')
        y = min(height - 1, round((1.0 - (float(value) - range_min) / (range_max - range_min)) * height))
        rect = QRectF(2.0, max(0.0, min(height - text_height, y - text_height / 2.0)),
                      float(axis_left_px - 10), text_height)
        if any(rect.adjusted(0.0, -2.0, 0.0, 2.0).intersects(other) for _, other, _ in labels):
            continue
        labels.append((label, rect, y))
    labels.sort(key=lambda entry: entry[2])
    for label, rect, y in labels:
        if plot_width > axis_left_px:
            painter.setPen(QColor(190, 160, 70, 84))
            painter.drawLine(axis_left_px + 1, y, plot_width - 1, y)
        painter.setPen(QColor(190, 160, 70))
        painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
        painter.drawLine(axis_left_px - 5, y, axis_left_px, y)
    if scale_mode == 'nits' and labels:
        gaps = [(0.0, labels[0][1].top())]
        gaps.extend((before.bottom(), after.top())
                    for (_, before, _), (_, after, _) in zip(labels, labels[1:]))
        for start, end in gaps:
            if end - start >= text_height + 4:
                painter.drawText(QRectF(2, start + 2, axis_left_px - 10, text_height),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, 'nits')
                break
    painter.restore()
    return labels


def draw_histogram_ruler(painter: QPainter, width: int) -> list[tuple[str, QRectF, float]]:
    """Paint the top code ruler in the same 20px/10px margins as the blit."""
    if width <= 0:
        return []
    painter.save()
    painter.setFont(_scope_ruler_font())
    painter.setPen(QColor(190, 160, 70))
    metrics = painter.fontMetrics()
    left, right = 20.0, float(max(21, width - 10))
    labels: list[tuple[str, QRectF, float]] = []
    candidates = (
        (0, 128, 256, 384, 512, 640, 768, 896, 1023),
        (0, 256, 512, 768, 1023), (0, 512, 1023), (0, 1023), (0,),
    )
    for codes in candidates:
        labels = []
        for code in codes:
            label = str(code)
            x = left + (right - left) * code / 1023.0
            text_width = float(metrics.horizontalAdvance(label))
            text_x = max(2.0, min(width - 2.0 - text_width, x - text_width / 2.0))
            rect = QRectF(text_x, 2.0, text_width, float(metrics.height()))
            labels.append((label, rect, x))
        if all(a[1].right() + 8.0 <= b[1].left() for a, b in zip(labels, labels[1:])):
            break
    for step in range(33):
        code = min(1023, step * 32)
        x = round(left + (right - left) * code / 1023.0)
        painter.drawLine(x, 22, x, 29 if step % 4 == 0 else 25)
    for label, rect, _ in labels:
        painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, label)
    painter.restore()
    return labels


def _scope_diag():
    """S01 呈现段诊断（可关闭；纯计数，不在 GL 路径加锁外开销）。"""
    from src.features.color.core.scope_diagnostics import scope_diagnostics
    return scope_diagnostics



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
    composites it. The waveform graticule is drawn with the selected ruler;
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
        # Resolve at construction time: this module can be imported before
        # main() installs the core-profile default. A captured import-time
        # format creates an unshared legacy context on macOS.
        fmt = QSurfaceFormat(QSurfaceFormat.defaultFormat())
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
        self._background_color = QColor(5, 5, 5)
        self._plot_floor = 0.0
        self._composite_plot = False
        self._backdrop_program: QOpenGLShaderProgram | None = None
        self._backdrop_vao: QOpenGLVertexArrayObject | None = None
        self._backdrop_available = True
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
        self._frame_publication: Optional[ScopePublication] = None
        self._copy_pending = False
        # Display-only GPU snapshot survives producer reallocations and Qt's
        # framebuffer recreation during a live window resize.
        self._cache_tex: Optional[int] = None
        self._cache_fbo: Optional[int] = None
        self._cache_size = (0, 0)
        self._cache_valid = False
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
        self._show_excursions = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_scope_background(self, color: QColor, *, tint_plot: bool = True, plot_floor: int = 0) -> None:
        self._background_color = QColor(color)
        self._plot_floor = plot_floor / 255.0
        self._composite_plot = tint_plot and (color.red() != 0 or plot_floor != 0)
        self.setStyleSheet(f"background-color: {color.name()};")
        self.update()

    def set_vectorscope_zoom(self, zoom: float) -> None:
        """Keep chroma reference positions aligned with magnified samples."""
        if self._targets_overlay is not None:
            self._targets_overlay.zoom = max(1.0, float(zoom))
            self._targets_overlay.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._targets_overlay is not None:
            self._targets_overlay.setGeometry(self.rect())

    def set_scope_frame(
        self, tex_id: Optional[int], width: int, height: int, *,
        publication: Optional[ScopePublication] = None,
    ) -> None:
        """Queue a shared output for one GPU copy on the next paint.

        Production callers supply a publication. Legacy callers without one
        must keep their externally owned texture live until the copy.
        """
        self._frame_tex_id = tex_id
        self._frame_w = max(1, int(width))
        self._frame_h = max(1, int(height))
        self._frame_publication = publication
        self._copy_pending = tex_id is not None
        self.update()

    def set_scale_mode(self, mode: str, transfer: str = 'srgb') -> None:
        """Choose the waveform axis annotation.

        ``mode``: 'code10' (full range 0..1023), 'code10_video' (64..940
        legal range), or 'nits' (HDR luminance marks for ``transfer``).
        """
        if mode not in ('percent', 'code10', 'code10_video', 'nits'):
            return
        if mode != self._scale_mode or transfer != self._transfer:
            self._scale_mode = mode
            self._transfer = transfer
            self.update()

    def set_legend(self, legend: str, *, transfer: str = 'srgb') -> None:
        """Choose the legend strip ('', 'falsecolor_sdr'/'_hdr'/'_zebra')."""
        legend = str(legend)
        if legend != self._legend or transfer != self._transfer:
            self._legend = legend
            self._transfer = transfer
            self.update()

    def set_show_excursions(self, show: bool) -> None:
        if show != self._show_excursions:
            self._show_excursions = show
            self.clear()

    def clear(self) -> None:
        """Drop the current frame (e.g. when the panel hides)."""
        self._frame_tex_id = None
        self._frame_w = 0
        self._frame_h = 0
        self._frame_publication = None
        self._copy_pending = False
        self._cache_valid = False
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
        self.context().aboutToBeDestroyed.connect(self._cleanup_context)

    def _cleanup_context(self) -> None:
        """Dispose presenter-owned GL objects with their own context current."""
        self.makeCurrent()
        try:
            self.teardownGL()
        finally:
            self.doneCurrent()

    def teardownGL(self) -> None:  # pragma: no cover - GL required
        """Release the widget-owned read FBO on context teardown."""
        if self._backdrop_vao is not None:
            self._backdrop_vao.destroy()
            self._backdrop_vao = None
        self._backdrop_program = None
        self._backdrop_available = True
        if self._cache_fbo is not None:
            glDeleteFramebuffers(1, [self._cache_fbo])
        if self._cache_tex is not None:
            glDeleteTextures([self._cache_tex])
        self._cache_fbo = self._cache_tex = None
        self._cache_size = (0, 0)
        self._cache_valid = False
        self._copy_pending = self._frame_tex_id is not None
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
        # A resized QOpenGLWidget has a new, empty FBO. Always initialise it;
        # retaining old Qt FBO pixels alone cannot protect live resize.
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background_color)
        painter.end()
        if self._frame_tex_id is None:
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
        except Exception:
            logger.debug('Scope display failed', exc_info=True)

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
        draw_histogram_ruler(painter, self.width())
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
        elif mode == 'hdr' and self._transfer == 'hlg':
            marks = [("203", 0.75), ("1000", 1.0)]
        elif mode == 'hdr':
            marks = [("50", 0.5), ("100", 1.0)]
        else:
            marks = [("25", 0.25), ("50", 0.50), ("75", 0.75), ("100+", 1.0)]
        for label, code in marks:
            mx = lx + int(float(code) * legend_w)
            painter.drawLine(mx, ly - 3, mx, ly + 11)
            painter.drawText(mx - 8, ly + 22, label)
        painter.end()

    def _paint_waveform_scale(self) -> None:  # pragma: no cover - Qt/GL required
        """Draw waveform labels and reference lines into the current FBO.

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
        draw_waveform_ruler(painter, h, self._axis_left_px, self._scale_mode, self._transfer,
                            plot_width=self.width(), show_excursions=self._show_excursions)
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

    def _allocate_cache(self, size: tuple[int, int]) -> tuple[int, int]:
        """Allocate a candidate; keep the previous snapshot until copy succeeds."""
        tex = glGenTextures(1)
        fbo = None
        try:
            fbo = glGenFramebuffers(1)
            glBindTexture(GL_TEXTURE_2D, tex)
            for parameter, value in (
                (GL_TEXTURE_MIN_FILTER, GL_LINEAR), (GL_TEXTURE_MAG_FILTER, GL_LINEAR),
                (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE), (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE),
            ):
                glTexParameteri(GL_TEXTURE_2D, parameter, value)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, *size, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo)
            glFramebufferTexture2D(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
            if glCheckFramebufferStatus(GL_DRAW_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Scope cache framebuffer incomplete')
        except Exception:
            if fbo is not None:
                glDeleteFramebuffers(1, [fbo])
            glDeleteTextures([tex])
            raise
        finally:
            glBindTexture(GL_TEXTURE_2D, 0)
        return tex, fbo

    def _copy_shared_texture(self) -> None:
        """Copy a live shared allocation once, while holding the GL mutex."""
        publication = self._frame_publication
        if publication is not None and not publication.wait_ready():
            self._copy_pending = False
            _scope_diag().record('widget', 'publication_retired', tex_id=self._frame_tex_id)
            return
        src = self._frame_tex_id
        if not glIsTexture(src):
            self._copy_pending = False
            _scope_diag().record('widget', 'texture_not_shared', tex_id=src)
            return
        if self._read_fbo is None:
            self._read_fbo = glGenFramebuffers(1)
        copied = False
        candidate = None
        size = (self._frame_w, self._frame_h)
        try:
            glBindFramebuffer(GL_READ_FRAMEBUFFER, self._read_fbo)
            glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, src, 0)
            if glCheckFramebufferStatus(GL_READ_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Shared scope framebuffer incomplete')
            if size != self._cache_size or self._cache_tex is None:
                candidate = self._allocate_cache(size)
            target_fbo = candidate[1] if candidate is not None else self._cache_fbo
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, target_fbo)
            _blit_scope_pixels((0, 0, *size), (0, 0, *size), GL_NEAREST)
            copied = True
            if candidate is not None:
                if self._cache_fbo is not None:
                    glDeleteFramebuffers(1, [self._cache_fbo])
                if self._cache_tex is not None:
                    glDeleteTextures([self._cache_tex])
                self._cache_tex, self._cache_fbo = candidate
                self._cache_size = size
                candidate = None
            self._cache_valid = True
            self._copy_pending = False
        finally:
            if candidate is not None:
                glDeleteFramebuffers(1, [candidate[1]])
                glDeleteTextures([candidate[0]])
            # Do not keep the producer texture attached after its lifetime ends.
            glBindFramebuffer(GL_READ_FRAMEBUFFER, self._read_fbo)
            glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0)
            try:
                if copied and publication is not None:
                    publication.copied()
            finally:
                target = self.defaultFramebufferObject()
                glBindFramebuffer(GL_READ_FRAMEBUFFER, target)
                glBindFramebuffer(GL_DRAW_FRAMEBUFFER, target)

    def _blit_shared_texture(self) -> None:
        """Refresh the local snapshot if possible, then redraw without a lock."""
        if self._copy_pending:
            mutex = self._mutex
            locked = mutex is None or mutex.tryLock()
            if locked:
                try:
                    self._copy_shared_texture()
                except Exception:
                    logger.debug('Scope cache refresh failed; retaining snapshot', exc_info=True)
                finally:
                    if mutex is not None:
                        mutex.unlock()
            else:
                _scope_diag().record('widget', 'mutex_busy', tex_id=self._frame_tex_id)
        if not self._cache_valid:
            return
        self._blit_cached_texture()

    def _blit_cached_texture(self) -> None:
        """Scale the UI-owned GPU snapshot into Qt's current framebuffer."""
        draw_fbo = self.defaultFramebufferObject()
        glBindFramebuffer(GL_READ_FRAMEBUFFER, self._cache_fbo)
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, draw_fbo)

        # The framebuffer is expressed in device pixels, not Qt logical
        # pixels. Without this scaling, Retina displays render the scope into
        # only a quarter of the target FBO.
        dpr = self.devicePixelRatioF()
        w = max(1, round(self.width() * dpr))
        h = max(1, round(self.height() * dpr))
        fw, fh = self._cache_size
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
        try:
            if self._composite_plot and self._backdrop_available:
                uv = QVector4D(src_x0 / fw, src_y0 / fh, src_x1 / fw, src_y1 / fh)
                if self._draw_with_backdrop(uv, (dx0, dy0, dw, dh), (w, h)):
                    return
            _blit_scope_pixels((src_x0, src_y0, src_x1, src_y1),
                               (dx0, dy0, dx0 + dw, dy0 + dh), GL_LINEAR)
        finally:
            glBindFramebuffer(GL_READ_FRAMEBUFFER, draw_fbo)
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, draw_fbo)

    def _draw_with_backdrop(
        self, uv: QVector4D, viewport: tuple[int, int, int, int], framebuffer_size: tuple[int, int],
    ) -> bool:
        """Composite the cached plot over the background at presentation time."""
        if self._backdrop_program is None:
            program = QOpenGLShaderProgram()
            if not (program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _BACKDROP_VERTEX)
                    and program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _BACKDROP_FRAGMENT)
                    and program.link()):
                logger.warning("Scope backdrop shader unavailable: %s", program.log())
                self._backdrop_available = False
                return False
            vao = QOpenGLVertexArrayObject()
            if not vao.create():
                logger.warning("Scope backdrop vertex array unavailable")
                self._backdrop_available = False
                return False
            self._backdrop_program, self._backdrop_vao = program, vao
        program, vao = self._backdrop_program, self._backdrop_vao
        if not program.bind():
            logger.warning("Scope backdrop shader bind failed: %s", program.log())
            self._backdrop_available = False
            return False
        disabled = [cap for cap in (GL_SCISSOR_TEST, GL_BLEND) if glIsEnabled(cap)]
        try:
            for cap in disabled:
                glDisable(cap)
            glViewport(*viewport)
            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, self._cache_tex)
            vao.bind()
            program.setUniformValue("uv_rect", uv)
            program.setUniformValue("background", self._background_color.redF())
            program.setUniformValue("plot_floor", self._plot_floor)
            program.setUniformValue("plot", 0)
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        finally:
            vao.release()
            glBindTexture(GL_TEXTURE_2D, 0)
            program.release()
            glViewport(0, 0, *framebuffer_size)
            for cap in disabled:
                glEnable(cap)
        return True
