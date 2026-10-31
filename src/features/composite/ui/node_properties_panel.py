"""
Node Properties Panel - Displays parameters of the selected node.

Shows node title, type, and editable parameters (sliders, combo boxes, etc.)
in a dockable panel on the right side of the Composite page.

Supports parameter metadata (min/max/step/range) from PARAM_DEFS.

Upgraded features:
- Color picker (ptype: color)
- File picker (ptype: file)
- Vec2/Vec3 controls (ptype: vec2, vec3)
- Channel selector (ptype: channel)
- Parameter search
- Reset parameter button
- Copy/paste parameters
- Grouped parameter display (group attribute)
- Keyframe animation editor for float/int parameters
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QSlider, QDoubleSpinBox, QSpinBox, QGroupBox, QScrollArea, QSizePolicy,
    QFrame, QPushButton, QLineEdit, QMenu, QDockWidget, QCheckBox,
    QFileDialog, QToolButton, QDialog, QDialogButtonBox,
    QPlainTextEdit
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QSize
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QPainterPath, QFont, QFontMetrics,
    QShortcut, QKeySequence, QLinearGradient, QMouseEvent,
)
from typing import Callable, Dict, Any, Optional, List, Tuple
import os

from .keyframe_editor import KeyframeEditorWidget
from src.shared.assets.icon_manager import get_icon, IconColors, make_link_button
from src.shared.i18n.i18n import I18N, tr, tr_or_none
from src.shared.ui.widgets import ToggleSwitch
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.style_sheet import (
    NODE_SECTION_STYLE,
    NODE_COLLAPSE_BTN_STYLE,
    NODE_TITLE_LABEL_STYLE,
    NODE_CONTENT_STYLE,
    PROPS_HEADER_STYLE,
    PROPS_TITLE_LABEL_STYLE,
    TOOLBAR_FLAT_BTN_STYLE,
    NODE_GROUP_BOX_STYLE,
    apply_combo_dropdown_style,
)


# Numeric rows show their optional slider only when the panel is at least this
# wide (the panel is user-resizable; on narrow widths the slider auto-hides and
# the drag-value box is the sole control). Set above the X/Y row's minimum width
# so sliders never appear until there's room for them without clipping the
# right-side value/keyframe controls.
_SLIDER_SHOW_WIDTH = 380
# XYZ 三合一行有 3 个 slider（比 XY 多一个），需要更宽的面板才显示 slider，
# 否则 3 个 slider 会挤占 DragValue 空间导致布局溢出。空间不足时优先隐藏。
_SLIDER_SHOW_WIDTH_TRIPLE = 480


# ChevronComboBox imported from src.shared.ui.widgets.chevron_combo


# Fixed pixel width of the numeric value box — it stays this wide regardless of
# panel width; the slider (when shown) takes the variable space.
_VALUE_FIXED_WIDTH = 64

# Style for the small X/Y "chain" toggle and the keyframe ◆ button on rows.
_CHAIN_STYLE = """
    QToolButton {
        background: transparent; border: 1px solid #555; border-radius: 2px;
        font-size: 12px; padding: 0px; color: #777;
    }
    QToolButton:checked { color: #ff8800; border-color: #ff8800; }
    QToolButton:hover { border-color: #888; }
"""


class TrimRangeSlider(QWidget):
    """Compact two-handle frame-range slider used by video nodes.

    The values are inclusive source-frame numbers.  It deliberately owns no
    graph state; the properties panel routes its signal through the normal
    parameter-change path so undo/render behavior stays centralized.
    """

    range_changed = pyqtSignal(int, int)

    def __init__(self, minimum: int, maximum: int, start: int, end: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._minimum = int(minimum)
        self._maximum = max(self._minimum, int(maximum))
        self._start = self._clamp(start)
        self._end = max(self._start, self._clamp(end))
        self._active_handle: Optional[str] = None
        self.setMinimumHeight(26)
        self.setMouseTracking(True)

    def _clamp(self, value: int) -> int:
        return max(self._minimum, min(int(value), self._maximum))

    def values(self) -> Tuple[int, int]:
        return self._start, self._end

    def set_values(self, start: int, end: int) -> None:
        """Update handles silently from authoritative node parameters."""
        new_start = self._clamp(start)
        new_end = max(new_start, self._clamp(end))
        if (new_start, new_end) == (self._start, self._end):
            return
        self._start, self._end = new_start, new_end
        self.update()

    def set_bounds(self, minimum: int, maximum: int) -> None:
        """Update the source bounds silently and retain the current range."""
        self._minimum = int(minimum)
        self._maximum = max(self._minimum, int(maximum))
        self._start = self._clamp(self._start)
        self._end = max(self._start, self._clamp(self._end))
        self.update()

    def _track_x(self) -> Tuple[float, float]:
        left = 7.0
        right = max(left + 1.0, float(self.width() - 7))
        return left, right

    def _value_to_x(self, value: int) -> float:
        left, right = self._track_x()
        span = self._maximum - self._minimum
        if span <= 0:
            return (left + right) / 2.0
        return left + (self._clamp(value) - self._minimum) / span * (right - left)

    def _x_to_value(self, x: float) -> int:
        left, right = self._track_x()
        ratio = max(0.0, min(1.0, (x - left) / max(1.0, right - left)))
        return self._clamp(round(self._minimum + ratio *
                                 (self._maximum - self._minimum)))

    def _set_from_x(self, x: float) -> None:
        value = self._x_to_value(x)
        if self._active_handle == "in":
            value = min(value, self._end)
            new_values = (value, self._end)
        elif self._active_handle == "out":
            value = max(value, self._start)
            new_values = (self._start, value)
        else:
            return
        if new_values == (self._start, self._end):
            return
        self._start, self._end = new_values
        self.update()
        self.range_changed.emit(*new_values)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        left, right = self._track_x()
        y = 10.0
        x_start = self._value_to_x(self._start)
        x_end = self._value_to_x(self._end)

        painter.setPen(QPen(QColor("#3b3b42"), 3))
        painter.drawLine(QPointF(left, y), QPointF(right, y))
        painter.setPen(QPen(QColor("#d9791c"), 3))
        painter.drawLine(QPointF(x_start, y), QPointF(x_end, y))

        painter.setPen(QPen(QColor("#aeb0b8"), 1))
        painter.setBrush(QColor("#aeb0b8"))
        painter.drawEllipse(QPointF(x_start, y), 4.5, 4.5)
        painter.drawEllipse(QPointF(x_end, y), 4.5, 4.5)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        x = float(event.position().x())
        in_dist = abs(x - self._value_to_x(self._start))
        out_dist = abs(x - self._value_to_x(self._end))
        if min(in_dist, out_dist) <= 10.0:
            self._active_handle = "in" if in_dist <= out_dist else "out"
        else:
            self._active_handle = "in" if x < self._value_to_x(self._start) \
                else "out"
        self._set_from_x(x)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._active_handle is not None:
            self._set_from_x(float(event.position().x()))
            event.accept()
            return
        event.ignore()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._active_handle = None
            event.accept()
            return
        event.ignore()


class TrimRangeEditor(QWidget):
    """Two-handle slider plus editable In/Out frame fields."""

    range_changed = pyqtSignal(int, int)

    def __init__(self, minimum: int, maximum: int, start: int, end: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._slider = TrimRangeSlider(minimum, maximum, start, end, self)
        value_min = int(minimum)
        value_max = max(value_min, int(maximum))
        self._in_spin = DragValue(
            "", value_min, value_max, int(start), decimals=0,
            compact=True)
        self._out_spin = DragValue(
            "", value_min, value_max, int(end), decimals=0,
            compact=True)
        for spin in (self._in_spin, self._out_spin):
            spin.setFixedValueWidth(55)

        self._in_label = QLabel(I18N.tr("node_props.in_label", "In"))
        self._out_label = QLabel(I18N.tr("node_props.out_label", "Out"))
        for label in (self._in_label, self._out_label):
            label.setAlignment(Qt.AlignmentFlag.AlignRight |
                               Qt.AlignmentFlag.AlignVCenter)
            label.setStyleSheet("color: #9b9ba2; font-size: 10px;")

        values_layout = QHBoxLayout()
        values_layout.setContentsMargins(0, 0, 0, 0)
        values_layout.setSpacing(4)
        values_layout.addStretch(1)
        values_layout.addWidget(self._in_label)
        values_layout.addWidget(self._in_spin)
        values_layout.addSpacing(8)
        values_layout.addWidget(self._out_label)
        values_layout.addWidget(self._out_spin)
        values_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._slider)
        layout.addLayout(values_layout)

        self._slider.range_changed.connect(self._on_slider_changed)
        self._in_spin.valueChanged.connect(self._on_in_changed)
        self._out_spin.valueChanged.connect(self._on_out_changed)
        self.set_values(start, end)

    def values(self) -> Tuple[int, int]:
        return self._slider.values()

    def set_values(self, start: int, end: int) -> None:
        """Update the slider and fields without emitting a graph edit."""
        start, end = self._normalise(start, end)
        self._slider.set_values(start, end)
        self._in_spin.blockSignals(True)
        self._out_spin.blockSignals(True)
        self._in_spin.setValue(start)
        self._out_spin.setValue(end)
        self._in_spin.blockSignals(False)
        self._out_spin.blockSignals(False)

    def set_bounds(self, minimum: int, maximum: int) -> None:
        self._slider.set_bounds(minimum, maximum)
        self._in_spin.setRange(minimum, maximum)
        self._out_spin.setRange(minimum, maximum)
        self.set_values(*self._slider.values())

    def _normalise(self, start: int, end: int) -> Tuple[int, int]:
        minimum = self._slider._minimum
        maximum = self._slider._maximum
        start = max(minimum, min(int(start), maximum))
        end = max(start, min(int(end), maximum))
        return start, end

    def _emit_values(self, start: int, end: int) -> None:
        start, end = self._normalise(start, end)
        self.set_values(start, end)
        self.range_changed.emit(start, end)

    def _on_slider_changed(self, start: int, end: int) -> None:
        self._in_spin.blockSignals(True)
        self._out_spin.blockSignals(True)
        self._in_spin.setValue(start)
        self._out_spin.setValue(end)
        self._in_spin.blockSignals(False)
        self._out_spin.blockSignals(False)
        self.range_changed.emit(start, end)

    def _on_in_changed(self, value: float) -> None:
        out_value = int(round(self._out_spin.value()))
        self._emit_values(min(int(round(value)), out_value), out_value)

    def _on_out_changed(self, value: float) -> None:
        in_value = int(round(self._in_spin.value()))
        self._emit_values(in_value, max(int(round(value)), in_value))


class _ParamLabel(QLabel):
    """A parameter-name label that emits ``doubleClicked`` — wired to reset the
    parameter(s) to their default value (replaces the old per-row ↺ button).

    An optional ``context_menu_builder`` (callable taking a global QPoint) powers
    the right-click "Separate / Combine X·Y" menu on X/Y rows."""

    doubleClicked = pyqtSignal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._context_menu_builder = None
        # B3-2: optional hook (node_id, param_name) -> None that offers
        # "Promote to Group" on numeric parameter rows when the page can
        # resolve an owning group.
        self._group_promote_hook = None
        # Right-click should fire contextMenuEvent (the default policy), not be
        # swallowed by the label.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    def set_context_menu_builder(self, builder) -> None:
        self._context_menu_builder = builder

    def set_group_promote_hook(self, hook) -> None:
        """Register the promote callback; presence enables the menu item."""
        self._group_promote_hook = hook

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._context_menu_builder is not None:
            # event.globalPos() already returns a QPoint (QPoint has no toPoint();
            # that's a QPointF method). Pass it straight to the menu builder.
            self._context_menu_builder(event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)


class _KeyframeToolButton(QToolButton):
    """Keyframe button with AE-style Alt-click expression shortcut."""

    alt_clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.AltModifier):
            self.alt_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


def group_xy_pairs(keys: List[str], param_defs: Optional[Dict] = None) -> List[Tuple]:
    """Group consecutive component parameters onto one row (Fusion-style).

    Walks ``keys`` in order and emits layout directives:
        ``('single', key)``                          — a standalone parameter row
        ``('pair', base, x_key, y_key)``             — an X/Y row (2 components)
        ``('triple', base, x_key, y_key, z_key)``    — an X/Y/Z row (3 components)
        ``('rgb', base, r_key, g_key, b_key)``       — an R/G/B color row (3 components)

    Matching rules (all variants require consecutive keys, same float ptype, same
    ``group`` so a row never crosses a group boundary):
        - ``<base>_x`` + ``<base>_y`` + ``<base>_z`` → triple
        - ``<base>_x`` + ``<base>_y`` (no ``_z``)    → pair   (backward compatible)
        - ``<base>_r`` + ``<base>_g`` + ``<base>_b`` → rgb

    Pure function — unit-testable without a panel.
    """
    param_defs = param_defs or {}
    out: List[Tuple] = []
    i = 0
    n = len(keys)

    def _meta(key: str):
        pd = param_defs.get(key)
        ptype = getattr(pd, "ptype", "float") if pd else "float"
        group = getattr(pd, "group", None) if pd else None
        return ptype, group

    while i < n:
        k = keys[i]
        grouped = False

        # ── X/Y/Z triple ──
        if k.endswith("_x") and i + 2 < n:
            base = k[:-2]
            k_y = keys[i + 1]
            k_z = keys[i + 2]
            if k_y == base + "_y" and k_z == base + "_z":
                t1, g1 = _meta(k)
                t2, g2 = _meta(k_y)
                t3, g3 = _meta(k_z)
                if (t1 == "float" and t2 == "float" and t3 == "float"
                        and g1 == g2 == g3):
                    out.append(("triple", base, k, k_y, k_z))
                    i += 3
                    grouped = True

        # ── X/Y pair ──
        # 到这里说明 triple 未成功（_z 不存在，或 _z 类型/group 不匹配 triple）。
        # 若 _x/_y 匹配则 pair；_z（若存在）留到下一轮成为 single。
        if not grouped and k.endswith("_x") and i + 1 < n:
            base = k[:-2]
            nxt = keys[i + 1]
            if nxt == base + "_y":
                t1, g1 = _meta(k)
                t2, g2 = _meta(nxt)
                if t1 == "float" and t2 == "float" and g1 == g2:
                    out.append(("pair", base, k, nxt))
                    i += 2
                    grouped = True

        # ── R/G/B color triple ──
        if not grouped and k.endswith("_r") and i + 2 < n:
            base = k[:-2]
            k_g = keys[i + 1]
            k_b = keys[i + 2]
            if k_g == base + "_g" and k_b == base + "_b":
                t1, g1 = _meta(k)
                t2, g2 = _meta(k_g)
                t3, g3 = _meta(k_b)
                # RGB 通道都是 float 0-1，且同 group
                if (t1 == "float" and t2 == "float" and t3 == "float"
                        and g1 == g2 == g3):
                    out.append(("rgb", base, k, k_g, k_b))
                    i += 3
                    grouped = True

        if not grouped:
            out.append(("single", k))
            i += 1
    return out


class CurveWidget(QWidget):
    """
    Curve editing widget for 1D curves (e.g., levels, tone mapping).

    Allows adding, moving, and removing control points.
    Emits curveChanged signal when the curve is modified.
    """

    curveChanged = pyqtSignal(list)  # List[Tuple[float, float]]

    # Point radius for hit testing and display
    POINT_RADIUS = 6
    HIT_RADIUS = 12

    def __init__(self, curve_data: List[Tuple[float, float]] = None, parent=None) -> None:
        """
        Initialize the curve widget.

        Args:
            curve_data: Initial control points [(x, y), ...]. Default is linear [(0, 0), (1, 1)].
            parent: Parent widget.
        """
        super().__init__(parent)
        self._points: List[Tuple[float, float]] = []
        self._dragging_index: int = -1
        self._hover_index: int = -1

        # Set default curve or use provided data
        if curve_data:
            self._points = sorted([tuple(p) for p in curve_data])
        else:
            self._points = [(0.0, 0.0), (1.0, 1.0)]

        # Widget settings
        self.setMinimumSize(200, 150)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def setCurve(self, points: List[Tuple[float, float]]) -> None:
        """Set the curve control points."""
        if points:
            self._points = sorted([tuple(p) for p in points])
            self.update()

    def curve(self) -> List[Tuple[float, float]]:
        """Get the current curve control points."""
        return list(self._points)

    def _to_widget(self, point: Tuple[float, float]) -> QPointF:
        """Convert normalized coordinates (0-1) to widget coordinates."""
        w = self.width() - 2 * self.POINT_RADIUS
        h = self.height() - 2 * self.POINT_RADIUS
        x = self.POINT_RADIUS + point[0] * w
        # Flip Y axis (0 at bottom, 1 at top)
        y = self.POINT_RADIUS + (1.0 - point[1]) * h
        return QPointF(x, y)

    def _from_widget(self, pos: QPointF) -> Tuple[float, float]:
        """Convert widget coordinates to normalized coordinates (0-1)."""
        w = self.width() - 2 * self.POINT_RADIUS
        h = self.height() - 2 * self.POINT_RADIUS
        x = (pos.x() - self.POINT_RADIUS) / w if w > 0 else 0.0
        y = 1.0 - (pos.y() - self.POINT_RADIUS) / h if h > 0 else 0.0
        return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))

    def _find_point_at(self, pos: QPointF) -> int:
        """Find the point index at the given position, or -1 if none."""
        for i, point in enumerate(self._points):
            wp = self._to_widget(point)
            if (pos - wp).manhattanLength() < self.HIT_RADIUS:
                return i
        return -1

    def paintEvent(self, event) -> None:
        """Paint the curve widget."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        rect = self.rect()
        painter.fillRect(rect, QColor("#2a2a2a"))

        # Draw grid
        painter.setPen(QPen(QColor("#444444"), 1))
        grid_pen = QPen(QColor("#444444"), 1, Qt.PenStyle.DotLine)
        painter.setPen(grid_pen)

        # Vertical grid lines
        for i in range(1, 10):
            x = rect.left() + (rect.width() * i / 10)
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())

        # Horizontal grid lines
        for i in range(1, 10):
            y = rect.top() + (rect.height() * i / 10)
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        # Draw diagonal reference line (linear curve)
        painter.setPen(QPen(QColor("#555555"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(
            int(self.POINT_RADIUS), int(self.height() - self.POINT_RADIUS),
            int(self.width() - self.POINT_RADIUS), int(self.POINT_RADIUS)
        )

        # Draw curve using linear interpolation
        if len(self._points) >= 2:
            path = QPainterPath()
            first_point = self._to_widget(self._points[0])
            path.moveTo(first_point)

            for point in self._points[1:]:
                widget_point = self._to_widget(point)
                path.lineTo(widget_point)

            painter.setPen(QPen(QColor("#cc6600"), 2))
            painter.drawPath(path)

        # Draw control points
        for i, point in enumerate(self._points):
            wp = self._to_widget(point)

            # Highlight hovered or dragged point
            if i == self._dragging_index or i == self._hover_index:
                painter.setBrush(QBrush(QColor("#ff8800")))
                painter.setPen(QPen(QColor("#ffffff"), 2))
            else:
                painter.setBrush(QBrush(QColor("#cc6600")))
                painter.setPen(QPen(QColor("#ffffff"), 1))

            painter.drawEllipse(wp, self.POINT_RADIUS, self.POINT_RADIUS)

        # Draw coordinate label for hovered point
        if self._hover_index >= 0:
            point = self._points[self._hover_index]
            label = f"({point[0]:.2f}, {point[1]:.2f})"
            painter.setPen(QPen(QColor("#ffffff")))
            painter.setFont(QFont("monospace", 8))
            wp = self._to_widget(point)
            painter.drawText(int(wp.x() + 10), int(wp.y() - 5), label)

    def mousePressEvent(self, event) -> None:
        """Handle mouse press - add point or start drag."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            index = self._find_point_at(pos)

            if index >= 0:
                # Start dragging existing point
                self._dragging_index = index
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            else:
                # Add new point
                new_point = self._from_widget(pos)

                # Find insertion position (keep sorted by x)
                insert_index = 0
                for i, p in enumerate(self._points):
                    if new_point[0] > p[0]:
                        insert_index = i + 1

                self._points.insert(insert_index, new_point)
                self._dragging_index = insert_index
                self.update()
                self._emit_curve_changed()

    def mouseMoveEvent(self, event) -> None:
        """Handle mouse move - drag point or update hover."""
        pos = event.position()

        if self._dragging_index >= 0:
            # Dragging a point
            new_point = self._from_widget(pos)

            # Constrain x position to not cross neighbors
            min_x = 0.0 if self._dragging_index == 0 else self._points[self._dragging_index - 1][0] + 0.001
            max_x = 1.0 if self._dragging_index == len(self._points) - 1 else self._points[self._dragging_index + 1][0] - 0.001

            new_x = max(min_x, min(max_x, new_point[0]))
            new_y = max(0.0, min(1.0, new_point[1]))

            self._points[self._dragging_index] = (new_x, new_y)
            self.update()
            self._emit_curve_changed()
        else:
            # Update hover state
            new_hover = self._find_point_at(pos)
            if new_hover != self._hover_index:
                self._hover_index = new_hover
                self.setCursor(
                    Qt.CursorShape.ClosedHandCursor if new_hover >= 0
                    else Qt.CursorShape.CrossCursor
                )
                self.update()

    def mouseReleaseEvent(self, event) -> None:
        """Handle mouse release - finish drag."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_index = -1
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event) -> None:
        """Handle double click - remove point."""
        pos = event.position()
        index = self._find_point_at(pos)

        # Don't remove first or last point, or if there are only 2 points
        if index > 0 and index < len(self._points) - 1 and len(self._points) > 2:
            self._points.pop(index)
            self._hover_index = -1
            self.update()
            self._emit_curve_changed()

    def leaveEvent(self, event) -> None:
        """Handle mouse leave - reset hover state."""
        self._hover_index = -1
        self.update()

    def _emit_curve_changed(self) -> None:
        """Emit the curveChanged signal."""
        self.curveChanged.emit(self.curve())


class _GradientPreviewWidget(QWidget):
    """Compact gradient preview bar shown between two color swatches.

    Paints a linear gradient from *color1* to *color2* across the widget width.
    Supports horizontal, vertical, and diagonal gradient directions.
    """

    def __init__(self, color1: str = "#000000", color2: str = "#ffffff",
                 direction: str = "horizontal", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color1 = QColor(color1)
        self._color2 = QColor(color2)
        self._direction = direction
        self.setFixedHeight(22)
        self.setMinimumWidth(60)

    def set_colors(self, color1: str, color2: str) -> None:
        self._color1 = QColor(color1)
        self._color2 = QColor(color2)
        self.update()

    def set_direction(self, direction: str) -> None:
        self._direction = direction
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 3  # corner radius

        # Build gradient based on direction
        if self._direction == "vertical":
            grad = QLinearGradient(0, 0, 0, h)
        elif self._direction == "diagonal_tl_br":
            grad = QLinearGradient(0, 0, w, h)
        elif self._direction == "diagonal_tr_bl":
            grad = QLinearGradient(w, 0, 0, h)
        else:  # horizontal
            grad = QLinearGradient(0, 0, w, 0)

        grad.setColorAt(0.0, self._color1)
        grad.setColorAt(1.0, self._color2)

        # Draw rounded rect
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), r, r)
        p.setClipPath(path)
        p.fillRect(QRectF(0, 0, w, h), QBrush(grad))

        # Border
        p.setClipping(False)
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r, r)
        p.end()


class NodeSectionWidget(QFrame):
    """
    Collapsible node section with toolbar, for Fusion-style properties panel.

    Each section displays one node's parameters with a header toolbar:
    - Enable/disable toggle
    - Node color indicator & picker
    - Save preset / Pin / Lock / Reset buttons
    """

    # Signals
    parameter_changed = pyqtSignal(str, str, object)     # node_id, param_name, value
    node_enabled_toggled = pyqtSignal(str, bool)         # node_id, enabled
    node_color_changed = pyqtSignal(str, str)             # node_id, hex_color
    node_reset_requested = pyqtSignal(str)                # node_id
    pin_toggled = pyqtSignal(str, bool)                   # node_id, is_pinned
    render_output_requested = pyqtSignal(str)             # node_id (Output node render)
    tracker_analyze_requested = pyqtSignal(str)           # node_id (A1 Analyze)
    tracker_bake_requested = pyqtSignal(str, str)         # node_id, "transform"|"cornerpin"
    tracker_cancel_requested = pyqtSignal(str)            # node_id
    tracker_import_color_requested = pyqtSignal(str)      # node_id (A1 import)
    tracker_stabilize_requested = pyqtSignal(str)         # node_id, A2 stabilize bake
    view_mode_toggle_requested = pyqtSignal(str)          # "2d"|"3d" (Scene3D 视图开关)
    look_through_camera_requested = pyqtSignal(str)        # Camera3D node_id
    camera_activation_toggled = pyqtSignal(str, bool)      # Camera3D node_id, active

    # Default colors for node categories
    DEFAULT_NODE_COLOR = "#666666"

    _BTN_STYLE = TOOLBAR_FLAT_BTN_STYLE

    _ICON_SIZE = 14  # icon px size for header buttons
    _BTN_SIZE = 24   # button widget size

    def __init__(self, node_id: str, node_type: str, node_title: str,
                 parameters: Dict[str, Any], param_options: Optional[Dict] = None,
                 param_defs: Optional[Dict] = None, enabled: bool = True,
                 node_color: str = "#666666", parent_panel=None) -> None:
        super().__init__()
        self.node_id = node_id
        self.node_type = node_type
        self.node_title = node_title
        self.parameters = dict(parameters) if parameters else {}
        self.param_options = param_options or {}
        self.param_defs = param_defs or {}
        self.enabled = enabled
        self.node_color = node_color
        self._parent_panel = parent_panel
        self._is_collapsed = False
        self._is_pinned = False
        self._is_locked = False
        self._node_read_only = False
        self._camera_active = False
        self._search_filter = ""
        # Teardown guard: once prepare_for_deletion() has run, the section's
        # C++ widget is destroyed synchronously and all child widgets are gone.
        # Subsequent calls (from overlapping removal paths — e.g. node-removed
        # + selection-changed firing in the same event loop) must be no-ops,
        # otherwise they touch freed C++ objects and segfault.
        self._is_torn_down: bool = False
        self._param_widgets: Dict[str, Any] = {}
        # Sliders on single numeric rows, toggled visible by available width.
        self._slidable_sliders: List[Any] = []
        # Sliders on XY-pair rows, toggled visible separately from single rows.
        self._pair_sliders: List[Any] = []
        # Sliders on XYZ-triple rows（3 个 slider，需更宽面板才显示）。
        self._triple_sliders: List[Any] = []
        # Parameters hidden by mode-dependent visibility (Constant node).
        self._hidden_params: set = set()
        # Parameters handled by custom UI builder (skip in generic loop).
        self._skip_params: set = set()

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(NODE_SECTION_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar ──
        self._header = QWidget()
        self._header.setFixedHeight(28)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(6, 0, 2, 0)
        header_layout.setSpacing(2)

        # Collapse toggle
        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("▼")
        self._collapse_btn.setFixedSize(16, 16)
        self._collapse_btn.setStyleSheet(NODE_COLLAPSE_BTN_STYLE)
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        header_layout.addWidget(self._collapse_btn)

        # Enable toggle (pill switch)
        self._enable_btn = ToggleSwitch(checked=enabled)
        self._enable_btn.toggled.connect(self._on_enable_toggled)
        header_layout.addWidget(self._enable_btn)

        # Color indicator
        self._color_indicator = QToolButton()
        self._color_indicator.setFixedSize(12, 12)
        self._update_color_indicator_style()
        self._color_indicator.setToolTip(
            I18N.tr("node_props.change_node_color", "Change node color"))
        self._color_indicator.clicked.connect(self._pick_color)
        header_layout.addWidget(self._color_indicator)

        # Title
        self._title_label = QLabel(node_title)
        self._title_label.setStyleSheet(NODE_TITLE_LABEL_STYLE)
        header_layout.addWidget(self._title_label)

        header_layout.addStretch()

        # Now that the title/collapse widgets exist, apply the node-color-aware
        # header gradient + adaptive text color in one pass.
        self._update_header_style()

        # ── Toolbar buttons (timeline toolbar style) ──
        # Icons render white so they stay legible on the node-color gradient
        # header (which can be any hue / luminance). Pinned / locked states
        # still swap to ORANGE for emphasis.
        # Camera view activation (Camera3D only): C4D-style "look through this
        # camera" toggle. It sits immediately left of Save Preset.
        self._camera_active_btn = None
        if node_type == "camera3d":
            self._camera_active_btn = QToolButton()
            self._camera_active_btn.setIcon(get_icon('fa5s.video', IconColors.WHITE))
            self._camera_active_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
            self._camera_active_btn.setToolTip(
                I18N.tr("node_props.activate_camera_view", "Activate camera view"))
            self._camera_active_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
            self._camera_active_btn.setCheckable(True)
            self._camera_active_btn.setStyleSheet(self._BTN_STYLE)
            self._camera_active_btn.clicked.connect(self._on_camera_activation_toggled)
            header_layout.addWidget(self._camera_active_btn)

        # Save preset
        self._save_btn = QToolButton()
        self._save_btn.setIcon(get_icon('fa5s.save', IconColors.WHITE))
        self._save_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._save_btn.setToolTip(I18N.tr("node_props.save_preset", "Save preset"))
        self._save_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
        self._save_btn.setStyleSheet(self._BTN_STYLE)
        # 此前该按钮没有 clicked 处理器——点击毫无反应。
        self._save_btn.clicked.connect(self._on_save_preset)
        header_layout.addWidget(self._save_btn)

        # Pin
        self._pin_btn = QToolButton()
        self._pin_btn.setIcon(get_icon('fa5s.thumbtack', IconColors.WHITE))
        self._pin_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._pin_btn.setToolTip(
            I18N.tr("node_props.pin_section", "Pin section (keep visible)"))
        self._pin_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
        self._pin_btn.setCheckable(True)
        self._pin_btn.setStyleSheet(self._BTN_STYLE)
        self._pin_btn.clicked.connect(self._on_pin_toggled)
        header_layout.addWidget(self._pin_btn)

        # Lock
        self._lock_btn = QToolButton()
        self._lock_btn.setIcon(get_icon('fa5s.lock', IconColors.WHITE))
        self._lock_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._lock_btn.setToolTip(
            I18N.tr("node_props.lock_parameters", "Lock parameters"))
        self._lock_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
        self._lock_btn.setCheckable(True)
        self._lock_btn.setStyleSheet(self._BTN_STYLE)
        self._lock_btn.clicked.connect(self._on_lock_toggled)
        header_layout.addWidget(self._lock_btn)

        # Reset
        self._reset_btn = QToolButton()
        self._reset_btn.setIcon(get_icon('fa5s.undo', IconColors.WHITE))
        self._reset_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._reset_btn.setToolTip(
            I18N.tr("node_props.reset_all_parameters", "Reset all parameters"))
        self._reset_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
        self._reset_btn.setStyleSheet(self._BTN_STYLE)
        self._reset_btn.clicked.connect(self._on_reset)
        header_layout.addWidget(self._reset_btn)

        # Help — opens the node's usage document (bilingual, follows the app
        # language). Docs live in src/shared/node_help/docs/<node_type>.md;
        # nodes without a document show a friendly "not available" notice.
        self._help_btn = QToolButton()
        self._help_btn.setIcon(get_icon('fa5s.info-circle', IconColors.WHITE))
        self._help_btn.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._help_btn.setToolTip(tr(
            "composite.node_help.tooltip", "Node usage help"))
        self._help_btn.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
        self._help_btn.setStyleSheet(self._BTN_STYLE)
        self._help_btn.clicked.connect(self._on_show_help)
        header_layout.addWidget(self._help_btn)

        layout.addWidget(self._header)

        # ── Content area ──
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet(NODE_CONTENT_STYLE)
        self._content_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(8, 6, 8, 6)
        self._content_layout.setSpacing(4)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._build_parameters()
        layout.addWidget(self._content_widget)

    # ── Style helpers ──

    def _update_header_style(self) -> None:
        """Render header background as a vertical gradient mirroring the canvas
        node header (lighter(130) → base color), and pick a readable text /
        chrome color by perceived luminance so light node colors (e.g. yellow,
        white) keep the title legible."""
        base = QColor(self.node_color)
        if not base.isValid():
            base = QColor(self.DEFAULT_NODE_COLOR)
        top_color = base.lighter(130)

        # Relative luminance (Rec. 709 weights) → pick black or white text.
        r, g, b = base.red() / 255.0, base.green() / 255.0, base.blue() / 255.0
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        text_color = "#000000" if luminance > 0.55 else "#FFFFFF"
        # Subtle separator line fades into the body; keep semi-transparent.
        self._header_text_color = text_color
        self._header.setStyleSheet(
            f"background-color: qlineargradient("
            f"x1:0, y1:0, x2:0, y2:1, stop:0 {top_color.name()}, stop:1 {base.name()});"
            f" border-bottom: 1px solid rgba(0,0,0,60);"
        )
        # Reapply dependent styles so the title and collapse glyph track the
        # current background luminance instead of staying a fixed gray.
        # During __init__ these widgets don't exist yet — the initial call only
        # sets the background; subsequent calls (set_node_color / live preview)
        # refresh the chrome too.
        title_label = getattr(self, "_title_label", None)
        if title_label is not None:
            title_label.setStyleSheet(
                f"color: {text_color}; font-weight: bold; font-size: 11px; border: none;"
            )
        collapse_btn = getattr(self, "_collapse_btn", None)
        if collapse_btn is not None:
            collapse_btn.setStyleSheet(
                f"QToolButton {{ background: transparent; border: none;"
                f" color: {text_color}; font-size: 9px; padding: 0; }}"
                f"QToolButton:hover {{ color: {text_color}; }}"
            )

    def _update_enable_style(self) -> None:
        # ToggleSwitch handles its own painting, no stylesheet needed
        pass

    def _update_color_indicator_style(self) -> None:
        self._color_indicator.setStyleSheet(
            f"QToolButton {{ background-color: {self.node_color}; border: 1px solid rgba(255,255,255,80);"
            f" border-radius: 2px; }}"
            f"QToolButton:hover {{ border-color: #fff; }}"
        )

    def set_camera_active(self, active: bool) -> None:
        """Update Camera3D header activation state without emitting."""
        self._camera_active = bool(active)
        btn = getattr(self, "_camera_active_btn", None)
        if btn is None:
            return
        btn.blockSignals(True)
        btn.setChecked(self._camera_active)
        btn.setIcon(get_icon(
            'fa5s.video',
            IconColors.ORANGE if self._camera_active else IconColors.WHITE))
        btn.setToolTip(
            "Deactivate camera view" if self._camera_active else "Activate camera view")
        btn.blockSignals(False)

    def _on_camera_activation_toggled(self, checked: bool) -> None:
        self.set_camera_active(bool(checked))
        self.camera_activation_toggled.emit(self.node_id, bool(checked))

    # ── Parameter building ──

    def prepare_for_deletion(self) -> None:
        """Safely tear down this section before deletion. Idempotent.

        The important rule is: do not wildcard-disconnect child widgets here.
        Camera3D sections contain many custom controls (DragValue,
        ToposClickableSlider, QLineEdit). During node deletion Qt may already
        have begun destroying some of those children; calling
        ``child.disconnect()`` at that point produces "disconnects from
        destroyed signal" warnings and can segfault. Hiding, disabling,
        blocking signals, detaching, and scheduling the section for deletion is
        enough; Qt will disconnect signals automatically when the QObject dies.
        """
        # Idempotency guard: the second (overlapping) teardown pass must be a
        # no-op, not a re-touch of an already-detached/deleted C++ widget.
        if self._is_torn_down:
            return
        self._is_torn_down = True

        # Stop new UI events/queued control emissions without touching signal
        # connection lists of children that may already be in destruction.
        try:
            self.hide()
            self.setEnabled(False)
            self.blockSignals(True)
        except RuntimeError:
            return
        for child in self.findChildren(QWidget):
            try:
                child.blockSignals(True)
                child.setEnabled(False)
                child.hide()
            except RuntimeError:
                pass
        # Detach from the layout/parent so Qt stops painting this subtree now,
        # then schedule the C++ destruction.
        self.setParent(None)
        self.deleteLater()

    def _build_parameters(self):
        """Build parameter controls, respecting search filter."""
        # Clear existing controls. Detach them from the layout immediately so
        # Qt stops painting this subtree, but do not wildcard-disconnect child
        # widgets: some custom controls may already be in destruction during
        # rapid rebuild/delete paths, and touching their connection lists can
        # segfault in Qt.
        while self._content_layout.count() > 0:
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.hide()
                    w.setEnabled(False)
                    w.blockSignals(True)
                except RuntimeError:
                    pass
                w.setParent(None)
                w.deleteLater()

        self._param_widgets.clear()
        self._slidable_sliders = []
        self._pair_sliders = []
        self._triple_sliders = []
        self._skip_params.clear()
        self._hidden_params.clear()

        if not self.parameters:
            return

        # Dispatch to node-type-specific custom UI builders.
        if self.node_type == "constant":
            self._parent_panel._build_constant_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "blur":
            self._parent_panel._build_blur_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "keyer":
            self._parent_panel._build_keyer_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "text":
            self._parent_panel._build_text_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "output":
            self._parent_panel._build_output_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        # Scene3D：顶部一个 2D/3D 预览模式开关（纯视图控件），然后正常参数行。
        if self.node_type == "scene3d":
            self._parent_panel._build_scene3d_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "camera3d":
            self._parent_panel._build_camera3d_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "video_frame":
            self._parent_panel._build_video_frame_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return
        if self.node_type == "tracker":
            self._parent_panel._build_tracker_ui(self, self._content_layout)
            self._apply_slider_visibility()
            return

        search = getattr(self, '_search_filter', '').lower()

        def matches_search(key: str) -> bool:
            if not search:
                return True
            return search in key.lower()

        # Classify into groups (search-filtered). When no groups exist, every key
        # lands in ungrouped, so the flat path below handles that too.
        has_groups = False
        groups: Dict[str, List[str]] = {}
        ungrouped_params = []

        for key in self.parameters.keys():
            if key in self._hidden_params or key in self._skip_params:
                continue
            pdef = self.param_defs.get(key)
            if pdef and getattr(pdef, 'hidden', False):
                continue
            if not matches_search(key):
                continue
            group_name = getattr(pdef, 'group', None) if pdef else None
            if group_name:
                has_groups = True
                if group_name not in groups:
                    groups[group_name] = []
                groups[group_name].append(key)
            else:
                ungrouped_params.append(key)

        # Compute widest label so every _ParamLabel shares one minimum width,
        # keeping DragValue / ComboBox left-edges vertically aligned across all
        # row types (single, XY-pair, combo).
        all_keys = list(ungrouped_params)
        for pk in groups.values():
            all_keys.extend(pk)
        fm = QFontMetrics(QFont("", 10))
        widest = 36
        for directive in group_xy_pairs(all_keys, self.param_defs):
            if directive[0] == 'pair':
                name = directive[1].replace("_", " ").title()
            else:
                name = directive[1].replace("_", " ").title()
            widest = max(widest, fm.horizontalAdvance(name) + 12)
        self._parent_panel._label_min_width = widest

        # Ungrouped rows (or all rows when there are no groups).
        self._build_directives_into(self._content_layout, ungrouped_params)

        if has_groups:
            for group_name, param_keys in groups.items():
                group_box = QGroupBox(self._parent_panel._group_display_name(group_name))
                group_box.setStyleSheet(NODE_GROUP_BOX_STYLE)
                group_layout = QVBoxLayout(group_box)
                group_layout.setContentsMargins(6, 4, 6, 4)
                group_layout.setSpacing(3)
                self._build_directives_into(group_layout, param_keys)
                self._content_layout.addWidget(group_box)

        self._apply_slider_visibility()

    def _build_directives_into(self, layout, keys: List[str]) -> None:
        """Build rows for ``keys`` into ``layout``, pairing ``_x``/``_y`` onto one
        Fusion-style row. Single-value rows register sliders on
        ``self._slidable_sliders``; XY-pair rows on ``self._pair_sliders``.
        Each list is toggled independently by available width.

        Honors the per-node "Separate Dimensions" state: a pair whose base is in
        the panel's split set is rendered as two single rows instead (each gets a
        "Combine" right-click menu on its label)."""
        split = self._parent_panel._split_dims.get(self.node_id, set())
        for directive in group_xy_pairs(keys, self.param_defs):
            if directive[0] == 'triple':
                # X/Y/Z 三合一（紧凑：变换类参数 _x/_y/_z 合并到一行）
                _, base, x_key, y_key, z_key = directive
                if base in split:
                    # Separated: 三个 single 行，各自可 re-combine
                    for k in (x_key, y_key, z_key):
                        row = self._parent_panel._create_param_row_for_section(
                            self.node_id, k, self.parameters[k],
                            self.param_options, self.param_defs, self,
                            slider_holder=self._slidable_sliders,
                            label_menu=('combine', base),
                        )
                        layout.addWidget(row)
                else:
                    row = self._parent_panel._create_xyz_param_row_for_section(
                        self.node_id, base, x_key, y_key, z_key,
                        self.parameters[x_key], self.parameters[y_key],
                        self.parameters[z_key],
                        self.param_options, self.param_defs, self,
                        slider_holder=self._triple_sliders,
                        label_menu=('separate', base),
                    )
                    layout.addWidget(row)
            elif directive[0] == 'rgb':
                # R/G/B 颜色三合一（紧凑：色块 + 3 个 DragValue）
                _, base, r_key, g_key, b_key = directive
                if base in split:
                    for k in (r_key, g_key, b_key):
                        row = self._parent_panel._create_param_row_for_section(
                            self.node_id, k, self.parameters[k],
                            self.param_options, self.param_defs, self,
                            slider_holder=self._slidable_sliders,
                            label_menu=('combine', base),
                        )
                        layout.addWidget(row)
                else:
                    row = self._parent_panel._create_rgb_param_row_for_section(
                        self.node_id, base, r_key, g_key, b_key,
                        self.parameters[r_key], self.parameters[g_key],
                        self.parameters[b_key],
                        self.param_options, self.param_defs, self,
                    )
                    layout.addWidget(row)
            elif directive[0] == 'pair':
                _, base, x_key, y_key = directive
                if base in split:
                    # Separated: two single rows, each re-combinable via its label.
                    for k in (x_key, y_key):
                        row = self._parent_panel._create_param_row_for_section(
                            self.node_id, k, self.parameters[k],
                            self.param_options, self.param_defs, self,
                            slider_holder=self._slidable_sliders,
                            label_menu=('combine', base),
                        )
                        layout.addWidget(row)
                else:
                    row = self._parent_panel._create_xy_param_row_for_section(
                        self.node_id, base, x_key, y_key,
                        self.parameters[x_key], self.parameters[y_key],
                        self.param_options, self.param_defs, self,
                        slider_holder=self._pair_sliders,
                        label_menu=('separate', base),
                    )
                    layout.addWidget(row)
            else:
                key = directive[1]
                row = self._parent_panel._create_param_row_for_section(
                    self.node_id, key, self.parameters[key],
                    self.param_options, self.param_defs, self,
                    slider_holder=self._slidable_sliders,
                )
                layout.addWidget(row)

    def _apply_slider_visibility(self) -> None:
        """Single-value sliders always show; XY-pair / XYZ-triple sliders only
        when the panel is wide enough. Triple rows need more room (3 sliders)."""
        # Single-value row sliders: always visible.
        for slider in self._slidable_sliders:
            slider.setVisible(True)
        # XY-pair row sliders: visible only when panel has room.
        show_pair = self.width() >= _SLIDER_SHOW_WIDTH
        for slider in self._pair_sliders:
            slider.setVisible(show_pair)
        # XYZ-triple row sliders: 需要更宽面板（3 个 slider），空间不足优先隐藏。
        show_triple = self.width() >= _SLIDER_SHOW_WIDTH_TRIPLE
        for slider in self._triple_sliders:
            slider.setVisible(show_triple)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_slider_visibility()

    # ── Public API ──

    def update_parameters(self, parameters: Dict[str, Any]) -> None:
        """Update parameter values, rebuilding if structure changed."""
        # No-op if this section is already torn down — a refresh signal may
        # arrive after the owning node was deleted and the section scheduled
        # for deletion. Touching the freed child widgets would segfault.
        if self._is_torn_down:
            return
        new_keys = set(parameters.keys()) if parameters else set()
        old_keys = set(self.parameters.keys())
        self.parameters = dict(parameters) if parameters else {}

        if new_keys != old_keys:
            self._build_parameters()
        else:
            # Incremental update
            for key, value in self.parameters.items():
                if key in self._param_widgets:
                    self._parent_panel._update_section_param_value(key, value, self._param_widgets)

    def set_enabled(self, enabled: bool) -> None:
        """Update enabled state without re-emitting signal."""
        if self._is_torn_down:
            return
        self.enabled = enabled
        self._enable_btn.blockSignals(True)
        self._enable_btn.setChecked(enabled)
        self._enable_btn.blockSignals(False)
        self._enable_btn.setEnabled(not self._node_read_only)
        self._update_enable_style()

    def set_node_color(self, color: str) -> None:
        """Update node color without re-emitting signal."""
        if self._is_torn_down:
            return
        self.node_color = color
        self._update_header_style()
        self._update_color_indicator_style()

    def set_read_only(self, read_only: bool) -> None:
        """Mirror the graph's persistent node lock in the properties UI."""
        if self._is_torn_down:
            return
        self._node_read_only = bool(read_only)
        self._content_widget.setEnabled(
            not (self._is_locked or self._node_read_only))
        self._enable_btn.setEnabled(not self._node_read_only)
        self._color_indicator.setEnabled(not self._node_read_only)
        self._reset_btn.setEnabled(not self._node_read_only)
        self.setToolTip(
            "Node is locked. Unlock it in the graph to edit parameters."
            if self._node_read_only else "")

    def set_search_filter(self, filter_text: str) -> None:
        """Filter parameters by search text and rebuild display."""
        self._search_filter = filter_text
        self._build_parameters()

    def is_pinned(self) -> bool:
        return self._is_pinned

    def is_locked(self) -> bool:
        return self._is_locked

    # ── Slot handlers ──

    def _on_enable_toggled(self, checked: bool) -> None:
        self.enabled = checked
        self._enable_btn.setChecked(checked)
        self.node_enabled_toggled.emit(self.node_id, self.enabled)

    def toggle_collapse(self) -> None:
        self._is_collapsed = not self._is_collapsed
        self._content_widget.setVisible(not self._is_collapsed)
        self._collapse_btn.setText("▶" if self._is_collapsed else "▼")

    def _on_pin_toggled(self) -> None:
        self._is_pinned = self._pin_btn.isChecked()
        color = IconColors.ORANGE if self._is_pinned else IconColors.WHITE
        self._pin_btn.setIcon(get_icon('fa5s.thumbtack', color))
        self.pin_toggled.emit(self.node_id, self._is_pinned)

    def _on_lock_toggled(self) -> None:
        self._is_locked = self._lock_btn.isChecked()
        color = IconColors.ORANGE if self._is_locked else IconColors.WHITE
        self._lock_btn.setIcon(get_icon('fa5s.lock', color))
        self._content_widget.setEnabled(
            not (self._is_locked or self._node_read_only))

    def _pick_color(self) -> None:
        # Use the project-wide CompactColorStudio picker (swatches, eyedropper,
        # hex/RGB/HSL/Lab, history) instead of the bare Qt QColorDialog, while
        # preserving the live-preview behavior the old dialog had.
        from src.shared.color import get_color

        current = QColor(self.node_color)

        def _on_live_header(color: QColor) -> None:
            hex_color = color.name()
            self.node_color = hex_color
            self._update_header_style()
            self._update_color_indicator_style()
            self.node_color_changed.emit(self.node_id, hex_color)

        result = get_color(parent=self, initial=current, on_live=_on_live_header)
        if result is not None:
            hex_color = result.name()
            self.node_color = hex_color
            self._update_header_style()
            self._update_color_indicator_style()
            self.node_color_changed.emit(self.node_id, hex_color)

    def _on_reset(self) -> None:
        """Reset all parameters to defaults."""
        for key in list(self.parameters.keys()):
            pdef = self.param_defs.get(key)
            if pdef and pdef.default is not None:
                self.parameters[key] = pdef.default
                self.parameter_changed.emit(self.node_id, key, pdef.default)
        self._build_parameters()

    def _on_show_help(self) -> None:
        """Open the node's usage document in a help dialog.

        The document language follows the app locale (see src/shared/node_help).
        Nodes without a document yet get a friendly notice instead.
        """
        from PyQt6.QtWidgets import QMessageBox
        from src.shared.i18n.i18n import I18N
        from src.shared.node_help import has_node_help

        if not has_node_help(self.node_type):
            QMessageBox.information(
                self,
                I18N.tr("composite.node_help.window_title",
                        "{title} — Help").format(title=self.node_title),
                I18N.tr("composite.node_help.unavailable",
                        "No usage help is available for this node yet."),
            )
            return

        from src.shared.node_help.dialog import NodeHelpDialog
        # The dialog loads its own document and offers a language switcher
        # (initial language follows the app locale).
        dialog = NodeHelpDialog(self.node_type, self.node_title, parent=self)
        dialog.exec()

    def _collect_current_values(self) -> Dict[str, Any]:
        """Read the live widget values back into a parameter snapshot.

        Well-defined widget kinds are read from their control; anything more
        complex (curve/hdri/custom) falls back to the last synced
        ``self.parameters`` value so a preset never invents data.

        AUD-10706: widgets of *animated* parameters show the playhead sample
        (sync_animated_values), not the base value — for those keys the
        snapshot keeps the base parameter so presets never bake a sampled
        frame value.
        """
        values = dict(self.parameters)
        live = (self._parent_panel._get_live_node(self.node_id)
                if getattr(self, "_parent_panel", None) is not None else None)
        animated_keys = (set(getattr(live, "keyframe_tracks", {}) or {})
                         if live is not None else set())
        for key, entry in self._param_widgets.items():
            if not isinstance(entry, dict):
                continue
            if key in animated_keys:
                continue  # base value already in ``values``
            widget = entry.get("widget")
            kind = entry.get("type")
            try:
                if kind == "bool":
                    values[key] = widget.isChecked()
                elif kind in ("combo", "channel", "bool_channel"):
                    data = widget.currentData()
                    values[key] = data if data is not None else widget.currentText()
                elif kind in ("float", "int"):
                    values[key] = widget.value()
                elif kind in ("str", "text"):
                    values[key] = widget.text()
                elif kind == "color":
                    values[key] = widget.text()
            except Exception:  # noqa: BLE001 - keep the synced value on read failure
                continue
        return values

    def _on_save_preset(self) -> None:
        """Save this node (with its current UI values) as a preset."""
        try:
            from PyQt6.QtWidgets import QInputDialog, QMessageBox

            from src.features.composite.utils.preset_manager import PresetManager
        except ImportError:
            return
        name, ok = QInputDialog.getText(
            self,
            "Save Node Preset / 保存节点预设",
            f"Preset name for {self.node_title}:",
            text=f"{self.node_type}_preset",
        )
        if not ok or not name.strip():
            return
        nodes_data = {
            "nodes": [{
                "id": self.node_id,
                "type": self.node_type,
                "parameters": self._collect_current_values(),
            }],
            "connections": [],
        }
        try:
            saved = PresetManager().save_preset(name.strip(), nodes_data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "Save Node Preset", f"Failed to save preset:\n{exc}",
            )
            return
        if saved:
            QMessageBox.information(
                self, "Save Node Preset", f"Preset '{name.strip()}' saved.",
            )


class NodePropertiesPanel(QWidget):
    """
    Properties panel that shows parameters of the currently selected node.

    Automatically builds UI controls based on parameter types and metadata:
    - option type → QComboBox
    - float → QDoubleSpinBox + QSlider (with min/max from PARAM_DEFS)
    - int → QSpinBox + QSlider (with min/max from PARAM_DEFS)
    - str → QLineEdit + browse button for file_path
    - bool → QCheckBox
    - color → Color picker button
    - file → File picker with browse button
    - vec2/vec3 → Multiple spinboxes for each dimension
    - channel → Channel selector (R, G, B, A, etc.)

    Supports multi-selection: displays common parameters across selected nodes
    and allows batch editing.

    Keyframe Animation:
    - float/int parameters show keyframe button (diamond icon)
    - Clicking keyframe button opens KeyframeEditorWidget dialog
    - keyframes_changed signal emitted when keyframes are modified
    """

    # Signal: a parameter value was changed by the user (single node)
    parameter_changed = pyqtSignal(str, str, object)  # node_id, param_name, value

    # Signal: a parameter was changed for multiple nodes (batch update)
    parameters_changed_batch = pyqtSignal(list, str, object, dict)  # node_ids, param_name, new_value, old_values_dict

    # Signal: keyframes were changed for a parameter
    keyframes_changed = pyqtSignal(str, str, str, list)  # node_id, node_type, param_name, keyframes_list

    # Signal: an AE-style expression was applied or cleared.
    expression_changed = pyqtSignal(str, str, object)  # node_id, param_name, expression|None

    # Signal: request to open keyframe editor for a parameter
    keyframe_edit_requested = pyqtSignal(str, str)  # node_id, param_name

    # P1: relayed from the KeyframeEditorWidget dialog so dragging its playhead
    # seeks the host viewport (CompositePage binds this to
    # set_composite_current_frame for live preview while editing curves).
    current_frame_changed = pyqtSignal(int)

    # Signal: node enabled/disabled toggled from section header
    node_enabled_toggled = pyqtSignal(str, bool)  # node_id, enabled

    # Signal: node color changed from section header
    node_color_changed = pyqtSignal(str, str)  # node_id, hex_color

    # Signal: Output node "Render Output" button clicked
    render_output_requested = pyqtSignal(str)  # node_id

    # Tracker action signals relayed from NodeSectionWidget.  The section
    # owns the buttons, while CompositePage connects to the panel-level API.
    tracker_analyze_requested = pyqtSignal(str)       # node_id
    tracker_bake_requested = pyqtSignal(str, str)     # node_id, target_kind
    tracker_cancel_requested = pyqtSignal(str)        # node_id
    tracker_import_color_requested = pyqtSignal(str)  # node_id
    tracker_stabilize_requested = pyqtSignal(str)     # node_id

    # Signal: Scene3D 参数面板的 2D/3D 预览模式开关被切换（纯视图状态，不进节点图/undo）。
    # 发射目标模式 "2d" / "3d"。page 接收后调 viewer.set_view_mode（与 Tab 键同路径）。
    view_mode_toggle_requested = pyqtSignal(str)  # "2d" | "3d"

    # Signal: Camera3D 参数面板请求进入该相机视角（纯 viewer 状态）。
    look_through_camera_requested = pyqtSignal(str)  # camera3d node_id
    camera_activation_toggled = pyqtSignal(str, bool)  # camera3d node_id, active

    # Signal: header gear menu requests settings sections.
    project_settings_requested = pyqtSignal()
    viewer3d_settings_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current_node_id: Optional[str] = None
        self._current_node_type: Optional[str] = None
        self._current_parameters: Dict[str, Any] = {}
        # 当前 viewer 的视图模式（"2d"/"3d"），由 page 同步；Scene3D 面板开关据此显示。
        self._current_view_mode: str = "2d"
        self._block_signals = False
        self._constant_batch_update = False  # True during batch color picker updates
        # B3-2: optional (node_id, param_name) -> None hook installed by the
        # host page (see composite_page.properties_panel setter). Presence
        # enables the "Promote to Group" entry in parameter context menus.
        # AUD-10701: without this default the menu handler below read an
        # undefined attribute and every right-click on a numeric/XY/XYZ/RGB
        # parameter label raised AttributeError before the menu could show.
        self._group_promote_hook: Optional[Callable[[str, str], None]] = None
        # CP-051: 参数级值剪贴板（面板内 复制/粘贴 参数值）
        self._param_value_clipboard: Optional[dict] = None
        # Image Color mode hides every ◆ keyframe button; new buttons created
        # afterwards inherit this flag (see set_keyframe_buttons_visible).
        self._keyframe_buttons_visible: bool = True
        self._param_defs: Dict = {}  # param_name -> ParamDef
        self._param_options: Dict[str, list] = {}
        self._search_filter = ""
        self._clipboard_params: Optional[Dict[str, Any]] = None

        # 缓存参数控件，用于增量更新
        self._param_widgets: Dict[str, QWidget] = {}  # param_name -> widget
        self._param_group: Optional[QWidget] = None  # 参数组容器

        # 多选节点支持
        self._current_node_ids: List[str] = []  # 多选节点 ID 列表
        self._current_node_types: List[str] = []  # 多选节点类型列表
        self._is_multi_selection: bool = False  # 是否为多选模式
        self._common_parameters: Dict[str, Any] = {}  # 公共参数（所有节点共有）
        self._param_values_per_node: Dict[str, Dict[str, Any]] = {}  # 每个节点的参数值缓存

        # Fusion-style multi-section support
        self._sections: Dict[str, NodeSectionWidget] = {}  # node_id -> section widget
        self._pinned_node_ids: set = set()  # node_ids that are pinned

        # Per-node "Separate Dimensions" state (AE-style): node_id -> set of X/Y
        # base names the user has split into two single rows. Empty/absent = the
        # pair stays on one combined row with a single shared keyframe.
        self._split_dims: Dict[str, set] = {}

        # Widest parameter label pixel width for the current node — computed in
        # _build_parameters before rows are created so every _ParamLabel uses
        # the same minimum width and input widgets align vertically.
        self._label_min_width: int = 36

        # 按 node_id 缓存当前图像尺寸 (w, h)，供 pivot 行显示真实中心值。
        # 模仿 _split_dims 的存储方式；image_size 不是节点参数，而是 viewer
        # 的渲染上下文，所以单独存这层映射。
        self._image_sizes: Dict[str, Optional[Tuple[int, int]]] = {}

        # Animation state
        self._total_frames: int = 100  # Total frames for timeline
        self._current_frame: int = 0   # Current frame for keyframe display

        # A2: the currently-open keyframe editor dialog (non-modal). Tracked so
        # the page can push main-playbar frame changes back into the editor
        # (bidirectional playhead sync) and so we don't open two editors for
        # the same parameter at once. ``None`` when no editor is open.
        self._active_kf_dialog: Optional[QDialog] = None
        self._active_kf_editor: Optional[Any] = None  # KeyframeEditorWidget

        # P6: optional NodeGraph reference so the ◆ button can read/write the
        # authoritative per-node keyframe_tracks (instead of mutating the
        # class-level PARAM_DEFS, which leaked state across same-type nodes).
        # Injected via set_node_graph(); absent in legacy/headless callers.
        self._node_graph: Optional[Any] = None

        # Project Settings section: lives at the bottom of the panel alongside
        # node sections (Nuke-style). Can be toggled visible via the S
        # shortcut and pinned so it persists while inspecting other nodes.
        self._project_settings_section = None  # ProjectSettingsSection (lazy)
        self._project_settings_visible = False
        self._project_settings_pinned = False
        self._viewer3d_settings_section = None  # Viewer3DSettingsSection (lazy)
        self._viewer3d_settings_visible = False
        self._viewer3d_settings_pinned = False
        self._active_camera_id: str = ""

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 防止 dock 被拖到裁切参数：最紧的一行是 vec2 控件行
        # （参数名标签 minWidth 36 + 两组「axis 12 + spinbox 65」+ 关键帧按钮 + 间距 ≈ 230px），
        # 留 ~50px 缓冲后设 280px 下限，确保 X/Y 行（translate/scale/pivot）不丢控件。
        self.setMinimumWidth(280)

        # Timeline toolbar button style
        _tool_btn_style = TOOLBAR_FLAT_BTN_STYLE

        # Header
        header = QWidget()
        header.setFixedHeight(28)
        header.setStyleSheet(PROPS_HEADER_STYLE)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 0, 4, 0)
        header_layout.setSpacing(2)

        self._title_label = QLabel(I18N.tr("node_props.properties_title", "Properties"))
        self._title_label.setStyleSheet(PROPS_TITLE_LABEL_STYLE)
        header_layout.addWidget(self._title_label)

        header_layout.addStretch()

        # Search toggle button (timeline toolbar style)
        self._search_toggle_btn = QToolButton()
        self._search_toggle_btn.setIcon(get_icon('fa5s.search', IconColors.DEFAULT))
        self._search_toggle_btn.setIconSize(QSize(14, 14))
        self._search_toggle_btn.setToolTip(
            I18N.tr("node_props.toggle_search", "Toggle search (Ctrl+F)"))
        self._search_toggle_btn.setFixedSize(24, 24)
        self._search_toggle_btn.setCheckable(True)
        self._search_toggle_btn.setStyleSheet(_tool_btn_style)
        self._search_toggle_btn.clicked.connect(self._toggle_search_bar)
        header_layout.addWidget(self._search_toggle_btn)

        self._settings_btn = QToolButton()
        self._settings_btn.setIcon(get_icon('fa5s.cog', IconColors.DEFAULT))
        self._settings_btn.setIconSize(QSize(14, 14))
        self._settings_btn.setToolTip(I18N.tr("node_props.settings", "Settings"))
        self._settings_btn.setFixedSize(24, 24)
        self._settings_btn.setStyleSheet(_tool_btn_style)
        self._settings_btn.clicked.connect(self._show_settings_menu)
        header_layout.addWidget(self._settings_btn)

        layout.addWidget(header)

        # Search bar (hidden by default)
        self._search_widget = QWidget()
        self._search_widget.setFixedHeight(28)
        self._search_widget.setStyleSheet(PROPS_HEADER_STYLE)
        search_layout = QHBoxLayout(self._search_widget)
        search_layout.setContentsMargins(8, 4, 8, 4)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            I18N.tr("node_props.search_placeholder", "Search parameters..."))
        self._search_input.setStyleSheet("""
            QLineEdit {
                background-color: #17181c; border: 1px solid #333;
                border-radius: 3px; padding: 2px 6px; color: #D6D8DE;
                font-size: 10px;
            }
            QLineEdit:focus { border-color: #E29227; }
        """)
        self._search_input.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self._search_input)
        self._search_widget.hide()
        layout.addWidget(self._search_widget)

        # Scroll area for properties
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setStyleSheet("QScrollArea { background-color: #27272B; border: none; }")
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setStyleSheet("background-color: #27272B;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(4)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll)

        self.setStyleSheet("""
            QWidget { background-color: #27272B; color: #D6D8DE; font-size: 11px; }
            /* QGroupBox uses global DARK_THEME style */
        """)

        # Keyboard shortcut: Ctrl+F / Cmd+F to toggle search
        search_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        search_shortcut.activated.connect(self._on_search_shortcut)

        # Enable context menu for float/dock toggle
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def set_group_promote_hook(
            self, hook: Optional[Callable[[str, str], None]]) -> None:
        """Register the "Promote to Group" resolver (B3-2, AUD-10701).

        ``hook(node_id, param_name)`` is called when the user picks the
        promote entry on a parameter context menu. Installed by
        ``composite_page.properties_panel`` (its ``hasattr`` guard now finds
        this method); ``None`` disables the entry.
        """
        self._group_promote_hook = hook

    def set_node(self, node_id: Optional[str], node_type: Optional[str],
                 node_title: Optional[str], parameters: Dict[str, Any],
                 param_options: Optional[Dict[str, list]] = None,
                 param_defs: Optional[Dict] = None,
                 state: str = "dirty", progress: float = 0.0,
                 error_message: str = "",
                 enabled: bool = True, node_color: str = "#666666",
                 image_size: Optional[Tuple[int, int]] = None,
                 read_only: bool = False) -> None:
        """Update the panel to show the given node's properties using a section.

        Backward-compatible with the old API. Delegates to set_nodes() internally.

        ``image_size`` (可选) 传入当前图像的 (width, height)，用于让 pivot 行在
        ``pivot_mode="center"`` 时显示真实图像中心（只读）。未传或 (0,0) 时 pivot
        行 fallback 到参数原值。
        """
        # Project Settings coexists with node sections, so selecting a node
        # does not hide it (only an explicit S-key toggle or unpin does).
        if not node_id:
            self._clear_unpinned_sections()
            return

        nodes_data = [{
            'node_id': node_id,
            'node_type': node_type,
            'node_title': node_title,
            'parameters': parameters,
            'param_options': param_options,
            'param_defs': param_defs,
            'state': state,
            'progress': progress,
            'error_message': error_message,
            'enabled': enabled,
            'node_color': node_color,
            'image_size': image_size,
            'read_only': read_only,
        }]
        self.set_nodes(nodes_data)

    def set_nodes(self, nodes_data: List[Dict[str, Any]]) -> None:
        """Update the panel to show Fusion-style node sections.

        Each selected node gets its own collapsible section with a toolbar.
        Pinned sections remain visible even when deselected.
        Selection order is preserved (first selected on top).

        Args:
            nodes_data: List of node data dictionaries, each containing:
                - node_id: str
                - node_type: str
                - node_title: str
                - parameters: Dict[str, Any]
                - param_options: Optional[Dict[str, list]]
                - param_defs: Optional[Dict]
                - enabled: bool (node enabled state)
                - node_color: str (hex color)
                - read_only: bool (persistent graph lock state)
                - state: str
        """
        if not nodes_data:
            # Only clear non-pinned sections
            self._clear_unpinned_sections()
            return

        self._block_signals = True
        # AUD-10702: the build/update chain below can raise (custom builders
        # hard-index node parameters, e.g. Constant's params["mode"]). Without
        # try/finally the flag would stay True and _on_param_changed would
        # early-return forever — permanently deadening every parameter edit.
        try:
            # Determine which nodes should be visible: selected + pinned
            selected_ids = [d.get('node_id') for d in nodes_data]
            visible_ids = list(selected_ids)  # preserve selection order
            # Add pinned nodes that aren't already in the selection.
            # AUD-10705: a pinned id whose node was deleted from the graph
            # must be dropped (zombie section) — but "not in nodes_data" alone
            # is NOT evidence of deletion (pinned nodes are by definition not
            # part of the selection), so existence is checked against the
            # bound NodeGraph. Without a graph (headless/legacy callers) the
            # page-driven remove_node() API is the cleanup path and pinned
            # sections keep their old behaviour.
            for pid in list(self._pinned_node_ids):
                if pid in visible_ids:
                    continue
                node_exists = True
                if self._node_graph is not None:
                    try:
                        node_exists = self._node_graph.get_node(pid) is not None
                    except (AttributeError, TypeError):
                        # Malformed graph object — do not guess a deletion.
                        node_exists = True
                if node_exists:
                    visible_ids.append(pid)
                else:
                    self._pinned_node_ids.discard(pid)
                    self._split_dims.pop(pid, None)

            # Build a lookup for node data
            data_by_id = {d.get('node_id'): d for d in nodes_data}

            # Remove sections that are no longer visible
            to_remove = [nid for nid in self._sections if nid not in visible_ids]
            for nid in to_remove:
                self._remove_section(nid)

            # Add or update sections in order
            for i, nid in enumerate(visible_ids):
                data = data_by_id.get(nid)

                # 缓存该节点的图像尺寸（多选未传时回填 None 以清除旧值）。
                new_size = data.get('image_size') if data else None
                old_size = self._image_sizes.get(nid, None)
                size_changed = (new_size != old_size)
                self._image_sizes[nid] = new_size

                if nid in self._sections:
                    # Update existing section
                    section = self._sections[nid]
                    if data:
                        section.update_parameters(data.get('parameters', {}))
                        if hasattr(section, 'set_camera_active'):
                            section.set_camera_active(
                                section.node_type == "camera3d"
                                and section.node_id == self._active_camera_id)
                        if 'enabled' in data:
                            section.set_enabled(data['enabled'])
                        if 'node_color' in data:
                            section.set_node_color(data['node_color'])
                        if 'read_only' in data:
                            section.set_read_only(bool(data['read_only']))
                    # Reorder: move to correct position
                    self._content_layout.removeWidget(section)
                    self._content_layout.insertWidget(i, section)
                else:
                    # Create new section
                    if data:
                        section = self._create_section(data)
                        self._sections[nid] = section
                        self._content_layout.insertWidget(i, section)

                # 图像尺寸变化会影响 pivot 行的显示值（center 模式下显示图像中心）。
                # update_parameters 不会因此重建 pivot 行，这里手动刷新一次显示。
                if size_changed:
                    self._refresh_pivot_display(section._param_widgets)

            # Update title
            node_count = len(visible_ids)
            if node_count == 0:
                self._title_label.setText(
                    I18N.tr("node_props.properties_title", "Properties"))
            elif node_count == 1:
                d = data_by_id.get(visible_ids[0], {})
                self._title_label.setText(f"Properties - {d.get('node_title', '')}")
            else:
                self._title_label.setText(f"Properties - {node_count} nodes")

            # Keep backward compatibility: set _current_node_id to first selected
            if selected_ids:
                first_data = data_by_id.get(selected_ids[0], {})
                self._current_node_id = selected_ids[0]
                self._current_node_type = first_data.get('node_type')
                self._current_parameters = dict(first_data.get('parameters', {}))
                self._param_defs = first_data.get('param_defs', {})
                self._param_options = first_data.get('param_options', {})
            else:
                self._current_node_id = None

            # Ensure the Project Settings section stays at the bottom after node
            # sections are rebuilt (it is not part of _sections, so insertWidget
            # above already leaves it last; this is a cheap no-op when in order).
            if self._project_settings_visible and self._project_settings_section is not None:
                self._append_project_settings_section()
            if self._viewer3d_settings_visible and self._viewer3d_settings_section is not None:
                self._append_viewer3d_settings_section()

            # 选中其他节点时，未 pin 的 Settings section 自动折叠，避免占用过多空间。
            # 已 pin 的保持展开（用户明确想固定它）。collapse() 幂等，且本方法仅在真正
            # 选中/切换节点时进入（参数微调走 update_parameters，不会误触发）。
            if (self._project_settings_visible
                    and not self._project_settings_pinned
                    and self._project_settings_section is not None):
                self._project_settings_section.collapse()
            if (self._viewer3d_settings_visible
                    and not self._viewer3d_settings_pinned
                    and self._viewer3d_settings_section is not None):
                self._viewer3d_settings_section.collapse()
        finally:
            self._block_signals = False

    def _remove_section(self, node_id: str) -> None:
        """Tear down one node section and its per-section side state (AUD-10705).

        Central removal path for set_nodes / _clear_unpinned_sections /
        remove_node. Cleans ``_image_sizes`` and the scene3d toggle map (the
        buttons are re-registered whenever the section is rebuilt). Does NOT
        touch ``_split_dims``: the user's Separate-Dimensions choice must
        survive deselect/reselect — that map is only purged when the node is
        truly gone (see remove_node / stale-pinned pruning in set_nodes).
        """
        section = self._sections.pop(node_id, None)
        if section is not None:
            self._content_layout.removeWidget(section)
            section.prepare_for_deletion()
        self._image_sizes.pop(node_id, None)
        toggles = getattr(self, "_scene3d_toggle_buttons", None)
        if isinstance(toggles, dict):
            toggles.pop(node_id, None)

    def remove_node(self, node_id: str) -> None:
        """Drop a deleted node's section and all panel-side state (AUD-10705).

        Public API for the host page: call on node-removed so a pinned node
        deleted from the graph does not leave a zombie section (and the
        per-node split-dim / scene3d-toggle maps do not keep growing).
        """
        self._pinned_node_ids.discard(node_id)
        self._split_dims.pop(node_id, None)
        self._remove_section(node_id)
        if not self._sections:
            self._title_label.setText(
                I18N.tr("node_props.properties_title", "Properties"))

    def _create_section(self, data: Dict[str, Any]) -> "NodeSectionWidget":
        """Create a NodeSectionWidget from node data dict."""
        section = NodeSectionWidget(
            node_id=data.get('node_id', ''),
            node_type=data.get('node_type', ''),
            node_title=data.get('node_title', ''),
            parameters=data.get('parameters', {}),
            param_options=data.get('param_options'),
            param_defs=data.get('param_defs'),
            enabled=data.get('enabled', True),
            node_color=data.get('node_color', '#666666'),
            parent_panel=self,
        )
        section.set_read_only(bool(data.get('read_only', False)))
        # Connect section signals to panel signals
        section.parameter_changed.connect(self.parameter_changed)
        section.node_enabled_toggled.connect(self.node_enabled_toggled)
        section.node_color_changed.connect(self.node_color_changed)
        section.pin_toggled.connect(self._on_section_pin_toggled)
        section.render_output_requested.connect(self.render_output_requested)
        section.tracker_analyze_requested.connect(self.tracker_analyze_requested)
        section.tracker_bake_requested.connect(self.tracker_bake_requested)
        section.tracker_cancel_requested.connect(self.tracker_cancel_requested)
        section.tracker_import_color_requested.connect(
            self.tracker_import_color_requested)
        section.tracker_stabilize_requested.connect(
            self.tracker_stabilize_requested)
        section.view_mode_toggle_requested.connect(self.view_mode_toggle_requested)
        section.look_through_camera_requested.connect(self.look_through_camera_requested)
        section.camera_activation_toggled.connect(self.camera_activation_toggled)
        section.set_camera_active(
            section.node_type == "camera3d"
            and section.node_id == self._active_camera_id)
        section.node_reset_requested.connect(
            lambda nid: self._reset_node_parameters(nid)
        )
        # Apply current search filter
        if self._search_filter:
            section.set_search_filter(self._search_filter)
        return section

    def set_active_camera_id(self, camera_id: Optional[str]) -> None:
        """Sync Camera3D header activation toggles."""
        self._active_camera_id = str(camera_id or "")
        for section in self._sections.values():
            if hasattr(section, 'set_camera_active'):
                section.set_camera_active(
                    section.node_type == "camera3d"
                    and section.node_id == self._active_camera_id)

    def _clear_unpinned_sections(self) -> None:
        """Remove all non-pinned sections."""
        to_remove = [nid for nid in self._sections if nid not in self._pinned_node_ids]
        for nid in to_remove:
            self._remove_section(nid)

        if not self._sections:
            self._title_label.setText(
                I18N.tr("node_props.properties_title", "Properties"))
            self._current_node_id = None
            self._current_parameters = {}

    def _show_settings_menu(self) -> None:
        """Show the Properties header settings menu."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #3a3a3a; color: #ccc; border: 1px solid #555;
                padding: 4px; font-size: 12px;
            }
            QMenu::item:selected { background-color: #cc6600; }
        """)
        project_action = menu.addAction(
            I18N.tr("node_props.project_settings", "Project Settings"))
        viewer3d_action = menu.addAction(
            I18N.tr("node_props.scene3d_settings", "3D Scene Settings"))
        chosen = menu.exec(self._settings_btn.mapToGlobal(
            self._settings_btn.rect().bottomLeft()))
        if chosen == project_action:
            self.project_settings_requested.emit()
        elif chosen == viewer3d_action:
            self.viewer3d_settings_requested.emit()

    def set_view_mode(self, mode: str) -> None:
        """记录当前 viewer 视图模式（由 page 在 view_mode 变化时同步）。

        Scene3D 参数面板的 2D/3D 开关据此显示高亮。纯视图状态，不影响节点参数。
        若当前显示的 scene3d section 的开关已绘制，直接更新它的 checked 状态
        （blockSignals 避免重入触发 toggle 信号）。
        """
        mode = "3d" if mode == "3d" else "2d"
        self._current_view_mode = mode
        for node_id, (btn_2d, btn_3d) in getattr(
                self, "_scene3d_toggle_buttons", {}).items():
            if node_id not in self._sections:
                continue
            # 互斥高亮：checked 的那个表示当前模式。
            want_3d = (mode == "3d")
            if btn_3d.isChecked() != want_3d:
                btn_3d.blockSignals(True)
                btn_3d.setChecked(want_3d)
                btn_3d.blockSignals(False)
            if btn_2d.isChecked() != (not want_3d):
                btn_2d.blockSignals(True)
                btn_2d.setChecked(not want_3d)
                btn_2d.blockSignals(False)

    def _on_section_pin_toggled(self, node_id: str, is_pinned: bool) -> None:
        """Handle pin toggle from a section."""
        if is_pinned:
            self._pinned_node_ids.add(node_id)
        else:
            self._pinned_node_ids.discard(node_id)

    def _reset_node_parameters(self, node_id: str) -> None:
        """Handle reset request from a section - emit parameter_changed for each param."""
        section = self._sections.get(node_id)
        if not section:
            return
        for key in list(section.parameters.keys()):
            pdef = section.param_defs.get(key)
            if pdef and pdef.default is not None:
                self.parameter_changed.emit(node_id, key, pdef.default)

    def _compute_common_parameters(self, nodes_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """计算所有选中节点的公共参数。

        只包含所有节点都有的参数，并检查值是否一致。

        Returns:
            Dict 包含公共参数名和值。如果参数值在不同节点间不一致，
            值会显示为 "mixed" 模式。
        """
        if not nodes_data:
            return {}

        # 获取第一个节点的参数名集合
        first_params = set(nodes_data[0].get('parameters', {}).keys())

        # 找出所有节点共有的参数名
        common_param_names = first_params.copy()
        for data in nodes_data[1:]:
            params = set(data.get('parameters', {}).keys())
            common_param_names &= params

        # 计算公共参数的值
        common_params = {}
        for param_name in common_param_names:
            values = []
            for data in nodes_data:
                val = data.get('parameters', {}).get(param_name)
                values.append(val)

            # 如果所有值相同，使用该值
            if all(v == values[0] for v in values):
                common_params[param_name] = values[0]
            else:
                # 值不一致，使用特殊标记（保持第一个节点的值，UI 会显示差异提示）
                common_params[param_name] = values[0]

        return common_params

    def _create_multi_selection_params_group(self, common_params: Dict[str, Any],
                                              nodes_data: List[Dict[str, Any]]) -> QWidget:
        """创建多选节点的参数控件组。

        参数控件会显示值差异指示（如果不同节点值不同）。
        """
        group = QGroupBox(I18N.tr("node_props.common_parameters", "Common Parameters"))
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        for key, value in common_params.items():
            if self._matches_search(key):
                # 检查值是否在所有节点中一致
                values_are_mixed = False
                for data in nodes_data:
                    node_val = data.get('parameters', {}).get(key)
                    if node_val != value:
                        values_are_mixed = True
                        break

                row = self._create_multi_selection_param_row(key, value, values_are_mixed)
                layout.addWidget(row)

        return group

    def _create_multi_selection_param_row(self, key: str, value: Any,
                                           values_are_mixed: bool) -> QWidget:
        """创建单个多选参数行，带有差异指示。"""
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        # 标签行（带差异指示）
        label_row = QHBoxLayout()
        label_row.setSpacing(4)

        label = QLabel(key.replace("_", " ").title())
        label.setStyleSheet("color: #bbb; font-size: 10px;")
        label_row.addWidget(label)

        # 如果值不一致，显示差异指示
        if values_are_mixed:
            mixed_label = QLabel(I18N.tr("node_props.mixed_values", "(mixed)"))
            mixed_label.setStyleSheet("color: #ff8800; font-size: 9px; font-style: italic;")
            label_row.addWidget(mixed_label)

        # 重置按钮
        reset_btn = QToolButton()
        reset_btn.setText("↺")
        reset_btn.setToolTip(f"Reset {key} to default")
        reset_btn.setFixedSize(16, 16)
        reset_btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999;
                border-radius: 2px;
            }
            QToolButton:hover { background-color: #555; color: #fff; }
        """)
        label_row.addWidget(reset_btn)
        row_layout.addLayout(label_row)

        # 创建参数控件（使用相同的控件创建逻辑）
        options = self._param_options.get(key)
        pdef = self._param_defs.get(key)

        if options is not None and isinstance(options, (list, tuple)):
            combo = ChevronComboBox()
            node_type = self._current_node_type or ""
            display_items = [self._option_display_label(node_type, key, str(o))
                             for o in options]
            combo.addItems(display_items)
            if value in options:
                combo.setCurrentIndex(options.index(value))
            apply_combo_dropdown_style(combo)
            combo.currentIndexChanged.connect(
                lambda idx, k=key, opts=list(options):
                    self._on_multi_param_changed(k, opts[idx]) if 0 <= idx < len(opts) else None
            )
            row_layout.addWidget(combo)
            self._param_widgets[key] = {'type': 'combo', 'widget': combo, 'options': list(options)}

        elif isinstance(value, bool):
            # 紧凑：标签已在上方 label_row，checkbox 不再重复参数名文字。
            checkbox = QCheckBox()
            checkbox.setChecked(value)
            checkbox.setText("")
            checkbox.toggled.connect(
                lambda checked, k=key: self._on_multi_param_changed(k, checked)
            )
            row_layout.addWidget(checkbox)
            self._param_widgets[key] = {'type': 'bool', 'widget': checkbox}

        elif isinstance(value, float):
            # Float: slider + spinbox
            h_layout = QHBoxLayout()
            h_layout.setSpacing(4)

            min_val = pdef.min_val if pdef else 0.0
            max_val = pdef.max_val if pdef else 1.0
            step = pdef.step if pdef else 0.01
            decimals = pdef.decimals if pdef else 2

            slider = QSlider(Qt.Orientation.Horizontal)
            slider_range = max_val - min_val
            slider_steps = int(slider_range / step)
            slider.setRange(0, max(slider_steps, 1))
            slider.setValue(int((value - min_val) / step))

            spinbox = QDoubleSpinBox()
            spinbox.setStyleSheet("background-color: #232325; border: 1px solid #151517;")
            spinbox.setRange(min_val, max_val)
            spinbox.setSingleStep(step)
            spinbox.setDecimals(decimals)
            spinbox.setValue(value)
            spinbox.setFixedWidth(70)

            def slider_changed(val, k=key, sb=spinbox, s=step, m=min_val) -> None:
                if self._block_signals:
                    return
                float_val = val * s + m
                sb.blockSignals(True)
                sb.setValue(float_val)
                sb.blockSignals(False)
                self._on_multi_param_changed(k, float_val)

            def spinbox_changed(val, k=key, sl=slider, s=step, m=min_val) -> None:
                if self._block_signals:
                    return
                sl.blockSignals(True)
                sl.setValue(int((val - m) / s))
                sl.blockSignals(False)
                self._on_multi_param_changed(k, val)

            slider.valueChanged.connect(slider_changed)
            spinbox.valueChanged.connect(spinbox_changed)

            h_layout.addWidget(slider)
            h_layout.addWidget(spinbox)
            row_layout.addLayout(h_layout)
            self._param_widgets[key] = {
                'type': 'float',
                'slider': slider,
                'spinbox': spinbox,
                'min_val': min_val,
                'max_val': max_val,
                'step': step
            }

        elif isinstance(value, int):
            h_layout = QHBoxLayout()
            h_layout.setSpacing(4)

            min_val = int(pdef.min_val) if pdef else 0
            max_val = int(pdef.max_val) if pdef else max(value * 2, 100)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(min_val, max_val)
            slider.setValue(value)

            spinbox = QSpinBox()
            spinbox.setStyleSheet("background-color: #232325; border: 1px solid #151517;")
            spinbox.setRange(min_val, max_val)
            spinbox.setValue(value)
            spinbox.setFixedWidth(70)

            def int_slider_changed(val, k=key, sb=spinbox) -> None:
                if self._block_signals:
                    return
                sb.blockSignals(True)
                sb.setValue(val)
                sb.blockSignals(False)
                self._on_multi_param_changed(k, val)

            def int_spinbox_changed(val, k=key, sl=slider) -> None:
                if self._block_signals:
                    return
                sl.blockSignals(True)
                sl.setValue(val)
                sl.blockSignals(False)
                self._on_multi_param_changed(k, val)

            slider.valueChanged.connect(int_slider_changed)
            spinbox.valueChanged.connect(int_spinbox_changed)

            h_layout.addWidget(slider)
            h_layout.addWidget(spinbox)
            row_layout.addLayout(h_layout)
            self._param_widgets[key] = {
                'type': 'int',
                'slider': slider,
                'spinbox': spinbox,
                'min_val': min_val,
                'max_val': max_val
            }

        elif isinstance(value, str):
            ptype = getattr(pdef, 'ptype', None) if pdef else None

            if ptype == 'color':
                color_btn = self._create_color_picker(key, value)
                # 替换信号连接为多选版本（实时预览，用项目色彩选择器 get_color）
                color_btn.clicked.disconnect()
                def pick_color_multi(_checked=False, k=key, btn=color_btn) -> None:
                    # _checked=False 占位：QPushButton.clicked(bool) 会把 checked 作为
                    # 首参传入，若不接收会顶替 k（key），导致参数名变成 bool → 类型崩溃。
                    self._last_color_param = k  # CP-056 吸管写回目标
                    from src.shared.color import get_color
                    current = QColor(self._common_parameters.get(k, '#808080'))

                    def _on_live_multi(color: QColor) -> None:
                        hex_color = color.name()
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {hex_color};
                                border: 1px solid #555;
                                border-radius: 3px;
                                min-height: 20px;
                            }}
                        """)
                        btn.setText(hex_color)
                        self._on_multi_param_changed(k, hex_color)

                    result = get_color(parent=self, initial=current, on_live=_on_live_multi)
                    if result is not None:
                        hex_color = result.name()
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {hex_color};
                                border: 1px solid #555;
                                border-radius: 3px;
                                min-height: 20px;
                            }}
                        """)
                        btn.setText(hex_color)
                        self._on_multi_param_changed(k, hex_color)

                color_btn.clicked.connect(pick_color_multi)
                row_layout.addWidget(color_btn)
                self._param_widgets[key] = {'type': 'color', 'widget': color_btn}

            elif ptype == 'file':
                file_picker = self._create_file_picker(key, value)
                row_layout.addWidget(file_picker)
                self._param_widgets[key] = {'type': 'file', 'widget': file_picker}

            elif ptype == 'channel':
                channel_combo = self._create_channel_selector(key, value)
                channel_combo.currentTextChanged.disconnect()
                channel_combo.currentTextChanged.connect(
                    lambda text, k=key: self._on_multi_param_changed(k, text)
                )
                row_layout.addWidget(channel_combo)
                self._param_widgets[key] = {'type': 'channel', 'widget': channel_combo}

            else:
                line_edit = QLineEdit(str(value))
                line_edit.setStyleSheet("""
                    QLineEdit {
                        background-color: #3a3a3a; border: 1px solid #555;
                        border-radius: 3px; padding: 2px 6px; color: #ccc;
                    }
                """)
                line_edit.editingFinished.connect(
                    lambda k=key, le=line_edit: self._on_multi_param_changed(k, le.text())
                )
                row_layout.addWidget(line_edit)
                self._param_widgets[key] = {'type': 'str', 'widget': line_edit}

        elif isinstance(value, (list, tuple)):
            ptype = getattr(pdef, 'ptype', None) if pdef else None

            if ptype == 'curve':
                curve_widget = self._create_curve_control(key, value)
                row_layout.addWidget(curve_widget)
                self._param_widgets[key] = {'type': 'curve', 'widget': curve_widget}
            elif ptype in ('vec2', 'vec3'):
                dim = 3 if ptype == 'vec3' else 2
                vec_widget = self._create_vec_control(key, value, dim)
                row_layout.addWidget(vec_widget)
                info: Dict[str, Any] = {
                    'type': ptype, 'widget': vec_widget, 'dim': dim,
                    'animatable': bool(pdef and pdef.is_animatable()),
                }
                if info['animatable']:
                    kf_btn = self._make_keyframe_button(key, pdef)
                    row_layout.addWidget(kf_btn)
                    info['keyframe_btn'] = kf_btn
                    info['keyframe_btn_keys'] = [key]
                self._param_widgets[key] = info
            else:
                val_label = QLabel(str(value))
                val_label.setStyleSheet("color: #999;")
                row_layout.addWidget(val_label)
                self._param_widgets[key] = {'type': 'readonly', 'widget': val_label}

        else:
            val_label = QLabel(str(value))
            val_label.setStyleSheet("color: #999;")
            row_layout.addWidget(val_label)
            self._param_widgets[key] = {'type': 'readonly', 'widget': val_label}

        return row

    def apply_picked_color(self, r: float, g: float, b: float) -> None:
        """CP-056: write a viewer-picked linear color into the last-touched
        colour parameter (silently no-ops when none was touched).

        Colour parameters persist hex strings in sRGB display encoding —
        the linear sample is encoded before writing, matching the colour
        editor's load/store convention.
        """
        key = getattr(self, "_last_color_param", None)
        if not key:
            return
        if not self._current_node_id:
            return

        def _l2s(v):
            v = max(0.0, min(1.0, float(v)))
            s = (v * 12.92 if v <= 0.0031308
                 else 1.055 * (v ** (1 / 2.4)) - 0.055)
            return int(round(max(0.0, min(1.0, s)) * 255))

        hex_color = "#{:02x}{:02x}{:02x}".format(_l2s(r), _l2s(g), _l2s(b))
        try:
            self._on_param_changed(key, hex_color)
        except Exception:
            pass

    def _on_multi_param_changed(self, key: str, value: Any) -> None:
        """Handle parameter change for multi-selection."""
        if self._block_signals:
            return

        # 更新公共参数缓存
        self._common_parameters[key] = value

        # 收集所有节点的旧值
        old_values = {}
        for node_id in self._current_node_ids:
            old_values[node_id] = self._param_values_per_node.get(node_id, {}).get(key)

        # 发送批量更新信号
        self.parameters_changed_batch.emit(self._current_node_ids, key, value, old_values)

    def _create_params_group(self, parameters: Dict[str, Any]) -> QWidget:
        """Create parameter controls based on parameter types, values, and metadata.
        
        Supports grouped display if parameters have 'group' attribute in their ParamDef.
        """
        # Check if any parameter has a group
        has_groups = False
        groups: Dict[str, List[str]] = {}
        ungrouped_params = []

        for key in parameters.keys():
            pdef = self._param_defs.get(key)
            group_name = getattr(pdef, 'group', None) if pdef else None
            
            if group_name:
                has_groups = True
                if group_name not in groups:
                    groups[group_name] = []
                groups[group_name].append(key)
            else:
                ungrouped_params.append(key)

        if has_groups:
            # Create grouped layout
            main_group = QGroupBox(I18N.tr("node_props.parameters_group", "Parameters"))
            main_layout = QVBoxLayout(main_group)
            main_layout.setContentsMargins(8, 4, 8, 4)
            main_layout.setSpacing(6)

            # Add ungrouped parameters first
            if ungrouped_params:
                for key in ungrouped_params:
                    if self._matches_search(key):
                        value = parameters[key]
                        row = self._create_param_row(key, value)
                        main_layout.addWidget(row)

            # Add grouped parameters
            for group_name, param_keys in groups.items():
                group_box = QGroupBox(group_name)
                group_layout = QVBoxLayout(group_box)
                group_layout.setContentsMargins(8, 4, 8, 4)
                group_layout.setSpacing(4)

                for key in param_keys:
                    if self._matches_search(key):
                        value = parameters[key]
                        row = self._create_param_row(key, value)
                        group_layout.addWidget(row)

                main_layout.addWidget(group_box)

            return main_group
        else:
            # Original flat layout
            group = QGroupBox(I18N.tr("node_props.parameters_group", "Parameters"))
            layout = QVBoxLayout(group)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(4)

            for key, value in parameters.items():
                if self._matches_search(key):
                    row = self._create_param_row(key, value)
                    layout.addWidget(row)

            return group

    def _matches_search(self, key: str) -> bool:
        """Check if parameter key matches the current search filter."""
        if not self._search_filter:
            return True
        return self._search_filter.lower() in key.lower()

    def _toggle_search_bar(self) -> None:
        """Toggle search bar visibility."""
        visible = self._search_toggle_btn.isChecked()
        color = IconColors.ORANGE if visible else IconColors.DEFAULT
        self._search_toggle_btn.setIcon(get_icon('fa5s.search', color))
        self._search_widget.setVisible(visible)
        if visible:
            self._search_input.setFocus()
            self._search_input.selectAll()
        else:
            # Clear search when hiding
            self._search_input.clear()

    def _on_search_shortcut(self) -> None:
        """Handle Ctrl+F / Cmd+F shortcut to toggle search."""
        is_visible = self._search_widget.isVisible()
        self._search_toggle_btn.setChecked(not is_visible)
        self._toggle_search_bar()

    def _on_search_changed(self, text: str) -> None:
        """Handle search text change - rebuild parameters display in all sections."""
        self._search_filter = text
        # Update all visible sections
        for section in self._sections.values():
            section.set_search_filter(text)

    def _create_param_row(self, key: str, value: Any,
                          slider_holder: Optional[list] = None,
                          label_menu: Optional[tuple] = None) -> QWidget:
        """Create a single parameter row with appropriate control."""
        # Numeric params use the Fusion-style row: label on the left + a drag/typing
        # value box (+ an optional responsive slider on single rows), with
        # double-click-name reset. All other types fall through to the shared
        # label + control layout below.
        pdef = self._param_defs.get(key)
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            return self._create_numeric_row(key, value, pdef, slider_holder,
                                             label_menu=label_menu)

        # Bool: 单行紧凑 [label] [checkbox]（提前 return，避免走占两行的 fallback）。
        if isinstance(value, bool):
            return self._create_bool_row(key, value, pdef)

        # Combo/option: horizontal row [label] [ComboBox] so the combo's
        # left-edge aligns with numeric DragValue left-edges (same _ParamLabel
        # width), and the combo stretches to fill the remaining width.
        options = self._param_options.get(key)
        if options is not None and isinstance(options, (list, tuple)):
            return self._create_combo_row(key, value, options, pdef)

        # Channel selector: also a horizontal combo row for alignment.
        if isinstance(value, str) and getattr(pdef, 'ptype', None) == 'channel':
            return self._create_channel_combo_row(key, value, pdef)

        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(2)

        # Label with reset button
        label_row = QHBoxLayout()
        label_row.setSpacing(4)
        
        label = QLabel(key.replace("_", " ").title())
        label.setStyleSheet("color: #bbb; font-size: 10px;")
        label_row.addWidget(label)
        
        # Reset button
        reset_btn = QToolButton()
        reset_btn.setText("↺")
        reset_btn.setToolTip(f"Reset {key} to default")
        reset_btn.setFixedSize(16, 16)
        reset_btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999;
                border-radius: 2px;
            }
            QToolButton:hover { background-color: #555; color: #fff; }
        """)
        
        pdef = self._param_defs.get(key)
        if pdef and pdef.default is not None:
            def reset_param(_checked=False, k=key, d=pdef.default,
                            n=self._current_node_id) -> None:
                # _checked=False 占位：QToolButton.clicked(bool) 会把 checked 作为
                # 首参传入，若不接收会顶替 k（key），导致参数名变成 bool → 类型崩溃。
                self._on_param_changed(k, d, n)
            reset_btn.clicked.connect(reset_param)
        else:
            reset_btn.hide()
        
        label_row.addWidget(reset_btn)
        row_layout.addLayout(label_row)

        if isinstance(value, bool):
            # Checkbox for boolean
            checkbox = QCheckBox(key.replace("_", " ").title())
            checkbox.setChecked(value)
            checkbox.toggled.connect(
                lambda checked, k=key, n=self._current_node_id:
                self._on_param_changed(k, checked, n)
            )
            row_layout.addWidget(checkbox)
            # 保存控件引用
            self._param_widgets[key] = {'type': 'bool', 'widget': checkbox}

        elif isinstance(value, str):
            # Check ptype for specialized controls
            ptype = getattr(pdef, 'ptype', None) if pdef else None
            
            if ptype == 'color':
                # Color picker
                color_btn = self._create_color_picker(key, value)
                row_layout.addWidget(color_btn)
                # 保存控件引用
                self._param_widgets[key] = {'type': 'color', 'widget': color_btn}

            elif ptype == 'file':
                # File picker
                file_picker = self._create_file_picker(key, value)
                row_layout.addWidget(file_picker)
                # 保存控件引用（file_picker 包含 line_edit）
                self._param_widgets[key] = {'type': 'file', 'widget': file_picker}

            elif ptype == 'hdri':
                # HDRI preset picker（下拉预置 + 自定义文件按钮）
                hdri_picker = self._create_hdri_picker(key, str(value))
                row_layout.addWidget(hdri_picker)
                self._param_widgets[key] = {'type': 'hdri', 'widget': hdri_picker}

            else:
                # String: line edit - 使用 editingFinished 替代 textChanged
                line_edit = QLineEdit(str(value))
                line_edit.setStyleSheet("""
                    QLineEdit {
                        background-color: #3a3a3a; border: 1px solid #555;
                        border-radius: 3px; padding: 2px 6px; color: #ccc;
                    }
                """)
                # 使用 editingFinished 等待用户完成输入后再触发更新
                line_edit.editingFinished.connect(
                    lambda k=key, le=line_edit, n=self._current_node_id:
                    self._on_param_changed(k, le.text(), n)
                )
                row_layout.addWidget(line_edit)
                # 保存控件引用
                self._param_widgets[key] = {'type': 'str', 'widget': line_edit}
                # C5: ▲▼ version binning for Read/Write path params
                if key == "file_path":
                    self._attach_version_stepper(
                        row_layout, key, line_edit,
                        node_id=self._current_node_id)

        elif isinstance(value, (list, tuple)):
            # Check if this is a vec2/vec3 parameter
            ptype = getattr(pdef, 'ptype', None) if pdef else None
            
            if ptype == 'curve':
                # Curve editor
                curve_widget = self._create_curve_control(key, value)
                row_layout.addWidget(curve_widget)
                # 保存控件引用
                self._param_widgets[key] = {'type': 'curve', 'widget': curve_widget}
            elif ptype in ('vec2', 'vec3'):
                dim = 3 if ptype == 'vec3' else 2
                vec_widget = self._create_vec_control(key, value, dim)
                row_layout.addWidget(vec_widget)
                # 保存控件引用
                info: Dict[str, Any] = {
                    'type': ptype, 'widget': vec_widget, 'dim': dim,
                    'animatable': bool(pdef and pdef.is_animatable()),
                }
                if info['animatable']:
                    kf_btn = self._make_keyframe_button(key, pdef)
                    row_layout.addWidget(kf_btn)
                    info['keyframe_btn'] = kf_btn
                    info['keyframe_btn_keys'] = [key]
                self._param_widgets[key] = info
            else:
                val_label = QLabel(str(value))
                val_label.setStyleSheet("color: #999;")
                row_layout.addWidget(val_label)
                # 保存控件引用（只读显示）
                self._param_widgets[key] = {'type': 'readonly', 'widget': val_label}

        else:
            val_label = QLabel(str(value))
            val_label.setStyleSheet("color: #999;")
            row_layout.addWidget(val_label)
            # 保存控件引用（只读显示）
            self._param_widgets[key] = {'type': 'readonly', 'widget': val_label}

        return row

    # ── Fusion-style numeric rows (drag/typing value box) ────────────────────

    def _param_display_name(self, key: str, node_type: Optional[str] = None) -> str:
        """Human-readable name for a parameter key, i18n-aware.

        Looks up ``nodes.<node_type>.params.<key>`` first; falls back to the
        mechanical ``key.title()`` when no translation exists (preserves the
        original behaviour for un-translated nodes/params).
        """
        nt = node_type or self._current_node_type
        if nt:
            translated = tr_or_none(f"nodes.{nt}.params.{key}")
            if translated:
                return translated
        return key.replace("_", " ").title()

    def _option_display_label(self, node_type: str, param_key: str,
                              option_value: str) -> str:
        """Display label for an option value, i18n-aware.

        Looks up ``nodes.<node_type>.<param_key>.<option_value>``; falls back
        to ``option_value.title()``. The returned string is for display ONLY —
        the raw ``option_value`` is still what gets stored in parameters (kept
        in sync by using currentIndexChanged + the options list, NOT the
        displayed text).
        """
        if node_type:
            translated = tr_or_none(
                f"nodes.{node_type}.{param_key}.{option_value}")
            if translated:
                return translated
        return str(option_value).title()

    def _group_display_name(self, group: str) -> str:
        """Display name for a parameter group, i18n-aware."""
        translated = tr_or_none(f"param_groups.{group}")
        if translated:
            return translated
        return group.replace("_", " ").title()

    @staticmethod
    def _numeric_meta(value: Any, pdef) -> Tuple[bool, float, float, float, int]:
        """Return (is_int, min, max, step, decimals) for a numeric param."""
        is_int = isinstance(value, int) and not isinstance(value, bool)
        if is_int:
            mn = int(pdef.min_val) if pdef else 0
            mx = int(pdef.max_val) if pdef else max(int(value) * 2, 100)
            return True, mn, mx, 1, 0
        mn = pdef.min_val if pdef else 0.0
        mx = pdef.max_val if pdef else 1.0
        step = pdef.step if pdef else 0.01
        dec = pdef.decimals if pdef else 2
        return False, mn, mx, step, dec

    @staticmethod
    def _value_to_slider(value: float, info: Dict) -> int:
        """Map a numeric value to its slider int position."""
        if info.get('type') == 'int':
            return int(value)
        mn = info['min_val']
        step = info['step'] or 1.0
        return int((value - mn) / step)

    def _make_numeric_slider(self, is_int: bool, mn: float, mx: float,
                             step: float, value: float,
                             default_value: Optional[float] = None) -> "QSlider":
        """A global-styled slider (ToposClickableSlider — same look as the color
        page). No inline stylesheet, so the app-wide QSlider QSS applies. The
        param's default is wired as the slider's double-click-reset target.

        Composite rows enable ``enable_velocity_drag`` so dragging the handle
        honors the same default/Shift/Ctrl speed tiers as the DragValue box
        (clicking the empty groove still jumps — only handle-drag is throttled).
        """
        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.enable_velocity_drag = True
        if is_int:
            slider.setRange(int(mn), int(mx))
            slider.setValue(int(value))
            if default_value is not None:
                slider.set_default_value(int(default_value))
        else:
            steps = max(int((mx - mn) / (step or 1.0)), 1)
            slider.setRange(0, steps)
            slider.setValue(int((value - mn) / (step or 1.0)))
            if default_value is not None:
                slider.set_default_value(int((default_value - mn) / (step or 1.0)))
        return slider

    def _make_keyframe_button(self, key: str, pdef) -> QToolButton:
        kf_btn = _KeyframeToolButton()
        kf_btn.setText("◆")
        kf_btn.setToolTip(I18N.tr(
            "node_props.kf_btn_tooltip",
            "左键：切换关键帧\nAlt+左键：编辑表达式\n右键：曲线编辑器"))
        kf_btn.setFixedSize(20, 20)
        kf_btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999; border-radius: 2px;
            }
            QToolButton:hover {
                background-color: #555; color: #cc6600; border-color: #cc6600;
            }
            QToolButton:checked {
                background-color: #ff5555; color: #fff; border-color: #ff5555;
            }
        """)
        # AE 语义：红色（checked）仅表示**当前播放帧**上有关键帧，而不是
        # 参数存在任意关键帧。无绑定的 NodeGraph 时退回旧行为（任意关键帧）。
        if self._node_graph is not None:
            has_kf = self._param_has_keyframe_at_frame(key)
        else:
            has_kf = bool(pdef and pdef.animated and pdef.keyframes)
        kf_btn.setCheckable(True)
        kf_btn.setChecked(has_kf)

        # F1: left-click toggles a keyframe at the current frame (AE/Nuke
        # stopwatch). Right-click opens the advanced editor / clears the track.
        # AUD-10700: bind the owning node at build time so the ◆ button of a
        # non-first section routes to that node.
        def _toggle(_checked=False, k=key, pd=pdef, n=self._current_node_id) -> None:
            self._toggle_keyframe_at_current_frame(k, pd, n)

        def _show_menu(pos, k=key, pd=pdef, btn=kf_btn,
                       n=self._current_node_id) -> None:
            # Sync param_def from the node so the editor opens with real data.
            self._sync_param_def_from_node(n, k, pd)
            actions = []
            if getattr(pd, "ptype", "") in {"float", "int"}:
                actions.append((
                    "打开曲线编辑器…",
                    lambda k=k, pd=pd, n=n: self._show_keyframe_editor(k, pd, n)))
            # Color tracks are edited atomically through the color picker and
            # the timeline diamond; a scalar curve editor cannot represent an
            # RGBA value without silently dropping three channels.
            actions.append((
                "删除所有关键帧",
                lambda k=k, pd=pd, n=n: self._on_keyframes_changed(k, pd, [], n)))
            self._show_kf_btn_context_menu(
                btn.mapToGlobal(pos),
                actions,
            )

        kf_btn.clicked.connect(_toggle)
        if (pdef is not None
                and getattr(pdef, "allow_expression", False)
                and getattr(pdef, "is_expression_compatible", lambda: False)()):
            kf_btn.alt_clicked.connect(
                lambda k=key, pd=pdef, n=self._current_node_id:
                self._show_expression_editor(k, pd, n))
        kf_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        kf_btn.customContextMenuRequested.connect(_show_menu)
        # Obey Image Color mode: newly-built buttons start hidden/disabled.
        if not getattr(self, "_keyframe_buttons_visible", True):
            kf_btn.setVisible(False)
            kf_btn.setEnabled(False)
        return kf_btn

    def _make_param_label(self, text: str) -> "_ParamLabel":
        label = _ParamLabel(text)
        label.setStyleSheet("color: #bbb; font-size: 10px;")
        # Uniform minimum width (computed per-node in _build_parameters) so all
        # input widgets — DragValue, ComboBox, SpinBox — start at the same
        # horizontal position regardless of row type.
        label.setMinimumWidth(self._label_min_width)
        # Never grow horizontally — the slider/value box absorb extra width, not
        # the label (keeps labels aligned when the panel is wide or the slider
        # is hidden).
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        return label

    def _create_combo_row(self, key: str, value: Any, options, pdef) -> QWidget:
        """Horizontal combo row: ``[label] [ComboBox(stretch)]``.

        Matches the numeric row layout so the ComboBox left-edge aligns with
        DragValue left-edges (same _ParamLabel width), and the combo stretches
        to fill the remaining width for a clean right edge.
        Double-click label resets to default (same as numeric rows).
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(key))
        if pdef and pdef.default is not None:
            label.doubleClicked.connect(
                lambda _=False, k=key, d=pdef.default, n=self._current_node_id:
                self._on_param_changed(k, d, n))
        h.addWidget(label)

        combo = ChevronComboBox()
        # Display translated labels but keep `options` (raw keys) as the source
        # of truth for storage/serialization. currentIndexChanged returns us
        # the index, which we map back to the raw key via the options list.
        node_type = self._current_node_type or ""
        display_items = [self._option_display_label(node_type, key, str(o))
                         for o in options]
        combo.addItems(display_items)
        if value in options:
            combo.setCurrentIndex(options.index(value))
        apply_combo_dropdown_style(combo)
        combo.currentIndexChanged.connect(
            lambda idx, k=key, opts=list(options), n=self._current_node_id:
                self._on_param_changed(k, opts[idx], n) if 0 <= idx < len(opts) else None
        )
        h.addWidget(combo, 1)

        info: Dict[str, Any] = {
            'type': 'combo', 'widget': combo, 'options': list(options),
            'animatable': bool(pdef and pdef.is_animatable()),
        }
        if info['animatable']:
            kf_btn = self._make_keyframe_button(key, pdef)
            h.addWidget(kf_btn)
            info['keyframe_btn'] = kf_btn
            info['keyframe_btn_keys'] = [key]
        self._param_widgets[key] = info
        return row

    def _create_bool_row(self, key: str, value: bool, pdef) -> QWidget:
        """Horizontal bool row: ``[label] [QCheckBox(no text)] [stretch]``.

        单行紧凑布局：标签左对齐（与 DragValue/combo 同 _ParamLabel 宽度），
        右侧一个无文字的 checkbox（避免与左侧标签重复显示参数名，旧实现
        占两行——标签行 + 带 text 的 checkbox 行）。double-click 标签重置默认值。
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(key))
        if pdef and pdef.default is not None:
            label.doubleClicked.connect(
                lambda _=False, k=key, d=pdef.default, n=self._current_node_id:
                self._on_param_changed(k, d, n))
        h.addWidget(label)

        checkbox = QCheckBox()
        checkbox.setChecked(bool(value))
        checkbox.setText("")  # 无文字：标签已在左侧，避免重复占两行
        checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        checkbox.toggled.connect(
            lambda checked, k=key, n=self._current_node_id:
            self._on_param_changed(k, checked, n)
        )
        h.addWidget(checkbox)
        h.addStretch(1)

        info: Dict[str, Any] = {
            'type': 'bool', 'widget': checkbox,
            'animatable': bool(pdef and pdef.is_animatable()),
        }
        if info['animatable']:
            kf_btn = self._make_keyframe_button(key, pdef)
            h.addWidget(kf_btn)
            info['keyframe_btn'] = kf_btn
            info['keyframe_btn_keys'] = [key]
        self._param_widgets[key] = info
        return row

    def _create_channel_combo_row(self, key: str, value: str, pdef) -> QWidget:
        """Horizontal channel-selector row: ``[label] [ComboBox(stretch)]``.

        Same alignment layout as ``_create_combo_row`` — the channel combo
        left-edge aligns with DragValue left-edges across all row types.
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(key))
        if pdef and pdef.default is not None:
            label.doubleClicked.connect(
                lambda _=False, k=key, d=pdef.default, n=self._current_node_id:
                self._on_param_changed(k, d, n))
        h.addWidget(label)

        combo = self._create_channel_selector(key, value)
        h.addWidget(combo, 1)

        self._param_widgets[key] = {'type': 'channel', 'widget': combo}
        return row

    def _create_numeric_row(self, key: str, value: Any, pdef,
                            slider_holder: Optional[list] = None,
                            label_menu: Optional[tuple] = None) -> QWidget:
        """Horizontal numeric row: ``[label] [slider?] [DragValue] [◆?]``.

        - label: double-click resets to default; right-click opens
          ``label_menu`` (the X/Y "Combine" menu when this is a split half).
        - slider: only on single rows; created hidden and registered on
          ``slider_holder`` so the section toggles it by available width.
        - DragValue: the primary control (drag-scrub / double-click-type).
        """
        is_int, mn, mx, step, dec = self._numeric_meta(value, pdef)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(key))
        if pdef and pdef.default is not None:
            label.doubleClicked.connect(
                lambda _=False, k=key, d=pdef.default, n=self._current_node_id:
                self._on_param_changed(k, d, n))
        split_base = label_menu[1] if label_menu is not None else None
        label.set_context_menu_builder(
            lambda gp, n=self._current_node_id, k=key, pd=pdef, b=split_base:
            self._show_parameter_context_menu(n, [(k, pd, "")], gp, b))
        label.setToolTip(I18N.tr("node_props.expr_dim_menu_tooltip",
                                 "右键打开表达式/维度菜单"))
        h.addWidget(label)

        drag = DragValue("", mn, mx, float(value), decimals=dec)
        drag.setSingleStep(step)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

        slider = None  # only created when slider_holder is provided
        info: Dict[str, Any] = {
            'type': 'int' if is_int else 'float',
            'widget': drag,
            'min_val': mn, 'max_val': mx, 'step': step,
            'decimals': dec,
            'animatable': bool(pdef and pdef.is_animatable()),
        }

        # Optional responsive slider (single rows only).
        if slider_holder is not None:
            default_v = pdef.default if (pdef and pdef.default is not None) else None
            slider = self._make_numeric_slider(
                is_int, mn, mx, step, float(value), default_value=default_v)
            slider.setVisible(False)
            h.addWidget(slider, 1)

            def drag_changed(v, k=key, sl=slider, inf=info,
                             n=self._current_node_id) -> None:
                if self._block_signals:
                    return
                sl.blockSignals(True)
                sl.setValue(self._value_to_slider(v, inf))
                sl.blockSignals(False)
                self._on_param_changed(k, v, n)

            def slider_changed(v, k=key, d=drag,
                               n=self._current_node_id) -> None:
                if self._block_signals:
                    return
                fv = v if info['type'] == 'int' else v * info['step'] + info['min_val']
                d.blockSignals(True)
                d.setValue(fv)
                d.blockSignals(False)
                self._on_param_changed(k, fv, n)

            drag.valueChanged.connect(drag_changed)
            slider.valueChanged.connect(slider_changed)
            info['slider'] = slider
            slider_holder.append(slider)
        else:
            drag.valueChanged.connect(
                lambda v, k=key, n=self._current_node_id:
                self._on_param_changed(k, v, n))

        # P3: auto-key on commit (release / inline-edit submit), never on each
        # valueChanged tick. _autokey_if_animated is a no-op for un-animated
        # params, so this is safe to wire unconditionally.
        drag.interaction_ended.connect(
            lambda k=key, d=drag, pd=pdef, n=self._current_node_id:
            self._autokey_if_animated(k, d.value(), pd, n))
        if slider is not None:
            slider.sliderReleased.connect(
                lambda k=key, d=drag, pd=pdef, n=self._current_node_id:
                self._autokey_if_animated(k, d.value(), pd, n))

        h.addWidget(drag)

        if info['animatable']:
            kf_btn = self._make_keyframe_button(key, pdef)
            h.addWidget(kf_btn)
            info['keyframe_btn'] = kf_btn
            info['keyframe_btn_keys'] = [key]

        self._param_widgets[key] = info
        return row

    def _show_expression_editor(self, param_name: str, param_def: Any,
                               node_id: Optional[str] = None) -> None:
        """Open a small, validated expression editor for one parameter.

        Validation happens before the value reaches the graph. This keeps the
        node graph and the renderer free of half-written expressions while
        still allowing an explicit empty value to clear the expression.
        ``node_id`` routes the dialog to the owning node (AUD-10700).
        """
        node_id = node_id if node_id is not None else self._current_node_id
        if not node_id:
            return
        node = self._get_live_node(node_id)
        current = node.get_expression(param_name) if node else None

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Expression - {param_name}")
        dialog.setMinimumSize(520, 260)
        dialog.setStyleSheet("""
            QDialog { background-color: #2d2d30; }
            QLabel { color: #c9c9c9; }
            QPlainTextEdit {
                background: #17181c; color: #e6e6e6; border: 1px solid #555;
                border-radius: 3px; padding: 5px; font-family: monospace;
            }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)

        hint = getattr(param_def, "expression_hint", None) or (
            "示例：value + 10、linear(time, 0, 2, 0, 100)、wiggle(1, 20)"
        )
        hint_label = QLabel(f"{param_name}  ·  {hint}")
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet("color: #999; font-size: 10px;")
        layout.addWidget(hint_label)

        editor = QPlainTextEdit()
        editor.setPlaceholderText(I18N.tr(
            "node_props.expr_editor_placeholder",
            "输入表达式；留空后点击“清除”可移除表达式"))
        editor.setPlainText(current or "")
        layout.addWidget(editor, 1)

        status = QLabel("")
        status.setWordWrap(True)
        status.setStyleSheet("color: #ff8a80; font-size: 10px;")
        layout.addWidget(status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        clear_btn = buttons.addButton("清除表达式", QDialogButtonBox.ButtonRole.DestructiveRole)
        layout.addWidget(buttons)

        from src.features.composite.utils.expression_parser import ExpressionParser
        parser = ExpressionParser()

        def validate() -> bool:
            expression = editor.toPlainText().strip()
            if not expression:
                status.clear()
                return True
            result = parser.validate(expression)
            if not result.success:
                status.setText(result.error.message if result.error else "表达式无效")
                return False
            status.setStyleSheet("color: #7bd88f; font-size: 10px;")
            status.setText(I18N.tr("node_props.expr_valid", "表达式语法有效"))
            return True

        def accept() -> None:
            if validate():
                dialog.accept()

        def clear_expression() -> None:
            editor.clear()
            self.expression_changed.emit(node_id, param_name, None)
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        clear_btn.clicked.connect(clear_expression)

        if dialog.exec() == QDialog.DialogCode.Accepted and editor.toPlainText().strip():
            self.expression_changed.emit(
                node_id, param_name, editor.toPlainText().strip())

    def _create_xy_param_row(self, base: str, x_key: str, y_key: str,
                             x_val: Any, y_val: Any, pdef_x, pdef_y,
                             slider_holder: Optional[list] = None,
                             label_menu: Optional[tuple] = None) -> QWidget:
        """X/Y row: ``[label] [⛓?] [x slider?] [x value] [y slider?] [y value] [◆]``.

        - label: double-click resets both axes; right-click opens the X/Y
          "Separate Dimensions" menu (``label_menu``).
        - chain toggle: only on ``scale`` (default linked), placed left of the
          value boxes; links X↔Y so changing one applies to the other.
        - per-axis responsive slider + fixed-width value box.
        - ONE shared keyframe button for the pair (AE-style: a multi-dim
          property has a single stopwatch).
        """
        # pivot 行特判：pivot_mode="center" 时旋转中心由渲染端解析为图像中心，
        # 参数里的 pivot_x/pivot_y 不参与计算（恒为 0.0）。为了让面板显示与
        # 实际行为一致，此时把 pivot_x/y 的显示值替换成图像中心并置灰只读；
        # 切到 "custom" 才恢复可编辑（由 _refresh_pivot_display 联动）。
        pivot_center_readonly = False
        if base == "pivot" and self._current_parameters.get("pivot_mode", "center") == "center":
            img = self._image_sizes.get(self._current_node_id)
            if img and img[0] > 0 and img[1] > 0:
                x_val, y_val = img[0] / 2.0, img[1] / 2.0
                pivot_center_readonly = True

        is_int_x, mn_x, mx_x, step_x, dec_x = self._numeric_meta(x_val, pdef_x)
        is_int_y, mn_y, mx_y, step_y, dec_y = self._numeric_meta(y_val, pdef_y)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(base))

        def reset_both(_=False, n=self._current_node_id) -> None:
            if pdef_x and pdef_x.default is not None:
                self._on_param_changed(x_key, pdef_x.default, n)
            if pdef_y and pdef_y.default is not None:
                self._on_param_changed(y_key, pdef_y.default, n)
        label.doubleClicked.connect(reset_both)
        split_base = label_menu[1] if label_menu is not None else None
        label.set_context_menu_builder(
            lambda gp, n=self._current_node_id, xs=x_key, ys=y_key,
                   px=pdef_x, py=pdef_y, b=split_base:
            self._show_parameter_context_menu(
                n, [(xs, px, "X"), (ys, py, "Y")], gp, b))
        label.setToolTip(I18N.tr("node_props.expr_dim_menu_tooltip",
                                 "右键打开表达式/维度菜单"))
        h.addWidget(label)

        # Chain toggle — scale/radius (linked by default) and Constant size
        # (aspect ratio lock, off by default). Placed left of the value boxes.
        # 统一小锁图标：锁定=橙 / 解锁=灰（见 icon_manager.make_link_button）。
        chain = None
        if base in ("scale", "size", "radius"):
            chain = make_link_button(
                initial_linked=(base in ("scale", "radius")),  # linked ON by default
                tooltip=("Link X and Y" if base in ("scale", "radius")
                         else "Link width and height aspect ratio"),
            )
            h.addWidget(chain)

        def axis_marker(letter: str) -> QLabel:
            m = QLabel(letter)
            m.setStyleSheet("color: #888; font-size: 10px;")
            m.setFixedWidth(10)
            m.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return m

        def build_axis(key, val, pdef, is_int, mn, mx, step, dec, letter):
            info: Dict[str, Any] = {
                'type': 'int' if is_int else 'float', 'widget': None,
                'min_val': mn, 'max_val': mx, 'step': step, 'decimals': dec,
                'animatable': bool(pdef and pdef.is_animatable()),
            }
            slider = None
            if slider_holder is not None:
                default_v = pdef.default if (pdef and pdef.default is not None) else None
                slider = self._make_numeric_slider(
                    is_int, mn, mx, step, float(val), default_value=default_v)
                slider.setVisible(False)
                h.addWidget(slider, 1)
                info['slider'] = slider
                slider_holder.append(slider)
            h.addWidget(axis_marker(letter))
            drag = DragValue("", mn, mx, float(val), decimals=dec)
            drag.setSingleStep(step)
            drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
            h.addWidget(drag)
            info['widget'] = drag
            # pivot center 模式：drag/slider 置灰只读，避免误写不参与计算的参数。
            if pivot_center_readonly:
                drag.setEnabled(False)
                if slider is not None:
                    slider.setEnabled(False)
                info['pivot_center_readonly'] = True
            return drag, slider, info

        drag_x, slider_x, info_x = build_axis(
            x_key, x_val, pdef_x, is_int_x, mn_x, mx_x, step_x, dec_x, "x")
        drag_y, slider_y, info_y = build_axis(
            y_key, y_val, pdef_y, is_int_y, mn_y, mx_y, step_y, dec_y, "y")

        # Link semantics: scale links 1:1 (X == Y), size links by aspect ratio
        # (the OTHER axis = this axis * captured ratio). For size we keep a
        # mutable ratio holder so it can be re-captured each time the lock is
        # engaged (toggling on at 1080×1920 locks 9:16, not the initial ratio).
        link_mode = "aspect" if base == "size" else "equal"
        aspect_ratio = None
        if link_mode == "aspect":
            aspect_ratio = [float(y_val) / float(x_val)] if x_val else [1.0]
            # Publish the ratio holder on the owning section so external flows
            # (e.g. the Constant size-preset combo) can re-capture the live
            # ratio via _recapture_constant_aspect after setting a new size.
            section = self._sections.get(self._current_node_id)
            if section is not None:
                setattr(section, "_constant_aspect_ratio", aspect_ratio)
            # Re-capture from the live values whenever the lock engages.
            def _on_chain_toggled_size(checked, ax=drag_x, ay=drag_y, ar=aspect_ratio) -> None:
                if checked and ax.value() > 0:
                    ar[0] = float(ay.value()) / float(ax.value())
            chain.toggled.connect(_on_chain_toggled_size)

        def compute_peer_value(this_val: float, this_is_x: bool) -> float:
            """Value to push to the peer axis when the link is engaged."""
            if link_mode == "equal":
                return this_val
            # aspect mode: peer = this * ratio (x→y) or this / ratio (y→x)
            r = aspect_ratio[0]
            if r <= 0:
                return this_val
            return this_val * r if this_is_x else this_val / r

        def push_to_peer(peer_drag, peer_slider, peer_info, peer_key,
                         peer_value, peer_min, peer_max,
                         n=self._current_node_id) -> None:
            """Clamp + set the peer drag AND slider, then emit its param change.

            Both the numeric drag and the slider must stay in sync — otherwise
            the peer axis shows the new value in its drag box while its slider
            thumb stays at the old position.
            """
            pv = max(float(peer_min), min(float(peer_max), float(peer_value)))
            peer_drag.blockSignals(True)
            peer_drag.setValue(pv)
            peer_drag.blockSignals(False)
            if peer_slider is not None:
                peer_slider.blockSignals(True)
                peer_slider.setValue(self._value_to_slider(pv, peer_info))
                peer_slider.blockSignals(False)
            self._on_param_changed(peer_key,
                                   int(pv) if isinstance(peer_value, int) else pv, n)

        def wire(drag, slider, info, key, peer_drag, peer_slider, peer_info, peer_key,
                 this_is_x: bool, peer_min: float, peer_max: float,
                 n=self._current_node_id) -> None:
            def drag_changed(v) -> None:
                if self._block_signals:
                    return
                if slider is not None:
                    slider.blockSignals(True)
                    slider.setValue(self._value_to_slider(v, info))
                    slider.blockSignals(False)
                self._on_param_changed(key, v, n)
                if chain is not None and chain.isChecked():
                    peer_val = compute_peer_value(v, this_is_x)
                    push_to_peer(peer_drag, peer_slider, peer_info, peer_key,
                                 peer_val, peer_min, peer_max, n)

            def slider_changed(v) -> None:
                if self._block_signals:
                    return
                fv = v if info['type'] == 'int' else v * info['step'] + info['min_val']
                drag.blockSignals(True)
                drag.setValue(fv)
                drag.blockSignals(False)
                self._on_param_changed(key, fv, n)
                if chain is not None and chain.isChecked():
                    peer_val = compute_peer_value(fv, this_is_x)
                    push_to_peer(peer_drag, peer_slider, peer_info, peer_key,
                                 peer_val, peer_min, peer_max, n)

            drag.valueChanged.connect(drag_changed)
            if slider is not None:
                slider.valueChanged.connect(slider_changed)

        wire(drag_x, slider_x, info_x, x_key, drag_y, slider_y, info_y, y_key,
             this_is_x=True, peer_min=mn_y, peer_max=mx_y)
        wire(drag_y, slider_y, info_y, y_key, drag_x, slider_x, info_x, x_key,
             this_is_x=False, peer_min=mn_x, peer_max=mx_x)

        # P3: auto-key on commit for each axis. No-op unless the axis already
        # has a keyframe track, so static pairs are unaffected.
        drag_x.interaction_ended.connect(
            lambda k=x_key, d=drag_x, pd=pdef_x, n=self._current_node_id:
            self._autokey_if_animated(k, d.value(), pd, n))
        drag_y.interaction_ended.connect(
            lambda k=y_key, d=drag_y, pd=pdef_y, n=self._current_node_id:
            self._autokey_if_animated(k, d.value(), pd, n))
        if slider_x is not None:
            slider_x.sliderReleased.connect(
                lambda k=x_key, d=drag_x, pd=pdef_x, n=self._current_node_id:
                self._autokey_if_animated(k, d.value(), pd, n))
        if slider_y is not None:
            slider_y.sliderReleased.connect(
                lambda k=y_key, d=drag_y, pd=pdef_y, n=self._current_node_id:
                self._autokey_if_animated(k, d.value(), pd, n))

        # One shared keyframe button for the pair (AE-style). Stored under both
        # axes so the refresh path + state update reach it from either key.
        if info_x['animatable'] or info_y['animatable']:
            kf = self._make_pair_keyframe_button(x_key, y_key, pdef_x, pdef_y)
            h.addWidget(kf)
            info_x['keyframe_btn'] = kf
            info_y['keyframe_btn'] = kf
            info_x['keyframe_btn_keys'] = [x_key, y_key]
            info_y['keyframe_btn_keys'] = [x_key, y_key]

        self._param_widgets[x_key] = info_x
        self._param_widgets[y_key] = info_y
        return row

    def _create_xyz_param_row(self, base: str, x_key: str, y_key: str, z_key: str,
                              x_val: Any, y_val: Any, z_val: Any,
                              pdef_x, pdef_y, pdef_z,
                              slider_holder: Optional[list] = None,
                              label_menu: Optional[tuple] = None) -> QWidget:
        """X/Y/Z row: ``[label] [⛓?] [x slider?] x[v] y[v] z slider?] z[v] [◆]``.

        三轴紧凑行：把 _x/_y/_z 合并到一行（变换类参数如 object_pos/rot）。
        复用 _create_xy_param_row 的 axis 构建逻辑，扩展到 Z 轴。chain toggle
        仅 scale/radius（Z=X=Y 等比）。Separate Dimensions 右键菜单同 XY。
        """
        is_int_x, mn_x, mx_x, step_x, dec_x = self._numeric_meta(x_val, pdef_x)
        is_int_y, mn_y, mx_y, step_y, dec_y = self._numeric_meta(y_val, pdef_y)
        is_int_z, mn_z, mx_z, step_z, dec_z = self._numeric_meta(z_val, pdef_z)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(base))

        def reset_all(_=False, n=self._current_node_id) -> None:
            for pdef, k in ((pdef_x, x_key), (pdef_y, y_key), (pdef_z, z_key)):
                if pdef and pdef.default is not None:
                    self._on_param_changed(k, pdef.default, n)
        label.doubleClicked.connect(reset_all)
        split_base = label_menu[1] if label_menu is not None else None
        label.set_context_menu_builder(
            lambda gp, n=self._current_node_id, xs=x_key, ys=y_key, zs=z_key,
                   px=pdef_x, py=pdef_y, pz=pdef_z, b=split_base:
            self._show_parameter_context_menu(
                n, [(xs, px, "X"), (ys, py, "Y"), (zs, pz, "Z")], gp, b))
        label.setToolTip(I18N.tr("node_props.expr_dim_menu_tooltip",
                                 "右键打开表达式/维度菜单"))
        h.addWidget(label)

        # Chain toggle — scale/radius（三轴等比联动）。其余变换无 chain。
        # 统一小锁图标：锁定=橙 / 解锁=灰。
        chain = None
        if base in ("scale", "radius"):
            chain = make_link_button(
                initial_linked=True,
                tooltip="Link X, Y and Z",
            )
            h.addWidget(chain)

        def axis_marker(letter: str) -> QLabel:
            m = QLabel(letter)
            m.setStyleSheet("color: #888; font-size: 10px;")
            m.setFixedWidth(10)
            m.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return m

        def build_axis(key, val, pdef, is_int, mn, mx, step, dec, letter):
            info: Dict[str, Any] = {
                'type': 'int' if is_int else 'float', 'widget': None,
                'min_val': mn, 'max_val': mx, 'step': step, 'decimals': dec,
                'animatable': bool(pdef and pdef.is_animatable()),
            }
            slider = None
            if slider_holder is not None:
                default_v = pdef.default if (pdef and pdef.default is not None) else None
                slider = self._make_numeric_slider(
                    is_int, mn, mx, step, float(val), default_value=default_v)
                slider.setVisible(False)
                h.addWidget(slider, 1)
                info['slider'] = slider
                slider_holder.append(slider)
            h.addWidget(axis_marker(letter))
            drag = DragValue("", mn, mx, float(val), decimals=dec)
            drag.setSingleStep(step)
            drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
            h.addWidget(drag)
            info['widget'] = drag
            return drag, slider, info

        drag_x, slider_x, info_x = build_axis(
            x_key, x_val, pdef_x, is_int_x, mn_x, mx_x, step_x, dec_x, "x")
        drag_y, slider_y, info_y = build_axis(
            y_key, y_val, pdef_y, is_int_y, mn_y, mx_y, step_y, dec_y, "y")
        drag_z, slider_z, info_z = build_axis(
            z_key, z_val, pdef_z, is_int_z, mn_z, mx_z, step_z, dec_z, "z")

        # Chain 联动：改任一轴 → 其余两轴同步（scale 等比）
        if chain is not None:
            def _on_chain_changed(src_drag, src_key, val,
                                  n=self._current_node_id) -> None:
                if chain.isChecked():
                    for d, k in ((drag_x, x_key), (drag_y, y_key), (drag_z, z_key)):
                        if d is not src_drag:
                            d.blockSignals(True)
                            d.setValue(val)
                            d.blockSignals(False)
                            self._on_param_changed(k, val, n)
            drag_x.valueChanged.connect(lambda v, d=drag_x: _on_chain_changed(d, x_key, v))
            drag_y.valueChanged.connect(lambda v, d=drag_y: _on_chain_changed(d, y_key, v))
            drag_z.valueChanged.connect(lambda v, d=drag_z: _on_chain_changed(d, z_key, v))

        # P3: auto-key on commit for each of the three axes (no-op unless the
        # axis already has a keyframe track).
        for _d, _k, _pd in ((drag_x, x_key, pdef_x),
                            (drag_y, y_key, pdef_y),
                            (drag_z, z_key, pdef_z)):
            _d.interaction_ended.connect(
                lambda k=_k, d=_d, pd=_pd, n=self._current_node_id:
                self._autokey_if_animated(k, d.value(), pd, n))

        # 共享 keyframe 按钮（多轴属性一个 stopwatch）
        if info_x['animatable'] or info_y['animatable'] or info_z['animatable']:
            kf = self._make_triple_keyframe_button(x_key, y_key, z_key,
                                                    pdef_x, pdef_y, pdef_z)
            h.addWidget(kf)
            info_x['keyframe_btn'] = kf
            info_y['keyframe_btn'] = kf
            info_z['keyframe_btn'] = kf
            for _info in (info_x, info_y, info_z):
                _info['keyframe_btn_keys'] = [x_key, y_key, z_key]

        self._param_widgets[x_key] = info_x
        self._param_widgets[y_key] = info_y
        self._param_widgets[z_key] = info_z
        return row

    def _create_rgb_param_row(self, base: str, r_key: str, g_key: str, b_key: str,
                              r_val: Any, g_val: Any, b_val: Any,
                              pdef_r, pdef_g, pdef_b) -> QWidget:
        """RGB color row: ``[label] [■色块] R[v] G[v] B[v]``.

        把 *_color_r/g/b 三个 0-1 标量合并到一行：左侧色块按钮（点击弹项目
        统一色彩选择器 CompactColorStudio，实时预览），右侧 3 个紧凑 DragValue。
        三个子控件按各自 key 注册，_update_section_param_value 天然兼容。
        """
        from src.shared.color import get_color

        def _clamp01(v):
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.0

        rv0, gv0, bv0 = _clamp01(r_val), _clamp01(g_val), _clamp01(b_val)

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(self._param_display_name(base))

        def reset_color(_=False, n=self._current_node_id) -> None:
            for pdef, k in ((pdef_r, r_key), (pdef_g, g_key), (pdef_b, b_key)):
                if pdef and pdef.default is not None:
                    self._on_param_changed(k, pdef.default, n)
        label.doubleClicked.connect(reset_color)
        label.set_context_menu_builder(
            lambda gp, n=self._current_node_id, rs=r_key, gs=g_key, bs=b_key,
                   pr=pdef_r, pg=pdef_g, pb=pdef_b:
            self._show_parameter_context_menu(
                n, [(rs, pr, "R"), (gs, pg, "G"), (bs, pb, "B")], gp))
        label.setToolTip(I18N.tr("node_props.expr_menu_tooltip",
                                 "右键打开表达式菜单"))
        h.addWidget(label)

        # ── 色块按钮（点击弹项目色彩选择器）──
        color_btn = QPushButton()
        color_btn.setFixedSize(28, 20)
        color_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # AUD-10707: ``*_color_r/g/b`` parameters are consumed by the
        # scene-linear graph (GLSL effects / 3D lighting — same as Constant's
        # color channels), while QColor carries sRGB display codes. Convert
        # exactly once at this UI→graph boundary, in both directions, so the
        # stored params and the picked appearance stay consistent.
        from src.shared.ui.widgets.color_picker_control import (
            linear_rgba_to_qcolor,
            qcolor_to_linear_rgba,
        )

        def _params_to_qcolor(r, g, b) -> QColor:
            return linear_rgba_to_qcolor((r, g, b, 1.0))

        def _swatch_css(r, g, b):
            qc = _params_to_qcolor(_clamp01(r), _clamp01(g), _clamp01(b))
            return (f"QPushButton{{background-color:{qc.name()};border:1px solid #555;"
                    f"border-radius:3px;}}")

        def _refresh_swatch(n=self._current_node_id) -> None:
            params = self._routed_parameters(n)
            r = _clamp01(params.get(r_key, 0.0))
            g = _clamp01(params.get(g_key, 0.0))
            b = _clamp01(params.get(b_key, 0.0))
            color_btn.setStyleSheet(_swatch_css(r, g, b))

        _refresh_swatch()

        def _pick_color(_checked=False, n=self._current_node_id) -> None:
            params = self._routed_parameters(n)
            r = _clamp01(params.get(r_key, 0.0))
            g = _clamp01(params.get(g_key, 0.0))
            b = _clamp01(params.get(b_key, 0.0))
            initial = _params_to_qcolor(r, g, b)

            def _on_live(color: QColor) -> None:
                # 实时预览：拖动选色时实时写 r/g/b（线性域，见 AUD-10707）
                lr, lg, lb, _a = qcolor_to_linear_rgba(color)
                self._on_param_changed(r_key, lr, n)
                self._on_param_changed(g_key, lg, n)
                self._on_param_changed(b_key, lb, n)
                _refresh_swatch(n)

            result = get_color(parent=self, initial=initial, on_live=_on_live)
            if result is not None:
                lr, lg, lb, _a = qcolor_to_linear_rgba(result)
                self._on_param_changed(r_key, lr, n)
                self._on_param_changed(g_key, lg, n)
                self._on_param_changed(b_key, lb, n)
            _refresh_swatch(n)

        color_btn.clicked.connect(_pick_color)
        h.addWidget(color_btn)

        # ── 3 个紧凑 DragValue（R/G/B，0-1，无 slider 保紧凑）──
        def build_channel(key, val, pdef, letter):
            drag = DragValue("", 0.0, 1.0, float(val), decimals=3)
            drag.setSingleStep(0.01)
            drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
            h.addWidget(axis_marker_rgb(letter))
            h.addWidget(drag)
            info: Dict[str, Any] = {
                'type': 'float', 'widget': drag,
                'min_val': 0.0, 'max_val': 1.0, 'step': 0.01, 'decimals': 3,
                'animatable': bool(pdef and pdef.is_animatable()),
            }
            # 改值时刷新色块
            drag.valueChanged.connect(lambda _v: _refresh_swatch())
            return drag, info

        def axis_marker_rgb(letter: str) -> QLabel:
            m = QLabel(letter)
            m.setStyleSheet("color: #888; font-size: 10px;")
            m.setFixedWidth(10)
            m.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return m

        drag_r, info_r = build_channel(r_key, rv0, pdef_r, "R")
        drag_g, info_g = build_channel(g_key, gv0, pdef_g, "G")
        drag_b, info_b = build_channel(b_key, bv0, pdef_b, "B")

        h.addStretch(1)

        self._param_widgets[r_key] = info_r
        self._param_widgets[g_key] = info_g
        self._param_widgets[b_key] = info_b
        return row

    def _make_triple_keyframe_button(self, x_key: str, y_key: str, z_key: str,
                                     pdef_x, pdef_y, pdef_z) -> QToolButton:
        """One keyframe ◆ button shared by an X/Y/Z triple (mirrors
        _make_pair_keyframe_button for 3 axes)."""
        btn = _KeyframeToolButton()
        btn.setText("◆")
        btn.setToolTip(I18N.tr(
            "node_props.kf_btn_tooltip_xyz",
            "左键：切换三轴关键帧\nAlt+左键：编辑 X 表达式\n右键：曲线编辑器"))
        btn.setFixedSize(20, 20)
        btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999; border-radius: 2px;
            }
            QToolButton:hover {
                background-color: #555; color: #cc6600; border-color: #cc6600;
            }
            QToolButton:checked {
                background-color: #ff5555; color: #fff; border-color: #ff5555;
            }
        """)
        # AE 语义：红色仅表示当前播放帧上有关键帧（任一轴）。
        if self._node_graph is not None:
            has_kf = (self._param_has_keyframe_at_frame(x_key)
                      or self._param_has_keyframe_at_frame(y_key)
                      or self._param_has_keyframe_at_frame(z_key))
        else:
            has_kf = bool(
                (pdef_x and getattr(pdef_x, 'animated', False) and getattr(pdef_x, 'keyframes', None))
                or (pdef_y and getattr(pdef_y, 'animated', False) and getattr(pdef_y, 'keyframes', None))
                or (pdef_z and getattr(pdef_z, 'animated', False) and getattr(pdef_z, 'keyframes', None))
            )
        btn.setCheckable(True)
        btn.setChecked(has_kf)

        keys = [x_key, y_key, z_key]
        pdefs = [pdef_x, pdef_y, pdef_z]

        # F1/F2: left-click toggles a keyframe at the current frame on every
        # axis (replaces the previous call to the undefined
        # _open_multi_keyframe_editor, which crashed on click). Right-click
        # opens the X-axis editor / clears all three tracks.
        # AUD-10700: bind the owning node at build time (see _make_keyframe_button).
        def _toggle(_checked=False, ks=keys, ps=pdefs, n=self._current_node_id) -> None:
            self._toggle_multi_axis_keyframe(ks, ps, n)

        def _show_menu(pos, ks=keys, ps=pdefs, n=self._current_node_id) -> None:
            # Open the single-param editor on X as the representative axis.
            self._sync_param_def_from_node(n, ks[0], ps[0])
            actions = [
                ("打开曲线编辑器… (X 轴)",
                 lambda n=n: self._show_keyframe_editor(ks[0], ps[0], n)),
                ("删除所有关键帧",
                 lambda n=n: [self._on_keyframes_changed(k, p, [], n)
                              for k, p in zip(ks, ps)]),
            ]
            self._show_kf_btn_context_menu(btn.mapToGlobal(pos), actions)

        btn.clicked.connect(_toggle)
        if (pdef_x is not None
                and getattr(pdef_x, "allow_expression", False)
                and getattr(pdef_x, "is_expression_compatible", lambda: False)()):
            btn.alt_clicked.connect(
                lambda k=x_key, pd=pdef_x, n=self._current_node_id:
                self._show_expression_editor(k, pd, n))
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(_show_menu)
        return btn

    def _create_param_row_for_section(self, node_id: str, key: str, value: Any,
                                       param_options: Dict, param_defs: Dict,
                                       section: "NodeSectionWidget",
                                       slider_holder: Optional[list] = None,
                                       label_menu: Optional[tuple] = None) -> QWidget:
        """Create a parameter row for a specific node section.

        Reuses _create_param_row by temporarily swapping panel context to the
        section's node, then re-routing the signal through the section widget.
        ``slider_holder`` (when given) collects single-row sliders so the section
        can toggle their visibility by available width. ``label_menu`` wires the
        param-name label's right-click menu (the "Combine" action for a split
        X/Y half).
        """
        # Save current state
        saved_node_id = self._current_node_id
        saved_node_type = self._current_node_type
        saved_params = self._current_parameters
        saved_defs = self._param_defs
        saved_options = self._param_options
        saved_widgets = self._param_widgets

        # AUD-10703: restore the swapped context in ``finally`` — a malformed
        # parameter value raising mid-build must not leave the panel routed to
        # this section's node with a gutted ``_param_widgets``.
        try:
            # Set context to section's node
            self._current_node_id = node_id
            self._current_node_type = getattr(section, 'node_type', None)
            self._current_parameters = section.parameters
            self._param_defs = param_defs
            self._param_options = param_options
            self._param_widgets = {}

            # Build the row using existing logic
            row = self._create_param_row(key, value, slider_holder=slider_holder,
                                         label_menu=label_menu)

            # Collect the widgets created for this row
            new_widgets = dict(self._param_widgets)
        finally:
            # Restore original state
            self._current_node_id = saved_node_id
            self._current_node_type = saved_node_type
            self._current_parameters = saved_params
            self._param_defs = saved_defs
            self._param_options = saved_options
            self._param_widgets = saved_widgets

        section._param_widgets.update(new_widgets)

        return row

    def _create_xy_param_row_for_section(self, node_id: str, base: str,
                                          x_key: str, y_key: str,
                                          x_val: Any, y_val: Any,
                                          param_options: Dict, param_defs: Dict,
                                          section: "NodeSectionWidget",
                                          slider_holder: Optional[list] = None,
                                          label_menu: Optional[tuple] = None) -> QWidget:
        """Create an X/Y paired row for a section (mirrors the context-swap of
        _create_param_row_for_section). ``label_menu`` wires the shared label's
        right-click "Separate Dimensions" menu."""
        saved_node_id = self._current_node_id
        saved_node_type = self._current_node_type
        saved_params = self._current_parameters
        saved_defs = self._param_defs
        saved_options = self._param_options
        saved_widgets = self._param_widgets

        # AUD-10703: try/finally context swap (see _create_param_row_for_section).
        try:
            self._current_node_id = node_id
            self._current_node_type = getattr(section, 'node_type', None)
            self._current_parameters = section.parameters
            self._param_defs = param_defs
            self._param_options = param_options
            self._param_widgets = {}

            row = self._create_xy_param_row(
                base, x_key, y_key, x_val, y_val,
                param_defs.get(x_key), param_defs.get(y_key),
                slider_holder=slider_holder, label_menu=label_menu)

            new_widgets = dict(self._param_widgets)
        finally:
            self._current_node_id = saved_node_id
            self._current_node_type = saved_node_type
            self._current_parameters = saved_params
            self._param_defs = saved_defs
            self._param_options = saved_options
            self._param_widgets = saved_widgets

        section._param_widgets.update(new_widgets)
        return row

    def _create_xyz_param_row_for_section(self, node_id: str, base: str,
                                          x_key: str, y_key: str, z_key: str,
                                          x_val: Any, y_val: Any, z_val: Any,
                                          param_options: Dict, param_defs: Dict,
                                          section: "NodeSectionWidget",
                                          slider_holder: Optional[list] = None,
                                          label_menu: Optional[tuple] = None) -> QWidget:
        """Create an X/Y/Z triple row for a section (mirrors the context-swap of
        _create_xy_param_row_for_section)."""
        saved_node_id = self._current_node_id
        saved_node_type = self._current_node_type
        saved_params = self._current_parameters
        saved_defs = self._param_defs
        saved_options = self._param_options
        saved_widgets = self._param_widgets

        # AUD-10703: try/finally context swap (see _create_param_row_for_section).
        try:
            self._current_node_id = node_id
            self._current_node_type = getattr(section, 'node_type', None)
            self._current_parameters = section.parameters
            self._param_defs = param_defs
            self._param_options = param_options
            self._param_widgets = {}

            row = self._create_xyz_param_row(
                base, x_key, y_key, z_key, x_val, y_val, z_val,
                param_defs.get(x_key), param_defs.get(y_key), param_defs.get(z_key),
                slider_holder=slider_holder, label_menu=label_menu)

            new_widgets = dict(self._param_widgets)
        finally:
            self._current_node_id = saved_node_id
            self._current_node_type = saved_node_type
            self._current_parameters = saved_params
            self._param_defs = saved_defs
            self._param_options = saved_options
            self._param_widgets = saved_widgets

        section._param_widgets.update(new_widgets)
        return row

    def _create_rgb_param_row_for_section(self, node_id: str, base: str,
                                          r_key: str, g_key: str, b_key: str,
                                          r_val: Any, g_val: Any, b_val: Any,
                                          param_options: Dict, param_defs: Dict,
                                          section: "NodeSectionWidget") -> QWidget:
        """Create an R/G/B color row for a section (mirrors the context-swap of
        _create_param_row_for_section)."""
        saved_node_id = self._current_node_id
        saved_node_type = self._current_node_type
        saved_params = self._current_parameters
        saved_defs = self._param_defs
        saved_options = self._param_options
        saved_widgets = self._param_widgets

        # AUD-10703: try/finally context swap (see _create_param_row_for_section).
        try:
            self._current_node_id = node_id
            self._current_node_type = getattr(section, 'node_type', None)
            self._current_parameters = section.parameters
            self._param_defs = param_defs
            self._param_options = param_options
            self._param_widgets = {}

            row = self._create_rgb_param_row(
                base, r_key, g_key, b_key, r_val, g_val, b_val,
                param_defs.get(r_key), param_defs.get(g_key), param_defs.get(b_key))

            new_widgets = dict(self._param_widgets)
        finally:
            self._current_node_id = saved_node_id
            self._current_node_type = saved_node_type
            self._current_parameters = saved_params
            self._param_defs = saved_defs
            self._param_options = saved_options
            self._param_widgets = saved_widgets

        section._param_widgets.update(new_widgets)
        return row

    def _update_section_param_value(self, key: str, value: Any,
                                     param_widgets: Dict) -> None:
        """Update a parameter widget value in a specific section."""
        if key not in param_widgets:
            return

        widget_info = param_widgets[key]
        widget_type = widget_info['type']

        if widget_type == 'combo':
            combo = widget_info['widget']
            options = widget_info.get('options', [])
            if value in options:
                combo.blockSignals(True)
                combo.setCurrentIndex(options.index(value))
                combo.blockSignals(False)
        elif widget_type == 'bool':
            checkbox = widget_info['widget']
            checkbox.blockSignals(True)
            checkbox.setChecked(bool(value))
            checkbox.blockSignals(False)
        elif widget_type in ('float', 'int'):
            drag = widget_info['widget']
            drag.blockSignals(True)
            drag.setValue(value)
            drag.blockSignals(False)
            slider = widget_info.get('slider')
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(self._value_to_slider(value, widget_info))
                slider.blockSignals(False)
        elif widget_type == 'checker':
            # Custom int-with-mapping: the incoming value is render-px; convert
            # back to the 1-100 UI scale before updating drag + slider.
            scale = widget_info.get('scale', 4) or 4
            try:
                rv = int(value)
            except (TypeError, ValueError):
                return
            ui_v = max(int(widget_info['min_val']),
                       min(int(widget_info['max_val']), round(rv / scale)))
            drag = widget_info['widget']
            drag.blockSignals(True)
            drag.setValue(float(ui_v))
            drag.blockSignals(False)
            slider = widget_info.get('slider')
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(ui_v)
                slider.blockSignals(False)
        elif widget_type == 'str':
            line_edit = widget_info['widget']
            line_edit.blockSignals(True)
            line_edit.setText(str(value))
            line_edit.blockSignals(False)
        elif widget_type == 'file':
            # AUD-10704: file picker container — the path lives in its
            # (read-only) line edit; without this branch external updates
            # (undo/redo, page write-backs) never refreshed the shown path.
            line_edit = widget_info['widget'].findChild(QLineEdit)
            if line_edit is not None:
                line_edit.blockSignals(True)
                line_edit.setText(str(value))
                line_edit.blockSignals(False)
        elif widget_type == 'hdri':
            # AUD-10704: HDRI preset combo — select the item whose data matches
            # ('' and '__sky__' both mean the default sky, mirroring the build
            # logic); append custom absolute paths not among the presets.
            combo = widget_info['widget'].findChild(QComboBox)
            if combo is not None:
                target = -1
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    if data == value or (data == "" and value == "__sky__"):
                        target = i
                        break
                if target < 0 and value not in ("", None, "__sky__"):
                    combo.blockSignals(True)
                    combo.addItem(str(value), value)
                    combo.blockSignals(False)
                    target = combo.count() - 1
                if target >= 0 and combo.currentIndex() != target:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(target)
                    combo.blockSignals(False)
        elif widget_type == 'channel':
            # AUD-10704: channel selector combo ("R"/"G"/"B"/"A"/"RGB"/"RGBA").
            combo = widget_info['widget']
            idx = combo.findText(str(value))
            if idx >= 0 and combo.currentIndex() != idx:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        elif widget_type in ('vec2', 'vec3'):
            # AUD-10704: vec containers hold one QDoubleSpinBox per axis, in
            # axis order (X, Y[, Z]) — update each from the incoming sequence.
            if isinstance(value, (list, tuple)):
                dim = int(widget_info.get('dim') or 2)
                spins = widget_info['widget'].findChildren(QDoubleSpinBox)
                for i, spin in enumerate(spins[:dim]):
                    if i < len(value):
                        spin.blockSignals(True)
                        spin.setValue(float(value[i]))
                        spin.blockSignals(False)
        elif widget_type == 'curve':
            # AUD-10704: the registered widget is the row container; the
            # CurveWidget inside exposes setCurve([(x, y), ...]).
            curve_widget = widget_info['widget'].findChild(CurveWidget)
            if curve_widget is not None and isinstance(value, (list, tuple)):
                points = [tuple(p) for p in value
                          if isinstance(p, (list, tuple)) and len(p) == 2]
                if points:
                    curve_widget.blockSignals(True)
                    curve_widget.setCurve(points)
                    curve_widget.blockSignals(False)
        elif widget_type == 'text':
            # AUD-10704: multi-line text editor — prefer the registered update
            # callback (it blocks signals so the edit is not re-emitted).
            update_fn = widget_info.get('update')
            if update_fn is not None:
                update_fn(str(value))
            else:
                editor = widget_info['widget']
                editor.blockSignals(True)
                editor.setPlainText(str(value))
                editor.blockSignals(False)
        elif widget_type == 'color':
            # Use the stored update function which knows how to refresh the
            # button preview (including alpha from the correct parameter key).
            update_fn = widget_info.get('update')
            if isinstance(value, (list, tuple)) and len(value) == 4:
                from src.features.composite.nodes.constant_node import ConstantNode
                value = ConstantNode.rgba_to_hex(*tuple(value)[:3])
            if update_fn:
                update_fn(value)
            else:
                # AUD-10704: generic color buttons (e.g. _create_color_picker)
                # register without an 'update' — refresh swatch + hex text
                # directly so undo/redo still updates the visible colour.
                color_btn = widget_info['widget']
                if isinstance(value, str) and value.startswith('#'):
                    color_btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: {value};
                            border: 1px solid #555;
                            border-radius: 3px;
                            min-height: 20px;
                        }}
                    """)
                    color_btn.setText(value)
        elif widget_type == 'custom':
            # Generic custom widgets (e.g. gradient preview) may also carry
            # an update callable.
            update_fn = widget_info.get('update')
            if update_fn:
                update_fn(value)
        elif widget_type == 'trim_range':
            trim_editor = widget_info['widget']
            start, end = trim_editor.values()
            maximum = int(widget_info.get('maximum', 1))
            if key == 'source_in':
                start = int(value)
            elif key == 'source_out':
                end = maximum if int(value) < 0 else int(value)
            elif key == 'total_frames':
                maximum = max(0, int(value) - 1)
                widget_info['maximum'] = maximum
                trim_editor.set_bounds(0, maximum)
                start, end = trim_editor.values()
                section_params = None
                for candidate in self._sections.values():
                    if candidate._param_widgets is param_widgets:
                        section_params = candidate.parameters
                        break
                raw_out = ((section_params or {}).get('source_out', -1)
                           if section_params is not None else -1)
                end = maximum if int(raw_out) < 0 else int(raw_out)
            trim_editor.set_values(start, end)

        # pivot_mode 是语义型 combo：center 时旋转中心 = 图像中心，pivot_x/y
        # 参数不参与计算。切换它必须联动刷新 pivot 行的显示值与可编辑态，
        # 否则面板会停在旧值（如 center 时的中心值，或 custom 时的参数值）。
        if key == "pivot_mode":
            self._refresh_pivot_display(param_widgets, value)

    def _refresh_pivot_display(self, param_widgets: Dict,
                               pivot_mode: Any = None,
                               overrides: Optional[Dict[str, Any]] = None) -> None:
        """同步 pivot_x/pivot_y 行的显示值与可编辑态。

        - ``pivot_mode="center"`` 且该节点有有效图像尺寸：显示图像中心 (w/2, h/2)
          并置灰 drag/slider（center 模式下 pivot_x/y 不参与计算，置灰避免误写）。
        - ``pivot_mode="custom"`` 或无图像尺寸：恢复 drag/slider 可编辑，显示
          参数真实值（由调用前写回 param_widgets 对应节点的 parameters 决定）。

        可在 pivot_mode 变更、image_size 变更、首次构建后调用。``pivot_mode``
        未传时从 param_widgets 里的 pivot_mode combo 当前值读取。
        ``overrides``（可选）：参数名 → 显示值，优先于 section.parameters——
        供 sync_animated_values 传入播放头采样值（AUD-10706 后采样值不再写回
        section.parameters，custom 模式的显示走这里）。
        """
        info_x = param_widgets.get("pivot_x")
        info_y = param_widgets.get("pivot_y")
        if not info_x or not info_y:
            return  # 不是 transform 节点（没有 pivot 行）

        # 取当前 pivot_mode：优先用传入值，否则读 combo。
        if pivot_mode is None:
            mode_info = param_widgets.get("pivot_mode")
            if mode_info and mode_info.get('type') == 'combo':
                combo = mode_info['widget']
                options = mode_info.get('options', [])
                idx = combo.currentIndex()
                pivot_mode = options[idx] if 0 <= idx < len(options) else "center"
            else:
                pivot_mode = "center"

        # 通过 pivot_x 控件反查所属 node_id，进而取缓存的图像尺寸与参数真实值。
        node_id = None
        for nid, section in self._sections.items():
            if section._param_widgets is param_widgets:
                node_id = nid
                break
        img = self._image_sizes.get(node_id) if node_id else None
        has_img = bool(img and img[0] > 0 and img[1] > 0)

        center_readonly = (pivot_mode == "center" and has_img)
        # custom 模式需要把显示值恢复成参数真实值（从 section.parameters 读），
        # 否则 drag 会停在之前 center 模式显示的图像中心值上。
        section = self._sections.get(node_id) if node_id else None
        params = section.parameters if section is not None else {}
        for info, key, half in (
            (info_x, "pivot_x", img[0] / 2.0 if has_img else 0.0),
            (info_y, "pivot_y", img[1] / 2.0 if has_img else 0.0),
        ):
            drag = info.get('widget')
            slider = info.get('slider')
            # 显示值：center+有尺寸→图像中心；否则→采样值（若有）或参数真实值。
            display_val = half if center_readonly else float(
                (overrides or {}).get(key, params.get(key, 0.0)))
            drag.blockSignals(True)
            drag.setValue(display_val)
            drag.blockSignals(False)
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(self._value_to_slider(display_val, info))
                slider.blockSignals(False)
            drag.setEnabled(not center_readonly)
            if slider is not None:
                slider.setEnabled(not center_readonly)
            info['pivot_center_readonly'] = center_readonly

    def _create_color_picker(self, key: str, value: str) -> QPushButton:
        """Create a color picker button with real-time preview.

        使用项目统一色彩选择器 CompactColorStudio（get_color）：色板/吸管/
        hex/RGB/HSL/Lab/历史记录，替代裸 QColorDialog。on_live 回调保留实时
        预览（拖动选色时节点参数即时更新，等价于原 currentColorChanged）。
        """
        from src.shared.color import get_color

        color_btn = QPushButton()
        initial_color = value if value else '#808080'
        color_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {initial_color};
                border: 1px solid #555;
                border-radius: 3px;
                min-height: 20px;
            }}
        """)
        color_btn.setText(initial_color)
        color_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def pick_color(_checked=False, k=key, btn=color_btn,
                       n=self._current_node_id) -> None:
            # _checked=False 占位：QPushButton.clicked(bool) 会把 checked 作为首参
            # 传入，若不接收会顶替 k（key），导致参数名变成 bool → 类型崩溃。
            # CP-056: 记录最近打开的颜色参数，供 viewer 吸管取色写回。
            self._last_color_param = k
            current = QColor(self._routed_parameters(n).get(k, '#808080'))

            def _on_live_color(color: QColor) -> None:
                """Update button preview and node parameter in real-time."""
                hex_color = color.name()
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {hex_color};
                        border: 1px solid #555;
                        border-radius: 3px;
                        min-height: 20px;
                    }}
                """)
                btn.setText(hex_color)
                self._on_param_changed(k, hex_color, n)

            result = get_color(parent=self, initial=current, on_live=_on_live_color)
            if result is not None:
                hex_color = result.name()
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {hex_color};
                        border: 1px solid #555;
                        border-radius: 3px;
                        min-height: 20px;
                    }}
                """)
                btn.setText(hex_color)
                self._on_param_changed(k, hex_color, n)

        color_btn.clicked.connect(pick_color)
        return color_btn

    def _create_file_picker(self, key: str, value: str) -> QWidget:
        """Create a file picker with browse button."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        line_edit = QLineEdit(value)
        line_edit.setReadOnly(True)
        line_edit.setStyleSheet("""
            QLineEdit {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 2px 6px; color: #ccc;
            }
        """)
        layout.addWidget(line_edit)

        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(24)
        browse_btn.setStyleSheet("""
            QPushButton {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 2px 6px; color: #ccc;
            }
            QPushButton:hover { background-color: #4a4a4a; }
        """)

        def browse_file(checked=False, k=key, le=line_edit,
                        n=self._current_node_id) -> None:
            # 注意：QPushButton.clicked 会把 bool checked 作为第一个参数传入槽。
            # 若不显式接收，它会顶替 k（key），导致 _on_param_changed 收到 bool
            # 作为参数名 → parameter_changed(str,str,object) 类型检查崩溃。
            # 故用 checked=False 占位，k/le 走默认值绑定真实 key。
            current_path = self._routed_parameters(n).get(k, "")
            start_dir = os.path.dirname(current_path) if current_path and os.path.exists(os.path.dirname(current_path)) else os.path.expanduser("~")

            file_path, _ = QFileDialog.getOpenFileName(
                self, "Select File", start_dir
            )
            if file_path:
                le.setText(file_path)
                self._on_param_changed(k, file_path, n)

        browse_btn.clicked.connect(browse_file)
        layout.addWidget(browse_btn)

        return widget

    def _create_hdri_picker(self, key: str, value: str) -> QWidget:
        """Create an HDRI preset picker: dropdown (presets) + browse button.

        下拉项：
          - "默认天空"（程序生成，值 ''）——开箱即用的默认环境光
          - "无"（关闭 IBL，值 '__none__'）
          - hdri/ 目录下的 .hdr/.exr 文件（值为相对文件名）
          - 若当前 value 是自定义绝对路径，末尾追加一项显示该路径
        "..." 按钮可选外部 HDR 文件（写回绝对路径）。
        """
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo = QComboBox()
        combo.setStyleSheet("""
            QComboBox {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 2px 6px; color: #ccc;
            }
            QComboBox:hover { border: 1px solid #666; }
            QComboBox QAbstractItemView {
                background-color: #2a2a2a; color: #ccc;
                selection-background-color: #4a6a8a;
            }
        """)

        # 收集下拉项：(显示文本, 实际值)
        # 顺序：无环境光（默认，稳）→ 默认天空（程序生成，实验性）→ hdri/ 预置
        items = [("无环境光", ""), ("默认天空", "__sky__")]
        # 扫描 hdri/ 目录的预置 HDR（含 HDR 浮点 + LDR 全景图）
        try:
            from src.shared.paths.paths import get_hdri_dir
            hdri_dir = get_hdri_dir()
            import os as _os
            if _os.path.isdir(hdri_dir):
                for fname in sorted(_os.listdir(hdri_dir)):
                    if fname.lower().endswith((".hdr", ".exr", ".hdri",
                                               ".jpg", ".jpeg", ".png",
                                               ".bmp", ".tiff", ".tif", ".webp")):
                        items.append((fname, fname))
        except Exception:
            pass

        # 若当前 value 是自定义绝对路径（不在预置列表），追加显示项
        display_values = [v for _, v in items]
        if value and value not in display_values and value not in ("__sky__",):
            items.append((value, value))

        # 填充下拉 + 定位当前选中
        current_idx = 0
        for i, (display, val) in enumerate(items):
            combo.addItem(display, val)
            # '' 和 '__sky__' 都算"默认天空"
            if val == value or (val == "" and value in ("", "__sky__")):
                current_idx = i
        combo.setCurrentIndex(current_idx)

        def on_combo(idx, k=key, combo=combo, n=self._current_node_id) -> None:
            data = combo.itemData(idx)
            if data is not None:
                self._on_param_changed(k, data, n)

        combo.currentIndexChanged.connect(on_combo)
        layout.addWidget(combo, 1)

        # "..." 自定义文件按钮
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(24)
        browse_btn.setStyleSheet("""
            QPushButton {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 2px 6px; color: #ccc;
            }
            QPushButton:hover { background-color: #4a4a4a; }
        """)

        def browse_hdri(checked=False, k=key, combo=combo,
                        n=self._current_node_id) -> None:
            # QPushButton.clicked 会传 bool checked 作首参，用占位避免顶替 k
            from src.shared.paths.paths import get_hdri_dir
            start_dir = get_hdri_dir()
            file_path, _ = QFileDialog.getOpenFileName(
                self, "选择 HDR 环境贴图", start_dir,
                "环境贴图 (*.hdr *.exr *.hdri *.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp);;All Files (*)"
            )
            if file_path:
                # 自定义文件：追加到下拉（若不在预置列表）并选中
                display_values = [combo.itemData(i) for i in range(combo.count())]
                if file_path not in display_values:
                    combo.blockSignals(True)
                    combo.addItem(file_path, file_path)
                    combo.blockSignals(False)
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.count() - 1)
                combo.blockSignals(False)
                self._on_param_changed(k, file_path, n)

        browse_btn.clicked.connect(browse_hdri)
        layout.addWidget(browse_btn)

        return widget

    def _create_vec_control(self, key: str, value: list, dim: int) -> QWidget:
        """Create a vec2/vec3 control with multiple spinboxes."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        values = list(value) if isinstance(value, (list, tuple)) else [0.0] * dim
        # Pad to required dimension
        while len(values) < dim:
            values.append(0.0)

        axes = ['X', 'Y', 'Z'][:dim]
        for i, axis in enumerate(axes):
            axis_label = QLabel(axis)
            axis_label.setFixedWidth(12)
            axis_label.setStyleSheet("color: #888; font-size: 9px;")
            layout.addWidget(axis_label)

            spinbox = QDoubleSpinBox()
            spinbox.setStyleSheet("background-color: #232325; border: 1px solid #151517;")
            spinbox.setRange(-99999, 99999)
            spinbox.setDecimals(4)
            spinbox.setValue(values[i])
            spinbox.setFixedWidth(65)

            def vec_changed(val, k=key, idx=i, d=dim, n=self._current_node_id) -> None:
                current = list(self._routed_parameters(n).get(k, [0.0] * d))
                while len(current) < d:
                    current.append(0.0)
                current[idx] = val
                self._on_param_changed(k, current, n)

            spinbox.valueChanged.connect(vec_changed)
            layout.addWidget(spinbox)

        return widget

    def _create_channel_selector(self, key: str, value: str) -> QComboBox:
        """Create a channel selector (R, G, B, A, etc.)."""
        channels = ["R", "G", "B", "A", "RGB", "RGBA"]
        
        combo = ChevronComboBox()
        combo.addItems(channels)
        if value in channels:
            combo.setCurrentIndex(channels.index(value))
        apply_combo_dropdown_style(combo)
        combo.currentTextChanged.connect(
            lambda text, k=key, n=self._current_node_id:
            self._on_param_changed(k, text, n)
        )
        return combo

    def _create_curve_control(self, key: str, value: Any) -> QWidget:
        """Create a curve editor widget for curve parameters.

        Args:
            key: Parameter name
            value: Curve data - list of points [(x, y), ...] or [[x, y], ...]

        Returns:
            Widget containing CurveWidget and optional reset button
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Convert value to list of tuples
        curve_data = None
        if value:
            if isinstance(value, (list, tuple)):
                try:
                    curve_data = [tuple(p) if isinstance(p, (list, tuple)) else p
                                  for p in value]
                except (TypeError, ValueError):
                    curve_data = None

        # Create curve widget
        curve_widget = CurveWidget(curve_data)
        curve_widget.setStyleSheet("""
            CurveWidget {
                background-color: #2a2a2a;
                border: 1px solid #555;
                border-radius: 3px;
            }
        """)

        def on_curve_changed(points, k=key, n=self._current_node_id) -> None:
            # Convert to list of lists for JSON serialization
            curve_list = [list(p) for p in points]
            self._on_param_changed(k, curve_list, n)

        curve_widget.curveChanged.connect(on_curve_changed)
        layout.addWidget(curve_widget)

        # Add hints label
        hints = QLabel(I18N.tr(
            "node_props.curve_points_hint",
            "Click to add | Drag to move | Double-click to delete"))
        hints.setStyleSheet("color: #888; font-size: 9px; font-style: italic;")
        layout.addWidget(hints)

        return widget

    def _update_param_widget_value(self, key: str, value: Any) -> None:
        """增量更新单个参数控件的值，避免完全重建。

        Args:
            key: 参数名称
            value: 新的参数值
        """
        if key not in self._param_widgets:
            return

        widget_info = self._param_widgets[key]
        widget_type = widget_info['type']

        if widget_type == 'combo':
            combo = widget_info['widget']
            options = widget_info.get('options', [])
            if value in options:
                combo.blockSignals(True)
                combo.setCurrentIndex(options.index(value))
                combo.blockSignals(False)

        elif widget_type == 'bool':
            checkbox = widget_info['widget']
            checkbox.blockSignals(True)
            checkbox.setChecked(bool(value))
            checkbox.blockSignals(False)

        elif widget_type in ('float', 'int'):
            drag = widget_info['widget']
            drag.blockSignals(True)
            drag.setValue(value)
            drag.blockSignals(False)
            slider = widget_info.get('slider')
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(self._value_to_slider(value, widget_info))
                slider.blockSignals(False)

        elif widget_type == 'str':
            line_edit = widget_info['widget']
            line_edit.blockSignals(True)
            line_edit.setText(str(value))
            line_edit.blockSignals(False)

        elif widget_type == 'color':
            color_btn = widget_info['widget']
            color_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {value if value else '#808080'};
                    border: 1px solid #555;
                    border-radius: 3px;
                    min-height: 20px;
                }}
            """)
            color_btn.setText(value if value else '#808080')

        elif widget_type == 'file':
            file_picker = widget_info['widget']
            # file_picker 是一个 QWidget，包含 QLineEdit
            line_edit = file_picker.findChild(QLineEdit)
            if line_edit:
                line_edit.blockSignals(True)
                line_edit.setText(str(value))
                line_edit.blockSignals(False)

        elif widget_type == 'hdri':
            hdri_picker = widget_info['widget']
            combo = hdri_picker.findChild(QComboBox)
            if combo:
                target = str(value)
                combo.blockSignals(True)
                found = False
                for i in range(combo.count()):
                    if combo.itemData(i) == target:
                        combo.setCurrentIndex(i)
                        found = True
                        break
                # 自定义绝对路径不在列表 → 追加并选中
                if not found and target:
                    combo.addItem(target, target)
                    combo.setCurrentIndex(combo.count() - 1)
                combo.blockSignals(False)

        elif widget_type == 'channel':
            channel_combo = widget_info['widget']
            channels = ["R", "G", "B", "A", "RGB", "RGBA"]
            if value in channels:
                channel_combo.blockSignals(True)
                channel_combo.setCurrentIndex(channels.index(value))
                channel_combo.blockSignals(False)

        elif widget_type == 'curve':
            curve_widget = widget_info['widget']
            # curve_widget 是 CurveWidget
            if isinstance(value, (list, tuple)):
                curve_data = [tuple(p) if isinstance(p, (list, tuple)) else p for p in value]
                curve_widget.blockSignals(True)
                curve_widget.setCurve(curve_data)
                curve_widget.blockSignals(False)

        elif widget_type in ('vec2', 'vec3'):
            vec_widget = widget_info['widget']
            dim = widget_info['dim']
            # vec_widget 包含多个 QDoubleSpinBox
            spinboxes = vec_widget.findChildren(QDoubleSpinBox)
            values = list(value) if isinstance(value, (list, tuple)) else [0.0] * dim
            for i, spinbox in enumerate(spinboxes):
                if i < len(values):
                    spinbox.blockSignals(True)
                    spinbox.setValue(values[i])
                    spinbox.blockSignals(False)

        elif widget_type == 'readonly':
            val_label = widget_info['widget']
            val_label.setText(str(value))

    # ── C5: path version binning ─────────────────────────────────────────

    def _attach_version_stepper(self, row_layout, key: str, line_edit,
                                node_id: Optional[str] = None) -> None:
        from src.shared.i18n.i18n import I18N
        """Add ▲▼ buttons stepping ``file_path`` across existing versions.

        Mirrors Nuke version binning: only versions that exist on disk are
        reachable; the last version token in the path is the binned one;
        paths without a version token get no buttons at all.
        """
        from PyQt6.QtWidgets import QToolButton
        from src.features.composite.utils import path_versioning

        has_version = path_versioning.find_version_span(line_edit.text()) is not None
        if not has_version:
            return

        def _step(delta: int, n=node_id) -> None:
            new_path = path_versioning.stepped_version(line_edit.text(), delta)
            if not new_path:
                return
            line_edit.setText(new_path)
            self._on_param_changed(key, new_path, n)

        up = QToolButton()
        up.setText("▲")
        up.setToolTip(I18N.tr("nodes.file_path.version_up",
                             "Version Up (next existing version)"))
        up.clicked.connect(lambda: _step(1))
        down = QToolButton()
        down.setText("▼")
        down.setToolTip(I18N.tr("nodes.file_path.version_down",
                              "Version Down (previous existing version)"))
        down.clicked.connect(lambda: _step(-1))
        row_layout.addWidget(up)
        row_layout.addWidget(down)

    def _on_param_changed(self, key: str, value: Any,
                          node_id: Optional[str] = None) -> None:
        """Handle parameter change from UI control.

        AUD-10700: ``node_id`` is bound by the row builders at construction
        time (while ``_create_*_row_for_section`` has swapped the panel context
        to the owning node). It routes the edit to the node that owns the row,
        so editing a non-first section (multi-selection / pinned) no longer
        writes to the panel-level first selected node. ``None`` keeps the
        legacy panel-level routing.
        """
        if self._block_signals:
            return
        route_id = node_id if node_id is not None else self._current_node_id
        route_params = self._current_parameters
        route_type = self._current_node_type
        section: Optional["NodeSectionWidget"] = None
        if route_id is not None:
            section = self._sections.get(route_id)
        if (node_id is not None and node_id != self._current_node_id
                and section is not None):
            # Routed edit for another node's section: dedup/write against that
            # section's own parameter snapshot, not the first node's cache.
            route_params = section.parameters
            route_type = section.node_type
        # ``trim_range`` is a UI-only aggregate over the persisted
        # source_in/source_out pair. Do not insert it into the section's
        # parameter dictionary or the next refresh would build a duplicate
        # readonly row; CompositePage expands it into the real parameters.
        if key == "trim_range" and route_type == "video_frame":
            if route_id:
                self.parameter_changed.emit(route_id, key, value)
            return
        # Skip if value hasn't changed (prevents redundant signals and rebuilds).
        if route_params.get(key) == value:
            return
        route_params[key] = value
        if route_id:
            # Keep the section's parameters dict in sync with route_params.
            # _current_parameters and section.parameters are independent copies
            # (set_nodes creates dict(...) for both), so updating only
            # _current_parameters leaves section.parameters stale. Downstream
            # rebuilds (e.g. _build_constant_ui reads section.parameters.get("mode"))
            # would then use the OLD value, causing mode switches to be ignored.
            if section is not None and section.parameters.get(key) != value:
                section.parameters[key] = value

            self.parameter_changed.emit(route_id, key, value)
            # pivot_mode 是语义型参数：切到 center/custom 时需联动刷新 pivot_x/y
            # 行的显示值与可编辑态（center 显示图像中心且置灰）。
            if key == "pivot_mode":
                if section is not None:
                    self._refresh_pivot_display(section._param_widgets, value)
            # Constant node: mode change triggers parameter visibility rebuild.
            # Skip during batch color updates to avoid destroying the live dialog.
            if key == "mode" and not self._constant_batch_update:
                self._handle_constant_mode_change(value, node_id=route_id)
            # Blur node: blur_type change triggers parameter visibility rebuild.
            if key == "blur_type":
                self._handle_blur_type_change(value, node_id=route_id)
            # Motion source changes whether the angle control is relevant.
            if key == "motion_source" and route_type == "blur":
                self._handle_blur_type_change(
                    section.parameters.get("blur_type", "box")
                    if section is not None else "box", node_id=route_id)
            # Keyer node: keyer_mode change triggers parameter visibility rebuild.
            if key == "keyer_mode":
                self._handle_keyer_mode_change(value, node_id=route_id)
            # Text node: outline_enable / shadow_enable / pivot_mode toggles
            # trigger parameter visibility rebuild so the group rows show/hide.
            if key in ("outline_enable", "shadow_enable", "pivot_mode") and \
                    route_type == "text":
                self._handle_text_state_change(node_id=route_id)

    def _routed_parameters(self, node_id: Optional[str]) -> Dict[str, Any]:
        """Parameter snapshot used for routed reads (AUD-10700).

        Fire-time reads inside picker/drag closures (initial color, current
        path, current vector) must consult the owning node's section when the
        row belongs to a non-first section; otherwise fall back to the legacy
        panel-level cache.
        """
        if node_id is not None and node_id != self._current_node_id:
            section = self._sections.get(node_id)
            if section is not None:
                return section.parameters
        return self._current_parameters

    def _copy_parameters(self) -> None:
        """Copy all parameters to clipboard."""
        if self._current_parameters:
            self._clipboard_params = dict(self._current_parameters)

    def _paste_parameters(self) -> None:
        """Paste parameters from clipboard."""
        if self._clipboard_params and self._current_node_id:
            for key, value in self._clipboard_params.items():
                if key in self._current_parameters:
                    self._on_param_changed(key, value)

    def clear(self) -> None:
        """Clear the panel (remove all non-pinned sections)."""
        self._clear_unpinned_sections()

    # ── Project Settings section (coexists with node sections) ──

    def _ensure_project_settings_section(self, settings_callback=None) -> None:
        """Lazily create the Project Settings section (once)."""
        if self._project_settings_section is not None:
            return
        from src.features.composite.ui.project_settings_section import (
            ProjectSettingsSection,
        )
        self._project_settings_section = ProjectSettingsSection(self)
        self._project_settings_section.pin_toggled.connect(
            self._on_project_settings_pin_toggled)
        if settings_callback is not None:
            self._project_settings_section.settings_changed.connect(
                settings_callback)

    def _on_project_settings_pin_toggled(self, is_pinned: bool) -> None:
        self._project_settings_pinned = is_pinned

    def _append_project_settings_section(self) -> None:
        """Add the Project Settings section to the bottom of the panel."""
        if self._project_settings_section is None:
            return
        # Avoid duplicate insertion.
        idx = self._content_layout.indexOf(self._project_settings_section)
        if idx == -1:
            self._content_layout.addWidget(self._project_settings_section)

    def _remove_project_settings_section(self) -> None:
        """Remove the Project Settings section from the layout (keep object)."""
        if self._project_settings_section is None:
            return
        self._content_layout.removeWidget(self._project_settings_section)
        self._project_settings_section.setParent(None)

    def show_project_settings(
        self,
        node_graph,
        settings_callback=None,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        duration_frames: int | None = None,
        timeline_bound: bool = False,
    ) -> None:
        """Show the Project Settings section at the bottom of the panel.

        It coexists with node-parameter sections (Nuke-style). The caller
        provides ``settings_callback`` to receive live settings changes.
        """
        self._ensure_project_settings_section(settings_callback)
        self._project_settings_section.load_from_graph(
            node_graph,
            width=width,
            height=height,
            fps=fps,
            duration_frames=duration_frames,
            timeline_bound=timeline_bound,
        )
        self._append_project_settings_section()
        self._project_settings_visible = True

    def hide_project_settings(self) -> None:
        """Hide the Project Settings section (unless pinned)."""
        if self._project_settings_pinned:
            return
        self._remove_project_settings_section()
        self._project_settings_visible = False

    def toggle_project_settings(self, node_graph, settings_callback=None) -> None:
        """Toggle Project Settings section visibility (S shortcut)."""
        if self._project_settings_visible:
            self.hide_project_settings()
        else:
            self.show_project_settings(node_graph, settings_callback)

    def _ensure_viewer3d_settings_section(self, settings_callback=None) -> None:
        """Lazily create the 3D Scene Settings section."""
        if self._viewer3d_settings_section is not None:
            return
        from src.features.composite.ui.viewer3d_settings_section import (
            Viewer3DSettingsSection,
        )
        self._viewer3d_settings_section = Viewer3DSettingsSection(self)
        self._viewer3d_settings_section.pin_toggled.connect(
            self._on_viewer3d_settings_pin_toggled)
        if settings_callback is not None:
            self._viewer3d_settings_section.settings_changed.connect(
                settings_callback)

    def _on_viewer3d_settings_pin_toggled(self, is_pinned: bool) -> None:
        self._viewer3d_settings_pinned = is_pinned

    def _append_viewer3d_settings_section(self) -> None:
        if self._viewer3d_settings_section is None:
            return
        if self._content_layout.indexOf(self._viewer3d_settings_section) == -1:
            self._content_layout.addWidget(self._viewer3d_settings_section)

    def _remove_viewer3d_settings_section(self) -> None:
        if self._viewer3d_settings_section is None:
            return
        self._content_layout.removeWidget(self._viewer3d_settings_section)
        self._viewer3d_settings_section.setParent(None)

    def show_viewer3d_settings(self, node_graph, settings_callback=None) -> None:
        """Show the default 3D Viewer scene settings section."""
        self._ensure_viewer3d_settings_section(settings_callback)
        self._viewer3d_settings_section.load_from_graph(node_graph)
        self._append_viewer3d_settings_section()
        self._viewer3d_settings_visible = True

    def hide_viewer3d_settings(self) -> None:
        if self._viewer3d_settings_pinned:
            return
        self._remove_viewer3d_settings_section()
        self._viewer3d_settings_visible = False

    def toggle_viewer3d_settings(self, node_graph, settings_callback=None) -> None:
        """Toggle the default 3D Viewer scene settings section."""
        if self._viewer3d_settings_visible:
            self.hide_viewer3d_settings()
        else:
            self.show_viewer3d_settings(node_graph, settings_callback)

    def _show_context_menu(self, pos) -> None:
        """Show context menu with float/dock toggle."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #3a3a3a; color: #ccc; border: 1px solid #555;
                padding: 4px; font-size: 12px;
            }
            QMenu::item:selected { background-color: #cc6600; }
        """)

        dock = self._find_parent_dock()
        if dock:
            if dock.isFloating():
                action = menu.addAction(I18N.tr("node_props.dock", "Dock"))
            else:
                action = menu.addAction(I18N.tr("node_props.float", "Float"))
            triggered = menu.exec(self.mapToGlobal(pos))
            if triggered == action:
                dock.setFloating(not dock.isFloating())

    def _find_parent_dock(self):
        """Walk up widget hierarchy to find parent QDockWidget."""
        widget = self.parent()
        while widget:
            if isinstance(widget, QDockWidget):
                return widget
            widget = widget.parent()
        return None

    def update_thumbnail(self, data) -> None:
        """No-op: thumbnail preview has been removed."""
        pass

    # ── Keyframe Animation Support ──

    def set_timeline_state(self, total_frames: int, current_frame: int) -> None:
        """Set timeline state for keyframe editor.

        Args:
            total_frames: Total frames in video/sequence.
            current_frame: Current frame number (0-based).
        """
        self._total_frames = total_frames
        frame_changed = (int(current_frame) != self._current_frame)
        self._current_frame = current_frame
        # ◆ 按钮红色跟随播放头：只有停在关键帧上才变红。
        if frame_changed:
            self.refresh_keyframe_button_states()
            # The Properties value controls must show the same sampled value
            # as Keyframes while the playhead moves. Keep this incremental so
            # scrubbing does not rebuild the whole Properties panel.
            if self._current_node_id:
                self.sync_animated_values(self._current_node_id,
                                          self._current_frame)

    def _close_active_kf_dialog(self) -> None:
        """A2: forget the currently-tracked keyframe editor dialog.

        Connected to the dialog's ``finished`` signal so the tracking fields
        are cleared whether the user hit OK or Cancel (or closed the window).
        """
        self._active_kf_dialog = None
        self._active_kf_editor = None

    def push_current_frame_to_editor(self, frame: int) -> None:
        """A3: push a main-playbar frame change into the open editor dialog.

        When the user scrubs the bottom transport bar, the keyframe editor
        dialog's playhead should follow (bidirectional sync). The editor's own
        ``current_frame_changed`` already seeks the host viewport the other
        direction; this closes the loop. Safe to call any time — a no-op when
        no editor is open.
        """
        editor = self._active_kf_editor
        if editor is None:
            return
        try:
            editor.set_current_frame(int(frame))
        except Exception:
            pass

    def _show_keyframe_editor(self, param_name: str, param_def: Any,
                              node_id: Optional[str] = None) -> None:
        """Show keyframe editor dialog for a parameter.

        A2: the dialog is now **non-modal** (``show()`` instead of ``exec()``)
        so the user can scrub the main Viewport / transport bar while editing
        curves — the whole point of a live preview. Keyframe writes remain
        atomic: nothing touches ``node.keyframe_tracks`` until OK; Cancel
        leaves the node untouched (the editor only held its own copy).
        ``node_id`` routes the dialog to the owning node (AUD-10700).

        Args:
            param_name: Name of the parameter to edit.
            param_def: ParamDef instance with animation metadata.
            node_id: Owning node id (defaults to the panel-level current node).
        """
        # Image Color mode: keyframe editing is disabled (static images have
        # no animation). Bail out before opening the dialog.
        if not getattr(self, "_keyframe_buttons_visible", True):
            return
        node_id = node_id if node_id is not None else self._current_node_id
        # P6: mirror the node-instance track onto param_def so the editor
        # opens with the authoritative keyframes (not stale class-level data).
        self._sync_param_def_from_node(node_id, param_name, param_def)

        # A2: if an editor is already open for any parameter, close it first so
        # we never have two live editors racing for the host viewport.
        if self._active_kf_dialog is not None:
            try:
                self._active_kf_dialog.close()
            except Exception:
                pass
            self._close_active_kf_dialog()

        # Create dialog
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Keyframe Editor - {param_name}")
        dialog.setMinimumSize(500, 400)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #2d2d30;
            }
            QLabel {
                color: #ccc;
            }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Create keyframe editor widget
        editor = KeyframeEditorWidget()
        editor.set_parameter(
            param_name,
            param_def,
            total_frames=self._total_frames,
            current_frame=self._current_frame
        )

        # P1/A2: dragging the dialog's playhead seeks the host viewport live so
        # the user can scrub the animation while editing. Non-modal + this
        # connection is what makes the Viewport follow the dialog playhead.
        # Keyframe writes still only happen on OK (F4) — seeking is display-only.
        editor.current_frame_changed.connect(self.current_frame_changed)

        # F4: the editor is an atomic, OK/Cancel unit. We deliberately do NOT
        # connect editor.keyframes_changed here — that used to write every
        # intermediate edit into node.keyframe_tracks immediately, so Cancel
        # could not roll anything back and OK wrote the data a second time.
        # The single committed write happens below on Accepted.

        layout.addWidget(editor)

        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.setStyleSheet("""
            QDialogButtonBox QPushButton {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 6px 12px; color: #ccc;
                min-width: 80px;
            }
            QDialogButtonBox QPushButton:hover {
                background-color: #4a4a4a;
            }
        """)

        def _on_accept() -> None:
            # Apply keyframes on accept. A-6: if animation was disabled in the
            # editor, emit an empty list so the node-level track is deleted.
            track = editor.get_track()
            if editor._is_animated and track and track.keyframes:
                self._on_keyframes_changed(param_name, param_def,
                                           [kf.to_dict() for kf in track.keyframes],
                                           node_id)
            elif not editor._is_animated:
                self._on_keyframes_changed(param_name, param_def, [], node_id)
            # A2: keep the dialog open after apply? No — OK commits and closes,
            # matching the original exec() semantics. Cancel discards edits.
            dialog.accept()

        button_box.accepted.connect(_on_accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        # A2: track the dialog + editor so the page can push frame changes in.
        dialog.finished.connect(self._close_active_kf_dialog)
        self._active_kf_dialog = dialog
        self._active_kf_editor = editor

        # A2: non-modal show — the main window stays interactive so the user
        # can scrub the transport bar / Viewport while the editor is open.
        dialog.setModal(False)
        dialog.show()

    def _on_keyframes_changed(self, param_name: str, param_def: Any,
                              keyframes: List[Dict[str, Any]],
                              node_id: Optional[str] = None) -> None:
        """Handle keyframes change from editor.

        P6/A-4: writes the keyframes onto the authoritative per-node
        ``keyframe_tracks`` store (via the NodeGraph reference), not onto the
        class-level ParamDef. An empty ``keyframes`` list removes the track
        (A-6). The legacy ParamDef is still mirrored so any code reading it
        stays consistent, and the ``keyframes_changed`` signal is still
        emitted for CompositePage to request a re-render / push undo.
        ``node_id`` routes the write to the owning node (AUD-10700).

        Args:
            param_name: Parameter name.
            param_def: ParamDef instance.
            keyframes: List of serialized keyframe dicts (empty = remove).
            node_id: Owning node id (defaults to the panel-level current node).
        """
        route_id = node_id if node_id is not None else self._current_node_id
        if not route_id or not param_def:
            return

        animated = len(keyframes) > 0

        # P6: authoritative write to the node-instance store
        self._apply_keyframes_to_node(route_id, param_name, param_def, keyframes)

        # Mirror onto ParamDef for backward compatibility with readers that
        # still inspect param_def.animated/keyframes.
        param_def.animated = animated
        param_def.keyframes = keyframes if animated else None

        # Update keyframe button state. Buttons live on the section widget, not
        # the panel's scratch dict, so check both (the current node's section is
        # where the ◆ the user clicked actually lives).
        self._set_keyframe_button_checked(param_name, animated, node_id=route_id)

        # Emit signal for external handling
        node_type = self._current_node_type
        if node_id is not None and node_id != self._current_node_id:
            section = self._sections.get(node_id)
            if section is not None:
                node_type = section.node_type
        self.keyframes_changed.emit(
            route_id,
            node_type or "",
            param_name,
            keyframes
        )

    def set_node_graph(self, node_graph: Any) -> None:
        """Inject the NodeGraph so ◆ buttons can read/write per-node tracks.

        P6: when set, the keyframe editor reads the existing track from the
        live node instance and writes changes back to it. Without a graph,
        the panel falls back to the legacy ParamDef-only behavior.
        """
        self._node_graph = node_graph

    def _get_live_node(self, node_id: str) -> Optional[Any]:
        """Return the live Node instance for ``node_id`` if a graph is set."""
        if self._node_graph is None:
            return None
        try:
            return self._node_graph.get_node(node_id)
        except Exception:
            return None

    def _autokey_if_animated(self, key: str, value: Any,
                             param_def: Any = None,
                             node_id: Optional[str] = None) -> None:
        """P3: auto-key — if ``key`` is already animated, drop/replace a
        keyframe at the current frame with ``value``.

        Only fires on commit signals (interaction_ended / sliderReleased), so a
        drag produces exactly one keyframe (the released value), not one per
        intermediate tick. Parameters with no existing track are left alone —
        the user opts into animation by clicking ◆ once, matching AE/Nuke.
        Reuses _on_keyframes_changed for undo + button state + panel refresh.
        ``node_id`` routes the keyframe to the owning node (AUD-10700).
        """
        from src.shared.keyframe import (
            Keyframe, InterpolationType, keyframe_track_from_dict,
            make_keyframe_track)

        node_id = node_id if node_id is not None else self._current_node_id
        if not node_id:
            return
        node = self._get_live_node(node_id)
        if node is None or not node.is_param_animated(key):
            return

        # Resolve the ParamDef for bounds (fallback to the track's own min/max).
        pdef = param_def
        if pdef is None:
            pdef = self._param_defs.get(key) if self._param_defs else None

        existing = node.keyframe_tracks.get(key)
        value_type = getattr(pdef, "ptype", "") or "scalar"
        try:
            track = (keyframe_track_from_dict(existing, value_type=value_type)
                     if isinstance(existing, dict) else None)
        except Exception:
            track = None
        if track is None:
            track = make_keyframe_track(
                param_name=key, value_type=value_type,
                min_val=getattr(pdef, 'min_val', 0.0),
                max_val=getattr(pdef, 'max_val', 1.0))
        try:
            val = self._normalize_keyframe_value(pdef, value)
        except (TypeError, ValueError):
            return
        # 播放头正停在关键帧上：只更新该关键帧的值，保留其插值类型、
        # 手柄与切线模式（add_keyframe 会整体替换，导致曲线形状丢失）。
        existing_kf = track.get_keyframe_at(self._current_frame)
        if existing_kf is not None:
            existing_kf.value = val
        else:
            track.add_keyframe(Keyframe(
                frame=self._current_frame, value=val,
                interpolation=InterpolationType.LINEAR,
            ))
        kfs = [kf.to_dict() for kf in track.keyframes]
        self._on_keyframes_changed(key, pdef, kfs, node_id)

    def _toggle_keyframe_at_current_frame(self, param_name: str,
                                          param_def: Any,
                                          node_id: Optional[str] = None) -> None:
        """Toggle a keyframe for ``param_name`` at the current frame.

        AE/Nuke stopwatch behaviour: left-clicking the ◆ control either adds a
        keyframe at the playhead using the parameter's current value or removes
        the keyframe already at the current frame. Removing the last keyframe
        deletes the whole track. Delegates to the existing
        _on_keyframes_changed pipeline so undo, button state, and the animation
        panels all refresh. ``node_id`` routes the keyframe to the owning
        node (AUD-10700).
        """
        from src.shared.keyframe import (
            Keyframe, InterpolationType, keyframe_track_from_dict,
            make_keyframe_track)

        node_id = node_id if node_id is not None else self._current_node_id
        if not node_id:
            return
        node = self._get_live_node(node_id)
        if node is None:
            return

        frame = self._current_frame
        current_value = node.parameters.get(
            param_name, getattr(param_def, 'default', 0.0))
        try:
            current_value = self._normalize_keyframe_value(
                param_def, current_value)
        except (TypeError, ValueError):
            return

        # Load the existing track (if any) to decide add vs. remove.
        track: Optional[Any] = None
        existing = node.keyframe_tracks.get(param_name)
        if isinstance(existing, dict) and existing.get("keyframes"):
            try:
                track = keyframe_track_from_dict(
                    existing, value_type=getattr(param_def, "ptype", "scalar"))
            except Exception:
                track = None

        has_at_frame = track is not None and track.get_keyframe_at(frame) is not None

        if has_at_frame and track is not None:
            # Remove the keyframe at the current frame.
            track.remove_keyframe(frame)
            kfs = [kf.to_dict() for kf in track.keyframes]
        else:
            # Add a keyframe at the current frame using the live parameter
            # value. The user may have changed the value before clicking the
            # stopwatch; using the existing interpolation here would silently
            # discard that edit and make the animation appear ineffective.
            if track is None:
                track = make_keyframe_track(
                    param_name=param_name,
                    value_type=getattr(param_def, "ptype", "scalar"),
                    min_val=getattr(param_def, 'min_val', 0.0),
                    max_val=getattr(param_def, 'max_val', 1.0))
            value = current_value
            track.add_keyframe(Keyframe(
                frame=frame, value=value,
                interpolation=InterpolationType.LINEAR,
            ))
            kfs = [kf.to_dict() for kf in track.keyframes]

        self._on_keyframes_changed(param_name, param_def, kfs, node_id)

    @staticmethod
    def _normalize_keyframe_value(param_def: Any, value: Any) -> Any:
        """Normalize a UI value to the declared animation value domain."""
        ptype = getattr(param_def, "ptype", "float") if param_def else "float"
        if ptype in {"color", "vec2", "vec3"}:
            if not isinstance(value, (list, tuple)):
                raise ValueError("vector keyframe requires a sequence")
            dimension = 4 if ptype == "color" else (3 if ptype == "vec3" else 2)
            if len(value) != dimension:
                raise ValueError("invalid vector keyframe dimension")
            return tuple(float(component) for component in value)
        if ptype == "bool":
            if not isinstance(value, bool):
                raise ValueError("bool keyframe requires a boolean")
            return value
        if ptype == "option":
            if not isinstance(value, str):
                raise ValueError("option keyframe requires a string")
            return value
        if ptype == "int":
            return int(round(float(value)))
        return float(value)

    def _toggle_multi_axis_keyframe(self, keys: List[str],
                                    pdefs: List[Any],
                                    node_id: Optional[str] = None) -> None:
        """Toggle a keyframe at the current frame across multiple axes (XY/XYZ).

        A shared ◆ button represents one multi-dim property. Clicking it must
        keep all axes in lockstep: when any axis already has a keyframe at the
        current frame, remove it from every axis; otherwise add one to every
        axis (each with its own value). This matches the AE stopwatch contract
        for multi-dimensional properties. ``node_id`` routes to the owning
        node (AUD-10700).
        """
        from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType

        node_id = node_id if node_id is not None else self._current_node_id
        if not node_id:
            return
        node = self._get_live_node(node_id)
        if node is None:
            return

        frame = self._current_frame

        # Detect whether ANY axis has a keyframe at the current frame → remove
        # mode; otherwise → add mode.
        any_at_frame = False
        tracks: Dict[str, Optional[KeyframeTrack]] = {}
        for key, pdef in zip(keys, pdefs):
            existing = node.keyframe_tracks.get(key)
            t: Optional[KeyframeTrack] = None
            if isinstance(existing, dict) and existing.get("keyframes"):
                try:
                    t = KeyframeTrack.from_dict(existing)
                except Exception:
                    t = None
            tracks[key] = t
            if t is not None and t.get_keyframe_at(frame) is not None:
                any_at_frame = True

        for key, pdef in zip(keys, pdefs):
            track = tracks.get(key)
            if any_at_frame:
                # Remove mode: drop the keyframe at this frame on every axis.
                if track is None:
                    continue
                track.remove_keyframe(frame)
                kfs = [kf.to_dict() for kf in track.keyframes]
            else:
                # Add mode: add a keyframe on every axis.
                current_value = float(node.parameters.get(
                    key, getattr(pdef, 'default', 0.0)) or 0.0)
                if track is None:
                    track = KeyframeTrack(
                        param_name=key,
                        min_val=getattr(pdef, 'min_val', 0.0),
                        max_val=getattr(pdef, 'max_val', 1.0),
                    )
                # Always use the live value when the user explicitly clicks
                # the shared stopwatch. Reusing the existing interpolation
                # would ignore a Scale edit made before this click.
                value = current_value
                track.add_keyframe(Keyframe(
                    frame=frame, value=value,
                    interpolation=InterpolationType.LINEAR,
                ))
                kfs = [kf.to_dict() for kf in track.keyframes]
            self._on_keyframes_changed(key, pdef, kfs, node_id)

    def _show_kf_btn_context_menu(self, global_pos, actions: List[tuple]) -> None:
        """Pop up a small context menu for a keyframe ◆ button.

        ``actions`` is a list of ``(label, callable)`` pairs. Shared by the
        single/pair/triple button factories so right-click keeps the advanced
        editor entry while left-click does the fast toggle.
        """
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #3a3a3a; color: #ccc; border: 1px solid #555;
                padding: 4px; font-size: 12px;
            }
            QMenu::item:selected { background-color: #cc6600; }
        """)
        for label, cb in actions:
            act = menu.addAction(label)
            act.triggered.connect(lambda _checked=False, c=cb: c())
        menu.exec(global_pos)

    def _apply_keyframes_to_node(self, node_id: str, param_name: str,
                                 param_def: Any, keyframes: List[Dict[str, Any]]) -> None:
        """Write (or remove) a keyframe track on the live node instance.

        Falls back to a no-op when no NodeGraph is bound (headless tests that
        only exercise the ParamDef path).
        """
        import copy
        node = self._get_live_node(node_id)
        if node is None:
            return
        # P2: capture the BEFORE track so CompositePage can push a real undo.
        before = copy.deepcopy(node.keyframe_tracks.get(param_name))
        if not keyframes:
            node.remove_keyframe_track(param_name)
        else:
            from src.shared.keyframe import Keyframe, make_keyframe_track
            track = make_keyframe_track(
                param_name=param_name,
                value_type=getattr(param_def, "ptype", "scalar"),
                min_val=getattr(param_def, 'min_val', 0.0),
                max_val=getattr(param_def, 'max_val', 1.0))
            for kf_data in keyframes:
                try:
                    track.add_keyframe(Keyframe.from_dict(kf_data))
                except Exception:
                    continue
            node.set_keyframe_track(param_name, track)
        # Stash the before snapshot for the next keyframes_changed emission.
        if not hasattr(self, '_pending_keyframe_before'):
            self._pending_keyframe_before = {}
        self._pending_keyframe_before[(node_id, param_name)] = before

    def _sync_param_def_from_node(self, node_id: Optional[str], param_name: str,
                                  param_def: Any) -> None:
        """Mirror a node-instance keyframe track onto ``param_def``.

        P6: the keyframe editor reads param_def.animated/keyframes. Before
        opening it, copy the live node's track (if any) onto param_def so the
        editor shows the real animation. Without a bound graph this is a no-op
        and param_def keeps its existing (possibly legacy) state.
        """
        node = self._get_live_node(node_id) if node_id else None
        if node is None:
            return
        track_data = node.keyframe_tracks.get(param_name)
        if isinstance(track_data, dict) and track_data.get("keyframes"):
            param_def.animated = True
            param_def.keyframes = track_data["keyframes"]
        else:
            param_def.animated = False
            param_def.keyframes = None

    def _param_has_keyframe_at_frame(self, param_name: str,
                                     node_id: Optional[str] = None) -> bool:
        """当前播放帧（``_current_frame``）上是否存在 ``param_name`` 的关键帧。

        AE 语义：◆ 按钮红色仅代表“播放头正停在某个关键帧上”。
        """
        node = self._get_live_node(node_id or self._current_node_id)
        if node is None:
            return False
        existing = getattr(node, 'keyframe_tracks', {}).get(param_name)
        if not isinstance(existing, dict) or not existing.get("keyframes"):
            return False
        frame = int(self._current_frame)
        return any(int(kf.get("frame", -1)) == frame
                   for kf in existing["keyframes"])

    def _keyframe_button_state(self, info: dict, param_name: str,
                               fallback: bool,
                               node_id: Optional[str] = None) -> bool:
        """Compute the ◆ checked state for a row: red only when the playhead
        sits on a keyframe (any axis of a shared multi-dim button)."""
        if self._node_graph is None:
            return fallback
        keys = info.get('keyframe_btn_keys') or [param_name]
        return any(self._param_has_keyframe_at_frame(k, node_id) for k in keys)

    def sync_animated_values(self, node_id: str, frame: int,
                             param_names: Optional[List[str]] = None) -> None:
        """Mirror current-frame animated values into existing controls.

        ``Node.parameters`` intentionally stores the non-animated/base value;
        the renderer overlays ``keyframe_tracks`` at evaluation time. This
        method applies the same overlay only to the Properties UI copy, so
        Keyframes and Properties show one value without mutating the base
        parameter or rebuilding widgets during a drag.
        """
        node = self._get_live_node(node_id)
        if node is None:
            return
        section = self._sections.get(node_id)
        if section is None:
            return
        param_widgets = getattr(section, "_param_widgets", {})
        names = (list(param_names) if param_names is not None
                 else list(getattr(node, "keyframe_tracks", {}).keys()))
        from src.shared.keyframe import keyframe_track_from_dict
        sampled: Dict[str, Any] = {}
        for param_name in names:
            data = getattr(node, "keyframe_tracks", {}).get(param_name)
            if not isinstance(data, dict) or not data.get("keyframes"):
                continue
            try:
                pdef = node.get_param_defs().get(param_name)
                value_type = getattr(pdef, "ptype", "") or data.get(
                    "value_type", "scalar")
                track = keyframe_track_from_dict({
                    "param_name": param_name,
                    "keyframes": data.get("keyframes", []),
                    "value_type": value_type,
                    "min_val": data.get("min_val", 0.0),
                    "max_val": data.get("max_val", 1.0),
                    "pre_extrap": data.get("pre_extrap", "constant"),
                    "post_extrap": data.get("post_extrap", "constant"),
                })
                base = getattr(node, "parameters", {}).get(param_name, 0.0)
                value = track.get_value_float(
                    float(frame), default=base)
                if pdef is not None and getattr(pdef, "ptype", "") == "int":
                    value = int(round(value))
            except (TypeError, ValueError, AttributeError, KeyError):
                continue

            # AUD-10706: the sampled value is a *display* overlay only. It must
            # NOT be written back into ``section.parameters`` /
            # ``_current_parameters`` — those snapshots feed preset saving
            # (_collect_current_values starts from section.parameters) and
            # section rebuilds; writing playhead samples here would bake the
            # "current frame" interpolation into presets and rebuilds instead
            # of the node's authoritative base parameters.
            sampled[param_name] = value
            self._update_section_param_value(param_name, value,
                                              param_widgets)

        self._refresh_pivot_display(param_widgets, overrides=sampled)
        self.refresh_keyframe_button_states()

    def refresh_keyframe_button_states(self) -> None:
        """Recompute the red state of every visible ◆ button.

        Called on playhead moves (set_timeline_state) and after external
        keyframe edits (Keyframes panel / viewer handle drags) so the button
        always reflects whether the *current frame* has a keyframe.
        """
        seen: set = set()

        def _walk(widgets: dict, node_id: Optional[str] = None) -> None:
            for name, info in widgets.items():
                if not isinstance(info, dict):
                    continue
                btn = info.get('keyframe_btn')
                if btn is None or id(btn) in seen:
                    continue
                seen.add(id(btn))
                btn.setChecked(
                    self._keyframe_button_state(info, name, btn.isChecked(),
                                                node_id))

        _walk(getattr(self, '_param_widgets', {}))
        # 所有已打开的 section（含 pin 的其他节点）都要跟随播放头刷新。
        for nid, section in getattr(self, '_sections', {}).items():
            _walk(getattr(section, '_param_widgets', {}), nid)

    def set_keyframe_buttons_visible(self, visible: bool) -> None:
        """Show or hide every ◆ keyframe button across the panel.

        Used by Image Color mode: the buttons stay disabled so users cannot
        add keyframes on a static image, but all node effects remain usable.
        New buttons created afterwards also obey the stored flag.
        """
        self._keyframe_buttons_visible = visible

        seen: set = set()

        def _apply(widgets: dict) -> None:
            for info in widgets.values():
                if not isinstance(info, dict):
                    continue
                btn = info.get('keyframe_btn')
                if btn is None or id(btn) in seen:
                    continue
                seen.add(id(btn))
                btn.setVisible(visible)
                btn.setEnabled(visible)

        _apply(getattr(self, '_param_widgets', {}))
        for section in getattr(self, '_sections', {}).values():
            _apply(getattr(section, '_param_widgets', {}))

    def _set_keyframe_button_checked(self, param_name: str, checked: bool,
                                     node_id: Optional[str] = None) -> None:
        """Reflect keyframe state on the ◆ button for ``param_name``, searching
        the panel's scratch widgets then the owning node's section widgets.

        ``checked`` (any-keyframe flag) is only a fallback for the legacy
        no-graph path; with a bound graph the button is red only when the
        current frame itself has a keyframe. ``node_id`` routes the lookup to
        the owning node's section (AUD-10700).
        """
        route_id = node_id if node_id is not None else self._current_node_id
        info = self._param_widgets.get(param_name)
        if info is None and route_id:
            section = self._sections.get(route_id)
            if section is not None:
                info = section._param_widgets.get(param_name)
                if info is None:
                    # Custom controls may expose one button for several
                    # underlying parameters (for example Constant's RGBA
                    # swatch). Resolve the shared registration by channel.
                    for candidate in section._param_widgets.values():
                        if (isinstance(candidate, dict)
                                and param_name in (candidate.get(
                                    'keyframe_btn_keys') or [])):
                            info = candidate
                            break
        if info is None:
            for candidate in self._param_widgets.values():
                if (isinstance(candidate, dict)
                        and param_name in (candidate.get(
                            'keyframe_btn_keys') or [])):
                    info = candidate
                    break
        if info and 'keyframe_btn' in info:
            info['keyframe_btn'].setChecked(
                self._keyframe_button_state(info, param_name, checked,
                                            node_id=route_id))

    # ── Separate / Combine X·Y dimensions (AE-style) ──

    def is_dim_split(self, node_id: str, base: str) -> bool:
        """True if the X/Y pair ``base`` has been split into two single rows."""
        return base in self._split_dims.get(node_id, ())

    def set_dim_split(self, node_id: str, base: str, split: bool) -> None:
        cur = set(self._split_dims.get(node_id, ()))
        if split:
            cur.add(base)
        else:
            cur.discard(base)
        if cur:
            self._split_dims[node_id] = cur
        else:
            self._split_dims.pop(node_id, None)

    def _toggle_dim_split(self, node_id: str, base: str, split: bool) -> None:
        """Flip the split state and rebuild that node's section rows."""
        if self.is_dim_split(node_id, base) == split:
            return
        self.set_dim_split(node_id, base, split)
        section = self._sections.get(node_id)
        if section is not None:
            section._build_parameters()
            section._apply_slider_visibility()

    def _show_parameter_context_menu(self, node_id: str, entries: list,
                                     global_pos, split_base: Optional[str] = None) -> None:
        """Show expression actions from a parameter-name context menu.

        ``entries`` contains ``(parameter_name, ParamDef, axis_label)``. A
        vector row therefore exposes separate X/Y/Z expression actions while
        keeping the row itself compact.
        """
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #3a3a3a; color: #ccc; border: 1px solid #555;
                padding: 4px; font-size: 12px;
            }
            QMenu::item:selected { background-color: #cc6600; }
        """)

        expression_entries = [
            (key, pdef, axis) for key, pdef, axis in entries
            if pdef is not None
            and getattr(pdef, "allow_expression", False)
            and getattr(pdef, "is_expression_compatible", lambda: False)()
        ]
        # B3-2: "Promote to Group" on the first numeric entry of the row
        # when the page provides a resolver hook.
        if self._group_promote_hook is not None:
            numeric = next(
                ((key, pdef) for key, pdef, _axis in entries
                 if pdef is not None
                 and getattr(pdef, "is_expression_compatible", lambda: False)()),
                None)
            if numeric is not None:
                promote_action = menu.addAction(
                    I18N.tr("node_props.promote_to_group", "提升到组 (Promote to Group)"))
                promote_action.triggered.connect(
                    lambda _checked=False, nid=node_id, k=numeric[0]:
                    self._group_promote_hook(nid, k))

        for key, pdef, axis in expression_entries:
            suffix = f" ({axis})" if axis else ""
            action = menu.addAction(f"编辑表达式{suffix}…")
            action.triggered.connect(
                lambda _checked=False, k=key, pd=pdef, nid=node_id:
                self._show_expression_editor(k, pd, nid))

        # ── CP-051: 参数级复制 / 粘贴 / pick-whip 链接 ──
        if entries:
            menu.addSeparator()
            first_key = entries[0][0]
            copy_act = menu.addAction(
                I18N.tr("node_props.copy_value", "复制参数值 (Copy Value)"))
            copy_act.triggered.connect(
                lambda _checked=False, nid=node_id, k=first_key:
                self._copy_param_value(nid, k))
            paste_keys = [k for k, _pd, _ax in entries
                          if self._param_value_clipboard
                          and k == self._param_value_clipboard.get("param")]
            paste_act = menu.addAction(
                I18N.tr("node_props.paste_value", "粘贴参数值 (Paste Value)"))
            paste_act.setEnabled(bool(paste_keys))
            if paste_keys:
                paste_act.triggered.connect(
                    lambda _checked=False, nid=node_id, k=paste_keys[0]:
                    self._paste_param_value(nid, k))
            link_act = menu.addAction(
                I18N.tr("node_props.pick_whip", "参数链接 (Pick Whip)…"))
            link_act.setEnabled(any(
                pd is not None
                and getattr(pd, "is_expression_compatible", lambda: False)()
                for _k, pd, _ax in entries))
            if link_act.isEnabled():
                link_act.triggered.connect(
                    lambda _checked=False, nid=node_id, k=first_key:
                    self._show_pick_whip_dialog(nid, k))

        if expression_entries and split_base is not None:
            menu.addSeparator()

        if split_base is None:
            if not expression_entries:
                menu.addAction(
                    I18N.tr("node_props.no_editable_expression",
                            "没有可编辑的表达式")).setEnabled(False)
        elif self.is_dim_split(node_id, split_base):
            act = menu.addAction(
                I18N.tr("node_props.combine_xy", "合并 X / Y  (Combine)"))
            act.triggered.connect(
                lambda _checked=False: self._toggle_dim_split(node_id, split_base, False))
        else:
            act = menu.addAction(
                I18N.tr("node_props.separate_xy", "分离 X / Y  (Separate Dimensions)"))
            act.triggered.connect(
                lambda _checked=False: self._toggle_dim_split(node_id, split_base, True))
        menu.exec(global_pos)

    # ── CP-051: 参数级复制/粘贴与 pick-whip ──

    def _copy_param_value(self, node_id: str, param_name: str) -> None:
        """Copy one parameter's current value to the panel clipboard."""
        import copy as _copy
        value = self._routed_parameters(node_id).get(param_name)
        if value is None:
            return
        self._param_value_clipboard = {
            "param": param_name,
            "value": _copy.deepcopy(value),
        }

    def _paste_param_value(self, node_id: str, param_name: str) -> None:
        """Paste the clipboarded value into the same-named parameter."""
        clip = getattr(self, "_param_value_clipboard", None)
        if not clip or clip.get("param") != param_name:
            return
        self._on_param_changed(param_name, clip.get("value"),
                               node_id=node_id)

    def _show_pick_whip_dialog(self, node_id: str, param_name: str) -> None:
        """CP-051: pick-whip editor for numeric parameters.

        Offers every OTHER node's numeric parameters as the binding source
        and persists via NodeGraph.set_parameter_binding (circular-safe).
        """
        graph = getattr(self, "_node_graph", None)
        if graph is None:
            return
        try:
            nodes = list(getattr(graph, "nodes", {}).items())
        except Exception:
            return

        from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                                     QDialogButtonBox as _BB, QLabel,
                                     QVBoxLayout)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"参数链接 — {param_name}")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(I18N.tr(
            "node_props.pick_whip_hint",
            "选择源节点与参数（目标 = 当前参数）:")))

        node_combo = QComboBox()
        source_params = {}
        for nid, node in nodes:
            if nid == node_id:
                continue
            # display_title 是 property（返回 str）——不要再调用
            title = f"{getattr(node, 'display_title', nid) or nid} [{nid}]"
            node_combo.addItem(title, userData=nid)
            params = []
            for pdef in getattr(node, "PARAM_DEFS", []) or []:
                if getattr(pdef, "ptype", "") in ("float", "int"):
                    params.append(pdef.name)
            source_params[nid] = params
        lay.addWidget(node_combo)

        param_combo = QComboBox()
        lay.addWidget(param_combo)

        def _refill(_idx=-1) -> None:
            nid = node_combo.currentData()
            param_combo.clear()
            for pname in source_params.get(nid, []):
                param_combo.addItem(pname, userData=pname)

        node_combo.currentIndexChanged.connect(_refill)
        _refill()

        buttons = QDialogButtonBox(_BB.StandardButton.Ok | _BB.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        src_nid = node_combo.currentData()
        src_param = param_combo.currentData()
        if not src_nid or not src_param:
            return
        success, error = graph.set_parameter_binding(
            node_id, param_name, src_nid, src_param)
        if not success:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "参数链接失败", str(error or "未知错误"))

    def _show_dim_menu(self, node_id: str, base: str, global_pos) -> None:
        """Backward-compatible dimension-only context menu entry point."""
        self._show_parameter_context_menu(node_id, [], global_pos, base)

    def _make_pair_keyframe_button(self, x_key: str, y_key: str,
                                   pdef_x, pdef_y) -> QToolButton:
        """One keyframe ◆ button shared by an X/Y pair (AE: a multi-dim property
        has a single stopwatch). Checked if either axis is animated; opening the
        editor mirrors the track to both axes."""
        btn = _KeyframeToolButton()
        btn.setText("◆")
        btn.setToolTip(I18N.tr(
            "node_props.kf_btn_tooltip_xy",
            "左键：切换 X/Y 关键帧\nAlt+左键：编辑 X 表达式\n右键：曲线编辑器"))
        btn.setFixedSize(20, 20)
        btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999; border-radius: 2px;
            }
            QToolButton:hover {
                background-color: #555; color: #cc6600; border-color: #cc6600;
            }
            QToolButton:checked {
                background-color: #ff5555; color: #fff; border-color: #ff5555;
            }
        """)
        # AE 语义：红色仅表示当前播放帧上有关键帧（任一轴）。
        if self._node_graph is not None:
            has_kf = (self._param_has_keyframe_at_frame(x_key)
                      or self._param_has_keyframe_at_frame(y_key))
        else:
            has_kf = bool(
                (pdef_x and getattr(pdef_x, 'animated', False) and getattr(pdef_x, 'keyframes', None))
                or (pdef_y and getattr(pdef_y, 'animated', False) and getattr(pdef_y, 'keyframes', None))
            )
        btn.setCheckable(True)
        btn.setChecked(has_kf)

        keys = [x_key, y_key]
        pdefs = [pdef_x, pdef_y]

        # F1: left-click toggles a keyframe at the current frame on both axes;
        # right-click opens the pair editor / clears both tracks.
        # AUD-10700: bind the owning node at build time (see _make_keyframe_button).
        def _toggle(_checked=False, ks=keys, ps=pdefs, n=self._current_node_id) -> None:
            self._toggle_multi_axis_keyframe(ks, ps, n)

        def _show_menu(pos, ks=keys, ps=pdefs, n=self._current_node_id) -> None:
            self._sync_param_def_from_node(n, ks[0], ps[0])
            self._sync_param_def_from_node(n, ks[1], ps[1])
            actions = [
                ("打开曲线编辑器…",
                 lambda n=n: self._show_pair_keyframe_editor(ks[0], ks[1],
                                                             ps[0], ps[1], n)),
                ("删除所有关键帧",
                 lambda n=n: [self._on_keyframes_changed(k, p, [], n)
                              for k, p in zip(ks, ps)]),
            ]
            self._show_kf_btn_context_menu(btn.mapToGlobal(pos), actions)

        btn.clicked.connect(_toggle)
        if (pdef_x is not None
                and getattr(pdef_x, "allow_expression", False)
                and getattr(pdef_x, "is_expression_compatible", lambda: False)()):
            btn.alt_clicked.connect(
                lambda k=x_key, pd=pdef_x, n=self._current_node_id:
                self._show_expression_editor(k, pd, n))
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(_show_menu)
        return btn

    def _make_multi_keyframe_button(self, keys: List[str],
                                    pdefs: List[Any],
                                    editor_key: Optional[str] = None) -> QToolButton:
        """Create one stopwatch for a custom multi-channel property.

        Custom controls such as Constant's RGBA swatch edit several numeric
        parameters as one user-facing property. Reuse the same add/remove
        semantics as XY/XYZ rows so these controls stay consistent with the
        regular numeric rows and the Keyframes panel.
        """
        btn = _KeyframeToolButton()
        btn.setText("◆")
        btn.setToolTip(I18N.tr(
            "node_props.kf_btn_tooltip_multi",
            "左键：切换多通道关键帧\nAlt+左键：编辑表达式\n右键：曲线编辑器"))
        btn.setFixedSize(20, 20)
        btn.setStyleSheet("""
            QToolButton {
                background-color: transparent; border: 1px solid #555;
                font-size: 10px; padding: 1px; color: #999; border-radius: 2px;
            }
            QToolButton:hover {
                background-color: #555; color: #cc6600; border-color: #cc6600;
            }
            QToolButton:checked {
                background-color: #ff5555; color: #fff; border-color: #ff5555;
            }
        """)
        has_kf = any(self._param_has_keyframe_at_frame(key) for key in keys) \
            if self._node_graph is not None else any(
                pdef and getattr(pdef, 'animated', False)
                and getattr(pdef, 'keyframes', None)
                for pdef in pdefs)
        btn.setCheckable(True)
        btn.setChecked(has_kf)

        def _toggle(_checked=False, ks=list(keys), ps=list(pdefs),
                    n=self._current_node_id) -> None:
            self._toggle_multi_axis_keyframe(ks, ps, n)

        def _show_menu(pos, ks=list(keys), ps=list(pdefs), button=btn,
                       n=self._current_node_id) -> None:
            first_key = editor_key or (ks[0] if ks else None)
            first_pdef = ps[ks.index(first_key)] if first_key in ks else None
            if first_key is not None and first_pdef is not None:
                self._sync_param_def_from_node(n, first_key, first_pdef)
            actions = []
            if first_key is not None and first_pdef is not None:
                actions.append((
                    "打开曲线编辑器…",
                    lambda k=first_key, pd=first_pdef, n=n:
                    self._show_keyframe_editor(k, pd, n)))
            actions.append((
                "删除所有关键帧",
                lambda n=n: [self._on_keyframes_changed(k, pd, [], n)
                             for k, pd in zip(ks, ps)]))
            self._show_kf_btn_context_menu(
                button.mapToGlobal(pos), actions)

        btn.clicked.connect(_toggle)
        first_expr = next(
            ((key, pdef) for key, pdef in zip(keys, pdefs)
             if pdef is not None
             and getattr(pdef, "allow_expression", False)
             and getattr(pdef, "is_expression_compatible", lambda: False)()),
            (None, None),
        )
        if first_expr[0] is not None:
            btn.alt_clicked.connect(
                lambda k=first_expr[0], pd=first_expr[1],
                       n=self._current_node_id:
                self._show_expression_editor(k, pd, n))
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(_show_menu)
        return btn

    def _show_pair_keyframe_editor(self, x_key: str, y_key: str,
                                   pdef_x: Any, pdef_y: Any,
                                   node_id: Optional[str] = None) -> None:
        """Open the keyframe editor on the X axis as the representative.

        A-7 fix: when the pair is linked (one shared ◆ button), the X and Y
        axes must keep independent values. On accept:
          - the X track is applied verbatim (the user edited X's values);
          - the Y track is rebuilt with the SAME frames as X, but each
            keyframe's value is taken from the Y axis's current value at
            that frame (existing Y track interpolation, or Y's current
            parameter value when Y had no track).
        This keeps the two axes time-synchronized without clobbering Y with
        X's numbers. The pair is still one property until the user Separates
        Dimensions. ``node_id`` routes the dialog to the owning node
        (AUD-10700).
        """
        node_id = node_id if node_id is not None else self._current_node_id
        # P6: sync both axes from the node-instance tracks before editing.
        self._sync_param_def_from_node(node_id, x_key, pdef_x)
        self._sync_param_def_from_node(node_id, y_key, pdef_y)

        # A2: close any already-open editor before opening a new one.
        if self._active_kf_dialog is not None:
            try:
                self._active_kf_dialog.close()
            except Exception:
                pass
            self._close_active_kf_dialog()

        dialog = QDialog(self)
        title_base = self._param_display_name(x_key).rsplit(" ", 1)[0]
        dialog.setWindowTitle(f"Keyframe Editor - {title_base}")
        dialog.setMinimumSize(500, 400)
        dialog.setStyleSheet("""
            QDialog { background-color: #2d2d30; }
            QLabel { color: #ccc; }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        editor = KeyframeEditorWidget()
        editor.set_parameter(
            x_key, pdef_x,
            total_frames=self._total_frames, current_frame=self._current_frame)
        # P1/A2: relay playhead seeks from this dialog to the host viewport
        # (non-modal, so the Viewport actually follows while editing).
        editor.current_frame_changed.connect(self.current_frame_changed)
        layout.addWidget(editor)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.setStyleSheet("""
            QDialogButtonBox QPushButton {
                background-color: #3a3a3a; border: 1px solid #555;
                border-radius: 3px; padding: 6px 12px; color: #ccc; min-width: 80px;
            }
            QDialogButtonBox QPushButton:hover { background-color: #4a4a4a; }
        """)

        def _on_accept() -> None:
            track = editor.get_track()
            if not editor._is_animated:
                # A-6: animation disabled → remove both tracks
                self._on_keyframes_changed(x_key, pdef_x, [], node_id)
                self._on_keyframes_changed(y_key, pdef_y, [], node_id)
                dialog.accept()
                return
            if not track or not track.keyframes:
                dialog.accept()
                return
            # X axis: apply the edited track verbatim.
            x_kfs = [kf.to_dict() for kf in track.keyframes]
            self._on_keyframes_changed(x_key, pdef_x, x_kfs, node_id)

            # Y axis (A-7): same frames, but Y keeps its own values.
            y_kfs = self._build_linked_y_keyframes(y_key, pdef_y, track.keyframes)
            self._on_keyframes_changed(y_key, pdef_y, y_kfs, node_id)
            dialog.accept()

        button_box.accepted.connect(_on_accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        # A2: track + non-modal show (mirrors the single-axis editor).
        dialog.finished.connect(self._close_active_kf_dialog)
        self._active_kf_dialog = dialog
        self._active_kf_editor = editor
        dialog.setModal(False)
        dialog.show()

    def _build_linked_y_keyframes(self, y_key: str, pdef_y: Any,
                                  x_keyframes: list) -> List[Dict[str, Any]]:
        """Build Y-axis keyframes that mirror X's frames but keep Y's values.

        For each X keyframe frame, the Y value is taken from:
          1. an existing Y track at that exact frame, else
          2. the Y track's interpolated value at that frame, else
          3. the node's current Y parameter value.
        Interpolation type is copied from the corresponding X keyframe so the
        two axes ease identically.
        """
        node = self._get_live_node(self._current_node_id)
        y_track = None
        y_default = 0.0
        if node is not None:
            y_default = float(node.parameters.get(y_key, 0.0) or 0.0)
            from src.shared.keyframe import KeyframeTrack
            y_data = node.keyframe_tracks.get(y_key)
            if isinstance(y_data, dict):
                y_track = KeyframeTrack.from_dict(y_data)

        result = []
        for xkf in x_keyframes:
            frame = getattr(xkf, 'frame', 0)
            if y_track is not None and y_track.keyframes:
                value = y_track.get_value(frame, default=y_default)
            else:
                value = y_default
            result.append({
                "frame": frame,
                "value": value,
                "interpolation": getattr(xkf, 'interpolation', None).value
                                 if getattr(xkf, 'interpolation', None) is not None
                                 else "linear",
                "bezier_in": list(getattr(xkf, 'bezier_in', (0.2, 0.0))),
                "bezier_out": list(getattr(xkf, 'bezier_out', (0.8, 1.0))),
            })
        return result

    # ──────────────────────────────────────────────────────────────
    # Constant Node — Custom UI Builder
    # ──────────────────────────────────────────────────────────────

    # Size presets. Width/height are the actual pixel dimensions; portrait
    # presets have height > width. Grouped by category in source for
    # readability; the combo lists them flat with the resolution in parentheses.
    _SIZE_PRESETS: List[Tuple[str, int, int]] = [
        # ── Video (landscape) ──
        ("SD NTSC", 720, 486),
        ("SD PAL", 720, 576),
        ("HD 720", 1280, 720),
        ("HD 1080", 1920, 1080),
        ("UHD 4K", 3840, 2160),
        ("DCI 4K", 4096, 2160),
        ("UHD 8K", 7680, 4320),
        # ── Video (portrait / vertical) ──
        ("HD 720 Vertical", 720, 1280),
        ("HD 1080 Vertical", 1080, 1920),
        ("UHD 4K Vertical", 2160, 3840),
        ("4:5 Portrait", 1080, 1350),
        ("2:3 Portrait", 1080, 1620),
        # ── Square ──
        ("Square 1024", 1024, 1024),
        ("Square 2048", 2048, 2048),
        ("Square 4096", 4096, 4096),
        # ── Paper / Print (300 DPI friendly pixel sizes) ──
        # A-series at 300 DPI: A4 = 210×297mm, A3 = 297×420mm.
        ("A4 (Portrait)", 2480, 3508),
        ("A4 (Landscape)", 3508, 2480),
        ("A3 (Portrait)", 3508, 4961),
        ("A3 (Landscape)", 4961, 3508),
        ("A2 (Portrait)", 4961, 7016),
        ("Letter (Portrait)", 2550, 3300),
        # ── Common poster / print sizes ──
        ("Poster 24×36in", 7200, 10800),
        ("Poster 18×24in", 5400, 7200),
        ("Poster 11×17in", 3300, 5100),
        # ── Photo / social ──
        ("Instagram Square", 1080, 1080),
        ("Instagram Portrait 4:5", 1080, 1350),
        ("Facebook Cover", 820, 312),
        ("Twitter Header", 1500, 500),
    ]

    _DIRECTION_LABELS: Dict[str, str] = {
        "horizontal": "Horizontal",
        "vertical": "Vertical",
        "diagonal_tl_br": "Diagonal ↘",
        "diagonal_tr_bl": "Diagonal ↙",
    }

    # ── Blur node UI ──

    def _handle_blur_type_change(self, blur_type: str,
                                 node_id: Optional[str] = None) -> None:
        """Rebuild Blur node UI when the blur_type combo changes.

        Mirrors _handle_constant_mode_change: rebuilds the parameter rows so
        only the parameters relevant to the active type are shown. The rebuild
        creates a new type combo whose ``currentTextChanged`` would re-trigger
        this handler, so ``_block_signals`` guards against the cascade.
        ``node_id`` routes the rebuild to the owning section (AUD-10700).
        """
        nid = node_id if node_id is not None else self._current_node_id
        section = self._sections.get(nid) if nid else None
        if section is None:
            return
        if (section.node_type or self._current_node_type) != "blur":
            return

        from src.features.composite.nodes.blur_node import BlurNode
        section._hidden_params = BlurNode.blur_type_param_visibility(
            blur_type, section.parameters.get("motion_source", "angle"))
        self._block_signals = True
        try:
            section._build_parameters()
        finally:
            self._block_signals = False

    def _build_blur_ui(self, section: "NodeSectionWidget",
                       layout: QVBoxLayout) -> None:
        """Build the mode-aware UI for the Blur node.

        Renders the blur_type combo, the parameters relevant to the active
        type (via BlurNode.blur_type_param_visibility), the RGBA channel button
        group, and the mix slider. Uses the generic _create_param_row_for_section
        for each visible parameter so the standard value/sync machinery applies.
        """
        from src.features.composite.nodes.blur_node import BlurNode

        blur_type = section.parameters.get("blur_type", "box")
        # Backward compat: legacy "directional" shown/normalised as "motion".
        if blur_type == "directional":
            blur_type = "motion"
        section._hidden_params = BlurNode.blur_type_param_visibility(
            blur_type, section.parameters.get("motion_source", "angle"))

        params = section.parameters
        pdefs = section.param_defs
        popts = section.param_options

        # Compact label widths for blur's short row names.
        self._label_min_width = 0

        # All params are handled here; we explicitly render the ones we want.
        # _skip_params marks every key as "owned by this custom builder" so the
        # generic _build_parameters loop will not also render them. We then
        # render exactly the visible subset here.
        section._skip_params = set(params.keys())

        # ── Blur type combo ──
        type_row = self._create_param_row_for_section(
            section.node_id, "blur_type", params.get("blur_type", "box"),
            popts, pdefs, section)
        layout.addWidget(type_row)

        # ── Type-specific + common params (order preserved from PARAM_DEFS) ──
        # Render every visible (not in _hidden_params, not hidden flag) parameter
        # in PARAM_DEFS order, except blur_type (above), channel_* (RGBA group
        # below) and mix (rendered after the channel group). Filtering is driven
        # by _hidden_params — NOT _skip_params, which only governs the generic
        # loop and would otherwise suppress everything we want to show.
        #
        # radius_x/radius_y (separable types only) are rendered as a single
        # X/Y paired row with a ⛓ chain toggle (linked by default), reusing the
        # same _create_xy_param_row_for_section path as transform scale.
        channel_keys = {"channel_r", "channel_g", "channel_b", "channel_a"}
        radius_pair = {"radius_x", "radius_y"}
        radius_rendered = False
        for pdef in BlurNode.PARAM_DEFS:
            key = pdef.name
            if key == "blur_type":
                continue  # already rendered above
            if key in channel_keys:
                continue  # rendered as the RGBA button group below
            if key == "mix":
                continue  # rendered after the channel group
            if key in section._hidden_params:
                continue
            if getattr(pdef, 'hidden', False):
                continue
            if key not in params:
                continue
            # Render the radius_x/radius_y pair as a single chained XY row.
            if key in radius_pair and not radius_rendered \
                    and "radius_x" in params and "radius_y" in params \
                    and "radius_x" not in section._hidden_params \
                    and "radius_y" not in section._hidden_params:
                row = self._create_xy_param_row_for_section(
                    section.node_id, "radius", "radius_x", "radius_y",
                    params["radius_x"], params["radius_y"],
                    popts, pdefs, section,
                    slider_holder=section._pair_sliders)
                layout.addWidget(row)
                radius_rendered = True
                continue
            if key in radius_pair:
                # Pair already rendered (or the other half is hidden) — skip
                # the individual rendering to avoid duplicate rows.
                if radius_rendered:
                    continue
            row = self._create_param_row_for_section(
                section.node_id, key, params[key], popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(row)

        # ── Channels: RGBA button group ──
        channel_row = self._create_blur_channel_group(section)
        layout.addWidget(channel_row)

        # ── Mix ──
        if "mix" in params and "mix" not in section._hidden_params:
            mix_row = self._create_param_row_for_section(
                section.node_id, "mix", params["mix"], popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(mix_row)

    def _create_blur_channel_group(self, section: "NodeSectionWidget") -> QWidget:
        """Create a horizontal RGBA channel toggle button group.

        Four checkable buttons (R/G/B/A). Each writes back to the matching
        channel_* parameter. Default: R/G/B checked, A unchecked. The buttons
        are independent (not mutually exclusive) — any combination is valid.
        """
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label(tr("channels.label", default="Channels"))
        h.addWidget(label)

        channels = [
            ("channel_r", "R", "#d44"),   # red
            ("channel_g", "G", "#4d4"),   # green
            ("channel_b", "B", "#44d"),   # blue
            ("channel_a", "A", "#999"),   # grey (alpha)
        ]

        for key, text, color in channels:
            btn = QPushButton(text, widget)
            btn.setCheckable(True)
            btn.setChecked(bool(section.parameters.get(key, key != "channel_a")))
            btn.setFixedSize(28, 20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"Toggle {text} channel blur")
            # Per-channel accent colour when checked.
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 1px solid #555;
                    border-radius: 3px;
                    color: #888;
                    font-weight: bold;
                    padding: 0px;
                }}
                QPushButton:checked {{
                    color: {color};
                    border-color: {color};
                    background: rgba(255,255,255,0.05);
                }}
                QPushButton:hover {{ border-color: #888; }}
            """)

            def _on_toggle(checked=False, k=key, n=section.node_id) -> None:
                self._on_param_changed(k, bool(checked), n)

            btn.clicked.connect(_on_toggle)
            # Register so external value updates reflect on the button.
            section._param_widgets[key] = {'type': 'bool_channel', 'widget': btn}
            h.addWidget(btn)

        h.addStretch()
        return widget

    def _handle_keyer_mode_change(self, keyer_mode: str,
                                  node_id: Optional[str] = None) -> None:
        """Rebuild Keyer node UI when the keyer_mode combo changes.

        Mirrors _handle_blur_type_change: rebuilds the parameter rows so only
        the parameters relevant to the active mode are shown. The rebuild
        creates a new mode combo whose ``currentIndexChanged`` would re-trigger
        this handler, so ``_block_signals`` guards against the cascade.
        ``node_id`` routes the rebuild to the owning section (AUD-10700).
        """
        nid = node_id if node_id is not None else self._current_node_id
        section = self._sections.get(nid) if nid else None
        if section is None:
            return
        if (section.node_type or self._current_node_type) != "keyer":
            return

        from src.features.composite.nodes.keyer_node import KeyerNode
        section._hidden_params = KeyerNode.keyer_mode_param_visibility(keyer_mode)
        self._block_signals = True
        try:
            section._build_parameters()
        finally:
            self._block_signals = False

    def _build_keyer_ui(self, section: "NodeSectionWidget",
                        layout: QVBoxLayout) -> None:
        """Build the mode-aware UI for the Keyer node.

        Renders the keyer_mode combo, then the parameters relevant to the
        active mode (via KeyerNode.keyer_mode_param_visibility). For chroma
        mode, the screen colour is shown as a colour swatch + eyedropper row
        (the same picker the Constant node uses) instead of three raw R/G/B
        sliders, so users can pick the screen colour directly from the image.
        """
        from src.features.composite.nodes.keyer_node import KeyerNode

        keyer_mode = section.parameters.get("keyer_mode", "auto")
        section._hidden_params = KeyerNode.keyer_mode_param_visibility(keyer_mode)

        params = section.parameters
        pdefs = section.param_defs
        popts = section.param_options

        # Compact label widths for keyer's short row names.
        self._label_min_width = 0

        # All params are handled here; we explicitly render the ones we want.
        # _skip_params marks every key as "owned by this custom builder" so the
        # generic _build_parameters loop will not also render them.
        section._skip_params = set(params.keys())

        # ── Keyer mode combo ──
        mode_row = self._create_param_row_for_section(
            section.node_id, "keyer_mode", params.get("keyer_mode", "auto"),
            popts, pdefs, section)
        layout.addWidget(mode_row)

        # ── Chroma mode: screen colour picker + balance ──
        screen_keys = {"screen_color_r", "screen_color_g", "screen_color_b",
                       "screen_color_hex"}
        if keyer_mode in ("auto", "chroma"):
            # Colour swatch + eyedropper row (replaces raw R/G/B sliders).
            # AUD-10707: screen_color_r/g/b are scene-linear; the hex fallback
            # is a display code, so encode linear→sRGB before formatting.
            from src.shared.ui.widgets.color_picker_control import (
                linear_rgba_to_qcolor,
            )
            hex_color = params.get(
                "screen_color_hex",
                linear_rgba_to_qcolor((
                    float(params.get("screen_color_r", 0.0)),
                    float(params.get("screen_color_g", 1.0)),
                    float(params.get("screen_color_b", 0.0)),
                    1.0)).name())
            color_row = self._create_keyer_color_row(section, hex_color)
            layout.addWidget(color_row)
            # screen_balance slider (chroma-specific).
            if "screen_balance" in params:
                row = self._create_param_row_for_section(
                    section.node_id, "screen_balance",
                    params["screen_balance"], popts, pdefs, section,
                    slider_holder=section._slidable_sliders)
                layout.addWidget(row)

        # ── Mode-specific + common params in PARAM_DEFS order ──
        for pdef in KeyerNode.PARAM_DEFS:
            key = pdef.name
            if key == "keyer_mode":
                continue  # already rendered above
            if key in screen_keys:
                continue  # rendered as the colour row above (chroma only)
            if key == "screen_balance":
                continue  # rendered above in chroma block
            if key in section._hidden_params:
                continue
            if getattr(pdef, 'hidden', False):
                continue
            if key not in params:
                continue
            row = self._create_param_row_for_section(
                section.node_id, key, params[key], popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(row)

    @staticmethod
    def _rgba_to_hex(r: float, g: float, b: float) -> str:
        """Convert normalized RGB floats (0-1) to '#rrggbb' hex string."""
        ri = max(0, min(255, int(r * 255.0 + 0.5)))
        gi = max(0, min(255, int(g * 255.0 + 0.5)))
        bi = max(0, min(255, int(b * 255.0 + 0.5)))
        return f"#{ri:02x}{gi:02x}{bi:02x}"

    def _create_keyer_color_row(self, section: "NodeSectionWidget",
                                hex_color: str) -> QWidget:
        """Create a screen-colour picker row for the Keyer (chroma mode).

        Square colour swatch (28x28) + eyedropper button. Clicking the swatch
        opens the project's CompactColorStudio picker; the eyedropper picks any
        colour from the screen (e.g. the greenscreen in the viewport). Writes
        the chosen colour back to screen_color_r/g/b + screen_color_hex.

        Mirrors ConstantNode's _create_constant_color_button but for the
        keyer's RGB-only screen colour (no alpha — the screen colour is a hue
        reference, not a composited pixel).
        """
        from src.shared.assets.icon_manager import icon_color_picker

        r_key, g_key, b_key = "screen_color_r", "screen_color_g", "screen_color_b"
        hex_key = "screen_color_hex"

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        lbl = self._make_param_label(
            tr("nodes.keyer.params.screen_color", default="Screen Color"))
        h.addWidget(lbl)

        # ── Square colour swatch ──
        btn = QPushButton()
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(
            I18N.tr("node_props.screen_colour_pick", "Screen Colour — click to pick"))

        def _update_btn_style(hex_str: str) -> None:
            qcolor = QColor(hex_str)
            ri, gi, bi = qcolor.red(), qcolor.green(), qcolor.blue()
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgb({ri}, {gi}, {bi});
                    border: 1px solid #555;
                    border-radius: 3px;
                }}
                QPushButton:hover {{ border: 1px solid #888; }}
            """)

        _update_btn_style(hex_color)

        def _apply_color(color: QColor, n=section.node_id) -> None:
            """Write the chosen colour back to RGB + hex params.

            AUD-10707: screen_color_r/g/b feed the chroma-keyer shader, which
            compares against the scene-linear image texture — convert the
            sRGB QColor to linear exactly once here (mirrors the Constant
            colour button). ``screen_color_hex`` stays a display code.
            """
            from src.shared.ui.widgets.color_picker_control import (
                qcolor_to_linear_rgba,
            )
            lr, lg, lb, _a = qcolor_to_linear_rgba(color)
            _update_btn_style(color.name())
            self._constant_batch_update = True
            try:
                self._on_param_changed(r_key, lr, n)
                self._on_param_changed(g_key, lg, n)
                self._on_param_changed(b_key, lb, n)
                self._on_param_changed(hex_key, color.name(), n)
            finally:
                self._constant_batch_update = False

        def _pick() -> None:
            from src.shared.color import get_color
            from src.shared.ui.widgets.color_picker_control import (
                linear_rgba_to_qcolor,
            )
            # Read the stored linear channels back as a display-referred
            # QColor for the picker's initial state.
            current = linear_rgba_to_qcolor((
                float(section.parameters.get(r_key, 0.0)),
                float(section.parameters.get(g_key, 1.0)),
                float(section.parameters.get(b_key, 0.0)),
                1.0))
            result = get_color(parent=self, initial=current, on_live=_apply_color)
            if result is not None:
                _apply_color(result)

        btn.clicked.connect(_pick)
        h.addWidget(btn)

        # ── Eyedropper button (pick colour from screen) ──
        picker_btn = QPushButton()
        picker_btn.setFixedSize(28, 28)
        picker_btn.setIcon(icon_color_picker())
        picker_btn.setIconSize(QSize(16, 16))
        picker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        picker_btn.setToolTip(
            I18N.tr("node_props.pick_screen_colour", "Pick screen colour from screen"))
        picker_btn.setStyleSheet("""
            QPushButton {
                background-color: #333; border: 1px solid #444; border-radius: 3px;
            }
            QPushButton:hover { background-color: #444; border: 1px solid #666; }
        """)

        def _start_eyedropper() -> None:
            from src.shared.color import EyeDropperOverlay
            # Store as instance attribute to prevent garbage collection
            # (EyeDropperOverlay is a QWidget; losing the reference kills it
            # before mousePressEvent can fire).
            self._active_dropper = EyeDropperOverlay()
            self._active_dropper.colorPicked.connect(_apply_color)
            self._active_dropper.start()

        picker_btn.clicked.connect(_start_eyedropper)
        h.addWidget(picker_btn)

        h.addStretch()

        section._param_widgets[hex_key] = {
            'type': 'color', 'widget': btn,
            'update': lambda hc: _update_btn_style(hc)
        }

        return row

    def _handle_constant_mode_change(self, mode: str,
                                     node_id: Optional[str] = None) -> None:
        """Rebuild Constant node UI when mode combo changes.

        Guards against infinite recursion: rebuilding creates a new mode combo
        whose ``currentTextChanged`` would re-trigger this handler.  Setting
        ``_block_signals`` during the rebuild prevents the cascade.
        ``node_id`` routes the rebuild to the owning section (AUD-10700).
        """
        nid = node_id if node_id is not None else self._current_node_id
        section = self._sections.get(nid) if nid else None
        if section is None:
            return
        if (section.node_type or self._current_node_type) != "constant":
            return

        from src.features.composite.nodes.constant_node import ConstantNode
        section._hidden_params = ConstantNode.mode_param_visibility(mode)
        self._block_signals = True
        try:
            section._build_parameters()
        finally:
            self._block_signals = False

    # ── Text node ──

    def _handle_text_state_change(self, node_id: Optional[str] = None) -> None:
        """Rebuild Text node UI when outline_enable / shadow_enable toggle.

        Mirrors _handle_constant_mode_change: rebuilding creates a new
        checkbox whose ``stateChanged`` would re-trigger this handler, so
        ``_block_signals`` guards against the cascade.
        ``node_id`` routes the rebuild to the owning section (AUD-10700).
        """
        nid = node_id if node_id is not None else self._current_node_id
        section = self._sections.get(nid) if nid else None
        if section is None:
            return
        if (section.node_type or self._current_node_type) != "text":
            return
        from src.features.composite.nodes.text_node import TextNode
        section._hidden_params = TextNode.param_visibility(section.parameters)
        self._block_signals = True
        try:
            section._build_parameters()
        finally:
            self._block_signals = False

    def _build_text_ui(self, section: "NodeSectionWidget",
                       layout: QVBoxLayout) -> None:
        """Build a clean, state-aware UI for the Text node.

        Layout (top-to-bottom):
          Size preset → Width/Height → Text (multi-line) → Font (family,
          size, weight, italic) → Alignment (h/v) → Spacing (letter, line)
          → Anchor (x/y) → Transform (rotation, scale) → Fill color →
          Outline (toggle + group) → Shadow (toggle + group).
        """
        from src.features.composite.nodes.text_node import TextNode

        # Compute hidden params from current state (outline/shadow toggles).
        section._hidden_params = TextNode.param_visibility(section.parameters)

        params = section.parameters
        pdefs = section.param_defs
        popts = section.param_options

        # Text's labels hug their widgets (see Constant for rationale).
        self._label_min_width = 0
        section._skip_params = set(params.keys())

        # ── Size preset ──
        self._build_text_size_row(section, params, layout)

        # Width / Height on a single row with a ⛓ aspect-ratio lock.
        size_row = self._create_xy_param_row_for_section(
            node_id=section.node_id,
            base="size",
            x_key="width", y_key="height",
            x_val=int(params.get("width", 1920)),
            y_val=int(params.get("height", 1080)),
            param_options=popts, param_defs=pdefs,
            section=section,
            slider_holder=section._slidable_sliders)
        layout.addWidget(size_row)
        section._skip_params.discard("width")
        section._skip_params.discard("height")
        self._wire_text_size_combo_sync(section)

        # ── Text content (multi-line editor) ──
        text_row = self._build_text_content_row(section, params)
        layout.addWidget(text_row)
        section._skip_params.discard("text")

        # ── Font family + size ──
        font_row = self._build_text_font_row(section, params, pdefs)
        layout.addWidget(font_row)
        section._skip_params.discard("font_family")
        section._skip_params.discard("font_size")

        # ── Weight + Italic ──
        weight_row = self._create_param_row_for_section(
            section.node_id, "font_weight", params.get("font_weight", "normal"),
            popts, pdefs, section)
        layout.addWidget(weight_row)
        section._skip_params.discard("font_weight")

        italic_row = self._create_param_row_for_section(
            section.node_id, "italic", params.get("italic", False),
            popts, pdefs, section)
        layout.addWidget(italic_row)
        section._skip_params.discard("italic")

        # ── Alignment (h + v) ──
        halign_row = self._create_param_row_for_section(
            section.node_id, "halign", params.get("halign", "center"),
            popts, pdefs, section)
        layout.addWidget(halign_row)
        section._skip_params.discard("halign")

        valign_row = self._create_param_row_for_section(
            section.node_id, "valign", params.get("valign", "middle"),
            popts, pdefs, section)
        layout.addWidget(valign_row)
        section._skip_params.discard("valign")

        # ── Spacing (letter + line) ──
        ls_row = self._create_param_row_for_section(
            section.node_id, "letter_spacing", params.get("letter_spacing", 0.0),
            popts, pdefs, section,
            slider_holder=section._slidable_sliders)
        layout.addWidget(ls_row)
        section._skip_params.discard("letter_spacing")

        linespacing_row = self._create_param_row_for_section(
            section.node_id, "line_spacing", params.get("line_spacing", 1.0),
            popts, pdefs, section,
            slider_holder=section._slidable_sliders)
        layout.addWidget(linespacing_row)
        section._skip_params.discard("line_spacing")

        # ── Transform: translate (px) ──
        # These mirror the Transform node exactly so the on-canvas transform
        # handles (move/scale/rotate gizmo) drive them 1:1.
        translate_row = self._create_xy_param_row_for_section(
            node_id=section.node_id,
            base="translate",
            x_key="translate_x", y_key="translate_y",
            x_val=float(params.get("translate_x", 0.0)),
            y_val=float(params.get("translate_y", 0.0)),
            param_options=popts, param_defs=pdefs,
            section=section,
            slider_holder=section._pair_sliders)
        layout.addWidget(translate_row)
        section._skip_params.discard("translate_x")
        section._skip_params.discard("translate_y")

        # ── Transform: scale (x/y) ──
        scale_row = self._create_xy_param_row_for_section(
            node_id=section.node_id,
            base="scale",
            x_key="scale_x", y_key="scale_y",
            x_val=float(params.get("scale_x", 1.0)),
            y_val=float(params.get("scale_y", 1.0)),
            param_options=popts, param_defs=pdefs,
            section=section,
            slider_holder=section._pair_sliders)
        layout.addWidget(scale_row)
        section._skip_params.discard("scale_x")
        section._skip_params.discard("scale_y")

        # ── Transform: rotation ──
        rot_row = self._create_param_row_for_section(
            section.node_id, "rotation", params.get("rotation", 0.0),
            popts, pdefs, section,
            slider_holder=section._slidable_sliders)
        layout.addWidget(rot_row)
        section._skip_params.discard("rotation")

        # ── Transform: pivot mode ──
        pivot_mode_row = self._create_param_row_for_section(
            section.node_id, "pivot_mode", params.get("pivot_mode", "center"),
            popts, pdefs, section)
        layout.addWidget(pivot_mode_row)
        section._skip_params.discard("pivot_mode")

        # ── Transform: pivot (only in custom mode) ──
        if str(params.get("pivot_mode", "center")) == "custom":
            pivot_row = self._create_xy_param_row_for_section(
                node_id=section.node_id,
                base="pivot",
                x_key="pivot_x", y_key="pivot_y",
                x_val=float(params.get("pivot_x", 0.0)),
                y_val=float(params.get("pivot_y", 0.0)),
                param_options=popts, param_defs=pdefs,
                section=section,
                slider_holder=section._pair_sliders)
            layout.addWidget(pivot_row)
            section._skip_params.discard("pivot_x")
            section._skip_params.discard("pivot_y")

        # ── Transform: filter ──
        filter_row = self._create_param_row_for_section(
            section.node_id, "filter", params.get("filter", "linear"),
            popts, pdefs, section)
        layout.addWidget(filter_row)
        section._skip_params.discard("filter")

        # ── Fill color ──
        fill_hex = TextNode.rgba_to_hex(
            float(params.get("fill_r", 1.0)),
            float(params.get("fill_g", 1.0)),
            float(params.get("fill_b", 1.0)))
        fill_btn = self._create_constant_color_button(
            section, fill_hex,
            r_key="fill_r", g_key="fill_g", b_key="fill_b", a_key="fill_a",
            hex_key="fill_hex", label="Fill Color")
        layout.addWidget(fill_btn)

        # ── Outline group ──
        outline_toggle_row = self._create_param_row_for_section(
            section.node_id, "outline_enable",
            params.get("outline_enable", False),
            popts, pdefs, section)
        layout.addWidget(outline_toggle_row)
        section._skip_params.discard("outline_enable")

        if bool(params.get("outline_enable", False)):
            ow_row = self._create_param_row_for_section(
                section.node_id, "outline_width", params.get("outline_width", 4.0),
                popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(ow_row)
            section._skip_params.discard("outline_width")

            outline_hex = TextNode.rgba_to_hex(
                float(params.get("outline_r", 0.0)),
                float(params.get("outline_g", 0.0)),
                float(params.get("outline_b", 0.0)))
            outline_btn = self._create_constant_color_button(
                section, outline_hex,
                r_key="outline_r", g_key="outline_g",
                b_key="outline_b", a_key="outline_a",
                hex_key="outline_hex", label="Outline Color")
            layout.addWidget(outline_btn)
            section._skip_params.discard("outline_r")
            section._skip_params.discard("outline_g")
            section._skip_params.discard("outline_b")
            section._skip_params.discard("outline_a")

        # ── Shadow group ──
        shadow_toggle_row = self._create_param_row_for_section(
            section.node_id, "shadow_enable",
            params.get("shadow_enable", False),
            popts, pdefs, section)
        layout.addWidget(shadow_toggle_row)
        section._skip_params.discard("shadow_enable")

        if bool(params.get("shadow_enable", False)):
            offset_row = self._create_xy_param_row_for_section(
                node_id=section.node_id,
                base="shadow_offset",
                x_key="shadow_offset_x", y_key="shadow_offset_y",
                x_val=float(params.get("shadow_offset_x", 8.0)),
                y_val=float(params.get("shadow_offset_y", 8.0)),
                param_options=popts, param_defs=pdefs,
                section=section,
                slider_holder=section._pair_sliders)
            layout.addWidget(offset_row)
            section._skip_params.discard("shadow_offset_x")
            section._skip_params.discard("shadow_offset_y")

            blur_row = self._create_param_row_for_section(
                section.node_id, "shadow_blur", params.get("shadow_blur", 8.0),
                popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(blur_row)
            section._skip_params.discard("shadow_blur")

            shadow_hex = TextNode.rgba_to_hex(
                float(params.get("shadow_r", 0.0)),
                float(params.get("shadow_g", 0.0)),
                float(params.get("shadow_b", 0.0)))
            shadow_btn = self._create_constant_color_button(
                section, shadow_hex,
                r_key="shadow_r", g_key="shadow_g",
                b_key="shadow_b", a_key="shadow_a",
                hex_key="shadow_hex", label="Shadow Color")
            layout.addWidget(shadow_btn)
            section._skip_params.discard("shadow_r")
            section._skip_params.discard("shadow_g")
            section._skip_params.discard("shadow_b")
            section._skip_params.discard("shadow_a")

        # No per-source output_color_space row — Text rasterizes sRGB; the
        # central IDT converts to linear. (See PARAM_DEFS note in text_node.py.)

    def _build_text_size_row(self, section: "NodeSectionWidget",
                             params: Dict[str, Any],
                             layout: QVBoxLayout) -> None:
        """Build the size preset combo row for the Text node.

        Mirrors _build_constant_size_row. Selecting a preset writes width/
        height through _on_param_changed and reflects the values into the
        dimension drag boxes.
        """
        from src.features.composite.nodes.text_node import TextNode

        w = int(params.get("width", 1920))
        h = int(params.get("height", 1080))

        current_preset = "Custom"
        for name, pw, ph in TextNode.SIZE_PRESETS:
            if pw == w and ph == h:
                current_preset = name
                break

        row = QWidget()
        h_layout = QHBoxLayout(row)
        h_layout.setContentsMargins(0, 2, 0, 2)
        h_layout.setSpacing(4)

        label = self._make_param_label("Size")
        h_layout.addWidget(label)

        combo = ChevronComboBox()
        for name, pw, ph in TextNode.SIZE_PRESETS:
            combo.addItem(f"{name}  ({pw}×{ph})", (pw, ph))
        combo.addItem("Custom", None)

        idx = 0
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is None and current_preset == "Custom":
                idx = i
                break
            elif data and data[0] == w and data[1] == h:
                idx = i
                break
        combo.setCurrentIndex(idx)
        apply_combo_dropdown_style(combo)

        def _on_preset_changed(ci, n=section.node_id) -> None:
            data = combo.itemData(ci)
            if data is not None:
                pw, ph = data
                self._on_param_changed("width", pw, n)
                self._on_param_changed("height", ph, n)
                pw_info = section._param_widgets.get("width")
                ph_info = section._param_widgets.get("height")
                if pw_info:
                    self._update_section_param_value("width", pw, section._param_widgets)
                if ph_info:
                    self._update_section_param_value("height", ph, section._param_widgets)

        combo.currentIndexChanged.connect(_on_preset_changed)
        h_layout.addWidget(combo, 1)
        layout.addWidget(row)
        section._param_widgets["size_preset"] = {
            'type': 'combo', 'widget': combo}

    def _wire_text_size_combo_sync(self, section: "NodeSectionWidget") -> None:
        """Re-resolve the size combo whenever width or height changes.

        Mirrors _wire_constant_size_combo_sync: hook the section's
        parameter_changed signal (NOT a panel-level monkey patch) so the
        combo jumps to Custom unless the new size matches a preset. The
        refresh reads the live combo via section._param_widgets each time
        (so it stays valid across rebuilds) and is guarded by a
        one-shot-wired flag to avoid duplicate connections.
        """
        def _maybe_sync_combo(key) -> None:
            if key in ("width", "height"):
                self._refresh_text_size_combo(section)

        # The section relays every _on_param_changed via parameter_changed.
        # Connect once; guard against double-connection on rebuilds.
        if not getattr(section, "_size_combo_sync_wired", False):
            section.parameter_changed.connect(
                lambda nid, key, value: _maybe_sync_combo(key))
            section._size_combo_sync_wired = True

    def _refresh_text_size_combo(self, section: "NodeSectionWidget") -> None:
        """Re-resolve the size combo to match the current width/height.

        Called after the user drags Width or Height directly — the resulting
        size usually no longer matches any preset, so the combo should jump to
        ``Custom``. Silently no-ops if the combo isn't built yet (e.g. during
        teardown when the underlying C++ widget has already been deleted).
        """
        info = section._param_widgets.get("size_preset")
        if not info or info.get('type') != 'combo':
            return
        combo = info['widget']
        # Guard: the section may be rebuilding; the combo's C++ object can be
        # gone while Python still holds the reference. Bail out in that case.
        try:
            count = combo.count()
        except RuntimeError:
            return
        w = int(section.parameters.get("width", 0))
        h = int(section.parameters.get("height", 0))
        target_idx = count - 1  # "Custom" is always last
        for i in range(count):
            data = combo.itemData(i)
            if data is not None and data[0] == w and data[1] == h:
                target_idx = i
                break
        if combo.currentIndex() != target_idx:
            combo.blockSignals(True)
            combo.setCurrentIndex(target_idx)
            combo.blockSignals(False)

    def _build_text_content_row(self, section: "NodeSectionWidget",
                                params: Dict[str, Any]) -> QWidget:
        """Build the multi-line text content editor row.

        A QPlainTextEdit that grows with content, wired to update the
        ``text`` parameter on every change (live preview).
        """
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(2)

        label = self._make_param_label("Text")
        v.addWidget(label)

        editor = QPlainTextEdit()
        editor.setPlainText(str(params.get("text", "")))
        editor.setMinimumHeight(60)
        editor.setMaximumHeight(140)
        editor.setStyleSheet("""
            QPlainTextEdit {
                background-color: #2a2a2a; color: #ddd;
                border: 1px solid #444; border-radius: 3px;
                padding: 4px; font-size: 11px;
            }
            QPlainTextEdit:focus { border: 1px solid #666; }
        """)
        editor.setPlaceholderText(I18N.tr(
            "node_props.text_editor_placeholder",
            "Enter text... (Shift+Enter for newline)"))

        # Debounce-free live update: each keystroke triggers a re-render.
        # Text rasterization is cheap (one QImage + one texture upload), and
        # the render scheduler already coalesces rapid param changes.
        def _on_text_changed(n=section.node_id) -> None:
            new_text = editor.toPlainText()
            self._on_param_changed("text", new_text, n)

        editor.textChanged.connect(_on_text_changed)
        v.addWidget(editor)

        section._param_widgets["text"] = {
            'type': 'text', 'widget': editor,
            'update': lambda t: editor.blockSignals(True) or (
                editor.setPlainText(t), editor.blockSignals(False)),
        }
        return row

    def _build_text_font_row(self, section: "NodeSectionWidget",
                             params: Dict[str, Any],
                             pdefs: Dict) -> QWidget:
        """Build the font family combo + font size drag on one row.

        The family combo is populated from QFontDatabase so the user can
        pick any installed font. A free-text fallback lets them type a
        family that isn't enumerated (e.g. one Qt hasn't indexed yet).
        """
        from PyQt6.QtGui import QFontDatabase

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label("Font")
        h.addWidget(label)

        combo = ChevronComboBox()
        combo.setEditable(True)
        current_family = str(params.get("font_family", "Arial"))

        # Populate with installed font families (sorted, unique).
        try:
            db = QFontDatabase()
            families = sorted(set(db.families()))
        except Exception:
            families = ["Arial", "Helvetica", "Times New Roman", "Courier New"]
        # Always include the current family even if not in the database list
        # (e.g. a manually typed family on another machine).
        if current_family and current_family not in families:
            families = [current_family] + families
        for fam in families:
            combo.addItem(fam, fam)

        # Set current.
        idx = combo.findData(current_family)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current_family)
        apply_combo_dropdown_style(combo)

        def _on_family_changed(_idx_or_text, n=section.node_id) -> None:
            data = combo.currentData()
            new_family = data if data else combo.currentText()
            self._on_param_changed("font_family", new_family, n)

        combo.currentIndexChanged.connect(_on_family_changed)
        # editable combo: also catch direct text edits
        combo.editTextChanged.connect(lambda t: _on_family_changed(None))
        h.addWidget(combo, 1)

        section._param_widgets["font_family"] = {
            'type': 'combo', 'widget': combo,
            'update': lambda fam: (combo.blockSignals(True),
                                   combo.setEditText(str(fam)),
                                   combo.blockSignals(False)),
        }

        # Font size drag (reuse the generic single-row builder so it gets the
        # slider + numeric editor + keyframe/expression support for free).
        size_row = self._create_param_row_for_section(
            section.node_id, "font_size", params.get("font_size", 120),
            section.param_options, pdefs, section,
            slider_holder=section._slidable_sliders)
        # Move the size row's widget out of its own row container into this
        # row's layout so family + size sit on one line.
        for i in reversed(range(size_row.layout().count())):
            item = size_row.layout().itemAt(i)
            if item and item.widget():
                w_item = item.widget()
                size_row.layout().removeWidget(w_item)
                h.addWidget(w_item)
        size_row.setParent(None)
        return row

    # ── Main builder ──

    def _build_output_ui(self, section: "NodeSectionWidget",
                         layout: QVBoxLayout) -> None:
        """Build the Output node UI: standard parameter rows + a Render button.

        Renders the regular parameters (format_mode/width/height/
        output_color_space/alpha_policy/background_color) via the generic
        directive path so they stay wired to the value/sync machinery, then
        appends a full-width "Render Output" button at the bottom that emits
        the section's render_output_requested signal.
        """
        params = section.parameters

        # Output node has no grouped/hidden params; render all keys via the
        # generic directive builder (handles XY pairing + label alignment).
        section._skip_params = set()
        section._hidden_params = set()

        # Compute label min width from this node's param names.
        from PyQt6.QtGui import QFontMetrics, QFont
        fm = QFontMetrics(QFont("", 10))
        widest = 36
        for key in params.keys():
            name = key.replace("_", " ").title()
            widest = max(widest, fm.horizontalAdvance(name) + 12)
        self._label_min_width = widest

        # Build all standard parameter rows into the layout.
        section._build_directives_into(layout, list(params.keys()))

        # ── Render button ──
        btn_row_widget = QFrame()
        btn_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        btn_layout = QHBoxLayout(btn_row_widget)
        btn_layout.setContentsMargins(0, 6, 0, 2)
        btn_layout.setSpacing(0)

        render_btn = QPushButton(
            tr("composite.render_output.render_btn", "渲染输出 / Render Output")
        )
        render_btn.setStyleSheet(
            "QPushButton{background:#cc6600;color:white;border:none;"
            "border-radius:4px;padding:6px 12px;font-weight:bold;}"
            "QPushButton:hover{background:#e67300;}"
            "QPushButton:pressed{background:#b35900;}"
            "QPushButton:disabled{background:#444;color:#888;}"
        )
        render_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Emit the render signal up through the section → panel → controller.
        node_id = section.node_id
        render_btn.clicked.connect(
            lambda _checked=False, nid=node_id: section.render_output_requested.emit(nid)
        )
        btn_layout.addWidget(render_btn, 1)
        layout.addWidget(btn_row_widget)

    def _build_scene3d_ui(self, section: "NodeSectionWidget",
                          layout: QVBoxLayout) -> None:
        """Build the Scene3D UI: a 2D/3D preview-mode toggle + standard params.

        顶部一个 2D/3D 预览模式开关（纯视图控件，非节点参数——不进节点图/undo，
        不随 .tos 保存）。开关与 Tab 键完全等价、双向同步：切换发射
        section.view_mode_toggle_requested → panel → page → viewer.set_view_mode
        （与 Tab 同路径）。
        """
        params = section.parameters
        # ── 2D/3D 预览模式开关（视图状态）──
        toggle_row = QFrame()
        toggle_row.setFrameShape(QFrame.Shape.NoFrame)
        tr_layout = QHBoxLayout(toggle_row)
        tr_layout.setContentsMargins(0, 2, 0, 6)
        tr_layout.setSpacing(6)

        lbl = QLabel(tr("composite.scene3d.preview_mode",
                        "预览模式 / Preview"))
        lbl.setStyleSheet("color:#bbb;font-size:11px;font-weight:bold;")
        tr_layout.addWidget(lbl)

        # 用一个互斥的两按钮组（2D / 3D），高亮当前模式。
        btn_2d = QPushButton(I18N.tr("node_props.mode_2d", "2D"))
        btn_3d = QPushButton(I18N.tr("node_props.mode_3d", "3D"))
        for b in (btn_2d, btn_3d):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(22)
            b.setStyleSheet(
                "QPushButton{background:#333;color:#bbb;border:1px solid #555;"
                "border-radius:3px;padding:2px 10px;font-size:11px;font-weight:bold;}"
                "QPushButton:checked{background:#2d6a4f;color:white;border-color:#4fae7a;}"
            )
        cur_mode = getattr(self, "_current_view_mode", "2d")
        (btn_3d if cur_mode == "3d" else btn_2d).setChecked(True)
        # 注册到面板，set_view_mode（Tab 切换时）可即时刷新按钮高亮。
        if not hasattr(self, "_scene3d_toggle_buttons"):
            self._scene3d_toggle_buttons = {}
        self._scene3d_toggle_buttons[section.node_id] = (btn_2d, btn_3d)
        btn_2d.clicked.connect(
            lambda _c=False: section.view_mode_toggle_requested.emit("2d")
        )
        btn_3d.clicked.connect(
            lambda _c=False: section.view_mode_toggle_requested.emit("3d")
        )
        tr_layout.addWidget(btn_2d)
        tr_layout.addWidget(btn_3d)
        tr_layout.addStretch(1)
        layout.addWidget(toggle_row)

        # ── 场景设置提示：相机/灯光/背景/IBL/网格/MSAA 等已移至全局设置 ──
        # Scene3D 是纯渲染器，场景环境参数统一在「3D Scene Settings」
        # （Properties 面板齿轮 → 3D Scene Settings）管理，所有 Scene3D 共享。
        hint_row = QFrame()
        hint_row.setFrameShape(QFrame.Shape.NoFrame)
        hr_layout = QHBoxLayout(hint_row)
        hr_layout.setContentsMargins(0, 2, 0, 6)
        hr_layout.setSpacing(4)
        scene_hint = QLabel(tr(
            "composite.scene3d.scene_settings_hint",
            "场景设置（相机/灯光/背景/IBL）在 3D Scene Settings 中统一管理"))
        scene_hint.setStyleSheet("color:#888;font-size:10px;font-style:italic;")
        scene_hint.setWordWrap(True)
        hr_layout.addWidget(scene_hint)
        layout.addWidget(hint_row)

        # ── P2: 相机/灯光/几何连接状态提示（连接的 payload 覆盖全局默认）──
        # 当 Camera3D 连到 in 端口时，全局默认相机被 payload 覆盖，
        # 显示提示避免"改了全局 cam_* 却没反应"的困惑。light/geo 同理。
        self._maybe_add_scene3d_connection_hint(section, layout)

        # ── 标准 Scene3D 参数行（width/height 等渲染器参数）──
        # 场景环境参数（cam_*/bg_*/light_*/hdri_preset 等）已移至全局设置，
        # 几何/材质参数由 Geo3D/Model3D 提供，不再在此面板显示。
        section._skip_params = set()
        section._hidden_params = set()
        from PyQt6.QtGui import QFontMetrics, QFont
        fm = QFontMetrics(QFont("", 10))
        widest = 36
        for key in params.keys():
            name = key.replace("_", " ").title()
            widest = max(widest, fm.horizontalAdvance(name) + 12)
        self._label_min_width = widest
        section._build_directives_into(layout, list(params.keys()))

    def _build_camera3d_ui(self, section: "NodeSectionWidget",
                           layout: QVBoxLayout) -> None:
        """Build Camera3D parameters.

        Camera view activation lives in the section header toggle. Keeping a
        second "Look Through" button here makes it too easy to enter Camera view
        without an obvious off switch, which conflicts with the global Viewer
        camera workflow.
        """
        params = section.parameters
        section._skip_params = set()
        section._hidden_params = set()
        from PyQt6.QtGui import QFontMetrics, QFont
        fm = QFontMetrics(QFont("", 10))
        widest = 36
        for key in params.keys():
            name = key.replace("_", " ").title()
            widest = max(widest, fm.horizontalAdvance(name) + 12)
        self._label_min_width = widest
        section._build_directives_into(layout, list(params.keys()))

    def _build_tracker_ui(self, section: "NodeSectionWidget",
                          layout: QVBoxLayout) -> None:
        """A1: Tracker properties — Analyze / Cancel / match-move baking.

        Pattern params (mode / reference_frame / pattern rect) and the
        serialized track_data continue through the normal parameter
        factory; this adds the action rows on top.
        """
        from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton

        from src.shared.i18n.i18n import I18N

        node_id = section.node_id

        actions = QHBoxLayout()
        analyze_btn = QPushButton(I18N.tr(
            "nodes.tracker.analyze", "Analyze"))
        analyze_btn.setToolTip(I18N.tr(
            "nodes.tracker.analyze_tooltip", "跟踪图案区域并存储逐帧结果"))
        analyze_btn.clicked.connect(
            lambda: self.tracker_analyze_requested.emit(node_id))
        cancel_btn = QPushButton(I18N.tr(
            "nodes.tracker.cancel", "Cancel"))
        cancel_btn.setToolTip(I18N.tr(
            "nodes.tracker.cancel_tooltip", "取消正在进行的分析"))
        cancel_btn.clicked.connect(
            lambda: self.tracker_cancel_requested.emit(node_id))
        import_btn = QPushButton(I18N.tr(
            "nodes.tracker.import", "Import from Color Page"))
        import_btn.setToolTip(I18N.tr(
            "nodes.tracker.import_tooltip", "导入 Color 页跟踪结果（共享算法存储）"))
        import_btn.clicked.connect(
            lambda: self.tracker_import_color_requested.emit(node_id))
        actions.addWidget(analyze_btn)
        actions.addWidget(cancel_btn)
        actions.addWidget(import_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        track = None
        try:
            from src.features.composite.nodes.tracker_node import (
                decode_track_data)
            track = decode_track_data(section.parameters.get("track_data"))
        except Exception:
            track = None
        if track:
            n_frames = len(track.get("transforms") or {})
            info = QLabel(
                f"{I18N.tr('nodes.tracker.tracked_frames', 'Tracked frames')}: "
                f"{n_frames} · "
                f"{I18N.tr('nodes.tracker.mode', 'mode')}: {track.get('mode', '?')} · "
                f"{I18N.tr('nodes.tracker.ref', 'ref')}: {track.get('reference_frame', '?')}")
            info.setStyleSheet("color: #9ad; font-size: 11px;")
            layout.addWidget(info)

        bake = QHBoxLayout()
        bake_transform = QPushButton(I18N.tr(
            "nodes.tracker.bake_transform", "Bake to Transform"))
        bake_transform.setToolTip(I18N.tr(
            "nodes.tracker.bake_transform_tooltip",
            "把跟踪结果烘焙为下游 Transform 节点的关键帧"))
        bake_transform.clicked.connect(
            lambda: self.tracker_bake_requested.emit(node_id, "transform"))
        bake_pin = QPushButton(I18N.tr(
            "nodes.tracker.bake_cornerpin", "Bake to CornerPin"))
        bake_pin.setToolTip(I18N.tr(
            "nodes.tracker.bake_cornerpin_tooltip",
            "把平面跟踪结果烘焙为下游 CornerPin 节点的四角关键帧"))
        bake_pin.clicked.connect(
            lambda: self.tracker_bake_requested.emit(node_id, "cornerpin"))
        bake.addWidget(bake_transform)
        bake.addWidget(bake_pin)
        stabilize_btn = QPushButton(I18N.tr(
            "nodes.tracker.bake_stabilize", "Bake Stabilize"))
        stabilize_btn.setToolTip(I18N.tr(
            "nodes.tracker.bake_stabilize_tooltip",
            "把平滑后的逆向运动烘焙为下游 Transform 关键帧（稳定画面）"))
        stabilize_btn.clicked.connect(
            lambda: self.tracker_stabilize_requested.emit(node_id))
        bake.addWidget(stabilize_btn)
        layout.addLayout(bake)

    def _build_video_frame_ui(self, section: "NodeSectionWidget",
                              layout: QVBoxLayout) -> None:
        """Build Video Frame properties with a combined In/Out trim row.

        ``source_in``/``source_out`` remain the persisted node parameters;
        this row is their compact, two-handle editor. The remaining video
        parameters continue through the normal parameter factory below it.
        """
        params = section.parameters
        try:
            total_frames = max(0, int(params.get("total_frames", 0) or 0))
        except (TypeError, ValueError):
            total_frames = 0
        try:
            start = max(0, int(params.get("source_in", 0) or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            raw_end = int(params.get("source_out", -1))
        except (TypeError, ValueError):
            raw_end = -1
        # Frame indices are zero-based, so a one-frame source ends at 0.
        # Keep a persisted out point visible while metadata is loading.
        maximum = max(0, total_frames - 1, start, raw_end)
        end = maximum if raw_end < 0 else max(start, min(raw_end, maximum))

        trim_row = QWidget()
        trim_layout = QHBoxLayout(trim_row)
        trim_layout.setContentsMargins(0, 2, 0, 3)
        trim_layout.setSpacing(4)
        trim_label = self._make_param_label(tr("nodes.video_frame.params.trim",
                                               "修剪"))
        trim_layout.addWidget(trim_label)
        trim_editor = TrimRangeEditor(0, maximum, start, end)
        trim_editor.setMinimumWidth(150)
        trim_editor.range_changed.connect(
            lambda in_frame, out_frame, nid=section.node_id:
            section.parameter_changed.emit(
                nid, "trim_range", (in_frame, out_frame)))
        trim_layout.addWidget(trim_editor, 1)
        layout.addWidget(trim_row)

        # Prevent duplicate source_in/source_out rows while keeping the
        # actual persisted parameters in the section state.
        section._skip_params.update({"source_in", "source_out"})
        section._param_widgets["trim_range"] = {
            "type": "trim_range",
            "widget": trim_editor,
            "maximum": maximum,
        }

        # Keep the normal parameter rows (file, speed, loop, etc.) below Trim.
        keys = [key for key in params.keys()
                if key not in section._skip_params]
        section._build_directives_into(layout, keys)

    def _maybe_add_scene3d_connection_hint(self, section: "NodeSectionWidget",
                                            layout: QVBoxLayout) -> None:
        """P2: 当 Camera3D/Light3D/Geo3D/Model3D 连接到 Scene3D 时显示提示。

        Scene3D 的 cam_* 参数是内置默认相机（fallback）——无 Camera3D 连接时生效，
        连了 Camera3D 则被 payload 覆盖。此提示让用户知道哪些 fallback 参数当前
        不生效，避免"改了 cam_fov 却没反应"的困惑。
        """
        if self._node_graph is None:
            return  # 无 graph（headless）→ 不显示
        try:
            conns = self._node_graph.get_input_connections(section.node_id)
        except Exception:
            return
        # v8：Scene3D 单一 `in` 端口。扫描上游节点，按 node_type 分类已连接的源类型。
        _TYPE_LABEL = {
            "camera3d": "Camera3D", "light3d": "Light3D",
            "geo3d": "Geo3D", "model3d": "Model3D",
        }
        connected_types = set()
        for c in conns:
            if c.dst_port_name != "in":
                continue
            upstream = self._node_graph.get_node(c.src_node_id)
            if upstream is not None and upstream.node_type in _TYPE_LABEL:
                connected_types.add(upstream.node_type)
        if not connected_types:
            return  # 无任何 scene 连接 → 全用全局默认，正常显示无需提示
        from PyQt6.QtWidgets import QLabel
        sources = sorted({_TYPE_LABEL[t] for t in connected_types})
        hint = QLabel("🔗 " + " / ".join(sources) + " 已连接："
                      "全局默认的对应设置被 payload 覆盖。"
                      "如需调整请编辑对应连接的节点。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "QLabel{background:#3a3220;color:#e8c97a;border-radius:4px;"
            "padding:6px 8px;font-size:11px;}")
        layout.addWidget(hint)

    def _build_constant_ui(self, section: "NodeSectionWidget",
                           layout: QVBoxLayout) -> None:
        """Build a clean, mode-aware UI for the Constant node."""
        from src.features.composite.nodes.constant_node import ConstantNode

        mode = section.parameters.get("mode", "solid")
        section._hidden_params = ConstantNode.mode_param_visibility(mode)

        params = section.parameters
        pdefs = section.param_defs
        popts = section.param_options

        # Constant's labels hug their widgets: each label sizes to its own text
        # width rather than sharing a uniform minimum width. The generic
        # _build_parameters path computes a shared _label_min_width so all rows
        # align; but Constant early-returns before that, and a compact layout
        # (name directly followed by its control, no vertical column alignment)
        # reads better for this node's short rows. Setting the minimum to 0 lets
        # each _ParamLabel shrink to fit via its Maximum size policy.
        self._label_min_width = 0

        # Mark all params as handled by custom builder; we'll explicitly add
        # the ones we want shown.
        section._skip_params = set(params.keys())

        # ── Size ──
        self._build_constant_size_row(section, params, layout)

        # Width / Height on a single row with a ⛓ aspect-ratio lock + per-axis
        # responsive sliders (mirrors the transform "scale" row layout). Using
        # the shared _create_xy_param_row_for_section keeps the slider show/hide
        # logic, label alignment, and chain styling consistent across the panel,
        # and registers the width/height widgets on section._param_widgets so the
        # size-preset combo can reflect changes into them.
        size_row = self._create_xy_param_row_for_section(
            node_id=section.node_id,
            base="size",
            x_key="width", y_key="height",
            x_val=int(params.get("width", 1920)),
            y_val=int(params.get("height", 1080)),
            param_options=popts, param_defs=pdefs,
            section=section,
            slider_holder=section._slidable_sliders)
        layout.addWidget(size_row)
        section._skip_params.discard("width")
        section._skip_params.discard("height")
        # When width/height change directly (drag or slider), re-resolve the
        # size combo so it shows "Custom" unless the result matches a preset.
        self._wire_constant_size_combo_sync(section)

        # ── Mode ──
        mode_row = self._create_param_row_for_section(
            section.node_id, "mode", params["mode"],
            popts, pdefs, section)
        layout.addWidget(mode_row)
        section._skip_params.discard("mode")

        # ── Colors ──
        # Primary color button
        color1 = tuple(params.get("color", (0.0, 0.0, 0.0, 1.0)))
        hex1 = ConstantNode.rgba_to_hex(*color1[:3])
        c1_btn = self._create_constant_color_button(
            section, hex1,
            r_key="color_r", g_key="color_g", b_key="color_b", a_key="color_a",
            hex_key="color_hex", label="Primary Color", color_key="color")
        layout.addWidget(c1_btn)

        # Secondary color button + gradient preview (mode-dependent)
        needs_color2 = mode in ("checker", "gradient", "radial")
        if needs_color2:
            color2 = tuple(params.get("color2", (1.0, 1.0, 1.0, 1.0)))
            hex2 = ConstantNode.rgba_to_hex(*color2[:3])

            # Swap-colors toggle — sits on the Secondary Color row, aligned to
            # the label slot so it lines up with the swatch column. Clicking it
            # swaps the primary/secondary RGBA and refreshes both swatches +
            # the gradient preview (see _swap_constant_colors).
            swap_row = QWidget()
            swap_h = QHBoxLayout(swap_row)
            swap_h.setContentsMargins(0, 2, 0, 2)
            swap_h.setSpacing(4)
            swap_label = self._make_param_label("")
            swap_h.addWidget(swap_label)
            swap_btn = QToolButton()
            swap_btn.setText("⇄")
            swap_btn.setToolTip(I18N.tr(
                "node_props.swap_colors", "Swap primary and secondary colors"))
            swap_btn.setFixedSize(28, 20)
            swap_btn.setStyleSheet(_CHAIN_STYLE)
            swap_btn.clicked.connect(
                lambda _=False, sec=section: self._swap_constant_colors(sec))
            swap_h.addWidget(swap_btn)
            swap_h.addStretch()
            layout.addWidget(swap_row)

            c2_btn = self._create_constant_color_button(
                section, hex2,
                r_key="color2_r", g_key="color2_g",
                b_key="color2_b", a_key="color2_a",
                hex_key="color2_hex", label="Secondary Color",
                color_key="color2")
            layout.addWidget(c2_btn)

            if mode == "gradient":
                # Gradient preview bar — placed below the Secondary Color row
                # so the two color swatches read as a pair above the preview.
                # Derive a preview direction from the free start/end params so
                # the preview tracks handle drags / preset clicks. The preview
                # widget only supports 4 canonical directions, so we pick the
                # closest one based on the dominant axis / corner.
                sx = float(params.get("start_x", 0.0))
                sy = float(params.get("start_y", 0.5))
                ex = float(params.get("end_x", 1.0))
                ey = float(params.get("end_y", 0.5))
                ddx, ddy = ex - sx, ey - sy
                if abs(ddx) < 1e-3 and abs(ddy) > 1e-3:
                    preview_dir = "vertical"
                elif abs(ddy) < 1e-3 and abs(ddx) > 1e-3:
                    preview_dir = "horizontal"
                elif ddx * ddy >= 0:
                    preview_dir = "diagonal_tl_br"
                else:
                    preview_dir = "diagonal_tr_bl"
                preview = _GradientPreviewWidget(hex1, hex2, preview_dir)
                layout.addWidget(preview)
                # Store reference so mode changes can update it
                section._param_widgets["_gradient_preview"] = {
                    'type': 'custom', 'widget': preview}

        # ── Mode-specific options ──
        if mode == "checker" and "checker_size" in params:
            row = self._create_constant_checker_row(section, popts, pdefs)
            layout.addWidget(row)
            section._skip_params.discard("checker_size")

        if mode == "gradient":
            # Gradient direction presets. The actual gradient is driven by the
            # free start/end params (editable via on-screen handles); these
            # buttons are shortcuts that snap start/end to canonical positions.
            # Each preset is (label, start_x, start_y, end_x, end_y).
            grad_presets = [
                ("↔", 0.0, 0.5, 1.0, 0.5),   # horizontal L→R
                ("↕", 0.5, 0.0, 0.5, 1.0),   # vertical T→B
                ("⤡", 0.0, 0.0, 1.0, 1.0),   # diagonal TL→BR
                ("⤢", 1.0, 0.0, 0.0, 1.0),   # diagonal TR→BL
            ]
            cur_sx = float(params.get("start_x", 0.0))
            cur_sy = float(params.get("start_y", 0.5))
            cur_ex = float(params.get("end_x", 1.0))
            cur_ey = float(params.get("end_y", 0.5))

            preset_row = QWidget()
            preset_h = QHBoxLayout(preset_row)
            preset_h.setContentsMargins(0, 2, 0, 2)
            preset_h.setSpacing(4)
            preset_label = self._make_param_label("Preset")
            preset_h.addWidget(preset_label)
            for icon, psx, psy, pex, pey in grad_presets:
                btn = QToolButton()
                btn.setText(icon)
                btn.setToolTip(f"start=({psx:.2f},{psy:.2f}) end=({pex:.2f},{pey:.2f})")
                btn.setFixedSize(28, 20)
                btn.setStyleSheet(_CHAIN_STYLE)
                # Highlight the active preset so the user can see which one
                # currently matches the start/end params.
                is_active = (abs(cur_sx - psx) < 1e-3 and abs(cur_sy - psy) < 1e-3
                             and abs(cur_ex - pex) < 1e-3 and abs(cur_ey - pey) < 1e-3)
                if is_active:
                    btn.setStyleSheet(_CHAIN_STYLE + "QToolButton{background:#3a6ea5;}")

                def _apply_preset(_checked=False, sx=psx, sy=psy, ex=pex, ey=pey, sec=section) -> None:
                    sec.parameters["start_x"] = sx
                    sec.parameters["start_y"] = sy
                    sec.parameters["end_x"] = ex
                    sec.parameters["end_y"] = ey
                    self._on_param_changed("start_x", sx, sec.node_id)
                    self._on_param_changed("start_y", sy, sec.node_id)
                    self._on_param_changed("end_x", ex, sec.node_id)
                    self._on_param_changed("end_y", ey, sec.node_id)
                    # Refresh the gradient preview if present.
                    preview_info = sec._param_widgets.get("_gradient_preview")
                    if preview_info and preview_info.get('type') == 'custom':
                        preview_info['widget'].update()

                btn.clicked.connect(_apply_preset)
                preset_h.addWidget(btn)
            preset_h.addStretch()
            layout.addWidget(preset_row)

        if mode == "radial" and "radial_radius" in params:
            row = self._create_param_row_for_section(
                section.node_id, "radial_radius", params["radial_radius"],
                popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(row)
            section._skip_params.discard("radial_radius")

        # Feather slider (gradient + radial). Placed after the mode-specific
        # radius/preset rows so the most-edited controls stay near the top.
        if mode in ("gradient", "radial") and "feather" in params:
            row = self._create_param_row_for_section(
                section.node_id, "feather", params["feather"],
                popts, pdefs, section,
                slider_holder=section._slidable_sliders)
            layout.addWidget(row)
            section._skip_params.discard("feather")

        # No per-source output_color_space row — the Constant generates linear
        # pixels; the Output node owns the display transform. (See PARAM_DEFS
        # note in constant_node.py.)

    # ── Size preset row ──

    def _build_constant_size_row(self, section: "NodeSectionWidget",
                                 params: Dict[str, Any],
                                 layout: QVBoxLayout) -> None:
        """Build the size preset combo row.

        Selecting a preset writes width/height through _on_param_changed (so the
        engine + section.parameters update) and then reflects the new values into
        the dimension drag boxes via _update_section_param_value. Without that
        second step the combo would change the rendered size but leave the
        Width/Height boxes showing the old value.
        """
        w = int(params.get("width", 1920))
        h = int(params.get("height", 1080))

        # Determine current preset
        current_preset = "Custom"
        for name, pw, ph in self._SIZE_PRESETS:
            if pw == w and ph == h:
                current_preset = name
                break

        row = QWidget()
        h_layout = QHBoxLayout(row)
        h_layout.setContentsMargins(0, 2, 0, 2)
        h_layout.setSpacing(4)

        label = self._make_param_label("Size")
        h_layout.addWidget(label)

        combo = ChevronComboBox()
        for name, pw, ph in self._SIZE_PRESETS:
            combo.addItem(f"{name}  ({pw}×{ph})", (pw, ph))
        combo.addItem("Custom", None)

        # Set current index
        idx = 0
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is None and current_preset == "Custom":
                idx = i
                break
            elif data and data[0] == w and data[1] == h:
                idx = i
                break
        combo.setCurrentIndex(idx)
        apply_combo_dropdown_style(combo)

        def _on_preset_changed(ci, n=section.node_id) -> None:
            data = combo.itemData(ci)
            if data is not None:
                pw, ph = data
                self._on_param_changed("width", pw, n)
                self._on_param_changed("height", ph, n)
                # Reflect the new values into the dimension drag boxes (the
                # width/height widgets are independent of the combo and don't
                # auto-update from an internally-emitted parameter_changed).
                pw_info = section._param_widgets.get("width")
                ph_info = section._param_widgets.get("height")
                if pw_info:
                    self._update_section_param_value("width", pw, section._param_widgets)
                if ph_info:
                    self._update_section_param_value("height", ph, section._param_widgets)
                # A preset sets a brand-new size, so the aspect lock must
                # capture THIS preset's ratio rather than the stale one. The
                # dimension rows store the live ratio on the chain button's
                # user-data via the _build_constant_dimension_rows closure; we
                # poke it directly through the section so the lock stays sane
                # across preset switches.
                self._recapture_constant_aspect(section)

        combo.currentIndexChanged.connect(_on_preset_changed)
        h_layout.addWidget(combo, 1)

        layout.addWidget(row)
        section._param_widgets["size_preset"] = {
            'type': 'combo', 'widget': combo}

    def _wire_constant_size_combo_sync(self, section: "NodeSectionWidget") -> None:
        """Re-resolve the size combo whenever width or height changes.

        Width/Height live on the shared _create_xy_param_row, whose internal
        drag/slider handlers emit parameter changes but don't know about the
        size combo. We hook the section's parameter_changed signal so that any
        direct edit of width/height re-resolves the combo (jumping to Custom
        unless the result matches a preset).
        """
        def _maybe_sync_combo(key) -> None:
            if key in ("width", "height"):
                self._refresh_constant_size_combo(section)

        # The section relays every _on_param_changed via parameter_changed.
        # Connect once; guard against double-connection on rebuilds.
        if not getattr(section, "_size_combo_sync_wired", False):
            section.parameter_changed.connect(
                lambda nid, key, value: _maybe_sync_combo(key))
            section._size_combo_sync_wired = True

    def _refresh_constant_size_combo(self, section: "NodeSectionWidget") -> None:
        """Re-resolve the size combo to match the current width/height.

        Called after the user drags Width or Height directly — the resulting
        size usually no longer matches any preset, so the combo should jump to
        ``Custom``. Silently no-ops if the combo isn't built yet.
        """
        info = section._param_widgets.get("size_preset")
        if not info or info.get('type') != 'combo':
            return
        combo = info['widget']
        w = int(section.parameters.get("width", 0))
        h = int(section.parameters.get("height", 0))
        target_idx = combo.count() - 1  # "Custom" is always last
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is not None and data[0] == w and data[1] == h:
                target_idx = i
                break
        if combo.currentIndex() != target_idx:
            combo.blockSignals(True)
            combo.setCurrentIndex(target_idx)
            combo.blockSignals(False)

    def _recapture_constant_aspect(self, section: "NodeSectionWidget") -> None:
        """Refresh the captured W:H aspect ratio from the live width/height.

        Called after a preset switch so that — if the aspect lock is engaged —
        subsequent width/height drags rescale to the NEW preset's ratio rather
        than the ratio that was live when the lock was first toggled on.
        Silently no-ops if the dimension rows haven't been built yet.
        """
        ratio_holder = getattr(section, "_constant_aspect_ratio", None)
        if ratio_holder is None:
            return
        cw = int(section.parameters.get("width", 0))
        ch = int(section.parameters.get("height", 0))
        if cw > 0:
            ratio_holder[0] = float(ch) / float(cw)

    def _swap_constant_colors(self, section: "NodeSectionWidget") -> None:
        """Swap the Constant node's primary and secondary colors.

        Reads the eight RGBA channels from the live parameters, writes them
        swapped back through _on_param_changed (so the engine + render update),
        then refreshes both swatches and the gradient preview so the panel
        reflects the new state immediately. Only meaningful in checker /
        gradient / radial modes (the only ones with a secondary color).
        """
        from src.features.composite.nodes.constant_node import ConstantNode

        primary = tuple(section.parameters.get(
            "color", (0.0, 0.0, 0.0, 1.0)))
        secondary = tuple(section.parameters.get(
            "color2", (1.0, 1.0, 1.0, 1.0)))

        # Batch-write so the live-preview / batch-update guard stays coherent
        # and a single re-render happens afterwards.
        self._constant_batch_update = True
        try:
            self._on_param_changed("color", secondary, section.node_id)
            self._on_param_changed("color2", primary, section.node_id)
        finally:
            self._constant_batch_update = False

        # Refresh both swatches via their stored update callables. These read
        # the new param values for the alpha channel and recompute the hex.
        for hex_key, ak in (("color_hex", "color_a"), ("color2_hex", "color2_a")):
            info = section._param_widgets.get(hex_key)
            if not info:
                continue
            color_key = "color" if hex_key == "color_hex" else "color2"
            new_hex = ConstantNode.rgba_to_hex(*tuple(
                section.parameters.get(color_key, (0.0, 0.0, 0.0, 1.0)))[:3])
            update_fn = info.get('update')
            if update_fn:
                update_fn(new_hex)

        # Refresh the gradient preview (gradient mode) so it reflects the swap.
        preview_info = section._param_widgets.get("_gradient_preview")
        if preview_info and preview_info.get('type') == 'custom':
            c1_hex = ConstantNode.rgba_to_hex(*tuple(
                section.parameters.get("color", (0.0, 0.0, 0.0, 1.0)))[:3])
            c2_hex = ConstantNode.rgba_to_hex(*tuple(
                section.parameters.get("color2", (1.0, 1.0, 1.0, 1.0)))[:3])
            preview_info['widget'].set_colors(c1_hex, c2_hex)

    # ── Checker size (UI 1-100 maps to render 4-400px) ──

    def _create_constant_checker_row(self, section: "NodeSectionWidget",
                                     popts: Dict, pdefs: Dict) -> QWidget:
        """Build the checker_size row with a 1-100 UI range mapped to render px.

        The stored ``checker_size`` parameter is the actual square size in
        pixels (4-400), but the DragValue + slider show a friendlier 1-100
        scale so the user doesn't have to drag into the thousands. Mapping is
        linear ``render = ui * 4`` (UI 1→4px, 50→200px, 100→400px).

        - DragValue: 1-100, the displayed/edited value.
        - Slider: 1-100, responsive (auto-hides on narrow panels).
        - On change: writes ``ui * 4`` to the ``checker_size`` param.
        - On build / external update: initializes from ``param // 4``.
        """
        # UI range constants
        UI_MIN, UI_MAX = 1, 100
        SCALE = 4  # render_px = ui * SCALE  →  4..400

        render_val = int(section.parameters.get("checker_size", 64))
        # Map stored render px → displayed UI value (inverse of ui*SCALE).
        ui_val = max(UI_MIN, min(UI_MAX, round(render_val / SCALE)))

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = self._make_param_label("Checker Size")
        h.addWidget(label)

        # Responsive slider (1-100), registered so the section toggles it.
        # Layout order matches the generic numeric row: [label] [slider] [drag]
        # (slider takes the variable space, drag is the fixed-width value box).
        slider = self._make_numeric_slider(
            True, UI_MIN, UI_MAX, 1, float(ui_val), default_value=None)
        slider.setVisible(False)
        h.addWidget(slider, 1)
        section._slidable_sliders.append(slider)

        drag = DragValue("", UI_MIN, UI_MAX, float(ui_val), decimals=0)
        drag.setSingleStep(1)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        h.addWidget(drag)

        # Wire double-click reset to the UI-scaled default (after drag exists).
        pdef = pdefs.get("checker_size")
        if pdef and pdef.default is not None:
            default_ui = max(UI_MIN, min(UI_MAX, round(int(pdef.default) / SCALE)))
            label.doubleClicked.connect(
                lambda _=False, dui=default_ui: _set_ui(dui))

        info: Dict[str, Any] = {
            # 'checker' is a custom int-with-mapping type: the DragValue/slider
            # show a 1-100 UI scale, while the stored param is render px (4-400).
            # _update_section_param_value has a dedicated branch that converts
            # incoming render-px back to the UI scale before touching widgets.
            'type': 'checker',
            'widget': drag,
            'min_val': UI_MIN, 'max_val': UI_MAX, 'step': 1, 'decimals': 0,
            'animatable': bool(pdef and pdef.is_animatable()),
            'slider': slider,
            'scale': SCALE,  # render_px = ui * scale
        }

        def _write_render(ui_value: int, n=section.node_id) -> None:
            """Push the mapped render px to the checker_size param."""
            render = max(2, int(round(ui_value * SCALE)))
            self._on_param_changed("checker_size", render, n)

        def _set_ui(ui_value: int) -> None:
            """Set the DragValue + slider to a UI value (blocked, no re-emit)."""
            u = max(UI_MIN, min(UI_MAX, int(ui_value)))
            drag.blockSignals(True)
            drag.setValue(float(u))
            drag.blockSignals(False)
            slider.blockSignals(True)
            slider.setValue(u)
            slider.blockSignals(False)

        def _on_drag_changed(v: float) -> None:
            if self._block_signals:
                return
            ui = int(v)
            slider.blockSignals(True)
            slider.setValue(ui)
            slider.blockSignals(False)
            _write_render(ui)

        def _on_slider_changed(v: int) -> None:
            if self._block_signals:
                return
            drag.blockSignals(True)
            drag.setValue(float(v))
            drag.blockSignals(False)
            _write_render(int(v))

        drag.valueChanged.connect(_on_drag_changed)
        slider.valueChanged.connect(_on_slider_changed)

        # Keep an existing checker_size track in sync when the user finishes
        # dragging. The UI value is mapped back to the stored render-pixel
        # value before writing the keyframe.
        if pdef and pdef.is_animatable():
            drag.interaction_ended.connect(
                lambda d=drag, pd=pdef: self._autokey_if_animated(
                    "checker_size", int(round(d.value() * SCALE)), pd))
            slider.sliderReleased.connect(
                lambda d=drag, pd=pdef: self._autokey_if_animated(
                    "checker_size", int(round(d.value() * SCALE)), pd))

            kf_btn = self._make_keyframe_button("checker_size", pdef)
            h.addWidget(kf_btn)
            info['keyframe_btn'] = kf_btn
            info['keyframe_btn_keys'] = ["checker_size"]

        section._param_widgets["checker_size"] = info
        return row

    # ── Color button ──

    def _create_constant_color_button(
        self, section: "NodeSectionWidget", hex_color: str,
        r_key: str, g_key: str, b_key: str, a_key: str,
        hex_key: str, label: str = "Color", color_key: Optional[str] = None
    ) -> QWidget:
        """Create a square color swatch + eyedropper button.

        - Square swatch (28x28) shows current color, no hex text.
        - Eyedropper button picks any color from the screen.
        - Clicking the swatch opens the project's CompactColorStudio picker
          (replaces the old QColorDialog) with live preview support.

        For Constant, ``color_key`` is the canonical atomic RGBA parameter.
        Other node color rows keep their existing channel-based behavior.
        """
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.shared.assets.icon_manager import icon_color_picker
        from src.shared.ui.widgets.color_picker_control import (
            linear_rgba_to_qcolor,
            qcolor_to_linear_rgba,
        )

        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        lbl = self._make_param_label(label)
        h.addWidget(lbl)

        # ── Square color swatch ──
        btn = QPushButton()
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(f"{label} — click to pick color")

        def _update_btn_style(hex_str: str, alpha: float = 1.0) -> None:
            """Update swatch appearance with current color (no hex text)."""
            qcolor = QColor(hex_str)
            r_i = qcolor.red()
            g_i = qcolor.green()
            b_i = qcolor.blue()
            a_i = max(0, min(255, int(alpha * 255.0)))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: rgba({r_i}, {g_i}, {b_i}, {a_i});
                    border: 1px solid #555;
                    border-radius: 3px;
                }}
                QPushButton:hover {{
                    border: 1px solid #888;
                }}
            """)

        initial_alpha = (tuple(section.parameters.get(color_key, (0.0, 0.0, 0.0, 1.0)))[3]
                         if color_key else section.parameters.get(a_key, 1.0))
        _update_btn_style(hex_color, float(initial_alpha))

        def _apply_color(color: QColor, n=section.node_id) -> None:
            """Write the chosen color back as one atomic RGBA value."""
            nonlocal hex_color
            hex_color = color.name()
            _update_btn_style(hex_color, color.alphaF())

            # QColor/Color Inspector values are sRGB display codes.  Constant
            # stores scene-linear channels, so convert exactly once at this
            # UI-to-graph boundary.
            r_linear, g_linear, b_linear, a = qcolor_to_linear_rgba(color)

            self._constant_batch_update = True
            try:
                if color_key:
                    self._on_param_changed(
                        color_key, (r_linear, g_linear, b_linear, a), n)
                else:
                    self._on_param_changed(r_key, r_linear, n)
                    self._on_param_changed(g_key, g_linear, n)
                    self._on_param_changed(b_key, b_linear, n)
                    self._on_param_changed(a_key, a, n)
                self._on_param_changed(hex_key, hex_color, n)
            finally:
                self._constant_batch_update = False

            # Update gradient preview if this color button feeds one
            preview_info = section._param_widgets.get("_gradient_preview")
            if preview_info and preview_info.get('type') == 'custom':
                c1_hex = ConstantNode.rgba_to_hex(*tuple(
                    self._current_parameters.get(
                        "color", (0.0, 0.0, 0.0, 1.0)))[:3])
                c2_hex = ConstantNode.rgba_to_hex(*tuple(
                    self._current_parameters.get(
                        "color2", (1.0, 1.0, 1.0, 1.0)))[:3])
                preview_info['widget'].set_colors(c1_hex, c2_hex)

        def _on_live(color: QColor) -> None:
            """Live preview callback (during drag in the picker)."""
            _apply_color(color)

        def _pick() -> None:
            # Open the project's CompactColorStudio picker (modal).
            from src.shared.color import get_color
            rgba = (tuple(section.parameters.get(
                color_key, (0.0, 0.0, 0.0, 1.0))) if color_key else (
                    float(section.parameters.get(r_key, 0.0)),
                    float(section.parameters.get(g_key, 0.0)),
                    float(section.parameters.get(b_key, 0.0)),
                    float(section.parameters.get(a_key, 1.0))))
            current = linear_rgba_to_qcolor(rgba)
            result = get_color(parent=self, initial=current, on_live=_on_live)
            if result is not None:
                _apply_color(result)

        btn.clicked.connect(_pick)
        h.addWidget(btn)

        # ── Eyedropper button (pick color from screen) ──
        picker_btn = QPushButton()
        picker_btn.setFixedSize(28, 28)
        picker_btn.setIcon(icon_color_picker())
        picker_btn.setIconSize(QSize(16, 16))
        picker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        picker_btn.setToolTip(
            I18N.tr("node_props.pick_color_from_screen", "Pick color from screen"))
        picker_btn.setStyleSheet("""
            QPushButton {
                background-color: #333; border: 1px solid #444; border-radius: 3px;
            }
            QPushButton:hover { background-color: #444; border: 1px solid #666; }
        """)

        def _start_eyedropper() -> None:
            from src.shared.color import EyeDropperOverlay
            # 必须把 dropper 存为实例属性，防止函数返回后被垃圾回收。
            # EyeDropperOverlay 是一个 QWidget，如果引用丢失会被立即销毁，
            # 导致 mousePressEvent 永远无法触发（吸管点了没反应）。
            self._active_dropper = EyeDropperOverlay()
            self._active_dropper.colorPicked.connect(_apply_color)
            self._active_dropper.start()

        picker_btn.clicked.connect(_start_eyedropper)
        h.addWidget(picker_btn)

        # The swatch is one user-facing RGBA property. Give it one stopwatch
        # so color animation is discoverable without exposing four separate
        # controls in the custom row.
        if color_key:
            kf_btn = self._make_keyframe_button(
                color_key, section.param_defs.get(color_key))
            keyframe_btn_keys = [color_key]
        else:
            color_keys = [r_key, g_key, b_key, a_key]
            color_pdefs = [section.param_defs.get(key) for key in color_keys]
            kf_btn = self._make_multi_keyframe_button(
                color_keys, color_pdefs, editor_key=r_key)
            keyframe_btn_keys = color_keys
        h.addWidget(kf_btn)

        h.addStretch()

        section._param_widgets[hex_key] = {
            'type': 'color', 'widget': btn,
            'update': lambda hc, ck=color_key, ak=a_key: _update_btn_style(
                hc, float(tuple(section.parameters.get(
                    ck, (0.0, 0.0, 0.0, 1.0)))[3]) if ck else
                float(section.parameters.get(ak, 1.0))),
            'keyframe_btn': kf_btn,
            'keyframe_btn_keys': keyframe_btn_keys,
        }

        return row
