"""A slim vertical scrollbar for panels with dense lists."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QScrollBar, QStyleOptionSlider, QWidget

from src.shared.ui.style_sheet import C


_MAX_THUMB_HEIGHT = 168
_STYLE = f"""
    QScrollBar:vertical {{ width: 6px; margin: 0; border: none; background: transparent; }}
    QScrollBar::handle:vertical {{ min-height: 20px; margin: 0;
        border: none; border-radius: 3px; background: {C.BORDER_MID}; }}
    QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
        background: {C.TEXT_HINT}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0; border: none; background: transparent; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        border: none; background: transparent; }}
"""


class CompactVerticalScrollBar(QScrollBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setStyleSheet(_STYLE)

    def initStyleOption(self, option: QStyleOptionSlider) -> None:
        super().initStyleOption(option)
        scroll_range = option.maximum - option.minimum
        track_height = option.rect.height()
        if scroll_range > 0 and track_height > _MAX_THUMB_HEIGHT:
            # Invert Qt's track height * pageStep / (range + pageStep) formula.
            # The style option controls geometry, while the scrollbar keeps its real page step.
            max_page = _MAX_THUMB_HEIGHT * scroll_range // (track_height - _MAX_THUMB_HEIGHT)
            option.pageStep = min(option.pageStep, max_page)
