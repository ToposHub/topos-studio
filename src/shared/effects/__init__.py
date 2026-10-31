"""
Effect Preview Quality Module
效果预览质量系统

Re-exports the playback degradation subsystem.  The authoritative
implementation lives in :mod:`src.shared.effects.preview_quality`;
this package exposes the manager singleton accessor
(:func:`get_effect_preview_quality_manager`) plus the pure helpers used
by render threads.  Manager state is mutated on the UI thread only;
exported query helpers are safe to call from any thread but carry no
internal locking.
"""

from src.shared.effects.preview_quality import (
    PreviewQualityLevel,
    EffectCostCategory,
    HeavyEffectDefinition,
    BUILTIN_HEAVY_EFFECTS,
    PlaybackDegradationState,
    EffectPreviewQualityManager,
    get_effect_preview_quality_manager,
    is_playback_degradation_active,
    get_degraded_effect_params,
)

__all__ = [
    "PreviewQualityLevel",
    "EffectCostCategory",
    "HeavyEffectDefinition",
    "BUILTIN_HEAVY_EFFECTS",
    "PlaybackDegradationState",
    "EffectPreviewQualityManager",
    "get_effect_preview_quality_manager",
    "is_playback_degradation_active",
    "get_degraded_effect_params",
]