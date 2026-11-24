"""Controls shared by the audio workspace and styled like Topos Edit."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFrame, QToolButton, QWidget

from src.features.edit.ui.edit_style import EDIT_COLORS, EDIT_SIZES
from src.shared.ui.style_sheet import C, COMPACT_HORIZONTAL_SCROLLBAR_STYLE
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


PANEL_QSS = """
QWidget#MixerConsole, QWidget#MixerViewer, QWidget#MixerTimeline,
QWidget#MixerClipEditorHost, QDialog#MixerEffectBrowser {
    background: %s; color: %s;
}
QFrame#MixerToolbar { background: %s; border-bottom: 1px solid %s; }
QLabel { background: transparent; border: none; color: %s; font-size: 11px; }
QPushButton { background: #2E2F35; border: 1px solid #3C3D45; border-radius: 4px;
    color: #CBCCD3; padding: 2px 6px; font-size: 11px; min-height: 16px; }
QPushButton:hover { background: #3A3B43; color: #F2F2F5; }
QPushButton:checked { background: #3F3223; color: #F2AB58; border-color: #654B34; }
QPushButton:disabled { color: #73747D; }
QLineEdit, QDoubleSpinBox { background: #202025; color: #D6D8DE;
    border: 1px solid #3A3A44; border-radius: 4px; padding: 2px 4px; font-size: 11px; }
QScrollArea { border: none; background: #222226; }
QListWidget { background: #191A1F; border: 1px solid #34363E; border-radius: 3px;
    color: #BFC0CC; font-size: 11px; }
QListWidget::item:selected { background: #433327; }
""" % (C.BG_PANEL, EDIT_COLORS["text"], EDIT_COLORS["bg_toolbar"],
       EDIT_COLORS["border_subtle"], EDIT_COLORS["text"]) + COMPACT_HORIZONTAL_SCROLLBAR_STYLE

TOOL_QSS = """
QToolButton { background: transparent; border: none; border-radius: 4px; padding: 3px; }
QToolButton:hover { background: rgba(255, 255, 255, 18); }
QToolButton:pressed { background: rgba(255, 255, 255, 10); }
QToolButton:focus { border: 1px solid #69636B; }
"""


def tool_button(
    label: str, icon: QIcon, callback: Callable[[], None] | None = None,
    *, size: int = EDIT_SIZES["icon_btn"], glyph: int = 18,
) -> QToolButton:
    button = QToolButton()
    button.setIcon(icon)
    button.setAccessibleName(label)
    button.setToolTip(label)
    button.setFixedSize(size, size)
    button.setIconSize(QSize(glyph, glyph))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(TOOL_QSS)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def toolbar_divider() -> QWidget:
    divider = QFrame()
    divider.setFixedSize(1, 16)
    divider.setStyleSheet(f"background: {EDIT_COLORS['border']}; border: none;")
    return divider


def compact_combo() -> ChevronComboBox:
    combo = ChevronComboBox()
    combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(3)
    combo.setStyleSheet("""
        QComboBox { background: #232325; border: 1px solid #383942; border-radius: 4px;
            padding: 1px 17px 1px 5px; color: #D6D8DE; font-size: 11px; min-height: 17px; }
        QComboBox:hover { background: #2E2F35; }
        QComboBox:disabled { color: #777982; }
        QComboBox::drop-down { border: none; background: transparent; width: 0; }
        QComboBox::down-arrow { image: none; border: none; width: 0; height: 0; }
    """)
    return combo
