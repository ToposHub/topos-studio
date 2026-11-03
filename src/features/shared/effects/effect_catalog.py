"""Unified registry of effect specs (the single source of truth for video
filters).

**Three-source aggregation contract** — the catalog's contents are defined
as the union of exactly three origins, each with its own discovery path:

1. **Composite built-in nodes** (``NODE_CATEGORIES["Filter"]``) — blur,
   sharpen, glow, defocus, edge_detect, ... Each built-in spec reuses the
   node's ``PARAM_DEFS`` verbatim and the composite evaluator's GLSL
   source, so the Edit parameter panel is identical to the Composite node
   properties panel by construction. All currently-wired built-ins
   (blur/sharpen/denoise/edge_detect/erode_dilate/defocus/glow) are
   registered lazily in :meth:`EffectCatalog._ensure_built`.
2. **Composite node plugins** — discovered by ``NodePluginManager`` from
   ``~/.topos/plugins/node_plugins/`` and ``<app>/plugins/node_plugins/``.
   Planned (Phase 3); not yet wired into the build.
3. **Color page ``.tglsl`` DCTL plugins** — discovered by Color's
   ``PluginManager`` from ``<app>/plugins/*.tglsl``, wrapped into
   standalone fragment shaders by ``dctl_wrapper.wrap_dctl_process``, and
   registered with ``effect_type = "plugin:<plugin_id>"``. Per-plugin
   failures are non-fatal (a broken ``.tglsl`` is skipped with a warning,
   never poisons the catalog).

Conflict rule: sources share one ``effect_type`` namespace; registering
the same id twice replaces the previous spec (plugin reload semantics —
see :meth:`EffectCatalog.register`).

The catalog is the bridge that lets the Edit page present the *same*
effect set the Composite page offers, with identical parameter schemas
(``ParamDef``), so the parameter panel can be shared verbatim.

Threading & lifecycle: the catalog is a plain Python object (no PyQt, no
signals); all accessors take an internal ``RLock`` and are safe from
render threads. The default set is built lazily on first access (keeps
``shaders.py``'s 6000+ lines of GLSL out of import time); call
:meth:`EffectCatalog.refresh` to force a rebuild on next access.

Singleton access: ``get_effect_catalog()`` (double-checked, guarded by a
module-level lock).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from src.features.composite.nodes.base_node import ParamDef
from src.features.composite.nodes.blur_node import (
    BLUR_TYPES,
    BOKEH_BLADES,
    EDGE_POLICIES,
    QUALITY_LEVELS,
    BlurNode,
)
from src.features.composite.nodes.sharpen_node import CHANNEL_MODES as SHARPEN_CHANNEL_MODES, SharpenNode
from src.features.composite.nodes.glow_node import GlowNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.edge_detect_node import EdgeDetectNode
from src.features.composite.nodes.erode_dilate_node import ErodeDilateNode
from src.features.composite.nodes.defocus_node import DefocusNode
from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind

logger = logging.getLogger(__name__)

#: AUD-26307: ``@export_support`` 声明值白名单——只有这些令牌语义上属于
#: "GL 导出链可交付"。``all``/``video`` 是 PLUGIN_DEV_GUIDE 的文档词汇，
#: ``opengl`` 是 W06（warm_tint）先例。plugin_manager 对声明值原样透传
#: （不做值域校验），所以这里必须 fail-closed。
_EXPORT_SUPPORT_OPENGL_TOKENS = frozenset({"all", "video", "opengl"})


def _plugin_capabilities_from_declaration(
    plugin_id: str, plugin: Dict[str, Any],
) -> Tuple[str, ...]:
    """Resolve ``plugin_capabilities`` from the ``@export_support`` header.

    T-P2-20/W06 语义 + AUD-26307 值域校验：声明 ``all``/``video``/
    ``opengl`` 的插件获得 ``export_opengl`` 能力（GL 导出效果链据此
    放行，resolve_execution_capability 据此标记可交付）；未声明/
    ``none`` 维持 gpu_preview-only；**其余任何令牌**（拼写错误如
    ``openggl``，或语义上不属于 GL 链的 ``cpu``/``metal``）按解析告警
    处理并维持 preview-only——违背声明的插件不得静默进入 GL 导出链，
    Deliver preflight 拒绝而非误放行。
    """
    raw = (plugin.get("capabilities") or {}).get("export_support", "")
    value = str(raw).strip().lower()
    if value in ("", "none"):
        return ("gpu_preview",)
    if value in _EXPORT_SUPPORT_OPENGL_TOKENS:
        return ("gpu_preview", "export_opengl")
    logger.warning(
        "DCTL plugin %r declares unknown @export_support value %r "
        "(expected one of: all/video/opengl/none); staying preview-only",
        plugin_id, raw,
    )
    return ("gpu_preview",)


# ──────────────────────────────────────────────────────────────────────────
# Shader source accessors.
#
# These wrap the composite ``shaders.py`` constants in accessors rather than
# importing them eagerly at module top-level. ``shaders.py`` is a large
# module (6000+ lines of GLSL strings); deferring the import until the first
# catalog build keeps startup snappy and avoids pulling GLSL into modules
# that only need the param schema.
# ──────────────────────────────────────────────────────────────────────────

def _blur_separable_sources() -> Tuple[str, str]:
    """Return ``(horizontal_frag, vertical_frag)`` for separable blur.

    These are the box/gaussian-capable two-pass shaders. ``u_kernel_mode``
    (0=box, 1=gaussian) is set at runtime from the ``blur_type`` parameter,
    so a single program pair serves both kernel shapes.
    """
    from src.features.composite.core.shaders import (
        _BLUR_SEPARABLE_H_FRAGMENT,
        _BLUR_SEPARABLE_V_FRAGMENT,
    )
    return (_BLUR_SEPARABLE_H_FRAGMENT, _BLUR_SEPARABLE_V_FRAGMENT)


def _blur_single_pass_sources() -> Dict[str, Tuple[str, ...]]:
    """Return the non-separable Blur shader variants.

    The Composite page already owns these implementations. Keeping the
    source lookup here makes the Edit page consume the exact same GLSL
    instead of maintaining a second, subtly different blur implementation.
    """
    from src.features.composite.core.shaders import (
        _BLUR_BILATERAL_FRAGMENT,
        _BLUR_MOTION_FRAGMENT,
        _BLUR_BOKEH_FRAGMENT,
    )
    return {
        "bilateral": (_BLUR_BILATERAL_FRAGMENT,),
        "motion": (_BLUR_MOTION_FRAGMENT,),
        "bokeh": (_BLUR_BOKEH_FRAGMENT,),
    }


def _sharpen_source() -> str:
    """Return the unsharp-mask sharpen fragment shader."""
    from src.features.composite.core.shaders import _SHARPEN_FRAGMENT
    return _SHARPEN_FRAGMENT


def _denoise_source() -> str:
    """Return the bilateral denoise fragment shader."""
    from src.features.composite.core.shaders import _DENOISE_FRAGMENT
    return _DENOISE_FRAGMENT


def _edge_detect_source() -> str:
    """Return the edge detection fragment shader."""
    from src.features.composite.core.shaders import _EDGE_DETECT_FRAGMENT
    return _EDGE_DETECT_FRAGMENT


def _erode_dilate_source() -> str:
    """Return the erode/dilate morphology fragment shader.

    Note: the composite evaluator also has a compute-shader path for
    size > 16. The EffectPassRunner is fragment-only — large sizes will
    be slower but still correct. This matches the evaluator's fragment
    fallback path.
    """
    from src.features.composite.core.shaders import _ERODE_DILATE_FRAGMENT
    return _ERODE_DILATE_FRAGMENT


def _defocus_source() -> str:
    """Return the defocus/bokeh fragment shader."""
    from src.features.composite.core.shaders import _DEFOCUS_FRAGMENT
    return _DEFOCUS_FRAGMENT


def _glow_sources() -> Tuple[str, str]:
    """Return ``(horizontal_frag, vertical_frag)`` for the glow/bloom effect."""
    from src.features.composite.core.shaders import (
        _GLOW_H_FRAGMENT,
        _GLOW_V_FRAGMENT,
    )
    return (_GLOW_H_FRAGMENT, _GLOW_V_FRAGMENT)


# ──────────────────────────────────────────────────────────────────────────
# Spec builders.
#
# Each builder constructs one ``EffectSpec`` by:
#   1. Reusing the corresponding composite node's ``PARAM_DEFS`` verbatim
#      (this is the contract that keeps the Edit parameter panel identical
#      to the Composite node properties panel).
#   2. Pairing them with the GLSL source(s) the composite evaluator already
#      runs for that node.
#   3. Declaring the param-name → uniform-name mapping where they differ.
# ──────────────────────────────────────────────────────────────────────────

def _build_blur_spec() -> EffectSpec:
    """Build the ``blur`` EffectSpec.

    Mirrors :class:`BlurNode`'s parameter schema and all of the Composite
    evaluator's blur paths. ``box``/``gaussian`` use the default separable
    pair; bilateral/motion/bokeh select single-pass shader variants at run
    time from ``blur_type``.
    """
    h_frag, v_frag = _blur_separable_sources()
    # BlurNode.PARAM_DEFS already covers every parameter the H/V shaders need
    # (radius_x/y, channel_r/g/b/a, mix, edge_policy, ...). Reuse verbatim.
    return EffectSpec(
        effect_type="blur",
        name="Blur",  # i18n handled at the panel layer
        # 模糊/锐化/散焦都是 Filter 下的子类型，与 Denoise/Edge Detect
        # 等平级，归入统一的 filter 分类，避免单元素碎分类。
        category="filter",
        param_defs=tuple(BlurNode.PARAM_DEFS),
        shader_kind=ShaderKind.SEPARABLE_PASSES,
        shader_sources=(h_frag, v_frag),
        dispatch_key="blur",
        shader_variants=_blur_single_pass_sources(),
        variant_shader_kinds={
            "bilateral": ShaderKind.SINGLE_PASS,
            "motion": ShaderKind.SINGLE_PASS,
            "bokeh": ShaderKind.SINGLE_PASS,
        },
        # The H/V shaders use these exact uniform names; BlurNode's param
        # names map to them 1:1 except for the two packing transforms:
        #   blur_type (str: "box"|"gaussian") -> u_kernel_mode (int 0|1)
        #   edge_policy (str) -> u_edge_policy (int 0|1|2)
        #   channel_r/g/b/a (bool×4) -> u_channel_mask (vec4)
        # These packing conversions live in ``EffectPassRunner`` (they are
        # per-shader-kind knowledge, not per-spec), so no entry is needed
        # in ``param_to_uniform`` here.
        param_to_uniform={},
        # Variant source hashes are already part of EffectPassRunner's cache
        # key, so box/gaussian can share one program pair while the three
        # single-pass variants compile independently.
        structural_param_names=(),
        icon="blur",
        description=(
            "Box / Gaussian / Bilateral / Motion / Bokeh blur with "
            "per-channel mask and mix."
        ),
        preview_cost="medium",
    )


def _build_sharpen_spec() -> EffectSpec:
    """Build the ``sharpen`` EffectSpec.

    Mirrors :class:`SharpenNode` (unsharp mask). The sharpen shader takes
    ``u_amount`` / ``u_radius`` / ``u_threshold`` / ``u_channels`` uniforms
    directly, so the param→uniform mapping is a straight prefix.
    """
    frag = _sharpen_source()
    return EffectSpec(
        effect_type="sharpen",
        name="Sharpen",
        # 锐化与模糊同属 Filter 大类下的子类型。
        category="filter",
        param_defs=tuple(SharpenNode.PARAM_DEFS),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(frag,),
        dispatch_key="sharpen",
        param_to_uniform={
            "amount": "u_amount",
            "radius": "u_radius",
            "threshold": "u_threshold",
            "channels": "u_channels",
        },
        # ``channels`` is "all"|"rgb"|"luminance" in the schema but the
        # shader expects an int (0/1/2). Packed by the runner, not structural.
        structural_param_names=(),
        icon="sharpen",
        description="Unsharp-mask sharpening with threshold and channel mode.",
        preview_cost="low",
    )


def _build_denoise_spec() -> EffectSpec:
    """Build the ``denoise`` EffectSpec (bilateral filter).

    Simplest of the new specs: single-pass, every uniform is a direct
    param pass-through (strength/radius/luminance_only/chroma_only), no
    packing required.
    """
    return EffectSpec(
        effect_type="denoise",
        name="Denoise",
        category="filter",
        param_defs=tuple(DenoiseNode.PARAM_DEFS),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(_denoise_source(),),
        dispatch_key="denoise",
        param_to_uniform={
            "strength": "u_strength",
            "radius": "u_radius",
            "luminance_only": "u_luminance_only",
            "chroma_only": "u_chroma_only",
        },
        icon="denoise",
        description="Bilateral-filter denoise with luma/chroma mode.",
        preview_cost="high",  # bilateral is expensive per pixel
    )


def _build_edge_detect_spec() -> EffectSpec:
    """Build the ``edge_detect`` EffectSpec.

    Single-pass, but four option params (method/direction/output_mode/channel)
    pack to ints via list-index. The packing happens in the runner's packer
    (see ``_pack_edge_detect_params``).
    """
    return EffectSpec(
        effect_type="edge_detect",
        name="Edge Detect",
        category="filter",
        param_defs=tuple(EdgeDetectNode.PARAM_DEFS),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(_edge_detect_source(),),
        dispatch_key="edge_detect",
        param_to_uniform={
            "method": "u_method",
            "threshold": "u_threshold",
            "threshold_high": "u_threshold_high",
            "direction": "u_direction",
            "output_mode": "u_output_mode",
            "invert": "u_invert",
            "output_strength": "u_output_strength",
            "kernel_size": "u_kernel_size",
            "preblur": "u_preblur",
            "channel": "u_channel",
        },
        icon="edge_detect",
        description="Edge detection (Sobel/Laplacian/Prewitt/Canny).",
        preview_cost="medium",
    )


def _build_erode_dilate_spec() -> EffectSpec:
    """Build the ``erode_dilate`` EffectSpec (morphological ops).

    Single-pass fragment shader, run ``iterations`` times in a ping-pong
    loop. The composite evaluator switches to a compute shader when
    ``size > 16``; this spec is fragment-only — large sizes will be
    slower but still correct. The runner handles the iteration loop
    (see ``EffectPassRunner._run_erode_dilate``).

    Three option params pack to ints: operation (erode/dilate → 0/1),
    channels (alpha/all → 0/1), edge_mode (extend/reflect/wrap → 0/1/2).
    """
    return EffectSpec(
        effect_type="erode_dilate",
        name="Erode / Dilate",
        category="filter",
        param_defs=tuple(ErodeDilateNode.PARAM_DEFS),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(_erode_dilate_source(),),
        dispatch_key="erode_dilate",
        # iterations handled specially by the runner; the four uniforms
        # below are set per iteration.
        param_to_uniform={
            "operation": "u_operation",
            "size": "u_size",
            "channels": "u_channels",
            "edge_mode": "u_edge_mode",
        },
        icon="erode_dilate",
        description="Morphological erode/dilate with iteration control.",
        preview_cost="medium",
    )


def _build_defocus_spec() -> EffectSpec:
    """Build the ``defocus`` EffectSpec.

    NOTE: The composite ``_apply_defocus`` had a schema/implementation
    mismatch — it read ``iris_size/iris_type/gain`` but DefocusNode's
    PARAM_DEFS declared ``max_radius/focal_size/...``. The fix below
    bridges them at the packer level: map ``max_radius → u_iris_size``
    and ignore the focal_* params the shader doesn't actually consume.
    The composite evaluator itself is left untouched (Phase 2 scope is
    Edit-page filters); a separate fix to ``_apply_defocus`` is tracked
    as future work.
    """
    return EffectSpec(
        effect_type="defocus",
        name="Defocus",
        # 镜头散焦本质是模糊，归入 Filter 大类。
        category="filter",
        param_defs=tuple(DefocusNode.PARAM_DEFS),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(_defocus_source(),),
        dispatch_key="defocus",
        # The packer (_pack_defocus_params) maps max_radius → u_iris_size
        # and the defocus shader's other uniforms (aperture_blades etc.)
        # to sensible defaults. param_to_uniform here only lists the
        # direct pass-throughs.
        param_to_uniform={
            "max_radius": "u_iris_size",
        },
        icon="defocus",
        description="Lens defocus / bokeh blur.",
        preview_cost="high",
    )


def _build_glow_spec() -> EffectSpec:
    """Build the ``glow`` EffectSpec (separable two-pass bloom).

    Most complex spec: H+V passes where the V pass samples BOTH the
    original input (unit 0) and the H-pass output (unit 1, ``u_bloom_tex``).
    The runner handles the dual-texture binding (see
    ``EffectPassRunner._run_glow``). The packer merges tint_r/g/b into
    a single ``u_tint`` vec3 and derives ``u_sample_count`` from quality.
    """
    h_frag, v_frag = _glow_sources()
    return EffectSpec(
        effect_type="glow",
        name="Glow",
        # 辉光是风格化光效，归入 stylize（与 Vignette 同列），让
        # filter 专注纯数学处理（降噪/边缘/形态学）。
        category="stylize",
        param_defs=tuple(GlowNode.PARAM_DEFS),
        shader_kind=ShaderKind.SEPARABLE_PASSES,
        shader_sources=(h_frag, v_frag),
        dispatch_key="glow",
        # tint_r/g/b are merged into u_tint by the packer (multi-param →
        # one uniform). quality → u_sample_count is also packer-driven.
        # Listed here are only the direct pass-throughs.
        param_to_uniform={
            "threshold": "u_threshold",
            "radius": "u_radius",
            "intensity": "u_intensity",
            "mix": "u_mix",
        },
        icon="glow",
        description="Bloom/glow with threshold, tint and intensity.",
        preview_cost="high",
    )


def _posterize_fragment_source() -> str:
    """Composite Posterize shader（F18 共享核，计划 L）。

    该 shader 的 uniform/IO 约定（u_texture/u_resolution/v_uv/fragColor）
    与 EffectPassRunner 的单 pass 约定逐字一致，可直接复用。
    """
    from src.features.composite.core.shaders import _POSTERIZE_FRAGMENT
    return _POSTERIZE_FRAGMENT


def _build_lib1_posterize_spec() -> EffectSpec:
    """F18（计划 L）：复用 Composite Posterize 节点算法的 Edit/Color 入口。

    参数 schema 与 plugins/node_plugins/posterize_plugin.py 的
    PosterizeNode.PARAM_DEFS 逐字对齐（测试钉住防漂移）；packer 在
    ``EffectPassRunner._PARAM_PACKERS["lib1_posterize"]``（levels int→float
    uniform、channel_mode option→int、dither bool→float）。
    """
    return EffectSpec(
        effect_type="lib1_posterize",
        name="Posterize (Lib1)",
        category="filter",
        param_defs=(
            ParamDef("levels", "int", 8, min_val=2, max_val=64),
            ParamDef("channel_mode", "option", "rgb",
                     labels=("rgb", "rgba", "luminance")),
            ParamDef("dither", "bool", False),
        ),
        shader_kind=ShaderKind.SINGLE_PASS,
        shader_sources=(_posterize_fragment_source(),),
        dispatch_key="lib1_posterize",
        param_to_uniform={},
        structural_param_names=(),
        icon="posterize",
        description="Per-channel level quantization with optional dither "
                    "(shared kernel with the Composite Posterize node).",
        preview_cost="low",
    )


# ──────────────────────────────────────────────────────────────────────────
# Phase 3: DCTL (.tglsl) plugin specs.
#
# These bridge the Color page's ``.tglsl`` plugin format into the unified
# EffectSpec so the Edit page can drop a DCTL plugin onto a clip just like
# the built-in blur/sharpen filters. The wrapper (``dctl_wrapper.py``)
# turns the plugin's ``process(vec3, vec2)`` function into a complete
# fragment shader that runs through ``EffectPassRunner``.
#
# The Color page itself is NOT touched — it keeps its own fused-shader
# path (``shader_compiler.ShaderPipeline``). Only the Edit/Composite pages
# gain the ability to use these plugins.
# ──────────────────────────────────────────────────────────────────────────

def _dctl_param_to_pdef(param: dict) -> Optional[ParamDef]:
    """Map one parsed ``@param`` dict to a composite ``ParamDef``.

    Returns ``None`` for non-shader params (``separator``) so the caller
    can skip them. The mapping mirrors the @param syntax in
    PLUGIN_DEV_GUIDE.md:

      slider   → float ParamDef (min/max/step)
      checkbox → bool  ParamDef
      color    → vec3  ParamDef (approximated as float triple; UI falls
                       back to read-only until Phase 3 adds a color row)
      select   → option ParamDef (labels from the @param options)
    """
    ptype = param.get("type")
    var = param.get("var_name", "")
    if not var:
        return None

    if ptype == "slider":
        return ParamDef(
            name=var, ptype="float",
            default=float(param.get("default", 0.0)),
            min_val=float(param.get("min", 0.0)),
            max_val=float(param.get("max", 1.0)),
            step=0.01, decimals=2,
        )
    if ptype == "checkbox":
        return ParamDef(
            name=var, ptype="bool",
            default=bool(int(param.get("default", 0))),
            min_val=0.0, max_val=1.0, step=1.0, decimals=0,
        )
    if ptype == "select":
        options = param.get("options") or []
        default_idx = int(param.get("default", 0))
        return ParamDef(
            name=var, ptype="option",
            default=options[default_idx] if 0 <= default_idx < len(options) else None,
            labels=list(options),
            min_val=0.0, max_val=float(max(1, len(options) - 1)), step=1.0,
        )
    if ptype == "color":
        # ParamDef has no native vec3 UI row, but we must keep the vec3
        # semantics so the runner's packer produces a 3-tuple for the
        # ``uniform vec3 u_<var>`` the wrapper declares. Storing the
        # default as a (r,g,b) tuple and ptype="color" lets the packer
        # expand it correctly; the Edit panel renders it read-only
        # (no color picker row yet) until a proper color control exists.
        default = param.get("default", [1.0, 1.0, 1.0])
        if isinstance(default, (list, tuple)) and len(default) >= 3:
            default_rgb = (float(default[0]), float(default[1]), float(default[2]))
        else:
            default_rgb = (1.0, 1.0, 1.0)
        return ParamDef(
            name=var, ptype="color",
            default=default_rgb,
            min_val=0.0, max_val=1.0, step=0.01, decimals=3,
        )
    # separator / unknown — UI-only, no shader presence.
    return None


def _build_dctl_plugin_specs() -> List[EffectSpec]:
    """Build EffectSpecs for every discovered ``.tglsl`` DCTL plugin.

    Delegates plugin discovery to Color's ``PluginManager`` (so there's
    a single source of truth for what plugins exist) and wraps each
    plugin's ``process`` function via ``dctl_wrapper.wrap_dctl_process``.

    Failures are per-plugin and non-fatal: a plugin that fails to parse
    or wrap is skipped with a warning, so one broken ``.tglsl`` file
    can't poison the whole catalog.
    """
    from src.features.shared.effects.dctl_wrapper import wrap_dctl_process

    specs: List[EffectSpec] = []
    _plugins_dir = "<unresolved>"
    try:
        import os
        from src.features.color.core.plugin_manager import get_plugin_manager
        pm = get_plugin_manager()
        # Pass an absolute path derived from this file's location so
        # discovery remains independent of the current working directory
        # and configured relative plugin paths.
        # effect_catalog.py is at src/features/shared/effects/, project
        # root is four parents up.
        _here = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(_here))))
        _plugins_dir = os.path.join(_project_root, "plugins")
        plugins = pm.scan_plugins(_plugins_dir)
    except Exception as exc:  # noqa: BLE001 — catalog must stay robust
        # Plugin manager unavailable (e.g. running headless in tests) —
        # return an empty list rather than crashing the catalog build, but
        # never silently: a real PermissionError/OSError, a scan_plugins
        # defect, or a frozen-layout path miss must be visible in the log
        # (FX-M03 "绝不静默丢效果" / AUD-26308), otherwise every DCTL
        # plugin would just disappear from the Edit page with no trace.
        logger.warning(
            "DCTL plugin scan failed (plugins dir=%s): %s — plugin "
            "effects unavailable in this catalog build", _plugins_dir, exc,
        )
        return specs

    for plugin_id, plugin in plugins.items():
        # Only DCTL plugins have a process() shader; AI plugins (.tai)
        # are out of scope for the EffectPassRunner (fragment-only).
        if plugin.get("type") != "dctl":
            continue
        try:
            params = plugin.get("params", []) or []
            param_defs = tuple(
                p for p in (_dctl_param_to_pdef(p) for p in params) if p is not None
            )
            shader_src = wrap_dctl_process(plugin.get("shader_code", ""), params)
            # select params are structural: changing the option may switch
            # GLSL branches the compiler can't keep live, so include them
            # in the program-cache key (forces a recompile on change).
            structural = tuple(
                p["var_name"] for p in params if p.get("type") == "select"
            )
            # AUD-26309: preview_cost 契约是 str（EffectSpec.preview_cost:
            # str）。preview_quality_level 是 PreviewQualityLevel 枚举，
            # 直接存入会把枚举对象塞进 str 字段——改读同 dict 内的字符串
            # 字段 preview_cost（plugin_manager P3-4 解析产物）。
            preview_cost = str(plugin.get("preview_cost") or "medium")
            from src.features.shared.effects.plugin_identity import (
                compute_content_hash,
                compute_parameter_schema_hash,
            )
            spec = EffectSpec(
                effect_type=f"plugin:{plugin_id}",
                name=plugin.get("name", plugin_id),
                # DCTL 是技术术语，面向用户改用 "plugin" 分类，显示名
                # 由 get_category_display_name 渲染为 "Plugins"。
                category="plugin",
                param_defs=param_defs,
                shader_kind=ShaderKind.DCTL_PROCESS,
                shader_sources=(shader_src,),
                dispatch_key=f"plugin:{plugin_id}",
                # The wrapper names every uniform u_{var_name}, so the
                # ParamDef.name → uniform mapping is built dynamically by
                # the runner's generic plugin packer (no static map).
                param_to_uniform={},
                structural_param_names=structural,
                icon="plugin",
                description=plugin.get("description", ""),
                preview_cost=preview_cost,
                plugin_id=f"plugin:{plugin_id}",
                plugin_version=str(plugin.get("version", "") or ""),
                plugin_content_hash=compute_content_hash((shader_src,)),
                plugin_source_uri=str(plugin.get("source_uri", "") or ""),
                plugin_parameter_schema_hash=compute_parameter_schema_hash(param_defs),
                # T-P2-20/W06: 插件可在头部声明 ``// @export_support opengl``
                # （经 plugin_manager CAPABILITY_KEYS 解析）。声明非 none 的
                # 插件获得 export_opengl 能力——GL 导出效果链据此放行，
                # 能力解析（resolve_execution_capability）据此标记可交付。
                # 未声明的插件维持 gpu_preview-only：导出 preflight 拒绝
                # 而非静默丢弃。
                plugin_capabilities=_plugin_capabilities_from_declaration(
                    plugin_id, plugin),
                # O7（遮挡裁剪优化计划）：``// @occlusion
                # background_independent`` 声明透传到 EffectSpec；
                # 未声明/声明值不符的插件保持 False（fail-closed）。
                background_independent=(
                    str((plugin.get("capabilities") or {})
                        .get("occlusion", "")).strip().lower()
                    == "background_independent"
                ),
            )
            specs.append(spec)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "EffectCatalog: failed to build spec for DCTL plugin '%s': %s",
                plugin_id, exc,
            )
    return specs


# Re-export the option-label lists so the runner can pack them to ints
# without importing from composite/nodes/* directly (keeps the import
# surface narrow and makes future swappable sources easier).
BLUR_TYPE_LABELS: Tuple[str, ...] = tuple(BLUR_TYPES)
EDGE_POLICY_LABELS: Tuple[str, ...] = tuple(EDGE_POLICIES)
QUALITY_LABELS: Tuple[str, ...] = tuple(QUALITY_LEVELS)
BOKEH_BLADE_LABELS: Tuple[str, ...] = tuple(BOKEH_BLADES)
SHARPEN_CHANNEL_LABELS: Tuple[str, ...] = tuple(SHARPEN_CHANNEL_MODES)

# Phase 2: label lists for the option params of the new Filter specs.
# Defined here (not imported from the node modules) because several node
# modules use the same generic names (CHANNEL_MODES, EDGE_MODES) and
# importing them all would collide.
EDGE_METHOD_LABELS: Tuple[str, ...] = ("sobel", "laplacian", "prewitt", "canny")
EDGE_DIRECTION_LABELS: Tuple[str, ...] = ("both", "horizontal", "vertical")
EDGE_OUTPUT_MODE_LABELS: Tuple[str, ...] = ("edges", "gradient", "direction")
EDGE_CHANNEL_LABELS: Tuple[str, ...] = ("luminance", "r", "g", "b", "a")
ERODE_OPERATION_LABELS: Tuple[str, ...] = ("erode", "dilate")
ERODE_EDGE_MODE_LABELS: Tuple[str, ...] = ("extend", "reflect", "wrap")
ERODE_CHANNEL_LABELS: Tuple[str, ...] = ("alpha", "all")
DEFOCUS_MODE_LABELS: Tuple[str, ...] = ("separable", "mipmap")


# ──────────────────────────────────────────────────────────────────────────
# The catalog singleton.
# ──────────────────────────────────────────────────────────────────────────

class EffectCatalog:
    """Process-wide registry of :class:`EffectSpec` instances.

    The catalog is intentionally a plain Python object (no PyQt dependency,
    no signals) so it can be used from render threads and headless tools.
    UIs that want live updates when the catalog changes should call
    :meth:`refresh` and re-read; the registration surface is small enough
    that a full refresh is cheap.
    """

    def __init__(self) -> None:
        """Create an empty catalog (specs are registered lazily on use)."""
        self._lock = threading.RLock()
        self._specs: Dict[str, EffectSpec] = {}
        self._by_category: Dict[str, List[str]] = {}
        self._built = False

    # ── Construction ──────────────────────────────────────────────────
    def _ensure_built(self) -> None:
        """Lazily build the default catalog on first access.

        Lazy build avoids importing ``shaders.py`` (and therefore pulling in
        a large GLSL string module) at import time — only when something
        actually asks for a spec.

        AUD-26312: DCTL 插件目录的磁盘扫描在锁外执行——首次构建可能读
        几百个 ``.tglsl`` 文件，持锁扫描会让并发的 ``get_spec`` /
        ``is_known`` 全部阻塞排队。进入锁后二次检查 ``_built``（其他线程
        可能已经建完并注册）再注册，保证不重复。
        """
        if self._built:
            return
        with self._lock:
            if self._built:
                return
        dctl_specs = _build_dctl_plugin_specs()
        with self._lock:
            if self._built:
                return
            # Phase 1 (MVP): blur + sharpen, end-to-end.
            # Phase 2: the remaining composite Filter nodes — each
            # reuses the node's PARAM_DEFS and the composite shader
            # source, so the Edit panel shows identical controls.
            from src.features.shared.effects.video_stabilization import stabilization_spec
            for spec in (
                stabilization_spec(),
                _build_blur_spec(),
                _build_sharpen_spec(),
                _build_denoise_spec(),
                _build_edge_detect_spec(),
                _build_erode_dilate_spec(),
                _build_defocus_spec(),
                _build_glow_spec(),
                _build_lib1_posterize_spec(),  # F18（计划 L）：共享核入口
            ):
                self._register_locked(spec)
            # Phase 3: aggregate DCTL (.tglsl) plugins so the Edit page
            # can drop them on a clip like the built-in filters. Failures
            # are per-plugin and non-fatal (see _build_dctl_plugin_specs).
            for spec in dctl_specs:
                self._register_locked(spec)
            self._built = True

    def _register_locked(self, spec: EffectSpec) -> None:
        """Insert ``spec`` into the indexes. Caller holds ``self._lock``.

        Re-registering an existing ``effect_type`` replaces the spec but
        does NOT append a duplicate id to ``_by_category`` (otherwise
        ``get_specs_by_category`` would return the same spec twice after
        a plugin reload / explicit ``register()`` of an existing id).
        """
        is_new = spec.effect_type not in self._specs
        self._specs[spec.effect_type] = spec
        if is_new:
            self._by_category.setdefault(spec.category, []).append(spec.effect_type)

    # ── Public API ────────────────────────────────────────────────────
    def register(self, spec: EffectSpec) -> None:
        """Register or replace a spec. Thread-safe.

        Re-registration with the same ``effect_type`` replaces the previous
        entry (used by plugin reload).
        """
        with self._lock:
            self._register_locked(spec)

    def get_spec(self, effect_type: str) -> Optional[EffectSpec]:
        """Look up a spec by its ``effect_type`` id. Returns ``None`` if
        unknown (callers should treat unknown types as no-op)."""
        self._ensure_built()
        with self._lock:
            return self._specs.get(effect_type)

    def get_all_specs(self) -> List[EffectSpec]:
        """Return every registered spec (order is unspecified; sort by
        :meth:`get_categories` / ``name`` for stable UI ordering)."""
        self._ensure_built()
        with self._lock:
            return list(self._specs.values())

    def get_categories(self) -> List[str]:
        """Return distinct category strings, sorted for stable UI order."""
        self._ensure_built()
        with self._lock:
            return sorted(self._by_category.keys())

    def is_built(self) -> bool:
        """R01: 目录是否已完成首次构建（供只读消费者避免触发磁盘扫描）。

        False 时只读投影方应等待后台预热（EffectsResourceCatalog.
        warm_background），而不是在 GUI 线程强制懒构建。
        """
        with self._lock:
            return self._built

    def get_specs_by_category(self, category: str) -> List[EffectSpec]:
        """Return specs in a given category, sorted by display name."""
        self._ensure_built()
        with self._lock:
            ids = self._by_category.get(category, [])
            specs = [self._specs[i] for i in ids if i in self._specs]
        specs.sort(key=lambda s: s.name)
        return specs

    def is_known(self, effect_type: str) -> bool:
        """Cheap membership check (used to skip unknown legacy effect_type
        values without raising).

        AUD-26312: "cheap" 指查询本身是 O(1) dict membership；首次调用
        仍会触发一次性的目录懒构建（含插件目录扫描），构建完成后才是
        真正廉价的成员判断。不要在性能敏感的每帧路径首次调用。
        """
        self._ensure_built()
        with self._lock:
            return effect_type in self._specs

    def refresh(self) -> None:
        """Force a rebuild on next access.

        Phase 3 will use this after discovering new node / ``.tglsl``
        plugins. For Phase 1 it's a no-op clearing of the built flag so
        tests can reset state.
        """
        with self._lock:
            self._specs.clear()
            self._by_category.clear()
            self._built = False


# ──────────────────────────────────────────────────────────────────────────
# Module-level singleton accessor (mirrors the pattern used by
# ``EffectRegistry`` and composite ``get_plugin_manager``).
# ──────────────────────────────────────────────────────────────────────────

_catalog_instance: Optional[EffectCatalog] = None
_catalog_lock = threading.Lock()


def get_effect_catalog() -> EffectCatalog:
    """Return the process-wide :class:`EffectCatalog` singleton."""
    global _catalog_instance
    if _catalog_instance is None:
        with _catalog_lock:
            if _catalog_instance is None:
                _catalog_instance = EffectCatalog()
    return _catalog_instance


# ──────────────────────────────────────────────────────────────────────────
# 调色节点图三后端能力矩阵（计划 B12.1，E30/E31 接点）
# ──────────────────────────────────────────────────────────────────────────

def graph_backend_capability_matrix() -> Dict[str, Dict[str, str]]:
    """图 node + 参数域 → 各后端 exact/fallback/unsupported 能力矩阵。

    数据源是三个后端的**实际目录**（GL=N05 编译器目录、CPU=N09 oracle
    域、Metal=N10 目录）——不从「存在 shader 函数」推断，目录与实测
    不符时按计划降级为未就绪（B12.4 实机证据见
    ``docs/validation/color_nodes/backend_matrix.md``）。

    Corrector 行细分参数域（基础点态 grading/curves 为三后端共有；
    插件/邻域/LUT3D 等按后端声明，非 exact 即显式拒绝，不静默近似）。
    """
    from src.features.color.core.color_graph_compiler import (
        GL_CAPABILITY_CATALOG,
    )
    from src.features.color.core.color_graph_cpu import (
        CPU_CAPABILITY_CATALOG,
    )
    from src.features.color.core.color_graph_metal import (
        METAL_CAPABILITY_CATALOG,
    )

    ops = sorted(set(GL_CAPABILITY_CATALOG)
                 | set(CPU_CAPABILITY_CATALOG)
                 | set(METAL_CAPABILITY_CATALOG))
    matrix: Dict[str, Dict[str, str]] = {
        op: {
            "gl": GL_CAPABILITY_CATALOG.get(op, "unsupported"),
            "cpu": CPU_CAPABILITY_CATALOG.get(op, "unsupported"),
            "metal": METAL_CAPABILITY_CATALOG.get(op, "unsupported"),
        }
        for op in ops
    }
    # Corrector 参数域细分（op 目录之外的第二轴；fail-closed 判据来自
    # N09/N10 共用的 production_layer_domain_errors）
    matrix["corrector:param_domains"] = {
        "gl": "exact",  # 生产 composition shader 全域（含插件经绑定）
        "cpu": "fallback",  # 基础点态域 + curves；其余显式 UnsupportedNode
        "metal": "fallback",  # 基础点态域 + curves；其余 fail-closed 拒绝
    }
    return matrix


def graph_node_backend_options() -> List[Dict[str, Any]]:
    """从实际 operator 支持生成 UI 可选项（B12.1）。

    每项：{"node_type", "backends": {...}, "ui_enabled": bool,
    "exact_backends": [..]}。``ui_enabled`` 仅在至少一个后端非
    unsupported 时为 True——UI 只呈现实际可执行的选项，provider 未
    实现的选项如实禁用（不出现「能建图但任何后端都拒绝执行」项）。
    """
    matrix = graph_backend_capability_matrix()
    options: List[Dict[str, Any]] = []
    for node_type, backends in matrix.items():
        if node_type == "corrector:param_domains":
            continue
        statuses = [s for s in backends.values() if s != "unsupported"]
        options.append({
            "node_type": node_type,
            "backends": dict(backends),
            "exact_backends": sorted(
                k for k, v in backends.items() if v == "exact"),
            "ui_enabled": bool(statuses),
        })
    return options
