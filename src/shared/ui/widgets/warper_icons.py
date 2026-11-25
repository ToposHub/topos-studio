"""SVG artwork for the Color Warper palette."""
from pathlib import Path

from PyQt6.QtGui import QIcon

from src.shared import assets
from src.shared.ui.tinted_icon import TintedIconEngine


_ICON_DIR = Path(assets.__file__).parent / "icons" / "warper"
WARPER_TOOL_IDLE = "#949aa4"
WARPER_TOOL_HOVER = "#c8cdd5"


def warper_icon(name: str, color: str | None = None) -> QIcon:
    icon = QIcon(str(_ICON_DIR / f"{name}.svg"))
    if name == "space" or icon.isNull():
        return icon
    return QIcon(TintedIconEngine(
        icon, idle=color or WARPER_TOOL_IDLE, hover=color or WARPER_TOOL_HOVER,
        active=color or "#fafbfc", disabled=color or "#646870",
    ))
