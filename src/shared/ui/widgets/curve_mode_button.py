"""SVG mode buttons for the curve palette header."""
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QToolButton, QWidget

from src.shared import assets


_CURVE_ICONS = Path(assets.__file__).parent / "icons" / "curves"


class CurveModeButton(QToolButton):
    def __init__(self, mode: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setCheckable(True)
        self.setIcon(QIcon(str(_CURVE_ICONS / f"{mode}.svg")))
        self.setToolTip(title)
        self.setAccessibleName(title)
