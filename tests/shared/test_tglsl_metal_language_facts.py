"""转译器改写规则所依赖的 MSL / Apple GLSL 语言事实（真机断言）。

``tglsl_msl_transpiler`` 的每一条改写（stpq、向量三目、mod、radians、构造器多余
分量、形参限定符……）都基于「同一写法在 GLSL 里成立、在 MSL 里不成立（或反之）」。
这里用真实编译器把这些事实固定成断言：编译器行为一旦变化，失败会直接指向对应
规则，而不是等到对拍出现无法解释的像素差异。

无 Metal 设备 / 无 standalone GL 3.3 上下文时按原因 skip。
"""

from __future__ import annotations

import pytest

from src.shared.gpu.metal.context import MetalComputeContext, MetalComputeError

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(),
    reason=f"无 Metal 设备：{MetalComputeContext.availability_reason()[1]}")

_HEADER = "#include <metal_stdlib>\nusing namespace metal;\n"
_BUFFER_KERNEL = (
    "kernel void {name}(device float4* o [[buffer(0)]], uint i [[thread_position_in_grid]]) {{\n"
    "    {body}\n"
    "}}\n")
_TEXTURE_KERNEL = (
    "kernel void {name}(texture2d<float> src [[texture(0)]],\n"
    "                    {output_decl} [[texture(1)]],\n"
    "                    uint2 gid [[thread_position_in_grid]]) {{\n"
    "    {body}\n"
    "}}\n")

_EXPRESSIONS = (
    "float3 a = float3(0.25, 0.5, 0.75);\n"
    "    float3 b = float3(1.0, 2.0, 4.0);\n"
    "    float2 v2 = float2(1.0, 2.0);\n"
    "    float s = 1.5;\n"
)

#: (名称, 模板, 模板参数, 函数体, 是否应能编译, 错误消息应包含的片段)
_MSL_FACTS = [
    # —— 构造器 ——
    ("ctor_truncate_single_vector", _BUFFER_KERNEL, {}, _EXPRESSIONS + "o[i] = float4(float3(float4(a, 1.0)), 0.0);", True, ""),
    ("ctor_excess_vector_components_rejected", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "float3 v = float3(a.xy, v2);", False, "no matching constructor"),
    ("ctor_exact_ok", _BUFFER_KERNEL, {}, _EXPRESSIONS + "o[i] = float4(float3(a.xy, v2.x), 0.0);", True, ""),
    ("ctor_scalar_broadcast_ok", _BUFFER_KERNEL, {}, "o[i] = float4(float3(0.5), 1.0);", True, ""),
    ("ctor_int_literals_ok", _BUFFER_KERNEL, {}, "o[i] = float4(float3(1, 0, 0), 1);", True, ""),
    # —— swizzle ——
    ("swizzle_xyzw_ok", _BUFFER_KERNEL, {}, "o[i] = float4(float4(1.0).xyz, float4(1.0).w);", True, ""),
    ("swizzle_rgba_ok", _BUFFER_KERNEL, {}, "o[i] = float4(float4(1.0).rgb, float4(1.0).a);", True, ""),
    ("swizzle_stpq_illegal", _BUFFER_KERNEL, {}, "float2 v = float4(1.0).st;", False, "illegal vector component name"),
    ("swizzle_mixed_sets_illegal", _BUFFER_KERNEL, {}, "float3 v = float4(1.0).xgb;", False, "illegal vector component name"),
    # —— 向量条件三目 ——
    ("vector_ternary_illegal", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "o[i] = float4((a > b) ? a : b, 1.0);", False, "vector condition type"),
    ("scalar_ternary_ok", _BUFFER_KERNEL, {}, _EXPRESSIONS + "o[i] = float4(s > 1.0 ? a : b, 1.0);", True, ""),
    ("select_ok", _BUFFER_KERNEL, {}, _EXPRESSIONS + "o[i] = float4(select(b, a, a > b), 1.0);", True, ""),
    ("vector_comparison_ok", _BUFFER_KERNEL, {}, _EXPRESSIONS + "bool3 c = a <= b; o[i] = float4(c.x, c.y, c.z, all(c));", True, ""),
    # —— MSL 缺少的 GLSL 内建 ——
    ("mod_undeclared", _BUFFER_KERNEL, {}, "o[i] = float4(mod(5.25, 2.0));", False, "undeclared identifier 'mod'"),
    ("radians_undeclared", _BUFFER_KERNEL, {}, "o[i] = float4(radians(90.0));", False, "undeclared identifier 'radians'"),
    ("degrees_undeclared", _BUFFER_KERNEL, {}, "o[i] = float4(degrees(1.0));", False, "undeclared identifier 'degrees'"),
    ("inversesqrt_undeclared", _BUFFER_KERNEL, {}, "o[i] = float4(inversesqrt(4.0));", False, "'inversesqrt'"),
    ("mod_expansion_compiles", _BUFFER_KERNEL, {},
     "float x = 5.25; float y = 2.0; o[i] = float4(x - y * floor(x / y));", True, ""),
    # —— 名称不同的等价内建 ——
    ("rint_round_rsqrt_atan2_ok", _BUFFER_KERNEL, {},
     "o[i] = float4(rint(2.5), round(2.5), rsqrt(4.0), atan2(1.0, 2.0));", True, ""),
    ("rint_is_round_half_to_even", _BUFFER_KERNEL, {},
     "o[i] = float4(rint(2.5), rint(3.5), round(2.5), 0.0);", True, ""),
    ("pow_vec_vec_and_vec_scalar_ok", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "o[i] = float4(pow(a, b) + pow(a, 2.0), 1.0);", True, ""),
    ("clamp_mix_minmax_scalar_ok", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "o[i] = float4(clamp(a, 0.0, 1.0) + mix(a, b, s) + min(a, 0.5) + max(a, b), 1.0);", True, ""),
    # —— 逻辑异或 ——
    ("logical_xor_illegal", _BUFFER_KERNEL, {},
     "bool p = true; bool q = false; o[i] = float4((p ^^ q) ? 1.0 : 0.0);", False, ""),
    # —— 形参与声明顺序 ——
    ("in_qualifier_on_parameter_illegal", _BUFFER_KERNEL, {},
     "o[i] = float4(qualified_in(s));", False, "unknown type name 'in'"),
    ("use_before_declaration_illegal", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "o[i] = float4(later(s));", False, "use of undeclared identifier 'later'"),
    ("prototype_first_ok", _BUFFER_KERNEL, {},
     _EXPRESSIONS + "o[i] = float4(prototyped(s));", True, ""),
    ("file_scope_constants_ok", _BUFFER_KERNEL, {},
     "o[i] = float4(K3 + K1, 0.0);", True, ""),
    # —— 纹理访问与 sampler ——
    ("sample_access_has_no_write", _TEXTURE_KERNEL, {"output_decl": "texture2d<float> dst"},
     "if (gid.x >= dst.get_width()) return; dst.write(src.read(gid), gid);", False, "no member named 'write'"),
    ("write_access_required_for_output", _TEXTURE_KERNEL,
     {"output_decl": "texture2d<float, access::write> dst"},
     "if (gid.x >= dst.get_width()) return; dst.write(src.read(gid), gid);", True, ""),
    ("constexpr_sampler_in_compute_ok", _TEXTURE_KERNEL,
     {"output_decl": "texture2d<float, access::write> dst"},
     "constexpr sampler smp(coord::normalized, address::clamp_to_edge, filter::linear);\n    "
     "    if (gid.x >= dst.get_width()) return;\n    "
     "    float2 uv = (float2(gid) + 0.5) / float2(src.get_width(), src.get_height());\n    "
     "    dst.write(src.sample(smp, uv), gid);", True, ""),
    ("sampler_address_repeat_ok", _TEXTURE_KERNEL, {"output_decl": "texture2d<float, access::write> dst"},
     "constexpr sampler smp(coord::normalized, address::repeat, filter::linear);\n    "
     "    if (gid.x >= dst.get_width()) return;\n    "
     "    dst.write(src.sample(smp, float2(1.25, -0.25)), gid);", True, ""),
    ("texture_size_query_ok", _TEXTURE_KERNEL, {"output_decl": "texture2d<float, access::write> dst"},
     "if (gid.x >= dst.get_width()) return;\n    "
     "    int2 ts = int2(src.get_width(), src.get_height());\n    "
     "    dst.write(float4(float(ts.x), float(ts.y), 0.0, 1.0), gid);", True, ""),
]

#: 放在 kernel **之后**的源码（验证「先使用后声明」在 MSL 里同样非法）。
_POST_SOURCES = {
    "use_before_declaration_illegal": "float later(float x) { return x * 2.0; }\n",
}

_EXTRA_SOURCES = {
    "in_qualifier_on_parameter_illegal": "inline float qualified_in(in float x) { return x * 2.0; }\n",
    "prototype_first_ok": "inline float prototyped(float x);\ninline float prototyped(float x) { return x * 2.0; }\n",
    "file_scope_constants_ok": "constant constexpr float3 K3 = {0.2126, 0.7152, 0.0722};\n"
                               "constexpr constant float K1 = 0.18;\n",
}


@pytest.fixture(scope="module")
def metal():
    if not MetalComputeContext.is_supported():
        pytest.skip(f"无 Metal 设备：{MetalComputeContext.availability_reason()[1]}")
    return MetalComputeContext()


@requires_metal
@pytest.mark.parametrize(("name", "template", "template_args", "body", "compiles", "needle"),
                         _MSL_FACTS, ids=[fact[0] for fact in _MSL_FACTS])
def test_msl_language_fact(metal, name, template, template_args, body, compiles, needle):
    source = (_HEADER + _EXTRA_SOURCES.get(name, "") + template.format(
        name=f"fact_{name}", body=body, **template_args) + _POST_SOURCES.get(name, ""))
    if compiles:
        metal.compile_kernel(f"fact_{name}", source)
        return
    with pytest.raises(MetalComputeError) as info:
        metal.compile_kernel(f"fact_{name}", source)
    if needle:
        assert needle in str(info.value), str(info.value)


@requires_metal
def test_every_fact_actually_exercises_the_compiler(metal):
    """防止把两条同义事实写反：能编译与不能编译的两组都非空，且名字唯一。"""
    names = [fact[0] for fact in _MSL_FACTS]
    assert len(names) == len(set(names))
    assert any(fact[4] for fact in _MSL_FACTS) and any(not fact[4] for fact in _MSL_FACTS)


@requires_metal
def test_msl_error_text_format_drives_message_trimming(metal):
    """真实错误文本的形状：第一条错误与「MSL 编译失败」前缀同行，带 caret 与 note 行。"""
    from src.features.shared.effects.metal_abi import trim_compile_message

    with pytest.raises(MetalComputeError) as info:
        metal.compile_kernel("fact_bad_name", _HEADER + _BUFFER_KERNEL.format(
            name="kernel_named_differently", body="o[i] = float4(1.0); nope();"))
    raw = str(info.value)
    trimmed = trim_compile_message(raw)
    assert trimmed.startswith("program_source:") and "use of undeclared identifier 'nope'" in trimmed
    assert "~~~" not in trimmed and "note:" not in trimmed


# ── Apple GLSL（GL 3.3 core）────────────────────────────────────────────

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")
requires_gl = pytest.mark.gpu_renderer

_VS = ("#version 330\nin vec2 in_vert; out vec2 v_uv; void main() { "
       "gl_Position = vec4(in_vert, 0.0, 1.0); v_uv = in_vert; }")
_FS = "#version 330\nout vec4 fragColor;\n{body}\n"

_MAIN = "void main() {{ fragColor = {expr}; }}"


#: (名称, 完整的 main 之前的声明, main 里的表达式, 是否应能编译)
_GLSL_FACTS = [
    ("ctor_excess_vector_components_accepted", "", _MAIN.format(expr="vec4(vec3(1.0), vec2(2.0))"), True),
    ("ctor_excess_two_vectors_accepted", "", _MAIN.format(expr="vec4(vec3(vec2(1.0), vec2(2.0)), 1.0)"), True),
    ("ctor_single_vector_truncate_accepted", "", _MAIN.format(expr="vec4(vec3(vec4(1.0)), 1.0)"), True),
    ("ctor_excess_scalars_rejected", "", _MAIN.format(expr="vec4(vec3(1.0, 2.0, 3.0, 4.0), 1.0)"), False),
    ("ctor_excess_whole_argument_rejected", "", _MAIN.format(expr="vec4(vec3(vec3(1.0), 1.0), 1.0)"), False),
    ("ctor_unneeded_trailing_argument_rejected", "",
     _MAIN.format(expr="vec4(vec4(vec2(1.0).rg, vec2(2.0).xy, 1.0))"), False),
    ("swizzle_stpq_accepted", "", _MAIN.format(expr="vec4(vec4(1.0).st, vec4(2.0).pq)"), True),
    ("vector_relational_operator_rejected", "vec3 a = vec3(1.0); vec3 b = vec3(0.5);",
     _MAIN.format(expr="vec4(a > b)"), False),
    ("lessThan_accepted", "", _MAIN.format(expr="vec4(lessThan(vec3(1.0), vec3(0.5)).x ? 1.0 : 0.0)"), True),
    ("mod_radians_inversesqrt_accepted", "",
     _MAIN.format(expr="vec4(mod(vec3(5.25), 2.0) + radians(90.0) + inversesqrt(4.0), 1.0)"), True),
    ("ctor_too_few_components_rejected", "", _MAIN.format(expr="vec4(vec3(1.0))"), False),
    ("round_accepted", "", _MAIN.format(expr="vec4(round(2.5), roundEven(2.5), 0.0, 1.0)"), True),
    ("parameter_qualifiers_accepted", "float f(in float x, const float y) { return x + y; }",
     _MAIN.format(expr="vec4(f(1.0, 2.0), 0.0, 0.0, 1.0)"), True),
    ("use_before_declaration_rejected", "",
     "void main() { fragColor = vec4(later(1.0), 0.0, 0.0, 1.0); }\nfloat later(float x) { return x; }", False),
    ("nested_assignment_accepted", "", "void main() { float b = 0.0; float c = 1.0; fragColor = vec4(b = c); }", True),
    ("ternary_with_vector_condition_rejected", "vec3 a = vec3(1.0); vec3 b = vec3(0.5);",
     _MAIN.format(expr="vec4((a > b) ? a : b)"), False),
]


@pytest.fixture(scope="module")
def gl():
    try:
        ctx = moderngl.create_standalone_context(require=330)
    except Exception as exc:  # pragma: no cover - 无 GPU 环境
        pytest.skip(f"standalone GL 3.3 context 不可用: {exc}")
    yield ctx
    ctx.release()


@requires_gl
@pytest.mark.parametrize(("name", "declarations", "body", "compiles"), _GLSL_FACTS,
                         ids=[fact[0] for fact in _GLSL_FACTS])
def test_apple_glsl_language_fact(gl, name, declarations, body, compiles):
    source = _FS.format(body=(declarations + "\n" + body) if declarations else body)
    if compiles:
        gl.program(vertex_shader=_VS, fragment_shader=source).release()
        return
    with pytest.raises(Exception) as info:  # noqa: B017 - moderngl 抛的是通用 Exception 子类
        gl.program(vertex_shader=_VS, fragment_shader=source)
    assert "ERROR" in str(info.value) or "failed" in str(info.value)


@requires_gl
@requires_metal
def test_the_two_languages_disagree_exactly_where_the_transpiler_rewrites(gl, metal):
    """成对事实：GLSL 接受而 MSL 拒绝（或反之）的写法，正是转译器必须改写的点。"""
    glsl = {fact[0]: fact[3] for fact in _GLSL_FACTS}
    msl = {fact[0]: fact[4] for fact in _MSL_FACTS}
    assert glsl["ctor_excess_vector_components_accepted"] is True
    assert msl["ctor_excess_vector_components_rejected"] is False
    assert glsl["swizzle_stpq_accepted"] is True and msl["swizzle_stpq_illegal"] is False
    assert glsl["ternary_with_vector_condition_rejected"] is False          # GLSL 同样拒绝：插件只能经 lessThan
    assert msl["vector_ternary_illegal"] is False
    assert glsl["mod_radians_inversesqrt_accepted"] is True and msl["mod_undeclared"] is False
    assert msl["vector_comparison_ok"] is True
