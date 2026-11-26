"""Localized categories for the registered audio effects exposed by Mixer."""

from __future__ import annotations

from collections.abc import Iterator, Set

from src.features.edit.core.effect_registry import (
    AUDIO_MIXER_EFFECT_TYPES, EffectDefinition, get_effect_registry,
)
from src.shared.i18n.audio_effects import audio_effect_display_name
from src.shared.i18n.i18n import I18N


_GROUPS = (
    ("eq", "EQ & Cleanup", ("strip_eq", "dynamic_eq", "de_esser")),
    ("dynamics", "Dynamics & Transients", (
        "dynamics", "compressor", "limiter", "true_peak_limiter", "expander",
        "bus_comp", "multiband_comp", "transient_designer",
    )),
    ("space", "Space & Time", (
        "reverb", "convolution_reverb", "delay", "imager", "spatial_panner",
    )),
    ("modulation", "Modulation", ("tremolo_auto_pan", "chorus_flanger", "phaser")),
    ("tone", "Tone & Voice", ("saturation", "voice_changer", "spectral_mangler")),
    ("other", "Other Audio Effects", ()),
)

def audio_effect_name(effect: EffectDefinition) -> str:
    return audio_effect_display_name(effect.effect_type, effect.name)


def audio_effect_groups(
    *, include_track_effects: bool = False, effect_types: Set[str] | None = None,
) -> Iterator[tuple[str, str, list[EffectDefinition]]]:
    # Spatial Panner is a track parameter. The old noise_gate ID aliases expander.
    supported = AUDIO_MIXER_EFFECT_TYPES - {"noise_gate"}
    if include_track_effects:
        supported |= {"spatial_panner"}
    if effect_types is not None:
        supported &= effect_types
    effects = {
        effect.effect_type: effect
        for effect in get_effect_registry().get_effects_by_category("audio")
        if effect.effect_type in supported
    }
    for key, group_name, ordered_types in _GROUPS:
        ordered = ordered_types if key != "other" else tuple(sorted(effects))
        entries = [effects.pop(effect_type) for effect_type in ordered if effect_type in effects]
        if entries:
            yield I18N.tr(f"mixer.effects_browser.group.{key}", group_name), group_name, entries
