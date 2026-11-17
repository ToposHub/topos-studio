"""UI-only audio effect catalog and owned parameter-window lifecycle."""
from .catalog import EFFECT_CATALOG, EffectDescriptor, EffectInstance, create_effect_panel, create_panel, effect_catalog, get_effect
from .windows import EffectParameterWindow, EffectWindowManager, stop_panel_timers
from .widgets import EffectBrowserWidget, EffectRackWidget

__all__ = [
    "EFFECT_CATALOG", "EffectDescriptor", "EffectInstance", "effect_catalog", "get_effect",
    "create_panel", "create_effect_panel", "EffectWindowManager", "EffectParameterWindow", "EffectBrowserWidget",
    "EffectRackWidget", "stop_panel_timers",
]
