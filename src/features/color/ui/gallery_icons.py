"""Native-scale vector artwork for Color's compact Gallery toolbar."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap


GALLERY_ICON_NAMES = frozenset({
    "sidebar", "import", "sort", "grid", "list", "search", "expand", "more",
    "album", "power_grade", "timeline", "memories", "capture",
    "chevron_down", "chevron_right", "close",
})


def _polyline(painter: QPainter, *points: tuple[float, float]) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    painter.drawPath(path)


class _GalleryIconEngine(QIconEngine):
    """Draw on a twenty-unit grid, retaining fine lines at every screen DPR."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def clone(self) -> QIconEngine:
        return _GalleryIconEngine(self.name)

    def key(self) -> str:
        return "ToposGallery:" + self.name

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        result = QPixmap(size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return result

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State,
                     scale: float) -> QPixmap:
        result = self.pixmap(
            QSize(max(1, round(size.width() * scale)),
                  max(1, round(size.height() * scale))), mode, state)
        result.setDevicePixelRatio(scale)
        return result

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode,
              state: QIcon.State) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(rect.width(), rect.height())
        painter.translate(rect.x() + (rect.width() - side) / 2.0,
                          rect.y() + (rect.height() - side) / 2.0)
        painter.scale(side / 20.0, side / 20.0)
        color = QColor("#55555d" if mode == QIcon.Mode.Disabled else
                       "#dedee3" if state == QIcon.State.On or mode == QIcon.Mode.Active
                       else "#a0a0a7")
        painter.setPen(QPen(color, 1.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        name = self.name
        if name == "sidebar":
            painter.drawRect(QRectF(2.0, 3.5, 16.0, 13.0))
            painter.drawLine(QPointF(7.0, 3.5), QPointF(7.0, 16.5))
            painter.fillRect(QRectF(3.5, 5.0, 2.0, 10.0), color)
        elif name == "import":
            _polyline(painter, (3.0, 12.5), (3.0, 16.0), (17.0, 16.0), (17.0, 12.5))
            _polyline(painter, (10.0, 3.0), (10.0, 11.5))
            _polyline(painter, (6.5, 8.0), (10.0, 11.5), (13.5, 8.0))
            painter.drawLine(QPointF(6.0, 18.0), QPointF(14.0, 18.0))
        elif name == "sort":
            for y, end in ((5.0, 11.5), (9.0, 9.5), (13.0, 7.5)):
                painter.drawLine(QPointF(2.5, y), QPointF(end, y))
            _polyline(painter, (15.5, 3.5), (15.5, 16.0))
            _polyline(painter, (12.5, 13.0), (15.5, 16.0), (18.5, 13.0))
        elif name in {"grid", "memories"}:
            for y in (4.0, 11.0):
                for x in (2.0, 8.0, 14.0):
                    painter.drawRect(QRectF(x, y, 4.0, 5.0))
                    if name == "memories":
                        painter.drawPoint(QPointF(x + 2.0, y + 2.5))
        elif name == "list":
            for y in (4.5, 10.0, 15.5):
                painter.fillRect(QRectF(2.5, y - 0.75, 1.5, 1.5), color)
                painter.drawLine(QPointF(6.0, y), QPointF(17.0, y))
        elif name == "search":
            painter.drawEllipse(QRectF(3.0, 2.5, 10.0, 10.0))
            painter.drawLine(QPointF(11.0, 11.0), QPointF(17.0, 17.0))
        elif name == "expand":
            for points in (
                ((3.0, 7.0), (3.0, 3.0), (7.0, 3.0)),
                ((13.0, 3.0), (17.0, 3.0), (17.0, 7.0)),
                ((17.0, 13.0), (17.0, 17.0), (13.0, 17.0)),
                ((7.0, 17.0), (3.0, 17.0), (3.0, 13.0)),
                ((4.0, 4.0), (8.0, 8.0)), ((12.0, 12.0), (16.0, 16.0)),
            ):
                _polyline(painter, *points)
        elif name == "more":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for x in (4.0, 10.0, 16.0):
                painter.drawEllipse(QRectF(x - 1.0, 9.0, 2.0, 2.0))
        elif name in {"album", "power_grade"}:
            painter.drawRect(QRectF(2.5, 4.5, 15.0, 11.0))
            _polyline(painter, (4.0, 14.0), (8.0, 9.5), (10.5, 12.0), (13.0, 9.5), (16.0, 13.0))
            painter.drawEllipse(QRectF(12.5, 6.5, 2.0, 2.0))
            if name == "power_grade":
                painter.fillRect(QRectF(13.0, 1.0, 6.0, 6.0), QColor("#28282e"))
                _polyline(painter, (16.0, 1.0), (16.0, 6.0))
                _polyline(painter, (13.5, 3.5), (18.5, 3.5))
        elif name == "timeline":
            painter.drawRect(QRectF(2.0, 5.0, 16.0, 9.0))
            painter.drawLine(QPointF(2.0, 9.5), QPointF(18.0, 9.5))
            for x in (6.0, 10.0, 14.0):
                painter.drawLine(QPointF(x, 5.0), QPointF(x, 9.5))
            painter.drawLine(QPointF(4.0, 16.5), QPointF(16.0, 16.5))
        elif name == "capture":
            _polyline(painter, (2.0, 6.0), (6.0, 6.0), (7.5, 3.5), (12.5, 3.5),
                      (14.0, 6.0), (18.0, 6.0), (18.0, 16.5), (2.0, 16.5), (2.0, 6.0))
            painter.drawEllipse(QRectF(6.5, 7.5, 7.0, 7.0))
        elif name == "chevron_down":
            _polyline(painter, (6.0, 8.0), (10.0, 12.0), (14.0, 8.0))
        elif name == "chevron_right":
            _polyline(painter, (8.0, 5.5), (12.5, 10.0), (8.0, 14.5))
        elif name == "close":
            _polyline(painter, (6.0, 6.0), (14.0, 14.0))
            _polyline(painter, (14.0, 6.0), (6.0, 14.0))
        painter.restore()


def gallery_icon(name: str) -> QIcon:
    """Return an outlined Gallery glyph with native disabled/checked variants."""
    if name not in GALLERY_ICON_NAMES:
        raise ValueError(f"Unknown Gallery icon: {name}")
    return QIcon(_GalleryIconEngine(name))
