"""
Sticky Note item for node graph UI.

A sticky note is a lightweight annotation element that does NOT participate
in rendering. It's similar to a backdrop but more focused on text annotations.
"""

from __future__ import annotations

from typing import Optional, Dict, Any, Callable, List, Tuple, Union
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QPen, QBrush, QColor, QPainter, QFont
from PyQt6.QtWidgets import (
    QGraphicsItem, QGraphicsRectItem, QGraphicsTextItem,
    QMenu, QColorDialog, QDialog, QVBoxLayout, QTextEdit, QPushButton,
    QHBoxLayout, QLabel, QSlider, QStyle, )
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import CONTEXT_MENU_STYLE, DIALOG_BASE_STYLE


def _numeric_pair(
    value: Union[None, List[Any], Tuple[Any, ...], Any]
) -> Optional[Tuple[float, float]]:
    """Return ``(x, y)`` for a 2-element numeric sequence, else ``None``.

    AUD-11003 helper: validates externally-loaded geometry payloads
    (``position`` / ``size``) — a missing key, ``None``, a string or a
    wrong-length sequence yields ``None`` so the caller can fall back to a
    default instead of crashing the load.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


class StickyNoteItem(QGraphicsRectItem):
    """A sticky note/annotation item for the node graph.

    Features:
    - Lightweight annotation (does not participate in rendering)
    - Single free-text block (Post-it style)
    - Editable opacity, font size and text color (Edit Sticky Note dialog)
    - 5 preset colors (yellow, blue, green, pink, orange)
    - Right-click menu for delete and color change
    - Full undo/redo integration (mirrors BackdropItem)
    - Saved to project file via serialization
    """

    # Preset colors (lighter, pastel-like tones)
    DEFAULT_COLORS = [
        "#FFE082",  # Yellow (default)
        "#81D4FA",  # Blue
        "#A5D6A7",  # Green
        "#F8BBD9",  # Pink
        "#FFCC80",  # Orange
    ]

    def __init__(
        self,
        note_id: str,
        text: str = "",
        color: str = "#FFE082",
        position: QPointF = None,
        size: QRectF = None,
        font_size: int = 12,
        opacity: float = 0.65,
        font_color: Optional[str] = None,
        z_order: int = -5,  # Above backdrops (-10), below nodes (0)
    ) -> None:
        super().__init__()

        self._note_id = note_id
        self._text = text
        self._color = QColor(color)
        # Background fill opacity (0.0–1.0). Baked into the fill brush so only
        # the fill is translucent — border and text stay fully readable.
        self._opacity = max(0.0, min(1.0, float(opacity)))
        self._font_size = font_size
        # Independent text color; defaults to white. Left untouched when the
        # background color changes (set via the Edit Sticky Note swatch).
        self._font_color = QColor(font_color) if font_color else QColor("#ffffff")
        self._z_order = z_order
        self._padding = 10.0
        self._corner_radius = 8.0

        # Change handlers
        self._changed_handler: Optional[Callable[[StickyNoteItem], None]] = None
        self._modified_handler: Optional[Callable[["StickyNoteItem", Dict[str, Any], Dict[str, Any]], None]] = None

        # Undo / programmatic-apply support
        self._change_old_data: Optional[Dict[str, Any]] = None  # pre-change snapshot for one undo step
        self._applying_state = False  # True while restoring state (undo/redo) — suppresses notify
        # AUD-11001: keep the non-modal color-picker dialog alive for as
        # long as it is open (a parentless, unreferenced dialog was
        # garbage-collected right after show(), flashing it away).
        self._color_dlg: Optional[QColorDialog] = None

        # Resize / move interaction state
        self._resizing = False
        self._resize_start_pos: Optional[QPointF] = None
        self._resize_start_rect: Optional[QRectF] = None
        self._move_start_position: Optional[QPointF] = None

        # Default size
        if size:
            self.setRect(size)
        else:
            self.setRect(0, 0, 200, 150)

        # Position
        if position:
            self.setPos(position)

        # Visual properties
        self.setZValue(z_order)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)

        # Appearance: fill (color + opacity) and opaque border
        self._border_color = QColor(self._color.darker(140))
        self._apply_fill()

        # Resize handle
        self._resize_handle_size = 12
        self._resize_handle = QGraphicsRectItem(self)
        self._resize_handle.setRect(
            self.rect().width() - self._resize_handle_size,
            self.rect().height() - self._resize_handle_size,
            self._resize_handle_size,
            self._resize_handle_size
        )
        self._resize_handle.setBrush(QBrush(QColor(200, 200, 200, 180)))
        self._resize_handle.setPen(QPen(Qt.GlobalColor.transparent))
        # Click-transparent: the parent detects handle hits and drives resize.
        self._resize_handle.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._resize_handle.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._resize_handle.setAcceptHoverEvents(True)

        # Initial resize handle update
        self._update_resize_handle()

        # Text item for display (single free-text block)
        self._text_item = QGraphicsTextItem(self)
        self._text_item.setPlainText(text)
        # No text interaction: clicks/double-clicks propagate to this item so
        # drag-to-move and double-click-to-edit both work.
        self._text_item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._apply_font()
        self._update_text_layout()

    # ── Fonts / fill / layout ──

    def _note_font(self) -> QFont:
        """Return font for note text."""
        font = QFont()
        font.setPixelSize(self._font_size)
        return font

    def _apply_fill(self) -> None:
        """Apply the current color + opacity to the fill brush.

        Only the fill is translucent; the border pen stays opaque so the note
        edge stays crisp. Applies alpha to the *base* color (not lighter()) —
        sticky notes use bright pastels, and lighter() would wash them white.
        """
        c = QColor(self._color)
        c.setAlphaF(self._opacity)
        self.setBrush(QBrush(c))
        self._border_color = QColor(self._color.darker(140))

    def _apply_font(self) -> None:
        """Apply the current font size + color to the text item."""
        if hasattr(self, '_text_item'):
            self._text_item.setFont(self._note_font())
            self._text_item.setDefaultTextColor(QColor(self._font_color))

    def _update_text_layout(self) -> None:
        """Wrap the text to the note width and inset it from the edges."""
        if not hasattr(self, '_text_item'):
            return
        pad = self._padding
        max_width = max(40.0, self.rect().width() - 2 * pad)
        self._text_item.setTextWidth(max_width)
        self._text_item.setPos(pad, pad)

    def setRect(self, *args) -> None:
        """Override setRect to update resize handle and text wrap on resize."""
        super().setRect(*args)
        if hasattr(self, '_resize_handle'):
            self._update_resize_handle()
        if hasattr(self, '_text_item'):
            self._update_text_layout()

    def _update_resize_handle(self) -> None:
        """Update resize handle position."""
        r = self.rect()
        if hasattr(self, '_resize_handle'):
            self._resize_handle.setRect(
                r.width() - self._resize_handle_size - 2,
                r.height() - self._resize_handle_size - 2,
                self._resize_handle_size,
                self._resize_handle_size
            )

    # ── Change handlers / undo plumbing (mirrors BackdropItem) ──

    def set_changed_handler(self, handler: Callable[[StickyNoteItem], None]) -> None:
        """Set callback for change notifications (move, resize, edit, delete)."""
        self._changed_handler = handler

    def set_modified_handler(
        self,
        handler: Optional[Callable[["StickyNoteItem", Dict[str, Any], Dict[str, Any]], None]],
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

    def apply_state(self, data: Dict[str, Any]) -> None:
        """Restore sticky note state from a dict (undo/redo). Does not notify."""
        was_applying = self._applying_state
        self._applying_state = True
        try:
            if 'position' in data:
                self.setPos(data['position'][0], data['position'][1])
            if 'size' in data:
                self.setRect(0, 0, data['size'][0], data['size'][1])
            if 'text' in data:
                self._text = data['text']
                self._text_item.setPlainText(self._text)
                self._update_text_layout()
            if 'color' in data:
                self._color = QColor(data['color'])
                self._apply_fill()
            if 'opacity' in data:
                self._opacity = max(0.0, min(1.0, float(data['opacity'])))
                self._apply_fill()
            font_changed = False
            if 'font_size' in data:
                self._font_size = int(data['font_size'])
                font_changed = True
            if 'font_color' in data:
                self._font_color = QColor(data['font_color'])
                font_changed = True
            if font_changed:
                self._apply_font()
            if 'z_order' in data:
                self._z_order = int(data['z_order'])
                self.setZValue(self._z_order)
        finally:
            self._applying_state = was_applying
        self.update()

    # ── Properties ──

    def note_id(self) -> str:
        """Get the sticky note ID."""
        return self._note_id

    def setText(self, text: str) -> None:
        """Set the note text."""
        self._begin_change()
        self._text = text
        self._text_item.setPlainText(text)
        self._update_text_layout()
        self._notify_changed()

    def text(self) -> str:
        """Get the note text."""
        return self._text

    def setColor(self, color: str) -> None:
        """Set the background color. Text color is independent and unchanged."""
        self._begin_change()
        self._color = QColor(color)
        self._apply_fill()
        self.update()
        self._notify_changed()

    def color(self) -> str:
        """Get the background color as hex string."""
        return self._color.name()

    def setBackgroundOpacity(self, opacity: float) -> None:
        """Set the background fill opacity (0.0–1.0). Border/text stay opaque."""
        opacity = max(0.0, min(1.0, float(opacity)))
        self._begin_change()
        self._opacity = opacity
        self._apply_fill()
        self.update()
        self._notify_changed()

    def backgroundOpacity(self) -> float:
        """Get the background fill opacity (0.0–1.0)."""
        return self._opacity

    def setFontColor(self, color) -> None:
        """Set the text color."""
        self._begin_change()
        self._font_color = QColor(color)
        self._apply_font()
        self.update()
        self._notify_changed()

    def fontColor(self) -> str:
        """Get the text color as hex string."""
        return self._font_color.name()

    def setFontSize(self, size: int) -> None:
        """Set the font size."""
        self._begin_change()
        self._font_size = size
        self._apply_font()
        self._update_text_layout()
        self._notify_changed()

    def fontSize(self) -> int:
        """Get the font size."""
        return self._font_size

    def setZOrder(self, z: int) -> None:
        """Set z-order (lower values are behind)."""
        self._begin_change()
        self._z_order = z
        self.setZValue(z)
        self._notify_changed()

    def zOrder(self) -> int:
        """Get z-order."""
        return self._z_order

    # ── Interaction ──

    def mouseDoubleClickEvent(self, event) -> None:
        """Handle double-click to edit text."""
        self._open_edit_dialog()

    def hoverMoveEvent(self, event) -> None:
        """Handle hover to show resize cursor on handle."""
        pos = event.pos()
        r = self.rect()
        handle_rect = QRectF(
            r.width() - self._resize_handle_size - 2,
            r.height() - self._resize_handle_size - 2,
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
                r.width() - self._resize_handle_size - 2,
                r.height() - self._resize_handle_size - 2,
                self._resize_handle_size,
                self._resize_handle_size
            )
            if handle_rect.contains(pos):
                self._resizing = True
                self._resize_start_pos = pos
                self._resize_start_rect = QRectF(r)
                self._begin_change()  # capture pre-resize state for undo
                event.accept()
                return
            # Track move start position
            self._move_start_position = self.pos()
            self._begin_change()  # capture pre-move state for undo
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Handle mouse move for resizing."""
        if self._resizing:
            pos = event.pos()
            delta = pos - self._resize_start_pos
            new_width = max(100, self._resize_start_rect.width() + delta.x())
            new_height = max(80, self._resize_start_rect.height() + delta.y())
            self.setRect(0, 0, new_width, new_height)
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """Handle mouse release for resizing and moving.

        Routes through ``_notify_changed`` (which self-discards no-ops) so a
        plain click-select never pushes an undo step.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resizing:
                self._resizing = False
                self._resize_start_pos = None
                self._resize_start_rect = None
                self._notify_changed()
                event.accept()
                return
            if self._move_start_position is not None:
                # Moved → commit; plain click → _notify_changed discards (old==new).
                self._notify_changed()
                self._move_start_position = None
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        """Handle item changes.

        Position changes are synced at release (via ``_notify_changed``), not
        live, which avoids firing ``graph_changed`` on every drag pixel.
        """
        if change == QGraphicsItem.GraphicsItemChange.ItemTransformHasChanged:
            # PyQt6 没有 ItemRectHasChanged，使用 ItemTransformHasChanged 替代
            self._update_resize_handle()
        return super().itemChange(change, value)

    def contextMenuEvent(self, event) -> None:
        """Show context menu for edit, color change and delete."""
        menu = QMenu()
        menu.setStyleSheet(CONTEXT_MENU_STYLE)

        action_edit = menu.addAction(I18N.tr("sticky_note.edit", "Edit Sticky Note"))
        action_color = menu.addAction(I18N.tr("sticky_note.change_color", "Change Color"))
        menu.addSeparator()

        # Add preset colors submenu
        color_menu = menu.addMenu("Preset Colors")
        color_names = ["Yellow", "Blue", "Green", "Pink", "Orange"]
        color_actions = []
        for i, color_hex in enumerate(self.DEFAULT_COLORS):
            action = color_menu.addAction(color_names[i])
            color_actions.append((action, color_hex))

        menu.addSeparator()
        action_delete = menu.addAction(I18N.tr("sticky_note.delete", "Delete Sticky Note"))
        action_to_front = menu.addAction(I18N.tr("sticky_note.bring_to_front", "Bring to Front"))
        action_to_back = menu.addAction(I18N.tr("sticky_note.send_to_back", "Send to Back"))

        action = menu.exec(event.scenePos())

        if action == action_edit:
            self._open_edit_dialog()
        elif action == action_color:
            self._choose_color()
        elif action == action_delete:
            # Signal delete via handler (the editor detects scene()==None).
            self.scene().removeItem(self)
            if self._changed_handler:
                self._changed_handler(self)  # Will be interpreted as delete
        elif action == action_to_front:
            self.setZOrder(0)
        elif action == action_to_back:
            self.setZOrder(-20)
        else:
            # Check preset color actions
            for act, color_hex in color_actions:
                if action == act:
                    self.setColor(color_hex)
                    break

    def _open_edit_dialog(self) -> None:
        """Open a dialog to edit the sticky note (text + opacity + font)."""
        dialog = QDialog()
        dialog.setWindowTitle(I18N.tr("sticky_note.edit", "Edit Sticky Note"))
        dialog.setMinimumSize(300, 240)
        dialog.setStyleSheet(DIALOG_BASE_STYLE)

        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel(I18N.tr("sticky_note.note_label", "Note:")))
        text_edit = QTextEdit()
        text_edit.setPlainText(self._text)
        text_edit.setFont(self._note_font())
        layout.addWidget(text_edit)

        # Opacity slider (background fill transparency, 0–100%)
        opacity_row = QHBoxLayout()
        opacity_lbl = QLabel(I18N.tr("sticky_note.opacity_label", "Opacity:"))
        opacity_lbl.setMinimumWidth(80)
        opacity_row.addWidget(opacity_lbl)
        opacity_slider = QSlider(Qt.Orientation.Horizontal)
        opacity_slider.setRange(0, 100)
        opacity_slider.setValue(int(round(self._opacity * 100)))
        opacity_value = QLabel(f"{opacity_slider.value()}%")
        opacity_value.setMinimumWidth(40)
        opacity_slider.valueChanged.connect(
            lambda v: opacity_value.setText(f"{v}%")
        )
        opacity_row.addWidget(opacity_slider, 1)
        opacity_row.addWidget(opacity_value)
        layout.addLayout(opacity_row)

        def _swatch(btn, color) -> None:
            """Show a color swatch on a button with a contrasting label."""
            txt = "#000" if color.lightness() >= 128 else "#fff"
            btn.setStyleSheet(
                f"background-color: {color.name()}; color: {txt};"
                f"min-width: 40px; padding: 5px; font-weight: bold;"
            )
            btn.setText("Aa")

        # ── Font size (label + slider + value + color swatch on one row) ──
        font_row = QHBoxLayout()
        font_lbl = QLabel(I18N.tr("sticky_note.font_size", "Font size:"))
        font_lbl.setMinimumWidth(80)
        font_row.addWidget(font_lbl)
        size_slider = QSlider(Qt.Orientation.Horizontal)
        size_slider.setRange(8, 48)
        size_slider.setValue(self._font_size)
        size_label = QLabel(str(self._font_size))
        size_label.setMinimumWidth(28)
        size_slider.valueChanged.connect(
            lambda v: size_label.setText(str(v))
        )
        font_row.addWidget(size_slider, 1)
        font_row.addWidget(size_label)
        color_btn = QPushButton()
        chosen_color = QColor(self._font_color)

        def _pick_color() -> None:
            nonlocal chosen_color
            c = QColorDialog.getColor(chosen_color, dialog, "Text Color")
            if c.isValid():
                chosen_color = c
                _swatch(color_btn, c)

        _swatch(color_btn, chosen_color)
        color_btn.clicked.connect(_pick_color)
        font_row.addWidget(color_btn)
        layout.addLayout(font_row)

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
            new_text = text_edit.toPlainText()
            new_opacity = opacity_slider.value() / 100.0
            new_size = size_slider.value()
            font_changed = (
                new_size != self._font_size
                or chosen_color.rgba() != self._font_color.rgba()
            )
            if (new_text != self._text
                    or abs(new_opacity - self._opacity) > 1e-6
                    or font_changed):
                # Apply all fields as a single undoable transaction
                self._begin_change()
                self._applying_state = True
                try:
                    self._text = new_text
                    self._text_item.setPlainText(new_text)
                    self._update_text_layout()
                    self._opacity = max(0.0, min(1.0, new_opacity))
                    self._apply_fill()
                    self._font_size = new_size
                    self._font_color = QColor(chosen_color)
                    self._apply_font()
                finally:
                    self._applying_state = False
                self._notify_changed()

    def _choose_color(self) -> None:
        """Open color picker dialog with real-time color preview.

        AUD-11001: the dialog is kept as an instance attribute so it
        survives after this method returns (a parentless, unreferenced
        non-modal dialog was garbage-collected immediately after ``show()``,
        making "Change Color" completely unusable).

        AUD-11002: the live preview applies intermediate colors WITHOUT
        pushing an undo record (the old path called ``setColor`` for every
        ``currentColorChanged``, i.e. one undo step per intermediate color).
        Accepting commits exactly one change record; cancelling rolls the
        preview back to the pre-dialog color.
        """
        if self._color_dlg is not None:
            # A picker is already open for this note — just surface it.
            self._color_dlg.raise_()
            return
        old_color = QColor(self._color)
        dlg = QColorDialog(old_color, None)
        dlg.setWindowTitle(I18N.tr("sticky_note.choose_color_title", "Choose Sticky Note Color"))
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        self._color_dlg = dlg  # AUD-11001: keep a reference alive.
        # Capture the pre-dialog state once so the whole drag (preview
        # included) commits as a single undo step.
        self._begin_change()

        def _on_live(color: QColor) -> None:
            if color.isValid():
                # Preview only — repaint, no undo/notification per tick.
                self._color = QColor(color)
                self._apply_fill()
                self.update()

        def _on_finished(result: int) -> None:
            try:
                if result == QColorDialog.DialogCode.Accepted:
                    final = dlg.currentColor()
                    if final.isValid():
                        self._color = QColor(final)
                        self._apply_fill()
                        self.update()
                    # One commit for the whole interaction (no-op if the
                    # color is unchanged — _notify_changed discards those).
                    self._notify_changed()
                else:
                    # Cancel / close → roll the preview back and drop the
                    # captured pre-dialog snapshot.
                    self._color = old_color
                    self._apply_fill()
                    self.update()
                    self._change_old_data = None
            finally:
                self._color_dlg = None

        dlg.currentColorChanged.connect(_on_live)
        dlg.finished.connect(_on_finished)
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
        """Custom paint for rounded rectangle with translucent fill."""
        # Disable default selection highlight
        option.state = option.state & ~QStyle.StateFlag.State_Selected

        r = self.rect()

        # Draw rounded rect background (fill carries the opacity via the brush)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self.brush())
        painter.setPen(QPen(self._border_color, 2))
        painter.drawRoundedRect(r, self._corner_radius, self._corner_radius)

        # Draw selection highlight if selected
        if self.isSelected():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 200, 100, 200), 3))
            painter.drawRoundedRect(r.adjusted(-2, -2, 2, 2), self._corner_radius + 2, self._corner_radius + 2)

    # ── Serialization ──

    def to_dict(self) -> Dict[str, Any]:
        """Serialize sticky note to dict."""
        return {
            "id": self._note_id,
            "type": "sticky_note",
            "text": self._text,
            "color": self.color(),
            "opacity": self._opacity,
            "font_color": self._font_color.name(),
            "position": [self.pos().x(), self.pos().y()],
            "size": [self.rect().width(), self.rect().height()],
            "font_size": self._font_size,
            "z_order": self._z_order,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "StickyNoteItem":
        """Create sticky note from dict.

        AUD-11003: the payload comes from external project files (old
        versions / hand-edited / corrupted). Missing or malformed
        ``id``/``position``/``size`` fall back to defaults instead of raising
        KeyError/TypeError and aborting the whole project load.
        """
        pos = _numeric_pair(data.get("position"))
        size = _numeric_pair(data.get("size"))
        return StickyNoteItem(
            note_id=str(data.get("id", "")),
            text=data.get("text", ""),
            color=data.get("color", "#FFE082"),
            position=QPointF(pos[0], pos[1]) if pos else None,
            size=QRectF(0, 0, size[0], size[1]) if size else None,
            font_size=data.get("font_size", 12),
            opacity=data.get("opacity", 0.65),
            font_color=data.get("font_color"),
            z_order=data.get("z_order", -5),
        )

    def __repr__(self) -> str:
        return f"StickyNoteItem(id={self._note_id}, text='{self._text[:20]}...')"
