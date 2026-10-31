"""
Minimap Widget - Small overview of the node graph.

Displays a bird's eye view of all nodes, connections, and the current viewport.
Features: click/drag navigation, hover tooltips, selected node highlight,
collapsible panel, and smooth viewport tracking.
"""

from PyQt6.QtWidgets import QWidget, QGraphicsView, QToolButton
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont,
    QMouseEvent, )

from src.shared.i18n.i18n import I18N


class MinimapWidget(QWidget):
    """
    Interactive minimap showing all nodes and connections in the graph.

    Features:
    - Node rectangles with header colors
    - Connection lines between nodes
    - Viewport indicator (draggable)
    - Hover highlighting with tooltip
    - Selected node highlight
    - Collapsible with toggle button
    """

    navigate_requested = pyqtSignal(QPointF)  # Scene position to center on

    MINIMAP_WIDTH = 200
    MINIMAP_HEIGHT = 140
    COLLAPSED_HEIGHT = 24
    MARGIN = 8
    CORNER_RADIUS = 6

    def __init__(self, editor: QGraphicsView, parent=None) -> None:
        super().__init__(parent)

        self._editor = editor
        self._nodes_data = []       # [(x, y, w, h, color, title, node_id), ...]
        self._connections = []      # [(x1, y1, x2, y2, color), ...]
        self._viewport_rect = QRectF()
        self._selected_node_id = None
        self._hovered_node_idx = -1
        self._dragging = False
        self._collapsed = False

        # Transform cache
        self._scale = 1.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._scene_rect = QRectF()

        self.setFixedSize(self.MINIMAP_WIDTH, self.MINIMAP_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Collapse toggle button
        self._toggle_btn = QToolButton(self)
        self._toggle_btn.setFixedSize(16, 16)
        self._toggle_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._toggle_btn.setToolTip(I18N.tr("minimap.toggle", "收起/展开小地图"))
        self._toggle_btn.setStyleSheet("""
            QToolButton {
                background: rgba(60, 60, 65, 180);
                border: 1px solid rgba(100, 100, 105, 120);
                border-radius: 3px;
                color: #aaa;
                font-size: 10px;
                font-weight: bold;
            }
            QToolButton:hover {
                background: rgba(80, 80, 85, 200);
                color: #ddd;
            }
        """)
        self._toggle_btn.clicked.connect(self._toggle_collapse)
        self._position_toggle_btn()

    # ── Public API ──

    def set_selected_node(self, node_id: str | None) -> None:
        """Highlight the given node in the minimap."""
        if self._selected_node_id != node_id:
            self._selected_node_id = node_id
            self.update()

    def refresh_viewport(self) -> None:
        """Lightweight viewport-only update (no node rescan)."""
        if self._editor.scene() is None:
            return
        self._viewport_rect = self._editor.mapToScene(
            self._editor.viewport().rect()
        ).boundingRect()
        self.update()

    def update_from_editor(self) -> None:
        """Rebuild node and connection data from the editor's scene."""
        self._nodes_data.clear()
        self._connections.clear()

        scene = self._editor.scene()
        if scene is None:
            return

        from .node_item import NodeItem
        from .connection_item import ConnectionItem

        # Collect nodes
        for item in scene.items():
            if isinstance(item, NodeItem):
                rect = item.sceneBoundingRect()
                self._nodes_data.append((
                    rect.x(), rect.y(), rect.width(), rect.height(),
                    QColor(item.header_color), item.node_title, item.node_id,
                ))

        # Collect connections
        for item in scene.items():
            if isinstance(item, ConnectionItem):
                if item.src_port is not None and item.dst_port is not None:
                    try:
                        src = item.src_port.get_center_scene_pos()
                        dst = item.dst_port.get_center_scene_pos()
                        color = item.DATA_TYPE_COLORS.get(
                            item.data_type, QColor(120, 120, 130, 150)
                        )
                        self._connections.append((
                            src.x(), src.y(), dst.x(), dst.y(), QColor(color)
                        ))
                    except RuntimeError:
                        pass

        # Cache viewport rect
        self._viewport_rect = self._editor.mapToScene(
            self._editor.viewport().rect()
        ).boundingRect()

        # Recalculate transform
        self._compute_transform()
        self.update()

    # ── Transform ──

    def _compute_transform(self) -> None:
        """Precompute scene-to-minimap transform."""
        self._scene_rect = QRectF()
        for x, y, w, h, *_ in self._nodes_data:
            self._scene_rect = self._scene_rect.united(QRectF(x, y, w, h))

        if not self._scene_rect.isValid():
            self._scale = 1.0
            self._offset_x = self.MARGIN
            self._offset_y = self.MARGIN
            return

        self._scene_rect = self._scene_rect.adjusted(-100, -60, 100, 60)

        available = QRectF(
            self.MARGIN, self.MARGIN,
            self.width() - 2 * self.MARGIN,
            self.height() - 2 * self.MARGIN,
        )
        scale_x = available.width() / self._scene_rect.width()
        scale_y = available.height() / self._scene_rect.height()
        self._scale = min(scale_x, scale_y)

        scaled_w = self._scene_rect.width() * self._scale
        scaled_h = self._scene_rect.height() * self._scale
        self._offset_x = available.x() + (available.width() - scaled_w) / 2
        self._offset_y = available.y() + (available.height() - scaled_h) / 2

    def _scene_to_minimap(self, sx, sy):
        """Convert scene coordinates to minimap pixel coordinates."""
        mx = self._offset_x + (sx - self._scene_rect.x()) * self._scale
        my = self._offset_y + (sy - self._scene_rect.y()) * self._scale
        return mx, my

    def _minimap_to_scene(self, mx, my):
        """Convert minimap pixel coordinates back to scene coordinates."""
        sx = self._scene_rect.x() + (mx - self._offset_x) / self._scale
        sy = self._scene_rect.y() + (my - self._offset_y) / self._scale
        return sx, sy

    # ── Hit Testing ──

    def _node_at(self, pos) -> int:
        """Return index of node at minimap position, or -1."""
        for i, (x, y, w, h, color, title, nid) in enumerate(self._nodes_data):
            mx, my = self._scene_to_minimap(x, y)
            mw = max(w * self._scale, 3)
            mh = max(h * self._scale, 2)
            if mx <= pos.x() <= mx + mw and my <= pos.y() <= my + mh:
                return i
        return -1

    # ── Painting ──

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self._collapsed_height() if self._collapsed else self.height()

        # Background with rounded corners
        bg_rect = QRectF(0, 0, self.width(), h)
        painter.setPen(QPen(QColor(70, 70, 75, 160), 1))
        painter.setBrush(QColor(30, 30, 33, 210))
        painter.drawRoundedRect(bg_rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        if self._collapsed:
            # Draw a thin label
            painter.setPen(QColor(150, 150, 155))
            painter.setFont(QFont("", 8))
            painter.drawText(
                bg_rect.adjusted(24, 0, -4, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Minimap",
            )
            painter.end()
            return

        if not self._nodes_data:
            painter.setPen(QColor(100, 100, 100))
            painter.setFont(QFont("", 8))
            painter.drawText(
                bg_rect, Qt.AlignmentFlag.AlignCenter, "No nodes"
            )
            painter.end()
            return

        # Draw connections
        pen = QPen()
        pen.setWidthF(1.0)
        for x1, y1, x2, y2, color in self._connections:
            mx1, my1 = self._scene_to_minimap(x1, y1)
            mx2, my2 = self._scene_to_minimap(x2, y2)
            c = QColor(color)
            c.setAlpha(100)
            pen.setColor(c)
            painter.setPen(pen)
            painter.drawLine(int(mx1), int(my1), int(mx2), int(my2))

        # Draw nodes
        for i, (x, y, w, h, color, title, nid) in enumerate(self._nodes_data):
            mx, my = self._scene_to_minimap(x, y)
            mw = max(w * self._scale, 3)
            mh = max(h * self._scale, 2)

            node_color = QColor(color)

            # Selected node highlight
            is_selected = (nid == self._selected_node_id)
            is_hovered = (i == self._hovered_node_idx)

            if is_selected:
                # Bright outline for selected node
                painter.setPen(QPen(QColor(255, 180, 60, 220), 2))
                node_color.setAlpha(230)
            elif is_hovered:
                painter.setPen(QPen(QColor(200, 200, 210, 180), 1))
                node_color.setAlpha(220)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                node_color.setAlpha(170)

            painter.setBrush(QBrush(node_color))
            painter.drawRect(int(mx), int(my), int(mw), int(mh))

        # Draw viewport indicator
        if self._viewport_rect.isValid():
            vx, vy = self._scene_to_minimap(
                self._viewport_rect.x(), self._viewport_rect.y()
            )
            vw = self._viewport_rect.width() * self._scale
            vh = self._viewport_rect.height() * self._scale

            # Fill
            painter.setBrush(QColor(255, 180, 60, 20))
            painter.setPen(QPen(QColor(255, 180, 60, 140), 1.5))
            painter.drawRect(int(vx), int(vy), int(vw), int(vh))

        # Node count label
        painter.setPen(QColor(120, 120, 125, 180))
        painter.setFont(QFont("", 7))
        painter.drawText(
            QRectF(4, h - 14, self.width() - 8, 10),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{len(self._nodes_data)} nodes",
        )

        painter.end()

    # ── Mouse Events ──

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._collapsed:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._navigate_to(event.position())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._collapsed:
            return

        if self._dragging:
            self._navigate_to(event.position())
            return

        # Hover detection
        idx = self._node_at(event.position())
        if idx != self._hovered_node_idx:
            self._hovered_node_idx = idx
            if idx >= 0:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(self._nodes_data[idx][5])  # title
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
                self.setToolTip("")
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._dragging = False

    def leaveEvent(self, event) -> None:
        if self._hovered_node_idx >= 0:
            self._hovered_node_idx = -1
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setToolTip("")
            self.update()

    def _navigate_to(self, pos) -> None:
        """Convert minimap position to scene position and navigate."""
        if not self._nodes_data or not self._scene_rect.isValid():
            return
        sx, sy = self._minimap_to_scene(pos.x(), pos.y())
        self.navigate_requested.emit(QPointF(sx, sy))

    # ── Collapse ──

    def _collapsed_height(self) -> int:
        return self.COLLAPSED_HEIGHT

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.setFixedSize(self.MINIMAP_WIDTH, self.COLLAPSED_HEIGHT)
        else:
            self.setFixedSize(self.MINIMAP_WIDTH, self.MINIMAP_HEIGHT)
        self._position_toggle_btn()
        self._reposition_in_parent()
        self.update()

    def _reposition_in_parent(self) -> None:
        """Reposition self to bottom-right of parent widget."""
        parent = self.parentWidget()
        if parent:
            self.move(
                parent.width() - self.width() - 8,
                parent.height() - self.height() - 8,
            )

    def _position_toggle_btn(self) -> None:
        self._toggle_btn.move(3, 3)
