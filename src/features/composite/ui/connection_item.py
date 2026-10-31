"""
Connection Item - Bezier curve connection between two node ports.

NUKE-style curved connection lines with flow animation and hover effects.
"""

from PyQt6.QtWidgets import QGraphicsPathItem, QGraphicsItem, QGraphicsSceneMouseEvent
from PyQt6.QtCore import QPointF, Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import (
    QPainterPath, QPen, QColor, QPainter, QBrush,
    QPainterPathStroker, QKeyEvent
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

    # Signal for connection reconnection request
    reconnect_requested = pyqtSignal(object, object, bool)  # ConnectionItem, new_port, dragging_src

    # Signal for connection disconnect request (Alt+drag without reconnection)
    disconnect_requested = pyqtSignal(object)  # ConnectionItem


class ConnectionItem(QGraphicsPathItem):
    """
    Visual connection between two port items.

    Draws a smooth cubic Bezier curve from source port to destination port.
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

    def __init__(self, src_port=None, dst_port=None, data_type="image") -> None:
        super().__init__()
        self.src_port = src_port  # PortItem
        self.dst_port = dst_port  # PortItem
        self.data_type = data_type

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

        # Reconnection state
        self._is_reconnecting = False
        self._reconnect_port: QGraphicsItem | None = None  # Port being dragged to
        self._reconnect_src: bool = True  # True if dragging source end

        # Disconnect state (Alt+drag to disconnect)
        self._is_disconnecting = False
        self._disconnect_src: bool = True  # True if disconnecting source end
        self._disconnect_temp_pos: QPointF | None = None  # Temp position during disconnect drag
        
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
            conn.signals().reconnect_requested.connect(handler)
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

    def update_path(self) -> None:
        """Recalculate the Bezier curve path."""
        if self.src_port is None or self.dst_port is None:
            return

        src_pos = self.src_port.get_center_scene_pos()
        dst_pos = self.dst_port.get_center_scene_pos()

        path = self._create_bezier_path(src_pos, dst_pos)
        self.setPath(path)

    def _create_bezier_path(self, src: QPointF, dst: QPointF) -> QPainterPath:
        """Create a smooth Bezier curve between two points."""
        path = QPainterPath()
        path.moveTo(src)

        # Calculate control points for a smooth horizontal S-curve
        dx = abs(dst.x() - src.x())
        offset = max(dx * 0.5, 60)

        ctrl1 = QPointF(src.x() + offset, src.y())
        ctrl2 = QPointF(dst.x() - offset, dst.y())

        path.cubicTo(ctrl1, ctrl2, dst)
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

        # Visual feedback for disconnect mode
        if self._is_disconnecting:
            # Draw dashed red line during disconnect drag
            main_color = QColor(255, 80, 80, 220)
            main_pen = QPen(main_color, 3.0)
            main_pen.setCosmetic(True)
            main_pen.setStyle(Qt.PenStyle.DashLine)
            main_pen.setDashPattern([6, 4])
        else:
            main_pen = QPen(main_color, 3.0)
            main_pen.setCosmetic(True)
        painter.setPen(main_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())

        # Draw reconnecting endpoint highlight
        if self._is_reconnecting and (self.src_port or self.dst_port):
            endpoint = self.dst_port.get_center_scene_pos() if self._reconnect_src else self.src_port.get_center_scene_pos()
            highlight_color = QColor(255, 255, 255, 180)
            highlight_pen = QPen(highlight_color, 8)
            highlight_pen.setCosmetic(True)
            painter.setPen(highlight_pen)
            painter.setBrush(QBrush(highlight_color))
            painter.drawEllipse(endpoint, 8, 8)

        # Draw flow animation overlay (dashed bright line)
        if self._flow_enabled:
            flow_pen = QPen(QColor(255, 255, 255, 100), 1.5)
            flow_pen.setCosmetic(True)
            flow_pen.setStyle(Qt.PenStyle.DashLine)
            flow_pen.setDashOffset(self._dash_offset)
            flow_pen.setDashPattern([4, 8])
            painter.setPen(flow_pen)
            painter.drawPath(self.path())

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
        path = self._create_bezier_path(src, dst)
        self.setPath(path)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Handle mouse press to start reconnection or disconnect drag.

        Normal drag: Reconnect to another port.
        Alt+drag: Disconnect (cancel connection, optionally reconnect to another port).
        """
        if event.button() == Qt.MouseButton.LeftButton:
            # Check if clicking near an endpoint
            click_pos = event.scenePos()
            src_pos = self.src_port.get_center_scene_pos() if self.src_port else QPointF()
            dst_pos = self.dst_port.get_center_scene_pos() if self.dst_port else QPointF()

            threshold = 15.0
            dist_src = (click_pos - src_pos).manhattanLength()
            dist_dst = (click_pos - dst_pos).manhattanLength()

            # Determine which end is being dragged
            dragging_src = dist_src < threshold and dist_src <= dist_dst
            dragging_dst = dist_dst < threshold

            if dragging_src or dragging_dst:
                # Check for Alt/Option key (disconnect mode)
                modifiers = event.modifiers()
                if modifiers & Qt.KeyboardModifier.AltModifier:
                    # Alt+drag: Disconnect mode
                    self._is_disconnecting = True
                    self._disconnect_src = dragging_src
                    self._disconnect_temp_pos = click_pos
                    self._is_reconnecting = False
                else:
                    # Normal drag: Reconnect mode
                    self._is_reconnecting = True
                    self._reconnect_src = dragging_src
                    self._reconnect_port = None
                    self._is_disconnecting = False

                # Store the fixed endpoint position for visual update
                self.update()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Handle mouse move during reconnection or disconnect drag."""
        if self._is_disconnecting:
            # Update path to show disconnect drag
            mouse_pos = event.scenePos()
            self._disconnect_temp_pos = mouse_pos

            # Draw from fixed endpoint to mouse position
            if self._disconnect_src:
                # Dragging source end, keep destination fixed
                dst_pos = self.dst_port.get_center_scene_pos() if self.dst_port else QPointF()
                path = self._create_bezier_path(mouse_pos, dst_pos)
            else:
                # Dragging destination end, keep source fixed
                src_pos = self.src_port.get_center_scene_pos() if self.src_port else QPointF()
                path = self._create_bezier_path(src_pos, mouse_pos)
            self.setPath(path)
            self.update()
            event.accept()
        elif self._is_reconnecting:
            # Update path to follow mouse
            mouse_pos = event.scenePos()
            if self._reconnect_src:
                dst_pos = self.dst_port.get_center_scene_pos() if self.dst_port else QPointF()
                path = self._create_bezier_path(mouse_pos, dst_pos)
            else:
                src_pos = self.src_port.get_center_scene_pos() if self.src_port else QPointF()
                path = self._create_bezier_path(src_pos, mouse_pos)
            self.setPath(path)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Handle mouse release to complete reconnection or disconnect."""
        if self._is_disconnecting:
            # Alt+drag disconnect mode
            release_pos = event.scenePos()

            # Find port under mouse
            scene = self.scene()
            target_port = None
            if scene:
                items = scene.items(release_pos)
                for item in items:
                    if hasattr(item, 'port_name') and hasattr(item, 'direction'):
                        target_port = item
                        break

            if target_port and self._can_reconnect(target_port):
                # Valid reconnection target - reconnect to new port
                self._perform_reconnection(target_port)
            else:
                # No valid target - disconnect completely
                self._disconnect_and_remove()

            # Reset disconnect state
            self._is_disconnecting = False
            self._disconnect_temp_pos = None
            event.accept()
        elif self._is_reconnecting:
            release_pos = event.scenePos()

            # Find port under mouse
            scene = self.scene()
            if scene:
                items = scene.items(release_pos)
                target_port = None
                for item in items:
                    # Check if it's a PortItem (need to import to check type)
                    if hasattr(item, 'port_name') and hasattr(item, 'direction'):
                        target_port = item
                        break

                if target_port and self._can_reconnect(target_port):
                    # Perform reconnection
                    self._perform_reconnection(target_port)

            # Reset reconnect state
            self._is_reconnecting = False
            self._reconnect_port = None
            self.update_path()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

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

    def _can_reconnect(self, target_port) -> bool:
        """Check if reconnection is valid (no self-loops, type match, no cycle)."""
        from .port_item import PortItem
        
        if not isinstance(target_port, PortItem):
            return False

        # Get the node that owns this connection
        if not self.src_port or not self.dst_port:
            return False
        
        src_node = self.src_port.parentItem()
        dst_node = self.dst_port.parentItem()
        target_node = target_port.parentItem()

        # No self-loops (connecting port to same node)
        # Determine which end is being dragged (support both reconnect and disconnect mode)
        dragging_src = self._reconnect_src if self._is_reconnecting else self._disconnect_src

        if dragging_src:
            # Dragging source end - check target is not on same node as destination
            if target_node == dst_node:
                return False
        else:
            # Dragging destination end - check target is not on same node as source
            if target_node == src_node:
                return False

        # Type check
        if target_port.data_type != self.data_type:
            return False

        # Direction check (must connect output to input)
        if dragging_src:
            # New source must be output
            if target_port.direction != "output":
                return False
        else:
            # New destination must be input
            if target_port.direction != "input":
                return False

        # Cycle detection
        if self._would_create_cycle(target_port):
            return False

        return True

    def _would_create_cycle(self, new_port) -> bool:
        """Check if connecting to new_port would create a cycle in the graph."""
        from .port_item import PortItem

        if not isinstance(new_port, PortItem):
            return False

        # Determine which node would be the new source/destination.
        # AUD-9801: derive the dragged end exactly like _can_reconnect /
        # _perform_reconnection — Alt+disconnect drags only update
        # _disconnect_src, so reading _reconnect_src alone would reverse
        # the cycle direction for that mode.
        dragging_src = self._reconnect_src if self._is_reconnecting else self._disconnect_src
        if dragging_src:
            # new_port becomes the new source
            potential_src_port = new_port
            potential_dst_port = self.dst_port
        else:
            # new_port becomes the new destination
            potential_src_port = self.src_port
            potential_dst_port = new_port

        if not potential_src_port or not potential_dst_port:
            return False

        src_node = potential_src_port.parentItem()
        dst_node = potential_dst_port.parentItem()

        # AUD-9800: PortItem no longer keeps per-port ``_connections``
        # lists (only a ``connection_count`` int), so the old DFS never
        # expanded a single neighbour and always returned False — the UI
        # cycle guard was dead. Build the node adjacency from the scene's
        # live ConnectionItems instead (one pass; the DFS below then stays
        # linear in the number of connections).
        outgoing: dict = {}
        scene = self.scene()
        if scene is not None:
            for item in scene.items():
                if not isinstance(item, ConnectionItem):
                    continue
                conn_src = getattr(item, "src_port", None)
                conn_dst = getattr(item, "dst_port", None)
                if conn_src is None or conn_dst is None:
                    continue
                conn_src_node = conn_src.parentItem()
                conn_dst_node = conn_dst.parentItem()
                if conn_src_node is None or conn_dst_node is None:
                    continue
                outgoing.setdefault(conn_src_node, []).append(conn_dst_node)

        # DFS from dst_node to see if we can reach src_node
        # (which would mean adding this connection creates a cycle)
        visited = set()
        stack = [dst_node]

        while stack:
            current_node = stack.pop()
            if current_node in visited:
                continue
            visited.add(current_node)

            if current_node is src_node:
                return True  # Found path back to source - cycle!

            # Explore all outgoing connections from this node
            stack.extend(outgoing.get(current_node, ()))

        return False

    def _perform_reconnection(self, target_port) -> None:
        """Request reconnection to a new port.

        Emits reconnect_requested signal to notify NodeEditorWidget,
        which handles the operation through the data model with undo support.

        This method only updates visual state; the actual connection changes
        are handled by NodeEditorWidget in response to the signal.

        Args:
            target_port: The new port to reconnect to (PortItem)
        """
        from .port_item import PortItem

        if not isinstance(target_port, PortItem):
            return

        # Determine which end is being dragged (support both reconnect and disconnect mode)
        dragging_src = self._reconnect_src if self._is_reconnecting else self._disconnect_src

        # Emit signal for NodeEditorWidget to handle proper reconnection
        # with data model updates and undo/redo support
        self._signals.reconnect_requested.emit(self, target_port, dragging_src)


    def _delete_connection(self) -> None:
        """Request deletion of this connection.

        Emits delete_requested signal to notify NodeEditorWidget,
        which handles the operation through the data model with undo support.

        This method only signals the request; actual deletion is handled
        by NodeEditorWidget in response to the signal, ensuring proper
        undo/redo tracking and data model synchronization.
        """
        self._signals.delete_requested.emit(self)

    def _disconnect_and_remove(self) -> None:
        """Request disconnect without reconnection (Alt+drag disconnect).

        Emits disconnect_requested signal to notify NodeEditorWidget,
        which handles the operation through the data model with undo support.

        This is triggered when Alt+dragging a connection endpoint to an
        invalid location, meaning the connection should be completely removed.
        """
        self._signals.disconnect_requested.emit(self)

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

    def get_nearest_point_on_path(self, scene_pos: QPointF) -> tuple[QPointF, float]:
        """Find the nearest point on this connection's Bezier curve.

        Uses recursive subdivision to approximate the nearest point.
        Returns both the point and the distance to it.

        Args:
            scene_pos: The point to find nearest point on curve

        Returns:
            (nearest_point, distance) tuple
        """
        if self.src_port is None or self.dst_port is None:
            return (scene_pos, float('inf'))

        src_pos = self.src_port.get_center_scene_pos()
        dst_pos = self.dst_port.get_center_scene_pos()

        # Calculate control points for the Bezier curve
        dx = abs(dst_pos.x() - src_pos.x())
        offset = max(dx * 0.5, 60)

        ctrl1 = QPointF(src_pos.x() + offset, src_pos.y())
        ctrl2 = QPointF(dst_pos.x() - offset, dst_pos.y())

        # Recursive subdivision to find nearest point
        # Depth 6 (64 samples) is sufficient for proximity detection;
        # depth 10 (1024 samples) is overkill and causes lag during fast drag.
        nearest_point, nearest_dist = self._find_nearest_on_cubic_bezier(
            src_pos, ctrl1, ctrl2, dst_pos, scene_pos, 6
        )

        return (nearest_point, nearest_dist)

    def _find_nearest_on_cubic_bezier(
        self,
        p0: QPointF, p1: QPointF, p2: QPointF, p3: QPointF,
        target: QPointF,
        depth: int
    ) -> tuple[QPointF, float]:
        """Find nearest point on cubic Bezier curve using recursive subdivision.

        Args:
            p0, p1, p2, p3: Bezier curve control points
            target: Point to find nearest point to
            depth: Recursion depth (controls precision)

        Returns:
            (nearest_point, distance) tuple
        """
        if depth <= 0:
            # At max depth, just sample endpoints and midpoint
            mid_t = 0.5
            mid_point = self._eval_cubic_bezier(p0, p1, p2, p3, mid_t)

            dist_start = self._point_distance(p0, target)
            dist_mid = self._point_distance(mid_point, target)
            dist_end = self._point_distance(p3, target)

            if dist_start <= dist_mid and dist_start <= dist_end:
                return (p0, dist_start)
            elif dist_mid <= dist_end:
                return (mid_point, dist_mid)
            else:
                return (p3, dist_end)

        # Subdivide at t=0.5 using de Casteljau's algorithm
        mid01 = self._midpoint(p0, p1)
        mid12 = self._midpoint(p1, p2)
        mid23 = self._midpoint(p2, p3)
        mid012 = self._midpoint(mid01, mid12)
        mid123 = self._midpoint(mid12, mid23)
        mid_point = self._midpoint(mid012, mid123)

        # Check distances at key points to decide which half to recurse
        dist_start = self._point_distance(p0, target)
        dist_mid = self._point_distance(mid_point, target)
        dist_end = self._point_distance(p3, target)

        # If we're close enough at midpoint, return it
        if dist_mid < self.SNAP_DISTANCE:
            return (mid_point, dist_mid)

        # Recurse on the closer half
        if dist_start < dist_end:
            return self._find_nearest_on_cubic_bezier(
                p0, mid01, mid012, mid_point, target, depth - 1
            )
        else:
            return self._find_nearest_on_cubic_bezier(
                mid_point, mid123, mid23, p3, target, depth - 1
            )

    def _eval_cubic_bezier(
        self, p0: QPointF, p1: QPointF, p2: QPointF, p3: QPointF, t: float
    ) -> QPointF:
        """Evaluate cubic Bezier curve at parameter t."""
        u = 1 - t
        u2 = u * u
        u3 = u2 * u
        t2 = t * t
        t3 = t2 * t

        x = u3 * p0.x() + 3 * u2 * t * p1.x() + 3 * u * t2 * p2.x() + t3 * p3.x()
        y = u3 * p0.y() + 3 * u2 * t * p1.y() + 3 * u * t2 * p2.y() + t3 * p3.y()

        return QPointF(x, y)

    def _midpoint(self, p1: QPointF, p2: QPointF) -> QPointF:
        """Calculate midpoint between two points."""
        return QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)

    def _point_distance(self, p1: QPointF, p2: QPointF) -> float:
        """Calculate Euclidean distance between two points."""
        dx = p1.x() - p2.x()
        dy = p1.y() - p2.y()
        return (dx * dx + dy * dy) ** 0.5

    def get_midpoint_scene(self) -> QPointF:
        """Get the scene-coordinate midpoint of this connection's Bezier curve."""
        if self.src_port is None or self.dst_port is None:
            return QPointF(0, 0)

        src_pos = self.src_port.get_center_scene_pos()
        dst_pos = self.dst_port.get_center_scene_pos()

        dx = abs(dst_pos.x() - src_pos.x())
        offset = max(dx * 0.5, 60)
        ctrl1 = QPointF(src_pos.x() + offset, src_pos.y())
        ctrl2 = QPointF(dst_pos.x() - offset, dst_pos.y())

        return self._eval_cubic_bezier(src_pos, ctrl1, ctrl2, dst_pos, 0.5)

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
        """Get the approximate t value (0-1) where to insert a Dot node.

        This is useful for determining which end of the connection
        the insert point is closer to.

        Args:
            scene_pos: Position where user wants to insert

        Returns:
            t value approximating position along curve (0 = source, 1 = destination)
        """
        if self.src_port is None or self.dst_port is None:
            return 0.5

        src_pos = self.src_port.get_center_scene_pos()
        dst_pos = self.dst_port.get_center_scene_pos()

        nearest_point, _ = self.get_nearest_point_on_path(scene_pos)

        # Approximate t by comparing distances to endpoints
        dist_to_src = self._point_distance(nearest_point, src_pos)
        total_dist = self._point_distance(src_pos, dst_pos)

        if total_dist < 1.0:
            return 0.5

        return dist_to_src / total_dist
