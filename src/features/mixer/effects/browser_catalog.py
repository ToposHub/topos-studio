"""Localized categories for the registered audio effects exposed by Mixer."""

from __future__ import annotations

from collections.abc import Iterator, Set

from src.features.edit.core.effect_registry import (
    AUDIO_MIXER_EFFECT_TYPES, EffectDefinition, get_effect_registry,
)
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

_NAME_KEYS = {
    "strip_eq": "audio.fx.parametric_eq",
    "expander": "audio.fx.expander_gate",
    "reverb": "audio.fx.acoustic_reverb",
    "delay": "audio.fx.stereo_delay",
    "voice_changer": "mixer.effects_browser.voice_changer",
    "spectral_mangler": "mixer.effects_browser.spectral_mangler",
}


def audio_effect_name(effect: EffectDefinition) -> str:
    effect_type = effect.effect_type
    name = I18N.tr(_NAME_KEYS.get(effect_type, f"audio.fx.{effect_type}"), effect.name)
    return I18N.tr(f"audio.fx.name.{effect_type}", name)


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
