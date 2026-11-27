"""Bindings from the supplied panels to existing audio and panner parameters."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .bindings_core import Field, SKIP


def _same(*paths: str) -> tuple[Field, ...]:
    return tuple(Field(path, path) for path in paths)


def _enum(ui: str, engine: str, choices: dict[str, str]) -> Field:
    labels = {value: label for label, value in choices.items()}
    return Field(ui, engine, read=lambda value: labels.get(value, value),
                 write=lambda label: choices.get(label, label))


def _prefix(section: str, paths: tuple[tuple[str, str], ...],
            engine_prefix: str = "") -> tuple[Field, ...]:
    return tuple(Field(f"{section}.{ui}", f"{engine_prefix}{engine}") for ui, engine in paths)


def _scale(factor: float) -> Callable[[Any], float]:
    return lambda value: float(value) * factor


def _delay_format(value: str) -> Any:
    return value if value in {"Mono", "Stereo", "Left", "Right"} else SKIP


_TIMING = (
    ("threshold", "threshold_db"), ("attack", "attack_ms"),
    ("hold", "hold_ms"), ("release", "release_ms"),
)
_EXPANDER = (*_TIMING, ("mode", "mode"), ("range", "range_db"), ("ratio", "ratio"))
_COMPRESSOR = (
    *_TIMING, ("ratio", "ratio"), ("knee", "knee"), ("mix", "mix"),
    ("sc_on", "sc_on"), ("sc_listen", "sc_listen"),
)
_SIDECHAIN_SOURCE = {"Internal": "internal"}

_EQ_BANDS = tuple(
    Field(f"bands.{index}.{key}", f"bands.{index}.{key}")
    for index in range(6)
    for key in ("enabled", "solo", "filter_type", "range_key", "freq", "gain", "q", "slope")
)

_VOICE_CHANGER = (
    *_same("enabled"),
    Field("output_gain", "output_gain_db"),
    *_prefix("formant", (
        ("enabled", "enabled"), ("shift", "shift_st"), ("throat", "throat_pct"),
        ("gender", "gender_pct"), ("preserve", "preserve_pct"),
        ("timbre", "timbre_pct"), ("mix", "mix_pct"),
    ), "formant_"),
    _enum("formant.mode", "formant_mode", {"Natural": "natural", "Crisp": "crisp"}),
    *_prefix("pitch", (
        ("enabled", "enabled"), ("semitones", "semitones"), ("fine", "fine_cents"),
        ("formant_link", "formant_link_pct"), ("glide", "glide_ms"),
        ("jitter", "jitter_pct"), ("mix", "mix_pct"),
    ), "pitch_"),
    _enum("pitch.tracking", "pitch_tracking", {"Vocal": "vocal", "Wide": "wide"}),
    *_prefix("vocoder", (
        ("enabled", "enabled"), ("bands", "bands"), ("carrier_hz", "carrier_hz"),
        ("formant", "formant_st"), ("unvoiced", "unvoiced_pct"),
        ("release", "release_ms"), ("mix", "mix_pct"),
    ), "vocoder_"),
    _enum("vocoder.carrier", "vocoder_carrier", {
        "Saw": "saw", "Pulse": "pulse", "Choir": "choir", "Noise": "noise",
    }),
)

_SPECTRAL_MANGLER = (
    *_same("enabled"),
    _enum("routing", "routing", {
        "Ring → Grain → Dist": "ring_grain_dist",
        "Grain → Ring → Dist": "grain_ring_dist",
        "Dist → Grain → Ring": "dist_grain_ring",
        "Parallel Blend": "parallel",
    }),
    Field("global_mix", "global_mix_pct"),
    Field("output_gain", "output_gain_db"),
    Field("limiter", "output_limiter_enabled"),
    *_prefix("ring", (
        ("enabled", "enabled"), ("carrier", "carrier_hz"), ("lfo_rate", "lfo_rate_hz"),
        ("lfo_depth", "lfo_depth_pct"), ("warp", "warp_pct"),
        ("spread", "spread_pct"), ("mix", "mix_pct"),
    ), "ring_"),
    _enum("ring.mode", "ring_mode", {
        "Sine": "sine", "Dual": "dual", "FM-Ring": "fm_ring", "Rectify": "rectify",
    }),
    *_prefix("grain", (
        ("enabled", "enabled"), ("freeze", "freeze"), ("size", "size_ms"),
        ("density", "density_hz"), ("pitch", "pitch_st"), ("pitch_jitter", "pitch_jitter_pct"),
        ("position", "position_pct"), ("spray", "spray_pct"),
        ("feedback", "feedback_pct"), ("mix", "mix_pct"),
    ), "grain_"),
    _enum("grain.mode", "grain_mode", {"Cloud": "cloud", "Reverse": "reverse", "Scatter": "scatter"}),
    *_prefix("dist", (
        ("enabled", "enabled"), ("drive", "drive_db"), ("fold", "fold_pct"),
        ("bias", "bias_pct"), ("tone", "tone_hz"), ("resonance", "resonance_pct"),
        ("mix", "mix_pct"),
    ), "dist_"),
    _enum("dist.curve", "dist_curve", {
        "Tape": "tape", "Fold": "fold", "Hard Clip": "hard_clip", "BitCrush": "bitcrush",
    }),
)

_EXPANDER_BINDING = ("dynamics", (*_same("enabled"), *_prefix("eg", _EXPANDER)))

CLASSIC_BINDINGS: dict[str, tuple[str, tuple[Field, ...]]] = {
    "strip_eq": ("eq", (*_same("enabled", "voicing", "master_gain"), *_EQ_BANDS)),
    "expander": _EXPANDER_BINDING,
    "noise_gate": _EXPANDER_BINDING,
    "compressor": ("dynamics", (
        *_same("enabled"),
        *_prefix("comp", _COMPRESSOR),
        _enum("comp.sc_source", "sc_source", _SIDECHAIN_SOURCE),
        Field("makeup", "makeup_gain"),
    )),
    "limiter": ("dynamics", (*_same("enabled"), *_prefix("lim", _TIMING))),
    "dynamics": ("dynamics", (
        *_same("enabled", "makeup", "eg.enabled", "comp.enabled", "lim.enabled"),
        *_prefix("eg", _EXPANDER, "eg."),
        *_prefix("comp", _COMPRESSOR, "comp."),
        _enum("comp.sc_source", "comp.sc_source", _SIDECHAIN_SOURCE),
        *_prefix("lim", _TIMING, "lim."),
    )),
    "reverb": ("reverb", _same(
        "enabled", "height", "length", "width", "pre_delay", "reverb_time", "distance",
        "brightness", "modulation", "er_lo_freq", "er_hi_freq", "er_lo_gain", "er_hi_gain",
        "rev_lo_freq", "rev_hi_freq", "rev_lo_gain", "rev_hi_gain", "dry_wet", "direct_db",
        "er_db", "reverb_db",
    )),
    "delay": ("delay", (
        *_same(
            "enabled", "link_lr", "low_cut", "high_cut", "left_delay",
            "right_delay", "left_feedback", "right_feedback", "high_ratio", "stereo_blend",
            "dry_wet",
        ),
        Field("input_format", "input_format", write=_delay_format),
        Field("level", "level_db"),
    )),
    "saturation": ("saturation", _same("enabled", "drive")),
    "imager": ("stereo_imager", (
        *_same("enabled", "width", "pan", "mono_maker_on"),
        Field("solo_mode", "_imager_audition"),
        Field("mid_gain", "mid_gain_db"), Field("side_gain", "side_gain_db"),
        Field("output_gain", "output_gain_db"), Field("mono_maker_freq", "mono_maker_hz"),
        Field("mono_maker_slope", "mono_maker_slope", lambda v: v + "dB", lambda v: v.removesuffix("dB")),
    )),
    "spatial_panner": ("panner", (
        *_same(
            "enabled", "space_mode", "format", "fb", "elevation", "rotate", "spread",
            "spread_auto", "divergence", "div_mode", "boom_on", "boom_route", "boom_gain",
        ),
        Field("lr", "pan", read=_scale(100.0), write=_scale(0.01)),
    )),
    "voice_changer": ("voice_changer", _VOICE_CHANGER),
    "spectral_mangler": ("spectral_mangler", _SPECTRAL_MANGLER),
}
