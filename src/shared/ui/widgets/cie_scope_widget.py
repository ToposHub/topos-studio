"""CIE 1931 xy chromaticity GPU scope presenter.

The density heat map is rendered by the shared GPU scope engine; this
widget owns *all* plot chrome as QPainter annotations over the blitted
texture, following the DisplayCAL-derived reference plot
(``cie_plot_demo_new.py``): gridded axes with tick labels, the
wavelength-coloured spectral locus (drawn as a Catmull-Rom → cubic
Bézier spline so the horseshoe is smooth instead of polygonal) plus the
straight purple line, selectable gamut preset triangles (line style +
optional fill each), the equal-energy point and the daylight / blackbody
locus curve.

The view is fixed to the full plot window (``xy in [0,0.8]x[0,0.9]``);
there is no zoom / pan interaction.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPainterPath
from PyQt6.QtWidgets import QMenu

from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
from src.shared.ui.widgets.vectorscope_widget import CIE_SPECTRAL_LOCUS

# Plot window (matches the GPU density FBO mapping: xy in [0,0.8]x[0,0.9]).
CIE_X_RANGE = (0.0, 0.8)
CIE_Y_RANGE = (0.0, 0.9)

GRID_COLOUR = QColor(32, 32, 32)
TEXT_COLOUR = QColor(153, 153, 153)
PURPLE_LINE_COLOUR = QColor(128, 0, 128)
DAYLIGHT_COLOUR = QColor(255, 165, 0)
BLACKBODY_COLOUR = QColor(138, 43, 226)

# Gamut presets (reference STANDARD_GAMUTS): xy primaries, stroke colour
# and pen style. style: 0 solid, 1 dash, 2 dot, 3 dash-dot.
CIE_GAMUT_PRESETS = {
    'sRGB': {
        'R': (0.640, 0.330), 'G': (0.300, 0.600), 'B': (0.150, 0.060),
        'color': QColor(255, 80, 80), 'style': Qt.PenStyle.DotLine,
    },
    'Rec. 709': {
        'R': (0.640, 0.330), 'G': (0.300, 0.600), 'B': (0.150, 0.060),
        'color': QColor(255, 180, 80), 'style': Qt.PenStyle.DotLine,
    },
    'Adobe RGB': {
        'R': (0.640, 0.330), 'G': (0.210, 0.710), 'B': (0.150, 0.060),
        'color': QColor(200, 80, 200), 'style': Qt.PenStyle.DashLine,
    },
    'DCI P3': {
        'R': (0.680, 0.320), 'G': (0.265, 0.690), 'B': (0.150, 0.060),
        'color': QColor(80, 200, 200), 'style': Qt.PenStyle.DashDotLine,
    },
    'Rec. 2020': {
        'R': (0.708, 0.292), 'G': (0.170, 0.797), 'B': (0.131, 0.046),
        'color': QColor(150, 220, 80), 'style': Qt.PenStyle.SolidLine,
    },
}


def wavelength_to_rgb(wavelength: float) -> QColor:
    """Approximate visible wavelength -> RGB (Dan Bruton's algorithm)."""
    wavelength = float(wavelength)
    if 380 <= wavelength < 440:
        r, g, b = -(wavelength - 440) / 60.0, 0.0, 1.0
    elif 440 <= wavelength < 490:
        r, g, b = 0.0, (wavelength - 440) / 50.0, 1.0
    elif 490 <= wavelength < 510:
        r, g, b = 0.0, 1.0, -(wavelength - 510) / 20.0
    elif 510 <= wavelength < 580:
        r, g, b = (wavelength - 510) / 70.0, 1.0, 0.0
    elif 580 <= wavelength < 645:
        r, g, b = 1.0, -(wavelength - 645) / 65.0, 0.0
    elif 645 <= wavelength <= 700:
        r, g, b = 1.0, 0.0, 0.0
    else:
        r = g = b = 0.0
    if 380 <= wavelength < 420:
        factor = 0.3 + 0.7 * (wavelength - 380) / 40.0
    elif 645 <= wavelength <= 700:
        factor = 0.3 + 0.7 * (700 - wavelength) / 55.0
    else:
        factor = 1.0
    return QColor(round(r * factor * 255), round(g * factor * 255),
                  round(b * factor * 255))


def planckian_locus_xy(kelvin: float) -> Optional[Tuple[float, float]]:
    """CIE 1931 xy of the Planckian locus at ``kelvin`` (Wikipedia fit)."""
    t = float(kelvin)
    if 1667 <= t <= 4000:
        x = (-0.2661239e9 / t ** 3 - 0.2343580e6 / t ** 2
             + 0.8776956e3 / t + 0.179910)
    elif 4000 < t <= 25000:
        x = (-3.0258469e9 / t ** 3 + 2.1070379e6 / t ** 2
             + 0.2226347e3 / t + 0.24039)
    else:
        return None
    if 1667 <= t <= 2222:
        y = -1.1063814 * x ** 3 - 1.34811020 * x ** 2 \
            + 2.18555832 * x - 0.20219683
    elif 2222 < t <= 4000:
        y = -0.9549476 * x ** 3 - 1.37418593 * x ** 2 \
            + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x ** 3 - 5.87338670 * x ** 2 \
            + 3.75112997 * x - 0.37001483
    return x, y


def daylight_locus_xy(kelvin: float) -> Optional[Tuple[float, float]]:
    """CIE 1931 xy of the daylight (D-series) locus (brucelindbloom fit)."""
    t = float(kelvin)
    if not 2500 <= t <= 25000:
        return None
    if t <= 7000:
        x = -4.607e9 / t ** 3 + 2.9678e6 / t ** 2 + 0.09911e3 / t + 0.244063
    else:
        x = -2.0064e9 / t ** 3 + 1.9018e6 / t ** 2 + 0.24748e3 / t + 0.237040
    y = -3.0 * x ** 2 + 2.87 * x - 0.275
    return x, y


def catmull_rom_to_bezier(
        points: List[Tuple[float, float]],
) -> List[Tuple[Tuple[float, float], ...]]:
    """Catmull-Rom through-point spline → per-segment cubic Bézier controls.

    Input ``n`` points yields ``n-1`` ``(p0, c1, c2, p3)`` segments that
    pass through every input point (standard 1/6 tangent tension); the
    ends are duplicated so the curve stays natural at both terminals.
    """
    if len(points) < 2:
        return []
    pts = [points[0], *points, points[-1]]
    segments: List[Tuple[Tuple[float, float], ...]] = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0,
              p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0,
              p2[1] - (p3[1] - p1[1]) / 6.0)
        segments.append((p1, c1, c2, p2))
    return segments


def cubic_bezier_point(p0: Tuple[float, float], c1: Tuple[float, float],
                       c2: Tuple[float, float], p3: Tuple[float, float],
                       t: float) -> Tuple[float, float]:
    """Point on a cubic Bézier at parameter ``t`` (de Casteljau form)."""
    u = 1.0 - t
    a = u * u * u
    b = 3.0 * u * u * t
    c = 3.0 * u * t * t
    d = t * t * t
    return (a * p0[0] + b * c1[0] + c * c2[0] + d * p3[0],
            a * p0[1] + b * c1[1] + c * c2[1] + d * p3[1])


class CieGpuScopeWidget(GpuScopeWidget):
    """CIE chromaticity presenter with reference-style plot chrome.

    The view is fixed to the full plot window — no zoom or pan.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent, preserve_aspect=True, show_value_axis=False)
        # Overlay options (reference right-click menu state).
        self.show_gamuts: List[str] = ['sRGB']
        self.show_gamut_filled = False
        self.show_locus = True
        self.show_ee_point = True
        self.show_grid = True
        self.show_labels = True
        self.color_temp_curve: Optional[str] = 'daylight'
        # AUD-38201: 静态几何在 xy 色度域预计算并缓存（光谱轨迹贝塞尔采样
        # 折线与色温曲线折线），paint 时仅做 xy→px 映射，不再每次重绘全量
        # 重算 Catmull-Rom/Bézier 采样与普朗克/昼光轨迹求值。
        self._locus_polyline: Optional[List[Tuple[QColor, float, float]]] = None
        self._ct_polylines: dict = {}

    def _get_locus_polyline(self) -> List[Tuple[QColor, float, float]]:
        """预计算光谱轨迹 xy 域折线（颜色随波长插值），只算一次。"""
        if self._locus_polyline is None:
            points = [(wl, x, y) for wl, x, y in CIE_SPECTRAL_LOCUS]
            polyline: List[Tuple[QColor, float, float]] = []
            bezier_segments = catmull_rom_to_bezier(
                [(x, y) for _, x, y in points])
            samples_per_segment = 12
            for i, (p0, c1, c2, p3) in enumerate(bezier_segments):
                wl_start, wl_end = points[i][0], points[i + 1][0]
                for t in range(1, samples_per_segment + 1):
                    f = t / float(samples_per_segment)
                    bx, by = cubic_bezier_point(p0, c1, c2, p3, f)
                    color = wavelength_to_rgb(
                        wl_start + (wl_end - wl_start) * f)
                    polyline.append((color, bx, by))
            self._locus_polyline = polyline
        return self._locus_polyline

    def _get_ct_polyline(self, curve_kind: str) -> List[Tuple[float, float, float]]:
        """预计算色温曲线（'daylight'/'blackbody'）的 xy 域折线，按类型缓存。

        返回 (kelvin, x, y) 三元组列表，kelvin 供端点标签使用。
        """
        cached = self._ct_polylines.get(curve_kind)
        if cached is not None:
            return cached
        polyline: List[Tuple[float, float, float]] = []
        if curve_kind == 'daylight':
            for kelvin in range(4000, 25000 + 1, 40):
                xy = daylight_locus_xy(kelvin)
                if xy is not None:
                    polyline.append((kelvin, xy[0], xy[1]))
        elif curve_kind == 'blackbody':
            for kelvin in range(1667, 25000 + 1, 38):
                xy = planckian_locus_xy(kelvin)
                if xy is not None:
                    polyline.append((kelvin, xy[0], xy[1]))
        self._ct_polylines[curve_kind] = polyline
        return polyline

    # ------------------------------------------------------------------
    # Fixed view (full plot window)
    # ------------------------------------------------------------------
    def _view_window(self) -> Tuple[float, float, float, float]:
        """Visible xy window — always the full plot range."""
        return (CIE_X_RANGE[0], CIE_X_RANGE[1],
                CIE_Y_RANGE[0], CIE_Y_RANGE[1])

    def _plot_dst_rect(self) -> QRectF:
        """Letterboxed destination rect (logical px) of the full window."""
        w, h = float(self.width()), float(self.height())
        span_x = CIE_X_RANGE[1] - CIE_X_RANGE[0]
        span_y = CIE_Y_RANGE[1] - CIE_Y_RANGE[0]
        scale = min(w / span_x, h / span_y)
        pw, ph = span_x * scale, span_y * scale
        return QRectF((w - pw) / 2.0, (h - ph) / 2.0, pw, ph)

    def _xy_to_px(self, x: float, y: float) -> QPointF:
        vx0, vx1, vy0, vy1 = self._view_window()
        rect = self._plot_dst_rect()
        return QPointF(
            rect.left() + (x - vx0) / (vx1 - vx0) * rect.width(),
            rect.top() + (vy1 - y) / (vy1 - vy0) * rect.height(),
        )

    def _px_to_xy(self, px: QPointF) -> Tuple[float, float]:
        vx0, vx1, vy0, vy1 = self._view_window()
        rect = self._plot_dst_rect()
        u = (px.x() - rect.left()) / max(rect.width(), 1e-6)
        v = (px.y() - rect.top()) / max(rect.height(), 1e-6)
        return vx0 + u * (vx1 - vx0), vy1 - v * (vy1 - vy0)

    def _blit_src_rect(self, fw: int, fh: int) -> Tuple[int, int, int, int]:
        """Present the full density display FBO (fixed full view)."""
        return 0, 0, fw, fh

    # ------------------------------------------------------------------
    # Right-click menu (reference on_right_click)
    # ------------------------------------------------------------------
    def contextMenuEvent(self, event) -> None:  # pragma: no cover - Qt
        from src.shared.i18n.i18n import I18N
        menu = QMenu(self)

        gamut_menu = menu.addMenu(I18N.tr("analysis.cie_gamuts", "显示色域"))
        for name in CIE_GAMUT_PRESETS:
            act = gamut_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name in self.show_gamuts)
            act.toggled.connect(lambda on, n=name: self.toggle_gamut(n, on))

        act = menu.addAction(
            I18N.tr("analysis.cie_fill_gamut", "填充色域区域"))
        act.setCheckable(True)
        act.setChecked(self.show_gamut_filled)
        act.toggled.connect(self.set_gamut_filled)

        menu.addSeparator()

        act = menu.addAction(
            I18N.tr("analysis.cie_show_locus", "显示光谱轨迹"))
        act.setCheckable(True)
        act.setChecked(self.show_locus)
        act.toggled.connect(self.set_show_locus)

        act = menu.addAction(
            I18N.tr("analysis.cie_show_ee", "显示等能点 (E)"))
        act.setCheckable(True)
        act.setChecked(self.show_ee_point)
        act.toggled.connect(self.set_show_ee_point)

        act = menu.addAction(I18N.tr("analysis.cie_show_grid", "显示网格"))
        act.setCheckable(True)
        act.setChecked(self.show_grid)
        act.toggled.connect(self.set_show_grid)

        act = menu.addAction(
            I18N.tr("analysis.cie_show_labels", "显示坐标轴标签"))
        act.setCheckable(True)
        act.setChecked(self.show_labels)
        act.toggled.connect(self.set_show_labels)

        menu.addSeparator()

        ct_menu = menu.addMenu(
            I18N.tr("analysis.cie_temp_curve", "色温曲线"))
        group_actions = []
        for value, label in (
            (None, I18N.tr("analysis.cie_temp_none", "不显示色温曲线")),
            ('daylight', I18N.tr(
                "analysis.cie_temp_daylight", "日光轨迹 (Daylight)")),
            ('blackbody', I18N.tr(
                "analysis.cie_temp_blackbody", "黑体轨迹 (Blackbody)")),
        ):
            act = ct_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.color_temp_curve == value)
            act.toggled.connect(
                lambda on, v=value: self.set_color_temp_curve(v))
            group_actions.append(act)

        menu.exec(event.globalPos())

    # ------------------------------------------------------------------
    # Option setters (menu callbacks; each repaints)
    # ------------------------------------------------------------------
    def toggle_gamut(self, name: str, on: bool) -> None:
        if on and name not in self.show_gamuts:
            self.show_gamuts.append(name)
        elif not on and name in self.show_gamuts:
            self.show_gamuts.remove(name)
        self.update()

    def set_gamut_filled(self, on: bool) -> None:
        self.show_gamut_filled = bool(on)
        self.update()

    def set_show_locus(self, on: bool) -> None:
        self.show_locus = bool(on)
        self.update()

    def set_show_ee_point(self, on: bool) -> None:
        self.show_ee_point = bool(on)
        self.update()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = bool(on)
        self.update()

    def set_show_labels(self, on: bool) -> None:
        self.show_labels = bool(on)
        self.update()

    def set_color_temp_curve(self, curve: Optional[str]) -> None:
        if curve in (None, 'daylight', 'blackbody'):
            self.color_temp_curve = curve
            self.update()

    # ------------------------------------------------------------------
    # Chrome painting
    # ------------------------------------------------------------------
    def _paint_scope_chrome(self) -> None:  # pragma: no cover - Qt/GL
        painter = QPainter()
        if not painter.begin(self):
            return
        try:
            self._draw_cie_overlay(painter)
        finally:
            painter.end()

    def _draw_cie_overlay(self, painter: QPainter) -> None:
        """Draw the full reference-style plot chrome with ``painter``.

        Separated from the QOpenGLWidget paint path so tests can render
        the chrome into a plain QImage deterministically.

        NOTE: no QPainter clipping is used here — the Qt *OpenGL* paint
        engine mis-applies clip rects on macOS (everything after
        ``setClipRect`` either vanishes or stays clipped forever). All
        geometry is instead clipped analytically against the plot rect
        (``_clip_segment``), which behaves identically on raster and GL.
        """
        w, h = self.width(), self.height()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(QFont("Arial", 8))

        rect = self._plot_dst_rect()
        vx0, vx1, vy0, vy1 = self._view_window()

        if self.show_grid:
            self._draw_grid(painter, rect, vx0, vx1, vy0, vy1)
        if self.color_temp_curve:
            self._draw_color_temp_curve(painter)
        if self.show_gamut_filled:
            self._draw_gamut_fills(painter)
        if self.show_locus:
            self._draw_locus(painter)
        self._draw_gamut_triangles(painter)
        if self.show_ee_point:
            self._draw_ee_point(painter)
        if self.show_labels:
            self._draw_axis_labels(painter, rect, vx0, vx1, vy0, vy1)
        self._draw_header(painter, w, h)

    def _clip_segment(self, p1: QPointF, p2: QPointF,
                      rect: QRectF) -> Optional[Tuple[QPointF, QPointF]]:
        """Liang–Barsky clip of a segment against the plot rect."""
        x0, y0, x1, y1 = p1.x(), p1.y(), p2.x(), p2.y()
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x0 - rect.left()), (dx, rect.right() - x0),
                     (-dy, y0 - rect.top()), (dy, rect.bottom() - y0)):
            if p == 0.0:
                if q < 0.0:
                    return None
                continue
            t = q / p
            if p < 0.0:
                if t > t1:
                    return None
                t0 = max(t0, t)
            else:
                if t < t0:
                    return None
                t1 = min(t1, t)
        return (QPointF(x0 + t0 * dx, y0 + t0 * dy),
                QPointF(x0 + t1 * dx, y0 + t1 * dy))

    def _draw_grid(self, painter, rect, vx0, vx1, vy0, vy1) -> None:
        if vx1 - vx0 <= 0 or vy1 - vy0 <= 0:
            return
        major_step = 0.1
        painter.setPen(QPen(GRID_COLOUR, 1))
        x = math.ceil(vx0 / major_step) * major_step
        while x <= vx1 + 1e-9:
            seg = self._clip_segment(self._xy_to_px(x, vy0),
                                     self._xy_to_px(x, vy1), rect)
            if seg:
                painter.drawLine(*seg)
            x += major_step
        y = math.ceil(vy0 / major_step) * major_step
        while y <= vy1 + 1e-9:
            seg = self._clip_segment(self._xy_to_px(vx0, y),
                                     self._xy_to_px(vx1, y), rect)
            if seg:
                painter.drawLine(*seg)
            y += major_step

    def _draw_locus(self, painter) -> None:
        """Wavelength-coloured spectral locus + purple closure line.

        The horseshoe is drawn as a Catmull-Rom → cubic Bézier spline
        through the 10 nm table points, so the curve is smooth instead
        of the polygonal straight-line join; each Bézier segment is
        sampled finely and stroked with the interpolated wavelength
        colour (clipped analytically against the plot rect).

        AUD-38201: 采样折线在 xy 域预计算缓存，这里只做 xy→px 映射与裁剪。
        """
        polyline = self._get_locus_polyline()
        rect = self._plot_dst_rect()
        prev_px: Optional[QPointF] = None
        for color, bx, by in polyline:
            nxt = self._xy_to_px(bx, by)
            if prev_px is not None:
                seg = self._clip_segment(prev_px, nxt, rect)
                if seg:
                    painter.setPen(QPen(color, 2))
                    painter.drawLine(*seg)
            prev_px = nxt
        # Purple line: 700 nm endpoint straight back to 380 nm.
        first = self._xy_to_px(CIE_SPECTRAL_LOCUS[0][1], CIE_SPECTRAL_LOCUS[0][2])
        last = self._xy_to_px(CIE_SPECTRAL_LOCUS[-1][1], CIE_SPECTRAL_LOCUS[-1][2])
        seg = self._clip_segment(last, first, rect)
        if seg:
            painter.setPen(QPen(PURPLE_LINE_COLOUR, 2))
            painter.drawLine(*seg)

    def _draw_gamut_fills(self, painter) -> None:
        for name in self.show_gamuts:
            preset = CIE_GAMUT_PRESETS[name]
            path = self._gamut_path(preset)
            fill = QColor(preset['color'])
            fill.setAlpha(30)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_gamut_triangles(self, painter) -> None:
        rect = self._plot_dst_rect()
        for name in self.show_gamuts:
            preset = CIE_GAMUT_PRESETS[name]
            painter.setPen(QPen(preset['color'], 2, preset['style']))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            r = self._xy_to_px(*preset['R'])
            g = self._xy_to_px(*preset['G'])
            b = self._xy_to_px(*preset['B'])
            for p1, p2 in ((r, g), (g, b), (b, r)):
                seg = self._clip_segment(p1, p2, rect)
                if seg:
                    painter.drawLine(*seg)

    def _gamut_path(self, preset) -> QPainterPath:
        path = QPainterPath()
        path.moveTo(self._xy_to_px(*preset['R']))
        path.lineTo(self._xy_to_px(*preset['G']))
        path.lineTo(self._xy_to_px(*preset['B']))
        path.closeSubpath()
        return path

    def _draw_ee_point(self, painter) -> None:
        ex, ey = 1.0 / 3.0, 1.0 / 3.0
        vx0, vx1, vy0, vy1 = self._view_window()
        if not (vx0 <= ex <= vx1 and vy0 <= ey <= vy1):
            return
        px = self._xy_to_px(ex, ey)
        painter.setPen(QPen(TEXT_COLOUR, 1, Qt.PenStyle.DotLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(px, 5.0, 5.0)
        painter.setPen(TEXT_COLOUR)
        painter.drawText(px + QPointF(-5, -18), "E")

    def _draw_color_temp_curve(self, painter) -> None:
        if self.color_temp_curve == 'daylight':
            color = DAYLIGHT_COLOUR
        elif self.color_temp_curve == 'blackbody':
            color = BLACKBODY_COLOUR
        else:
            return
        rect = self._plot_dst_rect()
        # AUD-38201: 复用按曲线类型缓存的 xy 域折线，仅做坐标映射。
        xy_points = self._get_ct_polyline(self.color_temp_curve)
        screen = [(kelvin, self._xy_to_px(x, y))
                  for (kelvin, x, y) in xy_points]
        if len(screen) < 2:
            return
        painter.setPen(QPen(color, 2))
        for (_, p1), (_, p2) in zip(screen, screen[1:]):
            seg = self._clip_segment(p1, p2, rect)
            if seg:
                painter.drawLine(*seg)
        start_k, start_p = screen[0]
        end_k, end_p = screen[-1]
        if rect.contains(start_p):
            painter.drawText(start_p + QPointF(5, -15), f"{start_k}K")
        if rect.contains(end_p):
            painter.drawText(end_p + QPointF(-30, 5), f"{end_k}K")

    def _draw_axis_labels(self, painter, rect, vx0, vx1, vy0, vy1) -> None:
        painter.setPen(TEXT_COLOUR)
        bottom = rect.bottom()
        left = rect.left()
        x = math.ceil(vx0 / 0.1) * 0.1
        while x <= vx1 + 1e-9:
            px = self._xy_to_px(x, vy0).x()
            if 12 < px < self.width() - 16:
                painter.drawText(QPointF(px - 8, min(bottom + 12,
                                                      self.height() - 3)),
                                 f"{x:.1f}")
            x = round(x + 0.1, 10)
        y = math.ceil(vy0 / 0.1) * 0.1
        while y <= vy1 + 1e-9:
            py = self._xy_to_px(vx0, y).y()
            if 14 < py < self.height() - 12:
                painter.drawText(QPointF(max(2.0, left - 18), py + 3),
                                 f"{y:.1f}")
            y = round(y + 0.1, 10)
        # Axis titles: "y" top-left above the y labels, "x" bottom-right.
        painter.drawText(QPointF(max(2.0, left - 18), rect.top() - 3), "y")
        painter.drawText(QPointF(rect.right() - 4,
                                 min(rect.bottom() + 24,
                                     self.height() - 3)), "x")

    def _draw_header(self, painter, w, h) -> None:
        # Active gamut legend (top right): style swatch + preset name.
        # (The former "Zoom" / "CIE" header rows were removed — they
        # collided with foreground annotations.)
        font_w = 8 * max(2, len(max(self.show_gamuts or [''], key=len)) + 2)
        y = 16
        for name in self.show_gamuts:
            preset = CIE_GAMUT_PRESETS[name]
            painter.setPen(QPen(preset['color'], 3, preset['style']))
            painter.drawLine(w - 30 - font_w, y, w - 12 - font_w, y)
            painter.setPen(preset['color'])
            painter.drawText(w - 8 - font_w, y + 4, name)
            y += 16
