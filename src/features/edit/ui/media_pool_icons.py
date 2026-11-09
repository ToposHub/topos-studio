"""Media-pool vectors on a 16-unit grid; no font or bitmap dependencies."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap

from src.shared.ui.style_sheet import C


POOL_ICON_NAMES = frozenset({
    "sidebar", "tag", "relink", "analysis", "metadata", "grid", "list",
    "search", "chevron-down", "sort", "more", "back", "warning", "info",
    "video", "audio", "image", "timeline",
})


def _line(painter: QPainter, *points: tuple[float, float], closed: bool = False) -> None:
    """Keep coordinates fractional until the target device rasterizes them."""
    if not points:
        return
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if closed:
        path.closeSubpath()
    painter.drawPath(path)


def draw_timeline_glyph(painter: QPainter, rect: QRect | QRectF,
                        color: str | QColor = C.TEXT_PRIMARY) -> None:
    """Draw a three-frame filmstrip over two tracks on a 14×12 logical grid."""
    target = QRectF(rect)
    if target.isEmpty():
        return
    painter.save()
    scale = min(target.width() / 14.0, target.height() / 12.0)
    painter.translate(target.center().x() - 7.0 * scale,
                      target.center().y() - 6.0 * scale)
    painter.scale(scale, scale)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.0)
    pen.setCapStyle(Qt.PenCapStyle.SquareCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(.7, .7, 12.6, 5.6), .6, .6)
    for x in (4.9, 9.1):
        _line(painter, (x, 1.0), (x, 6.0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(QRectF(.7, 8.0, 4.5, 1.5), .25, .25)
    painter.drawRoundedRect(QRectF(5.9, 8.0, 7.4, 1.5), .25, .25)
    painter.drawRoundedRect(QRectF(.7, 10.5, 7.0, 1.5), .25, .25)
    painter.drawRoundedRect(QRectF(8.4, 10.5, 4.9, 1.5), .25, .25)
    painter.restore()


class _PoolIconEngine(QIconEngine):
    """Rasterize vectors at their final size, including high-DPI requests."""

    def __init__(self, name: str, color: str) -> None:
        super().__init__()
        self._name = name
        self._color = color

    def clone(self) -> QIconEngine:
        return _PoolIconEngine(self._name, self._color)

    def key(self) -> str:
        return f"ToposMediaPoolVector:{self._name}:{self._color}"

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        result = QPixmap(size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return result

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode,
                     state: QIcon.State, scale: float) -> QPixmap:
        ratio = scale if scale > 0.0 else 1.0
        result = self.pixmap(QSize(max(1, round(size.width() * ratio)),
                                  max(1, round(size.height() * ratio))), mode, state)
        result.setDevicePixelRatio(ratio)
        return result

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode,
              state: QIcon.State) -> None:
        if rect.isEmpty():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(rect.width(), rect.height())
        painter.translate(rect.x() + (rect.width() - side) / 2.0,
                          rect.y() + (rect.height() - side) / 2.0)
        painter.scale(side / 16.0, side / 16.0)
        color = QColor(C.TEXT_PRIMARY if self._color == C.TEXT_HINT and (state == QIcon.State.On or mode in (QIcon.Mode.Active, QIcon.Mode.Selected)) else self._color)
        if mode == QIcon.Mode.Disabled:
            color.setAlphaF(color.alphaF() * 0.42)
        pen = QPen(color, 1.1)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        self._draw(painter, color)
        painter.restore()

    def _draw(self, painter: QPainter, color: QColor) -> None:
        name = self._name
        if name == "sidebar":
            painter.drawRect(QRectF(1.5, 2.5, 13.0, 11.0))
            _line(painter, (5.5, 2.5), (5.5, 13.5))
        elif name == "tag":
            _line(painter, (1.5, 3.5), (7.5, 3.5), (11.5, 7.5),
                  (6.5, 12.5), (1.5, 7.5), closed=True)
            painter.drawEllipse(QPointF(4.3, 6.0), 0.7, 0.7)
            _line(painter, (12.0, 10.0), (13.5, 11.5), (15.0, 10.0))
        elif name == "relink":
            pen = QPen(color, 1.45)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.save()
            painter.translate(8.0, 8.0)
            painter.rotate(-42)
            left = QPainterPath()
            left.moveTo(-.8, -2.6)
            left.lineTo(-3.8, -2.6)
            left.cubicTo(-7.5, -2.6, -7.5, 2.6, -3.8, 2.6)
            left.lineTo(-.8, 2.6)
            right = QPainterPath()
            right.moveTo(.8, -2.6)
            right.lineTo(3.8, -2.6)
            right.cubicTo(7.5, -2.6, 7.5, 2.6, 3.8, 2.6)
            right.lineTo(.8, 2.6)
            painter.drawPath(left)
            painter.drawPath(right)
            _line(painter, (-2.5, 0), (2.5, 0))
            painter.restore()
        elif name == "analysis":
            _line(painter, (2.5, 3.0), (13.5, 3.0), (13.5, 11.0),
                  (6.0, 11.0), (3.5, 13.0), (3.5, 11.0), (2.5, 11.0), closed=True)
            for y, right in ((5.3, 11.0), (7.3, 9.0), (9.3, 10.0)):
                _line(painter, (5.0, y), (right, y))
        elif name == "metadata":
            painter.drawRect(QRectF(1.5, 3.5, 13.0, 9.0))
            painter.drawRect(QRectF(3.0, 5.0, 4.0, 6.0))
            for y, right in ((5.5, 12.5), (8.0, 11.5), (10.5, 12.5)):
                _line(painter, (9.0, y), (right, y))
        elif name == "grid":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for x in (1.5, 6.5, 11.5):
                for y in (2.0, 6.5, 11.0):
                    painter.drawRect(QRectF(x, y, 3.0, 3.0))
        elif name == "list":
            for y in (3.5, 7.5, 11.5):
                painter.fillRect(QRectF(1.5, y, 2.0, 2.0), color)
                _line(painter, (5.5, y + 1.0), (14.5, y + 1.0))
        elif name == "search":
            painter.drawEllipse(QRectF(2.0, 1.5, 8.0, 8.0))
            _line(painter, (9.0, 8.5), (13.5, 13.0))
        elif name == "chevron-down":
            _line(painter, (4.0, 6.0), (8.0, 10.0), (12.0, 6.0))
        elif name == "sort":
            for y, right in ((3.5, 9.5), (7.5, 7.5), (11.5, 5.5)):
                _line(painter, (1.5, y), (right, y))
            _line(painter, (12.5, 2.5), (12.5, 13.0))
            _line(painter, (10.0, 10.5), (12.5, 13.0), (15.0, 10.5))
        elif name == "more":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for x in (3.0, 8.0, 13.0):
                painter.drawEllipse(QPointF(x, 8.0), 1.0, 1.0)
        elif name == "back":
            _line(painter, (10.0, 3.5), (5.5, 8.0), (10.0, 12.5))
        elif name == "warning":
            _line(painter, (8.0, 1.8), (14.5, 13.5), (1.5, 13.5), closed=True)
            _line(painter, (8.0, 5.5), (8.0, 9.0))
            painter.fillRect(QRectF(7.5, 11.0, 1.0, 1.0), color)
        elif name == "info":
            painter.drawEllipse(QRectF(1.5, 1.5, 13.0, 13.0))
            painter.fillRect(QRectF(7.4, 4.0, 1.2, 1.2), color)
            _line(painter, (7.0, 7.0), (8.0, 7.0), (8.0, 11.5))
            _line(painter, (6.5, 11.5), (9.5, 11.5))
        elif name == "video":
            painter.drawRect(QRectF(1.5, 3.5, 13.0, 9.0))
            for x in (4.5, 11.5):
                _line(painter, (x, 3.5), (x, 12.5))
            for y in (6.5, 9.5):
                _line(painter, (1.5, y), (4.5, y))
                _line(painter, (11.5, y), (14.5, y))
        elif name == "audio":
            _line(painter, (6.0, 11.5), (6.0, 3.0), (13.0, 1.8), (13.0, 10.0))
            _line(painter, (6.0, 5.5), (13.0, 4.3))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(2.0, 10.0, 4.0, 3.0))
            painter.drawEllipse(QRectF(9.0, 8.5, 4.0, 3.0))
        elif name == "image":
            painter.drawRect(QRectF(1.5, 2.5, 13.0, 11.0))
            painter.drawEllipse(QPointF(5.0, 6.0), 1.1, 1.1)
            _line(painter, (2.0, 12.5), (6.0, 8.5), (8.5, 10.5),
                  (11.5, 6.5), (14.0, 10.0))
        elif name == "timeline":
            draw_timeline_glyph(painter, QRectF(1.0, 2.0, 14.0, 12.0), color)


def pool_icon(name: str, color: str = C.TEXT_HINT) -> QIcon:
    """Create an icon, rejecting misspellings instead of returning blanks."""
    normalized = "chevron-down" if name == "chevron_down" else name
    if normalized not in POOL_ICON_NAMES:
        raise ValueError(f"Unknown media-pool icon: {name}")
    if not QColor.isValidColorName(color):
        raise ValueError(f"Invalid media-pool icon color: {color}")
    return QIcon(_PoolIconEngine(normalized, color))
