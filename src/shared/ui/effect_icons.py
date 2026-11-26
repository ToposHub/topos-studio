"""SVG icons shared by the Color, Edit, and Mixer effect libraries."""

from functools import cache, lru_cache
from pathlib import Path

from PyQt6.QtGui import QIcon

from src.shared.assets.icon_manager import icon_fx
from src.shared.ui.style_sheet import C
from src.shared.ui.tinted_icon import TintedIconEngine


_EFFECT_ICONS = Path(__file__).resolve().parents[1] / "assets" / "icons" / "effects"
COLOR_EFFECT_ICON_DIR = _EFFECT_ICONS / "color"
AUDIO_EFFECT_ICON_DIR = _EFFECT_ICONS / "audio"


@cache
def _color_effect_icon_paths() -> dict[str, Path]:
    return {path.stem: path for path in COLOR_EFFECT_ICON_DIR.glob("*.svg")}


def color_effect_icon(resource_id: str) -> QIcon:
    """Match a plugin ID independently of its version and display language."""
    plugin_id = resource_id.partition("@")[0]
    path = _color_effect_icon_paths().get(plugin_id, COLOR_EFFECT_ICON_DIR / "generic.svg")
    return QIcon(str(path))


@lru_cache(maxsize=128)
def audio_effect_icon(effect_id: str) -> QIcon:
    path = AUDIO_EFFECT_ICON_DIR / f"{effect_id}.svg"
    icon = QIcon(str(path)) if path.is_file() else icon_fx(C.TEXT_DIM)
    return QIcon(TintedIconEngine(
        icon, idle=C.TEXT_DIM, hover=C.TEXT_LABEL, active="#ffffff", disabled=C.TEXT_HINT,
    ))
