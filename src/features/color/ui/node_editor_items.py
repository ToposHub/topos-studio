"""Painter-only items for the Color node editor.

The dimensions and interaction affordances follow the Resolve 20 Color-page
reference.  Items contain presentation state only: they do not obtain frames,
evaluate grades, or own GPU resources.  The host supplies optional thumbnails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QLinearGradient, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QPixmap, QPolygonF,
)
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsObject, QGraphicsPathItem,
    QGraphicsSceneHoverEvent, QGraphicsSceneMouseEvent,
    QStyleOptionGraphicsItem, QWidget,
)

from src.shared.i18n.i18n import I18N

if TYPE_CHECKING:
    from .node_editor_state import ConnectionDraft, NodeDraft


COLORS: dict[str, str] = {
    "background": "#28282e",
    "card": "#333337",
    "card_bottom": "#252529",
    "border": "#101012",
    "selection": "#c65140",
    "image": "#8bc326",
    "key": "#26a9e0",
    "wire": "#77777e",
    "key_wire": "#168fc5",
    "text": "#e4e4e6",
    "muted": "#96969c",
}

_TERMINALS = frozenset({"source", "output", "alpha_output"})
_MIXERS = frozenset({"parallel_mixer", "layer_mixer", "key_mixer"})
_CHANNELS = frozenset({"splitter", "combiner"})


def _kind(node: NodeDraft) -> str:
    """Allow the presentation enum without depending on the render model."""
    value = node.kind
    return str(getattr(value, "value", value))


def _font(size: int = 10) -> QFont:
    font = QFont()
    font.setPixelSize(size)
    return font


class NodePortItem(QGraphicsItem):
    """Small visible connector with an enlarged drag target."""

    def __init__(
        self,
        node_id: str,
        port_name: str,
        direction: str,
        data_type: str,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.node_id = node_id
        self.port_name = port_name
        self.direction = direction
        self.data_type = data_type
        self.highlighted = False
        self.terminal = False
        self.setZValue(3.0)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setCursor(Qt.CursorShape.CrossCursor)
        kind = I18N.tr("color_nodes.key", "Key") if data_type == "key" else "RGB"
        flow = (
            I18N.tr("color_nodes.input", "Input")
            if direction == "in"
            else I18N.tr("color_nodes.output", "Output")
        )
        self.setToolTip(f"{kind} {flow}")

    def type(self) -> int:
        return int(QGraphicsItem.UserType) + 411

    def boundingRect(self) -> QRectF:
        return QRectF(-8.0, -8.0, 16.0, 16.0)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(COLORS["key" if self.data_type == "key" else "image"])
        if self.highlighted:
            color = color.lighter(145)
            painter.setPen(QPen(color, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(), 6.5, 6.5)
        painter.setPen(QPen(QColor("#111113"), 0.8))
        painter.setBrush(color)
        if self.terminal:
            painter.drawEllipse(QPointF(), 3.5, 3.5)
        elif self.direction == "in":
            painter.drawPolygon(QPolygonF([
                QPointF(-3.5, -4.0), QPointF(4.0, 0.0),
                QPointF(-3.5, 4.0),
            ]))
        else:
            painter.drawRect(QRectF(-3.5, -3.5, 7.0, 7.0))
        painter.restore()

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self.highlighted = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self.highlighted = False
        self.update()
        super().hoverLeaveEvent(event)


class NodeCardItem(QGraphicsObject):
    """A compact corrector, mixer, channel router, or terminal card."""

    moved = pyqtSignal(str)
    activated = pyqtSignal(str)
    opened = pyqtSignal(str)

    def __init__(
        self,
        node: NodeDraft,
        ports: Mapping[str, tuple[str, str]],
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.node = node
        self.ports: dict[str, NodePortItem] = {}
        self._port_spec: dict[str, tuple[str, str]] = dict(ports)
        self._thumbnail = QPixmap()
        self._initializing = True
        self._hovered = False
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self.setZValue(1.0)
        self._build_ports()
        self.refresh(node)
        self.setPos(float(node.x), float(node.y))
        self._initializing = False

    def type(self) -> int:
        return int(QGraphicsItem.UserType) + 410

    def body_rect(self) -> QRectF:
        kind = _kind(self.node)
        if kind in _TERMINALS:
            return QRectF(0.0, 0.0, 14.0, 26.0)
        if kind in _MIXERS:
            # Keep mixer inputs distinct even for a large branch count.
            return QRectF(0.0, 0.0, 28.0, max(50.0, 14.0 * self.node.input_count + 10.0))
        if kind in _CHANNELS:
            return QRectF(0.0, 0.0, 32.0, 64.0)
        return QRectF(0.0, 0.0, 64.0, 56.0)

    def boundingRect(self) -> QRectF:
        rect = self.body_rect().adjusted(-10.0, -2.0, 10.0, 2.0)
        if self.node.label:
            rect = rect.united(QRectF(-60.0, -20.0, 184.0, 18.0))
        return rect

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self.body_rect(), 2.0, 2.0)
        return path

    def _build_ports(self) -> None:
        for name, (direction, data_type) in self._port_spec.items():
            self.ports[name] = NodePortItem(
                self.node.id, name, direction, data_type, self,
            )
        self._position_ports()

    def _position_ports(self) -> None:
        rect = self.body_rect()
        kind = _kind(self.node)
        if kind in _TERMINALS:
            for port in self.ports.values():
                port.terminal = True
                port.setPos(rect.center())
            return
        for direction in ("in", "out"):
            group = [port for port in self.ports.values() if port.direction == direction]
            for index, port in enumerate(group):
                x = -8.0 if direction == "in" else rect.width() + 8.0
                if kind in _MIXERS or kind in _CHANNELS or len(group) > 2:
                    y = rect.height() * (index + 1.0) / (len(group) + 1.0)
                else:
                    y = 40.0 if port.data_type == "key" else 16.0
                port.setPos(x, y)

    def refresh(
        self,
        node: NodeDraft,
        ports: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        """Update display fields; preserve connector objects when compatible."""
        self.prepareGeometryChange()
        self.node = node
        if ports is not None and dict(ports) != self._port_spec:
            for port in self.ports.values():
                if port.scene() is not None:
                    port.scene().removeItem(port)
                port.setParentItem(None)
            self.ports.clear()
            self._port_spec = dict(ports)
            self._build_ports()
        self._position_ports()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not node.locked)
        self.setCursor(
            Qt.CursorShape.ArrowCursor if node.locked else Qt.CursorShape.OpenHandCursor
        )
        self.setToolTip(node.label or I18N.tr("color_nodes." + _kind(node), _kind(node).replace("_", " ").title()))
        self.update()

    def set_thumbnail(self, pixmap: QPixmap | None) -> None:
        """Receive a host-created preview; this item never requests a frame."""
        self._thumbnail = QPixmap(pixmap) if pixmap is not None else QPixmap()
        self.update()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.body_rect()
        kind = _kind(self.node)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(COLORS["card"]))
        gradient.setColorAt(1.0, QColor(COLORS["card_bottom"]))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(COLORS["border"]), 1.0))
        painter.drawRoundedRect(rect, 2.0, 2.0)
        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(COLORS["selection"]), 1.2))
            painter.drawRoundedRect(rect.adjusted(0.8, 0.8, -0.8, -0.8), 2.0, 2.0)
        elif self._hovered:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#65656a"), 0.8))
            painter.drawRoundedRect(rect.adjusted(0.7, 0.7, -0.7, -0.7), 2.0, 2.0)
        painter.setFont(_font())
        if self.node.label:
            painter.setPen(QColor(COLORS["text"]))
            label = painter.fontMetrics().elidedText(
                self.node.label, Qt.TextElideMode.ElideRight, 178,
            )
            painter.drawText(
                QRectF(rect.center().x() - 92.0, -19.0, 184.0, 17.0),
                Qt.AlignmentFlag.AlignCenter, label,
            )
        if kind in _TERMINALS:
            pass  # Its child connector supplies the central colored dot.
        elif kind in _MIXERS or kind in _CHANNELS:
            self._paint_router(painter, rect, kind)
        else:
            self._paint_corrector(painter, rect, kind)
        painter.restore()

    def _paint_corrector(self, painter: QPainter, rect: QRectF, kind: str) -> None:
        image_rect = QRectF(4.0, 4.0, 56.0, 32.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1d1d21"))
        painter.drawRect(image_rect)
        if not self._thumbnail.isNull():
            source = QRectF(self._thumbnail.rect())
            target_ratio = image_rect.width() / image_rect.height()
            if source.width() / source.height() > target_ratio:
                width = source.height() * target_ratio
                source = QRectF((source.width() - width) / 2.0, 0.0, width, source.height())
            else:
                height = source.width() / target_ratio
                source = QRectF(0.0, (source.height() - height) / 2.0, source.width(), height)
            painter.drawPixmap(image_rect, self._thumbnail, source)
        else:
            painter.setPen(QPen(QColor("#56565d"), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for dx in (-7.0, 0.0, 7.0):
                painter.drawEllipse(QPointF(32.0 + dx, 20.0), 6.5, 6.5)
            painter.setPen(QPen(QColor("#707078"), 1.0))
            painter.drawLine(QPointF(22.0, 27.0), QPointF(42.0, 13.0))
        if not self.node.enabled:
            painter.fillRect(image_rect, QColor(15, 15, 18, 165))
        footer = QRectF(4.0, 38.0, 56.0, 14.0)
        if self.node.color and QColor.isValidColor(self.node.color):
            color = QColor(self.node.color)
            color.setAlpha(115)
            painter.fillRect(footer, color)
        painter.setFont(_font(10))
        painter.setPen(QColor(COLORS["text"] if self.node.enabled else COLORS["muted"]))
        painter.drawText(QRectF(9.0, 38.0, 18.0, 15.0), Qt.AlignmentFlag.AlignVCenter, f"{self.node.number:02d}")
        if not self.node.enabled:
            painter.setPen(QPen(QColor(COLORS["muted"]), 1.0))
            painter.drawLine(QPointF(7.0, 48.0), QPointF(27.0, 42.0))
        painter.setPen(QPen(QColor("#c4c4ca"), 0.8))
        if kind == "compound":
            self._paint_diamond(painter, QPointF(38.0, 44.0), 4.5)
            self._paint_diamond(painter, QPointF(42.0, 47.0), 4.5)
        elif self.node.grade:
            painter.drawLine(QPointF(33.0, 49.0), QPointF(33.0, 40.0))
            painter.drawLine(QPointF(33.0, 49.0), QPointF(43.0, 49.0))
            path = QPainterPath(QPointF(34.0, 48.0))
            path.cubicTo(39.0, 48.0, 38.0, 41.0, 43.0, 41.0)
            painter.drawPath(path)
        if self.node.locked:
            painter.drawRoundedRect(QRectF(49.0, 43.0, 6.0, 5.0), 0.5, 0.5)
            painter.drawArc(QRectF(50.0, 39.0, 4.0, 7.0), 0, 180 * 16)
        elif self.node.shared_id:
            painter.drawRoundedRect(QRectF(47.0, 42.0, 6.0, 4.0), 2.0, 2.0)
            painter.drawRoundedRect(QRectF(51.0, 45.0, 6.0, 4.0), 2.0, 2.0)
        if self.node.cache_mode == "on":
            painter.fillRect(QRectF(4.0, rect.height() - 2.8, 56.0, 1.0), QColor("#329fd6"))

    @staticmethod
    def _paint_diamond(painter: QPainter, center: QPointF, radius: float) -> None:
        painter.drawPolygon(QPolygonF([
            QPointF(center.x(), center.y() - radius / 2.0),
            QPointF(center.x() + radius, center.y()),
            QPointF(center.x(), center.y() + radius / 2.0),
            QPointF(center.x() - radius, center.y()),
        ]))

    def _paint_router(self, painter: QPainter, rect: QRectF, kind: str) -> None:
        center = rect.center()
        painter.setPen(QPen(QColor(COLORS["text"]), 0.9))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y = center.x(), center.y()
        if kind == "parallel_mixer":
            for offset in (-3.0, 0.0, 3.0):
                self._paint_diamond(painter, QPointF(x, y + offset), 5.0)
        elif kind == "layer_mixer":
            for offset in (-3.0, 0.0, 3.0):
                painter.drawLine(QPointF(x - 5.0, y + offset), QPointF(x + 5.0, y + offset))
            painter.drawLine(QPointF(x - 4.0, y + 5.0), QPointF(x + 4.0, y - 5.0))
        elif kind == "key_mixer":
            painter.setPen(QPen(QColor(COLORS["key"]), 1.0))
            painter.drawEllipse(QPointF(x - 2.5, y - 2.0), 3.0, 3.0)
            painter.drawLine(QPointF(x, y), QPointF(x + 5.0, y + 5.0))
            painter.drawLine(QPointF(x + 3.0, y + 3.0), QPointF(x + 5.0, y + 1.0))
        else:
            for index, color in enumerate(("#db6965", "#8cc477", "#669bd7")):
                painter.setPen(QPen(QColor(color), 1.0))
                yy = y + (index - 1) * 8.0
                if kind == "splitter":
                    painter.drawLine(QPointF(x - 5.0, y), QPointF(x + 6.0, yy))
                else:
                    painter.drawLine(QPointF(x - 6.0, yy), QPointF(x + 5.0, y))

    def itemChange(
        self,
        change: QGraphicsItem.GraphicsItemChange,
        value: Any,
    ) -> Any:
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            if not self._initializing:
                self.moved.emit(self.node.id)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return result

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.node.id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit(self.node.id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)


class ConnectionItem(QGraphicsPathItem):
    """Resolve-style straight image/key wire with a generous hit target."""

    def __init__(
        self,
        connection: ConnectionDraft,
        start: QPointF,
        end: QPointF,
        data_type: str = "image",
    ) -> None:
        super().__init__()
        self.connection = connection
        self.data_type = data_type
        self._highlighted = False
        self._hovered = False
        self.setZValue(-2.0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{connection.src_port} → {connection.dst_port}")
        self.set_endpoints(start, end)
        self._update_pen()

    def type(self) -> int:
        return int(QGraphicsItem.UserType) + 412

    def set_endpoints(self, start: QPointF, end: QPointF) -> None:
        path = QPainterPath(start)
        path.lineTo(end)
        self.setPath(path)

    def set_highlighted(self, highlighted: bool) -> None:
        self._highlighted = highlighted
        self._update_pen()

    def _update_pen(self) -> None:
        color = QColor(COLORS["key_wire" if self.data_type == "key" else "wire"])
        if self._highlighted:
            color = QColor(COLORS["image"] if self.data_type == "image" else COLORS["key"])
        elif self._hovered or self.isSelected():
            color = color.lighter(160)
        pen = QPen(color, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(self.path())

    def itemChange(
        self,
        change: QGraphicsItem.GraphicsItemChange,
        value: Any,
    ) -> Any:
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._update_pen()
        return result

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hovered = True
        self._update_pen()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hovered = False
        self._update_pen()
        super().hoverLeaveEvent(event)
