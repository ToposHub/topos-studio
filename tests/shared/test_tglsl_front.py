"""受限 GLSL 前端（``tglsl_front``）单元测试：优先级、AST 形状、子集外结构的明确诊断。

纯 Python，不触达 GPU。前端决定了「什么能进可译子集」；这里钉住：
- 运算符优先级与结合性必须与 GLSL 3.30 §5.9 一致（曾经写反过：``a < b && c < d``
  被解析成 ``(a < (b && c)) < d``，靠类型检查才拦住）；
- 循环 / 宏 / 扩展 / 矩阵 / 数组 / struct / 自声明 uniform 等结构给出带阶段、代码、
  行列与源码片段的诊断，而不是含糊的语法错误；
- 40 个仓库插件要么解析成功，要么因已知结构被拒（逐个钉住原因）。
"""

from __future__ import annotations

import pathlib

import pytest

from src.features.shared.effects import tglsl_front as F
from src.features.shared.effects.tglsl_front import (
    Binary,
    Call,
    Constructor,
    Index,
    Swizzle,
    TglslFrontError,
    TranspileStage,
    Ternary,
    Unary,
    parse_tglsl_glsl,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"

_HEAD = "vec3 process(vec3 color, vec2 uv) {\n"


def _wrap(body: str) -> str:
    return _HEAD + body + "\n}\n"


def _shape(e):
    if isinstance(e, Binary):
        return f"({_shape(e.left)} {e.op} {_shape(e.right)})"
    if isinstance(e, Unary):
        return f"({e.op}{_shape(e.operand)})"
    if isinstance(e, Ternary):
        return f"({_shape(e.cond)} ? {_shape(e.then)} : {_shape(e.els)})"
    if isinstance(e, Call):
        return f"{e.callee}({', '.join(_shape(a) for a in e.args)})"
    if isinstance(e, Swizzle):
        return f"({_shape(e.base)}.{e.components})"
    if isinstance(e, Index):
        return f"{_shape(e.base)}[{_shape(e.index)}]"
    if isinstance(e, Constructor):
        return f"{e.ty.name}({', '.join(_shape(a) for a in e.args)})"
    return getattr(e, "text", None) or getattr(e, "name", None) or type(e).__name__


def _expr(source: str):
    unit = parse_tglsl_glsl(_wrap("return " + source + ";"))
    return unit.functions[0].body.stmts[0].value


# ── 优先级与结合性 ──────────────────────────────────────────────────────

@pytest.mark.parametrize(("text", "expected"), [
    ("a < b && c < d", "((a < b) && (c < d))"),
    ("a + b * c", "(a + (b * c))"),
    ("a - b - c", "((a - b) - c)"),
    ("a / b * c", "((a / b) * c)"),
    ("a || b && c", "(a || (b && c))"),
    ("a | b ^ c & d", "(a | (b ^ (c & d)))"),
    ("a == b < c", "(a == (b < c))"),
    ("a << b + c", "(a << (b + c))"),
    ("-a * b", "((-a) * b)"),
    ("a < b == c", "((a < b) == c)"),
    ("x ? y : z ? w : v", "(x ? y : (z ? w : v))"),
    ("a ^^ b || c", "((a ^^ b) || c)"),
    ("f(a + b, c * d) - e", "(f((a + b), (c * d)) - e)"),
    ("a % b + c", "((a % b) + c)"),
    ("a.b.g[0 + 1]", "((a.b).g)[(0 + 1)]"),
    ("!a && -b", "((!a) && (-b))"),
])
def test_operator_precedence_matches_glsl(text, expected):
    assert _shape(_expr(text)) == expected


def test_literal_forms_and_types():
    for text, base in (("1.0", "float"), ("1e-6", "float"), (".5", "float"), ("5.", "float"),
                       ("12", "int"), ("0x1F", "int"), ("7u", "uint"), ("true", "bool")):
        node = _expr(text)
        assert node.ty.base == base, text
        assert node.text == text


def test_swizzle_families_and_calls_parse():
    node = _expr("texture(u_input_tex, uv.st).rgba + vec4(color.xyz, uv.pq)")
    assert _shape(node) == "((texture(u_input_tex, (uv.st)).rgba) + vec4((color.xyz), (uv.pq)))"


# ── 接受的语法 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", [
    "precision highp float;\n" + _wrap("return color;"),
    _wrap("float smooth = 0.5; return color * smooth;"),             # smooth 作为标识符（Frequency_Split）
    _wrap("float a = 1.0, b, c = a * 2.0; b = c; return color * b;"),
    _wrap("const float k = 0.5; { { return color * k; } }"),
    _wrap("if (a > 0.0) return color; else if (a < 0.0) { return -color; } else return color;"),
    _wrap("color.rg = color.gr; color[2] = 0.5; color += uv.x; return color;"),
    _wrap("int n = 3; n++; --n; return color;"),
    _wrap("/* 块注释 */ // 行注释\n    return color;"),
    "const vec3 W = vec3(0.2126, 0.7152, 0.0722);\n"
    "float pick(const in float x, highp float y, float) { return x + y; }\n"
    + _wrap("return color * pick(1.0, 2.0, 3.0);"),
])
def test_accepted_syntax(source):
    parse_tglsl_glsl(source)


def test_global_consts_and_functions_are_collected():
    unit = parse_tglsl_glsl(
        "const float A = 1.0;\nconst vec3 B = vec3(A, 2.0, 3.0);\n"
        "float twice(float x) { return 2.0 * x; }\n" + _wrap("return color * twice(A);"))
    assert [c.name for c in unit.consts] == ["A", "B"]
    assert [f.name for f in unit.functions] == ["twice", "process"]
    assert unit.functions[0].params[0].name == "x"


# ── 子集外结构：明确诊断 ────────────────────────────────────────────────

@pytest.mark.parametrize(("source", "code", "fragment"), [
    (_wrap("for (int i = 0; i < 4; i++) { color += 0.1; } return color;"),
     "unsupported_construct:loop", "for 循环"),
    (_wrap("while (color.r > 0.0) { color.r -= 0.1; } return color;"),
     "unsupported_construct:loop", "while 循环"),
    (_wrap("do { color *= 2.0; } while (false); return color;"),
     "unsupported_construct:loop", "do 循环"),
    (_wrap("switch (1) { case 0: color = vec3(0.0); } return color;"),
     "unsupported_construct:switch", "switch"),
    (_wrap("break;"), "unsupported_construct:break", "break"),
    (_wrap("continue;"), "unsupported_construct:continue", "continue"),
    (_wrap("discard;"), "unsupported_construct:discard", "discard"),
    ("#define N 4\n" + _wrap("return color;"), "unsupported_construct:preprocessor", "宏定义"),
    ("#version 330\n" + _wrap("return color;"), "unsupported_construct:preprocessor", "版本声明"),
    ("#extension GL_ARB_shader_group_vote : enable\n" + _wrap("return color;"),
     "unsupported_construct:preprocessor", "扩展启用"),
    ("#include \"common.glsl\"\n" + _wrap("return color;"), "unsupported_construct:preprocessor", "文件包含"),
    ("#ifdef X\n" + _wrap("return color;") + "#endif\n",
     "unsupported_construct:preprocessor", "条件编译"),
    # 反斜杠续行的多行宏：宏体整体成词，诊断指向 #define 所在行
    ("#define ACC(N) \\\n    acc += color; \\\n    acc /= float(N);\n" + _wrap("return color;"),
     "unsupported_construct:preprocessor", "宏定义"),
    ("const mat2 R = mat2(1.0);\n" + _wrap("return color;"), "unsupported_construct:matrix", "mat2"),
    (_wrap("mat3 m = mat3(1.0); return color * m[0];"), "unsupported_construct:matrix", "mat3"),
    (_wrap("sampler2D other = u_input_tex; return color;"), "unsupported_construct:sampler", "sampler2D"),
    (_wrap("float taps[4]; return color;"), "unsupported_construct:array", "数组"),
    ("float pick(float v[9], int k) { return v[k]; }\n" + _wrap("return color;"),
     "unsupported_construct:array", "数组"),
    ("struct P { float x; };\n" + _wrap("return color;"), "unsupported_construct:struct", "struct"),
    ("uniform float u_gain;\n" + _wrap("return color * u_gain;"),
     "unsupported_construct:uniform", "uniform"),
    ("float f(out float x) { x = 1.0; return x; }\n" + _wrap("return color;"),
     "unsupported_construct:out_param", "out 形参"),
    ("float g_gain = 1.0;\n" + _wrap("return color;"), "unsupported_construct:global_mutable", "const"),
    ("in vec2 v_data;\n" + _wrap("return color;"), "unsupported_construct:interface", "接口限定符"),
    ("layout(location = 0) out vec4 f_color;\n" + _wrap("return color;"),
     "unsupported_construct:interface", "layout"),
    (_wrap("{ float nested(float x) { return x; } } return color;"),
     "unsupported_construct:nested_function", "再定义函数"),
    (_wrap("return color.position;"), "unsupported_construct:struct_member", "struct 成员"),
    (_wrap("float b = 0.0; float c = 1.0; return color * (b = c);"),
     "unsupported_construct:assignment_expression", "嵌在表达式里"),
    ("/* 未闭合\n" + _wrap("return color;"), "unterminated_comment", "未闭合"),
    (_wrap("return color @ uv;"), "unexpected_char", "'@'"),
    (_HEAD + "    return color", "syntax", "文件结尾"),
])
def test_out_of_subset_constructs_get_explicit_diagnostics(source, code, fragment):
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(source)
    issue = info.value.issue
    assert issue.stage == TranspileStage.PARSE.value
    assert issue.code == code
    assert fragment in issue.message
    assert issue.format().startswith(f"[parse] {code} (")


def test_identifier_prefixes_are_not_mistaken_for_types():
    """`material` / `matte` / `sampler_gain` 是合法标识符，不能被 mat*/sampler* 规则误杀。"""
    parse_tglsl_glsl(_wrap("float material = 1.0; float matte = 2.0; return color * material * matte;"))


def test_diagnostic_carries_line_column_and_snippet():
    source = ("vec3 process(vec3 color, vec2 uv) {\n"
              "    color *= 2.0;\n"
              "    for (int i = 0; i < 2; i++) {\n"
              "        color += 0.1;\n"
              "    }\n"
              "    return color;\n"
              "}\n")
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(source)
    issue = info.value.issue
    assert (issue.line, issue.column) == (3, 5)
    assert issue.snippet == "for (int i = 0; i < 2; i++) {"


def test_line_numbers_survive_block_comments():
    source = "/* 多行\n   块注释 */\n" + "vec3 process(vec3 color, vec2 uv) {\n    float x = ;\n}\n"
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(source)
    assert info.value.issue.line == 4
    assert info.value.issue.snippet == "float x = ;"


def test_multiline_macro_diagnostic_points_at_the_define_line():
    source = ("#define ACC(N) \\\n    acc += 1.0; \\\n    return acc;\n"
              "vec3 process(vec3 color, vec2 uv) { return color; }\n")
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(source)
    assert (info.value.issue.line, info.value.issue.column) == (1, 1)
    assert info.value.issue.snippet.startswith("#define ACC(N)")


def test_hash_not_at_line_start_is_rejected():
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(_wrap("return color # 1.0;"))
    assert info.value.issue.code == "unexpected_char"


# ── 仓库插件：逐个钉住解析结论 ──────────────────────────────────────────

#: 因结构在子集外而被前端拒绝的仓库插件 → 诊断代码。其余插件必须解析成功。
_EXPECTED_REJECTIONS = {
    "Anamorphic_Streak": "unsupported_construct:preprocessor",
    "Bokeh_Defocus": "unsupported_construct:array",
    "Color_Space_Transform": "unsupported_construct:matrix",
    "God_Rays": "unsupported_construct:loop",
    "Halation_Extended": "unsupported_construct:array",
    "Halftone_Print": "unsupported_construct:matrix",
    "LIB1_Median_Rank": "unsupported_construct:array",
    "Motion_Blur_Linear": "unsupported_construct:preprocessor",
    "Pixel_Blocks": "unsupported_construct:array",
    "Prism_Aberration": "unsupported_construct:matrix",
    "Radial_Blur_Pro": "unsupported_construct:preprocessor",
    "Skin_Even_Matte": "unsupported_construct:array",
    "Soft_Glow_Bloom": "unsupported_construct:array",
    "Tilt_Shift_Focus": "unsupported_construct:array",
    "Unsharp_Sharpen": "unsupported_construct:preprocessor",
    "bilateral_skin_smooth": "unsupported_construct:loop",
}


def _real_plugin_code(plugin_id: str) -> str:
    from src.features.color.core.plugin_manager import PluginManager

    data = PluginManager.__new__(PluginManager)._parse_plugin_file(
        str(PLUGINS_DIR / f"{plugin_id}.tglsl"), plugin_id)
    assert data is not None and data["type"] == "dctl"
    return data["shader_code"]


def test_repository_plugin_count_is_stable():
    files = sorted(p.stem for p in PLUGINS_DIR.glob("*.tglsl"))
    assert len(files) == 40, "仓库插件数量变化：请同步 _EXPECTED_REJECTIONS 与兼容矩阵"
    assert set(files) == set(_EXPECTED_REJECTIONS) | {
        "Bleach_Bypass", "CRT_Scanlines", "Channel_Mixer", "Cross_Process", "Day_Night_Converter",
        "Duotone_Toner", "Edge_Ink_Outline", "Fake_Infrared", "Film_Grain", "Film_Halation",
        "Frequency_Split", "Glitch_Blocks", "Grad_Filter_Exposure", "HighPass_Retouch",
        "Kaleido_Mirror", "LIB1_Gradient_Map", "Lens_Distortion", "Light_Leak_Corner",
        "Sky_Replace_Matte", "Three_Way_Tint", "Vibrance_Saturation", "Vignette_Shapes",
        "White_Balance_Advanced", "warm_tint",
    }


@pytest.mark.parametrize("plugin_id", sorted(_EXPECTED_REJECTIONS))
def test_repository_plugin_rejected_for_the_known_structural_reason(plugin_id):
    with pytest.raises(TglslFrontError) as info:
        parse_tglsl_glsl(_real_plugin_code(plugin_id))
    issue = info.value.issue
    assert issue.code == _EXPECTED_REJECTIONS[plugin_id], issue.format()
    assert issue.line > 0 and issue.snippet, "诊断必须带行号与源码片段"


def test_repository_plugin_every_rejection_names_a_real_plugin():
    files = {p.stem for p in PLUGINS_DIR.glob("*.tglsl")}
    assert set(_EXPECTED_REJECTIONS) <= files


def test_front_end_has_no_module_state():
    source = _real_plugin_code("Channel_Mixer")
    first = parse_tglsl_glsl(source)
    second = parse_tglsl_glsl(source)
    assert [f.name for f in first.functions] == [f.name for f in second.functions]
    assert F.TRANSPILER_VERSION.startswith("tglsl-msl/")
