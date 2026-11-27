"""Parameter translations for the rack effects that already process audio."""
from __future__ import annotations

from math import log10
from pathlib import Path
from typing import Any

from .bindings_core import Field, SKIP
from .production_extensions import sidechain_fields


def _percent(fraction: float) -> float:
    return fraction * 100.0


def _fraction(percent: float) -> float:
    return percent / 100.0


def _feedback_fraction(percent: float) -> Any:
    # Signed feedback is consumed by both production modulation processors.
    return _fraction(percent) if -95.0 <= percent <= 95.0 else SKIP


def _attack_percent(db: float) -> float:
    return db / (0.12 if db < 0.0 else 0.24)


def _attack_db(percent: float) -> float:
    return percent * (0.12 if percent < 0.0 else 0.24)


def _sustain_percent(db: float) -> float:
    return db / (0.24 if db < 0.0 else 0.12)


def _sustain_db(percent: float) -> float:
    return percent * (0.24 if percent < 0.0 else 0.12)


def _sweep_percent(octaves: float) -> float:
    return 100.0 * (octaves - 0.5) / 4.5


def _sweep_octaves(percent: float) -> float:
    return 0.5 + 4.5 * _fraction(percent)


def _percent_depth(depth_ms: float) -> float:
    return depth_ms * 10.0


def _depth_ms(percent: float) -> float:
    return percent / 10.0


def _stages(stages: int) -> int:
    return max(1, min(12, stages))


def _negate(value: float) -> float:
    return -value


def _bus_template_to_panel(template: str) -> str:
    return "API 2500" if template == "mx_glue" else "SSL G-Glue"


def _bus_template_to_engine(topology: str) -> str:
    return "mx_glue" if topology == "API 2500" else "dx_glue"


def _deesser_mode_to_panel(mode: str) -> str:
    return "sidechain" if mode == "wide" else "split"


def _deesser_mode_to_engine(mode: str) -> str:
    return "wide" if mode == "sidechain" else "split"


def _tremolo_mode_to_panel(mode: str) -> str:
    return {"auto_pan": "Auto-Pan", "dual": "Dual"}.get(mode, "Tremolo")


def _tremolo_mode_to_engine(mode: str) -> Any:
    return {"Auto-Pan": "auto_pan", "Tremolo": "tremolo", "Dual": "dual"}.get(mode, SKIP)


def _select_wave_file(value: str) -> Any:
    if not value:
        return ""
    # The drawing uses synthetic built-ins and sweep filenames. Only a chosen
    # PCM WAV can go through the existing production file loader.
    selected = Path(value)
    if selected.is_absolute() and selected.suffix.lower() == ".wav" and selected.is_file():
        return value
    return SKIP


def _is_chorus(params: dict[str, Any]) -> bool:
    return params.get("mode", "chorus") == "chorus"


def _is_flanger(params: dict[str, Any]) -> bool:
    return params.get("mode") == "flanger"


_MULTIBAND_FIELDS = (
    ("enabled", "enabled"),
    ("threshold", "threshold_db"),
    ("ratio", "ratio"),
    ("knee", "knee"),
    ("attack", "attack_ms"),
    ("release", "release_ms"),
    ("makeup", "makeup_gain"),
)

RACK_BINDINGS: dict[str, tuple[str, tuple[Field, ...]]] = {
    "exciter": ("exciter", (
        Field("quality", "ex_quality"),
        Field("crossovers.0", "ex_crossover_hz"),
        Field("global_mix", "ex_wet", _percent, _fraction),
        Field("output_gain", "ex_output_gain_db"),
        Field("oversampling", "ex_oversampling", lambda v: "2x" if v == 2 else "1x Off",
              lambda v: 2 if v == "2x" else 1),
        *(field for i in range(2) for field in (
            Field(f"bands.{i}.enabled", f"ex_bands.{i}.enabled"),
            Field(f"bands.{i}.mode", f"ex_bands.{i}.mode", str.title, str.lower),
            Field(f"bands.{i}.amount", f"ex_bands.{i}.amount", _percent, _fraction),
            Field(f"bands.{i}.mix", f"ex_bands.{i}.mix", _percent, _fraction),
        )),
    )),
    "subharmonic": ("subharmonic", (
        Field("detection_mode", "sh_detection_mode"),
        Field("source.freq", "sh_source_center_hz"),
        Field("source.bandwidth", "sh_source_bandwidth_hz"),
        Field("source.oct1_level", "sh_octave1_gain_db", lambda v: 100.0 * 10.0 ** (v / 20.0),
              lambda v: 20.0 * log10(max(v / 100.0, 1e-6))),
        Field("source.oct2_level", "sh_octave2_gain_db", lambda v: 100.0 * 10.0 ** (v / 20.0),
              lambda v: 20.0 * log10(max(v / 100.0, 1e-6))),
        Field("source.sub_lpf", "sh_sub_lpf_hz"),
        Field("drive.attack", "sh_attack_ms"), Field("drive.release", "sh_release_ms"),
        Field("output.mix", "sh_mix", _percent, _fraction),
        Field("output.trim_db", "sh_output_gain_db"),
    )),
    "de_esser": ("deesser", (
        Field("freq", "frequency"),
        Field("q", "q"),
        Field("threshold", "threshold_dbfs"),
        Field("range", "max_reduction_db"),
        Field("ratio", "ratio"),
        Field("attack", "attack_ms"),
        Field("release", "release_ms"),
        Field("listen", "listen"),
        Field("mode", "mode", _deesser_mode_to_panel, _deesser_mode_to_engine),
        Field("filter_shape", "detector_shape", lambda v: "High Shelf" if v == "high_shelf" else "Bandpass",
              lambda v: "high_shelf" if v == "High Shelf" else "bandpass"),
        Field("lookahead", "lookahead_ms"),
        Field("output_gain", "output_gain_db"),
    )),
    "true_peak_limiter": ("true_peak_limiter", (
        Field("ceiling", "ceiling_dbtp"),
        Field("engine.lookahead", "lookahead_ms"),
        Field("engine.release", "release_ms"),
        Field("engine.linked", "linked"),
    )),
    "dynamic_eq": ("dynamic_eq", (
        Field("ext_sc_source", "sidechain_source_id"),
        # A legacy production instance is one bell. The fourth slot is the panel's
        # presence band and remains the stable target even when another is selected.
        Field("bands.3.freq", "frequency"),
        Field("bands.3.q", "q"),
        Field("bands.3.gain", "gain_db"),
        Field("bands.3.threshold", "threshold_dbfs"),
        Field("bands.3.attack", "attack_ms"),
        Field("bands.3.release", "release_ms"),
        Field("bands.3.dyn_enabled", "dynamic_enabled"),
        Field("bands.3.dyn_range", "max_cut_db", _negate, _negate),
        Field("bands.3.max_boost", "max_boost_db"),
        Field("bands.3.threshold_slope", "slope"),
    )),
    "bus_comp": ("bus_compressor", (
        Field("topology", "template", _bus_template_to_panel, _bus_template_to_engine),
        Field("sc_hpf", "sc_hpf_hz"),
        Field("threshold", "threshold_db"), Field("ratio", "ratio"),
        Field("attack", "attack_ms"),
        Field("release", "release_ms", lambda v: v / 1000.0, lambda v: v * 1000.0),
        Field("makeup", "makeup_gain"), Field("mix", "mix"),
        Field("processor_knee", "knee", lambda v: v * 0.24, lambda v: v / 0.24),
    )),
    "multiband_comp": ("multiband_compressor", (
        Field("crossovers.0", "crossovers.0"),
        Field("crossovers.1", "crossovers.1"),
        *(
            Field(f"bands.{index}.{panel_key}", f"bands.{index}.{engine_key}")
            for index in range(3)
            for panel_key, engine_key in _MULTIBAND_FIELDS
        ),
    )),
    "convolution_reverb": ("convolution_reverb", (
        Field("file_path", "ir_path", write=_select_wave_file),
        Field("dry_wet", "wet", _percent, _fraction),
        Field("dry_level", "dry", _percent, _fraction),
        Field("true_stereo", "true_stereo"),
    )),
    "transient_designer": ("transient_designer", (
        Field("transient.attack", "attack_db", _attack_percent, _attack_db),
        Field("transient.sustain", "sustain_db", _sustain_percent, _sustain_db),
        Field("transient.attack_ms", "attack_ms"),
        Field("transient.release", "sustain_ms"),
        Field("transient.mix", "mix", _percent, _fraction), Field("output_gain", "output_gain_db"),
        *sidechain_fields(),
    )),
    "tremolo_auto_pan": ("tremolo_autopan", (
        Field("mode", "mode", _tremolo_mode_to_panel, _tremolo_mode_to_engine),
        Field("rate_hz", "rate_hz"),
        Field("depth", "depth", _percent, _fraction),
        Field("sync", "sync"), Field("bpm", "bpm"), Field("sync_div", "sync_div"),
        Field("shape", "duty_cycle_pct"), Field("smooth", "smooth_ms"),
        Field("pan_center", "pan_center_pct"), Field("pan_width", "pan_width_pct"),
        Field("phase_offset", "phase_offset_deg"),
        Field("waveform", "waveform", lambda v: v.replace("_", " ").title() or "Sine",
              lambda v: v.lower().replace(" ", "_")),
    )),
    "chorus_flanger": ("modulation", (
        Field("mode", "mode"),
        Field("chorus.rate", "rate_hz", when=_is_chorus),
        Field("chorus.depth", "depth_ms", _percent_depth, _depth_ms, when=_is_chorus),
        Field("chorus.delay_ms", "base_ms", when=_is_chorus),
        Field("chorus.feedback", "feedback", _percent, _feedback_fraction, when=_is_chorus),
        Field("chorus.voices", "voices", when=_is_chorus),
        Field("chorus.mix", "mix", _percent, _fraction, when=_is_chorus),
        Field("flanger.rate", "rate_hz", when=_is_flanger),
        Field("flanger.depth", "depth_ms", _percent_depth, _depth_ms, when=_is_flanger),
        Field("flanger.manual_ms", "base_ms", when=_is_flanger),
        Field("flanger.feedback", "feedback", _percent, _feedback_fraction, when=_is_flanger),
        Field("flanger.voices", "voices", when=_is_flanger),
        Field("flanger.mix", "mix", _percent, _fraction, when=_is_flanger),
    )),
    "phaser": ("modulation", (
        Field("phaser.rate", "rate_hz"),
        Field("phaser.stages", "stages", _stages),
        Field("phaser.depth", "sweep_octaves", _sweep_percent, _sweep_octaves),
        Field("phaser.center_hz", "base_hz"),
        Field("phaser.feedback", "feedback", _percent, _feedback_fraction),
        Field("phaser.mix", "mix", _percent, _fraction),
        Field("phaser.stereo_processing", "stereo_processing"),
    )),
}
