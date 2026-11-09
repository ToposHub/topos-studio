"""Read-only, lightweight information popup for media-pool poster badges."""
from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


class MediaPoolInfoPopup(QFrame):
    """Show already-probed metadata without decoding or opening an editor."""

    def __init__(self, title: str, rows: list[tuple[str, str]], parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("mediaPoolInfoPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAccessibleName(I18N.tr("edit.media_pool.clip_information", "Clip Information"))
        self.setMinimumWidth(285)
        self.setMaximumWidth(360)
        self.setStyleSheet(f"""
            QFrame#mediaPoolInfoPopup {{ background: {C.BG_DARK}; border: 1px solid {C.BORDER_HOVER}; border-radius: 3px; }}
            QLabel {{ background: transparent; border: none; color: {C.TEXT_PRIMARY}; font-size: 11px; }}
            QLabel#mediaPoolInfoTitle {{ font-weight: 600; font-size: 12px; }}
            QLabel#mediaPoolInfoKey {{ color: {C.TEXT_HINT}; }}
            QToolButton {{ background: transparent; border: none; color: {C.TEXT_HINT}; padding: 0; }}
            QToolButton:hover {{ color: {C.TEXT_PRIMARY}; background: {C.BG_ACTIVE}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("mediaPoolInfoTitle")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setWordWrap(True)
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.addWidget(name, 1)
        close = QToolButton()
        close.setText("×")
        close.setFixedSize(18, 18)
        close.setToolTip(I18N.tr("common.close", "Close"))
        close.setAccessibleName(close.toolTip())
        close.clicked.connect(self.close)
        heading.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)
        details = QGridLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setHorizontalSpacing(14)
        details.setVerticalSpacing(6)
        details.setColumnStretch(1, 1)
        for row, (key, text) in enumerate(rows):
            label = QLabel(key)
            label.setObjectName("mediaPoolInfoKey")
            label.setTextFormat(Qt.TextFormat.PlainText)
            value = QLabel(text or "—")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setAccessibleName(f"{key}: {text or '—'}")
            details.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            details.addWidget(value, row, 1)
        layout.addLayout(details)

    def show_at(self, anchor: QRect, available: QRect) -> None:
        """Anchor beside the clicked badge, keeping the popup on its screen."""
        self.adjustSize()
        x = anchor.right() + 8
        if x + self.width() > available.right() - 4:
            x = anchor.left() - self.width() - 8
        x = max(available.left() + 4, min(x, available.right() - self.width() - 4))
        y = max(available.top() + 4, min(anchor.top(), available.bottom() - self.height() - 4))
        self.move(x, y)
        self.show()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)
