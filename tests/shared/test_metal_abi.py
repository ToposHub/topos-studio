"""Metal 效果宿主 ABI（``metal_abi``）与参数打包（``metal_effect_runtime``）单元测试。

纯 Python，不触达 GPU。钉住：
- 参数结构体布局沿用调色域 composition 参数缓冲的 P6-06 规则（与
  ``CompositionUniformManifest`` 逐字段对齐）；
- 编译缓存键对「源码指纹、宿主 ABI、编译参数、像素格式、设备能力、插件版本、
  转译器/helper 版本、坐标约定、sampler 规格」每一项都敏感；
- 参数取值口径与生产 GL 打包器 ``EffectPassRunner._pack_plugin_params`` 一致；
- 编译消息精简保留第一条错误（真实错误文本的第一条与 ``MSL 编译失败`` 前缀同行）。
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from src.features.composite.nodes.base_node import ParamDef
from src.features.shared.effects import metal_abi as A
from src.features.shared.effects.effect_pass_runner import EffectPassRunner
from src.features.shared.effects.effect_spec import EffectSpec, PluginBinding, ShaderKind
from src.features.shared.effects.metal_effect_runtime import (
    binding_param_values,
    pack_params,
    resolve_param_value,
    schema_default_values,
)
from src.features.shared.effects.tglsl_front import TglslMetalError


def _param_defs():
    return (
        ParamDef("amount", "float", 0.5),
        ParamDef("mode", "option", "b", labels=("a", "b")),
        ParamDef("tint", "color", (0.2, 0.3, 0.4)),
        ParamDef("flip", "bool", True),
    )


def _spec(param_defs=None):
    return EffectSpec(
        effect_type="plugin:synthetic", name="synthetic", category="plugin",
        param_defs=_param_defs() if param_defs is None else param_defs,
        shader_kind=ShaderKind.DCTL_PROCESS,
        shader_sources=("vec3 process(vec3 color, vec2 uv) { return color; }",),
        dispatch_key="plugin:synthetic", plugin_id="plugin:synthetic", plugin_version="1.0.0",
        plugin_source_glsl="vec3 process(vec3 color, vec2 uv) { return color; }\n")


# ── 参数缓冲布局 ────────────────────────────────────────────────────────


def test_layout_follows_declared_order_with_4_byte_packed_scalars():
    layout = A.build_params_layout(_param_defs())
    assert layout.numpy_dtype.descr == [
        ("u_tex_size_x", "<f4"), ("u_tex_size_y", "<f4"),
        ("p_amount", "<f4"), ("p_mode", "<i4"),
        ("p_tint_x", "<f4"), ("p_tint_y", "<f4"), ("p_tint_z", "<f4"),
        ("p_flip", "<i4"),
    ]
    assert layout.byte_length == 32
    assert layout.struct_text == (
        "struct ToposFxParams {\n"
        "    float u_tex_size_x;\n"
        "    float u_tex_size_y;\n"
        "    float p_amount;\n"
        "    int p_mode;\n"
        "    float p_tint_x;\n"
        "    float p_tint_y;\n"
        "    float p_tint_z;\n"
        "    int p_flip;\n"
        "};")
    assert layout.field_for("tint").msl_expr() == "float3(params.p_tint_x, params.p_tint_y, params.p_tint_z)"
    assert layout.field_for("flip").msl_expr() == "params.p_flip"
    assert layout.field_for("nope") is None and layout.field_for("") is None
    assert A.build_params_layout(()).byte_length == 8          # 没有参数也要有 u_tex_size
    assert A.layout_fingerprint(layout) == A.layout_fingerprint(A.build_params_layout(_param_defs()))
    assert A.layout_fingerprint(layout) != A.layout_fingerprint(
        A.build_params_layout(_param_defs()[:1]))


def test_layout_matches_composition_parameter_buffer_convention():
    """与调色域组合参数缓冲同一布局规则：类型、分量数、相对偏移逐字段相同。"""
    from src.features.color.core.composition_uniforms import build_composition_uniform_manifest

    manifest = build_composition_uniform_manifest(bindings=[PluginBinding(spec=_spec())])
    theirs = [d for d in manifest.value_decls() if d.name.startswith("u_P0_")]
    base = theirs[0].offset
    theirs_layout = [(d.glsl_type, d.components, d.offset - base) for d in theirs]
    offset = 0
    ours_layout = []
    for field in A.build_params_layout(_param_defs()).param_fields:
        ours_layout.append((field.glsl_type, len(field.fields), offset))
        offset += field.byte_length
    assert ours_layout == theirs_layout
    assert [d.glsl_type for d in theirs] == ["float", "int", "vec3", "int"]   # DCTL ABI：checkbox/select = int


def test_layout_rejects_param_without_glsl_projection():
    with pytest.raises(ValueError, match="G5"):
        A.build_params_layout((ParamDef("levels", "int", 8), ParamDef("path", "file"),))


def test_struct_text_has_no_padding_risk():
    """全部成员都是 4 字节标量：MSL 结构体与 numpy dtype 逐字段对齐，不存在填充。"""
    layout = A.build_params_layout(_param_defs())
    for field in layout.fields:
        assert field.msl_scalar in ("float", "int")
        assert set(field.np_types) <= {"<f4", "<i4"}
    assert sum(f.byte_length for f in layout.fields) == layout.numpy_dtype.itemsize


# ── 编译缓存键 ──────────────────────────────────────────────────────────


def _key(**overrides):
    base = dict(
        host_abi=A.HOST_ABI, effect_id="plugin:warm_tint", msl_fingerprint="m1",
        source_fingerprint="s1", plugin_version="1.1", plugin_content_hash="c1",
        param_schema_hash="h1", layout_fingerprint="l1", compile_options=A.COMPILE_OPTIONS_CONTEXT_DEFAULT,
        pixel_format=A.PIXEL_FORMAT_RGBA16F, device_capability="d1",
        translator_version="tglsl-msl/1.0.0", helper_versions=(("builtin:exposure", "1.0.0"),),
        coord_convention=A.COORD_GL_ROW0_IS_V0, sampler_spec=A.SAMPLER_LINEAR_CLAMP)
    base.update(overrides)
    return A.MetalCompileKey(**base)


def test_compile_key_is_stable_and_kernel_name_embeds_the_digest():
    key = _key()
    assert key.digest == _key().digest
    assert len(key.digest) == 40 and key.kernel_name == f"topos_fx_{key.digest[:20]}"
    assert key.to_dict()["digest"] == key.digest


@pytest.mark.parametrize("overrides", [
    {"host_abi": "topos.tglsl-msl/2"},
    {"msl_fingerprint": "m2"},
    {"source_fingerprint": "s2"},
    {"plugin_version": "1.2"},
    {"plugin_content_hash": "c2"},
    {"param_schema_hash": "h2"},
    {"layout_fingerprint": "l2"},
    {"compile_options": "context-default(fast_math=off,language=default)"},
    {"pixel_format": A.PIXEL_FORMAT_RGBA8},
    {"device_capability": "d2"},
    {"translator_version": "tglsl-msl/1.0.1"},
    {"helper_versions": (("builtin:exposure", "1.0.1"),)},
    {"coord_convention": A.COORD_TOP_DOWN},
    {"sampler_spec": A.SAMPLER_LINEAR_REPEAT},
])
def test_every_cache_key_member_changes_the_digest(overrides):
    """任一成员变化都意味着旧产物不能代表新请求：必须得到不同的 kernel 名。"""
    assert _key(**overrides).digest != _key().digest
    assert _key(**overrides).kernel_name != _key().kernel_name


def test_helper_version_order_does_not_change_the_digest():
    a = _key(helper_versions=(("a", "1"), ("b", "2")))
    assert a.digest == _key(helper_versions=(("b", "2"), ("a", "1"))).digest


def test_host_abi_is_versioned():
    assert A.HOST_ABI == f"{A.HOST_ABI_ID}/{A.HOST_ABI_VERSION}" == "topos.tglsl-msl/1"
    assert (A.TEXTURE_SLOT_INPUT, A.TEXTURE_SLOT_OUTPUT, A.BUFFER_SLOT_PARAMS) == (0, 1, 0)


# ── 设备能力指纹 ────────────────────────────────────────────────────────


class _FakeDevice:
    def __init__(self, families=(True, False)):
        self._families = iter(families * 50)

    def name(self):
        return "Fake GPU"

    def registryID(self):
        return 7

    def hasUnifiedMemory(self):
        return True

    def maxThreadgroupMemoryLength(self):
        return 65536

    def argumentBuffersSupport(self):
        return 1

    def supportsFamily_(self, family):
        return next(self._families)


def test_device_fingerprint_is_stable_and_tracks_capabilities():
    assert A.device_capability_fingerprint(_FakeDevice()) == A.device_capability_fingerprint(_FakeDevice())
    assert A.device_capability_fingerprint(_FakeDevice((True, False))) != \
        A.device_capability_fingerprint(_FakeDevice((False, True)))
    assert A.device_capability_fingerprint(_FakeDevice()) != \
        A.device_capability_fingerprint(_FakeDevice((True, True)))
    bare = A.device_capability_fingerprint(object())          # 测试替身缺少方法：按未知处理，不抛错
    assert len(bare) == 16 and bare != A.device_capability_fingerprint(_FakeDevice())


# ── sampler / 坐标约定 ──────────────────────────────────────────────────


def test_sampler_and_coordinate_specs():
    assert A.sampler_declaration(A.SAMPLER_LINEAR_CLAMP) == (
        "constexpr sampler u_sampler(coord::normalized, address::clamp_to_edge, filter::linear);")
    assert A.sampler_declaration(A.SAMPLER_LINEAR_REPEAT) == (
        "constexpr sampler u_sampler(coord::normalized, address::repeat, filter::linear);")
    assert "filter::nearest" in A.sampler_declaration(A.SAMPLER_NEAREST_CLAMP)
    assert A.coord_uv_expression(A.COORD_GL_ROW0_IS_V0) == "px / u_tex_size"
    assert A.coord_uv_expression(A.COORD_TOP_DOWN) == (
        "float2(px.x / u_tex_size.x, (u_tex_size.y - px.y) / u_tex_size.y)")
    assert A.sample_coord_expression(A.COORD_GL_ROW0_IS_V0, "uv") == "uv"
    assert A.sample_coord_expression(A.COORD_TOP_DOWN, "uv") == "float2((uv).x, 1.0 - (uv).y)"
    for bad in ("linear+mirror", ""):
        with pytest.raises(ValueError):
            A.sampler_declaration(bad)
    with pytest.raises(ValueError):
        A.coord_uv_expression("upside_down")
    with pytest.raises(ValueError):
        A.sample_coord_expression("upside_down", "uv")


@pytest.mark.parametrize("height", [1, 2, 3, 1080, 2160])
def test_the_two_coordinate_conventions_are_exact_mirrors_in_fp32(height):
    """Row y of ``top_down`` and row H-1-y of ``gl_row0_is_v0`` are the same
    float32 value, bit for bit: both compute (H - 0.5 - y) / H directly.  The
    naive ``1 - (y + 0.5) / H`` is the same number in exact arithmetic but
    one ulp off in fp32, which is why the expression is spelled as it is."""
    f32 = np.float32
    y = np.arange(height, dtype=f32)
    gl_row0 = (y + f32(0.5)) / f32(height)                   # px / u_tex_size
    top_down = (f32(height) - (y + f32(0.5))) / f32(height)  # of row y
    assert top_down.tobytes() == gl_row0[::-1].copy().tobytes()
    naive = f32(1.0) - gl_row0
    if height > 2:
        assert not np.array_equal(naive.tobytes(), top_down.tobytes())


# ── 参数取值与打包 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("values", "raw_defaults"), [
    ({}, {}),
    ({"mode": "a", "amount": 0.25}, {}),
    ({"mode": 1, "flip": 0}, {}),
    ({"mode": "0"}, {}),
    ({"mode": "not-a-label"}, {}),
    ({"tint": (0.1, 0.2, 0.3)}, {}),
    ({"tint": 0.5}, {}),
    ({"tint": (0.1, 0.2)}, {}),                                    # 不足 3 个分量：用末分量补齐
])
def test_param_values_match_the_gl_runner_packer(values, raw_defaults):
    spec = _spec()
    mine = {name: resolve_param_value(pdef, values.get(name), raw_defaults.get(name))
            for name, pdef in zip((p.name for p in spec.param_defs), spec.param_defs)}
    theirs = EffectPassRunner._pack_plugin_params(spec, values)
    assert {f"u_{k}": v for k, v in mine.items()} == theirs


def test_raw_tglsl_defaults_take_precedence_over_schema_defaults():
    """带 ``defaults`` 的 PluginBinding（Color 页口径）：缺值时先回退到 .tglsl 原始默认值。

    生产 GL 打包器没有这一层（它只看 ``ParamDef.default``），所以这不是「与 GL
    打包器一致」的范畴，而是绑定自带的取值来源。
    """
    spec = _spec()
    raw = {"mode": 0, "tint": [0.9, 0.8, 0.7], "flip": 0, "amount": 0.75}
    assert binding_param_values(PluginBinding(spec=spec, values={}, defaults=raw, index=0)) == {
        "amount": 0.75, "mode": 0, "tint": (0.9, 0.8, 0.7), "flip": 0}
    # 显式给出的值永远优先于原始默认值
    assert binding_param_values(PluginBinding(spec=spec, values={"mode": "a"}, defaults=raw, index=0))["mode"] == 0


def test_binding_and_schema_default_values():
    binding = PluginBinding(spec=_spec(), values={"amount": 0.9}, defaults={"mode": 0, "flip": 0}, index=3)
    assert binding_param_values(binding) == {
        "amount": 0.9, "mode": 0, "tint": (0.2, 0.3, 0.4), "flip": 0}
    assert schema_default_values(_param_defs()) == {
        "amount": 0.5, "mode": 1, "tint": (0.2, 0.3, 0.4), "flip": 1}


def test_pack_params_writes_each_field_and_is_independent_per_buffer():
    layout = A.build_params_layout(_param_defs())
    values = {"amount": 0.25, "mode": 0, "tint": (0.1, 0.2, 0.3), "flip": 1}
    first = pack_params(layout, values, 19, 13)
    second = pack_params(layout, dict(values, amount=0.75), 19, 13)
    assert first["u_tex_size_x"] == 19.0 and first["u_tex_size_y"] == 13.0
    assert (first["p_amount"], first["p_mode"], first["p_tint_y"], first["p_flip"]) == (0.25, 0, 0.2, 1)
    assert second["p_amount"] == 0.75 and first["p_amount"] == 0.25          # 各自独立的缓冲
    assert first.tobytes() != second.tobytes()
    assert pack_params(layout, values, 19, 13).tobytes() == first.tobytes()
    assert all(np.issubdtype(layout.numpy_dtype[n], np.signedinteger) == (n == "p_mode" or n == "p_flip")
               for n in layout.numpy_dtype.names)


def test_pack_params_reports_unrepresentable_values():
    layout = A.build_params_layout((ParamDef("count", "int", 1),))
    with pytest.raises(TglslMetalError) as info:
        pack_params(layout, {"count": 2 ** 40}, 4, 4)
    assert info.value.diagnostic.code == "param_value_invalid"
    with pytest.raises(TglslMetalError):
        pack_params(layout, {"count": "many"}, 4, 4)


# ── 编译消息精简 ────────────────────────────────────────────────────────

_REAL_ERROR_TEXT = (
    "MSL 编译失败（topos_fx_abc）: program_source:10:9: error: no member named 'write' in "
    "'metal::texture2d<float>'\n"
    "    dst.write(x, gid);\n"
    "    ~~~ ^\n"
    "program_source:12:3: warning: unused variable 'c' [-Wunused-variable]\n"
    "    float3 c;\n"
    "        ^\n"
    "program_source:15:1: error: expected ')'\n"
    "    foo(\n"
    "    ^\n"
)


def test_compile_message_keeps_first_error_and_drops_carets_and_warnings():
    assert A.trim_compile_message(_REAL_ERROR_TEXT) == (
        "program_source:10:9: no member named 'write' in 'metal::texture2d<float>' | `dst.write(x, gid);`\n"
        "program_source:15:1: expected ')' | `foo(`")


def test_compile_message_limits_errors_and_falls_back_for_non_compiler_text():
    many = "".join(f"program_source:{i}:1: error: e{i}\n" for i in range(10))
    assert A.trim_compile_message(many).count("\n") + 1 == 4
    assert A.trim_compile_message("MSL 编译成功但找不到 kernel 函数 'x'") == \
        "MSL 编译成功但找不到 kernel 函数 'x'"
    assert A.trim_compile_message("") == ""
    assert len(A.trim_compile_message("x" * 5000)) == 600


def test_key_dataclass_is_frozen_and_complete():
    names = {f.name for f in dataclasses.fields(A.MetalCompileKey)}
    assert {"source_fingerprint", "host_abi", "compile_options", "pixel_format", "device_capability",
            "plugin_version", "msl_fingerprint", "param_schema_hash", "layout_fingerprint",
            "translator_version", "helper_versions", "coord_convention", "sampler_spec"} <= names
    with pytest.raises(dataclasses.FrozenInstanceError):
        _key().effect_id = "x"
