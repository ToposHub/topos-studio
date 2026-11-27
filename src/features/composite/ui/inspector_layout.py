"""Shared layout for parameters in the composite inspector."""

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QResizeEvent, QShowEvent
from PyQt6.QtWidgets import QComboBox, QLabel

from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


PARAM_LABEL_WIDTH = 96
MULTI_AXIS_VALUE_WIDTH = 46
PARAM_LABEL_STYLE = f"color: {C.TEXT_DIM}; font-size: 10px;"
SECTION_CONTENT_MARGINS = (8, 6, 8, 6)

_MIN_COMBO_LABEL_WIDTH = 60
_COMBO_TEXT_INSETS = 44  # ChevronComboBox borders, left padding and chevron.


class InspectorComboBox(ChevronComboBox):
    """Make room for the selected text while keeping short options aligned."""

    def __init__(self, label: QLabel) -> None:
        super().__init__()
        self._label = label
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(4)
        self.currentTextChanged.connect(self._fit_selected_text)

    def _fit_selected_text(self) -> None:
        if self.width() <= 0 or not self.isVisible():
            return
        # The two widths combined stay constant when the form gives the field
        # space from its label. This also lets the label recover on enlargement.
        available = self.width() + self._label.width()
        needed = self.fontMetrics().horizontalAdvance(self.currentText()) + _COMBO_TEXT_INSETS
        label_width = max(_MIN_COMBO_LABEL_WIDTH, min(PARAM_LABEL_WIDTH, available - needed))
        if self._label.width() != label_width:
            self._label.setFixedWidth(label_width)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_selected_text()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit_selected_text()

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        self._fit_selected_text()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            self.setToolTip(self.currentText())
        return super().event(event)
