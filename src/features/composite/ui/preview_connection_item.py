"""Preview Connection Item — dashed curve from a previewed node to the Output node.

Visual indicator for the node-preview pipeline (Nuke-style single-viewer
preview). When a node is being previewed in the viewer, a dashed, animated
curve is drawn from that node to the Output node so the user can see at a
glance which node is currently feeding the viewer.

It is intentionally NOT a real ConnectionItem:
- It carries no data and is not part of the graph topology.
- It cannot be selected, dragged, or deleted like a real connection.
- It uses a distinct dashed cyan style so it's never confused with real wiring.

Lifecycle: owned and toggled by NodeEditorWidget.set_preview_node(). The node
editor positions it whenever either endpoint node moves.
"""

from typing import Optional

from PyQt6.QtCore import QPointF, QTimer, QObject, Qt
from PyQt6.QtGui import QPainterPath, QPen, QColor, QPainter, QBrush
from PyQt6.QtWidgets import QGraphicsPathItem, QGraphicsItem


class _PreviewFlowDriver(QObject):
    """Drives the marching-ants dash animation for all preview connections.

    A single shared QTimer (parented to this QObject so PyQt6 6.10+ fires it)
    advances the dash offset. Started/stopped on demand to avoid idle wakeups.
    """

    INTERVAL_MS = 60  # ~16fps, light CPU

    def __init__(self) -> None:
        super().__init__()
        self._items: list = []
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._advance)
        self._offset = 0.0

    def add(self, item: "PreviewConnectionItem") -> None:
        if item not in self._items:
            self._items.append(item)
            if not self._timer.isActive():
                self._timer.start()

    def remove(self, item: "PreviewConnectionItem") -> None:
        if item in self._items:
            self._items.remove(item)
        if not self._items and self._timer.isActive():
            self._timer.stop()

    def _advance(self) -> None:
        # Marching-ants: shift the dash pattern along the curve.
        self._offset -= 1.0
        for item in self._items:
            item._set_dash_offset(self._offset)


# Module-level singleton; lazy so tests that never draw don't allocate a timer.
_flow_driver: Optional[_PreviewFlowDriver] = None


def _get_flow_driver() -> _PreviewFlowDriver:
    global _flow_driver
    if _flow_driver is None:
        _flow_driver = _PreviewFlowDriver()
    return _flow_driver


class PreviewConnectionItem(QGraphicsPathItem):
    """Dashed, animated curve from a previewed node's output to the Output node.

    The two endpoint NodeItems are referenced (not ports) so the curve survives
    reconnects and works even when the previewed node has multiple outputs.
    Endpoint positions are recomputed in ``update_path()`` from the item rects.
    """

    # Distinct from real ConnectionItem colours: cyan = "preview, not a real wire".
    COLOR = QColor(80, 200, 230, 210)
    GLOW_COLOR = QColor(80, 200, 230, 60)
    PEN_WIDTH = 2.4

    def __init__(self, src_node_item: QGraphicsItem,
                 dst_node_item: QGraphicsItem, parent=None) -> None:
        super().__init__(parent)
        self._src_item = src_node_item
        self._dst_item = dst_node_item

        pen = QPen(self.COLOR, self.PEN_WIDTH)
        pen.setCosmetic(True)  # constant screen width regardless of zoom
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([6.0, 5.0])  # dashes longer than gaps → clear "dashed"
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        self.setPen(pen)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))

        # Non-interactive: it's a visualization, not an editable wire.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # Sit slightly above real connections so it's visible over wiring.
        self.setZValue(0.6)

        self._dash_offset = 0.0
        _get_flow_driver().add(self)
        self.update_path()

    def _set_dash_offset(self, offset: float) -> None:
        self._dash_offset = offset
        pen = self.pen()
        pen.setDashOffset(offset)
        self.setPen(pen)
        self.update()

    def update_path(self) -> None:
        """Recompute the dashed curve from current endpoint positions."""
        if self._src_item is None or self._dst_item is None:
            return
        src_rect = self._src_item.sceneBoundingRect()
        dst_rect = self._dst_item.sceneBoundingRect()
        # Source: right edge, vertical centre.
        src = QPointF(src_rect.right(), src_rect.center().y())
        # Destination: left edge, vertical centre (Output node's input side).
        dst = QPointF(dst_rect.left(), dst_rect.center().y())
        self.setPath(self._create_bezier(src, dst))

    @staticmethod
    def _create_bezier(src: QPointF, dst: QPointF) -> QPainterPath:
        """Smooth horizontal S-curve, mirroring ConnectionItem geometry."""
        path = QPainterPath()
        path.moveTo(src)
        dx = abs(dst.x() - src.x())
        offset = max(dx * 0.5, 60.0)
        ctrl1 = QPointF(src.x() + offset, src.y())
        ctrl2 = QPointF(dst.x() - offset, dst.y())
        path.cubicTo(ctrl1, ctrl2, dst)
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Soft glow underlay so the dashed line reads against busy graphs.
        glow = QPen(self.GLOW_COLOR, 8.0)
        glow.setCosmetic(True)
        glow.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(glow)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        # Dashed main stroke (pen already carries the dash offset).
        super().paint(painter, option, widget)

    def remove_from_scene(self) -> None:
        """Detach from the animation driver and remove from the scene."""
        _get_flow_driver().remove(self)
        scene = self.scene()
        if scene is not None:
            self.setParentItem(None)
            scene.removeItem(self)
