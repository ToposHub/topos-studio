"""
Connection geometry, flow animation and hit testing for node graph cables.
"""

from math import hypot

from PyQt6.QtWidgets import QGraphicsPathItem, QGraphicsItem
from PyQt6.QtCore import QPointF, Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import (
    QPainterPath, QPen, QColor, QPainter, QBrush,
    QPainterPathStroker, QKeyEvent, QPolygonF
)


class _FlowDriver(QObject):
    """Singleton driver for flow animation — owns a persistent QTimer.

    Using a QObject-owned timer avoids PyQt6 6.10+ issues where
    class-level parentless QTimers never fire their timeout signal.

    The timer is automatically stopped when no connections are registered,
    and restarted when a new connection is added. This avoids unnecessary
    CPU wakeups when the node graph is empty.
    """

    # Timer intervals in milliseconds
    NORMAL_INTERVAL: int = 50   # ~20fps (normal speed)
    SLOW_INTERVAL: int = 100    # ~10fps (slow mode for performance)

    def __init__(self) -> None:
        super().__init__()
        self._connections: list = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._timer.setInterval(self.NORMAL_INTERVAL)
        self._timer_active: bool = False  # Track timer state
        self._slow_mode: bool = False      # Track slow mode state

    def add(self, conn) -> None:
        """Add a connection to the animation driver.

        Automatically starts the timer if it was stopped.
        """
        if conn not in self._connections:
            self._connections.append(conn)
            # Start timer if it was stopped and we now have connections
            if not self._timer_active and self._connections:
                self._timer.start(self._timer.interval())
                self._timer_active = True

    def remove(self, conn) -> None:
        """Remove a connection from the animation driver.

        Automatically stops the timer when no connections remain.
        """
        if conn in self._connections:
            self._connections.remove(conn)
            # Stop timer when no connections remain (CPU optimization)
            if self._timer_active and not self._connections:
                self._timer.stop()
                self._timer_active = False

    def set_slow_mode(self, slow: bool) -> None:
        """Switch between normal and slow animation mode.

        Slow mode uses a longer timer interval for better performance
        during intensive operations (e.g., real-time preview updates).

        Args:
            slow: True for slow mode (100ms interval), False for normal (50ms)
        """
        if self._slow_mode == slow:
            return  # No change needed

        self._slow_mode = slow
        new_interval = self.SLOW_INTERVAL if slow else self.NORMAL_INTERVAL
        self._timer.setInterval(new_interval)

        # If timer is running, apply the new interval immediately
        if self._timer_active:
            self._timer.start(new_interval)

    def is_slow_mode(self) -> bool:
        """Return current slow mode state."""
        return self._slow_mode

    def _advance(self) -> None:
        """Advance animation for all registered connections."""
        if not self._connections:
            return
        stale = []
        for conn in self._connections:
            try:
                conn._dash_offset -= 1.5
                conn.update()
            except RuntimeError:
                stale.append(conn)
        for conn in stale:
            self._connections.remove(conn)
        # Stop timer if all connections were removed
        if not self._connections and self._timer_active:
            self._timer.stop()
            self._timer_active = False


# Module-level singleton, lazy-initialized on first use
_flow_driver: _FlowDriver | None = None


def _get_flow_driver() -> _FlowDriver:
    global _flow_driver
    if _flow_driver is None:
        _flow_driver = _FlowDriver()
    return _flow_driver


class _ConnectionSignals(QObject):
    """Signal emitter for ConnectionItem operations.

    Since QGraphicsItem cannot directly define PyQt signals, this helper
    QObject class provides signal emission for connection operations that
    need to notify the NodeEditorWidget/CompositePage for proper data model
    updates and undo/redo tracking.
    """

    # Signal for connection deletion request (needs undo support)
    delete_requested = pyqtSignal(object)  # ConnectionItem


class ConnectionItem(QGraphicsPathItem):
    """
    Visual connection between two port items.

    Draws a cable from source port to destination port.
    Features:
    - Flow animation (dashed line moving along the curve)
    - Hover glow effect
    - Selection highlight
    """

    # Connection colors based on data type
    DATA_TYPE_COLORS = {
        "image": QColor(200, 160, 60, 200),     # Gold
        "float": QColor(100, 180, 100, 200),     # Green
        "vec2": QColor(100, 140, 200, 200),      # Blue
        "vec3": QColor(180, 100, 200, 200),      # Purple
        "scene": QColor(0, 180, 216, 200),       # Cyan (3D scene data flow)
    }

    ROUTES = ("curved", "straight", "orthogonal")

    def __init__(self, src_port=None, dst_port=None, data_type="image", *,
                 routing: str = "curved") -> None:
        super().__init__()
        self.src_port = src_port  # PortItem
        self.dst_port = dst_port  # PortItem
        self.data_type = data_type
        self._routing = routing
        self._segments: list[QPointF] | None = None

        # Signal emitter for notifying NodeEditorWidget of operations
        # (QGraphicsItem cannot directly define PyQt signals)
        self._signals = _ConnectionSignals()

        # Visual style
        self._base_color = self.DATA_TYPE_COLORS.get(data_type, QColor(150, 150, 150, 200))
        self._hovered = False

        # Main pen
        pen = QPen(self._base_color, 3.0)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QColor(0, 0, 0, 0))

        # Flow animation state
        self._dash_offset = 0.0
        self._flow_enabled = False

        # Drag validity state (used during connection dragging)
        self._drag_valid = True

        # Snap highlight state (for connection insertion detection)
        self._snap_highlighted: bool = False
        self._snap_valid: bool = True

        # Performance: LOD (Level of Detail)
        self._simplified_draw = False

        # Insertion dot state (shown when Cmd/Ctrl is held)
        self._show_insertion_dot = False
        self._insertion_dot_hovered = False
        self._insertion_dot_radius = 6.0

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)
        self.setAcceptHoverEvents(True)
        self.setZValue(-1)

        # Initial path update
        self.update_path()

        # Initial tooltip (will be updated on hover with current status)
        self._update_tooltip()

    def signals(self) -> "_ConnectionSignals":
        """Return the signal emitter for this connection.

        Use this to connect to signals for operation notifications:
            conn.signals().delete_requested.connect(handler)
        """
        return self._signals

    def start_flow_animation(self, slow_mode=False) -> None:
        """Start the flow animation.
        
        Args:
            slow_mode: If True, use reduced frame rate for better performance.
        """
        if not self._flow_enabled:
            self._flow_enabled = True
            _get_flow_driver().add(self)
            if slow_mode:
                self.slowdown_animation()

    def stop_flow_animation(self) -> None:
        """Stop the flow animation."""
        if self._flow_enabled:
            self._flow_enabled = False
            _get_flow_driver().remove(self)

    def slowdown_animation(self) -> None:
        """Switch to slow animation mode for better performance."""
        _get_flow_driver().set_slow_mode(True)

    def resume_animation(self) -> None:
        """Resume normal animation speed."""
        _get_flow_driver().set_slow_mode(False)

    def set_simplified_draw(self, enabled: bool) -> None:
        """Enable/disable simplified drawing for LOD."""
        if self._simplified_draw == enabled:
            return
        self._simplified_draw = enabled
        self.update()

    def set_routing(self, routing: str) -> None:
        if routing in self.ROUTES and routing != self._routing:
            self._routing = routing
            self.update_path()

    def update_path(self) -> None:
        """Recalculate the path from the current port positions."""
        if self.src_port is not None and self.dst_port is not None:
            self.set_temp_endpoints(self.src_port.get_center_scene_pos(),
                                    self.dst_port.get_center_scene_pos())

    def _create_path(self, src: QPointF, dst: QPointF) -> QPainterPath:
        path = QPainterPath(src)
        if self._routing == "straight":
            path.lineTo(dst)
        elif self._routing == "orthogonal":
            if src.x() <= dst.x():
                middle_x = (src.x() + dst.x()) / 2
                path.lineTo(middle_x, src.y())
                path.lineTo(middle_x, dst.y())
            else:
                # Keep both ends facing their ports, even on a leftward link.
                middle_y = (src.y() + dst.y()) / 2
                if abs(src.y() - dst.y()) < 24:
                    middle_y = min(src.y(), dst.y()) - 24
                source_x, target_x = src.x() + 24, dst.x() - 24
                path.lineTo(source_x, src.y())
                path.lineTo(source_x, middle_y)
                path.lineTo(target_x, middle_y)
                path.lineTo(target_x, dst.y())
            path.lineTo(dst)
        else:
            offset = max(abs(dst.x() - src.x()) * 0.5, 60)
            path.cubicTo(QPointF(src.x() + offset, src.y()),
                         QPointF(dst.x() - offset, dst.y()), dst)
        return path

    def paint(self, painter, option, widget=None) -> None:
        """Custom paint with anti-aliasing, glow, and flow animation."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Performance: Simplified drawing at low zoom
        if self._simplified_draw:
            # Just draw a simple line without glow or animation
            main_color = QColor(self._base_color)
            if not self._drag_valid:
                main_color = QColor(255, 60, 60, 180)
            main_pen = QPen(main_color, 2.0)
            main_pen.setCosmetic(True)
            painter.setPen(main_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.path())
            return

        # Draw glow if hovered or selected
        if self._hovered or self.isSelected():
            glow_color = QColor(self._base_color)
            glow_color.setAlpha(80)
            glow_pen = QPen(glow_color, 10)
            glow_pen.setCosmetic(True)
            painter.setPen(glow_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.path())

        # Draw snap highlight glow (for connection insertion)
        if self._snap_highlighted:
            if self._snap_valid:
                # Valid insert target - bright cyan glow
                snap_glow_color = QColor(100, 220, 255, 60)
                snap_ring_color = QColor(100, 220, 255, 160)
            else:
                # Invalid insert target - red glow
                snap_glow_color = QColor(255, 80, 80, 80)
                snap_ring_color = QColor(255, 80, 80, 200)

            # Wide glow
            snap_glow_pen = QPen(snap_glow_color, 8)
            snap_glow_pen.setCosmetic(True)
            painter.setPen(snap_glow_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.path())

            # Bright ring
            snap_ring_pen = QPen(snap_ring_color, 3)
            snap_ring_pen.setCosmetic(True)
            painter.setPen(snap_ring_pen)
            painter.drawPath(self.path())

        # Draw main line
        main_color = QColor(self._base_color)
        if self.isSelected():
            main_color = main_color.lighter(140)
        if not self._drag_valid:
            main_color = QColor(255, 60, 60, 180)

        main_pen = QPen(main_color, 3.0)
        main_pen.setCosmetic(True)
        painter.setPen(main_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())

        # Draw flow animation overlay (dashed bright line)
        if self._flow_enabled:
            flow_pen = QPen(QColor(255, 255, 255, 100), 1.5)
            flow_pen.setCosmetic(True)
            flow_pen.setStyle(Qt.PenStyle.DashLine)
            flow_pen.setDashOffset(self._dash_offset)
            flow_pen.setDashPattern([4, 8])
            painter.setPen(flow_pen)
            painter.drawPath(self.path())

        # The arrow shows the data direction on bends and loop-back layouts.
        if not self._show_insertion_dot:
            self._draw_direction_arrow(painter, main_color)

        # Draw insertion dot at midpoint (shown when Cmd/Ctrl is held)
        if self._show_insertion_dot:
            mid = self.get_midpoint_scene()
            # Map to local coordinates for drawing
            local_mid = self.mapFromScene(mid)
            r = self._insertion_dot_radius

            # Hovered state: larger, brighter
            if self._insertion_dot_hovered:
                # Outer glow
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(255, 255, 255, 50)))
                painter.drawEllipse(local_mid, r + 4, r + 4)
                # Main dot
                painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
                painter.setPen(QPen(QColor(255, 255, 255), 1.5))
                painter.drawEllipse(local_mid, r, r)
            else:
                # Normal dot
                painter.setPen(QPen(QColor(200, 200, 200, 180), 1.5))
                painter.setBrush(QBrush(QColor(80, 80, 85, 200)))
                painter.drawEllipse(local_mid, r, r)
                # Plus icon hint
                painter.setPen(QPen(QColor(200, 200, 200, 160), 1.5))
                painter.drawLine(
                    QPointF(local_mid.x() - 3, local_mid.y()),
                    QPointF(local_mid.x() + 3, local_mid.y())
                )
                painter.drawLine(
                    QPointF(local_mid.x(), local_mid.y() - 3),
                    QPointF(local_mid.x(), local_mid.y() + 3)
                )

    def _draw_direction_arrow(self, painter: QPainter, color: QColor) -> None:
        path = self.path()
        tip = path.pointAtPercent(0.72)
        tangent = tip - path.pointAtPercent(0.69)
        length = hypot(tangent.x(), tangent.y())
        if length < 1.0:
            return
        direction = tangent / length
        base = tip - direction * 8
        cross = QPointF(-direction.y(), direction.x()) * 3.5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([tip, base + cross, base - cross]))

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self.setZValue(1)  # Bring above other connections and nodes for clicking
        self._update_tooltip()
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self.setZValue(-1)
        self.update()
        super().hoverLeaveEvent(event)

    def _update_tooltip(self) -> None:
        """Update the tooltip with connection information.

        Format: "{src_node}:{src_port} → {dst_node}:{dst_port}"
        Optional: Show status if nodes have state information.
        """
        tooltip_parts = []

        # Get source info
        if self.src_port:
            src_node = self.src_port.parentItem()
            if src_node and hasattr(src_node, 'node_title'):
                src_title = src_node.node_title
            else:
                src_title = "Unknown"
            src_port_name = self.src_port.port_name
            tooltip_parts.append(f"{src_title}:{src_port_name}")
        else:
            tooltip_parts.append("Unknown source")

        tooltip_parts.append("→")

        # Get destination info
        if self.dst_port:
            dst_node = self.dst_port.parentItem()
            if dst_node and hasattr(dst_node, 'node_title'):
                dst_title = dst_node.node_title
            else:
                dst_title = "Unknown"
            dst_port_name = self.dst_port.port_name
            tooltip_parts.append(f"{dst_title}:{dst_port_name}")
        else:
            tooltip_parts.append("Unknown destination")

        # Build base tooltip
        tooltip = " ".join(tooltip_parts)

        # Add status information if available
        status_parts = []
        if self.src_port:
            src_node = self.src_port.parentItem()
            if src_node and hasattr(src_node, '_state'):
                status_parts.append(f"Source: {src_node._state}")
        if self.dst_port:
            dst_node = self.dst_port.parentItem()
            if dst_node and hasattr(dst_node, '_state'):
                status_parts.append(f"Target: {dst_node._state}")

        if status_parts:
            tooltip += "\n" + " | ".join(status_parts)

        # Add data type
        tooltip += f"\nType: {self.data_type}"

        self.setToolTip(tooltip)

    def shape(self):
        """Return a wider stroke path for easier clicking/selecting."""
        stroker = QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(self.path())

    def set_drag_validity(self, is_valid: bool) -> None:
        """Set visual validity state during connection dragging."""
        self._drag_valid = is_valid
        self.update()

    def set_temp_endpoints(self, src: QPointF, dst: QPointF) -> None:
        """Temporarily draw from src to dst (used during connection dragging)."""
        self._segments = None
        self.setPath(self._create_path(src, dst))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle key press for deletion."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.isSelected():
                self._delete_connection()
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def _delete_connection(self) -> None:
        """Request deletion of this connection.

        Emits delete_requested signal to notify NodeEditorWidget,
        which handles the operation through the data model with undo support.

        This method only signals the request; actual deletion is handled
        by NodeEditorWidget in response to the signal, ensuring proper
        undo/redo tracking and data model synchronization.
        """
        self._signals.delete_requested.emit(self)

    # ── Connection Snap Detection ──

    SNAP_DISTANCE = 15.0  # Distance threshold for connection snap detection

    def set_snap_highlight(self, active: bool, valid: bool = True) -> None:
        """Set snap highlight state during connection dragging.

        When a new connection is dragged near an existing connection,
        this highlight provides visual feedback that inserting a Dot node
        is possible.

        Args:
            active: Whether to show the highlight
            valid: True = green/white glow (valid insert target),
                   False = red glow (invalid - e.g., type mismatch)
        """
        self._snap_highlighted = active
        self._snap_valid = valid
        if active:
            self.setZValue(2)  # Bring above all other items
        else:
            self.setZValue(-1 if not self._hovered else 1)
        self.update()

    def _nearest_on_path(self, scene_pos: QPointF) -> tuple[QPointF, float, float]:
        """Project onto the painted path and return point, distance and progress."""
        if self._segments is None:
            polylines = self.path().toSubpathPolygons()
            self._segments = list(polylines[0]) if polylines else []
        target = self.mapFromScene(scene_pos)
        best, distance, progress, travelled = target, float("inf"), 0.0, 0.0
        for start, end in zip(self._segments, self._segments[1:]):
            segment = end - start
            length_squared = QPointF.dotProduct(segment, segment)
            if length_squared == 0:
                continue
            fraction = QPointF.dotProduct(target - start, segment) / length_squared
            fraction = max(0.0, min(1.0, fraction))
            point = start + segment * fraction
            gap = target - point
            length = length_squared ** 0.5
            candidate_distance = hypot(gap.x(), gap.y())
            if candidate_distance < distance:
                best, distance = point, candidate_distance
                progress = travelled + length * fraction
            travelled += length
        return self.mapToScene(best), distance, progress / travelled if travelled else 0.5

    def get_nearest_point_on_path(self, scene_pos: QPointF) -> tuple[QPointF, float]:
        point, distance, _ = self._nearest_on_path(scene_pos)
        return point, distance

    def get_midpoint_scene(self) -> QPointF:
        path = self.path()
        return self.mapToScene(path.pointAtPercent(path.percentAtLength(path.length() / 2)))

    def set_show_insertion_dot(self, show: bool) -> None:
        """Show or hide the insertion dot at the midpoint."""
        if self._show_insertion_dot != show:
            self._show_insertion_dot = show
            self.update()

    def hit_test_insertion_dot(self, scene_pos: QPointF) -> bool:
        """Check if a scene position hits the insertion dot."""
        if not self._show_insertion_dot:
            return False
        mid = self.get_midpoint_scene()
        dx = scene_pos.x() - mid.x()
        dy = scene_pos.y() - mid.y()
        r = self._insertion_dot_radius + 4  # Slightly generous hit area
        return dx * dx + dy * dy < r * r

    def get_insert_t_value(self, scene_pos: QPointF) -> float:
        """Return the nearest point's fractional distance along the cable."""
        return self._nearest_on_path(scene_pos)[2]
