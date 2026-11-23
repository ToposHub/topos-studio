"""受限 GLSL → MSL 转译器（阶段 6.3）。

输入是「已解析的插件」：原始 ``.tglsl`` 着色器源（``EffectSpec.plugin_source_glsl``）
与 ``@param`` manifest（``EffectSpec.param_defs``）。输出是**完整**的 Metal
compute kernel 源：参数结构体、注册表 helper、插件函数与入口 kernel；纹理、
sampler、uniform、输出格式全部显式绑定（布局契约见 ``metal_abi``）。

可译子集 = 纯像素算子 + 标量/向量参数 + 受控纹理采样
    - 只有 ``texture(u_input_tex, uv)`` 与 ``textureSize(u_input_tex, 0)`` 能触达
      纹理；其它采样器 / LOD / 偏移 / 导数变体一律显式拒绝；
    - 宿主 helper ``rgb2hsv`` / ``hsv2rgb`` / ``blur_rand`` / ``fast_blur`` 映射到
      调色域已有的 MSL 翻译（与 ``dctl_wrapper`` 注入的 GLSL 版对照）；
    - 注册表 helper（``MslHelper``）按**整函数体**模式匹配：插件的 ``process``
      与 helper 的 GLSL 模式在标识符重命名后结构相同时，生成器改为调用已登记
      的 MSL 实现，并以显式实参传入插件参数；不匹配则逐语句转译。

GLSL 与 MSL 语义不同、必须在这里改写的点（依据是 AMD RX 6900 XT 上的编译
探针，断言见 tests/shared/test_tglsl_metal_language_facts.py）
    - ``.stpq`` swizzle 在 MSL 非法 → 改写为 ``.xyzw``；
    - 向量条件的三目在 MSL 非法 → ``select(b, a, cond)``；``^^`` 在 MSL 非法 → ``!=``；
    - MSL 没有 ``mod`` / ``radians`` / ``degrees`` → 展开为等价表达式；
      ``round`` / ``roundEven`` → ``rint``；``inversesqrt`` → ``rsqrt``；
      ``atan(y, x)`` → ``atan2``；
    - 构造器的分量总数必须恰好等于目标：Apple 的 GLSL 编译器允许向量实参的
      多余分量被丢弃（``vec4(v3, v2)``），MSL 不允许 → 截取所需分量；
    - MSL 没有全局 uniform：引用插件参数或引擎量的函数追加显式的
      ``params`` / ``u_input_tex`` / ``u_sampler`` / ``u_tex_size`` 形参；
    - 用户标识符统一加前缀改名，不会撞 MSL 关键字，也不会跨插件重名。

线程边界：纯字符串处理，无状态；同一输入恒产生同一输出（源码指纹可复现）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, NoReturn, Optional, Sequence, Set, Tuple

from src.features.shared.effects.dctl_wrapper import _builtin_identifiers
from src.features.shared.effects.metal_abi import (
    BUFFER_SLOT_PARAMS,
    COORD_CONVENTIONS,
    COORD_GL_ROW0_IS_V0,
    COORD_TOP_DOWN,
    ENGINE_ASPECT_RATIO,
    ENGINE_INPUT_TEX,
    ENGINE_OUTPUT_TEX,
    ENGINE_SAMPLER,
    ENGINE_TEX_SIZE,
    ENGINE_TEX_UV,
    HOST_ABI,
    PARAMS_STRUCT_NAME,
    SAMPLER_LINEAR_CLAMP,
    TEXTURE_SLOT_INPUT,
    TEXTURE_SLOT_OUTPUT,
    ParamField,
    ParamsLayout,
    build_params_layout,
    coord_uv_expression,
    sample_coord_expression,
    sampler_declaration,
)
from src.features.shared.effects.tglsl_front import (
    BACKEND_METAL,
    BOOL,
    FLOAT,
    GLSL_TYPES,
    INT,
    TRANSPILER_VERSION,
    TranspileStage,
    GlslType,
    TglslFrontError,
    TglslMetalDiagnostic,
    TglslMetalError,
    Assign,
    Binary,
    Block,
    Call,
    Constructor,
    Empty,
    Expr,
    ExprStmt,
    FunctionDef,
    Ident,
    If,
    IncDec,
    Index,
    Literal,
    Return,
    Stmt,
    Swizzle,
    Ternary,
    TranslationUnit,
    Unary,
    VarDecl,
    parse_tglsl_glsl,
)

__all__ = [
    "HOST_HELPERS",
    "MSL_HOST_FAST_BLUR",
    "MSL_HOST_HSV",
    "MslHelper",
    "TranspiledEffect",
    "transpile_binding",
    "transpile_tglsl",
]

ENTRY_NAME = "process"
SAMPLER_TYPE = GlslType("sampler2D", 1)

# 用户标识符在 MSL 侧的改名前缀。前缀按「源码里没有任何用户标识符以它开头」
# 动态选取，因此 `t_x` 与 `x` 不会被映射到同一个名字。
_VAR_PREFIX_CANDIDATES = ("t_", "tt_", "ttt_", "tttt_")
_FN_PREFIX_CANDIDATES = ("f_", "ff_", "fff_", "ffff_")


def _map_swizzle(components: str) -> str:
    """任意 swizzle 字母表 → xyzw（MSL 不接受 stpq）。"""
    table = {"s": "x", "t": "y", "p": "z", "q": "w", "r": "x", "g": "y", "b": "z", "a": "w",
             "x": "x", "y": "y", "z": "z", "w": "w"}
    return "".join(table[c] for c in components)


def _msl_type(ty: GlslType) -> str:
    if ty.size == 1:
        return {"float": "float", "int": "int", "uint": "uint", "bool": "bool"}[ty.base]
    return f"{ty.base}{ty.size}"


def _sha256(text: str, length: int = 64) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _line_snippet(source: str, line: int) -> str:
    if line <= 0:
        return ""
    lines = source.split("\n")
    return lines[line - 1].strip()[:80] if line <= len(lines) else ""


# ── 注册表 helper ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MslHelper:
    """MSL 效果注册表中的一个受控算子：GLSL 模式 + 已登记的 MSL 实现。

    ``glsl_pattern`` 是一段合法的 ``vec3 process(vec3, vec2)`` 源。当插件的
    ``process`` 在标识符重命名后与之结构相同时，生成器调用 ``msl_name``：
    第一个 MSL 形参接收 ``process`` 的 color 实参，其余形参依次接收
    ``glsl_param_bindings`` 绑定到的插件参数。
    """

    helper_id: str
    version: str
    msl_name: str
    msl_params: Tuple[str, ...]
    msl_body: str
    glsl_pattern: str
    glsl_param_bindings: Tuple[str, ...] = ()
    param_binding_types: Tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if len(self.msl_params) != 1 + len(self.glsl_param_bindings):
            raise ValueError(
                f"MslHelper {self.helper_id!r}: MSL 形参数（{len(self.msl_params)}）必须等于 "
                f"1 + 绑定参数数（{len(self.glsl_param_bindings)}）")
        if len(self.param_binding_types) != len(self.glsl_param_bindings):
            raise ValueError(
                f"MslHelper {self.helper_id!r}: param_binding_types 与 glsl_param_bindings 数量不一致")

    @property
    def msl_return_type(self) -> str:
        return self.msl_params[0].split()[0]

    def msl_source(self) -> str:
        body = "\n".join("    " + line if line.strip() else line
                         for line in self.msl_body.strip("\n").splitlines())
        return (f"// helper {self.helper_id} v{self.version}\n"
                f"inline {self.msl_return_type} {self.msl_name}({', '.join(self.msl_params)}) {{\n"
                f"{body}\n"
                "}\n")


# ── 宿主 helper（dctl_wrapper 注入的 GLSL 版在 MSL 侧的对照）───────────

#: GLSL 名 → (MSL 名, GLSL 形参类型, 返回类型, 是否触达输入纹理)。
#: 键集合必须与 ``dctl_wrapper._HOST_HELPERS`` 一致：这三个是宿主注入、插件不得
#: 自定义的 helper。``blur_rand`` 不在其中——它只是 ``fast_blur`` 的内部依赖
#: （随 MSL_HOST_FAST_BLUR 一起发射）；插件自定义的同名函数在 GL 与 Metal 两条
#: 路径上都合法（Metal 侧改名为 f_blur_rand，不会与 msl_blur_rand 冲突）。
HOST_HELPERS: Dict[str, Tuple[str, Tuple[GlslType, ...], GlslType, bool]] = {
    "rgb2hsv": ("msl_rgb2hsv", (GLSL_TYPES["vec3"],), GLSL_TYPES["vec3"], False),
    "hsv2rgb": ("msl_hsv2rgb", (GLSL_TYPES["vec3"],), GLSL_TYPES["vec3"], False),
    "fast_blur": ("msl_fast_blur",
                  (SAMPLER_TYPE, GLSL_TYPES["vec2"], FLOAT, FLOAT, GLSL_TYPES["vec2"]),
                  GLSL_TYPES["vec3"], True),
}

# 函数体文本取自调色域 shader_nodes_msl（MSL_HSV_FUNCS / MSL_BLUR_SHARPEN，
# P7-03 已验证翻译）；tests/shared/test_tglsl_msl_transpiler.py 钉住逐字一致。
MSL_HOST_HSV = """\
// —— COMMON_DEFINITIONS.rgb2hsv / hsv2rgb 的 MSL 翻译 ——
float3 msl_rgb2hsv(float3 c) {
    float4 K = float4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    float4 p = mix(float4(c.bg, K.wz), float4(c.gb, K.xy), step(c.b, c.g));
    float4 q = mix(float4(p.xyw, c.r), float4(c.r, p.yzx), step(p.x, c.r));

    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return float3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

float3 msl_hsv2rgb(float3 c) {
    float4 K = float4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    float3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""

MSL_HOST_FAST_BLUR = """\
// —— BLUR_SHARPEN_SNIPPET.blur_rand / fast_blur 的 MSL 翻译 ——
float msl_blur_rand(float2 co) {
    return fract(sin(dot(co.xy, float2(12.9898, 78.233))) * 43758.5453);
}

float3 msl_fast_blur(texture2d<float> tex, sampler smp, float2 uv,
                     float intensity, float radius, float2 tex_size) {
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = (intensity * radius * scale) / 50.0;
    if (r <= 0.0) return tex.sample(smp, uv).rgb;

    float3 result = float3(0.0);
    float total_weight = 0.0;
    float noise = msl_blur_rand(uv) * 6.28;

    // 12 samples in a dithered spiral pattern (was 16): LINEAR filtering plus
    // per-pixel jitter keeps the smoothed disk visually equivalent.
    for (int i = 0; i < 12; i++) {
        float theta = float(i) * 2.39996 + noise;
        float dist = sqrt(float(i) / 12.0) * r;
        float2 offset = float2(cos(theta), sin(theta)) * dist / tex_size;
        result += tex.sample(smp, uv + offset).rgb;
        total_weight += 1.0;
    }

    return result / total_weight;
}
"""

_HOST_HELPER_SOURCES: Dict[str, str] = {
    "rgb2hsv": MSL_HOST_HSV,
    "hsv2rgb": MSL_HOST_HSV,
    "fast_blur": MSL_HOST_FAST_BLUR,
}


# ── 产物 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TranspiledEffect:
    """一次成功转译的全部产物。"""

    plugin_id: str
    plugin_version: str
    msl_source: str                 # 完整 kernel 源（可直接交 Metal 编译）
    kernel_name: str
    params_struct: str
    layout: ParamsLayout
    process_kind: str               # "vec3" | "vec4"
    coord_convention: str
    sampler_spec: str
    source_fingerprint: str         # 原始 GLSL（或 catalog 内容哈希）的指纹
    msl_fingerprint: str            # 生成 MSL 的 SHA-256
    features: Tuple[str, ...]       # sampling / texture_size / host_helper:<n>
    helper_ids: Tuple[str, ...]     # 用到的注册表 helper（进入缓存键）
    matched_helper: Optional[str] = None

    @property
    def uses_sampling(self) -> bool:
        return "sampling" in self.features

    def with_kernel_name(self, kernel_name: str) -> str:
        """把占位 kernel 名换成带缓存键摘要的名字（源码其余部分不变）。"""
        return self.msl_source.replace(
            f"kernel void {self.kernel_name}(", f"kernel void {kernel_name}(", 1)

    def reflection(self) -> List[Dict[str, Any]]:
        """反射绑定：参数名 → GLSL 类型 → 结构体字段（对拍证据用）。"""
        return [{"param": f.param_name, "glsl_type": f.glsl_type, "fields": list(f.fields)}
                for f in self.layout.fields]


# ── 符号与分析 ──────────────────────────────────────────────────────────

_KIND_PARAM = "param"
_KIND_ENGINE_TEX = "engine_tex"
_KIND_ENGINE_SIZE = "engine_size"
_KIND_ENGINE_ASPECT = "engine_aspect"
_KIND_LOCAL = "local"
_KIND_GLOBAL_CONST = "global_const"

_CALL_USER = "user"
_CALL_BUILTIN = "builtin"
_CALL_TEXTURE = "texture"
_CALL_TEXTURE_SIZE = "texture_size"
_CALL_HOST = "host"

_ARITH = frozenset({"+", "-", "*", "/"})
_INT_SCALAR_OPS = frozenset({"%", "<<", ">>", "&", "|", "^"})
_COMPARE = frozenset({"<", ">", "<=", ">="})
_EQUALITY = frozenset({"==", "!="})
_LOGICAL = frozenset({"&&", "||", "^^"})
_COMPOUND = frozenset({"+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "|=", "^="})
_SWIZZLE_ALPHABETS = ("xyzw", "rgba", "stpq")


def _is_float_class(ty: GlslType) -> bool:
    return ty.base == "float"


def _is_int_class(ty: GlslType) -> bool:
    return ty.base in ("int", "uint")


def _binary_result_size(a: GlslType, b: GlslType) -> Optional[int]:
    """向量-标量 / 同尺寸向量运算的结果分量数；不可组合返回 None。"""
    if a.size == b.size or a.size == 1 or b.size == 1:
        return max(a.size, b.size)
    return None


@dataclass
class _Symbol:
    name: str
    ty: GlslType
    kind: str
    is_const: bool = False
    field: Optional[ParamField] = None


class _Scope:
    __slots__ = ("names", "parent")

    def __init__(self, parent: Optional["_Scope"] = None) -> None:
        self.parent = parent
        self.names: Dict[str, _Symbol] = {}

    def declare(self, name: str, symbol: _Symbol) -> bool:
        if name in self.names:
            return False
        self.names[name] = symbol
        return True

    def lookup(self, name: str) -> Optional[_Symbol]:
        scope: Optional[_Scope] = self
        while scope is not None:
            found = scope.names.get(name)
            if found is not None:
                return found
            scope = scope.parent
        return None


@dataclass
class _FnInfo:
    """函数对引擎量的直接依赖（调用图闭包后为完整依赖）。"""

    needs_params: bool = False
    needs_tex: bool = False
    needs_size: bool = False


@dataclass
class _HelperMatch:
    helpers: List[MslHelper] = field(default_factory=list)
    binding: Dict[str, str] = field(default_factory=dict)   # 模式参数标识符 → 插件参数名


class _Analyzer:
    """顶层校验（VALIDATE）+ 类型检查（TYPECHECK）。AST 节点上回填 ``ty`` / ``ref``。"""

    def __init__(self, unit: TranslationUnit, layout: ParamsLayout,
                 plugin_id: str, plugin_version: str) -> None:
        self.unit = unit
        self.layout = layout
        self.plugin_id = plugin_id
        self.plugin_version = plugin_version
        self.params: Dict[str, _Symbol] = {}
        self.global_consts: Dict[str, _Symbol] = {}
        self.functions: Dict[str, List[FunctionDef]] = {}
        self.fn_info: Dict[int, _FnInfo] = {}
        self.process: Optional[FunctionDef] = None
        self.process_kind = "vec3"
        self.features: Set[str] = set()
        self.user_identifiers: Set[str] = set()
        self._current_fn: Optional[FunctionDef] = None
        #: process 的第二个形参（uv）对应的符号；恒等采样 ``texture(u_input_tex, uv)``
        #: 在 top_down 约定下据此直接使用纹素坐标。
        self.uv_symbol: Optional[_Symbol] = None
        self._engine_tex = _Symbol(ENGINE_INPUT_TEX, SAMPLER_TYPE, _KIND_ENGINE_TEX)
        self._engine_size = _Symbol(ENGINE_TEX_SIZE, GLSL_TYPES["vec2"], _KIND_ENGINE_SIZE)
        self._engine_aspect = _Symbol(ENGINE_ASPECT_RATIO, FLOAT, _KIND_ENGINE_ASPECT)

    # ── 诊断 ──
    def _fail(self, stage: str, code: str, message: str, node: Any = None) -> NoReturn:
        line = int(getattr(node, "line", -1)) if node is not None else -1
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=self.plugin_id, plugin_version=self.plugin_version,
            stage=stage, backend=BACKEND_METAL, code=code, message=message,
            line=line,
            column=int(getattr(node, "col", -1)) if node is not None else -1,
            snippet=_line_snippet(self.unit.source, line)))

    def validate(self, code: str, message: str, node: Any = None) -> NoReturn:
        self._fail(TranspileStage.VALIDATE.value, code, message, node)

    def typecheck(self, code: str, message: str, node: Any = None) -> NoReturn:
        self._fail(TranspileStage.TYPECHECK.value, code, message, node)

    # ── 顶层校验（阶段 VALIDATE）──
    def validate_unit(self) -> None:
        reserved = _builtin_identifiers()
        seen_params: Set[str] = set()
        for field in self.layout.param_fields:
            name = field.param_name
            if name in reserved:
                self.validate("reserved_param_name",
                              f"@param {name!r} 与 GLSL 保留字同名（与 dctl_wrapper 的跨页改写同策略拒绝）")
            if name in seen_params:
                self.validate("duplicate_param", f"@param {name!r} 重复声明")
            seen_params.add(name)
            self.params[name] = _Symbol(name, GLSL_TYPES[field.glsl_type], _KIND_PARAM, field=field)

        fn_names: Set[str] = set()
        for fn in self.unit.functions:
            if fn.name in HOST_HELPERS:
                self.validate("host_helper_redefined",
                              f"插件自定义了宿主共享 helper `{fn.name}`（PLUGIN_DEV_GUIDE 契约禁止）", fn)
            overloads = self.functions.setdefault(fn.name, [])
            for other in overloads:
                if [p.ty for p in other.params] == [p.ty for p in fn.params]:
                    self.validate("duplicate_function", f"函数 `{fn.name}` 的同一签名重复定义", fn)
            overloads.append(fn)
            fn_names.add(fn.name)
            self.user_identifiers.add(fn.name)
            for p in fn.params:
                if p.name:
                    self.user_identifiers.add(p.name)

        const_names: Set[str] = set()
        for const in self.unit.consts:
            if const.name in fn_names or const.name in self.params or const.name in const_names \
                    or const.name in HOST_HELPERS:
                self.validate("name_collision",
                              f"全局常量 `{const.name}` 与函数 / 参数 / 另一常量同名", const)
            const_names.add(const.name)
            self.user_identifiers.add(const.name)
            self.global_consts[const.name] = _Symbol(const.name, const.ty, _KIND_GLOBAL_CONST, is_const=True)

        for name in self.params:
            if name in fn_names or name in const_names:
                self.validate("name_collision", f"@param {name!r} 与函数或全局常量同名")

        if any(fn.name == "main" for fn in self.unit.functions):
            # 计划 6.3「多个入口」：process 之外不允许再出现入口函数。
            self.validate("entry_point",
                          "插件不得自定义 main() 入口（只允许唯一入口 process）")
        entries = [fn for fn in self.unit.functions if fn.name == ENTRY_NAME]
        if len(entries) != 1:
            self.validate("entry_point",
                          f"必须且只能定义一个 `{ENTRY_NAME}` 入口（找到 {len(entries)} 个）")
        entry = entries[0]
        color_ty = entry.params[0].ty if len(entry.params) == 2 else None
        signature_ok = (color_ty is not None and color_ty.base == "float"
                        and color_ty.size in (3, 4) and entry.params[1].ty == GLSL_TYPES["vec2"])
        if not signature_ok:
            self.validate("entry_signature", "process 的签名必须是 process(vec3|vec4 color, vec2 uv)", entry)
        if entry.ret != color_ty:
            self.validate("entry_signature", "process 的返回类型必须与 color 形参类型相同", entry)
        for p in entry.params:
            # 与 GL 路径同策略：只禁止 @param 与 process 形参同名。保留字表本身
            # 含 color / uv / process（composition 宿主管线标识符），不能用它检查 process 形参。
            if p.name in self.params:
                self.validate("reserved_param_name",
                              f"process 形参 `{p.name}` 与 @param 参数同名", entry)
        self.process = entry
        self.process_kind = f"vec{color_ty.size}"

    # ── 类型检查（阶段 TYPECHECK）──
    def check_unit(self) -> None:
        for const in self.unit.consts:
            self._current_fn = None
            init_ty = self.check_expr(const.init, _Scope())
            if init_ty != const.ty:
                self.typecheck("const_type_mismatch",
                               f"常量 `{const.name}` 声明为 {const.ty.name}，初始化式是 {init_ty.name}",
                               const.init)
            if not self._is_const_expr(const.init):
                self.typecheck("non_constant_initializer",
                               f"全局常量 `{const.name}` 的初始化式不是编译期常量", const.init)
        for fn in self.unit.functions:
            self._check_function(fn)
        # needs 闭包：调用方继承被调用方的引擎依赖（GLSL 禁止递归，调用图无环）。
        changed = True
        while changed:
            changed = False
            for fn in self.unit.functions:
                info = self.fn_info[id(fn)]
                for callee in getattr(fn, "_callees", ()):
                    callee_info = self.fn_info[id(callee)]
                    if (callee_info.needs_params and not info.needs_params) or \
                            (callee_info.needs_tex and not info.needs_tex) or \
                            (callee_info.needs_size and not info.needs_size):
                        info.needs_params |= callee_info.needs_params
                        info.needs_tex |= callee_info.needs_tex
                        info.needs_size |= callee_info.needs_size
                        changed = True

    def _is_const_expr(self, e: Expr) -> bool:
        if isinstance(e, Literal):
            return True
        if isinstance(e, Ident):
            return e.ref is not None and e.ref.kind == _KIND_GLOBAL_CONST
        if isinstance(e, Constructor):
            return all(self._is_const_expr(a) for a in e.args)
        if isinstance(e, Unary):
            return self._is_const_expr(e.operand)
        if isinstance(e, Ternary):
            return all(self._is_const_expr(x) for x in (e.cond, e.then, e.els))
        if isinstance(e, Binary):
            return self._is_const_expr(e.left) and self._is_const_expr(e.right)
        return False

    def _check_function(self, fn: FunctionDef) -> None:
        self.fn_info[id(fn)] = _FnInfo()
        scope = _Scope()
        self._current_fn = fn
        for p in fn.params:
            if not p.name:
                continue
            symbol = _Symbol(p.name, p.ty, _KIND_LOCAL)
            if not scope.declare(p.name, symbol):
                self.typecheck("redeclaration", f"函数 `{fn.name}` 的形参 `{p.name}` 重复", p)
            if fn is self.process and fn.params.index(p) == 1:
                self.uv_symbol = symbol
            self.user_identifiers.add(p.name)
        fn._callees = []  # type: ignore[attr-defined]
        for stmt in (fn.body.stmts if fn.body else ()):
            self.check_stmt(stmt, scope, fn)

    def _touch(self, what: str) -> None:
        fn = self._current_fn
        if fn is not None:
            setattr(self.fn_info[id(fn)], f"needs_{what}", True)

    def _check_binary_type(self, node: Expr, op: str, lt: GlslType, rt: GlslType) -> GlslType:
        if op in _ARITH:
            same_base = lt.base == rt.base
            if (_is_float_class(lt) and _is_float_class(rt)) or \
                    (_is_int_class(lt) and _is_int_class(rt) and same_base):
                size = _binary_result_size(lt, rt)
                if size is None:
                    self.typecheck("vector_size_mismatch",
                                   f"运算符 {op} 两侧向量分量数不同（{lt.name} 与 {rt.name}）", node)
                return lt.at_size(size)
            self.typecheck("operator_type", f"运算符 {op} 的操作数类型不合法：{lt.name} {op} {rt.name}", node)
        if op in _INT_SCALAR_OPS:
            if not (_is_int_class(lt) and _is_int_class(rt) and lt == rt and lt.is_scalar):
                self.typecheck("operator_type",
                               f"运算符 {op} 只支持同类型的 int/uint 标量：{lt.name} {op} {rt.name}", node)
            return lt
        if op in _COMPARE:
            if not (lt == rt and lt.is_scalar and lt.base in ("float", "int", "uint")):
                self.typecheck("operator_type",
                               f"比较运算符 {op} 要求同类型数值标量：{lt.name} {op} {rt.name}", node)
            return BOOL
        if op in _EQUALITY:
            if lt != rt or lt.base not in ("float", "int", "uint", "bool"):
                self.typecheck("operator_type",
                               f"运算符 {op} 要求两侧为同类型的数值/bool：{lt.name} {op} {rt.name}", node)
            return BOOL.at_size(lt.size)
        if op in _LOGICAL:
            if lt != BOOL or rt != BOOL:
                self.typecheck("operator_type", f"逻辑运算符 {op} 只支持 bool 标量", node)
            return BOOL
        self.typecheck("unsupported_operator", f"不支持的运算符 {op!r}", node)

    def check_expr(self, e: Expr, scope: _Scope) -> GlslType:
        """求表达式类型并回填到 ``e.ty``（发射器与 helper 匹配依赖它）。"""
        ty = self._check_expr_inner(e, scope)
        e.ty = ty
        return ty

    def _check_expr_inner(self, e: Expr, scope: _Scope) -> GlslType:
        if isinstance(e, Literal):
            return e.ty
        if isinstance(e, Ident):
            sym = scope.lookup(e.name)
            if sym is None and e.name in self.params:
                sym = self.params[e.name]
                self._touch("params")
            if sym is None and e.name == ENGINE_INPUT_TEX:
                sym = self._engine_tex
                self._touch("tex")
            if sym is None and e.name == ENGINE_TEX_SIZE:
                sym = self._engine_size
                self._touch("size")
            if sym is None and e.name == ENGINE_ASPECT_RATIO:
                sym = self._engine_aspect
                self._touch("size")
            if sym is None and e.name in self.global_consts:
                sym = self.global_consts[e.name]
            if sym is None:
                self.typecheck("undeclared_identifier",
                               f"未声明的标识符 `{e.name}`（参数必须经 @param 声明；引擎量只有 "
                               "u_input_tex / u_tex_size / u_aspect_ratio）", e)
            e.ref = sym
            e.ty = sym.ty
            return sym.ty
        if isinstance(e, Constructor):
            return self._check_constructor(e, scope)
        if isinstance(e, Swizzle):
            return self._check_swizzle(e, scope)
        if isinstance(e, Index):
            base_ty = self.check_expr(e.base, scope)
            idx_ty = self.check_expr(e.index, scope)
            if base_ty.size <= 1 or not _is_int_class(idx_ty) or not idx_ty.is_scalar:
                self.typecheck("index_type", "下标只支持「向量[整数标量]」", e)
            return base_ty.at_size(1)
        if isinstance(e, Unary):
            return self._check_unary(e, scope)
        if isinstance(e, Binary):
            lt = self.check_expr(e.left, scope)
            rt = self.check_expr(e.right, scope)
            return self._check_binary_type(e, e.op, lt, rt)
        if isinstance(e, Ternary):
            return self._check_ternary(e, scope)
        if isinstance(e, Call):
            return self._check_call(e, scope)
        self.typecheck("unsupported_expression", f"不支持的表达式类型 {type(e).__name__}", e)

    def _check_constructor(self, e: Constructor, scope: _Scope) -> GlslType:
        arg_types = [self.check_expr(a, scope) for a in e.args]
        target = e.ty
        if target is None or target.base == "sampler2d":
            self.typecheck("constructor_type", "构造器缺少合法的目标类型", e)
        if target.size == 1:
            if len(arg_types) != 1 or not arg_types[0].is_scalar:
                self.typecheck("constructor_arity", f"{target.name}() 构造器恰好接收 1 个标量实参", e)
            return target
        if target.is_float:
            for t in arg_types:
                # 标量可转换；整向量转换（vec2(ivec2)）只允许单实参且分量数相同
                convertible_vector = (len(arg_types) == 1 and t.size == target.size)
                if not (t.is_float or convertible_vector
                        or (t.is_scalar and t.base in ("int", "uint", "bool"))):
                    self.typecheck("constructor_type",
                                   f"{target.name}() 的向量实参必须是 float 向量（实际 {t.name}）", e)
        elif any(t.base != target.base for t in arg_types):
            self.typecheck("constructor_type", f"{target.name}() 的实参必须都是 {target.base} 类型", e)
        if len(arg_types) == 1 and arg_types[0].is_scalar:
            return target            # 单标量广播：vec3(x) == float3(x)
        total = sum(t.size for t in arg_types)
        if total < target.size:
            self.typecheck("constructor_not_enough_components",
                           f"{target.name}() 的实参共 {total} 个分量，少于所需的 {target.size} 个", e)
        if total > target.size:
            # GLSL 规定「多出的实参」是错误，但向量实参的剩余分量可以丢弃。
            remaining = target.size
            for t in arg_types:
                if remaining == 0:
                    self.typecheck("constructor_excess_arguments",
                                   f"{target.name}() 的实参分量多于所需，且多出的是完整实参", e)
                remaining -= min(t.size, remaining)
        return target

    def _check_swizzle(self, e: Swizzle, scope: _Scope) -> GlslType:
        base_ty = self.check_expr(e.base, scope)
        comps = e.components
        if base_ty.size <= 1 or len(comps) > 4:
            self.typecheck("invalid_swizzle", f"标量不能 swizzle，且 swizzle 最多 4 个分量：`.{comps}`", e)
        matched = False
        for alphabet in _SWIZZLE_ALPHABETS:
            if all(c in alphabet for c in comps):
                matched = True
                if any(alphabet.index(c) >= base_ty.size for c in comps):
                    self.typecheck("swizzle_out_of_range",
                                   f"swizzle `.{comps}` 超出 {base_ty.name} 的分量范围", e)
                break
        if not matched:
            self.typecheck("invalid_swizzle", f"非法 swizzle `.{comps}`（不能混用字母表）", e)
        return base_ty.at_size(len(comps))

    def _check_unary(self, e: Unary, scope: _Scope) -> GlslType:
        t = self.check_expr(e.operand, scope)
        op = e.op
        if op in ("++", "--", "post++", "post--"):
            self._require_lvalue(e.operand)
            if t.is_bool or not t.is_scalar:
                self.typecheck("operator_type", "自增/自减只作用于数值标量左值", e)
        elif op in ("-", "+"):
            if t.is_bool:
                self.typecheck("operator_type", f"一元 {op} 不支持 bool", e)
        elif op == "!":
            if t != BOOL:
                self.typecheck("operator_type", "! 只支持 bool 标量", e)
        elif op == "~":
            if not (_is_int_class(t) and t.is_scalar):
                self.typecheck("operator_type", "~ 只支持 int/uint 标量", e)
        return t

    def _check_ternary(self, e: Ternary, scope: _Scope) -> GlslType:
        cond_ty = self.check_expr(e.cond, scope)
        then_ty = self.check_expr(e.then, scope)
        els_ty = self.check_expr(e.els, scope)
        if then_ty != els_ty:
            self.typecheck("ternary_type", "三目的两个分支类型必须相同", e)
        if cond_ty == BOOL:
            return then_ty
        if cond_ty.is_bool and not cond_ty.is_scalar and then_ty.size == cond_ty.size:
            e.ref = "vector"          # 向量条件：MSL 侧必须发 select()
            return then_ty
        self.typecheck("ternary_condition",
                       "三目的条件必须是 bool 标量，或与分支分量数相同的 bool 向量", e)

    def _check_call(self, e: Call, scope: _Scope) -> GlslType:
        arg_types = [self.check_expr(a, scope) for a in e.args]
        name = e.callee
        # 1) 插件自定义函数：按实参类型精确匹配重载（GLSL 没有隐式转换）。
        for overload in self.functions.get(name, ()):
            if [p.ty for p in overload.params] == arg_types:
                caller = self._current_fn
                if caller is not None and overload not in caller._callees:  # type: ignore[attr-defined]
                    caller._callees.append(overload)  # type: ignore[attr-defined]
                e.ref = (_CALL_USER, overload)
                return overload.ret or GlslType("void", 1)
        # 2) 宿主 helper（dctl_wrapper 的 GLSL 版 → 调色域 MSL 翻译）。
        if name in HOST_HELPERS:
            msl_name, param_types, ret, touches_tex = HOST_HELPERS[name]
            if arg_types != list(param_types):
                self.typecheck("host_helper_signature",
                               f"宿主 helper `{name}` 的实参类型应为 "
                               f"({', '.join(t.name for t in param_types)})", e)
            if touches_tex:
                self._require_engine_sampler(e.args[0])
                self._touch("tex")
            self.features.add(f"host_helper:{name}")
            e.ref = (_CALL_HOST, msl_name)
            return ret
        # 3) 受控纹理访问。
        if name == "texture":
            if len(arg_types) != 2 or arg_types[1] != GLSL_TYPES["vec2"]:
                self.typecheck("texture_signature",
                               "texture() 在子集内只有 texture(u_input_tex, vec2) 一种形态", e)
            self._require_engine_sampler(e.args[0])
            self._touch("tex")
            self.features.add("sampling")
            e.ref = (_CALL_TEXTURE, None)
            return GLSL_TYPES["vec4"]
        if name == "textureSize":
            if len(arg_types) != 2 or arg_types[1] != INT:
                self.typecheck("texture_signature",
                               "textureSize() 在子集内只有 textureSize(u_input_tex, int) 一种形态", e)
            self._require_engine_sampler(e.args[0])
            self._touch("tex")
            self.features.add("texture_size")
            e.ref = (_CALL_TEXTURE_SIZE, None)
            return GLSL_TYPES["ivec2"]
        # 4) 内建函数规则表。
        rule = _BUILTIN_RULES.get(name)
        if rule is not None:
            result_ty, emit_rule = rule(self, e, arg_types)
            e.ref = (_CALL_BUILTIN, name, emit_rule)
            return result_ty
        if self.functions.get(name):
            self.typecheck("no_matching_overload",
                           f"函数 `{name}` 没有与实参类型 "
                           f"({', '.join(t.name for t in arg_types)}) 匹配的重载", e)
        self.typecheck("unknown_function",
                       f"未知的函数 `{name}`（该内建不在可译子集内，或未经声明）", e)

    def _require_engine_sampler(self, arg: Expr) -> None:
        if not (isinstance(arg, Ident) and arg.name == ENGINE_INPUT_TEX):
            self.typecheck("uncontrolled_sampler",
                           f"只允许触达引擎注入的 {ENGINE_INPUT_TEX}（其它采样器不在可译子集内）", arg)

    def _require_lvalue(self, e: Expr) -> None:
        if isinstance(e, Ident):
            if e.ref is None or e.ref.is_const or e.ref.kind != _KIND_LOCAL:
                self.typecheck("assignment_target", f"`{e.name}` 不是可赋值的左值（const 或 uniform）", e)
        elif isinstance(e, Swizzle):
            if len(set(e.components)) != len(e.components):
                self.typecheck("assignment_target", "赋值目标的 swizzle 不能含重复分量", e)
            self._require_lvalue(e.base)
        elif isinstance(e, Index):
            self._require_lvalue(e.base)
        else:
            self.typecheck("assignment_target", "赋值目标必须是变量 / swizzle / 下标", e)

    def check_stmt(self, s: Stmt, scope: _Scope, fn: FunctionDef) -> None:
        if isinstance(s, VarDecl):
            if s.init is not None:
                init_ty = self.check_expr(s.init, scope)
                if init_ty != s.ty:
                    self.typecheck("declaration_type_mismatch",
                                   f"`{s.name}` 声明为 {s.ty.name}，初始化式是 {init_ty.name}", s.init)
            if not scope.declare(s.name, _Symbol(s.name, s.ty, _KIND_LOCAL, is_const=s.const)):
                self.typecheck("redeclaration", f"变量 `{s.name}` 在同一作用域重复声明", s)
            self.user_identifiers.add(s.name)
        elif isinstance(s, Assign):
            target_ty = self.check_expr(s.target, scope)
            value_ty = self.check_expr(s.value, scope)
            self._require_lvalue(s.target)
            if s.op == "=":
                if target_ty != value_ty:
                    self.typecheck("assignment_type",
                                   f"赋值两侧类型不同：{target_ty.name} = {value_ty.name}", s)
            elif s.op in _COMPOUND:
                result = self._check_binary_type(s, s.op[:-1], target_ty, value_ty)
                if result != target_ty:
                    self.typecheck("assignment_type",
                                   f"复合赋值结果类型 {result.name} 与目标 {target_ty.name} 不同", s)
            else:
                self.typecheck("unsupported_operator", f"不支持的赋值运算符 {s.op!r}", s)
        elif isinstance(s, IncDec):
            target_ty = self.check_expr(s.target, scope)
            self._require_lvalue(s.target)
            if target_ty.is_bool or not target_ty.is_scalar:
                self.typecheck("operator_type", "自增/自减只作用于数值标量左值", s)
        elif isinstance(s, ExprStmt):
            self.check_expr(s.expr, scope)
        elif isinstance(s, If):
            if self.check_expr(s.cond, scope) != BOOL:
                self.typecheck("condition_type", "if 的条件必须是 bool 标量", s.cond)
            self.check_stmt(s.then, _Scope(scope), fn)
            if s.els is not None:
                self.check_stmt(s.els, _Scope(scope), fn)
        elif isinstance(s, Return):
            if fn.ret is None:
                if s.value is not None:
                    self.typecheck("return_type", "void 函数不能返回值", s)
                return
            if s.value is None:
                self.typecheck("return_type", f"函数 `{fn.name}` 必须返回 {fn.ret.name}", s)
            if self.check_expr(s.value, scope) != fn.ret:
                self.typecheck("return_type", f"return 的类型与函数 `{fn.name}` 的返回类型不一致", s)
        elif isinstance(s, Block):
            inner = _Scope(scope)
            for stmt in s.stmts:
                self.check_stmt(stmt, inner, fn)
        elif isinstance(s, Empty):
            pass
        else:
            self.typecheck("unsupported_statement", f"不支持的语句 {type(s).__name__}", s)


# ── 内建函数规则 ────────────────────────────────────────────────────────
# 每条规则校验实参类型并返回 (结果类型, MSL 发射规则)。

_Rule = Callable[["_Analyzer", Call, List[GlslType]], Tuple[GlslType, str]]


def _arity(an: _Analyzer, node: Expr, name: str, types: List[GlslType], count: int) -> None:
    if len(types) != count:
        an.typecheck("builtin_arity", f"{name}() 需要 {count} 个实参，实际 {len(types)} 个", node)


def _rule_gen1(name: str, emit: str = "same") -> _Rule:
    """(genType) → genType，genType = float 标量或 float 向量。"""
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 1)
        if not _is_float_class(t[0]):
            an.typecheck("builtin_type",
                         f"{name}() 的实参必须是 float 或 float 向量（实际 {t[0].name}）", node)
        return t[0], emit
    return rule


def _rule_abs_or_sign(name: str) -> _Rule:
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 1)
        if t[0].base not in ("float", "int"):
            an.typecheck("builtin_type", f"{name}() 的实参必须是 float/int 标量或向量", node)
        return t[0], "same"
    return rule


def _rule_pair(name: str, emit: str = "same", scalar_second: bool = False,
               scalar_first: bool = False) -> _Rule:
    """(T, T) / (T, float) / (float, T) 的 float 内建，结果类型为向量一侧的 T。"""
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 2)
        a, b = t
        if not (_is_float_class(a) and _is_float_class(b)):
            an.typecheck("builtin_type", f"{name}() 的实参必须是 float 或 float 向量", node)
        if a == b:
            return a, emit
        if scalar_second and b == FLOAT:
            return a, emit
        if scalar_first and a == FLOAT:
            return b, emit
        an.typecheck("builtin_type", f"{name}() 的实参类型必须匹配（实际 {a.name}, {b.name}）", node)
    return rule


def _rule_minmax(name: str) -> _Rule:
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 2)
        a, b = t
        if _is_float_class(a) and _is_float_class(b) and (a == b or b == FLOAT):
            return a, "same"
        if a == b and _is_int_class(a):
            return a, "same"
        an.typecheck("builtin_type", f"{name}() 的实参类型不合法（{a.name}, {b.name}）", node)
    return rule


def _rule_atan(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    if len(t) == 1:
        return _rule_gen1("atan")(an, node, t)
    result, _ = _rule_pair("atan")(an, node, t)
    return result, "atan2"          # GLSL atan(y, x) == MSL atan2(y, x)


def _rule_clamp(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "clamp", t, 3)
    x, lo, hi = t
    if not (_is_float_class(x) and ((lo == x and hi == x) or (lo == FLOAT and hi == FLOAT))):
        an.typecheck("builtin_type",
                     "clamp() 的实参必须是 (T, T, T) 或 (T, float, float)，T 为 float 标量/向量", node)
    return x, "same"


def _rule_mix(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "mix", t, 3)
    x, y, a = t
    if _is_float_class(x) and x == y:
        if a == x or a == FLOAT:
            return x, "same"
        if a.is_bool and not a.is_scalar and a.size == x.size:
            return x, "select"   # GLSL mix(x, y, bvec) == MSL select(x, y, bvec)
    an.typecheck("builtin_type",
                 "mix() 的实参必须是 (T, T, T) / (T, T, float) / (T, T, bvec)", node)


def _rule_smoothstep(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "smoothstep", t, 3)
    e0, e1, x = t
    if not (_is_float_class(x) and ((e0 == x and e1 == x) or (e0 == FLOAT and e1 == FLOAT))):
        an.typecheck("builtin_type",
                     "smoothstep() 的实参必须是 (T, T, T) 或 (float, float, T)", node)
    return x, "same"


def _rule_geom2(name: str) -> _Rule:
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 2)
        if not (_is_float_class(t[0]) and t[0] == t[1]):
            an.typecheck("builtin_type", f"{name}() 的两个实参必须是同类型的 float 标量/向量", node)
        return FLOAT, "same"
    return rule


def _rule_length(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "length", t, 1)
    if not _is_float_class(t[0]):
        an.typecheck("builtin_type", "length() 的实参必须是 float 标量/向量", node)
    return FLOAT, "same"


def _rule_cross(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "cross", t, 2)
    if t[0] != GLSL_TYPES["vec3"] or t[1] != GLSL_TYPES["vec3"]:
        an.typecheck("builtin_type", "cross() 的两个实参都必须是 vec3", node)
    return GLSL_TYPES["vec3"], "same"


def _rule_compare(name: str, op: str, ordering: bool) -> _Rule:
    """lessThan / equal 等：逐分量比较向量，结果为同尺寸 bool 向量。"""
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 2)
        a, b = t
        bases = ("float", "int", "uint") if ordering else ("float", "int", "uint", "bool")
        if a != b or a.is_scalar or a.base not in bases:
            an.typecheck("builtin_type",
                         f"{name}() 的两个实参必须是同类型向量（{'/'.join(bases)}）", node)
        return BOOL.at_size(a.size), f"cmp:{op}"
    return rule


def _rule_anyall(name: str) -> _Rule:
    def rule(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
        _arity(an, node, name, t, 1)
        if not (t[0].is_bool and not t[0].is_scalar):
            an.typecheck("builtin_type", f"{name}() 的实参必须是 bool 向量", node)
        return BOOL, "same"
    return rule


def _rule_not(an: _Analyzer, node: Call, t: List[GlslType]) -> Tuple[GlslType, str]:
    _arity(an, node, "not", t, 1)
    if not (t[0].is_bool and not t[0].is_scalar):
        an.typecheck("builtin_type", "not() 的实参必须是 bool 向量", node)
    return t[0], "not"


_BUILTIN_RULES: Dict[str, _Rule] = {}
for _name in ("sin", "cos", "tan", "asin", "acos", "sinh", "cosh", "tanh", "exp", "log",
              "exp2", "log2", "sqrt", "floor", "trunc", "ceil", "fract", "normalize"):
    _BUILTIN_RULES[_name] = _rule_gen1(_name)
_BUILTIN_RULES["abs"] = _rule_abs_or_sign("abs")
_BUILTIN_RULES["sign"] = _rule_abs_or_sign("sign")
_BUILTIN_RULES["radians"] = _rule_gen1("radians", "radians")
_BUILTIN_RULES["degrees"] = _rule_gen1("degrees", "degrees")
_BUILTIN_RULES["round"] = _rule_gen1("round", "rint")
_BUILTIN_RULES["roundEven"] = _rule_gen1("roundEven", "rint")
_BUILTIN_RULES["inversesqrt"] = _rule_gen1("inversesqrt", "rsqrt")
_BUILTIN_RULES["pow"] = _rule_pair("pow", scalar_second=True)
_BUILTIN_RULES["mod"] = _rule_pair("mod", emit="mod", scalar_second=True)
_BUILTIN_RULES["reflect"] = _rule_pair("reflect")
_BUILTIN_RULES["step"] = _rule_pair("step", scalar_first=True)
_BUILTIN_RULES["atan"] = _rule_atan
_BUILTIN_RULES["min"] = _rule_minmax("min")
_BUILTIN_RULES["max"] = _rule_minmax("max")
_BUILTIN_RULES["clamp"] = _rule_clamp
_BUILTIN_RULES["mix"] = _rule_mix
_BUILTIN_RULES["smoothstep"] = _rule_smoothstep
_BUILTIN_RULES["dot"] = _rule_geom2("dot")
_BUILTIN_RULES["distance"] = _rule_geom2("distance")
_BUILTIN_RULES["length"] = _rule_length
_BUILTIN_RULES["cross"] = _rule_cross
for _name, _op, _ordering in (("lessThan", "<", True), ("lessThanEqual", "<=", True),
                              ("greaterThan", ">", True), ("greaterThanEqual", ">=", True),
                              ("equal", "==", False), ("notEqual", "!=", False)):
    _BUILTIN_RULES[_name] = _rule_compare(_name, _op, _ordering)
_BUILTIN_RULES["any"] = _rule_anyall("any")
_BUILTIN_RULES["all"] = _rule_anyall("all")
_BUILTIN_RULES["not"] = _rule_not
del _name, _op, _ordering


# ── MSL 发射 ───────────────────────────────────────────────────────────


class _Emitter:
    """把类型检查通过的 AST 发射为 MSL 文本。"""

    def __init__(self, analyzer: _Analyzer, coord_convention: str, sampler_spec: str,
                 match: "_HelperMatch") -> None:
        self.an = analyzer
        self.coord = coord_convention
        self.sampler = sampler_spec
        self.match = match
        identifiers = analyzer.user_identifiers
        self.var_prefix = next(p for p in _VAR_PREFIX_CANDIDATES
                               if not any(i.startswith(p) for i in identifiers))
        self.fn_prefix = next(p for p in _FN_PREFIX_CANDIDATES
                              if not any(i.startswith(p) for i in identifiers))

    def _var(self, name: str) -> str:
        return self.var_prefix + name

    def _fn(self, name: str) -> str:
        return self.fn_prefix + name

    def _lifted_decl(self, info: _FnInfo) -> List[str]:
        out: List[str] = []
        if info.needs_params:
            out.append(f"constant {PARAMS_STRUCT_NAME}& params")
        if info.needs_tex:
            out += [f"texture2d<float> {ENGINE_INPUT_TEX}", f"sampler {ENGINE_SAMPLER}"]
        if info.needs_size:
            out.append(f"float2 {ENGINE_TEX_SIZE}")
        return out

    def _lifted_args(self, info: _FnInfo) -> List[str]:
        return [decl.split()[-1] for decl in self._lifted_decl(info)]

    # ── 表达式 ──
    def _expr(self, e: Expr) -> str:
        if isinstance(e, Literal):
            return e.text
        if isinstance(e, Ident):
            sym = e.ref
            if sym.kind == _KIND_PARAM:
                return sym.field.msl_expr()
            if sym.kind == _KIND_ENGINE_TEX:
                return ENGINE_INPUT_TEX
            if sym.kind == _KIND_ENGINE_SIZE:
                return ENGINE_TEX_SIZE
            if sym.kind == _KIND_ENGINE_ASPECT:
                # 与 dctl_wrapper 的别名改写同式：W / H
                return f"({ENGINE_TEX_SIZE}.x / {ENGINE_TEX_SIZE}.y)"
            return self._var(e.name)
        if isinstance(e, Constructor):
            return self._constructor(e)
        if isinstance(e, Swizzle):
            return f"{self._expr(e.base)}.{_map_swizzle(e.components)}"
        if isinstance(e, Index):
            return f"({self._expr(e.base)})[{self._expr(e.index)}]"
        if isinstance(e, Unary):
            x = self._expr(e.operand)
            if e.op in ("post++", "post--"):
                return f"({x}{e.op[4:]})"
            return f"({e.op}{x})"
        if isinstance(e, Binary):
            op = "!=" if e.op == "^^" else e.op
            return f"({self._expr(e.left)} {op} {self._expr(e.right)})"
        if isinstance(e, Ternary):
            cond, then, els = self._expr(e.cond), self._expr(e.then), self._expr(e.els)
            if e.ref == "vector":
                return f"select({els}, {then}, {cond})"
            return f"({cond} ? {then} : {els})"
        if isinstance(e, Call):
            return self._call(e)
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=self.an.plugin_id, plugin_version=self.an.plugin_version,
            stage=TranspileStage.GENERATE.value, backend=BACKEND_METAL,
            code="unsupported_expression",
            message=f"不支持的表达式类型 {type(e).__name__}", line=e.line, column=e.col))

    def _constructor(self, e: Constructor) -> str:
        out: List[str] = []
        remaining = e.ty.size
        for a in e.args:
            size = a.ty.size
            if size <= remaining:
                out.append(self._expr(a))
                remaining -= size
            else:
                # 该实参只取前 remaining 个分量（GLSL 左到右消费、丢弃多余分量）
                if isinstance(a, Swizzle):
                    out.append(f"{self._expr(a.base)}.{_map_swizzle(a.components)[:remaining]}")
                else:
                    out.append(f"({self._expr(a)}).{'xyzw'[:remaining]}")
                remaining = 0
        return f"{_msl_type(e.ty)}({', '.join(out)})"

    def _call(self, e: Call) -> str:
        ref = e.ref
        kind = ref[0]
        if kind == _CALL_USER:
            callee = ref[1]
            args = [self._expr(a) for a in e.args] + self._lifted_args(self.an.fn_info[id(callee)])
            return f"{self._fn(callee.name)}({', '.join(args)})"
        if kind == _CALL_HOST:
            if e.callee == "fast_blur":
                # GLSL fast_blur(sampler2D tex, uv, ...)：采样器实参固定是 u_input_tex
                args = [ENGINE_INPUT_TEX, ENGINE_SAMPLER] + [self._expr(a) for a in e.args[1:]]
            else:
                args = [self._expr(a) for a in e.args]
            return f"{ref[1]}({', '.join(args)})"
        if kind == _CALL_TEXTURE:
            if self.coord == COORD_TOP_DOWN and self._is_entry_uv(e.args[1]):
                # 恒等采样：直接用纹素坐标，与 gl_row0_is_v0 的镜像行逐位相同。
                return f"{ENGINE_INPUT_TEX}.sample({ENGINE_SAMPLER}, {ENGINE_TEX_UV})"
            coord = self._expr(e.args[1])
            return f"{ENGINE_INPUT_TEX}.sample({ENGINE_SAMPLER}, {sample_coord_expression(self.coord, coord)})"
        if kind == _CALL_TEXTURE_SIZE:
            return f"int2({ENGINE_INPUT_TEX}.get_width(), {ENGINE_INPUT_TEX}.get_height())"
        _, name, rule = ref
        args = [self._expr(a) for a in e.args]
        if rule == "same":
            return f"{name}({', '.join(args)})"
        if rule in ("rint", "rsqrt"):
            return f"{rule}({args[0]})"
        if rule == "atan2":
            return f"atan2({args[0]}, {args[1]})"
        if rule == "mod":
            # GLSL mod(x, y) = x - y * floor(x / y)（结果符号跟随 y；MSL 无 mod）
            return f"({args[0]} - {args[1]} * floor(({args[0]}) / ({args[1]})))"
        if rule == "radians":
            return f"({args[0]} * 0.017453292519943295)"
        if rule == "degrees":
            return f"({args[0]} * 57.29577951308232)"
        if rule == "select":
            return f"select({args[0]}, {args[1]}, {args[2]})"
        if rule.startswith("cmp:"):
            return f"({args[0]} {rule[4:]} {args[1]})"
        if rule == "not":
            comps = ", ".join(f"!{args[0]}.{c}" for c in "xyzw"[:e.ty.size])
            return f"bool{e.ty.size}({comps})"
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=self.an.plugin_id, plugin_version=self.an.plugin_version,
            stage=TranspileStage.GENERATE.value, backend=BACKEND_METAL,
            code="unknown_emit_rule", message=f"内建 `{name}` 没有发射规则 {rule!r}",
            line=e.line, column=e.col))

    def _is_entry_uv(self, arg: Expr) -> bool:
        """实参是否就是 process 的 uv 形参本身（未被同名局部变量遮蔽）。"""
        return isinstance(arg, Ident) and arg.ref is not None and arg.ref is self.an.uv_symbol

    def _lvalue(self, e: Expr) -> str:
        if isinstance(e, Ident):
            return self._var(e.name)
        if isinstance(e, Swizzle):
            return f"{self._lvalue(e.base)}.{_map_swizzle(e.components)}"
        if isinstance(e, Index):
            return f"{self._lvalue(e.base)}[{self._expr(e.index)}]"
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=self.an.plugin_id, plugin_version=self.an.plugin_version,
            stage=TranspileStage.GENERATE.value, backend=BACKEND_METAL,
            code="assignment_target", message="赋值目标必须是变量 / swizzle / 下标",
            line=e.line, column=e.col))

    # ── 语句 ──
    def _stmt(self, s: Stmt, indent: int) -> List[str]:
        pad = "    " * indent
        if isinstance(s, VarDecl):
            decl = f"{_msl_type(s.ty)} {self._var(s.name)}"
            if s.const:
                decl = "const " + decl
            if s.init is not None:
                return [f"{pad}{decl} = {self._expr(s.init)};"]
            return [f"{pad}{decl};"]
        if isinstance(s, Assign):
            return [f"{pad}{self._lvalue(s.target)} {s.op} {self._expr(s.value)};"]
        if isinstance(s, IncDec):
            target = self._lvalue(s.target)
            return [f"{pad}{s.op}{target};" if s.prefix else f"{pad}{target}{s.op};"]
        if isinstance(s, ExprStmt):
            return [f"{pad}{self._expr(s.expr)};"]
        if isinstance(s, If):
            lines = [f"{pad}if ({self._expr(s.cond)}) {{"] + self._stmt(s.then, indent + 1) + [f"{pad}}}"]
            if s.els is not None:
                lines += [f"{pad}else {{"] + self._stmt(s.els, indent + 1) + [f"{pad}}}"]
            return lines
        if isinstance(s, Return):
            if s.value is None:
                return [f"{pad}return;"]
            return [f"{pad}return {self._expr(s.value)};"]
        if isinstance(s, Block):
            lines: List[str] = []
            for stmt in s.stmts:
                lines += self._stmt(stmt, indent)
            return lines
        if isinstance(s, Empty):
            return []
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=self.an.plugin_id, plugin_version=self.an.plugin_version,
            stage=TranspileStage.GENERATE.value, backend=BACKEND_METAL,
            code="unsupported_statement", message=f"不支持的语句 {type(s).__name__}",
            line=s.line, column=s.col))

    # ── 函数与 kernel ──
    def _function(self, fn: FunctionDef, prototype: bool) -> str:
        info = self.an.fn_info[id(fn)]
        ret = _msl_type(fn.ret) if fn.ret is not None else "void"
        params = []
        for p in fn.params:
            ty = _msl_type(p.ty)
            params.append(f"{ty} {self._var(p.name)}" if p.name else f"{ty} /* unnamed */")
        params += self._lifted_decl(info)
        head = f"inline {ret} {self._fn(fn.name)}({', '.join(params)})"
        if prototype:
            return head + ";"
        if fn is self.an.process and self.match.helpers:
            # 注册表 helper 命中：以显式实参调用已登记的 MSL 实现。
            helper = self.match.helpers[0]
            bound = [self.an.layout.field_for(self.match.binding[name]).msl_expr()
                     for name in helper.glsl_param_bindings]
            body = [f"    return {helper.msl_name}({', '.join([self._var(fn.params[0].name)] + bound)});"]
        else:
            body = self._stmt(fn.body, 1) if fn.body else []
        return head + " {\n" + "\n".join(body) + "\n}"

    def _kernel(self, kernel_name: str) -> str:
        process = self.an.process
        info = self.an.fn_info[id(process)]
        is_vec3 = self.an.process_kind == "vec3"
        call_args = ["src.rgb" if is_vec3 else "src", "uv"] + self._lifted_args(info)
        written = "float4(result, src.a)" if is_vec3 else "result"   # vec3 入口：宿主保持 Alpha
        return "\n".join([
            f"kernel void {kernel_name}(",
            f"    texture2d<float> {ENGINE_INPUT_TEX} [[texture({TEXTURE_SLOT_INPUT})]],",
            f"    texture2d<float, access::write> {ENGINE_OUTPUT_TEX} [[texture({TEXTURE_SLOT_OUTPUT})]],",
            f"    constant {PARAMS_STRUCT_NAME}& params [[buffer({BUFFER_SLOT_PARAMS})]],",
            "    uint2 gid [[thread_position_in_grid]]) {",
            f"    if (gid.x >= {ENGINE_OUTPUT_TEX}.get_width() || "
            f"gid.y >= {ENGINE_OUTPUT_TEX}.get_height()) {{",
            "        return;",
            "    }",
            "    " + sampler_declaration(self.sampler),
            f"    const float2 {ENGINE_TEX_SIZE} = float2(params.u_tex_size_x, params.u_tex_size_y);",
            "    const float2 px = float2(gid) + 0.5;",
            f"    const float2 uv = {coord_uv_expression(self.coord)};",
        ] + ([f"    const float2 {ENGINE_TEX_UV} = px / {ENGINE_TEX_SIZE};"]
             if self.coord == COORD_TOP_DOWN else []) + [
            f"    const float4 src = {ENGINE_INPUT_TEX}.read(gid);",
            f"    const float{3 if is_vec3 else 4} result = "
            f"{self._fn(ENTRY_NAME)}({', '.join(call_args)});",
            f"    {ENGINE_OUTPUT_TEX}.write({written}, gid);",
            "}",
        ])

    def emit(self, kernel_name: str) -> str:
        an = self.an
        header = (f"// Generated by Topos {TRANSPILER_VERSION} for host ABI {HOST_ABI}.\n"
                  f"// plugin={an.plugin_id or '<unnamed>'} version={an.plugin_version or '<none>'}"
                  f" coord={self.coord} sampler={self.sampler}\n"
                  "// Do not edit: regenerate with transpile_tglsl().")
        parts: List[str] = [header, "#include <metal_stdlib>", "using namespace metal;",
                            an.layout.struct_text]
        # 多个宿主 helper 可能共用同一段 MSL（rgb2hsv / hsv2rgb 同在 MSL_HOST_HSV）：
        # 按去掉首尾换行后的整段文本去重，否则会发射两次导致 MSL 重定义错误。
        used_hosts = sorted({f.split(":", 1)[1] for f in an.features if f.startswith("host_helper:")})
        emitted: List[str] = []
        for name in used_hosts:
            text = _HOST_HELPER_SOURCES[name].strip("\n")
            if text not in emitted:
                emitted.append(text)
        for helper in self.match.helpers:
            emitted.append(helper.msl_source().strip("\n"))
        parts += emitted
        for const in an.unit.consts:
            parts.append(f"constexpr constant {_msl_type(const.ty)} {self._var(const.name)} = "
                         f"{self._expr(const.init)};")
        parts.append("\n".join(self._function(fn, True) for fn in an.unit.functions))
        for fn in an.unit.functions:
            parts.append(self._function(fn, False))
        parts.append(self._kernel(kernel_name))
        return "\n\n".join(parts) + "\n"


# ── 注册表 helper 的整函数体匹配 ────────────────────────────────────────


def _pattern_locals(fn: FunctionDef) -> Set[str]:
    names: Set[str] = set()

    def walk(s: Stmt) -> None:
        if isinstance(s, VarDecl):
            names.add(s.name)
        elif isinstance(s, Block):
            for stmt in s.stmts:
                walk(stmt)
        elif isinstance(s, If):
            walk(s.then)
            if s.els is not None:
                walk(s.els)

    for stmt in (fn.body.stmts if fn.body else ()):
        walk(stmt)
    return names


def _literal_value(lit: Literal) -> Any:
    if lit.ty.is_float:
        return float(lit.text)
    if lit.ty.base in ("int", "uint"):
        return int(lit.text.rstrip("uU"), 0)
    return lit.text


class _Matcher:
    """判断插件的 ``process`` 是否与 helper 的 GLSL 模式「重命名后同构」。

    ``sigma`` 把模式的 process 形参映射到插件的 process 形参；模式里出现在
    ``glsl_param_bindings`` 中的标识符绑定到插件参数（类型必须一致、一一对应）；
    其余标识符是模式的局部变量，按首次出现顺序与插件局部变量一一对应。
    """

    def __init__(self, helper: MslHelper, sigma: Dict[str, str],
                 pattern_locals: Set[str], params: Dict[str, _Symbol]) -> None:
        self.helper = helper
        self.sigma = sigma
        self.pattern_locals = pattern_locals
        self.params = params
        self.binding: Dict[str, str] = {}
        self.locals: Dict[str, str] = {}

    def block_equiv(self, a: Optional[Block], b: Optional[Block]) -> bool:
        if a is None or b is None or len(a.stmts) != len(b.stmts):
            return False
        return all(self.stmt_equiv(x, y) for x, y in zip(a.stmts, b.stmts))

    def _bind_local(self, pattern_name: str, plugin_name: str) -> bool:
        if pattern_name in self.locals:
            return self.locals[pattern_name] == plugin_name
        if plugin_name in self.locals.values():
            return False
        self.locals[pattern_name] = plugin_name
        return True

    def _ident_equiv(self, a: Ident, b: Expr) -> bool:
        if not isinstance(b, Ident):
            return False
        name = a.name
        if name in self.sigma:
            return b.name == self.sigma[name]
        if name in self.helper.glsl_param_bindings:
            index = self.helper.glsl_param_bindings.index(name)
            want = GLSL_TYPES.get(self.helper.param_binding_types[index])
            resolved = b.ref
            if want is None or not isinstance(resolved, _Symbol) or resolved.kind != _KIND_PARAM \
                    or resolved.ty != want:
                return False
            if name in self.binding:
                return self.binding[name] == b.name
            if b.name in self.binding.values():
                return False
            self.binding[name] = b.name
            return True
        if name in self.locals:
            return self.locals[name] == b.name
        if name in self.pattern_locals:
            return self._bind_local(name, b.name)
        return False

    def expr_equiv(self, a: Expr, b: Expr) -> bool:
        if type(a) is not type(b):
            return False
        if isinstance(a, Literal):
            return a.ty.base == b.ty.base and _literal_value(a) == _literal_value(b)
        if isinstance(a, Ident):
            return self._ident_equiv(a, b)
        if isinstance(a, Constructor):
            return a.ty == b.ty and len(a.args) == len(b.args) \
                and all(self.expr_equiv(x, y) for x, y in zip(a.args, b.args))
        if isinstance(a, Swizzle):
            return _map_swizzle(a.components) == _map_swizzle(b.components) \
                and self.expr_equiv(a.base, b.base)
        if isinstance(a, Index):
            return self.expr_equiv(a.base, b.base) and self.expr_equiv(a.index, b.index)
        if isinstance(a, Unary):
            return a.op == b.op and self.expr_equiv(a.operand, b.operand)
        if isinstance(a, Binary):
            return a.op == b.op and self.expr_equiv(a.left, b.left) and self.expr_equiv(a.right, b.right)
        if isinstance(a, Ternary):
            return self.expr_equiv(a.cond, b.cond) and self.expr_equiv(a.then, b.then) \
                and self.expr_equiv(a.els, b.els)
        if isinstance(a, Call):
            return a.callee == b.callee and len(a.args) == len(b.args) \
                and all(self.expr_equiv(x, y) for x, y in zip(a.args, b.args))
        return False

    def stmt_equiv(self, a: Stmt, b: Stmt) -> bool:
        if type(a) is not type(b):
            return False
        if isinstance(a, VarDecl):
            if a.ty != b.ty or a.const != b.const or (a.init is None) != (b.init is None):
                return False
            if not self._bind_local(a.name, b.name):
                return False
            return a.init is None or self.expr_equiv(a.init, b.init)
        if isinstance(a, Assign):
            return a.op == b.op and self.expr_equiv(a.target, b.target) and self.expr_equiv(a.value, b.value)
        if isinstance(a, IncDec):
            return a.op == b.op and a.prefix == b.prefix and self.expr_equiv(a.target, b.target)
        if isinstance(a, ExprStmt):
            return self.expr_equiv(a.expr, b.expr)
        if isinstance(a, If):
            branches = (a.els is None) == (b.els is None)
            return self.expr_equiv(a.cond, b.cond) and self.stmt_equiv(a.then, b.then) and branches \
                and (a.els is None or self.stmt_equiv(a.els, b.els))
        if isinstance(a, Return):
            return (a.value is None) == (b.value is None) \
                and (a.value is None or self.expr_equiv(a.value, b.value))
        if isinstance(a, Block):
            return self.block_equiv(a, b)
        return isinstance(a, Empty)

    def function_equiv(self, pattern: FunctionDef, entry: FunctionDef) -> bool:
        if pattern.ret != entry.ret or len(pattern.params) != len(entry.params):
            return False
        if any(pp.ty != qp.ty for pp, qp in zip(pattern.params, entry.params)):
            return False
        return self.block_equiv(pattern.body, entry.body)


def _match_helpers(unit: TranslationUnit, helpers: Sequence[MslHelper],
                   params: Dict[str, _Symbol]) -> _HelperMatch:
    """返回第一个与插件 ``process`` 同构的注册表 helper（没有则为空匹配）。"""
    match = _HelperMatch()
    entry = next((fn for fn in unit.functions if fn.name == ENTRY_NAME), None)
    if entry is None:
        return match
    for helper in helpers:
        try:
            pattern = parse_tglsl_glsl(helper.glsl_pattern)
        except TglslFrontError:
            continue    # 注册表自身的模式不合法：不匹配（注册表测试负责发现）
        entries = [fn for fn in pattern.functions if fn.name == ENTRY_NAME]
        if len(entries) != 1 or pattern.consts:
            continue
        pattern_entry = entries[0]
        sigma = {pp.name: qp.name for pp, qp in zip(pattern_entry.params, entry.params) if pp.name}
        matcher = _Matcher(helper, sigma, _pattern_locals(pattern_entry), params)
        if matcher.function_equiv(pattern_entry, entry):
            match.helpers = [helper]
            match.binding = dict(matcher.binding)
            return match
    return match


# ── 公开入口 ────────────────────────────────────────────────────────────


def registry_pattern_source() -> str:
    """默认注册表里曝光 helper 的 GLSL 模式（计划「参考实现 6B」左侧样本的函数体）。"""
    from src.features.shared.effects.msl_effect_registry import EXPOSURE_HELPER

    return EXPOSURE_HELPER.glsl_pattern


def transpile_binding(binding: Any, **options: Any) -> TranspiledEffect:
    """从 :class:`PluginBinding`（其 ``spec`` 为 catalog 解析产物）转译。

    ADR 0004 决策 1 的入口约束：只接受 ``PluginBinding``；原始 instance dict
    直接 ``TypeError``，不静默降级。
    """
    from src.features.shared.effects.effect_spec import PluginBinding

    if not isinstance(binding, PluginBinding):
        raise TypeError("转译入口只接受 PluginBinding（ADR 0004 G5），拒绝原始 instance 数据")
    spec = binding.spec
    plugin_id = spec.plugin_id[len("plugin:"):] if spec.plugin_id.startswith("plugin:") else spec.plugin_id
    return transpile_tglsl(
        spec.plugin_source_glsl, spec.param_defs,
        plugin_id=plugin_id, plugin_version=spec.plugin_version,
        content_hash=spec.plugin_content_hash, **options)


def transpile_tglsl(
    plugin_source: str,
    param_defs: Sequence[Any],
    *,
    plugin_id: str = "",
    plugin_version: str = "",
    content_hash: str = "",
    kernel_name: str = "topos_fx_kernel",
    coord_convention: str = COORD_GL_ROW0_IS_V0,
    sampler_spec: str = SAMPLER_LINEAR_CLAMP,
    registry_helpers: Sequence[MslHelper] = (),
) -> TranspiledEffect:
    """把一个 ``.tglsl`` 插件的着色器源转译为完整 MSL。

    Raises:
        ValueError: 坐标约定未知。
        TglslMetalError: 结构在子集外、类型不合法或入口不符合契约——诊断携带
            插件 ID、版本、阶段、后端、行列与源码片段。
    """
    if coord_convention not in COORD_CONVENTIONS:
        raise ValueError(f"未知坐标约定 {coord_convention!r}")
    try:
        layout = build_params_layout(param_defs)
    except ValueError as exc:
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=plugin_id, plugin_version=plugin_version,
            stage=TranspileStage.VALIDATE.value, backend=BACKEND_METAL,
            code="param_manifest_invalid", message=str(exc))) from exc
    if not plugin_source.strip():
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id=plugin_id, plugin_version=plugin_version,
            stage=TranspileStage.VALIDATE.value, backend=BACKEND_METAL,
            code="missing_plugin_source",
            message="插件没有可转译的 GLSL 源（EffectSpec.plugin_source_glsl 为空）"))
    try:
        unit = parse_tglsl_glsl(plugin_source)
    except TglslFrontError as exc:
        raise TglslMetalError(TglslMetalDiagnostic.from_issue(
            exc.issue, plugin_id=plugin_id, plugin_version=plugin_version)) from exc

    analyzer = _Analyzer(unit, layout, plugin_id, plugin_version)
    analyzer.validate_unit()
    analyzer.check_unit()
    match = _match_helpers(unit, registry_helpers, analyzer.params)

    emitter = _Emitter(analyzer, coord_convention, sampler_spec, match)
    source = emitter.emit(kernel_name)

    return TranspiledEffect(
        plugin_id=plugin_id, plugin_version=plugin_version,
        msl_source=source, kernel_name=kernel_name,
        params_struct=layout.struct_text, layout=layout,
        process_kind=analyzer.process_kind, coord_convention=coord_convention,
        sampler_spec=sampler_spec,
        source_fingerprint=content_hash or _sha256(plugin_source),
        msl_fingerprint=_sha256(source),
        features=tuple(sorted(analyzer.features)),
        helper_ids=tuple(sorted({h.helper_id for h in match.helpers})),
        matched_helper=match.helpers[0].helper_id if match.helpers else None)
