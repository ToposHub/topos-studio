"""
Tests for Audio Effects registration in EffectRegistry.

P-clip-FX — clip-level mixer FX tests.

The 8 mixer audio effects (strip_eq / limiter / compressor / reverb / delay /
saturation / imager / noise_gate) reuse the AudioMixer CPU DSP and can be
dropped onto individual audio clips. These tests cover:
  - registration (8 effects, correct effect_type / category / icon)
  - basic params schema (only 2-4 "basic" sliders; full set lives in the dialog)
  - create_effect_instance seeds the FULL default params dict (from *Params)
  - the legacy placeholder effects (gain / eq / noise_reduction) are gone
"""

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.effect_registry import (
    get_effect_registry,
    create_effect_instance,
    AUDIO_MIXER_EFFECT_TYPES,
    STRIP_EQ_EFFECT,
    LIMITER_EFFECT,
    COMPRESSOR_EFFECT,
    REVERB_EFFECT,
    DELAY_EFFECT,
    SATURATION_EFFECT,
    IMAGER_EFFECT,
    EXPANDER_EFFECT,
    NOISE_GATE_EFFECT,
)


# effect_type → 期望出现在基础 params_schema 里的部分关键参数名
# CW-A1: 参数化 EQ / 声学混响 / 立体声延迟 / Expander（noise_gate 为其别名）
_EXPECTED_BASIC_PARAMS = {
    "strip_eq": {"master_gain"},
    "limiter": {"threshold_db", "release_ms"},
    "compressor": {"threshold_db", "ratio", "makeup_gain"},
    "reverb": {"dry_wet", "reverb_time"},
    "delay": {"left_delay", "right_delay", "dry_wet"},
    "saturation": {"drive"},
    "imager": {"width", "pan"},
    "expander": {"threshold_db", "range_db", "release_ms"},
    "noise_gate": {"threshold_db", "range_db", "release_ms"},
    # CW-A1: Dynamics 完整单元——基础区只暴露 Make Up（细项在完整面板编辑）
    "dynamics": {"makeup"},
    # NF-01：两款新复合 FX——基础区暴露核心身份参数（细项在完整面板）
    "voice_changer": {"pitch_semitones", "formant_shift_st", "vocoder_carrier"},
    "spectral_mangler": {"routing", "global_mix_pct", "dist_curve"},
    # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX 基础参数键（≤5 个，
    # 完整参数集在 mixer 通用参数对话框）
    "de_esser": {"threshold_dbfs", "ratio", "frequency", "max_reduction_db"},
    "true_peak_limiter": {"ceiling_dbtp", "lookahead_ms", "release_ms"},
    "dynamic_eq": {"frequency", "q", "gain_db", "threshold_dbfs", "max_cut_db"},
    "bus_comp": {"template", "sc_hpf_hz"},
    "multiband_comp": {"crossover_low", "crossover_high", "attack_ms"},
    "convolution_reverb": {"wet", "dry"},
    "transient_designer": {"attack_db", "sustain_db", "attack_ms", "sustain_ms"},
    "tremolo_auto_pan": {"rate_hz", "depth"},
    "chorus_flanger": {"rate_hz", "depth_ms", "feedback", "mix"},
    "phaser": {"rate_hz", "stages", "base_hz", "sweep_octaves", "mix"},
    "exciter": {"ex_crossover_hz", "ex_oversampling", "ex_wet", "ex_output_gain_db"},
    "subharmonic": {"sh_source_center_hz", "sh_octave1_gain_db", "sh_octave2_gain_db", "sh_mix"},
}

_ALL_DEFINITIONS = {
    "strip_eq": STRIP_EQ_EFFECT,
    "limiter": LIMITER_EFFECT,
    "compressor": COMPRESSOR_EFFECT,
    "reverb": REVERB_EFFECT,
    "delay": DELAY_EFFECT,
    "saturation": SATURATION_EFFECT,
    "imager": IMAGER_EFFECT,
    "expander": EXPANDER_EFFECT,
    "noise_gate": NOISE_GATE_EFFECT,
}


class TestAudioEffectDefinitions:
    """Test the 8 mixer audio effect definitions are properly registered."""

    @pytest.mark.parametrize("effect_type", sorted(AUDIO_MIXER_EFFECT_TYPES))
    def test_effect_registered(self, effect_type):
        """Each mixer effect_type is registered with category=audio."""
        registry = get_effect_registry()
        defn = registry.get_definition(effect_type)
        assert defn is not None, f"{effect_type} not registered"
        assert defn.category == "audio"
        assert defn.name, f"{effect_type} has empty name"
        assert defn.icon == "audio_track"

    @pytest.mark.parametrize("effect_type", sorted(AUDIO_MIXER_EFFECT_TYPES))
    def test_basic_params_schema(self, effect_type):
        """Each effect exposes the expected basic params (for inline sliders)."""
        registry = get_effect_registry()
        defn = registry.get_definition(effect_type)
        expected = _EXPECTED_BASIC_PARAMS[effect_type]
        actual = set(defn.params_schema.keys())
        missing = expected - actual
        assert not missing, f"{effect_type} missing basic params: {missing}"
        # 基础参数应保持精简（2-4 个），完整参数集在独立 dialog 里
        assert len(actual) <= 5, (
            f"{effect_type} exposes too many basic params ({len(actual)}); "
            "full set should live in the standalone dialog"
        )

    def test_audio_category_exists(self):
        """The 'audio' category is registered."""
        registry = get_effect_registry()
        assert "audio" in registry.get_categories()

    def test_all_audio_effects(self):
        """CW-A1: mixer 音频效果（8 基础 + noise_gate 别名）+ 2 个完整单元
        （dynamics / spatial_panner，拖放路由特殊处理）注册在 audio 分类。"""
        registry = get_effect_registry()
        audio_effects = registry.get_effects_by_category("audio")
        effect_types = {e.effect_type for e in audio_effects}
        assert effect_types == set(AUDIO_MIXER_EFFECT_TYPES) | {
            "spatial_panner",
        }
        assert len(audio_effects) == len(AUDIO_MIXER_EFFECT_TYPES) + 1


class TestLegacyPlaceholdersRemoved:
    """The old placeholder audio effects (never wired to DSP) are gone."""

    @pytest.mark.parametrize("legacy", ["gain", "eq", "noise_reduction"])
    def test_legacy_not_registered(self, legacy):
        registry = get_effect_registry()
        assert registry.get_definition(legacy) is None, (
            f"legacy placeholder '{legacy}' should be removed"
        )


class TestCreateEffectInstanceFullDefaults:
    """create_effect_instance seeds the FULL default params dict for audio FX.

    The basic params_schema only declares 2-4 sliders, but the DSP consumes a
    complete dict — create_effect_instance must seed from *Params.default()
    so the DSP receives every field.
    """

    @pytest.mark.parametrize("effect_type", sorted(AUDIO_MIXER_EFFECT_TYPES))
    def test_instance_has_full_defaults(self, effect_type):
        instance = create_effect_instance(effect_type)
        assert instance is not None
        assert instance.effect_type == effect_type
        # 至少包含基础参数之外的字段（证明是完整 dict 而非仅 schema 子集）
        basic = _EXPECTED_BASIC_PARAMS[effect_type]
        extra = set(instance.params.keys()) - basic
        assert len(extra) >= 1, (
            f"{effect_type} instance params should include advanced fields "
            f"beyond basic schema; got only {set(instance.params.keys())}"
        )
        # 所有 mixer 效果都带 enabled 字段（DSP 旁路标志）
        assert "enabled" in instance.params

    def test_nested_defaults_are_independent_per_effect_instance(self):
        first = create_effect_instance("strip_eq")
        second = create_effect_instance("strip_eq")
        first.params["bands"][0]["gain"] = 11.0
        assert second.params["bands"][0]["gain"] != 11.0
        assert create_effect_instance("strip_eq").params["bands"][0]["gain"] != 11.0

    def test_reverb_full_defaults(self):
        instance = create_effect_instance("reverb")
        # CW-A1: 声学房间模型完整键集
        assert set(instance.params.keys()) == {
            "height", "length", "width", "pre_delay", "reverb_time",
            "distance", "brightness", "modulation",
            "er_lo_freq", "er_hi_freq", "er_lo_gain", "er_hi_gain",
            "rev_lo_freq", "rev_hi_freq", "rev_lo_gain", "rev_hi_gain",
            "dry_wet", "direct_db", "er_db", "reverb_db", "enabled",
        }
        assert instance.params["reverb_time"] == 2730.0
        assert instance.params["dry_wet"] == 100.0

    def test_compressor_full_defaults(self):
        instance = create_effect_instance("compressor")
        assert set(instance.params.keys()) == {
            "threshold_db", "ratio", "attack_ms", "hold_ms", "release_ms",
            "knee", "mix", "makeup_gain", "sc_on", "sc_listen",
            "sc_source", "enabled",
        }


class TestAudioEffectParameterUpdate:
    """Basic param updates via with_param still work for mixer audio effects."""

    def test_compressor_threshold_update(self):
        instance = create_effect_instance("compressor")
        updated = instance.with_param("threshold_db", -30.0)
        assert updated.params["threshold_db"] == -30.0
        # 其它字段保留
        assert updated.params["ratio"] == 2.5

    def test_reverb_dry_wet_update(self):
        instance = create_effect_instance("reverb")
        updated = instance.with_param("dry_wet", 75.0)
        assert updated.params["dry_wet"] == 75.0
        assert updated.params["reverb_time"] == 2730.0


class TestAudioEffectSearch:
    """Searching finds the mixer audio effects."""

    @pytest.mark.parametrize("query,expected_type", [
        ("compressor", "compressor"),
        ("reverb", "reverb"),
        ("delay", "delay"),
        ("equalizer", "strip_eq"),
    ])
    def test_search_finds_effect(self, query, expected_type):
        registry = get_effect_registry()
        results = registry.search(query)
        assert any(e.effect_type == expected_type for e in results), (
            f"search '{query}' should find {expected_type}"
        )
