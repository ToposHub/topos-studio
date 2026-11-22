"""Production mappings for the versioned, opt-in multiband processors."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .bindings_core import Field, collect_fields, project_fields

DYNAMIC_FIELDS = (
    ("enabled", "enabled"), ("freq", "frequency_hz"), ("gain", "static_gain_db"),
    ("q", "q"), ("threshold", "threshold_dbfs"), ("attack", "attack_ms"),
    ("release", "release_ms"), ("dyn_enabled", "dynamic_enabled"),
    ("max_boost", "max_boost_db"), ("threshold_slope", "slope"),
)
SAT_FIELDS = (
    ("enabled", "enabled"), ("bias", "bias_pct"), ("level", "level_db"),
)


def sidechain_fields() -> tuple[Field, ...]:
    return (
        Field("sidechain.hpf_freq", "sc_hpf_hz"), Field("sidechain.lpf_freq", "sc_lpf_hz"),
        Field("sidechain.slope", "sc_filter_slope_db_oct"), Field("sidechain.listen", "sc_listen"),
    )


def extended_fields(effect_type: str, params: dict[str, Any]) -> tuple[Field, ...] | None:
    if effect_type == "transient_designer" and ("attack_pct" in params or "sustain_pct" in params):
        return (
            Field("transient.attack", "attack_pct"), Field("transient.sustain", "sustain_pct"),
            Field("transient.attack_ms", "attack_ms"), Field("transient.release", "sustain_ms"),
            Field("transient.mix", "mix", lambda v: v * 100.0, lambda v: v / 100.0),
            Field("output_gain", "output_gain_db"),
            *sidechain_fields(),
        )
    if effect_type == "dynamic_eq" and params.get("bands"):
        return (Field("ext_sc_source", "sidechain_source_id"),) + tuple(field for i in range(min(6, len(params["bands"]))) for field in (
            *(Field(f"bands.{i}.{ui}", f"bands.{i}.{engine}") for ui, engine in DYNAMIC_FIELDS),
            Field(f"bands.{i}.dyn_range", f"bands.{i}.max_cut_db", lambda v: -v, lambda v: -v),
        ))
    if effect_type == "multiband_comp":
        count = len(params["crossovers"]) + 1
        names = (("enabled", "enabled"), ("mute", "mute"), ("threshold", "threshold_db"),
                 ("ratio", "ratio"), ("knee", "knee"), ("attack", "attack_ms"),
                 ("release", "release_ms"), ("makeup", "makeup_gain"))
        return tuple(Field(f"crossovers.{i}", f"crossovers.{i}") for i in range(count - 1)) + tuple(
            Field(f"bands.{i}.{ui}", f"bands.{i}.{engine}") for i in range(count) for ui, engine in names)
    if effect_type == "saturation" and params.get("model_profile") == "multiband":
        return (
            Field("xover_low", "mb_xover_low_hz"), Field("xover_high", "mb_xover_high_hz"),
            Field("oversampling", "mb_oversampling", lambda v: f"{v}x", lambda v: int(v[:-1])),
            Field("mix", "mb_mix", lambda v: v * 100.0, lambda v: v / 100.0),
            Field("output_gain", "mb_output_gain_db"),
            *(field for i in range(3) for field in (
                *(Field(f"bands.{i}.{ui}", f"mb_bands.{i}.{engine}") for ui, engine in SAT_FIELDS),
                Field(f"bands.{i}.model", f"mb_bands.{i}.model", str.title, str.lower),
                Field(f"bands.{i}.drive", f"mb_bands.{i}.drive_db", lambda v: v / 3.0, lambda v: v * 3.0),
                Field(f"bands.{i}.tone", f"mb_bands.{i}.tone_db", lambda v: v / 0.12, lambda v: v * 0.12),
            )),
        )
    return None


def display_projection(effect_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if effect_type == "transient_designer":
        return {"sc_hpf_hz": 0.0, "sc_lpf_hz": 0.0, "sc_filter_slope_db_oct": 12,
                "sc_listen": False, "sidechain_source_id": "internal", "attack_pct": 0.0,
                "sustain_pct": 0.0} | params if ("attack_pct" in params or "sustain_pct" in params) else params
    if effect_type == "dynamic_eq":
        return {"sidechain_source_id": "internal"} | params
    if effect_type == "bus_comp":
        from src.features.edit.core.audio_effects.bus_multiband import BUS_COMP_TEMPLATES
        defaults = deepcopy(BUS_COMP_TEMPLATES[params["template"]])
        defaults["makeup_gain"] = defaults.pop("makeup_db", 0.0)
        return defaults | params
    if effect_type == "de_esser":
        return {"detector_shape": "bandpass", "lookahead_ms": 0.0, "output_gain_db": 0.0} | params
    return params


def project_extended(effect_type: str, state: dict[str, Any], params: dict[str, Any]) -> bool:
    fields = extended_fields(effect_type, params)
    if fields is None:
        return False
    project_fields(state, params, fields)
    if effect_type == "multiband_comp":
        state["band_count"] = len(params["crossovers"]) + 1
    elif effect_type == "dynamic_eq":
        for i, band in enumerate(state["bands"]):
            band.update(filter_type="Bell", range_key="All", auto_timing=False, trigger_mode="INT")
            if i >= len(params["bands"]):
                band["enabled"] = False
    return True


def collect_extended(effect_type: str, state: dict[str, Any], baseline: dict[str, Any],
                     params: dict[str, Any]) -> set[str] | None:
    fields = extended_fields(effect_type, params)
    if fields is None:
        return None
    if effect_type == "multiband_comp" and state["band_count"] != baseline["band_count"]:
        count = state["band_count"]
        params["crossovers"] = list(state["crossovers"][:count - 1])
        bands = list(params["bands"])
        while len(bands) < count:
            bands.append({"enabled": False, "threshold_db": -12.0, "ratio": 1.5,
                          "attack_ms": 10.0, "release_ms": 100.0, "knee": 0.0, "makeup_gain": 0.0})
        params["bands"] = bands[:count]
        fields = extended_fields(effect_type, params)
        changed = {"crossovers", "bands"}
    else:
        changed = set()
    return changed | collect_fields(state, baseline, params, fields)
