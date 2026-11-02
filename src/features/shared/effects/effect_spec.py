"""Renderer-agnostic description of a video filter / effect.

Why this exists
---------------
Topos Color has three GPU renderers that all need to run "effects":

* ``EditGpuPreviewRenderer`` (clip preview on the Edit page)
* ``CompositeRenderer`` (the composite node graph)
* export renderers (headless Composite / Edit export)

All three contexts live in the same OpenGL share group
(see ``src/main.py`` ``AA_ShareOpenGLContexts``), so texture IDs (``glo``)
can be passed across them zero-copy. **However**, moderngl ``Program`` /
``Framebuffer`` / ``VertexArray`` objects are bound to the context that
created them and cannot be reused across contexts.

The consequence: we cannot let ``EditGpuPreviewRenderer`` simply call
``CompositeRenderer._apply_blur`` — that method uses programs and FBOs owned
by the composite context. Instead, we factor out everything about an effect
that *is* shareable — the GLSL source, the parameter schema, the uniform
mapping — into a pure-data ``EffectSpec``. Each renderer compiles and runs
the spec on its own context.

The ``EffectSpec`` deliberately reuses ``ParamDef`` from
``src/features/composite/nodes/base_node.py`` rather than Edit's
``ParamSchema``: ``ParamDef`` is a strict superset (adds ``vec2/vec3/channel``
types, ``group``, ``animated``, ``allow_expression``) and matches the
Composite page UI verbatim, which is the design goal — "video filter
parameters look exactly like composite node parameters".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple

from src.features.composite.nodes.base_node import ParamDef


class ShaderKind:
    """How an ``EffectSpec``'s shader sources should be executed.

    Values are kept as plain strings (not an ``Enum``) so they serialise
    trivially and stay decoupled from PyQt / GL imports.

    * ``SEPARABLE_PASSES`` — two fragment shaders run back-to-back
      (horizontal then vertical). Used by box / gaussian blur. Each pass
      samples the previous pass's output texture. Channel mask and mix are
      applied on the final pass.
    * ``SINGLE_PASS`` — one fragment shader reads the input texture and
      writes the output in a single fullscreen-quad render. Used by
      sharpen, bilateral/motion/bokeh blur, glow, edge_detect, ...
    * ``DCTL_PROCESS`` — the source is a DaVinci-DCTL-style
      ``vec3 process(vec3 color, vec2 uv)`` function (Color page ``.tglsl``
      plugins). The wrapper in ``dctl_wrapper.py`` wraps it into a complete
      fragment shader with ``main()``; once wrapped it executes exactly like
      ``SINGLE_PASS``.
    """

    SEPARABLE_PASSES = "separable_passes"
    SINGLE_PASS = "single_pass"
    DCTL_PROCESS = "dctl_process"


@dataclass(frozen=True)
class EffectSpec:
    """Pure-data description of an effect, shareable across renderers.

    Attributes:
        effect_type: globally unique id, e.g. ``"blur"``, ``"sharpen"``,
            ``"plugin:film_halation"``. This is the value stored in
            ``EffectInstance.effect_type`` and used as the dispatch key.
        name: human-readable display name (already i18n-resolved).
        category: UI grouping, one of ``"transform"``/``"filter"``/
            ``"stylize"``/``"plugin"``/``"audio"``. Blur/sharpen/defocus
            all live under ``"filter"``. Used by the Edit Effects panel
            (and Favorites grouping); the display name is resolved via
            :meth:`EffectRegistry.get_category_display_name`.
        param_defs: tuple of :class:`ParamDef` describing every parameter.
            Renderers and parameter panels both read this — the panel builds
            its widgets from it, the runner maps values to shader uniforms.
        shader_kind: one of :class:`ShaderKind`. Determines whether the
            runner executes one or two passes.
        shader_sources: GLSL fragment source strings. Length must match the
            kind: ``SEPARABLE_PASSES`` → 2 (H then V); ``SINGLE_PASS`` and
            ``DCTL_PROCESS`` → 1. Vertex shader is supplied by the runner
            (it is shared boilerplate — see ``_PASSTHROUGH_VERTEX``).
        dispatch_key: routing key used by the composite ``_dispatch_effect``
            table. For composite-built-in effects it equals ``effect_type``
            (e.g. ``"blur"``); for ``.tglsl`` plugins it is
            ``"plugin:<plugin_id>"``. Kept separate from ``effect_type`` so
            the dispatch table can stay stable even if the user-facing id
            changes.
        param_to_uniform: maps a ``ParamDef.name`` to the shader uniform it
            feeds. Defaults to identity (uniform named the same as the
            parameter). Only non-identity mappings need to be listed.
        structural_param_names: names of params whose *value* changes the
            compiled program (e.g. blur ``blur_type`` selects which shader
            file to use; ``.tglsl`` ``select`` params may rewire branches
            that the GLSL compiler optimises away). The runner includes
            these in its program-cache key so a value change triggers a
            recompile. Empty by default — most effects have no such params.
        icon: optional icon resource name.
        description: short tooltip / search text.
        preview_cost: hint for preview-quality degradation, one of
            ``"low"``/``"medium"``/``"high"``/``"skip"``. Mirrors the Color
            page ``@preview_cost`` annotation.
    """

    effect_type: str
    name: str
    category: str
    param_defs: Tuple[ParamDef, ...]
    shader_kind: str
    shader_sources: Tuple[str, ...]
    dispatch_key: str
    # Optional shader variants selected by a structural parameter.  The
    # unified Blur effect uses this to share one catalog entry while keeping
    # the composite node's five implementations (separable and single-pass)
    # intact. Every variant must have the source count required by its kind.
    shader_variants: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    variant_shader_kinds: Dict[str, str] = field(default_factory=dict)
    param_to_uniform: Dict[str, str] = field(default_factory=dict)
    structural_param_names: Tuple[str, ...] = ()
    icon: str = ""
    description: str = ""
    preview_cost: str = "medium"
    # Registered render passes owned by a dedicated tool are not user-addable
    # clip effects. They remain discoverable by renderers through the catalog.
    internal: bool = False
    # O7（timeline_occlusion_culling_optimization_plan）：遮挡安全能力
    # 声明。True 表示该效果不读取/不依赖下层已合成背景，且不会把可见
    # 内容扩展到画布之外——只有显式声明为 True 且覆盖测试通过的效果
    # 才允许参与未来的高级遮挡裁剪。默认 False（fail-closed）：任何
    # 挂载效果在第一阶段遮挡裁剪中都阻止其所在层成为遮挡层。
    # 内置效果不声明（全部按 False）；.tglsl 插件可用
    # ``// @occlusion background_independent`` 注解显式声明。
    background_independent: bool = False
    # Stable plugin descriptor. Built-ins leave these at their defaults.
    plugin_id: str = ""
    plugin_version: str = ""
    plugin_content_hash: str = ""
    plugin_source_uri: str = ""
    plugin_parameter_schema_hash: str = ""
    plugin_capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate source-count / kind consistency at construction time.

        Cheap sanity check — catches typos early instead of at first render.
        Uses ``object.__setattr__`` because the dataclass is frozen.
        """
        if self.shader_kind == ShaderKind.SEPARABLE_PASSES:
            expected = 2
        else:
            expected = 1
        if len(self.shader_sources) != expected:
            raise ValueError(
                f"EffectSpec '{self.effect_type}': shader_kind="
                f"'{self.shader_kind}' expects {expected} shader source(s), "
                f"got {len(self.shader_sources)}"
            )
        for variant, sources in self.shader_variants.items():
            kind = self.variant_shader_kinds.get(variant, self.shader_kind)
            expected_variant = 2 if kind == ShaderKind.SEPARABLE_PASSES else 1
            if len(sources) != expected_variant:
                raise ValueError(
                    f"EffectSpec '{self.effect_type}' variant '{variant}': "
                    f"shader_kind='{kind}' expects {expected_variant} "
                    f"shader source(s), got {len(sources)}"
                )
        unknown_kinds = set(self.variant_shader_kinds) - set(self.shader_variants)
        if unknown_kinds:
            raise ValueError(
                f"EffectSpec '{self.effect_type}' has shader kinds without "
                f"variants: {sorted(unknown_kinds)}"
            )

    def uniform_name(self, param_name: str) -> str:
        """Resolve a parameter name to its shader uniform name.

        Falls back to the parameter name itself when no explicit mapping is
        declared. Centralising this here keeps the runner and the parameter
        panel in sync.
        """
        return self.param_to_uniform.get(param_name, param_name)

    def sources_for_variant(self, variant: str) -> Tuple[str, ...]:
        """Return shader sources for ``variant`` or the default sources."""
        return self.shader_variants.get(variant, self.shader_sources)

    def shader_kind_for_variant(self, variant: str) -> str:
        """Return the execution kind for ``variant`` or the default kind."""
        return self.variant_shader_kinds.get(variant, self.shader_kind)

    def default_parameters(self) -> Dict[str, Any]:
        """Build a ``{name: value}`` dict from ``param_defs`` defaults.

        Used to seed a fresh ``EffectInstance`` (see ``EffectCatalog``) and
        as a fallback when a parameter is missing from a legacy serialised
        effect.
        """
        result: Dict[str, Any] = {}
        for pdef in self.param_defs:
            if pdef.hidden:
                # Hidden params still need a value (shaders may reference
                # them) but the UI skips them — seed the default here.
                result[pdef.name] = pdef.default if pdef.default is not None else (
                    0.0 if pdef.ptype == "float"
                    else 0 if pdef.ptype == "int"
                    else False if pdef.ptype == "bool"
                    else "" if pdef.ptype == "str"
                    else (pdef.labels[0] if (pdef.ptype == "option" and pdef.labels) else None)
                )
                continue
            defaults = pdef.make_default_parameters()
            result.update(defaults)
        return result


# ── P7-01：双后端权威 manifest（ADR 0004 决策 1/2）────────────────────────
#
# 编译入口（composition / 未来的 MSL emitter）只接受 manifest 形态的插件
# 输入：参数名 + 类型 + uniform 映射 + structural 参数。EffectSpec 是该
# manifest 的唯一权威载体——不新建第二份语义清单（ADR 0004 否决「新建 IR」）。

# ParamDef.ptype → GLSL uniform 类型的权威投影。
# 与 DCTL ABI（.tglsl @param / dctl_wrapper._PARAM_GLTYPE）同源：
# checkbox 与 select 在 ABI 中声明为 int（不是 bool）。MSL 侧的 struct
# 投影（msl_type/offset）在 P7-02 扩展到同一 manifest，不另立类型表。
GLSL_UNIFORM_TYPES: Dict[str, str] = {
    "float": "float",
    "int": "int",
    "bool": "int",      # DCTL checkbox ABI
    "option": "int",    # DCTL select ABI
    "color": "vec3",
    "vec2": "vec2",
    "vec3": "vec3",
}


@dataclass(frozen=True)
class UniformBinding:
    """manifest 单项：参数名 + GLSL 类型 + 最终 uniform 标识符 + structural 标记。"""

    param_name: str
    glsl_type: str
    uniform_name: str  # 已含实例前缀的最终 GLSL 标识符（如 u_P0_mix_amount）
    structural: bool = False


@dataclass(frozen=True)
class PluginBinding:
    """编译入口的插件权威输入：EffectSpec（manifest 载体）+ 实例状态。

    ADR 0004 决策 1：插件 shader 以 manifest 进入编译器；原始 instance
    dict 不得直接进入编译入口（编译器收到即 TypeError——G5 显式失败，
    禁止静默降级）。``values`` 是实例当前参数值（var_name → value），
    仅用于 structural 缓存键与运行期 uniform 上传，不参与代码生成。
    """

    spec: EffectSpec
    enabled: bool = True
    values: Mapping[str, Any] = field(default_factory=dict)
    # 原始 .tglsl @param 默认值（var_name → raw default，未经 ParamDef
    # 转换）。select 的默认是 int 索引（ParamDef.default 是 label 字符串
    # ——UI 侧约定），运行期 uniform 上传回退时需要原始 int。
    defaults: Mapping[str, Any] = field(default_factory=dict)
    # 本插件在 layer 插件列表中的原始位置。编译器以它为 uniform/函数名
    # 前缀（u_P{index}_ / process_P{index}），与运行期 uniform 上传侧的
    # enumerate 对齐；加载失败被剔除的插件仍须占位，不得让后续插件前移。
    index: int = 0

    def uniform_manifest(self, prefix: str = "") -> Tuple[UniformBinding, ...]:
        """派生带实例前缀的 uniform manifest。

        顺序 = spec.param_defs 顺序（即 .tglsl @param 声明顺序）；
        任一参数无 GLSL 类型投影 → ValueError（G5：manifest 校验失败
        必须显式报错，禁止静默丢参数）。
        """
        structural = set(self.spec.structural_param_names)
        bindings: list[UniformBinding] = []
        for pdef in self.spec.param_defs:
            glsl_type = GLSL_UNIFORM_TYPES.get(pdef.ptype)
            if glsl_type is None:
                raise ValueError(
                    f"EffectSpec '{self.spec.effect_type}' 参数 '{pdef.name}'"
                    f"（ptype='{pdef.ptype}'）无 GLSL uniform 类型投影；"
                    "manifest 校验失败（G5），不得静默丢参数"
                )
            bindings.append(UniformBinding(
                param_name=pdef.name,
                glsl_type=glsl_type,
                uniform_name=prefix + self.spec.uniform_name(pdef.name),
                structural=pdef.name in structural,
            ))
        return tuple(bindings)
