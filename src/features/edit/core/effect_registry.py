"""
Effect Registry Module - Registry of available effects.

This module defines all built-in effects available in Topos Color.
Effects are registered with their definitions, including parameter schemas.

Built-in Effects:
- Transform: Position, scale, rotation (already in ClipParams, effect version)
- Crop: Left, right, top, bottom crop (already in ClipParams)
- Gaussian Blur: Blur intensity
- Sharpen: Sharpen intensity
- Opacity: Opacity control (0-100%)
- Vignette: Vignette effect

Categories:
- transform: Transform, Crop, Opacity
- blur: Gaussian Blur
- sharpen: Sharpen
- stylize: Vignette

Reference:
- PLUGIN_DEV_GUIDE.md for GLSL shader integration
- docs/edit_page_timeline_architecture.md
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import logging
import threading

# AUD-16807：EffectInstance 顶层导入。effect_instance 只依赖 src.shared，
# 与本模块无环（模块底部的延迟 import 是历史遗留，只会让单例/缓存的
# 初始化时序更难推断）。
from src.features.edit.core.effect_instance import (
    EffectDefinition,
    EffectInstance,
    ParamSchema,
)

logger = logging.getLogger(__name__)


# ================================================================
# EffectSpec → EffectDefinition adapter
#
# The unified EffectCatalog (src/features/shared/effects/) holds the
# renderer-agnostic description of every video filter — the same spec
# the composite page and the renderers use. The Edit UI, however, was
# originally written against EffectDefinition / ParamSchema, so we bridge
# the two here: each EffectSpec is converted into an EffectDefinition on
# registry build, letting the panel and the renderer share one source of
# truth without a panel rewrite (Phase 2 will let the panel read ParamDef
# directly).
# ================================================================

# ParamDef.ptype → ParamSchema.type.
#
# ParamDef supports a richer type set than ParamSchema. The mappings that
# lose information (vec2/vec3 → string, channel → choice) are acceptable
# for Phase 1: those param types don't appear in the MVP effects (blur /
# sharpen). Phase 2 replaces this table entirely by having the panel read
# ParamDef directly.
_PARAM_TYPE_MAP: Dict[str, str] = {
    "float": "float",
    "int": "int",
    "bool": "bool",
    "color": "color",
    "option": "choice",
    "str": "string",
    # Best-effort downgrades for types ParamSchema can't represent.
    "vec2": "string",
    "vec3": "string",
    "channel": "choice",
    "file": "string",
    "hdri": "string",
}


def _paramdef_to_schema(param_def) -> ParamSchema:
    """Convert a composite ``ParamDef`` to an Edit ``ParamSchema``.

    Lossy for non-MVP types (see ``_PARAM_TYPE_MAP``); raises never —
    unknown ptypes fall back to ``float`` so a corrupt spec can't break
    the panel.
    """
    ptype = _PARAM_TYPE_MAP.get(param_def.ptype, "float")
    choices = tuple(param_def.labels) if param_def.labels else ()
    # ParamSchema.__post_init__ requires choices for the 'choice' type.
    if ptype == "choice" and not choices:
        ptype = "string"
    return ParamSchema(
        type=ptype,
        default=param_def.default,
        min_value=float(param_def.min_val),
        max_value=float(param_def.max_val),
        step=float(param_def.step),
        label=param_def.name.replace("_", " ").title(),
        choices=choices,
        unit="",
    )


def _spec_to_definition(spec) -> EffectDefinition:
    """Convert an :class:`EffectSpec` to an :class:`EffectDefinition`.

    Hidden params are included so that ``create_instance()`` seeds their
    defaults (shaders may reference them); the panel hides them via the
    hidden flag at the ParamDef level when it reads the spec directly in
    Phase 2.
    """
    params_schema = {
        pdef.name: _paramdef_to_schema(pdef)
        for pdef in spec.param_defs
    }
    return EffectDefinition(
        effect_type=spec.effect_type,
        name=spec.name,
        category=spec.category,
        description=spec.description,
        params_schema=params_schema,
        icon=spec.icon,
    )


# ================================================================
# Built-in Effect Definitions
# ================================================================

# T-P1-20（审计 P6-step-1 落地）：transform / crop / opacity / vignette 的
# legacy FX 定义已从注册表移除——它们从未接任何渲染后端（attach 后零视觉
# 效果），真实能力一直以 clip-native 形态存在（TransformParams /
# CropParams / ClipInstance.opacity）。NON_ATTACHABLE_VIDEO_EFFECT_TYPES
# 保留为旧工程数据防线；EffectInstance 反序列化时 ghost 实例会被迁移剔除
# （见 effect_instance.from_dict）。

# ================================================================
# Audio Effect Definitions (clip-level mixer FX)
#
# These reuse the 8 CPU-DSP effects from the Audio Mixer
# (src/features/edit/core/audio_dsp.py: _EFFECT_CLASS_REGISTRY) so they
# can be dropped onto individual audio clips and processed per-clip.
#
# Each definition only declares 2-4 "basic" params for the inline slider
# section; the full parameter set lives in the standalone dialog opened
# via the "打开面板" button (clip_audio_effect_dialog_adapter.py).
# Default params for create_instance() are seeded from the corresponding
# *Params.default().to_dict() (see _AUDIO_DEFAULT_PARAMS below) so the
# DSP always receives a complete dict.
# ================================================================

# Map mixer effect_type → factory returning the full default params dict.
# Lazily built from timeline_v2 *Params to avoid a hard import cycle here.
_AUDIO_DEFAULT_PARAMS: Optional[dict] = None
# AUD-16807：惰性全局缓存与单例的初始化锁——check-then-act 必须原子，
# 否则并发首调可能重复构建 dict / 单例（EffectRegistry 构造含 catalog
# 注册，重复构造既浪费又可能让两次调用拿到不同实例）。
_AUDIO_DEFAULT_PARAMS_LOCK = threading.Lock()


def _get_audio_default_params() -> dict:
    """Return the full default params dict for each mixer audio effect."""
    global _AUDIO_DEFAULT_PARAMS
    if _AUDIO_DEFAULT_PARAMS is None:
        with _AUDIO_DEFAULT_PARAMS_LOCK:
            if _AUDIO_DEFAULT_PARAMS is None:
                from src.features.edit.core.timeline_v2 import (
        _ensure_effect_param_registry,
                    StripEQParams, LimiterParams, CompressorParams, ReverbParams,
                    DelayParams, SaturationParams, ImagerParams, ExpanderParams,
                    DynamicsParams, VoiceChangerParams, SpectralManglerParams,
                )
                # CW-A1: Expander/Gate 接替 Noise Gate；noise_gate 键保持注册
                #（旧数据无损承接为 Gate），新增 expander 类型。
                gate_default = {**ExpanderParams.default().to_dict(), "mode": "Gate"}
                _AUDIO_DEFAULT_PARAMS = {
                    "strip_eq": StripEQParams.default().to_dict(),
                    "limiter": LimiterParams.default().to_dict(),
                    "compressor": CompressorParams.default().to_dict(),
                    "reverb": ReverbParams.default().to_dict(),
                    "delay": DelayParams.default().to_dict(),
                    "saturation": SaturationParams.default().to_dict(),
                    "imager": ImagerParams.default().to_dict(),
                    "expander": ExpanderParams.default().to_dict(),
                    "noise_gate": gate_default,
                    "dynamics": DynamicsParams.default().to_dict(),
                    # NF-01：两款新复合 FX 默认参数（新建 clip/Mixer 同源）
                    "voice_changer": VoiceChangerParams.default().to_dict(),
                    "spectral_mangler": SpectralManglerParams.default().to_dict(),
                    # P1（2026-11-19 计划第三阶段）：新 FX 默认参数
                    "de_esser": DeEsserParams.default().to_dict(),
                    "true_peak_limiter": TruePeakLimiterParams.default().to_dict(),
                    "dynamic_eq": DynamicEqParams.default().to_dict(),
                    "bus_comp": BusCompParams.default().to_dict(),
                    "multiband_comp": MultibandCompParams.default().to_dict(),
                    "convolution_reverb": ConvolutionReverbParams.default().to_dict(),
                    "transient_designer": TransientDesignerParams.default().to_dict(),
                    "tremolo_auto_pan": TremoloAutoPanParams.default().to_dict(),
                    "chorus_flanger": ChorusFlangerParams.default().to_dict(),
                    "phaser": PhaserParams.default().to_dict(),
                }
    return _AUDIO_DEFAULT_PARAMS


# Set of mixer audio effect_types — used by the panel to show the
# "打开面板" button and by the DSP layer to filter clip.effects.
AUDIO_MIXER_EFFECT_TYPES = frozenset({
    "strip_eq", "limiter", "compressor", "reverb",
    "delay", "saturation", "imager", "expander", "noise_gate",
    "dynamics",  # CW-A1: Dynamics 完整单元（EG→Comp→Lim 单实例复合）
    # NF-01（2026-11-15 计划 §6.3）：两款新复合 FX
    "voice_changer", "spectral_mangler",
    # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX
    "de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
    "multiband_comp", "convolution_reverb", "transient_designer",
    "tremolo_auto_pan", "chorus_flanger", "phaser",
})


# Explicit ordering of video categories for the Effects panel.
#
# Without this, get_categories() returns dict-insertion order — a fragile
# implicit contract that silently flips whenever registration order
# changes (e.g. a new builtin is added before the catalog loop). The
# panel reads this list to lay out its tree top-to-bottom, so a stable,
# intentional order keeps the UI predictable. "audio" is listed last
# (it has its own tab but the order also governs Favorites grouping);
# unknown categories fall after the known ones, sorted alphabetically.
_CATEGORY_ORDER: Tuple[str, ...] = (
    "transform",  # Transform / Crop / Opacity — geometric, most used
    "filter",     # Filter — Blur / Sharpen / Defocus / Denoise / Edge Detect / Erode·Dilate
    "stylize",    # Stylize — Vignette / Glow
    "generator",  # Generators — Adjustment Clip / Solid Color (no-source clip templates)
    "plugin",     # Plugins — DCTL (.tglsl) plugins
    "audio",      # Audio — 8 mixer FX (own tab, listed last)
)


STRIP_EQ_EFFECT = EffectDefinition(
    effect_type="strip_eq",
    name="Parametric EQ",
    category="audio",
    description="6-band parametric equalizer (open panel for full curve)",
    # 频段形状复杂（每段形/频/增益/Q/斜率），基础区只暴露整体增益；
    # 完整编辑在 ParametricEQDialog
    params_schema={
        "master_gain": ParamSchema(
            type="float",
            default=0.0,
            min_value=-20.0,
            max_value=20.0,
            step=0.1,
            label="Master Gain",
            unit="dB",
        ),
    },
    icon="audio_track",
)

LIMITER_EFFECT = EffectDefinition(
    effect_type="limiter",
    name="Limiter",
    category="audio",
    description="Brick-wall limiter to prevent clipping",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-6.0,
            min_value=-30.0,
            max_value=0.0,
            step=0.1,
            label="Threshold",
            unit="dB",
        ),
        "release_ms": ParamSchema(
            type="float",
            default=93.0,
            min_value=50.0,
            max_value=4000.0,
            step=1.0,
            label="Release",
            unit="ms",
        ),
    },
    icon="audio_track",
)

COMPRESSOR_EFFECT = EffectDefinition(
    effect_type="compressor",
    name="Compressor",
    category="audio",
    description="Dynamic range compressor with sidechain",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-15.0,
            min_value=-60.0,
            max_value=0.0,
            step=0.5,
            label="Threshold",
            unit="dB",
        ),
        "ratio": ParamSchema(
            type="float",
            default=2.5,
            min_value=1.2,
            max_value=20.0,
            step=0.1,
            label="Ratio",
            unit=":1",
        ),
        "makeup_gain": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=24.0,
            step=0.1,
            label="Makeup",
            unit="dB",
        ),
    },
    icon="audio_track",
)

REVERB_EFFECT = EffectDefinition(
    effect_type="reverb",
    name="Acoustic Reverb",
    category="audio",
    description="Acoustic room reverb (direct / ER / late FDN)",
    params_schema={
        "dry_wet": ParamSchema(
            type="float",
            default=100.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Dry/Wet",
            unit="%",
        ),
        "reverb_time": ParamSchema(
            type="float",
            default=2730.0,
            min_value=100.0,
            max_value=4000.0,
            step=10.0,
            label="Reverb Time",
            unit="ms",
        ),
    },
    icon="audio_track",
)

DELAY_EFFECT = EffectDefinition(
    effect_type="delay",
    name="Stereo Delay",
    category="audio",
    description="Dual-line stereo delay with cross feedback",
    params_schema={
        "left_delay": ParamSchema(
            type="float",
            default=121.0,
            min_value=0.0,
            max_value=1200.0,
            step=1.0,
            label="L Time",
            unit="ms",
        ),
        "right_delay": ParamSchema(
            type="float",
            default=135.0,
            min_value=0.0,
            max_value=1200.0,
            step=1.0,
            label="R Time",
            unit="ms",
        ),
        "dry_wet": ParamSchema(
            type="float",
            default=30.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Dry/Wet",
            unit="%",
        ),
    },
    icon="audio_track",
)

SATURATION_EFFECT = EffectDefinition(
    effect_type="saturation",
    name="Saturation",
    category="audio",
    description="Harmonic saturation / waveshaper",
    params_schema={
        "drive": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Drive",
            unit="%",
        ),
    },
    icon="audio_track",
)

IMAGER_EFFECT = EffectDefinition(
    effect_type="imager",
    name="Stereo Imager",
    category="audio",
    description="Mid/Side stereo width control",
    params_schema={
        "width": ParamSchema(
            type="float",
            default=100.0,
            min_value=0.0,
            max_value=200.0,
            step=0.5,
            label="Width",
            unit="%",
        ),
        "pan": ParamSchema(
            type="float",
            default=0.0,
            min_value=-100.0,
            max_value=100.0,
            step=0.5,
            label="Pan",
            unit="",
        ),
    },
    icon="audio_track",
)

EXPANDER_EFFECT = EffectDefinition(
    effect_type="expander",
    name="Expander / Gate",
    category="audio",
    description="Downward expander with gate mode",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-35.0,
            min_value=-50.0,
            max_value=0.0,
            step=0.5,
            label="Threshold",
            unit="dB",
        ),
        "range_db": ParamSchema(
            type="float",
            default=18.0,
            min_value=0.0,
            max_value=60.0,
            step=0.5,
            label="Range",
            unit="dB",
        ),
        "release_ms": ParamSchema(
            type="float",
            default=93.0,
            min_value=50.0,
            max_value=4000.0,
            step=1.0,
            label="Release",
            unit="ms",
        ),
    },
    icon="audio_track",
)

# CW-A1: noise_gate 类型键保留为 Expander(Gate) 别名——旧工程数据的
# effect_type 不变，参数经 ExpanderParams.from_dict 无损承接。
NOISE_GATE_EFFECT = EffectDefinition(
    effect_type="noise_gate",
    name="Expander / Gate",
    category="audio",
    description="Legacy noise gate (alias of expander in Gate mode)",
    params_schema=dict(EXPANDER_EFFECT.params_schema),
    icon="audio_track",
)

# CW-A1: Da Vinci Fairlight 式完整单元——Dynamics（Expander/Gate →
# Compressor → Limiter 三合一）与 Spatial Panner（轨级声像器）。
# 二者不进 AUDIO_MIXER_EFFECT_TYPES（DSP 链没有同名效果器）：拖放到音频
# 轨时由 timeline_view 的 drop 路由特殊处理——dynamics 展开为三实例、
# spatial_panner 写入轨的 panner 字段。
DYNAMICS_EFFECT = EffectDefinition(
    effect_type="dynamics",
    name="Dynamics",
    category="audio",
    description="Expander/Gate + Compressor + Limiter as one Dynamics unit",
    # 基础区只暴露 Make Up（EG/Comp/Lim 细项在完整 Dynamics 面板编辑）
    params_schema={
        "makeup": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=24.0,
            step=0.1,
            label="Make Up",
            unit="dB",
        ),
    },
    icon="audio_track",
)

SPATIAL_PANNER_EFFECT = EffectDefinition(
    effect_type="spatial_panner",
    name="Spatial Panner",
    category="audio",
    description="2D/3D spatial panner (L/R, F/B, elevation, spread, boom)",
    params_schema={},
    icon="audio_track",
)


# ================================================================
# Generator Clip Definitions
#
# These are NOT real video filters — they are "clip templates" the user
# drags from the Effects panel to create a new generator clip (no source
# media). Two initial generators ship:
#
#   adjustment_clip — an Adjustment Clip whose effect chain is applied to
#                     all video tracks below it in the timeline.
#   solid_color     — a Solid Color clip that paints a flat color frame.
#
# Because they are not filters, they are excluded from AUDIO_MIXER_EFFECT_TYPES
# and are never consumed by _apply_clip_effects (the renderer recognises the
# corresponding MediaAsset.media_type instead). Their effect_type values are
# matched by the timeline drop handler to route to the generator-creation path
# rather than the effect-attach path.
# ================================================================

# Set of generator effect_types — drag of these creates a clip, not an effect.
# Used by the Effects panel (startDrag MIME selection) and the timeline
# dropEvent router to distinguish generator drops from regular effect drops.

def _float_schema(default: float, lo: float, hi: float, step: float,
                  label: str, unit: str = "") -> ParamSchema:
    return ParamSchema(type="float", default=float(default),
                       min_value=float(lo), max_value=float(hi),
                       step=float(step), label=label, unit=unit)


def _bool_schema(default: bool, label: str) -> ParamSchema:
    return ParamSchema(type="bool", default=bool(default), label=label)


def _choice_schema(default: str, choices: tuple, label: str) -> ParamSchema:
    return ParamSchema(type="choice", default=str(default),
                       choices=tuple(str(c) for c in choices), label=label)


# ── NF-01（2026-11-15 计划 §6.3）：两款新复合 FX 的完整 ParamSchema ──
# schema 与 timeline_v2 的 VoiceChangerParams/SpectralManglerParams 扁平键
# 一一对应（默认值同源）；random_seed 等元数据不进 schema（不向普通
# 连续 lane 暴露）。

VOICE_CHANGER_EFFECT = EffectDefinition(
    effect_type="voice_changer",
    name="Voice Changer",
    category="audio",
    description="Formant / Pitch / Vocoder voice transform chain",
    params_schema={
        "pitch_semitones": _float_schema(0.0, -24.0, 24.0, 0.1,
                                         "Semitones", "st"),
        "formant_shift_st": _float_schema(0.0, -12.0, 12.0, 0.1,
                                          "Formant Shift", "st"),
        "vocoder_carrier": _choice_schema(
            "saw", ("saw", "pulse", "choir", "noise"), "Carrier"),
        "output_gain_db": _float_schema(0.0, -20.0, 20.0, 0.1,
                                        "Output Gain", "dB"),
    },
    icon="audio_track",
)

SPECTRAL_MANGLER_EFFECT = EffectDefinition(
    effect_type="spectral_mangler",
    name="Spectral Mangler",
    category="audio",
    description="Ring / Granular / Distortion mangling topology",
    params_schema={
        "routing": _choice_schema(
            "ring_grain_dist",
            ("ring_grain_dist", "grain_ring_dist", "dist_grain_ring",
             "parallel"), "Routing"),
        "global_mix_pct": _float_schema(100.0, 0.0, 100.0, 1.0,
                                        "Global Mix", "%"),
        "dist_curve": _choice_schema(
            "fold", ("tape", "fold", "hard_clip", "bitcrush"),
            "Dist Curve"),
        "output_gain_db": _float_schema(0.0, -24.0, 12.0, 0.1,
                                        "Output Gain", "dB"),
    },
    icon="audio_track",
)


# ── P1（2026-11-19 计划第三阶段 #1-#7）：新 FX 拖拽源条目 ──
# schema 取 timeline_v2 Params dataclass 的核心连续键（与 mixer 通用
# 参数对话框同源）；mode/panel 类元数据不进 schema。

DE_ESSER_EFFECT = EffectDefinition(
    effect_type="de_esser",
    name="De-Esser",
    category="audio",
    description="Split-band sibilance control with streaming RMS envelope",
    params_schema={
        "threshold_dbfs": _float_schema(-32.0, -60.0, 0.0, 0.1,
                                        "Threshold", "dBFS"),
        "ratio": _float_schema(4.0, 1.0, 12.0, 0.1, "Ratio", ":1"),
        "frequency": _float_schema(6500.0, 1000.0, 16000.0, 10.0,
                                   "Split Frequency", "Hz"),
        "max_reduction_db": _float_schema(12.0, 0.0, 24.0, 0.5,
                                          "Max Reduction", "dB"),
    },
    icon="audio_track",
)

TRUE_PEAK_LIMITER_EFFECT = EffectDefinition(
    effect_type="true_peak_limiter",
    name="True Peak Limiter",
    category="audio",
    description="4x oversampled true-peak limiter with lookahead",
    params_schema={
        "ceiling_dbtp": _float_schema(-1.0, -12.0, 0.0, 0.1,
                                      "Ceiling", "dBTP"),
        "lookahead_ms": _float_schema(5.0, 1.0, 20.0, 0.1,
                                      "Lookahead", "ms"),
        "release_ms": _float_schema(80.0, 20.0, 500.0, 1.0,
                                    "Release", "ms"),
    },
    icon="audio_track",
)

DYNAMIC_EQ_EFFECT = EffectDefinition(
    effect_type="dynamic_eq",
    name="Dynamic EQ",
    category="audio",
    description="Bell EQ whose gain follows the band energy envelope",
    params_schema={
        "frequency": _float_schema(3000.0, 20.0, 20000.0, 10.0,
                                   "Frequency", "Hz"),
        "q": _float_schema(1.4, 0.3, 12.0, 0.05, "Resonance", "Q"),
        "gain_db": _float_schema(0.0, -18.0, 18.0, 0.1, "Base Gain", "dB"),
        "threshold_dbfs": _float_schema(-30.0, -60.0, 0.0, 0.5,
                                        "Threshold", "dBFS"),
        "max_cut_db": _float_schema(9.0, 0.0, 24.0, 0.5, "Max Cut", "dB"),
    },
    icon="audio_track",
)

BUS_COMP_EFFECT = EffectDefinition(
    effect_type="bus_comp",
    name="Bus Compressor",
    category="audio",
    description="Glue compressor with DX/MX templates and sidechain HPF",
    params_schema={
        "template": _choice_schema("dx_glue", ("dx_glue", "mx_glue"),
                                   "Template"),
        "sc_hpf_hz": _float_schema(120.0, 0.0, 500.0, 5.0,
                                   "Sidechain HPF", "Hz"),
    },
    icon="audio_track",
)

MULTIBAND_COMP_EFFECT = EffectDefinition(
    effect_type="multiband_comp",
    name="Multiband Compressor",
    category="audio",
    description="Three-band subtractive-crossover compressor",
    params_schema={
        "crossover_low": _float_schema(180.0, 60.0, 800.0, 5.0,
                                       "Crossover 1", "Hz"),
        "crossover_high": _float_schema(4200.0, 800.0, 12000.0, 25.0,
                                        "Crossover 2", "Hz"),
        "attack_ms": _float_schema(10.0, 1.0, 100.0, 0.5, "Attack", "ms"),
    },
    icon="audio_track",
)

CONVOLUTION_REVERB_EFFECT = EffectDefinition(
    effect_type="convolution_reverb",
    name="Convolution Reverb",
    category="audio",
    description="Partitioned-FFT convolution reverb with WAV impulse response",
    params_schema={
        "wet": _float_schema(0.35, 0.0, 1.0, 0.01, "Wet", ""),
        "dry": _float_schema(1.0, 0.0, 1.0, 0.01, "Dry", ""),
    },
    icon="audio_track",
)

TRANSIENT_DESIGNER_EFFECT = EffectDefinition(
    effect_type="transient_designer",
    name="Transient Designer",
    category="audio",
    description="dB-domain attack/sustain shaping with 3 dB knee",
    params_schema={
        "attack_db": _float_schema(6.0, -12.0, 24.0, 0.5, "Attack", "dB"),
        "sustain_db": _float_schema(0.0, -24.0, 12.0, 0.5, "Sustain", "dB"),
        "attack_ms": _float_schema(8.0, 0.5, 50.0, 0.5, "Attack Time", "ms"),
        "sustain_ms": _float_schema(80.0, 10.0, 300.0, 1.0,
                                    "Sustain Release", "ms"),
    },
    icon="audio_track",
)

TREMOLO_AUTO_PAN_EFFECT = EffectDefinition(
    effect_type="tremolo_auto_pan",
    name="Tremolo / Auto Pan",
    category="audio",
    description="Phase-continuous LFO tremolo or equal-power auto pan",
    params_schema={
        "rate_hz": _float_schema(4.0, 0.05, 20.0, 0.05, "Rate", "Hz"),
        "depth": _float_schema(0.7, 0.0, 1.0, 0.01, "Depth", ""),
    },
    icon="audio_track",
)

CHORUS_FLANGER_EFFECT = EffectDefinition(
    effect_type="chorus_flanger",
    name="Chorus / Flanger",
    category="audio",
    description="Modulated delay line with feedback tail budget",
    params_schema={
        "rate_hz": _float_schema(0.6, 0.01, 20.0, 0.01, "Rate", "Hz"),
        "depth_ms": _float_schema(3.0, 0.0, 10.0, 0.05, "Depth", "ms"),
        "feedback": _float_schema(0.0, 0.0, 0.95, 0.01, "Feedback", ""),
        "mix": _float_schema(0.5, 0.0, 1.0, 0.01, "Mix", ""),
    },
    icon="audio_track",
)

PHASER_EFFECT = EffectDefinition(
    effect_type="phaser",
    name="Phaser",
    category="audio",
    description="All-pass cascade with phase-continuous LFO sweep",
    params_schema={
        "rate_hz": _float_schema(0.5, 0.01, 20.0, 0.01, "Rate", "Hz"),
        "stages": _float_schema(4.0, 1.0, 12.0, 1.0, "Stages", ""),
        "base_hz": _float_schema(600.0, 100.0, 4000.0, 10.0,
                                 "Base Frequency", "Hz"),
        "sweep_octaves": _float_schema(2.5, 0.5, 5.0, 0.1,
                                       "Sweep", "oct"),
        "mix": _float_schema(0.5, 0.0, 1.0, 0.01, "Mix", ""),
    },
    icon="audio_track",
)


def _paramdef_to_schema(param_def) -> ParamSchema:
    """Convert a composite ``ParamDef`` to an Edit ``ParamSchema``.

    Lossy for non-MVP types (see ``_PARAM_TYPE_MAP``); raises never —
    unknown ptypes fall back to ``float`` so a corrupt spec can't break
    the panel.
    """
    ptype = _PARAM_TYPE_MAP.get(param_def.ptype, "float")
    choices = tuple(param_def.labels) if param_def.labels else ()
    # ParamSchema.__post_init__ requires choices for the 'choice' type.
    if ptype == "choice" and not choices:
        ptype = "string"
    return ParamSchema(
        type=ptype,
        default=param_def.default,
        min_value=float(param_def.min_val),
        max_value=float(param_def.max_val),
        step=float(param_def.step),
        label=param_def.name.replace("_", " ").title(),
        choices=choices,
        unit="",
    )


def _spec_to_definition(spec) -> EffectDefinition:
    """Convert an :class:`EffectSpec` to an :class:`EffectDefinition`.

    Hidden params are included so that ``create_instance()`` seeds their
    defaults (shaders may reference them); the panel hides them via the
    hidden flag at the ParamDef level when it reads the spec directly in
    Phase 2.
    """
    params_schema = {
        pdef.name: _paramdef_to_schema(pdef)
        for pdef in spec.param_defs
    }
    return EffectDefinition(
        effect_type=spec.effect_type,
        name=spec.name,
        category=spec.category,
        description=spec.description,
        params_schema=params_schema,
        icon=spec.icon,
    )


# ================================================================
# Built-in Effect Definitions
# ================================================================

# T-P1-20（审计 P6-step-1 落地）：transform / crop / opacity / vignette 的
# legacy FX 定义已从注册表移除——它们从未接任何渲染后端（attach 后零视觉
# 效果），真实能力一直以 clip-native 形态存在（TransformParams /
# CropParams / ClipInstance.opacity）。NON_ATTACHABLE_VIDEO_EFFECT_TYPES
# 保留为旧工程数据防线；EffectInstance 反序列化时 ghost 实例会被迁移剔除
# （见 effect_instance.from_dict）。

# ================================================================
# Audio Effect Definitions (clip-level mixer FX)
#
# These reuse the 8 CPU-DSP effects from the Audio Mixer
# (src/features/edit/core/audio_dsp.py: _EFFECT_CLASS_REGISTRY) so they
# can be dropped onto individual audio clips and processed per-clip.
#
# Each definition only declares 2-4 "basic" params for the inline slider
# section; the full parameter set lives in the standalone dialog opened
# via the "打开面板" button (clip_audio_effect_dialog_adapter.py).
# Default params for create_instance() are seeded from the corresponding
# *Params.default().to_dict() (see _AUDIO_DEFAULT_PARAMS below) so the
# DSP always receives a complete dict.
# ================================================================

# Map mixer effect_type → factory returning the full default params dict.
# Lazily built from timeline_v2 *Params to avoid a hard import cycle here.
_AUDIO_DEFAULT_PARAMS: Optional[dict] = None
# AUD-16807：惰性全局缓存与单例的初始化锁——check-then-act 必须原子，
# 否则并发首调可能重复构建 dict / 单例（EffectRegistry 构造含 catalog
# 注册，重复构造既浪费又可能让两次调用拿到不同实例）。
_AUDIO_DEFAULT_PARAMS_LOCK = threading.Lock()


def _get_audio_default_params() -> dict:
    """Return the full default params dict for each mixer audio effect."""
    global _AUDIO_DEFAULT_PARAMS
    if _AUDIO_DEFAULT_PARAMS is None:
        with _AUDIO_DEFAULT_PARAMS_LOCK:
            if _AUDIO_DEFAULT_PARAMS is None:
                from src.features.edit.core.timeline_v2 import (
                    StripEQParams, LimiterParams, CompressorParams, ReverbParams,
                    DelayParams, SaturationParams, ImagerParams, ExpanderParams,
                    DynamicsParams, VoiceChangerParams, SpectralManglerParams,
                    DeEsserParams, TruePeakLimiterParams, DynamicEqParams,
                    BusCompParams, MultibandCompParams, ConvolutionReverbParams,
                    TransientDesignerParams, TremoloAutoPanParams,
                    ChorusFlangerParams, PhaserParams,
                )
                # CW-A1: Expander/Gate 接替 Noise Gate；noise_gate 键保持注册
                #（旧数据无损承接为 Gate），新增 expander 类型。
                gate_default = {**ExpanderParams.default().to_dict(), "mode": "Gate"}
                _AUDIO_DEFAULT_PARAMS = {
                    "strip_eq": StripEQParams.default().to_dict(),
                    "limiter": LimiterParams.default().to_dict(),
                    "compressor": CompressorParams.default().to_dict(),
                    "reverb": ReverbParams.default().to_dict(),
                    "delay": DelayParams.default().to_dict(),
                    "saturation": SaturationParams.default().to_dict(),
                    "imager": ImagerParams.default().to_dict(),
                    "expander": ExpanderParams.default().to_dict(),
                    "noise_gate": gate_default,
                    "dynamics": DynamicsParams.default().to_dict(),
                    # NF-01：两款新复合 FX 默认参数（新建 clip/Mixer 同源）
                    "voice_changer": VoiceChangerParams.default().to_dict(),
                    "spectral_mangler": SpectralManglerParams.default().to_dict(),
                    # P1（2026-11-19 计划第三阶段）：新 FX 默认参数
                    "de_esser": DeEsserParams.default().to_dict(),
                    "true_peak_limiter": TruePeakLimiterParams.default().to_dict(),
                    "dynamic_eq": DynamicEqParams.default().to_dict(),
                    "bus_comp": BusCompParams.default().to_dict(),
                    "multiband_comp": MultibandCompParams.default().to_dict(),
                    "convolution_reverb": ConvolutionReverbParams.default().to_dict(),
                    "transient_designer": TransientDesignerParams.default().to_dict(),
                    "tremolo_auto_pan": TremoloAutoPanParams.default().to_dict(),
                    "chorus_flanger": ChorusFlangerParams.default().to_dict(),
                    "phaser": PhaserParams.default().to_dict(),
                }
    return _AUDIO_DEFAULT_PARAMS


# Set of mixer audio effect_types — used by the panel to show the
# "打开面板" button and by the DSP layer to filter clip.effects.
AUDIO_MIXER_EFFECT_TYPES = frozenset({
    "strip_eq", "limiter", "compressor", "reverb",
    "delay", "saturation", "imager", "expander", "noise_gate",
    "dynamics",  # CW-A1: Dynamics 完整单元（EG→Comp→Lim 单实例复合）
    # NF-01（2026-11-15 计划 §6.3）：两款新复合 FX
    "voice_changer", "spectral_mangler",
    # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX
    "de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
    "multiband_comp", "convolution_reverb", "transient_designer",
    "tremolo_auto_pan", "chorus_flanger", "phaser",
})


# Explicit ordering of video categories for the Effects panel.
#
# Without this, get_categories() returns dict-insertion order — a fragile
# implicit contract that silently flips whenever registration order
# changes (e.g. a new builtin is added before the catalog loop). The
# panel reads this list to lay out its tree top-to-bottom, so a stable,
# intentional order keeps the UI predictable. "audio" is listed last
# (it has its own tab but the order also governs Favorites grouping);
# unknown categories fall after the known ones, sorted alphabetically.
_CATEGORY_ORDER: Tuple[str, ...] = (
    "transform",  # Transform / Crop / Opacity — geometric, most used
    "filter",     # Filter — Blur / Sharpen / Defocus / Denoise / Edge Detect / Erode·Dilate
    "stylize",    # Stylize — Vignette / Glow
    "generator",  # Generators — Adjustment Clip / Solid Color (no-source clip templates)
    "plugin",     # Plugins — DCTL (.tglsl) plugins
    "audio",      # Audio — 8 mixer FX (own tab, listed last)
)


STRIP_EQ_EFFECT = EffectDefinition(
    effect_type="strip_eq",
    name="Parametric EQ",
    category="audio",
    description="6-band parametric equalizer (open panel for full curve)",
    # 频段形状复杂（每段形/频/增益/Q/斜率），基础区只暴露整体增益；
    # 完整编辑在 ParametricEQDialog
    params_schema={
        "master_gain": ParamSchema(
            type="float",
            default=0.0,
            min_value=-20.0,
            max_value=20.0,
            step=0.1,
            label="Master Gain",
            unit="dB",
        ),
    },
    icon="audio_track",
)

LIMITER_EFFECT = EffectDefinition(
    effect_type="limiter",
    name="Limiter",
    category="audio",
    description="Brick-wall limiter to prevent clipping",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-6.0,
            min_value=-30.0,
            max_value=0.0,
            step=0.1,
            label="Threshold",
            unit="dB",
        ),
        "release_ms": ParamSchema(
            type="float",
            default=93.0,
            min_value=50.0,
            max_value=4000.0,
            step=1.0,
            label="Release",
            unit="ms",
        ),
    },
    icon="audio_track",
)

COMPRESSOR_EFFECT = EffectDefinition(
    effect_type="compressor",
    name="Compressor",
    category="audio",
    description="Dynamic range compressor with sidechain",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-15.0,
            min_value=-60.0,
            max_value=0.0,
            step=0.5,
            label="Threshold",
            unit="dB",
        ),
        "ratio": ParamSchema(
            type="float",
            default=2.5,
            min_value=1.2,
            max_value=20.0,
            step=0.1,
            label="Ratio",
            unit=":1",
        ),
        "makeup_gain": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=24.0,
            step=0.1,
            label="Makeup",
            unit="dB",
        ),
    },
    icon="audio_track",
)

REVERB_EFFECT = EffectDefinition(
    effect_type="reverb",
    name="Acoustic Reverb",
    category="audio",
    description="Acoustic room reverb (direct / ER / late FDN)",
    params_schema={
        "dry_wet": ParamSchema(
            type="float",
            default=100.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Dry/Wet",
            unit="%",
        ),
        "reverb_time": ParamSchema(
            type="float",
            default=2730.0,
            min_value=100.0,
            max_value=4000.0,
            step=10.0,
            label="Reverb Time",
            unit="ms",
        ),
    },
    icon="audio_track",
)

DELAY_EFFECT = EffectDefinition(
    effect_type="delay",
    name="Stereo Delay",
    category="audio",
    description="Dual-line stereo delay with cross feedback",
    params_schema={
        "left_delay": ParamSchema(
            type="float",
            default=121.0,
            min_value=0.0,
            max_value=1200.0,
            step=1.0,
            label="L Time",
            unit="ms",
        ),
        "right_delay": ParamSchema(
            type="float",
            default=135.0,
            min_value=0.0,
            max_value=1200.0,
            step=1.0,
            label="R Time",
            unit="ms",
        ),
        "dry_wet": ParamSchema(
            type="float",
            default=30.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Dry/Wet",
            unit="%",
        ),
    },
    icon="audio_track",
)

SATURATION_EFFECT = EffectDefinition(
    effect_type="saturation",
    name="Saturation",
    category="audio",
    description="Harmonic saturation / waveshaper",
    params_schema={
        "drive": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            label="Drive",
            unit="%",
        ),
    },
    icon="audio_track",
)

IMAGER_EFFECT = EffectDefinition(
    effect_type="imager",
    name="Stereo Imager",
    category="audio",
    description="Mid/Side stereo width control",
    params_schema={
        "width": ParamSchema(
            type="float",
            default=100.0,
            min_value=0.0,
            max_value=200.0,
            step=0.5,
            label="Width",
            unit="%",
        ),
        "pan": ParamSchema(
            type="float",
            default=0.0,
            min_value=-100.0,
            max_value=100.0,
            step=0.5,
            label="Pan",
            unit="",
        ),
    },
    icon="audio_track",
)

EXPANDER_EFFECT = EffectDefinition(
    effect_type="expander",
    name="Expander / Gate",
    category="audio",
    description="Downward expander with gate mode",
    params_schema={
        "threshold_db": ParamSchema(
            type="float",
            default=-35.0,
            min_value=-50.0,
            max_value=0.0,
            step=0.5,
            label="Threshold",
            unit="dB",
        ),
        "range_db": ParamSchema(
            type="float",
            default=18.0,
            min_value=0.0,
            max_value=60.0,
            step=0.5,
            label="Range",
            unit="dB",
        ),
        "release_ms": ParamSchema(
            type="float",
            default=93.0,
            min_value=50.0,
            max_value=4000.0,
            step=1.0,
            label="Release",
            unit="ms",
        ),
    },
    icon="audio_track",
)

# CW-A1: noise_gate 类型键保留为 Expander(Gate) 别名——旧工程数据的
# effect_type 不变，参数经 ExpanderParams.from_dict 无损承接。
NOISE_GATE_EFFECT = EffectDefinition(
    effect_type="noise_gate",
    name="Expander / Gate",
    category="audio",
    description="Legacy noise gate (alias of expander in Gate mode)",
    params_schema=dict(EXPANDER_EFFECT.params_schema),
    icon="audio_track",
)

# CW-A1: Da Vinci Fairlight 式完整单元——Dynamics（Expander/Gate →
# Compressor → Limiter 三合一）与 Spatial Panner（轨级声像器）。
# 二者不进 AUDIO_MIXER_EFFECT_TYPES（DSP 链没有同名效果器）：拖放到音频
# 轨时由 timeline_view 的 drop 路由特殊处理——dynamics 展开为三实例、
# spatial_panner 写入轨的 panner 字段。
DYNAMICS_EFFECT = EffectDefinition(
    effect_type="dynamics",
    name="Dynamics",
    category="audio",
    description="Expander/Gate + Compressor + Limiter as one Dynamics unit",
    # 基础区只暴露 Make Up（EG/Comp/Lim 细项在完整 Dynamics 面板编辑）
    params_schema={
        "makeup": ParamSchema(
            type="float",
            default=0.0,
            min_value=0.0,
            max_value=24.0,
            step=0.1,
            label="Make Up",
            unit="dB",
        ),
    },
    icon="audio_track",
)

SPATIAL_PANNER_EFFECT = EffectDefinition(
    effect_type="spatial_panner",
    name="Spatial Panner",
    category="audio",
    description="2D/3D spatial panner (L/R, F/B, elevation, spread, boom)",
    params_schema={},
    icon="audio_track",
)


# ================================================================
# Generator Clip Definitions
#
# These are NOT real video filters — they are "clip templates" the user
# drags from the Effects panel to create a new generator clip (no source
# media). Two initial generators ship:
#
#   adjustment_clip — an Adjustment Clip whose effect chain is applied to
#                     all video tracks below it in the timeline.
#   solid_color     — a Solid Color clip that paints a flat color frame.
#
# Because they are not filters, they are excluded from AUDIO_MIXER_EFFECT_TYPES
# and are never consumed by _apply_clip_effects (the renderer recognises the
# corresponding MediaAsset.media_type instead). Their effect_type values are
# matched by the timeline drop handler to route to the generator-creation path
# rather than the effect-attach path.
# ================================================================

# Set of generator effect_types — drag of these creates a clip, not an effect.
# Used by the Effects panel (startDrag MIME selection) and the timeline
# dropEvent router to distinguish generator drops from regular effect drops.

def _float_schema(default: float, lo: float, hi: float, step: float,
                  label: str, unit: str = "") -> ParamSchema:
    return ParamSchema(type="float", default=float(default),
                       min_value=float(lo), max_value=float(hi),
                       step=float(step), label=label, unit=unit)


def _bool_schema(default: bool, label: str) -> ParamSchema:
    return ParamSchema(type="bool", default=bool(default), label=label)


def _choice_schema(default: str, choices: tuple, label: str) -> ParamSchema:
    return ParamSchema(type="choice", default=str(default),
                       choices=tuple(str(c) for c in choices), label=label)


# ── NF-01（2026-11-15 计划 §6.3）：两款新复合 FX 的完整 ParamSchema ──
# schema 与 timeline_v2 的 VoiceChangerParams/SpectralManglerParams 扁平键
# 一一对应（默认值同源）；random_seed 等元数据不进 schema（不向普通
# 连续 lane 暴露）。


GENERATOR_EFFECT_TYPES = frozenset({"adjustment_clip", "solid_color"})

# FX-C06 → T-P1-20：ghost effect_types 已从注册表完全移除（审计 P6-step-1
# 落地）——它们从未接任何渲染后端，真实能力以 clip-native 形态存在
# （transform / crop / opacity 由 Inspector 编辑 clip 参数；vignette 为
# 历史 stylize 条目）。本集合保留为旧工程数据防线：反序列化时 ghost 实例
# 被迁移剔除（effect_instance.from_dict），任何残留 attach 路径仍被拒绝。
NON_ATTACHABLE_VIDEO_EFFECT_TYPES = frozenset({
    "transform",   # clip-native: TransformParams on ClipInstance
    "crop",        # clip-native: CropParams on ClipInstance
    "opacity",     # clip-native: ClipInstance.opacity
    "vignette",    # legacy stylize, never wired to a backend
})


def is_attachable_video_effect(effect_type: str) -> bool:
    """Whether an effect_type may be ATTACHED to a clip as a renderable filter.

    Generators (clip templates) and the FX-C06 ghost types (clip-native
    properties / legacy unrendered) are not attachable. The Effects panel uses
    this to decide what to show as a double-click-attachable video filter.
    """
    if effect_type in GENERATOR_EFFECT_TYPES:
        return False
    if effect_type in NON_ATTACHABLE_VIDEO_EFFECT_TYPES:
        return False
    return True


ADJUSTMENT_CLIP_DEFINITION = EffectDefinition(
    effect_type="adjustment_clip",
    name="Adjustment Clip",
    category="generator",
    description="Create a clip that applies its effects to all tracks below it",
    params_schema={},  # 调节片段无内置参数，效果挂在创建后的 clip.effects 上
    icon="adjustment",
)

SOLID_COLOR_DEFINITION = EffectDefinition(
    effect_type="solid_color",
    name="Solid Color",
    category="generator",
    description="Create a clip that paints a flat color frame",
    params_schema={
        "color": ParamSchema(
            type="color",
            default=(0.0, 0.0, 0.0),
            min_value=0.0,
            max_value=1.0,
            step=0.01,
            label="Color",
            unit="",
        ),
    },
    icon="solid_color",
)


# ================================================================
# Effect Registry
# ================================================================

class EffectRegistry:
    """
    Registry of all available effects.

    This class manages the collection of EffectDefinitions and provides
    lookup methods for the UI panels.

    Usage:
        registry = EffectRegistry()
        blur_def = registry.get_definition('gaussian_blur')
        blur_instance = blur_def.create_instance()

    Categories (AUD-16806：与实际注册内容同步，修复文档漂移):
        - audio: 8 个 mixer FX（strip_eq / limiter / compressor / reverb /
          delay / saturation / imager / noise_gate，CPU DSP，clip 级）
        - generator: clip 模板（Adjustment Clip / Solid Color，拖拽建
          clip 用，非 attach 特效）
        - filter / stylize / plugin / transform: 来自统一 EffectCatalog
          （src.features.shared.effects），与 composite 页和 GPU renderer
          同源；catalog 不可用时本会话缺失并告警（不再回退 legacy 条目，
          transform/crop/opacity/vignette ghost FX 已按 T-P1-20 移除）。
    """

    def __init__(self) -> None:
        """Initialize registry with built-in effects."""
        self._definitions: Dict[str, EffectDefinition] = {}
        self._categories: Dict[str, List[str]] = {}

        # Register built-in effects
        self._register_builtin_effects()

    def _register_builtin_effects(self) -> None:
        """Register all built-in effects."""
        # T-P1-20：transform/crop/opacity/vignette ghost FX 不再注册——
        # registry 可见但语义不明的条目为零容忍（能力契约单一真相）。

        # Audio category — 8 mixer FX (clip-level, CPU DSP).
        # effect_type 与 audio_dsp._EFFECT_CLASS_REGISTRY 对齐，保证
        # clip.effects 里的实例能被 AudioMixer per-clip 链消费。
        self.register(STRIP_EQ_EFFECT)
        self.register(LIMITER_EFFECT)
        self.register(COMPRESSOR_EFFECT)
        self.register(REVERB_EFFECT)
        self.register(DELAY_EFFECT)
        self.register(SATURATION_EFFECT)
        self.register(IMAGER_EFFECT)
        self.register(EXPANDER_EFFECT)
        self.register(NOISE_GATE_EFFECT)
        # CW-A1: 完整单元条目（拖放路由特殊处理，见 timeline_view）
        self.register(DYNAMICS_EFFECT)
        self.register(SPATIAL_PANNER_EFFECT)
        # NF-01：两款新复合 FX
        self.register(VOICE_CHANGER_EFFECT)
        self.register(SPECTRAL_MANGLER_EFFECT)
        # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX
        self.register(DE_ESSER_EFFECT)
        self.register(TRUE_PEAK_LIMITER_EFFECT)
        self.register(DYNAMIC_EQ_EFFECT)
        self.register(BUS_COMP_EFFECT)
        self.register(MULTIBAND_COMP_EFFECT)
        self.register(CONVOLUTION_REVERB_EFFECT)
        self.register(TRANSIENT_DESIGNER_EFFECT)
        self.register(TREMOLO_AUTO_PAN_EFFECT)
        self.register(CHORUS_FLANGER_EFFECT)
        self.register(PHASER_EFFECT)

        # Generator category — clip templates (Adjustment Clip / Solid Color).
        # These are dragged from the panel to CREATE a new clip, not attach an
        # effect; the timeline drop handler recognises GENERATOR_EFFECT_TYPES.
        self.register(ADJUSTMENT_CLIP_DEFINITION)
        self.register(SOLID_COLOR_DEFINITION)

        # Video filters (blur / sharpen / ...) come from the unified
        # EffectCatalog — the same source the composite page and the GPU
        # renderer read. This replaces the legacy GAUSSIAN_BLUR_EFFECT /
        # SHARPEN_EFFECT hardcoded entries, which used a different
        # parameter schema (intensity/radius) that the renderer never
        # actually consumed. Legacy effect_type values in old projects are
        # migrated on load by EffectInstance.from_dict.
        try:
            from src.features.shared.effects import get_effect_catalog
            catalog = get_effect_catalog()
        except Exception as catalog_err:  # noqa: BLE001
            # Catalog must never block the registry. AUD-16804：旧回退分支
            # 注册已被 T-P1-20 删除的 GAUSSIAN_BLUR_EFFECT/SHARPEN_EFFECT，
            # 触发即 NameError 让单例构造崩溃（"优雅回退"变必然崩溃）。
            # 现仅告警并保留上面已注册的音频/生成器条目，面板不至于空白。
            logger.warning(
                "EffectRegistry: catalog unavailable (%s); "
                "video filter effects will be missing this session",
                catalog_err,
            )
        else:
            # AUD-16805：畸形 spec 只跳过自身，不得中断整个注册循环——
            # 旧实现单个 spec 抛异常会让循环内尚未注册的全部 video filter
            # 一并消失（叠加 AUD-16804 的场景则直接炸掉单例构造）。
            for spec in catalog.get_all_specs():
                try:
                    if getattr(spec, "internal", False):
                        continue
                    self.register(_spec_to_definition(spec))
                except Exception as spec_err:  # noqa: BLE001 — 单条隔离
                    logger.warning(
                        "EffectRegistry: skipping malformed effect spec "
                        "%r: %s",
                        getattr(spec, "effect_type", spec),
                        spec_err,
                    )

        logger.debug(f"Registered {len(self._definitions)} built-in effects")

    def register(self, definition: EffectDefinition) -> None:
        """
        Register an effect definition.

        Args:
            definition: EffectDefinition to register
        """
        effect_type = definition.effect_type

        # Add to definitions
        self._definitions[effect_type] = definition

        # Add to category
        category = definition.category
        if category not in self._categories:
            self._categories[category] = []
        if effect_type not in self._categories[category]:
            self._categories[category].append(effect_type)

        logger.debug(f"Registered effect: {effect_type} in category {category}")

    def get_definition(self, effect_type: str) -> Optional[EffectDefinition]:
        """
        Get effect definition by type.

        Args:
            effect_type: Effect type identifier

        Returns:
            EffectDefinition or None if not found
        """
        return self._definitions.get(effect_type)

    def get_all_definitions(self) -> List[EffectDefinition]:
        """
        Get all registered effect definitions.

        Returns:
            List of all EffectDefinitions
        """
        return list(self._definitions.values())

    def get_categories(self) -> List[str]:
        """
        Get all categories, ordered by the panel's preferred layout.

        Known categories come first in the order defined by
        ``_CATEGORY_ORDER`` (transform → blur → filter → stylize →
        plugin → audio); any unknown categories follow alphabetically.
        This replaces the old dict-insertion-order return, which was a
        fragile implicit contract that flipped whenever a builtin was
        registered before the catalog loop.

        Returns:
            List of category identifiers, panel-display-ordered
        """
        known = [c for c in _CATEGORY_ORDER if c in self._categories]
        unknown = sorted(
            c for c in self._categories if c not in _CATEGORY_ORDER
        )
        return known + unknown

    def get_effects_by_category(self, category: str) -> List[EffectDefinition]:
        """
        Get effects in a specific category.

        Args:
            category: Category name

        Returns:
            List of EffectDefinitions in that category
        """
        effect_types = self._categories.get(category, [])
        return [self._definitions[t] for t in effect_types if t in self._definitions]

    def search(self, query: str) -> List[EffectDefinition]:
        """
        Search effects by name or description.

        Args:
            query: Search query (case-insensitive)

        Returns:
            List of matching EffectDefinitions
        """
        query_lower = query.lower()
        results = []

        for definition in self._definitions.values():
            if query_lower in definition.name.lower():
                results.append(definition)
            elif query_lower in definition.description.lower():
                results.append(definition)

        return results

    def get_category_display_name(self, category: str) -> str:
        """
        Get human-readable category name.

        Args:
            category: Category identifier

        Returns:
            Display name for UI
        """
        # Map category identifiers to display names. Keep in sync with the
        # actual category values used in EffectDefinition / EffectSpec —
        # this used to drift (had a stale "color" entry, missing "filter"/
        # "plugin"). Every category that has at least one effect must be
        # listed here so the panel never falls back to the ugly ".title()".
        category_names = {
            "transform": "Transform",
            "filter": "Filter",
            "stylize": "Stylize",
            "generator": "Generators",
            "audio": "Audio",
            "plugin": "Plugins",
        }
        return category_names.get(category, category.title())


# ================================================================
# Global Registry Instance
# ================================================================

# Singleton registry instance
_registry_instance: Optional[EffectRegistry] = None
# AUD-16807：单例初始化锁（双检锁——构造只在首调发生，锁只在未初始化
# 时才被争用）。
_registry_lock = threading.Lock()


def get_effect_registry() -> EffectRegistry:
    """
    Get the global effect registry instance.

    Returns:
        EffectRegistry singleton
    """
    global _registry_instance
    if _registry_instance is None:
        with _registry_lock:
            if _registry_instance is None:
                _registry_instance = EffectRegistry()
    return _registry_instance


def get_effect_definition(effect_type: str) -> Optional[EffectDefinition]:
    """
    Convenience function to get effect definition.

    Args:
        effect_type: Effect type identifier

    Returns:
        EffectDefinition or None
    """
    return get_effect_registry().get_definition(effect_type)


def create_effect_instance(effect_type: str, order: int = 0) -> Optional[EffectInstance]:
    """
    Convenience function to create effect instance.

    For mixer audio effects the instance is seeded with the *full* default
    params dict (from the corresponding ``*Params.default().to_dict()``)
    rather than only the basic params declared in ``params_schema`` — the
    DSP consumes the complete dict and the standalone dialog edits it.

    Args:
        effect_type: Effect type identifier
        order: Processing order

    Returns:
        EffectInstance or None if effect_type not found
    """
    definition = get_effect_definition(effect_type)
    if definition is None:
        logger.warning(f"Effect type not found: {effect_type}")
        return None
    # Audio mixer FX: seed full default params so the DSP always receives
    # a complete dict (basic-schema-only seeds would omit advanced fields).
    if effect_type in AUDIO_MIXER_EFFECT_TYPES:
        try:
            full_defaults = _get_audio_default_params().get(effect_type, {})
        except Exception:  # noqa: BLE001 — defensive: timeline_v2 import
            full_defaults = {}
        if full_defaults:
            return EffectInstance(
                effect_type=effect_type,
                params=dict(full_defaults),
                is_enabled=True,
                order=order,
            )
    return definition.create_instance(order)


# EffectInstance 已在模块顶部导入（AUD-16807：模块底部延迟 import 是
# 历史遗留——effect_instance 与本模块无环，顶层导入让初始化时序可推断）。


# ================================================================
# Registry Health Check (P1-07)
# ================================================================
# The audit found the audio-effects tab degrades to a generic "coming
# soon" placeholder when the registry has nothing to show, which reads as
# a missing product capability even when the real cause is a broken
# registration/import. This check turns that state into an actionable
# per-effect diagnosis: registered? DSP backend present? Why missing?

@dataclass(frozen=True)
class AudioEffectStatus:
    """Health of one builtin audio effect."""

    effect_type: str
    registered: bool
    has_backend: bool

    @property
    def ok(self) -> bool:
        return self.registered and self.has_backend

    @property
    def reason(self) -> str:
        if self.registered and self.has_backend:
            return ""
        if not self.registered and not self.has_backend:
            return "not registered and no DSP backend"
        if not self.registered:
            return (
                "definition missing from EffectRegistry — registration "
                "failed or the builtin set was skipped"
            )
        return (
            "definition registered but audio_dsp backend class missing — "
            "preview/export would silently drop this effect"
        )

    def to_dict(self) -> dict:
        return {
            "effect_type": self.effect_type,
            "registered": self.registered,
            "has_backend": self.has_backend,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EffectRegistryHealth:
    """Aggregated registry health for the builtin audio effect set."""

    effects: Tuple[AudioEffectStatus, ...] = ()
    registry_error: Optional[str] = None

    @property
    def missing(self) -> Tuple[AudioEffectStatus, ...]:
        return tuple(e for e in self.effects if not e.ok)

    @property
    def is_healthy(self) -> bool:
        return self.registry_error is None and not self.missing

    def to_dict(self) -> dict:
        return {
            "is_healthy": self.is_healthy,
            "registry_error": self.registry_error,
            "effects": [e.to_dict() for e in self.effects],
        }


def get_effect_registry_health(
    registry: Optional[EffectRegistry] = None,
) -> EffectRegistryHealth:
    """Check the builtin audio effect set end-to-end (P1-07).

    Verifies for every ``AUDIO_MIXER_EFFECT_TYPES`` entry that (a) the
    EffectRegistry carries the definition the panel needs to display and
    instantiate it, and (b) ``audio_dsp`` has the matching processor class
    that preview/export actually run. Pure read-side probing — safe from
    any thread, never mutates either registry.

    Args:
        registry: Registry to probe (defaults to the global singleton).

    Returns:
        Immutable EffectRegistryHealth with one status per builtin effect.
    """
    reg = registry if registry is not None else get_effect_registry()

    backend_types: frozenset = frozenset()
    backend_error: Optional[str] = None
    try:
        from src.features.edit.core.audio_dsp import _ensure_effect_registry

        backend_types = frozenset(_ensure_effect_registry().keys())
    except Exception as e:  # noqa: BLE001
        backend_error = f"audio_dsp import failed: {e}"

    statuses = tuple(
        AudioEffectStatus(
            effect_type=effect_type,
            registered=reg.get_definition(effect_type) is not None,
            has_backend=effect_type in backend_types,
        )
        for effect_type in sorted(AUDIO_MIXER_EFFECT_TYPES)
    )

    return EffectRegistryHealth(effects=statuses, registry_error=backend_error)
