"""Metal 效果宿主 ABI、参数缓冲布局、编译缓存键与设备能力指纹。

阶段 6.2 / 6.4 的公共契约层。转译器（``tglsl_msl_transpiler``）、MSL 效果
注册表（``msl_effect_registry``）与运行器（``metal_effect_runtime``）只通过
这里定义的常量与数据类协作，保证「生成的 MSL 源」与「上传的参数字节」
永远描述同一份布局。

宿主 ABI（``topos.tglsl-msl/1``）
    - ``texture(0)`` = 输入 ``u_input_tex``（``texture2d<float>``，可采样）；
    - ``texture(1)`` = 输出 ``u_output_tex``（``texture2d<float, access::write>``）；
    - ``buffer(0)`` = ``constant ToposFxParams&``（4 字节紧排的扁平结构体）；
    - sampler 不占插槽：由 kernel 源内的 ``constexpr sampler`` 显式声明，
      采样行为因此是编译产物的一部分并进入源码指纹；
    - 线程网格 ``(width, height, 1)``，``gid`` 即输出纹素坐标。

参数结构体布局沿用 composition 管线既有的 P6-06 规则（平铺标量、4 字节
紧排、向量逐分量展开），前两个字段恒为 ``u_tex_size_x`` / ``u_tex_size_y``，
其后按 ``@param`` 声明顺序每个参数一段：

    slider   → float   p_<name>
    checkbox → int     p_<name>      （DCTL checkbox ABI = int）
    select   → int     p_<name>      （DCTL select ABI = int 索引）
    color    → float   p_<name>_x / _y / _z

坐标约定（两种，均由 ABI 显式命名并进入缓存键）
    - ``gl_row0_is_v0``：纹素行号即 v 索引——与 ``EffectPassRunner`` 的
      全屏 quad（``v_uv`` 原点在 numpy 第 0 行）逐像素同构，对拍用；
    - ``top_down``：第 0 行是图像顶部，``uv.y = 0`` 对应插件契约里的
      「左下角」，采样坐标在 kernel 内翻转。两种约定精确互为上下镜像：
      ``top_down`` 的输出等于 ``gl_row0_is_v0`` 输出的上下翻转（恒等采样与纯
      uv 运算逐位相同；带偏移的采样差在门限内）。

线程边界：纯数据/字符串，无 GPU 依赖；只有 ``device_capability_fingerprint``
读取传入的 ``MTLDevice``（不编码命令）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from src.features.shared.effects.effect_spec import GLSL_UNIFORM_TYPES

HOST_ABI_ID = "topos.tglsl-msl"
HOST_ABI_VERSION = 1
HOST_ABI = f"{HOST_ABI_ID}/{HOST_ABI_VERSION}"

TEXTURE_SLOT_INPUT = 0
TEXTURE_SLOT_OUTPUT = 1
BUFFER_SLOT_PARAMS = 0

PARAMS_STRUCT_NAME = "ToposFxParams"
ENGINE_INPUT_TEX = "u_input_tex"
ENGINE_OUTPUT_TEX = "u_output_tex"
ENGINE_TEX_SIZE = "u_tex_size"
ENGINE_ASPECT_RATIO = "u_aspect_ratio"
ENGINE_SAMPLER = "u_sampler"
#: 纹素空间坐标（恒为 px / u_tex_size）。``top_down`` 约定下 ``uv`` 是它的上下镜像；
#: 插件在 ``uv`` 处的恒等采样直接用它，于是两种约定精确互为镜像。
ENGINE_TEX_UV = "t_uv"

COORD_GL_ROW0_IS_V0 = "gl_row0_is_v0"
COORD_TOP_DOWN = "top_down"
COORD_CONVENTIONS: Tuple[str, ...] = (COORD_GL_ROW0_IS_V0, COORD_TOP_DOWN)

#: 像素格式名 → (MTLPixelFormat 常量名, numpy dtype, 每像素分量数)。
PIXEL_FORMAT_RGBA16F = "rgba16f"
PIXEL_FORMAT_RGBA8 = "rgba8"
PIXEL_FORMAT_RGBA32F = "rgba32f"
PIXEL_FORMATS: Dict[str, Tuple[str, str, int]] = {
    PIXEL_FORMAT_RGBA16F: ("MTLPixelFormatRGBA16Float", "float16", 4),
    PIXEL_FORMAT_RGBA8: ("MTLPixelFormatRGBA8Unorm", "uint8", 4),
    # RGBA32Float 仅在支持该格式 filterable 的设备上可用于采样路径。
    PIXEL_FORMAT_RGBA32F: ("MTLPixelFormatRGBA32Float", "float32", 4),
}

#: sampler 规格标识（进入缓存键；kernel 内展开为 constexpr sampler）。
SAMPLER_LINEAR_CLAMP = "linear+clamp_to_edge"
SAMPLER_NEAREST_CLAMP = "nearest+clamp_to_edge"
SAMPLER_LINEAR_REPEAT = "linear+repeat"

#: ``MetalComputeContext.compile_kernel`` 当前不接受编译选项（内部以 ``None``
#: 调用 ``newLibraryWithSource_options_error_``，即运行时的默认 ``MTLCompileOptions``：
#: fast math 开启、默认语言版本）。缓存键仍然记录生效的编译参数，保证该 API
#: 一旦允许传选项，键随之变化而不是复用旧产物。实际默认值见
#: :func:`describe_default_compile_options`（对拍证据记录）。
COMPILE_OPTIONS_CONTEXT_DEFAULT = "context-default(MTLCompileOptions=nil; fast-math=default-on; language=default)"


def describe_default_compile_options() -> Dict[str, Any]:
    """读取运行时的默认 ``MTLCompileOptions`` 与操作系统版本（编译器身份的证据）。

    Metal 不暴露编译器版本号；可复现的身份是「系统版本 + 默认语言版本 + 数学
    模式 + 设备」。非 macOS 宿主返回 ``{"available": False}``。
    """
    facts: Dict[str, Any] = {"compile_options": COMPILE_OPTIONS_CONTEXT_DEFAULT}
    try:
        import platform

        facts["os_version"] = platform.mac_ver()[0]
        import Metal

        options = Metal.MTLCompileOptions.alloc().init()
        facts["fast_math_enabled"] = bool(options.fastMathEnabled())
        facts["math_mode"] = int(options.mathMode())
        facts["default_language_version"] = int(options.languageVersion())
        facts["available"] = True
    except Exception:  # noqa: BLE001 — 探测失败只影响证据，不影响编译
        facts["available"] = False
    return facts


def _sha256_text(text: str, length: int = 64) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def sampler_declaration(spec: str) -> str:
    """sampler 规格标识 → ``constexpr sampler`` 声明。"""
    if spec == SAMPLER_LINEAR_CLAMP:
        return "constexpr sampler u_sampler(coord::normalized, address::clamp_to_edge, filter::linear);"
    if spec == SAMPLER_NEAREST_CLAMP:
        return "constexpr sampler u_sampler(coord::normalized, address::clamp_to_edge, filter::nearest);"
    if spec == SAMPLER_LINEAR_REPEAT:
        return "constexpr sampler u_sampler(coord::normalized, address::repeat, filter::linear);"
    raise ValueError(f"未知 sampler 规格 {spec!r}")


def coord_uv_expression(convention: str) -> str:
    """坐标约定 → 由纹素中心 ``px`` 计算 ``uv`` 的 MSL 表达式。

    两种约定精确互为上下镜像：``top_down`` 的第 y 行与 ``gl_row0_is_v0`` 的第
    H-1-y 行算的是同一个算式 ``(H - 0.5 - y) / H``，而不是先算再相减——后者在
    fp32 里会差 1 个 ulp，使镜像对拍无法逐位成立。
    """
    if convention == COORD_GL_ROW0_IS_V0:
        return "px / u_tex_size"
    if convention == COORD_TOP_DOWN:
        return "float2(px.x / u_tex_size.x, (u_tex_size.y - px.y) / u_tex_size.y)"
    raise ValueError(f"未知坐标约定 {convention!r}（支持: {', '.join(COORD_CONVENTIONS)}）")


def sample_coord_expression(convention: str, uv_expr: str) -> str:
    """坐标约定 → 把插件语义的 ``uv`` 换算成纹素采样坐标的 MSL 表达式。"""
    if convention == COORD_GL_ROW0_IS_V0:
        return uv_expr
    if convention == COORD_TOP_DOWN:
        return f"float2(({uv_expr}).x, 1.0 - ({uv_expr}).y)"
    raise ValueError(f"未知坐标约定 {convention!r}（支持: {', '.join(COORD_CONVENTIONS)}）")


# ── 参数缓冲布局 ────────────────────────────────────────────────────────

# GLSL 类型 → (MSL 标量类型, 分量数, numpy 基类型)。与调色域
# composition_uniforms._MSL_SCALAR 同规则（P6-06：4 字节紧排、向量逐分量展开）。
MSL_SCALAR: Dict[str, Tuple[str, int, str]] = {
    "float": ("float", 1, "<f4"),
    "int": ("int", 1, "<i4"),
    "bool": ("int", 1, "<i4"),
    "vec2": ("float", 2, "<f4"),
    "vec3": ("float", 3, "<f4"),
    "vec4": ("float", 4, "<f4"),
}
_COMPONENTS = "xyzw"


@dataclass(frozen=True)
class ParamField:
    """一个参数（或引擎保留字段）在 ``ToposFxParams`` 中的投影。"""

    param_name: str                 # 为空串 = 引擎保留字段（u_tex_size）
    glsl_type: str
    fields: Tuple[str, ...]         # MSL struct 字段名，按声明顺序
    np_types: Tuple[str, ...]       # 与 fields 一一对应的小端 numpy 类型
    msl_scalar: str                 # "float" | "int"

    @property
    def byte_length(self) -> int:
        return 4 * len(self.fields)

    def struct_lines(self) -> Tuple[str, ...]:
        return tuple(f"    {self.msl_scalar} {name};" for name in self.fields)

    def msl_expr(self) -> str:
        """在 kernel 源内读取该参数的 MSL 表达式。"""
        if len(self.fields) == 1:
            return f"params.{self.fields[0]}"
        ctor = {"float": "float", "int": "int"}[self.msl_scalar] + str(len(self.fields))
        return f"{ctor}(" + ", ".join(f"params.{name}" for name in self.fields) + ")"


def _engine_tex_size_field() -> ParamField:
    return ParamField(param_name="", glsl_type="vec2",
                      fields=("u_tex_size_x", "u_tex_size_y"),
                      np_types=("<f4", "<f4"), msl_scalar="float")


@dataclass(frozen=True)
class ParamsLayout:
    """``ToposFxParams`` 的完整布局：MSL 结构体文本 + 对齐的 numpy dtype。"""

    fields: Tuple[ParamField, ...]

    @property
    def param_fields(self) -> Tuple[ParamField, ...]:
        return tuple(f for f in self.fields if f.param_name)

    @property
    def struct_text(self) -> str:
        lines = [f"struct {PARAMS_STRUCT_NAME} {{"]
        for field in self.fields:
            lines.extend(field.struct_lines())
        lines.append("};")
        return "\n".join(lines)

    @property
    def numpy_dtype(self) -> np.dtype:
        spec = []
        for field in self.fields:
            for name, np_type in zip(field.fields, field.np_types):
                spec.append((name, np_type))
        return np.dtype(spec)

    @property
    def byte_length(self) -> int:
        return int(self.numpy_dtype.itemsize)

    def field_for(self, param_name: str) -> Optional[ParamField]:
        """按参数名取字段；引擎保留字段（空名）不会被空串命中。"""
        for field in self.fields:
            if param_name and field.param_name == param_name:
                return field
        return None


def build_params_layout(param_defs: Sequence[Any]) -> ParamsLayout:
    """按 ``@param`` 声明顺序（即 ``EffectSpec.param_defs`` 顺序）生成布局。

    任一参数没有 GLSL 类型投影时显式 ``ValueError``（G5：manifest 校验失败
    不得静默丢参数——与 ``PluginBinding.uniform_manifest`` 同一策略）。
    """
    fields = [_engine_tex_size_field()]
    for pdef in param_defs:
        glsl_type = GLSL_UNIFORM_TYPES.get(pdef.ptype)
        if glsl_type is None:
            raise ValueError(
                f"参数 {pdef.name!r}（ptype={pdef.ptype!r}）无 GLSL uniform 类型投影；"
                "manifest 校验失败（G5），不得静默丢参数")
        scalar, count, np_type = MSL_SCALAR[glsl_type]
        if count == 1:
            names = (f"p_{pdef.name}",)
        else:
            names = tuple(f"p_{pdef.name}_{_COMPONENTS[i]}" for i in range(count))
        fields.append(ParamField(param_name=pdef.name, glsl_type=glsl_type,
                                 fields=names, np_types=(np_type,) * count,
                                 msl_scalar=scalar))
    return ParamsLayout(fields=tuple(fields))


def layout_fingerprint(layout: ParamsLayout) -> str:
    """布局指纹（字段名 + 类型），与 ``metal_abi`` 版本一起进入缓存键。"""
    canonical = json.dumps(
        [[f.param_name, f.glsl_type, list(f.np_types)] for f in layout.fields],
        ensure_ascii=True, separators=(",", ":"))
    return _sha256_text(canonical, 16)


# ── 编译缓存键（6.4）────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetalCompileKey:
    """一次 Metal 编译产物的完整身份。

    任一字段变化都意味着「旧产物不能代表新请求」，必须重新编译：
    源码指纹（生成的 MSL + 原始 GLSL）、宿主 ABI、编译参数、像素格式、
    设备能力、插件版本与内容/参数 schema 指纹、转译器与 helper 版本、
    坐标约定与 sampler 规格。
    """

    host_abi: str
    effect_id: str
    msl_fingerprint: str
    source_fingerprint: str
    plugin_version: str
    plugin_content_hash: str
    param_schema_hash: str
    layout_fingerprint: str
    compile_options: str
    pixel_format: str
    device_capability: str
    translator_version: str
    helper_versions: Tuple[Tuple[str, str], ...]
    coord_convention: str
    sampler_spec: str

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            [self.host_abi, self.effect_id, self.msl_fingerprint, self.source_fingerprint,
             self.plugin_version, self.plugin_content_hash, self.param_schema_hash,
             self.layout_fingerprint, self.compile_options, self.pixel_format,
             self.device_capability, self.translator_version,
             sorted(self.helper_versions), self.coord_convention, self.sampler_spec],
            ensure_ascii=True, separators=(",", ":"))
        return _sha256_text(canonical, 40)

    @property
    def kernel_name(self) -> str:
        """kernel 函数名 = 键摘要。

        ``MetalComputeContext.compile_kernel`` 以函数名为缓存键；把摘要编进
        函数名，不同键绝不会命中彼此的缓存产物。
        """
        return f"topos_fx_{self.digest[:20]}"

    def to_dict(self) -> dict:
        return {
            "host_abi": self.host_abi,
            "effect_id": self.effect_id,
            "msl_fingerprint": self.msl_fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "plugin_version": self.plugin_version,
            "plugin_content_hash": self.plugin_content_hash,
            "param_schema_hash": self.param_schema_hash,
            "layout_fingerprint": self.layout_fingerprint,
            "compile_options": self.compile_options,
            "pixel_format": self.pixel_format,
            "device_capability": self.device_capability,
            "translator_version": self.translator_version,
            "helper_versions": [list(h) for h in self.helper_versions],
            "coord_convention": self.coord_convention,
            "sampler_spec": self.sampler_spec,
            "digest": self.digest,
            "kernel_name": self.kernel_name,
        }


# ── 设备能力指纹 ────────────────────────────────────────────────────────


def device_capability_fingerprint(device: Any) -> str:
    """设备身份 + 能力的稳定指纹（进入缓存键；同型号同驱动得到相同值）。

    读取 ``name`` / ``registryID`` / ``hasUnifiedMemory`` / 支持的 GPU family /
    线程组内存上限 / argument buffer 支持。任一能力在测试替身上缺失时按
    「未知」记录而不是抛错——指纹只要求稳定，不要求完整。
    """
    facts: Dict[str, Any] = {}
    for name, getter in (
        ("name", lambda d: str(d.name())),
        ("registry_id", lambda d: int(d.registryID())),
        ("unified_memory", lambda d: bool(d.hasUnifiedMemory())),
        ("max_threadgroup_memory", lambda d: int(d.maxThreadgroupMemoryLength())),
        ("argument_buffers", lambda d: int(d.argumentBuffersSupport())),
    ):
        try:
            facts[name] = getter(device)
        except Exception:  # noqa: BLE001 — 能力探测失败 = 未知，不阻断编译
            facts[name] = None
    families = []
    try:
        import Metal  # pyobjc-framework-Metal

        for attr in sorted(dir(Metal)):
            if not attr.startswith("MTLGPUFamily") or attr.endswith("Available"):
                continue
            try:
                if bool(device.supportsFamily_(getattr(Metal, attr))):
                    families.append(attr[len("MTLGPUFamily"):])
            except Exception:  # noqa: BLE001
                continue
    except ImportError:
        families = ["unknown"]
    facts["gpu_families"] = families
    return _sha256_text(json.dumps(facts, sort_keys=True, ensure_ascii=True), 16)


# ── 编译消息精简 ────────────────────────────────────────────────────────


def trim_compile_message(text: str, *, max_errors: int = 4, max_chars: int = 600) -> str:
    """把 Metal 编译器输出精简为「位置 + 错误 + 出错源码行」。

    ``MetalComputeContext.compile_kernel`` 的错误文本形如
    ``MSL 编译失败（name）: program_source:10:9: error: ...``——第一条错误与
    前缀同行，所以按子串定位而不是要求整行以 ``program_source`` 开头。丢弃
    caret 行、note 与 warning，最多保留 ``max_errors`` 条错误。
    """
    errors: list[str] = []
    lines = [line.rstrip() for line in (text or "").splitlines()]
    index = 0
    while index < len(lines) and len(errors) < max_errors:
        line = lines[index]
        start = line.find("program_source")
        marker = line.find(" error: ", start if start >= 0 else 0)
        if start >= 0 and marker >= 0:
            location = line[start:marker].strip().rstrip(":")
            message = line[marker + len(" error: "):].strip()
            snippet = ""
            if index + 1 < len(lines) and not lines[index + 1].lstrip().startswith(("~", "^")):
                snippet = lines[index + 1].strip()[:100]
            entry = f"{location}: {message}" + (f" | `{snippet}`" if snippet else "")
            if entry not in errors:
                errors.append(entry)
        index += 1
    if not errors:
        return (text or "").strip()[:max_chars]
    return "\n".join(errors)[:max_chars]
