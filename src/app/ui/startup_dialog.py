"""Shared controls for the image and video startup dialogs."""

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QButtonGroup, QDialog, QFrame, QHBoxLayout, QPushButton, QWidget

from src.shared.i18n.i18n import I18N


class WorkModeSelector(QFrame):
    """The image and video tabs displayed by both studio launchers."""

    mode_requested = pyqtSignal(str)

    def __init__(self, current_mode: str) -> None:
        super().__init__()
        self.setObjectName("pmModes")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for mode, default in (("image", "Image Grading"), ("video", "Video Grading")):
            button = QPushButton(I18N.tr(f"preferences.general.work_mode.{mode}", default))
            button.setObjectName("pmMode")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setFixedHeight(36)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(button)
            button.setChecked(mode == current_mode)
            button.clicked.connect(lambda _checked, target=mode: self.mode_requested.emit(target))
            self.buttons[mode] = button
            layout.addWidget(button, 1)

        self.setStyleSheet("""
            QFrame#pmModes {
                background: #202024; border: 1px solid #414148; border-radius: 10px;
            }
            QPushButton#pmMode {
                background: transparent; color: #f2f2f4; border: 1px solid transparent;
                border-radius: 6px; padding: 0 12px;
                min-width: 100px; font-size: 13px; font-weight: 600;
            }
            QPushButton#pmMode:hover { background: #393940; }
            QPushButton#pmMode:pressed { background: #323238; }
            QPushButton#pmMode:focus { border-color: #ff955c; }
            QPushButton#pmMode:checked {
                background: #c84b08; color: #ffffff; font-weight: 700; padding: 0 16px;
            }
            QPushButton#pmMode:checked:hover { background: #d54404; }
            QPushButton#pmMode:checked:pressed { background: #b94008; }
            QPushButton#pmMode:checked:focus { border-color: #ffffff; }
            QPushButton#pmMode:disabled { color: #81818b; background: transparent; }
        """)


class DraggableHeaderDialog(QDialog):
    """Allow a frameless dialog to move from its header."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._header = QWidget(self)
        self._drag_offset: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        header_bottom = self._header.mapTo(self, self._header.rect().bottomLeft()).y()
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= header_bottom:
            self._drag_offset = None
            window = self.windowHandle()
            if window is None or not window.startSystemMove():
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
