"""The effect inserts for the clip currently open in the audio editor."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QMimeData, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QDrag, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QHideEvent,
    QIcon, QKeyEvent, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES, get_effect_definition
from src.shared.assets.icon_manager import icon_delete, icon_down, icon_up
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import glyph_icon

from .effects_panel import audio_effect_groups, audio_effect_name

if TYPE_CHECKING:
    from src.features.edit.core.timeline_v2 import ClipInstance


_RACK_STYLE = """
    QPushButton#ClipEffectAdd {
        background: #263638;
        border: 1px solid #3C5254;
        border-radius: 4px;
        color: #C0E9E5;
        font-size: 11px;
        font-weight: 600;
        padding: 4px 9px;
    }
    QPushButton#ClipEffectAdd:hover { background: #2E4446; }
    QPushButton#ClipEffectAdd:pressed { background: #1F2F31; }
    QPushButton#ClipEffectAdd:disabled { color: #85858E; background: #26262A; border-color: #37373E; }
    QFrame#ClipEffectRow { border: none; border-bottom: 1px solid #35353B; }
    QFrame#ClipEffectRow:hover { background: #2C2C31; }
    QLabel#ClipEffectHint { color: #A3A3AE; font-size: 11px; padding: 0 2px; }
    QPushButton#ClipEffectName, QToolButton {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 3px;
    }
    QPushButton#ClipEffectName {
        color: #E0E0E6;
        font-size: 11px;
        text-align: left;
        padding: 0 4px;
    }
    QPushButton#ClipEffectName[bypassed="true"], QPushButton#ClipEffectName:disabled {
        color: #A1A1AC;
    }
    QPushButton#ClipEffectName:hover:enabled, QToolButton:hover:enabled { background: #383840; }
    QToolButton#ClipEffectRemove:hover:enabled { background: #4B3036; }
    QPushButton#ClipEffectAdd:focus, QPushButton#ClipEffectName:focus, QToolButton:focus {
        border-color: #578680;
    }
    QFrame#ClipEffectDropLine { background: #75D4CB; border: none; }
"""

_REORDER_MIME = "application/x-topos-clip-effect-reorder"


def _tr(key: str, fallback: str) -> str:
    return I18N.tr(f"mixer.clip_editor.rack.{key}", fallback)


def _direction_icon(normal: QIcon, disabled: QIcon) -> QIcon:
    size = QSize(26, 26)
    icon = QIcon(normal.pixmap(size))
    icon.addPixmap(disabled.pixmap(size), QIcon.Mode.Disabled)
    return icon


class _EffectName(QPushButton):
    edit_requested = pyqtSignal()

    def __init__(self, name: str, parent: QWidget, *, movable: bool) -> None:
        super().__init__(name, parent)
        self._movable = movable
        self._press_pos: QPoint | None = None
        self.clicked.connect(self.edit_requested.emit)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = None
            event.accept()
            self.click()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            event.accept()
            if not event.isAutoRepeat():
                self.click()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        start = self._press_pos
        if (self._movable and start is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_REORDER_MIME, b"1")
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(start)
            event.accept()
            drag.exec(Qt.DropAction.MoveAction)
        else:
            super().mouseMoveEvent(event)


class ClipEffectRack(QWidget):
    add_requested = pyqtSignal(str, str)
    settings_requested = pyqtSignal(str, str, str)
    enabled_changed = pyqtSignal(str, str, bool)
    reorder_requested = pyqtSignal(str, str, int)
    remove_requested = pyqtSignal(str, str)
    reveal_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_RACK_STYLE)
        self.setAcceptDrops(True)
        self._clip: ClipInstance | None = None
        self._editable = False
        self._rows: tuple[tuple[str, str, bool, int], ...] = ()

        self._power_on = glyph_icon("power", "#75D4CB", size=28)
        self._power_off = glyph_icon("power", "#94949F", size=28)
        self._up_icon = _direction_icon(icon_up("#B1B1BC"), icon_up("#62626B"))
        self._down_icon = _direction_icon(icon_down("#B1B1BC"), icon_down("#62626B"))
        self._remove_icon = icon_delete("#B1B1BC")

        self.add_button = QPushButton(
            glyph_icon("plus", "#A6DDD7", size=24), _tr("add", "Add effect"), self)
        self.add_button.setObjectName("ClipEffectAdd")
        self.add_button.setIconSize(QSize(12, 12))
        self.add_button.setMinimumHeight(27)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self._show_add_menu)
        self._hint = QLabel(self)
        self._hint.setObjectName("ClipEffectHint")
        self._hint.setWordWrap(True)

        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body = QWidget(self._scroll)
        self._list = QVBoxLayout(self._body)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(0)
        self._list.addStretch()
        self._scroll.setWidget(self._body)
        self._drop_line = QFrame(self._body)
        self._drop_line.setObjectName("ClipEffectDropLine")
        self._drop_line.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_line.hide()
        self._drag_position: tuple[int, QPoint] | None = None
        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.setInterval(32)
        self._drag_scroll_timer.timeout.connect(self._scroll_during_drag)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(8)
        layout.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._hint)
        layout.addWidget(self._scroll, 1)
        self.set_clip(None, editable=False)

    def set_clip(self, clip: ClipInstance | None, *, editable: bool) -> None:
        clip_id = clip.clip_id if clip is not None else ""
        editable = editable and clip is not None
        rows = tuple(
            (effect.effect_id, effect.effect_type, effect.is_enabled, index)
            for index, effect in sorted(enumerate(clip.effects), key=lambda item: item[1].order)
            if effect.effect_type in AUDIO_MIXER_EFFECT_TYPES
        ) if clip is not None else ()
        same_clip = ((self._clip.clip_id if self._clip is not None else "") == clip_id)
        unchanged = same_clip and editable == self._editable and rows == self._rows
        self._clip = clip
        self._editable = editable
        self.add_button.setEnabled(editable)
        if unchanged and (rows or self._hint.text()):
            return
        self._rows = rows
        self._clear_drag()
        hint = ""
        if clip is None:
            hint = _tr("source", "Open a clip from the timeline to add effects.")
        elif not self._editable:
            hint = _tr("locked", "Unlock the track to edit these effects.")
        elif not rows:
            hint = _tr("empty", "No effects on this clip yet.")
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))
        while self._list.count() > 1:
            previous = self._list.takeAt(0).widget()
            previous.hide()
            previous.deleteLater()
        for row_index in range(len(rows)):
            self._add_row(clip_id, row_index)

    def add_to_context_menu(self, menu: QMenu) -> None:
        if self._clip is None:
            return
        if menu.actions():
            menu.addSeparator()
        submenu = menu.addMenu(glyph_icon("plus"), _tr("add", "Add effect"))
        submenu.setEnabled(self._editable)
        if self._editable:
            self._populate_add_menu(submenu, self._clip.clip_id)

    def _show_add_menu(self) -> None:
        if self._clip is None or not self._editable:
            return
        menu = QMenu(self.add_button)
        self._populate_add_menu(menu, self._clip.clip_id)
        menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))
        menu.deleteLater()

    def _populate_add_menu(self, menu: QMenu, clip_id: str) -> None:
        for title, _, entries in audio_effect_groups():
            category = menu.addMenu(title)
            for effect in entries:
                action = category.addAction(audio_effect_name(effect))
                action.setData(effect.effect_type)
                action.triggered.connect(
                    lambda _checked=False, kind=effect.effect_type: self._add_effect(clip_id, kind))

    def _add_effect(self, clip_id: str, effect_type: str) -> None:
        if self._clip is not None and self._clip.clip_id == clip_id and self._editable:
            self.reveal_requested.emit()
            self.add_requested.emit(effect_type, clip_id)

    @staticmethod
    def _action_button(row: QFrame, object_name: str, icon: QIcon, label: str) -> QToolButton:
        button = QToolButton(row)
        button.setObjectName(object_name)
        button.setIcon(icon)
        button.setIconSize(QSize(13, 13))
        button.setFixedSize(24, 26)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(label)
        button.setToolTip(label)
        return button

    def _add_row(self, clip_id: str, index: int) -> None:
        effect_id, effect_type, enabled, _ = self._rows[index]
        definition = get_effect_definition(effect_type)
        name = audio_effect_name(definition) if definition is not None else effect_type
        row = QFrame()
        row.setObjectName("ClipEffectRow")
        row.setFixedHeight(34)
        row.setEnabled(self._editable)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(1, 1, 1, 2)
        layout.setSpacing(2)

        toggle_label = (_tr("bypass", "Bypass {effect}") if enabled
                        else _tr("enable", "Enable {effect} in the rack"))
        active = self._action_button(row, "ClipEffectToggle",
                                     self._power_on if enabled else self._power_off,
                                     toggle_label.format(effect=name))
        active.setCheckable(True)
        active.setChecked(enabled)
        active.clicked.connect(
            lambda checked: self.enabled_changed.emit(clip_id, effect_id, checked))

        movable = len(self._rows) > 1
        settings = _EffectName(name, row, movable=movable)
        settings.setObjectName("ClipEffectName")
        settings.setProperty("bypassed", not enabled)
        settings.setFixedHeight(26)
        settings.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        settings.setCursor(Qt.CursorShape.OpenHandCursor if movable else Qt.CursorShape.PointingHandCursor)
        settings.setAccessibleName(_tr("settings_for", "Settings for {effect}").format(effect=name))
        gesture = (_tr("edit_or_drag", "Double-click to edit · Drag to reorder") if movable
                   else _tr("edit", "Double-click to edit"))
        settings.setToolTip(f"{name}\n{gesture}")
        settings.edit_requested.connect(
            lambda: self.settings_requested.emit(clip_id, effect_id, effect_type))

        layout.addWidget(active)
        layout.addWidget(settings, 1)
        for offset, object_name, icon, label in (
            (-1, "ClipEffectMoveUp", self._up_icon, _tr("up", "Move {effect} up")),
            (1, "ClipEffectMoveDown", self._down_icon, _tr("down", "Move {effect} down")),
        ):
            target = index + offset
            can_move = self._editable and 0 <= target < len(self._rows)
            button = self._action_button(row, object_name, icon, label.format(effect=name))
            button.setEnabled(can_move)
            button.clicked.connect(lambda _checked=False, target=target: self._move_row(index, target))
            layout.addWidget(button)
        remove = self._action_button(row, "ClipEffectRemove", self._remove_icon,
                                     _tr("remove", "Remove {effect}").format(effect=name))
        remove.clicked.connect(lambda: self.remove_requested.emit(clip_id, effect_id))
        layout.addWidget(remove)
        self._list.insertWidget(self._list.count() - 1, row)

    def _move_row(self, source: int, target: int) -> None:
        if (self._editable and self._clip is not None and source != target
                and 0 <= source < len(self._rows) and 0 <= target < len(self._rows)):
            self.reorder_requested.emit(self._clip.clip_id, self._rows[source][0], self._rows[target][3])

    def _drag_index(self, event: QDropEvent) -> int:
        source = event.source()
        if self._editable and isinstance(source, _EffectName) and event.mimeData().hasFormat(_REORDER_MIME):
            return self._list.indexOf(source.parentWidget())
        return -1

    def _drop_target(self, source: int, pos: QPoint) -> int:
        y = self._body.mapFrom(self, pos).y()
        return sum(y > self._list.itemAt(index).geometry().center().y()
                   for index in range(len(self._rows)) if index != source)

    def _show_drop_line(self, source: int, pos: QPoint) -> None:
        target = self._drop_target(source, pos)
        if source == target:
            self._drop_line.hide()
            return
        row = self._list.itemAt(target).geometry()
        y = row.top() if target < source else row.bottom() + 1
        self._drop_line.setGeometry(1, min(y, self._body.height() - 2), self._body.width() - 2, 2)
        self._drop_line.raise_()
        self._drop_line.show()

    def _clear_drag(self) -> None:
        self._drag_position = None
        self._drag_scroll_timer.stop()
        self._drop_line.hide()

    def _scroll_during_drag(self) -> None:
        if self._drag_position is None:
            return
        source, pos = self._drag_position
        viewport = self._scroll.viewport()
        y = viewport.mapFrom(self, pos).y()
        edge = min(20, viewport.height() // 4)
        delta = -12 if y < edge else 12 if y >= viewport.height() - edge else 0
        bar = self._scroll.verticalScrollBar()
        previous = bar.value()
        if delta:
            bar.setValue(previous + delta)
        if bar.value() != previous:
            self._show_drop_line(source, pos)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._drag_index(event) >= 0:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        source = self._drag_index(event)
        pos = event.position().toPoint()
        viewport = self._scroll.viewport()
        if source < 0 or not viewport.rect().contains(viewport.mapFrom(self, pos)):
            self._clear_drag()
            event.ignore()
            return
        self._drag_position = (source, pos)
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()
        self._show_drop_line(source, pos)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._clear_drag()
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        source = self._drag_index(event)
        pos = event.position().toPoint()
        active = self._drag_position is not None
        self._clear_drag()
        viewport = self._scroll.viewport()
        if source >= 0 and active and viewport.rect().contains(viewport.mapFrom(self, pos)):
            target = self._drop_target(source, pos)
            event.acceptProposedAction()
            self._move_row(source, target)

    def hideEvent(self, event: QHideEvent) -> None:
        self._clear_drag()
        super().hideEvent(event)
