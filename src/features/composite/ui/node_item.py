"""
Node Item - Visual representation of a node in the node graph.

Displays as a rounded rectangle with:
- Colored header bar with node title
- Input ports on the left side
- Output ports on the right side
- Drop shadow and selection glow
- Disabled state overlay
- Optional thumbnail preview at bottom (visibility driven by editor preview policy)
"""

from PyQt6.QtWidgets import (
    QGraphicsRectItem, QGraphicsItem, QGraphicsTextItem,
    QGraphicsDropShadowEffect,     QGraphicsProxyWidget, QLineEdit,
    QGraphicsSceneMouseEvent, QStyleOptionGraphicsItem
)
from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import (
    QPen, QBrush, QColor, QPainter, QPainterPath, QFont,
    QLinearGradient, QPixmap, QKeyEvent, QFocusEvent
)
from typing import Any, Dict, List, Optional

from .port_item import PortItem
from .connection_item import ConnectionItem


class _RenameLineEdit(QLineEdit):
    """Inline rename editor (B4). Escape cancels; focus loss commits."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            esc = getattr(self, "_esc_hook", None)
            if esc:
                esc()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        commit = getattr(self, "_commit_hook", None)
        if commit:
            commit()


class NodeItem(QGraphicsRectItem):
    """
    Visual node representation in the node graph.

    Layout:
    ┌──────────────────────────┐
    │  ████ Header ████        │  ← Colored header with title
    ├──────────────────────────┤
    │ ● Input A     Output ●   │  ← Ports
    │ ● Input B                │
    └──────────────────────────┘
    """

    NODE_WIDTH = 180
    HEADER_HEIGHT = 28
    PORT_ROW_HEIGHT = 24
    PORT_MARGIN = 14
    BODY_PADDING = 6
    CORNER_RADIUS = 8
    STATUS_BAR_HEIGHT = 6
    THUMBNAIL_SIZE = 64         # Legacy: thumbnail preview max dimension
    THUMBNAIL_MARGIN = 6        # Margin around thumbnail
    THUMBNAIL_MAX_HEIGHT = 120  # Max thumbnail display height in node

    # Dot node constants
    DOT_SIZE = 14               # Diameter of dot node circle
    DOT_PORT_OFFSET = 7         # Port position offset from center

    def __init__(self, node_id: str, node_title: str = "Node",
                 header_color: str = "#555555", is_dot: bool = False,
                 node_type: str = "", parent=None) -> None:
        self._is_dot = is_dot
        # node_type 用于节点级自动匹配（如 Camera3D 落到 Scene3D 上自动选 camera_in）。
        self.node_type = node_type

        if is_dot:
            # Dot node: small square, centered on the dot circle
            super().__init__(0, 0, self.DOT_SIZE, self.DOT_SIZE, parent)
        else:
            # Calculate initial height
            self._num_rows = 1  # Will be updated
            h = self.HEADER_HEIGHT + self.BODY_PADDING * 2 + self._num_rows * self.PORT_ROW_HEIGHT
            super().__init__(0, 0, self.NODE_WIDTH, h, parent)

        self.node_id = node_id
        self.node_title = node_title
        self.header_color = QColor(header_color)

        # Ports
        self.input_ports: Dict[str, PortItem] = {}
        self.output_ports: Dict[str, PortItem] = {}

        # Connections (for updating curves)
        self.connections: List[ConnectionItem] = []

        # Disabled state
        self._disabled = False

        # Read-only graph state.  A locked node remains selectable so the
        # user can inspect it, but cannot be moved through the canvas.
        self._locked = False

        # State tracking (dirty/processing/cached/failed/bypassed/placeholder)
        self._state = "dirty"
        self._progress = 0.0
        self._error_msg = ""
        self._diagnostic_level = ""
        self._diagnostic_message = ""
        self._dependency_highlighted = False

        # Performance badge data (set from UI thread)
        self._perf_time_ms: float = 0.0
        self._perf_cached: bool = False
        self._perf_error: Optional[str] = None
        
        # Performance: LOD (Level of Detail)
        self._simplified_draw = False

        # Shake-to-disconnect glow (0.0 to 1.0)
        self._shake_glow = 0.0

        # Thumbnail preview (optional; visibility set by the editor's preview policy)
        self._thumbnail: Optional[QPixmap] = None
        self._show_thumbnail: bool = True  # Effective visibility, driven by editor
        # AUD-10621: cache the smooth-scaled thumbnail. The scaled result only
        # depends on (source pixmap, target size), but paint() runs on every
        # repaint (drag, progress, state switches) — recomputing the smooth
        # scale each frame is pure CPU waste on multi-node canvases. The cache
        # key uses QPixmap.cacheKey() so content replacement invalidates it.
        self._scaled_thumbnail: Optional[QPixmap] = None
        self._scaled_thumb_key: Optional[tuple] = None

        # B4: inline rename editor proxy (None when not renaming)
        self._rename_proxy: Optional[QGraphicsProxyWidget] = None

        # Preview-pipeline dashed connections (node → Output) attached to this
        # node. Updated in itemChange() so the curve follows the node. Empty by
        # default; NodeEditorWidget toggles membership via attach/detach.
        self._preview_connections: list = []

        # Visual setup
        if not self._is_dot:
            self._setup_visuals()
            self._create_title()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    def _setup_visuals(self) -> None:
        """Set up the visual style of the node."""
        # Drop shadow
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(12)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(2, 3)
        self.setGraphicsEffect(shadow)

    def _create_title(self) -> None:
        """Create the title text in the header."""
        self._title_item = QGraphicsTextItem(self)
        self._title_item.setPlainText(self.node_title)
        font = QFont()
        font.setPixelSize(12)
        font.setBold(True)
        self._title_item.setFont(font)
        self._title_item.setDefaultTextColor(QColor(240, 240, 240))

        # Position title centered in header
        text_width = self._title_item.boundingRect().width()
        x = (self.NODE_WIDTH - text_width) / 2
        self._title_item.setPos(x, 6)

    def set_title(self, new_title: str) -> None:
        """Update the on-canvas title dynamically (命名升级 P1).

        Used when ``instance_label`` changes after creation (e.g. 序号重分配、
        图标选中态切换）。重算居中位置，触发重绘。
        """
        self.node_title = new_title
        if self._title_item is not None:
            self._title_item.setPlainText(new_title)
            text_width = self._title_item.boundingRect().width()
            x = (self.NODE_WIDTH - text_width) / 2
            self._title_item.setPos(x, 6)
            self.update()

    # ── B4: in-canvas inline rename ──

    def begin_inline_rename(self, on_commit, on_cancel=None) -> None:
        """Overlay a QLineEdit on the header for in-canvas renaming.

        Args:
            on_commit: callback(new_title: str) fired on Enter or focus loss
                (empty text commits as "clear custom label").
            on_cancel: optional callback fired on Escape.

        The editor is a QGraphicsProxyWidget child stacked above the title
        text so it follows the node while dragged. Enter / focus loss commit;
        Escape cancels and restores the previous title.
        """
        if self._is_dot or self._title_item is None:
            return
        if self._rename_proxy is not None:
            return  # already editing

        def _finish(commit: bool) -> None:
            if self._rename_proxy is None:
                return
            editor, proxy = self._rename_proxy.widget(), self._rename_proxy
            self._rename_proxy = None
            text = editor.text().strip() if commit else ""
            proxy.setParentItem(None)
            proxy.setWidget(None)
            proxy.deleteLater()
            if self._title_item is not None:
                self._title_item.setVisible(True)
            if commit:
                on_commit(text)
            elif on_cancel is not None:
                on_cancel()

        editor = _RenameLineEdit(self.node_title)
        editor._esc_hook = lambda: _finish(False)
        # A commit on an UNCHANGED text is not a rename — it would pin the
        # auto title as a user_label and flood the undo stack with a no-op
        # entry.  Treat Enter / focus loss on identical text as cancel.
        initial_text = self.node_title

        def _commit_if_changed() -> None:
            _finish(editor.text().strip() != initial_text.strip())

        editor._commit_hook = _commit_if_changed
        editor.returnPressed.connect(_commit_if_changed)
        editor.setStyleSheet(
            "QLineEdit { background: #1e1e1e; color: #f0f0f0;"
            " border: 1px solid #4a9eff; border-radius: 3px;"
            " padding: 1px 4px; font-size: 11px; selection-background-color: #4a9eff; }"
        )
        editor.setFixedHeight(self.HEADER_HEIGHT - 8)

        proxy = QGraphicsProxyWidget(self)
        proxy.setWidget(editor)
        proxy.setZValue(10)
        editor_width = max(60, self.NODE_WIDTH - 24)
        proxy.setGeometry(QRectF(
            (self.NODE_WIDTH - editor_width) / 2, 4.0,
            editor_width, self.HEADER_HEIGHT - 8))
        self._rename_proxy = proxy
        self._title_item.setVisible(False)
        editor.setFocus()

    @property
    def is_renaming(self) -> bool:
        return self._rename_proxy is not None

    def add_input_port(self, name: str, data_type: str = "image", max_connections: int = 1):
        """Add an input port to the left side of the node.

        max_connections: 端口容量（1=单连接默认，>1=多输入，如 Scene3D scene 端口）。
        """
        port = PortItem(name, "input", data_type, max_connections=max_connections, parent=self)
        self.input_ports[name] = port
        self._update_layout()
        return port

    def add_output_port(self, name: str, data_type: str = "image"):
        """Add an output port to the right side of the node."""
        port = PortItem(name, "output", data_type, parent=self)
        self.output_ports[name] = port
        self._update_layout()
        return port

    def _update_layout(self) -> None:
        """Recalculate node size and port positions.

        Note: STATUS_BAR_HEIGHT is always included to prevent height changes
        when entering processing state, which would cause connection line jitter.
        The progress bar is drawn overlaying the bottom area when processing.

        When showing a thumbnail (driven by the editor's preview policy via
        ``_show_thumbnail``), extra space is added below the node to display it.

        For dot nodes, uses a minimal circle layout with ports at edges.
        """
        if self._is_dot:
            # Dot node: fixed small size, ports at left/right edges
            self.prepareGeometryChange()
            self.setRect(0, 0, self.DOT_SIZE, self.DOT_SIZE)
            center_y = self.DOT_SIZE / 2
            for name, port in self.input_ports.items():
                port.setPos(0, center_y)
            for name, port in self.output_ports.items():
                port.setPos(self.DOT_SIZE, center_y)
            self.update()
            return

        num_rows = max(len(self.input_ports), len(self.output_ports), 1)
        # Always reserve space for status bar to prevent height fluctuations
        total_height = (
            self.HEADER_HEIGHT
            + self.BODY_PADDING * 2
            + num_rows * self.PORT_ROW_HEIGHT
            + self.STATUS_BAR_HEIGHT
        )

        # Add space for thumbnail when enabled (editor policy) and thumbnail exists
        if self._show_thumbnail and self._thumbnail:
            thumb_h = self._calc_thumbnail_display_height()
            total_height += thumb_h + self.THUMBNAIL_MARGIN * 2

        # Must notify scene before geometry change, otherwise old area
        # (glow, shadow) leaves visual artifacts (residual yellow glow).
        self.prepareGeometryChange()
        self.setRect(0, 0, self.NODE_WIDTH, total_height)

        # Position input ports (left side)
        for i, (name, port) in enumerate(self.input_ports.items()):
            y = self.HEADER_HEIGHT + self.BODY_PADDING + i * self.PORT_ROW_HEIGHT + self.PORT_ROW_HEIGHT // 2
            port.setPos(0, y)

        # Position output ports (right side)
        for i, (name, port) in enumerate(self.output_ports.items()):
            y = self.HEADER_HEIGHT + self.BODY_PADDING + i * self.PORT_ROW_HEIGHT + self.PORT_ROW_HEIGHT // 2
            port.setPos(self.NODE_WIDTH, y)

        self.update()

    def update_connections(self) -> None:
        """Update all connection curves from this node."""
        for conn in self.connections:
            conn.update_path()

    def get_port_at(self, scene_pos: QPointF) -> Optional[PortItem]:
        """Get the port item at a scene position, if any."""
        for port in list(self.input_ports.values()) + list(self.output_ports.values()):
            port_scene = port.scenePos()
            dx = scene_pos.x() - port_scene.x()
            dy = scene_pos.y() - port_scene.y()
            if dx * dx + dy * dy < (PortItem.PORT_RADIUS + 4) ** 2:
                return port
        return None

    def set_disabled(self, disabled: bool) -> None:
        """Set the disabled/bypass state."""
        self._disabled = disabled
        self._state = "bypassed" if disabled else "dirty"
        self.update()

    @property
    def is_locked(self) -> bool:
        """Whether canvas edits should be blocked for this node."""
        return self._locked

    def set_locked(self, locked: bool) -> None:
        """Toggle the read-only state without changing graph parameters."""
        self._locked = bool(locked)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
                     not self._locked)
        self._refresh_tooltip()
        self.update()

    def set_diagnostic(self, level: str, message: str) -> None:
        """Attach a non-rendering preflight diagnostic to this node."""
        normalized = str(level).strip().lower()
        if normalized not in {"error", "warning", "info"}:
            normalized = "info"
        self._diagnostic_level = normalized
        self._diagnostic_message = str(message).strip()
        self._refresh_tooltip()
        self.update()

    def set_dependency_highlighted(self, highlighted: bool) -> None:
        """Mark this node as part of the currently focused graph branch."""
        self._dependency_highlighted = bool(highlighted)
        self.update()

    def clear_diagnostic(self) -> None:
        """Remove the current preflight diagnostic marker."""
        self._diagnostic_level = ""
        self._diagnostic_message = ""
        self._refresh_tooltip()
        self.update()

    @property
    def diagnostic_level(self) -> str:
        """Return the current preflight diagnostic severity."""
        return self._diagnostic_level

    @property
    def diagnostic_message(self) -> str:
        """Return the current preflight diagnostic tooltip text."""
        return self._diagnostic_message

    def _refresh_tooltip(self) -> None:
        """Keep lock, runtime error and preflight text visible together."""
        messages = []
        if self._locked:
            messages.append("Node is locked. Unlock it to move or edit connections.")
        if self._state == "failed" and self._error_msg:
            messages.append(self._error_msg)
        if self._diagnostic_message:
            messages.append(self._diagnostic_message)
        self.setToolTip("\n".join(messages))

    def set_state(self, state: str, progress: float = 0.0, error_msg: str = "") -> None:
        """Set the node state for visual rendering."""
        self._state = state
        self._progress = progress
        self._error_msg = error_msg
        self._refresh_tooltip()
        # Adjust height for processing state (progress bar)
        self._update_layout()
        self.update()

    def set_progress(self, progress: float) -> None:
        """Update progress for processing state."""
        self._progress = progress
        self.update()

    def set_perf_badge(self, eval_time_ms: float = 0.0, was_cached: bool = False,
                       error: Optional[str] = None) -> None:
        """Set performance badge data for this node.

        Called from UI thread after each frame_ready.
        Args:
            eval_time_ms: CPU wall time for this node's evaluation.
            was_cached: Whether the result came from cache.
            error: Error message if evaluation failed.
        """
        self._perf_time_ms = eval_time_ms
        self._perf_cached = was_cached
        self._perf_error = error
        self.update()

    def set_simplified_draw(self, enabled: bool) -> None:
        """Enable/disable simplified drawing for LOD.
        
        When enabled, draws only a simple colored rectangle without details.
        """
        if self._simplified_draw == enabled:
            return
        self._simplified_draw = enabled
        self.update()

    def boundingRect(self) -> QRectF:
        """Expand bounding rect to cover selection/shake glow drawn outside base rect.

        AUD-10620: the non-dot paint() draws a selection glow on
        ``rect.adjusted(-4, -4, 4, 4)`` stroked with a pen of width 8, i.e.
        4px offset + 4px half pen-width ≈ 8px beyond the base rect; the
        shake-to-disconnect glow (offset -4, pen 4) reaches ≈6px. Without the
        expansion, partial scene redraws clip those strokes and leave
        residual glow artifacts at the node edges.
        """
        rect = super().boundingRect()
        if self._is_dot:
            # Selection glow: radius r+3=10, pen width 4 → extends ~5px beyond rect
            return rect.adjusted(-5, -5, 5, 5)
        # Selection glow (4px offset + 4px half-pen) and shake glow both stay
        # within 8px of the base rect.
        return rect.adjusted(-8, -8, 8, 8)

    def _paint_dot(self, painter: QPainter) -> None:
        """Minimal rendering for Dot nodes — just a small filled circle."""
        r = self.DOT_SIZE / 2
        center = QPointF(r, r)

        # Selection glow
        if self.isSelected():
            painter.setPen(QPen(QColor(255, 180, 60, 100), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r + 3, r + 3)

        # Main circle
        body_color = QColor(60, 60, 64) if not self._disabled else QColor(45, 45, 48)
        border_color = QColor(100, 100, 105) if not self._disabled else QColor(60, 60, 65)
        painter.setPen(QPen(border_color, 1.5))
        painter.setBrush(QBrush(body_color))
        painter.drawEllipse(center, r, r)

        # Inner dot (data type color hint)
        inner_color = QColor(self.header_color)
        inner_color.setAlpha(200)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(inner_color))
        painter.drawEllipse(center, r * 0.45, r * 0.45)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: Optional[Any] = None) -> None:
        """Custom paint with rounded rect, header gradient, and state overlays."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Dot node: minimal circle rendering
        if self._is_dot:
            self._paint_dot(painter)
            return

        # Performance: Simplified drawing at low zoom
        if self._simplified_draw:
            rect = self.rect()
            # Just draw a simple colored rectangle
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.header_color))
            painter.drawRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
            return

        rect = self.rect()

        # Body - rounded rectangle with subtle gradient
        body_gradient = QLinearGradient(0, 0, 0, rect.height())
        if self._disabled:
            body_gradient.setColorAt(0, QColor(55, 55, 58))
            body_gradient.setColorAt(1, QColor(40, 40, 42))
        else:
            body_gradient.setColorAt(0, QColor(50, 50, 54))
            body_gradient.setColorAt(1, QColor(38, 38, 42))

        path = QPainterPath()
        path.addRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        # Selection glow (multi-layer for soft effect)
        if self.isSelected():
            # Layer 1: Outermost soft glow
            glow_path = QPainterPath()
            glow_rect = rect.adjusted(-4, -4, 4, 4)
            glow_path.addRoundedRect(glow_rect, self.CORNER_RADIUS + 3, self.CORNER_RADIUS + 3)
            painter.setPen(QPen(QColor(255, 180, 60, 25), 8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(glow_path)

            # Layer 2: Mid glow
            glow_path2 = QPainterPath()
            glow_rect2 = rect.adjusted(-2, -2, 2, 2)
            glow_path2.addRoundedRect(glow_rect2, self.CORNER_RADIUS + 1, self.CORNER_RADIUS + 1)
            painter.setPen(QPen(QColor(255, 180, 60, 50), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(glow_path2)

            # Layer 3: Sharp border
            painter.setPen(QPen(QColor(255, 180, 60), 2))
        elif self._dependency_highlighted:
            painter.setPen(QPen(QColor(95, 185, 255, 230), 2))
        else:
            painter.setPen(QPen(QColor(70, 70, 75), 1))

        painter.setBrush(QBrush(body_gradient))
        painter.drawPath(path)

        # Shake-to-disconnect glow overlay
        if self._shake_glow > 0:
            shake_alpha = int(self._shake_glow * 80)
            shake_pen_alpha = int(self._shake_glow * 200)

            # Glow border
            shake_glow_rect = rect.adjusted(-4, -4, 4, 4)
            shake_path = QPainterPath()
            shake_path.addRoundedRect(
                shake_glow_rect,
                self.CORNER_RADIUS + 3,
                self.CORNER_RADIUS + 3,
            )
            painter.setPen(QPen(QColor(255, 60, 60, shake_pen_alpha), 4))
            painter.setBrush(QBrush(QColor(255, 60, 60, shake_alpha)))
            painter.drawPath(shake_path)

        # Header - colored rectangle at top with rounded top corners
        header_path = QPainterPath()
        header_rect = QRectF(0, 0, rect.width(), self.HEADER_HEIGHT)
        header_path.addRoundedRect(header_rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

        # Clip the bottom of the header (no rounded bottom corners)
        clip_path = QPainterPath()
        clip_path.addRect(0, 0, rect.width(), self.HEADER_HEIGHT)

        header_gradient = QLinearGradient(0, 0, 0, self.HEADER_HEIGHT)
        if self._disabled:
            disabled_color = self.header_color.darker(200)
            header_gradient.setColorAt(0, disabled_color)
            header_gradient.setColorAt(1, disabled_color.darker(120))
        else:
            header_gradient.setColorAt(0, self.header_color.lighter(130))
            header_gradient.setColorAt(1, self.header_color)
        painter.setBrush(QBrush(header_gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(header_path & clip_path)

        # Separator line between header and body
        sep_y = self.HEADER_HEIGHT
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.drawLine(int(rect.left() + 4), int(sep_y), int(rect.right() - 4), int(sep_y))

        # Locked badge — drawn as a small vector lock so it remains readable
        # with the system font and does not depend on an emoji glyph.
        if self._locked:
            lock_x = rect.width() - 20
            lock_y = 7
            painter.setPen(QPen(QColor(255, 220, 120, 230), 1.4))
            painter.setBrush(QBrush(QColor(55, 45, 25, 180)))
            painter.drawRoundedRect(lock_x, lock_y + 6, 11, 8, 2, 2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(lock_x + 2, lock_y, 7, 10, 0, 180 * 16)

        # Port labels
        font = QFont()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(190, 190, 190) if not self._disabled else QColor(120, 120, 120))

        for name, port in self.input_ports.items():
            pos = port.pos()
            painter.drawText(int(pos.x() + 12), int(pos.y() + 4), name)

        for name, port in self.output_ports.items():
            pos = port.pos()
            text_width = painter.fontMetrics().horizontalAdvance(name)
            painter.drawText(int(pos.x() - text_width - 12), int(pos.y() + 4), name)

        # Disabled overlay — diagonal hatch lines
        if self._disabled:
            painter.setPen(QPen(QColor(255, 80, 80, 60), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            step = 16
            for i in range(-int(rect.height()), int(rect.width()) + int(rect.height()), step):
                p1 = QPointF(max(rect.left(), i), rect.top() + max(0, -i))
                p2 = QPointF(min(rect.right(), i + rect.height()), rect.top() + min(rect.height(), rect.right() - i))
                painter.drawLine(p1, p2)

            # "BYPASS" text in center
            painter.setPen(QColor(255, 80, 80, 120))
            bypass_font = QFont()
            bypass_font.setPixelSize(9)
            bypass_font.setBold(True)
            painter.setFont(bypass_font)
            center = rect.center()
            painter.drawText(int(center.x() - 25), int(center.y() + 3), "BYPASS")

        # ── State overlays ──

        # Placeholder state — yellow hatch + "NOT IMPLEMENTED"
        elif self._state == "placeholder":
            painter.setPen(QPen(QColor(200, 180, 40, 50), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            step = 16
            for i in range(-int(rect.height()), int(rect.width()) + int(rect.height()), step):
                p1 = QPointF(max(rect.left(), i), rect.top() + max(0, -i))
                p2 = QPointF(min(rect.right(), i + rect.height()), rect.top() + min(rect.height(), rect.right() - i))
                painter.drawLine(p1, p2)

            painter.setPen(QColor(200, 180, 40, 140))
            font = QFont()
            font.setPixelSize(8)
            font.setBold(True)
            painter.setFont(font)
            center = rect.center()
            text = "NOT IMPLEMENTED"
            tw = painter.fontMetrics().horizontalAdvance(text)
            painter.drawText(int(center.x() - tw // 2), int(center.y() + 3), text)

        # Processing state — progress bar at bottom
        if self._state == "processing":
            bar_y = rect.height() - self.STATUS_BAR_HEIGHT
            bar_w = rect.width() * min(self._progress, 1.0)

            # Background track
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(40, 40, 50))
            painter.drawRect(0, int(bar_y), int(rect.width()), self.STATUS_BAR_HEIGHT)

            # Progress fill
            painter.setBrush(QColor(64, 128, 255, 180))
            painter.drawRect(0, int(bar_y), int(bar_w), self.STATUS_BAR_HEIGHT)

        # Cached badge — green dot in header top-right
        if self._state == "cached":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(80, 200, 80))
            painter.drawEllipse(int(rect.width() - 16), 6, 8, 8)

        # Failed badge — red triangle in header top-right
        if self._state == "failed":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 60, 60))
            tri_x = rect.width() - 17
            tri_y = 5
            tri_path = QPainterPath()
            tri_path.moveTo(tri_x, tri_y + 10)
            tri_path.lineTo(tri_x + 5, tri_y)
            tri_path.lineTo(tri_x + 10, tri_y + 10)
            tri_path.closeSubpath()
            painter.drawPath(tri_path)

        # Preflight diagnostic badge.  Runtime state remains independent so a
        # warning/error can be cleared without changing render evaluation.
        if self._diagnostic_level:
            diagnostic_colors = {
                "error": QColor(255, 75, 75),
                "warning": QColor(255, 205, 70),
                "info": QColor(90, 175, 255),
            }
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(diagnostic_colors[self._diagnostic_level])
            painter.drawEllipse(6, 6, 8, 8)

        # ── Performance badge ── (ms timing or "cached" at node bottom)
        if self._perf_time_ms > 0 or self._perf_cached:
            font = QFont()
            font.setPixelSize(9)
            painter.setFont(font)

            if self._perf_cached:
                badge_text = "cached"
                badge_color = QColor(80, 200, 80, 200)
            elif self._perf_error:
                badge_text = f"{self._perf_time_ms:.1f}ms"
                badge_color = QColor(255, 60, 60, 220)
            elif self._perf_time_ms >= 50.0:
                badge_text = f"{self._perf_time_ms:.1f}ms"
                badge_color = QColor(255, 80, 80, 220)
            elif self._perf_time_ms >= 10.0:
                badge_text = f"{self._perf_time_ms:.1f}ms"
                badge_color = QColor(255, 200, 60, 220)
            else:
                badge_text = f"{self._perf_time_ms:.1f}ms"
                badge_color = QColor(200, 200, 200, 180)

            tw = painter.fontMetrics().horizontalAdvance(badge_text)
            badge_x = int((self.NODE_WIDTH - tw) / 2)
            badge_y = int(rect.height()) - 12

            # Subtle background pill
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 120))
            painter.drawRoundedRect(badge_x - 4, badge_y - 10, tw + 8, 14, 3, 3)

            painter.setPen(badge_color)
            painter.drawText(badge_x, badge_y, badge_text)

        # ── Thumbnail preview ── (shown when _show_thumbnail, set by editor policy)
        if self._show_thumbnail and self._thumbnail:
            avail_w = self.NODE_WIDTH - self.THUMBNAIL_MARGIN * 2
            thumb_h = self._calc_thumbnail_display_height()
            thumb_x = self.THUMBNAIL_MARGIN
            thumb_y = int(rect.height()) - thumb_h - self.THUMBNAIL_MARGIN

            # AUD-10621: reuse the cached smooth-scaled pixmap; only recompute
            # when the source content or the target size changed.
            scaled = self._get_scaled_thumbnail(avail_w, thumb_h)

            # Center the scaled pixmap (crop overflow)
            draw_x = thumb_x + (avail_w - scaled.width()) // 2
            draw_y = thumb_y + (thumb_h - scaled.height()) // 2

            # Clip to thumbnail area with rounded corners
            painter.save()
            clip_path = QPainterPath()
            clip_path.addRoundedRect(thumb_x, thumb_y, avail_w, thumb_h, 4, 4)
            painter.setClipPath(clip_path)
            painter.drawPixmap(draw_x, draw_y, scaled)
            painter.restore()

            # Border
            painter.setPen(QPen(QColor(100, 100, 100), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(thumb_x, thumb_y, avail_w, thumb_h, 4, 4)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Handle double-click (for COLOR node → switch to Color page)."""
        # This will be connected by the NodeEditorWidget
        if hasattr(self, '_double_click_handler'):
            self._double_click_handler(self.node_id)
        super().mouseDoubleClickEvent(event)

    def set_double_click_handler(self, handler) -> None:
        self._double_click_handler = handler

    def set_selected(self, selected: bool) -> None:
        """Set visual selection state."""
        self.prepareGeometryChange()  # boundingRect depends on selection state
        self.setSelected(selected)
        self.update()

    def set_thumbnail(self, pixmap: Optional[QPixmap]) -> None:
        """Set the thumbnail preview pixmap.

        Args:
            pixmap: QPixmap to display as thumbnail (64x64), or None to clear.
        """
        self._thumbnail = pixmap
        # AUD-10621: drop the stale scaled cache (recomputed lazily in paint).
        self._scaled_thumbnail = None
        self._scaled_thumb_key = None
        # Update layout to accommodate thumbnail space when selected
        self._update_layout()
        self.update()

    def _get_scaled_thumbnail(self, avail_w: int, thumb_h: int) -> Optional[QPixmap]:
        """Return the thumbnail smooth-scaled to the display area (cached).

        Cache key: (source cacheKey, target size) — ``QPixmap.cacheKey()``
        changes whenever the source pixmap's content is replaced or modified,
        so the cache never serves a stale image. Scaling mode matches the
        previous per-frame behaviour exactly (expanding + smooth).
        """
        if self._thumbnail is None:
            return None
        key = (self._thumbnail.cacheKey(), avail_w, thumb_h)
        if self._scaled_thumbnail is None or self._scaled_thumb_key != key:
            self._scaled_thumbnail = self._thumbnail.scaled(
                avail_w, thumb_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            self._scaled_thumb_key = key
        return self._scaled_thumbnail

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange,
                   value: Any):
        """Update connections and layout when node state changes."""
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.update_connections()
            # Keep the preview-pipeline dashed connection (node → Output) in
            # sync as the endpoint moves. ``_preview_connections`` is populated
            # by NodeEditorWidget when a node is being previewed.
            for pc in getattr(self, "_preview_connections", ()):
                pc.update_path()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Update layout when selection changes (thumbnail space)
            self._update_layout()
        return super().itemChange(change, value)

    def set_thumbnail_visible(self, visible: bool) -> None:
        """Set whether this node currently displays its thumbnail.

        Driven by the editor's preview-mode policy (not selection). Re-layouts
        so the node's height reserves/releases the thumbnail area. No-op if the
        flag is unchanged.
        """
        if self._show_thumbnail == visible:
            return
        self._show_thumbnail = visible
        self._update_layout()
        self.update()

    def _calc_thumbnail_display_height(self) -> int:
        """Calculate thumbnail display height preserving source aspect ratio.

        The thumbnail fills the full node width (minus margins).
        Height is proportional to source aspect ratio, capped at THUMBNAIL_MAX_HEIGHT.
        """
        if self._thumbnail is None:
            return self.THUMBNAIL_MAX_HEIGHT
        avail_w = self.NODE_WIDTH - self.THUMBNAIL_MARGIN * 2
        pm_w = self._thumbnail.width()
        pm_h = self._thumbnail.height()
        if pm_w > 0 and pm_h > 0:
            aspect = pm_h / pm_w
            return min(int(avail_w * aspect), self.THUMBNAIL_MAX_HEIGHT)
        return self.THUMBNAIL_MAX_HEIGHT

    def clear_thumbnail(self) -> None:
        """Clear the thumbnail preview."""
        self._thumbnail = None
        # AUD-10621: drop the scaled cache along with the source.
        self._scaled_thumbnail = None
        self._scaled_thumb_key = None
        self.update()
