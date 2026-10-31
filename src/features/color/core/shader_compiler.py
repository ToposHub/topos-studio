import re
import logging
from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Iterable, List

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import moderngl

logger = logging.getLogger(__name__)
from src.features.color.core.shader_nodes import (COMMON_DEFINITIONS, GRADING_SNIPPET,
                                    CURVES_SNIPPET, MIXER_SNIPPET, LUT3D_SNIPPET,
                                    WARPER_SNIPPET, BLEND_MODES_SNIPPET,
                                    QUALIFIER_SNIPPET, FILM_GRAIN_SNIPPET,
                                    SOFT_CLIP_SNIPPET, CURVES_SOFT_CLIP_SNIPPET,
                                    BLUR_SHARPEN_SNIPPET)
from src.features.color.core.plugin_manager import get_plugin_manager
from src.features.color.core.composition_uniforms import (
    build_composition_uniform_manifest,
)
from src.features.shared.effects.effect_spec import EffectSpec, PluginBinding, ShaderKind

# 编译诊断：编译失败时上报运行时报错（不含路径/源码）供 AI 助手只读查看。
from src.shared.render_diagnostics import (
    CODE_SHADER_COMPILE_FAILED,
    CODE_SHADER_UNDECLARED_UNIFORM,
    SOURCE_COMPOSITION,
    record_compile_failure,
)


# AUD-0832：GLSL 330 保留字/常用内建函数与类型/composition 宿主管线
# 标识符。@param 名与它们同名时，全文 \b 名字盲替换会改写内建调用或
# process() 签名形参（与 uniform 声明形成重定义/遮蔽），编译失败或
# 语义被破坏——构建期按此清单显式拒绝并给出诊断。
_GLSL_RESERVED_IDENTIFIERS = frozenset({
    # 关键字与类型
    "attribute", "const", "bool", "break", "bvec2", "bvec3", "bvec4",
    "continue", "discard", "do", "double", "dvec2", "dvec3", "dvec4",
    "else", "false", "float", "for", "if", "in", "inout", "int",
    "isampler2D", "isampler3D", "isamplerCube", "ivec2", "ivec3", "ivec4",
    "layout", "mat2", "mat3", "mat4", "mat2x2", "mat2x3", "mat2x4",
    "mat3x2", "mat3x3", "mat3x4", "mat4x2", "mat4x3", "mat4x4",
    "out", "precision", "return", "sampler1D", "sampler2D", "sampler3D",
    "samplerCube", "sampler1DShadow", "sampler2DShadow", "sampler2DArray",
    "struct", "true", "uint", "uniform", "usampler2D", "usampler3D",
    "usamplerCube", "using", "uvec2", "uvec3", "uvec4", "varying",
    "vec2", "vec3", "vec4", "void", "while", "highp", "mediump", "lowp",
    # 常用内建函数
    "abs", "acos", "all", "any", "asin", "atan", "ceil", "clamp", "cos",
    "cross", "degrees", "dFdx", "dFdy", "distance", "dot", "equal",
    "exp", "exp2", "faceforward", "floor", "fract", "fwidth",
    "greaterThan", "greaterThanEqual", "inversesqrt", "inverse", "length",
    "lessThan", "lessThanEqual", "log", "log2", "matrixCompMult", "max",
    "min", "mix", "mod", "normalize", "not", "notEqual", "pow", "radians",
    "reflect", "refract", "sign", "sin", "smoothstep", "sqrt", "step",
    "tan", "texture", "textureLod", "textureOffset", "textureProj",
    "texelFetch", "transpose", "trunc",
    # composition 宿主管线标识符（main 形参链/片段输入/局部变量/process）
    "color", "uv", "texcoord", "v_texcoord", "f_color", "tex_color",
    "mask", "graded", "blended", "process",
})


def _process_signature_param_names(code: str) -> frozenset:
    """提取插件 ``process(...)`` 形参名（AUD-0832）。

    @param 名与签名形参（如 ``vec3 process(vec3 color, vec2 uv)`` 的
    color/uv）同名时，\b 改写会重命名形参并与命名空间 uniform 声明
    形成重定义——必须按插件粒度动态加入黑名单。
    """
    match = re.search(r'\bprocess\s*\(([^)]*)\)', code)
    if match is None:
        return frozenset()
    names = []
    for param in match.group(1).split(','):
        tokens = param.replace('in ', ' ').replace('const ', ' ').split()
        if tokens:
            names.append(tokens[-1])
    return frozenset(names)


def plugin_binding_from_instance(instance: dict) -> PluginBinding | None:
    """把 layer 内的插件 instance dict 解析为编译入口的权威 ``PluginBinding``。

    P7-01（ADR 0004 决策 1）：插件以 manifest（参数名 + 类型 + uniform 映射 +
    structural 参数）进入编译器；EffectSpec 是 manifest 的权威载体。param_defs
    复用 effect_catalog 的 ``_dctl_param_to_pdef``（单一转换来源，与 Edit 页
    catalog 完全一致）；shader 源保留 **RAW 形态**——composition 的命名空间
    由 GL emitter 内部 re.sub 承担（正则改写是 GLSL emitter 实现细节，
    MSL emitter 禁止依赖）。

    返回 None = 插件无法进入编译（无 plugin_id / 非 dctl / 未找到 / 无
    shader 源）。调用方必须记录显式诊断（G5：禁止静默丢插件）——
    用 :func:`plugin_bindings_from_instances`。
    """
    if not isinstance(instance, dict):
        raise TypeError(
            f"插件实例必须是 dict，得到 {type(instance).__name__}"
        )
    plugin_id = instance.get("plugin_id")
    if not plugin_id:
        return None
    if instance.get("type", "dctl") != "dctl":
        return None
    plugin_data = get_plugin_manager().get_plugin(plugin_id)
    if not plugin_data or not plugin_data.get("shader_code"):
        return None

    # 延迟导入：effect_catalog 顶层较重，且只在插件路径需要。
    from src.features.shared.effects.effect_catalog import _dctl_param_to_pdef
    from src.features.shared.effects.plugin_identity import compute_content_hash

    raw_params = plugin_data.get("params", []) or []
    param_defs = tuple(
        p for p in (_dctl_param_to_pdef(param) for param in raw_params)
        if p is not None
    )
    raw_src = plugin_data["shader_code"]
    spec = EffectSpec(
        effect_type=f"plugin:{plugin_id}",
        name=plugin_data.get("name", plugin_id),
        category="plugin",
        param_defs=param_defs,
        shader_kind=ShaderKind.DCTL_PROCESS,
        # RAW 源（非 dctl_wrapper 包装形态）：spec 只承载 manifest 语义，
        # 不承载可执行源；包装形态属于 Edit 页 EffectPassRunner。
        shader_sources=(raw_src,),
        dispatch_key=f"plugin:{plugin_id}",
        # @param var_name 即 uniform 名（emitter 加实例前缀），恒等映射。
        param_to_uniform={},
        structural_param_names=tuple(
            param["var_name"] for param in raw_params
            if param.get("type") == "select" and param.get("var_name")
        ),
        preview_cost=plugin_data.get("preview_quality_level") or "medium",
        plugin_id=f"plugin:{plugin_id}",
        plugin_version=str(plugin_data.get("version", "") or ""),
        plugin_content_hash=compute_content_hash((raw_src,)),
        plugin_source_uri=str(plugin_data.get("source_uri", "") or ""),
    )
    return PluginBinding(
        spec=spec,
        enabled=bool(instance.get("enabled", True)),
        # 拷贝：binding 生命周期与 instance 解耦（structural 缓存键读取）。
        values=dict(instance.get("params", {}) or {}),
        # 原始默认值：select 的 uniform 需要 int 索引（ParamDef.default
        # 已转为 label），上传回退时用。
        defaults={
            param["var_name"]: param.get("default")
            for param in raw_params
            if param.get("var_name")
        },
    )


def plugin_bindings_from_instances(instances: Iterable[dict] | None) -> list[PluginBinding]:
    """批量解析插件实例；无法进入编译的实例记录显式 warning（G5）。

    返回列表与输入等长对齐（不可解析位置为缺口），``binding.index``
    始终是原始列表位置——uniform/函数名前缀与运行期上传侧（按原始
    enumerate）保持一致，加载失败的插件仍占位。
    """
    bindings: list[PluginBinding] = []
    for p_idx, instance in enumerate(instances or []):
        binding = plugin_binding_from_instance(instance)
        if binding is None:
            pid = instance.get("plugin_id") if isinstance(instance, dict) \
                else type(instance).__name__
            logger.warning(
                "插件无法进入 composition 编译（无 plugin_id / 非 dctl / "
                f"未找到 / 无 shader 源）：plugin_id={pid}（位置 {p_idx}）"
            )
            continue
        bindings.append(replace(binding, index=p_idx))
    return bindings


class ShaderPipeline:
    """
    Dynamically compiles GLSL composition shaders from the current layer stack.

    P7-01：唯一生产路径是 :meth:`build_composition_shader`（逐层 composition
    pass）。旧的 build_shader 融合路径因缺失 BLUR_SHARPEN_SNIPPET 自始
    编译失败（台账 0004a 死路径发现），已随 P7-01 删除。
    """
    def __init__(self, ctx: "moderngl.Context") -> None:
        self.ctx = ctx
        # AUD-0831：容量上限的 LRU。结构键是 8 个布尔/结构维度 × 插件
        # 参数组合的笛卡尔积，长调色会话反复开关效果会无限累积 GL
        # program（driver 侧驻留内存），此前仅 release() 全量释放。
        self.comp_cache: "OrderedDict[Any, Any]" = OrderedDict()

    #: LRU 容量：超出按最久使用淘汰，被逐出的 program 即时 release
    #: （_evict_overflow 只在 build_composition_shader 内调用，即渲染
    #: 线程/GL 上下文持有方，与 program 创建同上下文）。
    MAX_COMP_CACHE_PROGRAMS = 64

    def _evict_overflow(self) -> None:
        """AUD-0831：超出容量时按 LRU 淘汰并即时释放被逐出的 program。"""
        while len(self.comp_cache) > self.MAX_COMP_CACHE_PROGRAMS:
            _evicted_key, evicted = self.comp_cache.popitem(last=False)
            try:
                evicted.release()
            except Exception:
                logger.debug("Failed to release evicted shader program",
                             exc_info=True)

    def release(self) -> None:
        """Release every program owned by this pipeline.

        Composition shaders are cached by structural layer configuration and
        live in the renderer's GL context; they must be released explicitly
        during render-thread shutdown — relying on Python finalization leaves
        their GL objects alive after a Color workspace is closed.  Dedup by
        identity: two cache keys may reference one program object.
        """
        programs = {id(program): program for program in self.comp_cache.values()}
        for program in programs.values():
            try:
                program.release()
            except Exception:
                logger.debug("Failed to release shader program", exc_info=True)
        self.comp_cache.clear()

    @staticmethod
    def has_active_mixer_params(params: Any) -> bool:
        """Return True only when RGB Mixer contains at least one non-zero adjustment."""
        if not isinstance(params, dict):
            return False
        for values in params.values():
            if not isinstance(values, (list, tuple)) or len(values) < 3:
                continue
            try:
                if any(abs(float(v)) > 1.0e-6 for v in values[:3]):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def has_active_curves_lut(lut: Any) -> bool:
        """Return True only when an RGB curves LUT is not the identity curve."""
        if lut is None:
            return False
        try:
            arr = np.asarray(lut, dtype=np.float32)
            if arr.shape[0] != 256:
                return True
            if arr.ndim == 3:
                samples = arr[:, 0, :]
            elif arr.ndim == 2:
                samples = arr[:, :1] if arr.shape[1] == 1 else arr
            elif arr.ndim == 1:
                samples = arr[:, None]
            else:
                return True
            if float(np.max(samples)) > 1.1:
                samples = samples / 255.0
            expected = (np.arange(256, dtype=np.float32) / 255.0)[:, None]
            return not np.allclose(samples, expected, atol=1.0e-4)
        except Exception:
            return True

    @staticmethod
    def has_active_hsl_curve_lut(lut: Any) -> bool:
        """Return True only when an HSL curves LUT differs from the identity curves."""
        if lut is None:
            return False
        try:
            arr = np.asarray(lut, dtype=np.float32)
            if arr.shape != (6, 256):
                return True
            expected = np.ones((6, 256), dtype=np.float32)
            expected[1, :] = 0.0
            return not np.allclose(arr, expected, atol=1.0e-4)
        except Exception:
            return True

    def build_composition_shader(
        self,
        has_curves: bool = False,
        has_warper: bool = False,
        has_lut3d: bool = False,
        has_soft_clip: bool = False,
        grain_enabled: bool = False,
        has_qualifier: bool = False,
        has_hsl_curve: bool = False,
        has_mixer: bool = False,
        has_curves_soft_clip: bool = False,
        plugins: Iterable[PluginBinding] | None = None,
        *,
        request_id: Any = None,
        layer_id: Any = None,
    ) -> "moderngl.Program | None":
        """
        Builds a simplified composition shader for a single layer pass.
        Expects: u_input_tex (source), u_matte_tex (pre-computed mask).

        Args:
            plugins: P7-01 权威输入——:class:`PluginBinding` 序列（用
                :func:`plugin_bindings_from_instances` 从插件实例构造）。
            manifest（EffectSpec）驱动 uniform 声明、命名空间改写与
            结构化缓存键；原始 instance dict 进入编译入口即 TypeError
            （G5：显式失败，禁止静默降级）。
            has_curves_soft_clip: 曲线面板四参数柔化裁切（指数软膝）。
                旗标只在参数非恒等时开启（默认恒等映射无需编译进管线）。
            request_id/layer_id: 可选诊断关联上下文，不参与 shader 缓存键。
        """
        bindings: list[PluginBinding] = list(plugins) if plugins else []
        for binding in bindings:
            if not isinstance(binding, PluginBinding):
                raise TypeError(
                    "build_composition_shader 的 plugins 必须是 PluginBinding "
                    f"（P7-01 权威 manifest），得到 {type(binding).__name__}；"
                    "用 plugin_binding_from_instances() 构造"
                )

        # 结构化缓存键（P7-01）：manifest 驱动。CRITICAL: enabled 进键（插件
        # 开关触发重编译）；只有 structural（select）参数值进键——slider 等
        # 值参数走 uniform，进键会让每次拖动都重编译。
        def _binding_structural_params(binding: PluginBinding) -> tuple:
            structural = []
            for ub in binding.uniform_manifest():
                if ub.structural and ub.param_name in binding.values:
                    structural.append((ub.param_name, binding.values[ub.param_name]))
            return tuple(sorted(structural))

        plugin_key = tuple(
            (binding.spec.plugin_id or binding.spec.effect_type,
             # AUD-0830：插件内容身份进键——spec 携带 plugin_content_hash
             # （FX-M07），重扫描/编辑插件文件后 hash 变化触发重编译，
             # 不再命中旧编译产物渲染旧效果。
             binding.spec.plugin_content_hash or "",
             binding.enabled, _binding_structural_params(binding))
            for binding in bindings
        ) if bindings else ()
        key = (has_curves, has_warper, has_lut3d, has_soft_clip, grain_enabled, has_qualifier, has_hsl_curve, has_mixer, has_curves_soft_clip, plugin_key)
        if key in self.comp_cache:
            # AUD-0831：LRU 命中刷新新旧顺序
            self.comp_cache.move_to_end(key)
            return self.comp_cache[key]

        # P7-02：uniform 声明由单一 manifest 驱动（决策 2）。header 区段
        # （输入/遮罩 sampler）在 SNIPPET 之前、body 区段（基础调色/可选
        # sampler/grain/插件）在 SNIPPET 之后——与重构前手写文本的物理
        # 顺序逐行一致（G1 位级门禁）。
        uniform_manifest = build_composition_uniform_manifest(
            has_curves=has_curves, has_hsl_curve=has_hsl_curve,
            has_warper=has_warper, has_lut3d=has_lut3d,
            grain_enabled=grain_enabled, bindings=bindings)

        shader_code = [
            "#version 330",
            *uniform_manifest.declarations("header"),
            "in vec2 v_texcoord;",
            "out vec4 f_color;",
            COMMON_DEFINITIONS,
            GRADING_SNIPPET,
            CURVES_SNIPPET,
            MIXER_SNIPPET,
            LUT3D_SNIPPET,
            WARPER_SNIPPET,
            BLEND_MODES_SNIPPET,
            BLUR_SHARPEN_SNIPPET,  # Must be before FILM_GRAIN_SNIPPET (declares u_tex_size)
            FILM_GRAIN_SNIPPET,
            SOFT_CLIP_SNIPPET,
            CURVES_SOFT_CLIP_SNIPPET,
            QUALIFIER_SNIPPET,
        ]
        shader_code += uniform_manifest.declarations("body")
        # Phase 2: Qualifier uniforms removed - qualifier is now computed in matte generation

        # P0-COL-01：插件体内允许引用的 uniform 全集（manifest 含 header
        # 采样器、snippet-owned 与已声明的插件命名空间 uniform）。
        host_uniform_names = {d.name for d in uniform_manifest.decls}

        # Plugin Function Definitions（P7-01：命名空间改写是 GL emitter
        # 内部实现细节——manifest 提供参数名与前缀；MSL emitter 禁止依赖
        # 本机制，其命名空间由生成的 struct/函数前缀承担，见 ADR 0004 决策 1。
        # 与旧实现的差异：separator 等无 shader 形态的参数不参与改写——
        # 它们没有 uniform 声明，旧实现对它们改写只会制造未声明标识符。）
        for binding in bindings:
            if not binding.enabled:
                continue
            p_idx = binding.index
            code = binding.spec.shader_sources[0]
            plugin_manifest = binding.uniform_manifest(prefix=f"u_P{p_idx}_")
            # AUD-0832：改写前校验 @param 名。名字盲 \b 替换作用于整个
            # 插件源文本——param 名若与 GLSL 保留字/内建、宿主管线标识
            # 符或 process() 自身形参同名（如 mix/texture/color/uv），
            # 内建调用与函数签名会被一并改写：轻则未声明/重定义编译失
            # 败，重则 uniform 被形参遮蔽、语义静默破坏。构建期显式拒
            # 绝并写入诊断，不再产出含糊的 driver 报错。
            signature_params = _process_signature_param_names(code)
            reserved_hits = sorted({
                ub.param_name for ub in plugin_manifest
                if ub.param_name in _GLSL_RESERVED_IDENTIFIERS
                or ub.param_name in signature_params
            })
            if reserved_hits:
                message = (
                    f"Plugin '{binding.spec.name}' declares @param name(s) "
                    f"{reserved_hits} that collide with GLSL reserved/builtin "
                    "or process() signature identifiers; the namespace "
                    "rewrite would corrupt them. Rename the @param variable."
                )
                logger.error(message)
                record_compile_failure(
                    source=SOURCE_COMPOSITION,
                    code=CODE_SHADER_COMPILE_FAILED,
                    subjects=[
                        binding.spec.plugin_id or binding.spec.effect_type
                        for binding in bindings
                    ],
                    message=message,
                    request_id=request_id,
                    layer_id=layer_id,
                    plugin_ids=[
                        item.spec.plugin_id or item.spec.effect_type
                        for item in bindings
                    ],
                )
                return None
            # Replace function name: process( -> process_P{p_idx}(
            code = re.sub(r'\bprocess\s*\(', f'process_P{p_idx}(', code)
            # Replace parameter variable names with namespaced uniforms
            for ub in plugin_manifest:
                code = re.sub(
                    r'\b' + re.escape(ub.param_name) + r'\b',
                    ub.uniform_name, code,
                )
            # P0-COL-01：改写后残留的 u_* 引用必须都是宿主已声明的 uniform。
            # 参数名的 \b 改写不会命中 `u_<param>`（'_' 是 word 字符），插件
            # 若按旧约定引用 u_strength 会留下未声明标识符 → GL 编译失败 →
            # 该层被渲染循环静默跳过。这里提前拒绝、指名插件与标识符，并把
            # 失败写入诊断缓冲（AI 助手/日志可检索），不再产生含糊的
            # driver 报错。
            # 注释里的 u_* 标识符（如文档写法 `u_P<n>_<name>`）不是代码
            # 引用——扫描前剥离 // 与 /* */ 注释（GLSL 无字符串字面量，
            # 剥离仅作用于本次检查，不参与后续编译源）。
            code_no_comments = re.sub(
                r'/\*.*?\*/|//[^\n]*', ' ', code, flags=re.S)
            undeclared = sorted({
                ident for ident in re.findall(
                    r'\bu_[A-Za-z0-9_]+\b', code_no_comments)
                if ident not in host_uniform_names
            })
            if undeclared:
                message = (
                    f"Plugin '{binding.spec.name}' references undeclared "
                    f"uniform(s) {undeclared}; reference @param values by "
                    f"bare name (namespaced at build time), or host uniforms "
                    f"{sorted(host_uniform_names)}"
                )
                logger.error(message)
                record_compile_failure(
                    source=SOURCE_COMPOSITION,
                    code=CODE_SHADER_UNDECLARED_UNIFORM,
                    subjects=[binding.spec.plugin_id or binding.spec.name],
                    message=message,
                    request_id=request_id,
                    layer_id=layer_id,
                    plugin_ids=[
                        item.spec.plugin_id or item.spec.effect_type
                        for item in bindings
                    ],
                )
                return None
            shader_code.append(f"// Plugin: {binding.spec.name}")
            shader_code.append(code)

        shader_code.append("void main() {")
        shader_code.append("    vec4 tex_color = texture(u_input_tex, v_texcoord);")
        shader_code.append("    vec3 color = tex_color.rgb;")
        shader_code.append("    float mask = texture(u_matte_tex, v_texcoord).r;")
        
        # Phase 2: Qualifier is now computed in matte generation (AsyncRenderer or ImageEngine._generate_full_res_matte)
        # Matte texture already contains qualifier + mask_shapes + ai_matte + blur/clean postprocess
        # No shader-side qualifier computation needed here
        
        # Apply Blur / Sharpen first to allow subsequent grading
        shader_code.append("    vec3 graded = apply_blur_sharpen(color, u_input_tex, v_texcoord, u_fx_mode, u_fx_sharpen, u_fx_texture, u_fx_halo, u_fx_radius, u_fx_iterations, u_tex_size, u_fx_enabled, u_blur_enabled, u_blur_mode, u_blur_intensity, u_blur_detail, u_blur_radius, u_blur_bokeh, u_blur_angle);")

        shader_code.append("    graded = apply_grading(graded, u_lift, u_gamma, u_gain, u_exposure, u_temp, u_tint, u_contrast, u_saturation, u_vibrance, u_shadow, u_highlights, u_mid_detail, u_sharpness, u_hue_shift, u_lift_saturation, u_gamma_saturation, u_gain_saturation);")

        if has_curves: shader_code.append("    graded = apply_curves(graded, u_curves_tex);")
        if has_hsl_curve: shader_code.append("    graded = apply_hsl_curve(graded, u_hsl_curve_tex);")

        if has_soft_clip:
            shader_code.append("    if (u_sc_enabled) {")
            shader_code.append("        graded = apply_soft_clip(graded, u_sc_high, u_sc_white_point, u_sc_shoulder_strength, u_sc_low, u_sc_black_point, u_sc_toe_strength, u_sc_exposure, u_sc_contrast);")
            shader_code.append("    }")
        # 曲线面板四参数柔化裁切（指数软膝）：编译期门控——只在参数非
        # 恒等时编译进管线（默认恒等映射保留超白/负值，无需执行）。
        if has_curves_soft_clip:
            shader_code.append("    graded = apply_curves_soft_clip(graded, u_cs_low_clip, u_cs_low_soft, u_cs_high_clip, u_cs_high_soft);")
        if has_mixer:
            shader_code.append("    graded = apply_mixer(graded, u_red_p, u_orange_p, u_yellow_p, u_green_p, u_aqua_p, u_blue_p, u_purple_p, u_magenta_p);")
        if has_warper: shader_code.append("    graded = apply_warper(graded, u_warper_tex, u_warper_grid_size);")
        if has_lut3d: shader_code.append("    graded = apply_3dlut(graded, u_lut_tex, u_lut_intensity);")

        # Apply Plugin Effects（P7-01：binding 由 from_instances 构造，
        # 「插件已加载且有源」在入口已保证——def/call 两循环天然镜像。）
        for binding in bindings:
            if not binding.enabled:
                continue
            shader_code.append(
                f"    graded = process_P{binding.index}(graded, v_texcoord);"
            )

        shader_code.append("    vec3 blended = apply_blend_mode(color, graded, u_blend_mode);")
        shader_code.append("    color = mix(color, blended, mask * u_opacity);")
        
        if grain_enabled:
            shader_code.append("    color = apply_film_grain(color, v_texcoord, u_grain_intensity, u_grain_size, u_grain_monochrome, u_grain_shadows, u_grain_midtones, u_grain_highlights, u_grain_seed, u_grain_aspect_ratio, u_tex_size);")
        
        shader_code.append("    f_color = vec4(color, tex_color.a);")
        shader_code.append("}")

        vertex_shader = """
        #version 330
        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;
        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """
        try:
            prog = self.ctx.program(vertex_shader=vertex_shader, fragment_shader="\n".join(shader_code))
            self.comp_cache[key] = prog
            # AUD-0831：新 program 入缓存后按容量淘汰（LRU 最旧端）
            self._evict_overflow()
            return prog
        except Exception as e:
            logger.error(f"Shader Compilation Error: {e}")
            # 上报到诊断缓冲（仅运行时报错 + 涉及的 plugin_id，不含路径/源码）。
            record_compile_failure(
                source=SOURCE_COMPOSITION,
                code=CODE_SHADER_COMPILE_FAILED,
                subjects=[
                    binding.spec.plugin_id or binding.spec.effect_type
                    for binding in bindings
                ],
                message=str(e),
                request_id=request_id,
                layer_id=layer_id,
                plugin_ids=[
                    binding.spec.plugin_id or binding.spec.effect_type
                    for binding in bindings
                ],
            )
            return None

    def _params_to_rgb(self, hue: float, sat: float, bright: float) -> List[float]:
        """Wheel Hue/Sat/Brightness → RGB 因子（P7-02 起实现移至
        composition_uniforms.params_to_rgb，此处保留委托兼容既有调用方）。"""
        from src.features.color.core.composition_uniforms import params_to_rgb
        return params_to_rgb(hue, sat, bright)
