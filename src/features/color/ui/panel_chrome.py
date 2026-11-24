"""Title rows and icon states shared by the Color workspace panels."""

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QToolButton, QVBoxLayout, QWidget

from src.shared.assets.icon_manager import icon_reset
from src.shared.ui.style_sheet import C

HEADER_HEIGHT = 30
HEADER_CONTROL_HEIGHT = 26
HEADER_ICON_SIZE = 14
HEADER_ICON_IDLE = "#6e7480"
HEADER_ICON_HOVER = "#b8bec8"
HEADER_ICON_ACTIVE = "#f3f4f6"
HEADER_ICON_DISABLED = "#4e5158"

_RESET_ICON_IDLE = "#9096a0"
_RESET_CONTROL_SIZE = 16
_RESET_ICON_SIZE = 11


class _ResetButtonAppearance(QObject):
    """Apply the glyph state consistently to push buttons and tool buttons."""

    def __init__(self, button: QAbstractButton, glyph: Callable[[str], QIcon]) -> None:
        super().__init__(button)
        self._button = button
        self._glyph = glyph
        self._hovered = button.underMouse()
        self._color = ""
        button.installEventFilter(self)
        button.pressed.connect(self._update_icon)
        button.released.connect(self._update_icon)
        self._update_icon()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.Hide):
            self._hovered = kind == QEvent.Type.Enter
            self._update_icon()
        elif kind == QEvent.Type.EnabledChange:
            self._hovered = self._button.underMouse()
            self._update_icon()
        return False

    def _update_icon(self) -> None:
        if not self._button.isEnabled():
            color = HEADER_ICON_DISABLED
        elif self._button.isDown():
            color = HEADER_ICON_ACTIVE
        else:
            color = HEADER_ICON_HOVER if self._hovered else _RESET_ICON_IDLE
        if color != self._color:
            self._color = color
            icon = self._glyph(color)
            self._button.setIcon(icon if not icon.isNull() else icon_reset(color))


def style_reset_button(
    button: QAbstractButton, *, global_reset: bool = False,
    glyph: Callable[[str], QIcon] = icon_reset,
) -> None:
    """Use the wheel's global dimensions or a compact reset for a local action."""
    width, height = (24, HEADER_CONTROL_HEIGHT) if global_reset else (_RESET_CONTROL_SIZE, _RESET_CONTROL_SIZE)
    size = HEADER_ICON_SIZE if global_reset else _RESET_ICON_SIZE
    button.setFixedSize(width, height)
    button.setIconSize(QSize(size, size))
    button.setText("")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    # The application stylesheet also assigns sizes to legacy reset buttons.
    button.setStyleSheet(f"""
        QAbstractButton {{
            background: transparent; border: none; padding: 0; margin: 0;
            min-width: {width}px; max-width: {width}px;
            min-height: {height}px; max-height: {height}px;
        }}
    """)
    _ResetButtonAppearance(button, glyph)


def color_panel_stylesheet(style: str) -> str:
    """Resolve the Color surface roles in QSS with ordinary selector braces."""
    return (style.replace("__COLOR_PANEL_BASE__", C.BG_PANEL)
            .replace("__COLOR_PANEL_INSET__", C.COLOR_PANEL_INSET)
            .replace("__COLOR_PANEL_RAISED__", C.COLOR_PANEL_RAISED))


class _HeaderIconEngine(QIconEngine):
    def __init__(self, icon: QIcon, highlighted: bool) -> None:
        super().__init__()
        self._icon = QIcon(icon)
        self._highlighted = highlighted

    def clone(self) -> QIconEngine:
        return _HeaderIconEngine(self._icon, self._highlighted)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        pixmap = self.scaledPixmap(rect.size(), mode, state, painter.device().devicePixelRatioF())
        painter.drawPixmap(rect, pixmap)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pixmap = self._icon.pixmap(size, scale, QIcon.Mode.Normal, state)
        if pixmap.isNull():
            return pixmap
        if mode == QIcon.Mode.Disabled:
            color = HEADER_ICON_DISABLED
        elif self._highlighted or state == QIcon.State.On or mode == QIcon.Mode.Selected:
            color = HEADER_ICON_ACTIVE
        elif mode == QIcon.Mode.Active:
            color = HEADER_ICON_HOVER
        else:
            color = HEADER_ICON_IDLE
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(QRectF(QPointF(), pixmap.deviceIndependentSize()), QColor(color))
        painter.end()
        return pixmap


def header_icon(icon: QIcon, *, highlighted: bool = False) -> QIcon:
    """Keep the glyph shape and apply the title-row palette in every Qt state."""
    return QIcon() if icon.isNull() else QIcon(_HeaderIconEngine(icon, highlighted))


def header_button_color(button: QAbstractButton) -> QColor:
    """Use the same palette for icon controls that draw their own glyph."""
    if not button.isEnabled():
        return QColor(HEADER_ICON_DISABLED)
    if button.isChecked() or button.isDown():
        return QColor(HEADER_ICON_ACTIVE)
    return QColor(HEADER_ICON_HOVER if button.underMouse() else HEADER_ICON_IDLE)


def style_header_button(
    button: QAbstractButton, *, width: int | None = None, icon: QIcon | None = None,
) -> None:
    button.setFixedHeight(HEADER_CONTROL_HEIGHT)
    if width is not None:
        button.setFixedWidth(width)
    button.setIconSize(QSize(HEADER_ICON_SIZE, HEADER_ICON_SIZE))
    if icon is not None:
        button.setIcon(header_icon(icon))
    if isinstance(button, QToolButton):
        button.setAutoRaise(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(f"""
        QAbstractButton {{ background: transparent; border: none; padding: 0; margin: 0;
                          color: {HEADER_ICON_IDLE}; }}
        QAbstractButton:hover {{ color: {HEADER_ICON_HOVER}; }}
        QAbstractButton:checked, QAbstractButton:pressed {{ color: {HEADER_ICON_ACTIVE}; }}
        QAbstractButton:disabled {{ color: {HEADER_ICON_DISABLED}; }}
        QToolButton::menu-indicator {{ image: none; width: 0; }}
    """)


def create_panel_header(
    *, object_name: str = "colorToolHeader", bottom_rule: bool = True,
) -> QWidget:
    header = QWidget()
    header.setObjectName(object_name)
    header.setFixedHeight(HEADER_HEIGHT)
    border = f"border-bottom: 1px solid {C.PANEL_DIVIDER};" if bottom_rule else ""
    header.setStyleSheet(
        f"QWidget#{object_name} {{ background: {C.COLOR_PANEL_HEADER}; {border} }}"
        "QLabel { background: transparent; }"
    )
    return header


def create_panel_layout(
    panel: QWidget, margins: tuple[int, int, int], spacing: int,
) -> tuple[QHBoxLayout, QVBoxLayout]:
    """Keep padding on the controls and let the title row reach the panel edges."""
    outer = QVBoxLayout(panel)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    header = create_panel_header()
    title_row = QHBoxLayout(header)
    left, right, bottom = margins
    title_row.setContentsMargins(left, 0, right, 0)
    title_row.setSpacing(spacing)
    outer.addWidget(header)
    body = QVBoxLayout()
    body.setContentsMargins(left, 0, right, bottom)
    body.setSpacing(spacing)
    outer.addLayout(body, 1)
    return title_row, body
