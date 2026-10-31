"""
Port Item - Visual representation of an input/output port on a node.

Small colored circles on the sides of nodes that can be connected.
"""

from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPen, QBrush, QColor, QPainter, QRadialGradient


class PortItem(QGraphicsEllipseItem):
    """
    A port (input or output) on a node item.

    Visual: Small circle (10px diameter) on the left (input) or right (output) of a node.
    Color-coded by data type with gradient fill.
    """

    PORT_RADIUS = 6
    DATA_TYPE_COLORS = {
        "image": QColor(200, 160, 60),     # Gold for image
        "float": QColor(100, 180, 100),     # Green for float
        "vec2": QColor(100, 140, 200),      # Blue for vec2
        "vec3": QColor(180, 100, 200),      # Purple for vec3
        "int": QColor(200, 100, 100),       # Red for int
        "bool": QColor(180, 180, 180),      # Gray for bool
        "scene": QColor(0, 180, 216),       # Cyan for scene (3D data flow)
    }

    def __init__(self, port_name: str, direction: str, data_type: str = "image",
                 max_connections: int = 1, parent=None) -> None:
        r = self.PORT_RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r, parent)

        self.port_name = port_name
        self.direction = direction  # "input" or "output"
        self.data_type = data_type
        # 输入端口容量：1=单连接（默认），>1=多输入（如 Scene3D 的 scene 端口）。
        # 输出端口理论上无限连接（不强制容量）。
        self.max_connections = max_connections

        # Visual style
        self._base_color = self.DATA_TYPE_COLORS.get(data_type, QColor(150, 150, 150))
        self._update_brush()
        self.setPen(QPen(QColor(30, 30, 30), 1.5))

        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges)

        # Connection state: 用计数器支持多输入（旧版是布尔单标志）。
        # connection_count = 当前连接数；_connected 作为兼容 property 暴露。
        self.connection_count = 0

        # Snap highlight state (used during connection dragging)
        self._snap_highlighted = False
        self._snap_valid = True

        # Compact mode for dot nodes — smaller, no visible fill
        self._compact = False

        self._update_tooltip()

    def _update_brush(self) -> None:
        """Create a radial gradient brush for a 3D look."""
        gradient = QRadialGradient(-1, -1, self.PORT_RADIUS * 2)
        gradient.setColorAt(0, self._base_color.lighter(140))
        gradient.setColorAt(0.7, self._base_color)
        gradient.setColorAt(1, self._base_color.darker(130))
        self.setBrush(QBrush(gradient))

    def _update_tooltip(self) -> None:
        dir_text = "Input" if self.direction == "input" else "Output"
        self.setToolTip(f"{dir_text}: {self.port_name} ({self.data_type})")

    @property
    def _connected(self) -> bool:
        """兼容旧读取点：是否至少有一条连接（计数 > 0）。"""
        return self.connection_count > 0

    def increment_connection(self) -> None:
        """增加一条连接（多输入支持）。首次连接时更新视觉到「已连接」态。"""
        was_empty = self.connection_count == 0
        self.connection_count += 1
        if was_empty:
            self.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            self.update()

    def decrement_connection(self) -> None:
        """减少一条连接。归零时更新视觉到「未连接」态；不低于 0。"""
        self.connection_count = max(0, self.connection_count - 1)
        if self.connection_count == 0:
            self.setPen(QPen(QColor(30, 30, 30), 1.5))
            self.update()

    def is_at_capacity(self) -> bool:
        """输入端口是否已满载（多输入 max>1 时达上限；输出端口永不满载）。"""
        if self.direction != "input":
            return False
        return self.max_connections > 0 and self.connection_count >= self.max_connections

    def set_connected(self, connected: bool) -> None:
        """向后兼容入口：True→计数置 1（首次），False→计数归零。

        多输入场景应优先用 increment/decrement_connection。
        """
        if connected:
            if self.connection_count == 0:
                self.connection_count = 1
                self.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
        else:
            self.connection_count = 0
            self.setPen(QPen(QColor(30, 30, 30), 1.5))
        self.update()

    def hoverEnterEvent(self, event) -> None:
        # Enlarge slightly on hover via pen
        self.setPen(QPen(QColor(255, 200, 100), 2.5))
        # Brighten the brush
        hover_color = self._base_color.lighter(150)
        gradient = QRadialGradient(-1, -1, self.PORT_RADIUS * 2)
        gradient.setColorAt(0, hover_color)
        gradient.setColorAt(0.5, self._base_color.lighter(120))
        gradient.setColorAt(1, self._base_color)
        self.setBrush(QBrush(gradient))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        if self._connected:
            self.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
        else:
            self.setPen(QPen(QColor(30, 30, 30), 1.5))
        self._update_brush()
        super().hoverLeaveEvent(event)

    def get_center_scene_pos(self) -> QPointF:
        """Get the center position of this port in scene coordinates."""
        return self.scenePos()

    def set_snap_highlight(self, active: bool, valid: bool = True) -> None:
        """Set snap highlight state during connection dragging.

        Args:
            active: Whether to show the highlight
            valid: True = green (valid target), False = red (invalid target)
        """
        self._snap_highlighted = active
        self._snap_valid = valid
        self.update()

    def set_compact(self, compact: bool) -> None:
        """Set compact mode — smaller invisible ports for dot nodes."""
        self._compact = compact
        if compact:
            r = 3  # Smaller hit area
            self.setRect(-r, -r, 2 * r, 2 * r)
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.update()

    def paint(self, painter, option, widget=None) -> None:
        """Custom paint with anti-aliasing and snap highlight."""
        # Compact mode ports are invisible (hit-test only)
        if self._compact:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw snap highlight ring first (behind the port)
        if self._snap_highlighted:
            r = self.PORT_RADIUS
            highlight_r = r + 5
            if self._snap_valid:
                highlight_color = QColor(100, 255, 100, 80)
                ring_color = QColor(100, 255, 100, 180)
            else:
                highlight_color = QColor(255, 80, 80, 60)
                ring_color = QColor(255, 80, 80, 160)

            # Glow
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(highlight_color)
            painter.drawEllipse(int(-highlight_r), int(-highlight_r),
                                int(highlight_r * 2), int(highlight_r * 2))

            # Ring
            painter.setPen(QPen(ring_color, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(int(-highlight_r), int(-highlight_r),
                                int(highlight_r * 2), int(highlight_r * 2))

        super().paint(painter, option, widget)

    def itemChange(self, change, value):
        # Notify parent node item when position changes (for connection updates)
        if change == QGraphicsItem.GraphicsItemChange.ItemScenePositionHasChanged:
            parent = self.parentItem()
            if parent and hasattr(parent, 'update_connections'):
                parent.update_connections()
        return super().itemChange(change, value)
