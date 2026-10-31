"""Motion Graphic preset library: reusable scene fragments with GIF previews.

A preset is one folder — code (``build.py``) or an editor-saved scene
(``scene.json``), an animated ``preview.gif`` and optional ``assets/`` —
grouped by category under a library root.  The editor browses the merged
bundled + user libraries, drags presets onto the canvas and saves the current
selection back as new presets.  See ``README.md`` in this package for the
on-disk contract.
"""

from . import builders
from .fragment import (
    extract_fragment,
    fragment_bounds,
    fragment_max_keyframe_frame,
    prepare_insert,
    retime_fragment_to_page,
)
from .library import MgPresetLibrary, PresetError, PresetRecord
from .loader import load_preset_scene
from .manifest import (
    PRESET_ENTRY_CODE,
    PRESET_ENTRY_SCENE,
    PRESET_PREVIEW_NAME,
    PresetManifest,
    normalize_category,
    slugify,
)
from .paths import bundled_preset_root, default_preset_roots, user_preset_root
from .preview import PREVIEW_WIDTH, render_preset_gif

#: Drag-and-drop mime type shared by the library panel and the canvas views.
MG_PRESET_MIME = "application/x-topos-mg-preset"

__all__ = [
    "MG_PRESET_MIME", "PRESET_ENTRY_CODE", "PRESET_ENTRY_SCENE", "PRESET_PREVIEW_NAME",
    "PREVIEW_WIDTH", "MgPresetLibrary", "PresetError", "PresetManifest", "PresetRecord",
    "builders", "bundled_preset_root", "default_preset_roots", "extract_fragment",
    "fragment_bounds", "fragment_max_keyframe_frame", "load_preset_scene",
    "normalize_category", "prepare_insert", "render_preset_gif",
    "retime_fragment_to_page", "slugify", "user_preset_root",
]
