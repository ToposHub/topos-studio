"""DCTL (.tglsl) → EffectSpec shader wrapper.

The Color page runs DCTL plugins through ``shader_compiler.ShaderPipeline``
which fuses many plugins into one big shader with per-layer/per-plugin
namespacing (``L{i}_P{p_idx}_{var}``). That path is Color-page specific
and tightly coupled to the layer/composition model.

This module produces a **standalone fragment shader** for a single DCTL
plugin so the same ``.tglsl`` source can run through the cross-page
``EffectPassRunner`` — letting the Edit page drop a ``.tglsl`` plugin
onto a clip and have it behave like the built-in blur/sharpen filters.

Contract with EffectPassRunner (see ``effect_pass_runner.py``):
- Vertex shader is ``_PASSTHROUGH_VERTEX`` (``in_vert``/``in_uv`` → ``v_uv``).
- Fragment shader receives ``uniform sampler2D u_texture`` and ``in vec2 v_uv``.
- Each ``@param`` becomes a ``uniform`` named ``u_{var_name}``:
    slider   → ``uniform float``
    checkbox → ``uniform int``    (0/1)
    color    → ``uniform vec3``
    select   → ``uniform int``    (option index)

Cross-page ABI aliasing (L02, upgrade plan L):
- Plugins are authored for the Color composition contract: they reference
  parameters by **bare name** (the Color compiler rewrites them to
  ``u_P<n>_<name>``) and use the engine-injected uniforms
  ``u_input_tex`` / ``u_tex_size`` / ``u_aspect_ratio``. None of those
  spellings exist in the standalone runner contract, so without a rewrite
  every real plugin fails to compile here (undeclared identifier →
  compile failure → silent no-op; see L00 ledger §5.1).
- This wrapper therefore rewrites, in the plugin body only (the metadata
  block is already stripped):
    ``<param>``            → ``u_<param>``       (per @param, \b-word regex)
    ``u_input_tex``        → ``u_texture``
    ``u_tex_size``         → ``u_resolution``
    ``u_aspect_ratio``     → ``(u_resolution.x / u_resolution.y)``
  This mirrors the Color-side per-parameter ``\b`` rewrite semantics
  (shader_compiler.py AUD-0832 lineage), one program per plugin — no
  layer index is needed.
- A @param whose name collides with a GLSL keyword/builtin (or with the
  ``process()`` signature parameters) is rejected with ``ValueError`` —
  the same policy the Color compiler enforces (a bare-name rewrite of
  e.g. ``mix`` would corrupt builtin calls).

The wrapper is pure string manipulation — no GL context needed — so it
can be unit-tested without a GPU.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


# ──────────────────────────────────────────────────────────────────────────
# @param type → GLSL uniform declaration
# ──────────────────────────────────────────────────────────────────────────

_PARAM_GLTYPE = {
    "slider": "float",
    "checkbox": "int",
    "color": "vec3",
    "select": "int",
}

# Color 合成管线的引擎注入 uniform → 本 runner 约定的别名（L02）。
_ENGINE_UNIFORM_ALIASES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bu_input_tex\b"), "u_texture"),
    (re.compile(r"\bu_tex_size\b"), "u_resolution"),
    # 宽高比无独立 uniform：由分辨率推导（W/H，与 composition_uniforms 同口径）
    (re.compile(r"\bu_aspect_ratio\b"), "(u_resolution.x / u_resolution.y)"),
)

# Color composition 宿主注入的共享 helper（shader_nodes.COMMON_DEFINITIONS 原文）。
# 插件按 PLUGIN_DEV_GUIDE 依赖宿主提供 rgb2hsv/hsv2rgb 且不得自定义（重定义编译错）；
# standalone runner 同样必须供给。仅在插件源码引用且未自定义时注入。
_HOST_HELPER_PRELUDE = """
// Convert RGB to HSV
vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));

    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

// Convert HSV to RGB
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""

_HOST_HELPERS = ("rgb2hsv", "hsv2rgb", "fast_blur")

# fast_blur 依赖 blur_rand（shader_nodes.py 同源；测试钉住字节级一致防漂移）。
_HOST_PRELUDE_FAST_BLUR = """
float blur_rand(vec2 co) {
    return fract(sin(dot(co.xy ,vec2(12.9898,78.233))) * 43758.5453);
}

vec3 fast_blur(sampler2D tex, vec2 uv, float intensity, float radius, vec2 tex_size) {
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = (intensity * radius * scale) / 50.0;
    if (r <= 0.0) return texture(tex, uv).rgb;

    vec3 result = vec3(0.0);
    float total_weight = 0.0;
    float noise = blur_rand(uv) * 6.28;

    // 12 samples in a dithered spiral pattern (was 16): LINEAR filtering plus
    // per-pixel jitter keeps the smoothed disk visually equivalent.
    for (int i = 0; i < 12; i++) {
        float theta = float(i) * 2.39996 + noise;
        float dist = sqrt(float(i) / 12.0) * r;
        vec2 offset = vec2(cos(theta), sin(theta)) * dist / tex_size;
        result += texture(tex, uv + offset).rgb;
        total_weight += 1.0;
    }

    return result / total_weight;
}
"""


def _inject_host_helpers(src: str) -> str:
    """按需注入宿主共享 helper（引用且未自定义时；自定义属契约违规，显式报错）。

    PLUGIN_DEV_GUIDE 禁止插件自定义这些函数（Color composition 会重定义
    编译错）；因此"引用了但没定义"→ 注入，"自己定义了"→ 报错。
    未被使用的函数由链接器自动剔除，注入无成本。
    """
    referenced = [h for h in _HOST_HELPERS if re.search(rf"\b{h}\s*\(", src)]
    if not referenced:
        return src
    locally_defined = [
        h for h in _HOST_HELPERS
        if re.search(rf"\b(?:vec3|vec4|float)\s+{h}\s*\(", src)
    ]
    if locally_defined:
        raise ValueError(
            f"插件自定义了宿主共享 helper {locally_defined}——"
            "PLUGIN_DEV_GUIDE 契约禁止（Color composition 侧同样重定义编译错）")
    prelude = _HOST_HELPER_PRELUDE
    if any(h in referenced for h in ("fast_blur",)):
        prelude += "\n" + _HOST_PRELUDE_FAST_BLUR
    return prelude + "\n" + src


def _builtin_identifiers() -> frozenset[str]:
    """GLSL 关键字/内建函数黑名单（沿用 Color 侧 shader_compiler 口径）。

    延迟导入避免 shared→color 的 import 期依赖；shader_compiler 缺失或
    结构变化时退化为本文件维护的最小集合（fail-open 于名称，fail-closed
    于编译——碰撞名在真实编译处仍然报错）。
    """
    try:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        return _GLSL_RESERVED_IDENTIFIERS
    except Exception:  # pragma: no cover - 结构变化时兜底
        return frozenset({
            "mix", "clamp", "smoothstep", "texture", "textureSize", "dot", "pow",
            "min", "max", "mod", "step", "length", "normalize", "floor", "abs",
            "exp", "exp2", "sqrt", "fract", "sin", "cos", "tan", "atan",
            "float", "int", "bool", "vec2", "vec3", "vec4", "uniform", "const",
            "return", "if", "else", "for", "while", "break", "continue",
        })


def _process_signature_param_names(code: str) -> frozenset[str]:
    """提取 ``process(...)`` 形参名（对齐 shader_compiler AUD-0832 语义）。"""
    match = re.search(r"\bprocess\s*\(([^)]*)\)", code)
    if match is None:
        return frozenset()
    names = []
    for param in match.group(1).split(","):
        tokens = param.replace("in ", " ").replace("const ", " ").split()
        if tokens:
            names.append(tokens[-1])
    return frozenset(names)


def _alias_plugin_source(process_src: str, params: List[Dict[str, Any]]) -> str:
    """把 Color 约定的插件源码改写为本 runner 的 standalone 约定（L02）。"""
    reserved = _builtin_identifiers() | _process_signature_param_names(process_src)
    src = process_src
    for p in params:
        if p.get("type") not in _PARAM_GLTYPE:
            continue  # separator 等 UI-only 类型无 shader 存在
        var = p.get("var_name", "")
        if not var:
            continue
        if var in reserved:
            raise ValueError(
                f"@param {var!r} 与 GLSL 保留字/process 形参同名，"
                "跨页改写会破坏内建调用（Color 侧 shader_compiler 同策略拒绝）")
        src = re.sub(rf"\b{re.escape(var)}\b", f"u_{var}", src)
    for pattern, replacement in _ENGINE_UNIFORM_ALIASES:
        src = pattern.sub(replacement, src)
    return src


def _process_function_source(plugin_src: str) -> str:
    """Extract the ``process(vec3, vec2)`` function body from plugin source.

    The PLUGIN_DEV_GUIDE contract requires every plugin to define
    ``vec3 process(vec3 color, vec2 uv)``. We keep that function verbatim
    (including any helper functions / constants the author placed around
    it) so the wrapper just prepends uniforms + appends ``main()``.

    Strategy: strip the metadata comment block (everything between the
    ``// --- Topos Plugin Metadata ---`` markers, if present) and return
    the rest. We don't try to isolate just the ``process`` function —
    authors may define helper functions above it, which the GLSL
    compiler needs to see.
    """
    # Drop the metadata block if the file uses the conventional markers.
    src = re.sub(
        r"//\s*---\s*Topos Plugin Metadata\s*---.*?//\s*-{5,}\s*$",
        "",
        plugin_src,
        count=1,
        flags=re.DOTALL | re.MULTILINE,
    )
    return src.strip()


def _uniform_declarations(params: List[Dict[str, Any]]) -> str:
    """Build ``uniform`` declarations for every @param.

    Args:
        params: list of parsed param dicts from PluginManager (each has
            ``type`` in {slider, checkbox, color, select}, ``var_name``,
            and a ``default``).

    Returns:
        GLSL source lines like ``uniform float u_blur_radius;``.
    """
    lines: list[str] = []
    for p in params:
        ptype = p.get("type")
        gltype = _PARAM_GLTYPE.get(ptype)
        if gltype is None:
            # Unknown param types (e.g. 'separator') are UI-only and
            # have no shader presence — skip them.
            continue
        var = p.get("var_name", "")
        if not var:
            continue
        lines.append(f"uniform {gltype} u_{var};")
    return "\n".join(lines)


def wrap_dctl_process(plugin_src: str, params: List[Dict[str, Any]]) -> str:
    """Wrap a single DCTL plugin's source into a complete fragment shader.

    The returned source is ready for ``EffectPassRunner._compile_spec``
    (it pairs with ``_PASSTHROUGH_VERTEX`` and declares the runner's
    expected ``u_texture`` / ``v_uv`` / ``fragColor`` contract).

    Args:
        plugin_src: the raw ``.tglsl`` file content (metadata + process).
        params: the parsed ``@param`` list (from PluginManager).

    Returns:
        A complete ``#version 330`` fragment shader string.
    """
    process_src = _inject_host_helpers(
        _alias_plugin_source(_process_function_source(plugin_src), params))
    uniforms = _uniform_declarations(params)

    # The process() function takes (vec3 color, vec2 uv) — feed it the
    # sampled texture RGB and the uv. Plugins are responsible for any
    # alpha handling; we pass through the original alpha so the effect
    # doesn't accidentally zero out transparency.
    return f"""#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
in vec2 v_uv;
out vec4 fragColor;

{uniforms}

{process_src}

void main() {{
    vec4 src = texture(u_texture, v_uv);
    vec3 result = process(src.rgb, v_uv);
    fragColor = vec4(result, src.a);
}}
"""


def default_uniform_values(params: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Map each @param's default to the ``u_{var_name}`` uniform value.

    AUD-26302: 文档更正——生产代码当前**不**经过本函数：EffectCatalog
    经 ``ParamDef`` 默认值走 ``EffectSpec.default_parameters()``，runner
    的插件打包器按各自逻辑取默认值。本函数保留为纯映射助手，供测试
    与外部工具复用；不要假设修改它会改变运行时 uniform 默认值。

    - slider   → float
    - checkbox → int
    - color    → (r, g, b) tuple
    - select   → int (option index)
    """
    out: Dict[str, Any] = {}
    for p in params:
        ptype = p.get("type")
        var = p.get("var_name", "")
        if not var:
            continue
        default = p.get("default")
        if ptype == "slider":
            out[f"u_{var}"] = float(default) if default is not None else 0.0
        elif ptype == "checkbox":
            out[f"u_{var}"] = int(default) if default is not None else 0
        elif ptype == "color":
            if isinstance(default, (list, tuple)) and len(default) >= 3:
                out[f"u_{var}"] = (float(default[0]), float(default[1]), float(default[2]))
            else:
                out[f"u_{var}"] = (1.0, 1.0, 1.0)
        elif ptype == "select":
            out[f"u_{var}"] = int(default) if default is not None else 0
    return out
