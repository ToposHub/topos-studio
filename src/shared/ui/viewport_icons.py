"""Small, consistent vector controls shared by video monitor chrome.

Every glyph is drawn on a 20-unit grid by a QIconEngine, so native scaling and
high-DPI screens retain the original outlines.  No font or bitmap is involved.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap,
    QPolygonF,
)


VIEWER_ICON_NAMES = frozenset({
    "source_clip", "source_tape", "timeline", "single_view", "dual_view",
    "more", "chevron_down", "previous", "next", "reverse", "play", "pause",
    "stop", "loop", "step_back", "step_forward", "transform", "guides",
    "match_frame", "mark_in", "mark_out", "source_patch", "proxy", "resolution",
    "bypass", "marker", "previous_marker", "next_marker", "video_only", "audio_only",
    "image_wipe", "split_screen", "highlight", "power_window", "unmix", "mute", "enhanced_viewer",
})


def _path(points: tuple[tuple[float, float], ...], closed: bool = False) -> QPainterPath:
    result = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        result.lineTo(QPointF(*point))
    if closed:
        result.closeSubpath()
    return result


def _line(painter: QPainter, *points: tuple[float, float]) -> None:
    painter.drawPath(_path(points))


def _triangle(painter: QPainter, left: float, reverse: bool = False) -> None:
    points = ((left + 6.0, 4.7), (left, 10.0), (left + 6.0, 15.3)) if reverse else (
        (left, 4.7), (left + 6.0, 10.0), (left, 15.3))
    painter.drawPolygon(QPolygonF([QPointF(*point) for point in points]))


def _film(painter: QPainter, rect: QRectF, holes: bool = True) -> None:
    painter.drawRect(rect)
    if not holes:
        return
    left, right = rect.left() + 2.6, rect.right() - 2.6
    painter.drawLine(QPointF(left, rect.top()), QPointF(left, rect.bottom()))
    painter.drawLine(QPointF(right, rect.top()), QPointF(right, rect.bottom()))
    for offset in (3.0, 6.0, 9.0):
        y = rect.top() + offset
        if y < rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(left, y))
            painter.drawLine(QPointF(right, y), QPointF(rect.right(), y))


class _ViewerIconEngine(QIconEngine):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def clone(self) -> QIconEngine:
        return _ViewerIconEngine(self._name)

    def key(self) -> str:
        return "ToposViewportVector:" + self._name

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float,
    ) -> QPixmap:
        pixmap = self.pixmap(
            QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale))),
            mode, state)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def paint(
        self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(rect.width(), rect.height())
        painter.translate(rect.x() + (rect.width() - side) / 2.0, rect.y() + (rect.height() - side) / 2.0)
        painter.scale(side / 20.0, side / 20.0)
        color = QColor(
            "#555559" if mode == QIcon.Mode.Disabled else
            "#d2d2d6" if mode in {QIcon.Mode.Active, QIcon.Mode.Selected} or state == QIcon.State.On else
            "#969699")
        pen = QPen(color, 1.1)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        self._draw(painter, color, mode)
        painter.restore()

    def _draw(self, painter: QPainter, color: QColor, mode: QIcon.Mode) -> None:
        name = self._name
        if name in {"single_view", "dual_view"}:
            painter.drawRect(QRectF(2.5, 4.5, 15.0, 11.0))
            if name == "dual_view":
                _line(painter, (10.0, 4.5), (10.0, 15.5))
        elif name == "more":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for x in (4.0, 10.0, 16.0):
                painter.drawEllipse(QPointF(x, 10.0), 1.25, 1.25)
        elif name in {"chevron_down", "previous_marker", "next_marker"}:
            points = {
                "chevron_down": ((6.5, 8.0), (10.0, 11.5), (13.5, 8.0)),
                "previous_marker": ((11.5, 6.5), (8.0, 10.0), (11.5, 13.5)),
                "next_marker": ((8.5, 6.5), (12.0, 10.0), (8.5, 13.5)),
            }[name]
            _line(painter, *points)
        elif name in {"play", "reverse", "previous", "next", "step_back", "step_forward"}:
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            reverse = name in {"reverse", "previous", "step_back"}
            if name in {"previous", "next"}:
                _triangle(painter, 3.4, reverse)
                _triangle(painter, 10.0, reverse)
            else:
                _triangle(painter, 6.5, reverse)
            if name in {"previous", "next", "step_back", "step_forward"}:
                x = 2.2 if reverse else 16.5
                painter.drawRect(QRectF(x, 4.8, 1.3, 10.4))
        elif name == "pause":
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(QRectF(5.0, 4.8, 3.2, 10.4))
            painter.drawRect(QRectF(11.8, 4.8, 3.2, 10.4))
        elif name == "stop":
            painter.fillRect(QRectF(5.0, 5.0, 10.0, 10.0), color)
        elif name == "loop":
            path = QPainterPath(QPointF(13.4, 5.6))
            path.cubicTo(6.5, 2.6, 1.9, 6.1, 2.8, 10.8)
            path.cubicTo(3.3, 14.5, 7.3, 16.1, 11.7, 15.2)
            path.cubicTo(15.5, 14.5, 17.8, 11.4, 16.4, 8.5)
            painter.drawPath(path)
            _line(painter, (11.8, 2.6), (14.8, 5.9), (10.4, 7.1))
        elif name in {"source_clip", "video_only"}:
            _film(painter, QRectF(2.7, 4.5, 14.6, 11.0))
            if name == "source_clip":
                _line(painter, (6.7, 12.5), (9.2, 9.0), (11.2, 11.3), (13.0, 9.5))
                painter.drawEllipse(QPointF(12.0, 7.5), 0.65, 0.65)
        elif name == "source_tape":
            for top in (4.5, 11.5):
                painter.drawRect(QRectF(2.5, top, 15.0, 4.0))
                for x in (6.2, 10.0, 13.8):
                    _line(painter, (x, top), (x, top + 4.0))
        elif name == "timeline":
            painter.drawRect(QRectF(2.5, 5.0, 15.0, 10.0))
            for x in (7.5, 12.5):
                _line(painter, (x, 5.0), (x, 15.0))
            _line(painter, (2.5, 8.0), (17.5, 8.0))
            _line(painter, (2.5, 12.0), (17.5, 12.0))
        elif name in {"transform", "guides"}:
            for x, y, dx, dy in ((3.0, 4.0, 4.0, 4.0), (17.0, 4.0, -4.0, 4.0), (3.0, 16.0, 4.0, -4.0), (17.0, 16.0, -4.0, -4.0)):
                _line(painter, (x, y + dy), (x, y), (x + dx, y))
            if name == "transform":
                painter.drawRect(QRectF(5.3, 6.3, 9.4, 7.4))
                for x, y in ((3.0, 4.0), (17.0, 4.0), (3.0, 16.0), (17.0, 16.0)):
                    painter.fillRect(QRectF(x - 1.3, y - 1.3, 2.6, 2.6), color)
            else:
                _line(painter, (10.0, 6.0), (10.0, 14.0))
                _line(painter, (6.0, 10.0), (14.0, 10.0))
        elif name == "match_frame":
            _line(painter, (6.5, 4.2), (3.5, 4.2), (3.5, 15.8), (6.5, 15.8))
            _line(painter, (13.5, 4.2), (16.5, 4.2), (16.5, 15.8), (13.5, 15.8))
            _line(painter, (6.0, 8.0), (12.5, 8.0), (10.7, 6.2))
            _line(painter, (14.0, 12.0), (7.5, 12.0), (9.3, 13.8))
        elif name in {"mark_in", "mark_out"}:
            if name == "mark_out":
                painter.translate(20.0, 0.0)
                painter.scale(-1.0, 1.0)
            _line(painter, (6.5, 4.0), (6.5, 15.5), (15.0, 15.5))
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([QPointF(8.6, 5.0), QPointF(12.6, 7.7), QPointF(8.6, 10.4)]))
        elif name == "source_patch":
            _film(painter, QRectF(2.0, 3.5, 10.5, 8.0))
            _line(painter, (7.5, 12.7), (7.5, 17.0), (10.0, 17.0), (12.4, 15.0), (12.4, 12.7))
            _line(painter, (15.0, 7.0), (17.0, 9.0), (19.0, 7.0))
        elif name in {"proxy", "resolution"}:
            if name == "resolution":
                painter.drawRect(QRectF(2.0, 3.5, 13.5, 10.0))
                painter.drawRect(QRectF(7.0, 9.5, 11.0, 7.0))
                x, y, scale = 8.5, 11.0, 0.72
            else:
                painter.drawRect(QRectF(2.5, 5.0, 15.0, 10.0))
                x, y, scale = 5.5, 7.4, 1.0
            painter.save()
            painter.translate(x, y)
            painter.scale(scale, scale)
            # Deliberately drawn letterforms: no platform font dependency.
            _line(painter, (0.0, 0.0), (0.0, 5.0))
            _line(painter, (3.0, 0.0), (3.0, 5.0))
            _line(painter, (0.0, 2.5), (3.0, 2.5))
            painter.drawRect(QRectF(5.0, 0.0, 3.3, 4.9))
            _line(painter, (7.1, 3.3), (9.0, 5.4))
            painter.restore()
        elif name == "bypass":
            colors = ("#d2b342", "#6fa586", "#b96b82") if mode != QIcon.Mode.Disabled else ("#555559",) * 3
            for start, ring_color in zip((30, 150, 270), colors):
                ring_pen = QPen(QColor(ring_color), 1.35)
                painter.setPen(ring_pen)
                painter.drawArc(QRectF(3.1, 3.1, 13.8, 13.8), start * 16, 105 * 16)
            painter.setPen(QPen(color, 1.1))
            _line(painter, (3.0, 17.0), (17.0, 3.0))
        elif name == "marker":
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([QPointF(10.0, 4.0), QPointF(15.0, 10.0), QPointF(10.0, 16.0), QPointF(5.0, 10.0)]))
        elif name == "image_wipe":
            painter.drawRect(QRectF(2.5, 4.5, 15.0, 11.0))
            painter.fillRect(QRectF(3.0, 5.0, 6.5, 10.0), color)
            _line(painter, (10.0, 3.0), (10.0, 17.0))
        elif name == "split_screen":
            painter.drawRect(QRectF(2.5, 4.5, 15.0, 11.0))
            for x in (7.5, 12.5):
                _line(painter, (x, 4.5), (x, 15.5))
            _line(painter, (2.5, 10.0), (17.5, 10.0))
        elif name == "highlight":
            painter.drawEllipse(QRectF(3.0, 3.0, 14.0, 14.0))
            painter.save()
            painter.setClipRect(QRectF(10.0, 2.0, 9.0, 16.0))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(3.0, 3.0, 14.0, 14.0))
            painter.restore()
            _line(painter, (2.0, 18.0), (18.0, 2.0))
        elif name == "power_window":
            painter.drawEllipse(QRectF(3.5, 4.5, 13.0, 11.0))
            for x, y in ((10.0, 3.5), (10.0, 16.5), (2.5, 10.0), (17.5, 10.0)):
                painter.fillRect(QRectF(x - 1.0, y - 1.0, 2.0, 2.0), color)
        elif name == "unmix":
            for y in (5.0, 9.0, 13.0):
                _line(painter, (3.0, y + 2.0), (10.0, y - 1.0), (17.0, y + 2.0), (10.0, y + 5.0), (3.0, y + 2.0))
        elif name == "mute":
            speaker = ((3.0, 7.5), (6.0, 7.5), (10.0, 4.0), (10.0, 16.0), (6.0, 12.5), (3.0, 12.5))
            painter.setBrush(color)
            painter.drawPath(_path(speaker, True))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(8.0, 5.0, 8.0, 10.0), -60 * 16, 120 * 16)
            painter.drawArc(QRectF(6.0, 2.5, 13.0, 15.0), -55 * 16, 110 * 16)
        elif name == "enhanced_viewer":
            for x, y, dx, dy in ((3.0, 3.0, 5.0, 5.0), (17.0, 3.0, -5.0, 5.0),
                                  (3.0, 17.0, 5.0, -5.0), (17.0, 17.0, -5.0, -5.0)):
                _line(painter, (x, y + dy), (x, y), (x + dx, y))
        elif name == "audio_only":
            for x, half in ((3.0, 1.2), (5.8, 3.5), (8.6, 6.0), (11.4, 4.0), (14.2, 2.2), (17.0, 1.0)):
                _line(painter, (x, 10.0 - half), (x, 10.0 + half))


def viewer_icon(name: str) -> QIcon:
    """Return a vector icon; reject unknown names to catch UI wiring errors."""
    if name not in VIEWER_ICON_NAMES:
        raise ValueError(f"Unknown viewer icon: {name}")
    return QIcon(_ViewerIconEngine(name))
