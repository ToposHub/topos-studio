"""Title rows and icon states shared by the Color workspace panels."""

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QSize, Qt
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QAbstractButton, QHBoxLayout, QLabel, QMainWindow, QToolButton, QVBoxLayout, QWidget,
    QWIDGETSIZE_MAX,
)

from src.shared.assets.icon_manager import icon_reset
from src.shared.ui.dock_tab_style import DOCK_TAB_BAR_HEIGHT
from src.shared.ui.panel_divider import PanelDockWidget
from src.shared.ui.style_sheet import C
from src.shared.ui.tinted_icon import TintedIconEngine
from src.shared.ui.widgets.param_slider_row import ParamSliderRow

HEADER_HEIGHT = 30
PARAMETER_BAR_HEIGHT = 34
PARAMETER_BAR_BOTTOM_MARGIN = 2
PARAMETER_ROW_HEIGHT = 20
PARAMETER_ROW_SPACING = 3
SECTION_TITLE_STYLE = f"color: {C.TEXT_PRIMARY}; font-size: 12px; font-weight: 600; background: transparent;"
PARAMETER_NAME_STYLE = "color: #aeb0b6; font-size: 11px; font-weight: 400; background: transparent;"
PARAMETER_DISABLED_COLOR = "#777982"
PARAMETER_LABEL_STYLE = (
    f"QLabel {{ {PARAMETER_NAME_STYLE} }} "
    f"QLabel:disabled {{ color: {PARAMETER_DISABLED_COLOR}; }}"
)
HEADER_CONTROL_HEIGHT = 26
HEADER_ICON_SIZE = 14
HEADER_ICON_IDLE = "#6e7480"
HEADER_ICON_HOVER = "#b8bec8"
HEADER_ICON_ACTIVE = "#f3f4f6"
HEADER_ICON_DISABLED = "#4e5158"
COLOR_TOOL_DOCK_DEFAULT_HEIGHT = 360
COLOR_TOOL_DOCK_MAX_HEIGHT = 380

_RESET_ICON_IDLE = "#9096a0"
_RESET_CONTROL_SIZE = 16
_RESET_ICON_SIZE = 11


class ColorParameterRow(ParamSliderRow):
    """Use compact rows and parameter typography in the Color workspace."""

    _LABEL_STYLE = PARAMETER_LABEL_STYLE
    _VALUE_ROW_HEIGHT = PARAMETER_ROW_HEIGHT


class ColorToolDockWidget(PanelDockWidget):
    """Keep the height cap with the tool through docking and floating."""

    def __init__(self, title: str, host: QMainWindow) -> None:
        super().__init__(title, host)
        self._dock_host = host
        self.setMaximumHeight(COLOR_TOOL_DOCK_MAX_HEIGHT)
        self.topLevelChanged.connect(self.sync_height_limit)
        self.dockLocationChanged.connect(self._dock_area_changed)

    def tab_height(self) -> int:
        # The tab strip is outside the dock widget's own geometry.
        return DOCK_TAB_BAR_HEIGHT if any(
            not other.isHidden() for other in self._dock_host.tabifiedDockWidgets(self)
        ) else 0

    def sync_height_limit(self, is_floating: bool | None = None) -> None:
        floating = self.isFloating() if is_floating is None else is_floating
        docked = (not floating and self._dock_host.dockWidgetArea(self)
                  != Qt.DockWidgetArea.NoDockWidgetArea)
        height = COLOR_TOOL_DOCK_MAX_HEIGHT - self.tab_height() if docked else QWIDGETSIZE_MAX
        if self.maximumHeight() != height:
            self.setMaximumHeight(height)

    def _dock_area_changed(self, _area: Qt.DockWidgetArea) -> None:
        self.sync_height_limit()


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
            .replace("__COLOR_PANEL_RAISED__", C.COLOR_PANEL_RAISED)
            .replace("__COLOR_PARAMETER_NAME__", PARAMETER_NAME_STYLE)
            .replace("__COLOR_PARAMETER_DISABLED__", PARAMETER_DISABLED_COLOR)
            .replace("__COLOR_SECTION_TITLE__", SECTION_TITLE_STYLE))


def create_parameter_section(
    title: str, *, spacing: int = PARAMETER_ROW_SPACING,
) -> tuple[QWidget, QVBoxLayout]:
    """Build a flat section with a compact title and parameter spacing."""
    section = QWidget()
    layout = QVBoxLayout(section)
    layout.setContentsMargins(10, 5, 10, 5)
    layout.setSpacing(spacing)
    heading = QLabel(title, section)
    heading.setObjectName("colorParameterSectionTitle")
    heading.setStyleSheet(SECTION_TITLE_STYLE)
    layout.addWidget(heading)
    return section, layout


def create_panel_divider(orientation: Qt.Orientation) -> QWidget:
    """Create a one-pixel layout seam between sections."""
    divider = QWidget()
    divider.setObjectName("colorParameterDivider")
    if orientation == Qt.Orientation.Vertical:
        divider.setFixedWidth(1)
    else:
        divider.setFixedHeight(1)
    divider.setStyleSheet(f"background: {C.PANEL_DIVIDER}; border: none;")
    return divider


def header_icon(icon: QIcon, *, highlighted: bool = False) -> QIcon:
    """Keep the glyph shape and apply the title-row palette in every Qt state."""
    if icon.isNull():
        return QIcon()
    return QIcon(TintedIconEngine(
        icon, idle=HEADER_ICON_ACTIVE if highlighted else HEADER_ICON_IDLE,
        hover=HEADER_ICON_ACTIVE if highlighted else HEADER_ICON_HOVER,
        active=HEADER_ICON_ACTIVE, disabled=HEADER_ICON_DISABLED,
    ))


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
