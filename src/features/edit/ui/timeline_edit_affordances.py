"""Small vector affordances for timeline edits, independent of edit commands.

Only the pointer and painted edge communicate the active edit here. Model
changes, hit testing, and gesture lifetimes stay with the timeline items/view.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap

EditCursorKind = Literal[
    "trim_left", "trim_right", "ripple_left", "ripple_right",
    "roll", "slip", "slide", "fade",
]
TrimEdge = Literal["left", "right"]

_CURSOR_KINDS: tuple[EditCursorKind, ...] = (
    "trim_left", "trim_right", "ripple_left", "ripple_right",
    "roll", "slip", "slide", "fade",
)
_CURSOR_SIZE = 32
_CURSOR_DPR = 2.0
_HOTSPOT = 16


def _line(path: QPainterPath, *points: tuple[float, float]) -> None:
    """Append an open line, without joining independent pointer components."""
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)


def _arrow(path: QPainterPath, tail: float, tip: float, y: float = 16.0) -> None:
    """Append a horizontal single arrow with a four-pixel arrowhead."""
    direction = 1.0 if tip > tail else -1.0
    _line(path, (tail, y), (tip, y))
    _line(path, (tip - direction * 4.0, y - 4.0),
          (tip, y), (tip - direction * 4.0, y + 4.0))


def _cursor_path(kind: EditCursorKind) -> QPainterPath:
    path = QPainterPath()
    if kind in {"trim_left", "trim_right", "ripple_left", "ripple_right"}:
        left = kind.endswith("left")
        inward = 1.0 if left else -1.0
        _line(path, (16.0 + inward * 4.0, 6.0), (16.0, 6.0),
              (16.0, 26.0), (16.0 + inward * 4.0, 26.0))
        if kind.startswith("ripple"):
            boundary = 16.0 + inward * 4.0
            _line(path, (boundary + inward * 4.0, 6.0), (boundary, 6.0),
                  (boundary, 26.0), (boundary + inward * 4.0, 26.0))
        _arrow(path, 16.0 - inward * 4.0, 16.0 - inward * 12.0)
    elif kind == "roll":
        _line(path, (14.0, 6.0), (14.0, 26.0))
        _line(path, (18.0, 6.0), (18.0, 26.0))
        _arrow(path, 10.0, 4.0)
        _arrow(path, 22.0, 28.0)
    elif kind == "slip":
        _line(path, (9.0, 7.0), (5.0, 7.0), (5.0, 25.0), (9.0, 25.0))
        _line(path, (23.0, 7.0), (27.0, 7.0), (27.0, 25.0), (23.0, 25.0))
        _arrow(path, 16.0, 10.0)
        _arrow(path, 16.0, 22.0)
    elif kind == "slide":
        path.addRect(QRectF(12.0, 10.0, 8.0, 12.0))
        _arrow(path, 9.0, 3.0)
        _arrow(path, 23.0, 29.0)
    else:  # fade: a compact arrow without trim brackets or a system cursor.
        _arrow(path, 16.0, 5.0)
        _arrow(path, 16.0, 27.0)
    return path


@lru_cache(maxsize=len(_CURSOR_KINDS))
def edit_cursor(kind: EditCursorKind) -> QCursor:
    """Return a cached, outlined 2x cursor; call on the Qt UI thread.

    The hotspot is the logical edit boundary at (16, 16), rather than the
    centre of a differently positioned bitmap. Every path leaves enough
    transparent space for its one-pixel dark outline at both device scales.
    """
    if kind not in _CURSOR_KINDS:
        raise ValueError(f"Unknown timeline edit cursor: {kind!r}")
    if QGuiApplication.instance() is None:
        raise RuntimeError("Timeline edit cursors require a Qt application")
    pixmap = QPixmap(round(_CURSOR_SIZE * _CURSOR_DPR), round(_CURSOR_SIZE * _CURSOR_DPR))
    pixmap.setDevicePixelRatio(_CURSOR_DPR)
    pixmap.fill(Qt.GlobalColor.transparent)
    path = _cursor_path(kind)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for color, width in ((QColor(0, 0, 0, 245), 3.5),
                             (QColor(255, 255, 255, 255), 1.5)):
            painter.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
    finally:
        painter.end()
    return QCursor(pixmap, _HOTSPOT, _HOTSPOT)


def paint_trim_bracket(
    painter: QPainter,
    clip_rect: QRectF,
    edge: TrimEdge,
    color: QColor | str,
    active: bool = False,
) -> None:
    """Paint a full-height two-pixel bracket with short inward-facing caps.

    The optional active glow is at most five logical pixels wide. All ink
    stays within the clip, including for very short or narrow clip geometry.
    The caller's pen, brush, clipping, and antialiasing state are preserved.
    """
    if edge not in {"left", "right"}:
        raise ValueError(f"Unknown trim edge: {edge!r}")
    if clip_rect.isEmpty():
        return
    ink = QColor(color)
    if not ink.isValid():
        raise ValueError("Trim bracket color must be valid")
    inward = 1.0 if edge == "left" else -1.0
    boundary = (min(clip_rect.left() + 1.0, clip_rect.center().x())
                if edge == "left" else max(clip_rect.right() - 1.0, clip_rect.center().x()))
    top = min(clip_rect.top() + 1.0, clip_rect.center().y())
    bottom = max(clip_rect.bottom() - 1.0, clip_rect.center().y())
    cap = max(0.0, min(6.0, clip_rect.width() - 2.0))
    path = QPainterPath()
    _line(path, (boundary + inward * cap, top), (boundary, top),
          (boundary, bottom), (boundary + inward * cap, bottom))
    painter.save()
    try:
        painter.setClipRect(clip_rect, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if active:
            glow = QColor(ink)
            glow.setAlpha(min(48, ink.alpha()))
            painter.setPen(QPen(glow, 5.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin))
            painter.drawPath(path)
        painter.setPen(QPen(ink, 2.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin))
        painter.drawPath(path)
    finally:
        painter.restore()


__all__ = ["EditCursorKind", "TrimEdge", "edit_cursor", "paint_trim_bracket"]
