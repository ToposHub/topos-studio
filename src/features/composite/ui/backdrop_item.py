"""
Backdrop item for node graph UI.

A backdrop is a UI element that groups related nodes visually.
It does NOT participate in rendering.
"""

from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any, Callable, Tuple
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPen, QBrush, QColor, QPainter, QFont
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsRectItem, QGraphicsTextItem,
    QColorDialog, QDialog, QVBoxLayout, QTextEdit, QPushButton,
    QHBoxLayout, QStyle, )
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import DIALOG_BASE_STYLE

logger = logging.getLogger(__name__)


def _coerce_float(value: Any, default: float, label: str) -> float:
    """AUD-9402：外部持久化数值安全转换；非法值回退默认并告警。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("backdrop 字段 %r 非数值（%r），回退默认 %s",
                       label, value, default)
        return default


def _coerce_int(value: Any, default: int, label: str) -> int:
    """AUD-9402：外部持久化整数安全转换；非法值回退默认并告警。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("backdrop 字段 %r 非整数（%r），回退默认 %s",
                       label, value, default)
        return default


def _coerce_str(value: Any, default: str) -> str:
    """AUD-9402：字符串字段类型守卫（QColor 等只接受 str/int）。"""
    if isinstance(value, str):
        return value
    return default


def _coerce_point(data: Dict[str, Any], key: str,
                  default: Tuple[float, float]) -> Tuple[float, float]:
    """AUD-9402：从持久化 dict 安全取 (x, y)；缺键/形状不符回退默认并告警。"""
    value = data.get(key)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            pass
    logger.warning("backdrop 字段 %r 缺失或格式非法（%r），回退默认 %s",
                   key, value, default)
    return default


class BackdropItem(QGraphicsRectItem):
    """A backdrop item for grouping nodes visually."""

    # Default z-order for backdrops
    DEFAULT_Z_ORDER = -10

    DEFAULT_COLORS = [
        "#2d4a3e",  # Dark green
        "#3d2d4a",  # Dark purple
        "#4a3d2d",  # Dark orange
        "#2d3d4a",  # Dark blue
        "#4a2d3d",  # Dark red
    ]

    def __init__(
        self,
        backdrop_id: str,
        title: str = "Backdrop",
        position: QPointF = None,
        size: QRectF = None,
        color: str = None,
        note: str = "",
        opacity: float = 0.65,
        title_font_size: int = 14,
        title_color: str = "#ffffff",
        note_font_size: int = 12,
        note_color: str = "#e1e1e1",
        z_order: int = -10,
    ) -> None:
        super().__init__()

        self.backdrop_id = backdrop_id
        self._title = title
        self._note = note
        self._color = QColor(color or self.DEFAULT_COLORS[0])
        # Background fill opacity (0.0–1.0). Baked into the brush so only the
        # fill is translucent — border, title and note stay fully readable.
        self._opacity = max(0.0, min(1.0, float(opacity)))
        # Per-text style (editable from the Edit Backdrop dialog)
        self._title_font_size = int(title_font_size)
        self._title_color = QColor(title_color)
        self._note_font_size = int(note_font_size)
        self._note_color = QColor(note_color)
        self._z_order = z_order
        self._changed_handler: Optional[Callable[[BackdropItem], None]] = None
        self._contained_node_ids: List[str] = []

        # Resize interaction state
        self._resizing = False
        self._resize_start_pos: Optional[QPointF] = None
        self._resize_start_rect: Optional[QRectF] = None
        self._resize_start_position: Optional[QPointF] = None

        # Move tracking (for undo)
        self._move_start_position: Optional[QPointF] = None

        # Node-follow-on-move: provider returns {node_id: NodeItem}.
        # Nodes fully contained at drag-start are translated with the backdrop.
        self._node_items_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._move_node_snapshot: Optional[Dict[str, QPointF]] = None
        self._pending_node_moves: Optional[List[Tuple[str, Tuple[float, float], Tuple[float, float]]]] = None

        # Undo / programmatic-apply support
        self._modified_handler: Optional[Callable[["BackdropItem", Dict[str, Any], Dict[str, Any]], None]] = None
        self._change_old_data: Optional[Dict[str, Any]] = None  # pre-change snapshot for one undo step
        self._applying_state = False  # True while restoring state (undo/redo) — suppresses notify + node-follow

        # AUD-9400：非模态取色对话框的引用保持（局部变量会被 Python GC
        # 销毁 C++ 对象，窗口闪现即失效）。
        self._color_dialog: Optional[QColorDialog] = None

        # Default size
        if size:
            self.setRect(size)
        else:
            self.setRect(0, 0, 300, 200)

        # Position
        if position:
            self.setPos(position)

        # Visual properties
        self.setZValue(z_order)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self._apply_fill()  # fill = color + opacity (border/text stay opaque)
        self.setPen(QPen(self._color.darker(150), 2))

        # Title text
        self._title_item = QGraphicsTextItem(self)
        self._title_item.setPlainText(title)
        self._title_item.setPos(10, 5)
        self._title_item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

        # Note text (below the title), word-wrapped to the backdrop width
        self._note_item = QGraphicsTextItem(self)

        # Apply font sizes/colors to both text items (also lays out the note)
        self._apply_fonts()

        # Resize handle
        self._resize_handle_size = 15
        self._resize_handle = QGraphicsRectItem(self)
        self._resize_handle.setRect(
            self.rect().width() - self._resize_handle_size,
            self.rect().height() - self._resize_handle_size,
            self._resize_handle_size,
            self._resize_handle_size
        )
        self._resize_handle.setBrush(QBrush(Qt.GlobalColor.white))
        self._resize_handle.setPen(QPen(Qt.GlobalColor.gray, 1))
        self._resize_handle.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self._resize_handle.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._resize_handle.setAcceptHoverEvents(True)

        # Initial resize handle update
        self._update_resize_handle()

    def _title_font(self) -> QFont:
        """Return font for title."""
        font = QFont()
        font.setPixelSize(14)
        font.setBold(True)
        return font

    def _note_font(self) -> QFont:
        """Return font for the note body."""
        font = QFont()
        font.setPixelSize(12)
        return font

    def _update_note_item(self) -> None:
        """Lay out the note text below the title, wrapped to the backdrop width."""
        if not hasattr(self, '_note_item'):
            return
        text = self._note.strip() if self._note else ""
        self._note_item.setVisible(bool(text))
        if not text:
            return
        max_width = max(40.0, self.rect().width() - 20.0)
        self._note_item.setTextWidth(max_width)
        self._note_item.setPlainText(text)
        # Place the note directly below the title so a large title font can
        # never overlap it. The title sits at y=5; add its rendered height
        # plus a small gap instead of the old hard-coded y=28.
        title_item = getattr(self, '_title_item', None)
        title_h = title_item.boundingRect().height() if title_item else 14.0
        self._note_item.setPos(10.0, 5.0 + title_h + 6.0)

    def setRect(self, *args) -> None:
        """Override setRect to update resize handle and note wrap on resize."""
        super().setRect(*args)
        if hasattr(self, '_resize_handle'):
            self._update_resize_handle()
        if hasattr(self, '_note_item'):
            self._update_note_item()

    def _update_resize_handle(self) -> None:
        """Update resize handle position."""
        r = self.rect()
        self._resize_handle.setRect(
            r.width() - self._resize_handle_size,
            r.height() - self._resize_handle_size,
            self._resize_handle_size,
            self._resize_handle_size
        )

    def _apply_fill(self) -> None:
        """Apply the current color + opacity to the fill brush.

        Only the fill is translucent; the border pen stays opaque so the
        backdrop edge stays crisp. Using the brush alpha (not item-level
        setOpacity) keeps the title and note fully readable.
        """
        c = QColor(self._color.lighter(120))
        c.setAlphaF(self._opacity)
        self.setBrush(QBrush(c))

    def _apply_fonts(self) -> None:
        """Apply the current font sizes/colors to the title and note items."""
        if hasattr(self, '_title_item'):
            font = self._title_font()
            font.setPixelSize(self._title_font_size)
            self._title_item.setFont(font)
            self._title_item.setDefaultTextColor(QColor(self._title_color))
        if hasattr(self, '_note_item'):
            font = self._note_font()
            font.setPixelSize(self._note_font_size)
            self._note_item.setFont(font)
            self._note_item.setDefaultTextColor(QColor(self._note_color))
            self._update_note_item()

    def set_changed_handler(self, handler: Callable[[BackdropItem], None]) -> None:
        """Set callback for change notifications (move, resize, edit)."""
        self._changed_handler = handler

    def set_node_items_provider(self, provider: Optional[Callable[[], Dict[str, Any]]]) -> None:
        """Set a callable returning the current {node_id: NodeItem} map.

        Used so contained nodes can follow the backdrop while it is dragged.
        """
        self._node_items_provider = provider

    def set_modified_handler(
        self,
        handler: Optional[Callable[["BackdropItem", Dict[str, Any], Dict[str, Any]], None]],
    ) -> None:
        """Set callback fired once per logical edit with (item, old_data, new_data).

        The editor uses this to push a single undo step for move/resize/edit/
        color/z-order changes. Not fired while state is being restored
        programmatically (undo/redo).
        """
        self._modified_handler = handler

    def _begin_change(self) -> None:
        """Capture pre-change state once for the current edit transaction."""
        if self._change_old_data is None:
            self._change_old_data = self.to_dict()

    def _notify_changed(self) -> None:
        """Sync to the graph and emit one modified event (no-op while applying)."""
        if self._applying_state:
            return
        old_data = self._change_old_data
        self._change_old_data = None
        new_data = self.to_dict()
        # No-op (e.g. click without dragging): persist nothing, emit nothing.
        if old_data is not None and old_data == new_data:
            return
        if self._changed_handler:
            self._changed_handler(self)
        if self._modified_handler is not None and old_data is not None:
            self._modified_handler(self, old_data, new_data)

    def take_node_moves(self):
        """Return and clear node moves recorded during the last backdrop drag."""
        moves = self._pending_node_moves
        self._pending_node_moves = None
        return moves

    def apply_state(self, data: Dict[str, Any]) -> None:
        """Restore backdrop state from a dict (undo/redo). Does not notify.

        AUD-9402：对（可能来自外部持久化的）数据逐字段做存在性与类型校验，
        缺键/类型错回退当前值并告警，不再让 KeyError/TypeError 中断整个
        恢复流程（项目恢复调用链对此无兜底）。
        """
        was_applying = self._applying_state
        self._applying_state = True
        try:
            if 'position' in data:
                pos = _coerce_point(data, 'position', (self.x(), self.y()))
                self.setPos(pos[0], pos[1])
            if 'size' in data:
                size = _coerce_point(data, 'size',
                                     (self.rect().width(), self.rect().height()))
                self.setRect(0, 0, max(1.0, size[0]), max(1.0, size[1]))
            if 'title' in data:
                self._title = _coerce_str(data['title'], self._title)
                self._title_item.setPlainText(self._title)
            if 'note' in data:
                self._note = _coerce_str(data['note'], self._note)
                self._update_note_item()
            if 'color' in data:
                value = data['color']
                if isinstance(value, str):
                    self._color = QColor(value)
                    self._apply_fill()
                    self.setPen(QPen(self._color.darker(150), 2))
                else:
                    logger.warning("backdrop 字段 'color' 非字符串（%r），保留当前颜色",
                                   value)
            if 'opacity' in data:
                self._opacity = max(0.0, min(1.0, _coerce_float(
                    data['opacity'], self._opacity, 'opacity')))
                self._apply_fill()
            font_changed = False
            if 'title_font_size' in data:
                self._title_font_size = _coerce_int(
                    data['title_font_size'], self._title_font_size,
                    'title_font_size')
                font_changed = True
            if 'title_color' in data:
                self._title_color = QColor(_coerce_str(
                    data['title_color'], self._title_color.name()))
                font_changed = True
            if 'note_font_size' in data:
                self._note_font_size = _coerce_int(
                    data['note_font_size'], self._note_font_size,
                    'note_font_size')
                font_changed = True
            if 'note_color' in data:
                self._note_color = QColor(_coerce_str(
                    data['note_color'], self._note_color.name()))
                font_changed = True
            if font_changed:
                self._apply_fonts()
            if 'z_order' in data:
                self._z_order = _coerce_int(
                    data['z_order'], self._z_order, 'z_order')
                self.setZValue(self._z_order)
        finally:
            self._applying_state = was_applying
        self.update()

    def _snapshot_contained_nodes(self) -> None:
        """Snapshot current positions of contained nodes at drag-start."""
        self._move_node_snapshot = None
        provider = self._node_items_provider() if self._node_items_provider else None
        if not provider:
            return
        snap = {}
        for nid in self._contained_node_ids:
            node = provider.get(nid)
            if node is not None:
                snap[nid] = QPointF(node.pos())
        self._move_node_snapshot = snap

    def _finalize_node_moves(self) -> None:
        """Record (node_id, old_pos, new_pos) for nodes that followed the drag."""
        self._pending_node_moves = None
        if self._move_node_snapshot is None:
            return
        provider = self._node_items_provider() if self._node_items_provider else None
        if not provider:
            return
        moves = []
        for nid, start in self._move_node_snapshot.items():
            node = provider.get(nid)
            if node is not None:
                moves.append((nid, (start.x(), start.y()), (node.x(), node.y())))
        self._pending_node_moves = moves

    # ── Properties ──

    def setTitle(self, title: str) -> None:
        """Set the backdrop title."""
        self._begin_change()
        self._title = title
        self._title_item.setPlainText(title)
        self._notify_changed()

    def title(self) -> str:
        """Get the backdrop title."""
        return self._title

    def setNote(self, note: str) -> None:
        """Set the backdrop note."""
        self._begin_change()
        self._note = note
        self._update_note_item()
        self._notify_changed()

    def note(self) -> str:
        """Get the backdrop note."""
        return self._note

    def setColor(self, color: str) -> None:
        """Set the backdrop color."""
        self._begin_change()
        self._color = QColor(color)
        self._apply_fill()
        self.setPen(QPen(self._color.darker(150), 2))
        self._update_note_item()
        self.update()
        self._notify_changed()

    def color(self) -> str:
        """Get the backdrop color as hex string."""
        return self._color.name()

    def setBackgroundOpacity(self, opacity: float) -> None:
        """Set the background fill opacity (0.0–1.0). Border/text stay opaque."""
        opacity = max(0.0, min(1.0, float(opacity)))
        if abs(opacity - self._opacity) < 1e-6:
            return
        self._begin_change()
        self._opacity = opacity
        self._apply_fill()
        self.update()
        self._notify_changed()

    def backgroundOpacity(self) -> float:
        """Get the background fill opacity (0.0–1.0)."""
        return self._opacity

    def setZOrder(self, z: int) -> None:
        """Set z-order (lower values are behind)."""
        self._begin_change()
        self._z_order = z
        self.setZValue(z)
        self._notify_changed()

    def zOrder(self) -> int:
        """Get z-order."""
        return self._z_order

    def contained_node_ids(self) -> List[str]:
        """Get contained node IDs."""
        return self._contained_node_ids

    def send_to_back(self, z_value: float) -> None:
        """Send backdrop to back with specified z-value."""
        self._begin_change()
        self._z_order = int(z_value)
        self.setZValue(z_value)
        self._notify_changed()

    # ── Interaction ──

    def hoverMoveEvent(self, event) -> None:
        """Handle hover to show resize cursor on handle."""
        pos = event.pos()
        r = self.rect()
        handle_rect = QRectF(
            r.width() - self._resize_handle_size,
            r.height() - self._resize_handle_size,
            self._resize_handle_size,
            self._resize_handle_size
        )
        if handle_rect.contains(pos):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        """Reset cursor on hover leave."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:
        """Handle mouse press for resize detection and move tracking."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            r = self.rect()
            handle_rect = QRectF(
                r.width() - self._resize_handle_size,
                r.height() - self._resize_handle_size,
                self._resize_handle_size,
                self._resize_handle_size
            )
            if handle_rect.contains(pos):
                self._resizing = True
                self._resize_start_pos = pos
                self._resize_start_rect = QRectF(r)
                self._resize_start_position = self.pos()
                self._begin_change()  # capture pre-resize state for undo
                event.accept()
                return
            # Track move start position
            self._move_start_position = self.pos()
            self._begin_change()  # capture pre-move state for undo
            # Snapshot contained nodes so they translate with the backdrop
            self._snapshot_contained_nodes()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Handle mouse move for resizing."""
        if self._resizing:
            pos = event.pos()
            delta = pos - self._resize_start_pos
            new_width = max(100, self._resize_start_rect.width() + delta.x())
            new_height = max(80, self._resize_start_rect.height() + delta.y())
            self.setRect(0, 0, new_width, new_height)
            self._update_resize_handle()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Handle mouse release for resizing and moving."""
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resizing:
                self._resizing = False
                self._resize_start_pos = None
                self._resize_start_rect = None
                self._resize_start_position = None
                # Commit resize: sync graph + one undo step
                self._notify_changed()
                event.accept()
                return
            # Check if moved
            if self._move_start_position is not None:
                current_pos = self.pos()
                moved = current_pos != self._move_start_position
                if moved:
                    # Record how far each contained node moved, for undo
                    self._finalize_node_moves()
                    self._notify_changed()
                else:
                    # Nothing changed — discard the pre-change snapshot
                    self._change_old_data = None
                self._move_start_position = None
                self._move_node_snapshot = None
        super().mouseReleaseEvent(event)

    def update_contained_nodes(self, all_node_items: Dict[str, Any]) -> bool:
        """Update contained node IDs from the current node items.
        
        Args:
            all_node_items: Dict mapping node_id -> NodeItem
            
        Returns:
            True if contained nodes changed
        """
        old_ids = set(self._contained_node_ids)
        new_ids = set(self._get_contained_node_ids(all_node_items))
        
        self._contained_node_ids = list(new_ids)
        return old_ids != new_ids

    def _get_contained_node_ids(self, all_node_items: Dict[str, Any]) -> List[str]:
        """Return node IDs that are fully contained within this backdrop."""
        contained = []
        backdrop_rect = self.sceneBoundingRect()

        for node_id, item in all_node_items.items():
            if hasattr(item, 'sceneBoundingRect'):
                item_rect = item.sceneBoundingRect()
                # Check if node is fully within backdrop
                if backdrop_rect.contains(item_rect):
                    contained.append(node_id)

        return contained

    def itemChange(self, change, value):
        """Handle item changes."""
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # Translate contained nodes with the backdrop during a user drag.
            # Skipped while restoring state (undo/redo) — node positions are
            # restored explicitly by the move-nodes undo action in that case.
            if (not self._applying_state
                    and self._move_node_snapshot is not None
                    and self._move_start_position is not None
                    and self._node_items_provider):
                provider = self._node_items_provider()
                dx = value.x() - self._move_start_position.x()
                dy = value.y() - self._move_start_position.y()
                for nid, start in self._move_node_snapshot.items():
                    node = provider.get(nid) if provider else None
                    if node is not None:
                        node.setPos(start.x() + dx, start.y() + dy)
        elif change == QGraphicsItem.GraphicsItemChange.ItemTransformHasChanged:
            # PyQt6 没有 ItemRectHasChanged，使用 ItemTransformHasChanged 替代
            self._update_resize_handle()
            # Don't emit during continuous resize - handled in mouseReleaseEvent
            pass
        return super().itemChange(change, value)

    def _open_edit_dialog(self) -> None:
        """Open a dialog to edit backdrop title and note."""
        dialog = QDialog()
        dialog.setWindowTitle(I18N.tr("backdrop.edit_title", "Edit Backdrop"))
        dialog.setMinimumSize(300, 200)
        dialog.setStyleSheet(DIALOG_BASE_STYLE)

        layout = QVBoxLayout(dialog)

        from PyQt6.QtWidgets import QLabel, QLineEdit
        layout.addWidget(QLabel(I18N.tr("backdrop.title_label", "Title:")))
        title_edit = QLineEdit()
        title_edit.setText(self._title)
        layout.addWidget(title_edit)

        layout.addWidget(QLabel(I18N.tr("backdrop.note_label", "Note:")))
        note_edit = QTextEdit()
        note_edit.setPlainText(self._note)
        layout.addWidget(note_edit)

        # Opacity slider (background fill transparency, 0–100%)
        from PyQt6.QtWidgets import QSlider
        opacity_row = QHBoxLayout()
        opacity_lbl = QLabel(I18N.tr("backdrop.opacity_label", "Opacity:"))
        opacity_lbl.setMinimumWidth(80)
        opacity_row.addWidget(opacity_lbl)
        opacity_slider = QSlider(Qt.Orientation.Horizontal)
        opacity_slider.setRange(0, 100)
        opacity_slider.setValue(int(round(self._opacity * 100)))
        opacity_label = QLabel(f"{opacity_slider.value()}%")
        opacity_label.setMinimumWidth(40)
        opacity_slider.valueChanged.connect(
            lambda v: opacity_label.setText(f"{v}%")
        )
        opacity_row.addWidget(opacity_slider, 1)
        opacity_row.addWidget(opacity_label)
        layout.addLayout(opacity_row)

        def _swatch(btn, color) -> None:
            """Show a color swatch on a button with a contrasting label."""
            txt = "#000" if color.lightness() >= 128 else "#fff"
            btn.setStyleSheet(
                f"background-color: {color.name()}; color: {txt};"
                f"min-width: 40px; padding: 5px; font-weight: bold;"
            )
            btn.setText("Aa")

        # ── Title font (label + slider + value + color on one row, like Opacity) ──
        title_font_row = QHBoxLayout()
        title_font_lbl = QLabel(I18N.tr("backdrop.title_font", "Title font:"))
        title_font_lbl.setMinimumWidth(80)
        title_font_row.addWidget(title_font_lbl)
        title_size_slider = QSlider(Qt.Orientation.Horizontal)
        title_size_slider.setRange(8, 48)
        title_size_slider.setValue(self._title_font_size)
        title_size_label = QLabel(str(self._title_font_size))
        title_size_label.setMinimumWidth(28)
        title_size_slider.valueChanged.connect(
            lambda v: title_size_label.setText(str(v))
        )
        title_font_row.addWidget(title_size_slider, 1)
        title_font_row.addWidget(title_size_label)
        title_color_btn = QPushButton()
        chosen_title_color = QColor(self._title_color)

        def _pick_title_color() -> None:
            nonlocal chosen_title_color
            c = QColorDialog.getColor(chosen_title_color, dialog, "Title Text Color")
            if c.isValid():
                chosen_title_color = c
                _swatch(title_color_btn, c)

        _swatch(title_color_btn, chosen_title_color)
        title_color_btn.clicked.connect(_pick_title_color)
        title_font_row.addWidget(title_color_btn)
        layout.addLayout(title_font_row)

        # ── Note font (label + slider + value + color on one row, like Opacity) ──
        note_font_row = QHBoxLayout()
        note_font_lbl = QLabel(I18N.tr("backdrop.note_font", "Note font:"))
        note_font_lbl.setMinimumWidth(80)
        note_font_row.addWidget(note_font_lbl)
        note_size_slider = QSlider(Qt.Orientation.Horizontal)
        note_size_slider.setRange(8, 48)
        note_size_slider.setValue(self._note_font_size)
        note_size_label = QLabel(str(self._note_font_size))
        note_size_label.setMinimumWidth(28)
        note_size_slider.valueChanged.connect(
            lambda v: note_size_label.setText(str(v))
        )
        note_font_row.addWidget(note_size_slider, 1)
        note_font_row.addWidget(note_size_label)
        note_color_btn = QPushButton()
        chosen_note_color = QColor(self._note_color)

        def _pick_note_color() -> None:
            nonlocal chosen_note_color
            c = QColorDialog.getColor(chosen_note_color, dialog, "Note Text Color")
            if c.isValid():
                chosen_note_color = c
                _swatch(note_color_btn, c)

        _swatch(note_color_btn, chosen_note_color)
        note_color_btn.clicked.connect(_pick_note_color)
        note_font_row.addWidget(note_color_btn)
        layout.addLayout(note_font_row)

        # Button row
        button_layout = QHBoxLayout()
        ok_btn = QPushButton(I18N.tr("common.ok", "OK"))
        cancel_btn = QPushButton(I18N.tr("common.cancel", "Cancel"))
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_title = title_edit.text()
            new_note = note_edit.toPlainText()
            new_opacity = opacity_slider.value() / 100.0
            new_title_size = title_size_slider.value()
            new_note_size = note_size_slider.value()
            font_changed = (
                new_title_size != self._title_font_size
                or new_note_size != self._note_font_size
                or chosen_title_color.rgba() != self._title_color.rgba()
                or chosen_note_color.rgba() != self._note_color.rgba()
            )
            if (new_title != self._title
                    or new_note != self._note
                    or abs(new_opacity - self._opacity) > 1e-6
                    or font_changed):
                # Apply all fields as a single undoable transaction
                self._begin_change()
                self._applying_state = True
                try:
                    self._title = new_title
                    self._title_item.setPlainText(new_title)
                    self._note = new_note
                    self._update_note_item()
                    self._opacity = max(0.0, min(1.0, new_opacity))
                    self._apply_fill()
                    self._title_font_size = new_title_size
                    self._title_color = QColor(chosen_title_color)
                    self._note_font_size = new_note_size
                    self._note_color = QColor(chosen_note_color)
                    self._apply_fonts()
                finally:
                    self._applying_state = False
                self._notify_changed()

    def _choose_color(self) -> None:
        """Open color picker dialog with real-time color preview.

        AUD-9400：非模态对话框必须存为实例属性——局部变量在方法返回后归零，
        PyQt6 随即删除 C++ 对象（窗口闪现即被销毁）；关闭时释放引用。

        AUD-9401：实时预览不再逐 tick 提交 undo 组——拖动色轮时每个
        currentColorChanged 都会走 setColor → _notify_changed（完整 undo 组
        + containment 重算 + graph_changed 全图同步），一次拖动产生几十个
        undo 步。改为：预览期间只刷新画笔/填充（不发通知），对话框 accepted
        时按「预览前颜色 → 最终颜色」提交一次 undo（与 _open_edit_dialog 的
        事务模式一致）；rejected/关闭则回滚到预览前颜色且不提交。
        """
        if (self._color_dialog is not None
                and self._color_dialog.isVisible()):
            self._color_dialog.raise_()
            self._color_dialog.activateWindow()
            return

        original_color = QColor(self._color)
        self._begin_change()  # capture pre-change state once for undo

        dlg = QColorDialog(original_color, None)
        dlg.setWindowTitle(I18N.tr("backdrop.choose_color_title", "Choose Backdrop Color"))
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)

        def _apply_color(color: QColor) -> None:
            # Live visual refresh only — no notifications, no undo.
            self._color = QColor(color)
            self._apply_fill()
            self.setPen(QPen(self._color.darker(150), 2))
            self.update()

        def _on_live(color: QColor) -> None:
            if color.isValid():
                _apply_color(color)

        def _release_dialog() -> None:
            if self._color_dialog is dlg:
                self._color_dialog = None

        def _on_accepted() -> None:
            # Final color already applied live — commit one undo step.
            _release_dialog()
            self._notify_changed()

        def _on_rejected() -> None:
            _release_dialog()
            # Roll back to the pre-dialog color, no undo record.
            self._applying_state = True
            try:
                _apply_color(original_color)
            finally:
                self._applying_state = False
            self._change_old_data = None  # discard the pre-change snapshot

        dlg.currentColorChanged.connect(_on_live)
        dlg.accepted.connect(_on_accepted)
        dlg.rejected.connect(_on_rejected)
        self._color_dialog = dlg
        dlg.show()

    # ── Painting ──

    def boundingRect(self) -> QRectF:
        """Bounding rect covering the border + selection halo.

        ``paint()`` draws outside ``self.rect()``: the 2px border pen (≈1px out)
        and, when selected, a halo at ``r.adjusted(-2, -2, 2, 2)`` with a 3px pen
        (≈3.5px out), plus antialiasing. The inherited ``boundingRect`` reports
        only ``rect()``, so those outer pixels are never marked dirty on
        move/resize and leave trailing ghosts. Include a safe margin so Qt
        invalidates the full painted extent.
        """
        r = self.rect()
        margin = 5.0
        return r.adjusted(-margin, -margin, margin, margin)

    def paint(self, painter, option, widget) -> None:
        """Custom paint for backdrop."""
        # Disable default selection highlight
        option.state = option.state & ~QStyle.StateFlag.State_Selected

        r = self.rect()

        # Draw background
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        painter.drawRect(r)

        # Draw selection highlight if selected
        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 200, 100, 200), 3))
            painter.drawRect(r.adjusted(-2, -2, 2, 2))

    # ── Serialization ──

    def to_dict(self) -> Dict[str, Any]:
        """Serialize backdrop to dict."""
        return {
            'id': self.backdrop_id,
            'title': self._title,
            'note': self._note,
            'color': self.color(),
            'opacity': self._opacity,
            'title_font_size': self._title_font_size,
            'title_color': self._title_color.name(),
            'note_font_size': self._note_font_size,
            'note_color': self._note_color.name(),
            'position': [self.x(), self.y()],
            'size': [self.rect().width(), self.rect().height()],
            'z_order': self._z_order,
            'contained_node_ids': self._contained_node_ids,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'BackdropItem':
        """Create backdrop from dict.

        AUD-9402：外部持久化数据（旧版本工程/手改 JSON/第三方生成）缺键或
        类型错时不再抛 KeyError/TypeError 中断项目恢复——必填键缺失/畸形
        回退默认值并告警。
        """
        position = _coerce_point(data, 'position', (0.0, 0.0))
        size = _coerce_point(data, 'size', (300.0, 200.0))
        color = data.get('color')
        return BackdropItem(
            backdrop_id=str(data.get('id', '')),
            title=_coerce_str(data.get('title', 'Backdrop'), 'Backdrop'),
            position=QPointF(position[0], position[1]),
            size=QRectF(0, 0, max(1.0, size[0]), max(1.0, size[1])),
            color=color if isinstance(color, str) else None,
            note=_coerce_str(data.get('note', ''), ''),
            opacity=_coerce_float(data.get('opacity', 0.65), 0.65, 'opacity'),
            title_font_size=_coerce_int(
                data.get('title_font_size', 14), 14, 'title_font_size'),
            title_color=_coerce_str(
                data.get('title_color', '#ffffff'), '#ffffff'),
            note_font_size=_coerce_int(
                data.get('note_font_size', 12), 12, 'note_font_size'),
            note_color=_coerce_str(
                data.get('note_color', '#e1e1e1'), '#e1e1e1'),
            z_order=_coerce_int(data.get('z_order', -10), -10, 'z_order'),
        )

    def __repr__(self) -> str:
        return f"BackdropItem(id={self.backdrop_id}, title='{self._title}')"