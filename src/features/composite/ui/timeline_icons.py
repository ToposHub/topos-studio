"""QPainter-drawn AE-style timeline icons (P3 view, T35).

Draws the icons from ``timeline_layout.ICON_SPECS`` using pure QPainter
geometry — no font icons, no image assets — so the timeline has a complete,
self-consistent icon set even where ``icon_manager``'s qtawesome glyphs
don't match AE conventions (diamond keyframes, expression '=', playhead
triangle, magnet snap, …).

Why a custom painter instead of qtawesome:

* plan §16.9 / §3.2 require *geometric* icons whose shape encodes state
  (selected diamond is bigger; Hold is a square; expression error gets a
  corner dot). Font glyphs can't vary their outline per state.
* the four state colors (normal/hover/checked/disabled) come from
  ``timeline_layout.resolve_token`` so theme changes flow through.
* integer pixel alignment is enforced (plan §16.9) so icons stay crisp on
  Retina.

The module exposes one entry point — :func:`draw_icon` — plus a few
specialised shapes (keyframe diamond, playhead) the views call directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

from . import timeline_layout

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from PyQt6.QtGui import QIcon


# ── colour resolution ──────────────────────────────────────────────────


def _state_color(state: str) -> str:
    """Map a state name → a semantic token, then resolve to a hex."""
    token_for = {
        "normal": "text_secondary",
        "hover": "text_primary",
        "checked": "accent_blue",
        "disabled": "text_hint",
        "error": "error",
        "selected": "accent_orange",
    }
    token = token_for.get(state, "text_secondary")
    return timeline_layout.resolve_token(token)


# ── pixel alignment ────────────────────────────────────────────────────


def _align(value: float) -> float:
    """Snap to .5 for crisp 1px strokes (plan §16.9)."""
    return round(value) + 0.5


# ── public entry point ─────────────────────────────────────────────────


def draw_icon(painter: QPainter, name: str, rect: QRectF,
              state: str = "normal") -> None:
    """Draw the named icon centered in ``rect`` with the given state colour.

    ``rect`` is the icon's bounding box (typically the inner area of a
    24×22 button). The icon scales to fit. Unknown names draw nothing —
    callers fall back to ``icon_manager`` for anything not covered here.
    """
    painter.save()
    try:
        color = QColor(_state_color(state))
        pen = QPen(color)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(color)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        cx = rect.center().x()
        cy = rect.center().y()
        half = min(rect.width(), rect.height()) / 2.0

        if name == "diamond":
            _draw_diamond(painter, cx, cy, half)
        elif name == "diamond_plus":
            _draw_diamond_outline(painter, cx, cy, half * 0.78)
            _draw_plus(painter, cx, cy, half * 0.34, color)
        elif name == "diamond_minus":
            _draw_diamond_outline(painter, cx, cy, half * 0.78)
            _draw_minus(painter, cx, cy, half * 0.36, color)
        elif name == "play":
            _draw_play(painter, cx, cy, half, paused=False)
        elif name == "pause":
            _draw_play(painter, cx, cy, half, paused=True)
        elif name in ("fast_backward", "fast_forward"):
            _draw_skip(painter, cx, cy, half,
                       backward=(name == "fast_backward"))
        elif name in ("step_backward", "step_forward"):
            _draw_step(painter, cx, cy, half,
                       backward=(name == "step_backward"))
        elif name in ("lock", "lock_open"):
            _draw_lock(painter, cx, cy, half,
                       open_=name.endswith("_open"))
        elif name in ("eye", "eye_off"):
            _draw_eye(painter, cx, cy, half,
                      off=name.endswith("_off"))
        elif name == "solo":
            _draw_solo(painter, cx, cy, half)
        elif name == "shy":
            _draw_eye(painter, cx, cy, half, off=True)
        elif name in ("expand", "collapse"):
            _draw_chevron(painter, cx, cy, half,
                          down=(name == "expand"))
        elif name == "snap":
            _draw_magnet(painter, cx, cy, half)
        elif name in ("expression", "expression_error"):
            _draw_expression(painter, cx, cy, half,
                             error=(name == "expression_error"))
        elif name == "playhead":
            _draw_playhead_marker(painter, cx, cy, half)
        elif name == "bezier":
            _draw_bezier(painter, cx, cy, half)
        elif name == "fit":
            _draw_fit(painter, cx, cy, half)
        elif name in ("zoom_in", "zoom_out", "search"):
            _draw_magnifier(painter, cx, cy, half,
                            glyph=name)
        elif name == "filter":
            _draw_filter(painter, cx, cy, half)
        elif name == "graph_editor":
            _draw_graph_editor(painter, cx, cy, half)
        elif name == "nodes_mode":
            _draw_nodes_mode(painter, cx, cy, half)
        elif name == "layers_mode":
            _draw_layers_mode(painter, cx, cy, half)
        elif name in ("link", "pick_whip"):
            _draw_link(painter, cx, cy, half)
        # Unknown name → draw nothing (caller's fallback handles it).
    finally:
        painter.restore()


# ── individual shapes ──────────────────────────────────────────────────


def _draw_diamond(p: QPainter, cx: float, cy: float, half: float) -> None:
    """45° diamond — the canonical keyframe shape (plan §16.6)."""
    h = max(2.0, half)
    poly = QPolygonF([
        QPointF(_align(cx), cy - h),
        QPointF(cx + h, _align(cy)),
        QPointF(_align(cx), cy + h),
        QPointF(cx - h, _align(cy)),
    ])
    p.drawPolygon(poly)


def _draw_diamond_outline(p: QPainter, cx: float, cy: float,
                          half: float) -> None:
    """Unfilled keyframe diamond used by the add/remove toolbar buttons."""
    h = max(2.0, half)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(p.pen().color(), 1.2))
    poly = QPolygonF([
        QPointF(_align(cx), cy - h),
        QPointF(cx + h, _align(cy)),
        QPointF(_align(cx), cy + h),
        QPointF(cx - h, _align(cy)),
    ])
    p.drawPolygon(poly)


def _draw_link(p: QPainter, cx: float, cy: float, half: float) -> None:
    """Small chain/pick-whip glyph for AE-style parameter linking.

    Two offset rounded links are deliberately used instead of a font glyph:
    the shape remains readable at 12–16 px and its diagonal silhouette is
    distinct from the expression marker in the adjacent column.
    """
    pen = QPen(p.pen().color())
    pen.setWidthF(1.25)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    link_w = half * 0.95
    link_h = half * 0.48
    left = QRectF(cx - link_w * 0.88, cy - link_h * 0.35,
                  link_w, link_h)
    right = QRectF(cx - link_w * 0.12, cy - link_h * 0.65,
                   link_w, link_h)
    p.drawRoundedRect(left, link_h / 2.0, link_h / 2.0)
    p.drawRoundedRect(right, link_h / 2.0, link_h / 2.0)


def _draw_plus(p: QPainter, cx: float, cy: float, half: float,
               color: QColor) -> None:
    pen = QPen(color)
    pen.setWidth(1)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
    p.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))


def _draw_minus(p: QPainter, cx: float, cy: float, half: float,
                color: QColor) -> None:
    pen = QPen(color)
    pen.setWidth(1)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))


def _draw_play(p: QPainter, cx: float, cy: float, half: float,
               paused: bool) -> None:
    if paused:
        bar_w = max(1.0, half * 0.3)
        p.drawRect(QRectF(cx - half * 0.7, cy - half * 0.7,
                          bar_w, half * 1.4))
        p.drawRect(QRectF(cx + half * 0.4, cy - half * 0.7,
                          bar_w, half * 1.4))
    else:
        poly = QPolygonF([
            QPointF(cx - half * 0.6, cy - half),
            QPointF(cx + half * 0.8, cy),
            QPointF(cx - half * 0.6, cy + half),
        ])
        p.drawPolygon(poly)


def _draw_skip(p: QPainter, cx: float, cy: float, half: float,
               backward: bool) -> None:
    sign = -1.0 if backward else 1.0
    edge_x = cx + sign * half
    # Vertical edge bar.
    p.drawLine(QPointF(edge_x, cy - half), QPointF(edge_x, cy + half))
    # Two compact triangles, matching AE's jump-to-boundary buttons.
    for shift in (0.0, -0.5 if backward else 0.5):
        tcx = cx + shift * half
        poly = QPolygonF([
            QPointF(tcx + sign * half * 0.42, cy - half * 0.8),
            QPointF(tcx - sign * half * 0.42, cy),
            QPointF(tcx + sign * half * 0.42, cy + half * 0.8),
        ])
        p.drawPolygon(poly)


def _draw_step(p: QPainter, cx: float, cy: float, half: float,
               backward: bool) -> None:
    sign = -1.0 if backward else 1.0
    # Single triangle + trailing bar.
    poly = QPolygonF([
        QPointF(cx - sign * half * 0.6, cy - half * 0.8),
        QPointF(cx + sign * half * 0.6, cy),
        QPointF(cx - sign * half * 0.6, cy + half * 0.8),
    ])
    p.drawPolygon(poly)
    edge_x = cx + sign * half * 0.9
    p.drawLine(QPointF(edge_x, cy - half * 0.8),
               QPointF(edge_x, cy + half * 0.8))


def _draw_lock(p: QPainter, cx: float, cy: float, half: float,
               open_: bool) -> None:
    body_w = half * 1.2
    body_h = half * 0.9
    body = QRectF(cx - body_w / 2, cy - half * 0.05,
                  body_w, body_h)
    pen = QPen(p.pen().color())
    pen.setWidthF(1.1)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(body, 1.0, 1.0)
    # Open/closed shackles use different geometry, not merely a color.
    shackle_cx = cx + (half * 0.22 if open_ else 0.0)
    arc_r = body_w * 0.42
    arc_rect = QRectF(shackle_cx - arc_r, cy - half * 0.75,
                      arc_r * 2, arc_r * 1.35)
    p.drawArc(arc_rect, 35 * 16 if open_ else 0, 120 * 16 if open_ else 180 * 16)
    # Keyhole kept inside the body.
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(cx, cy + half * 0.34), half * 0.13, half * 0.13)
    p.drawRect(QRectF(cx - half * 0.07, cy + half * 0.34,
                      half * 0.14, half * 0.27))


def _draw_eye(p: QPainter, cx: float, cy: float, half: float,
              off: bool) -> None:
    pen = p.pen()
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Almond-shaped eye outline, closer to AE than a generic ellipse.
    path = QPainterPath()
    path.moveTo(cx - half, cy)
    path.cubicTo(cx - half * 0.45, cy - half * 0.65,
                 cx + half * 0.45, cy - half * 0.65,
                 cx + half, cy)
    path.cubicTo(cx + half * 0.45, cy + half * 0.65,
                 cx - half * 0.45, cy + half * 0.65,
                 cx - half, cy)
    p.drawPath(path)
    # Pupil.
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(cx, cy), half * 0.25, half * 0.25)
    if off:
        # Diagonal slash.
        p.drawLine(QPointF(cx - half, cy - half * 0.55),
                   QPointF(cx + half, cy + half * 0.55))


def _draw_solo(p: QPainter, cx: float, cy: float, half: float) -> None:
    # Filled circle for the solo/headphone glyph.
    p.drawEllipse(QPointF(cx, cy), half * 0.7, half * 0.7)


def _draw_chevron(p: QPainter, cx: float, cy: float, half: float,
                  down: bool) -> None:
    pen = p.pen()
    pen.setWidthF(1.4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    sign = 1.0 if down else -1.0
    p.drawLine(QPointF(cx - half * 0.7, cy - sign * half * 0.4),
               QPointF(cx, cy + sign * half * 0.4))
    p.drawLine(QPointF(cx, cy + sign * half * 0.4),
               QPointF(cx + half * 0.7, cy - sign * half * 0.4))


def _draw_magnet(p: QPainter, cx: float, cy: float, half: float) -> None:
    pen = p.pen()
    pen.setWidthF(1.4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # U-shape: two verticals + bottom curve (approximated as polyline).
    w = half * 1.2
    h = half * 1.0
    poly = QPolygonF([
        QPointF(cx - w * 0.5, cy - h * 0.7),
        QPointF(cx - w * 0.5, cy + h * 0.3),
        QPointF(cx - w * 0.25, cy + h * 0.7),
        QPointF(cx + w * 0.25, cy + h * 0.7),
        QPointF(cx + w * 0.5, cy + h * 0.3),
        QPointF(cx + w * 0.5, cy - h * 0.7),
    ])
    p.drawPolyline(poly)
    # Two short tips.
    p.drawLine(QPointF(cx - w * 0.5, cy - h * 0.7),
               QPointF(cx - w * 0.5, cy - h * 0.9))
    p.drawLine(QPointF(cx + w * 0.5, cy - h * 0.7),
               QPointF(cx + w * 0.5, cy - h * 0.9))


def _draw_expression(p: QPainter, cx: float, cy: float, half: float,
                     error: bool) -> None:
    pen = p.pen()
    pen.setWidthF(1.4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Two horizontal lines = '='.
    p.drawLine(QPointF(cx - half * 0.6, cy - half * 0.25),
               QPointF(cx + half * 0.6, cy - half * 0.25))
    p.drawLine(QPointF(cx - half * 0.6, cy + half * 0.25),
               QPointF(cx + half * 0.6, cy + half * 0.25))
    if error:
        # Corner dot (top-right).
        p.setBrush(QColor(timeline_layout.resolve_token("error")))
        p.drawEllipse(QPointF(cx + half * 0.85, cy - half * 0.85),
                      half * 0.18, half * 0.18)


def _draw_playhead_marker(p: QPainter, cx: float, cy: float,
                          half: float) -> None:
    # Inverted triangle pointing down (the playhead's top marker).
    poly = QPolygonF([
        QPointF(cx - half, cy - half),
        QPointF(cx + half, cy - half),
        QPointF(cx, cy + half),
    ])
    p.drawPolygon(poly)


def _draw_bezier(p: QPainter, cx: float, cy: float, half: float) -> None:
    pen = p.pen()
    pen.setWidthF(1.0)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Two handle lines + two endpoint dots + the keyframe centre.
    p.drawLine(QPointF(cx - half * 0.8, cy + half * 0.6),
               QPointF(cx, cy))
    p.drawLine(QPointF(cx, cy),
               QPointF(cx + half * 0.8, cy - half * 0.6))
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(cx - half * 0.8, cy + half * 0.6),
                  half * 0.18, half * 0.18)
    p.drawEllipse(QPointF(cx + half * 0.8, cy - half * 0.6),
                  half * 0.18, half * 0.18)
    _draw_diamond(p, cx, cy, half * 0.4)


def _draw_fit(p: QPainter, cx: float, cy: float, half: float) -> None:
    pen = p.pen()
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Four corner brackets.
    off = half * 0.7
    arm = half * 0.4
    for sx in (-1, 1):
        for sy in (-1, 1):
            x = cx + sx * off
            y = cy + sy * off
            p.drawLine(QPointF(x, y), QPointF(x - sx * arm, y))
            p.drawLine(QPointF(x, y), QPointF(x, y - sy * arm))


def _draw_magnifier(p: QPainter, cx: float, cy: float, half: float,
                    glyph: str) -> None:
    pen = p.pen()
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    lens_r = half * 0.55
    lens_c = QPointF(cx - half * 0.2, cy - half * 0.2)
    p.drawEllipse(lens_c, lens_r, lens_r)
    # Handle.
    p.drawLine(QPointF(lens_c.x() + lens_r * 0.7,
                       lens_c.y() + lens_r * 0.7),
               QPointF(cx + half * 0.7, cy + half * 0.7))
    if glyph == "zoom_in":
        _draw_plus(p, lens_c.x(), lens_c.y(), lens_r * 0.5, p.pen().color())
    elif glyph == "zoom_out":
        _draw_minus(p, lens_c.x(), lens_c.y(), lens_r * 0.5,
                    p.pen().color())


def _draw_filter(p: QPainter, cx: float, cy: float, half: float) -> None:
    pen = p.pen()
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    poly = QPolygonF([
        QPointF(cx - half * 0.8, cy - half * 0.6),
        QPointF(cx + half * 0.8, cy - half * 0.6),
        QPointF(cx + half * 0.2, cy),
        QPointF(cx + half * 0.2, cy + half * 0.7),
        QPointF(cx - half * 0.2, cy + half * 0.7),
        QPointF(cx - half * 0.2, cy),
    ])
    p.drawPolyline(poly)


def _draw_graph_editor(p: QPainter, cx: float, cy: float,
                       half: float) -> None:
    pen = p.pen()
    pen.setWidthF(1.0)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Axes.
    p.drawLine(QPointF(cx - half * 0.8, cy + half * 0.8),
               QPointF(cx - half * 0.8, cy - half * 0.8))
    p.drawLine(QPointF(cx - half * 0.8, cy + half * 0.8),
               QPointF(cx + half * 0.8, cy + half * 0.8))
    # Curve.
    from PyQt6.QtGui import QPainterPath
    path = QPainterPath()
    path.moveTo(cx - half * 0.7, cy + half * 0.5)
    path.cubicTo(cx - half * 0.2, cy - half * 0.9,
                 cx + half * 0.2, cy + half * 0.9,
                 cx + half * 0.7, cy - half * 0.5)
    p.drawPath(path)


def _draw_nodes_mode(p: QPainter, cx: float, cy: float,
                     half: float) -> None:
    """Draw a small connected-node graph for the Nodes projection."""
    pen = p.pen()
    pen.setWidthF(1.15)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    left = QPointF(cx - half * 0.78, cy - half * 0.48)
    right = QPointF(cx + half * 0.72, cy - half * 0.48)
    bottom = QPointF(cx, cy + half * 0.62)
    p.drawLine(left, bottom)
    p.drawLine(right, bottom)
    node_r = max(1.8, half * 0.24)
    for point in (left, right, bottom):
        p.drawEllipse(point, node_r, node_r)


def _draw_layers_mode(p: QPainter, cx: float, cy: float,
                      half: float) -> None:
    """Draw three offset cards for the stacked Layers projection."""
    pen = p.pen()
    pen.setWidthF(1.05)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    card_w = half * 1.45
    card_h = half * 0.48
    for index in range(3):
        x = cx - card_w * 0.5 + (index - 1) * half * 0.12
        y = cy - card_h * 0.5 + (index - 1) * half * 0.42
        p.drawRoundedRect(QRectF(x, y, card_w, card_h),
                          card_h * 0.22, card_h * 0.22)


# ── QIcon bridge ───────────────────────────────────────────────────────


def make_qicon(name: str, state: str = "normal",
               icon_px: int = timeline_layout.TOOLBAR_ICON_PX) -> "QIcon":
    """Render a ``timeline_icons`` glyph into a ``QIcon`` for use in a
    QAction / QToolButton.

    The toolbar (plan §3.2) wants diamond keyframe icons and AE-style
    transport glyphs that qtawesome's font glyphs don't match. This helper
    paints ``draw_icon`` into a device-independent pixmap and wraps it as a
    QIcon so existing QAction-based toolbar code keeps working unchanged.

    A 2× device-pixel-ratio is used so the icon stays crisp on Retina without
    a separate high-DPI code path (plan §16.9). The returned icon has only
    one mode (Normal) — checked/hover states are painted by the button via
    stylesheet, not by swapping icons, so the toolbar styling stays in one
    place.
    """
    from PyQt6.QtGui import QColor, QIcon, QPixmap

    size = max(8, int(icon_px))
    # Render at 2× for crispness on high-DPI; the QIcon scales down at paint.
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(QColor(0, 0, 0, 0))   # transparent background
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        draw_icon(p, name,
                  QRectF(0, 0, size * scale, size * scale),
                  state)
    finally:
        p.end()
    # Tag the pixmap with its device-pixel-ratio so Qt draws it at the right
    # logical size when the action is shown in a 14×14 button.
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


__all__ = ["draw_icon", "make_qicon"]
