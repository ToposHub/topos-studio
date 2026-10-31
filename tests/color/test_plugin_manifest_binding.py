"""P7-01：EffectSpec 权威 manifest 单元测试（ADR 0004 决策 1/5）。

覆盖三件事：
1. GLSL 类型投影表与 DCTL ABI（dctl_wrapper._PARAM_GLTYPE）不漂移；
2. PluginBinding.uniform_manifest（参数名 + 类型 + uniform 映射 +
   structural 标记）的派生规则与显式失败语义（G5）；
3. 编译入口 build_composition_shader 只接受 PluginBinding，缓存键
   「值参数不重编译 / structural 与 enabled 重编译」行为与重构前一致
   （位级输出一致性由 test_shader_golden_corpus 的 G1 门禁保证）。
"""

from __future__ import annotations

import os

import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.shader_compiler import (  # noqa: E402
    ShaderPipeline,
    plugin_binding_from_instance,
    plugin_bindings_from_instances,
)
from src.features.shared.effects.effect_spec import (  # noqa: E402
    GLSL_UNIFORM_TYPES,
    EffectSpec,
    PluginBinding,
    ShaderKind,
)
from src.features.shared.effects.dctl_wrapper import _PARAM_GLTYPE  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLUGIN_DIR = os.path.join(_REPO, "plugins")


def _scan_plugins():
    from src.features.color.core.plugin_manager import get_plugin_manager
    get_plugin_manager().scan_plugins(_PLUGIN_DIR)


def _highpass(index: int = 0, **overrides) -> dict:
    inst = {
        "plugin_id": "HighPass_Retouch", "enabled": True,
        "params": {"smooth_radius": 0.5, "detail_preserve": 0.5,
                   "mix_amount": 0.85},
    }
    inst.update(overrides)
    return inst


# ── 1. 类型投影表与 DCTL ABI 不漂移 ─────────────────────────────────────

# catalog 转换（_dctl_param_to_pdef）：@param type → ParamDef.ptype
_CATALOG_PTYPE = {"slider": "float", "checkbox": "bool",
                  "color": "color", "select": "option"}


@pytest.mark.parametrize("abi_type", ["slider", "checkbox", "color", "select"])
def test_glsl_type_table_matches_dctl_abi(abi_type):
    """GLSL_UNIFORM_TYPES 经 catalog ptype 折算后必须与 ABI GLSL 类型一致。"""
    assert GLSL_UNIFORM_TYPES[_CATALOG_PTYPE[abi_type]] == _PARAM_GLTYPE[abi_type]


# ── 2. manifest 派生规则 ────────────────────────────────────────────────


def _manual_spec(param_defs, structural=()) -> EffectSpec:
    return EffectSpec(
        effect_type="plugin:manual", name="manual", category="plugin",
        param_defs=tuple(param_defs), shader_kind=ShaderKind.DCTL_PROCESS,
        shader_sources=("vec3 process(vec3 c, vec2 uv) { return c; }",),
        dispatch_key="plugin:manual",
        structural_param_names=tuple(structural),
    )


def test_uniform_manifest_prefix_order_structural():
    from src.features.composite.nodes.base_node import ParamDef
    spec = _manual_spec([
        ParamDef(name="amount", ptype="float"),
        ParamDef(name="mode", ptype="option", labels=("a", "b")),
        ParamDef(name="tint", ptype="color"),
        ParamDef(name="flip", ptype="bool"),
    ], structural=("mode",))
    binding = PluginBinding(spec=spec, values={"amount": 0.5})

    manifest = binding.uniform_manifest(prefix="u_P0_")

    assert [(ub.uniform_name, ub.glsl_type) for ub in manifest] == [
        ("u_P0_amount", "float"),
        ("u_P0_mode", "int"),     # select ABI
        ("u_P0_tint", "vec3"),    # color ABI
        ("u_P0_flip", "int"),     # checkbox ABI
    ]
    assert [ub.param_name for ub in manifest] == ["amount", "mode", "tint", "flip"]
    assert [ub.structural for ub in manifest] == [False, True, False, False]


def test_uniform_manifest_unknown_ptype_raises():
    """G5：类型未知 → 显式 ValueError，禁止静默丢参数。"""
    from src.features.composite.nodes.base_node import ParamDef
    spec = _manual_spec([
        ParamDef(name="path", ptype="file"),
    ])
    with pytest.raises(ValueError, match="ptype='file'"):
        PluginBinding(spec=spec).uniform_manifest()


def test_uniform_manifest_empty_params():
    manifest = PluginBinding(spec=_manual_spec([])).uniform_manifest("u_P0_")
    assert manifest == ()


# ── 3. 真实插件解析（plugins/ 目录）─────────────────────────────────────


def test_from_instance_real_plugin():
    _scan_plugins()
    binding = plugin_binding_from_instance(_highpass())

    assert binding is not None
    assert binding.spec.plugin_id == "plugin:HighPass_Retouch"
    assert binding.enabled is True
    # separator（无 var_name）被剔除，slider 全部映射为 float ParamDef
    assert [p.name for p in binding.spec.param_defs] == [
        "smooth_radius", "detail_preserve", "mix_amount"]
    assert all(p.ptype == "float" for p in binding.spec.param_defs)
    assert binding.spec.structural_param_names == ()
    # values 是拷贝：实例 dict 后续变化不影响 binding
    assert binding.values == {"smooth_radius": 0.5, "detail_preserve": 0.5,
                              "mix_amount": 0.85}


def test_from_instance_values_are_copied():
    _scan_plugins()
    inst = _highpass()
    binding = plugin_binding_from_instance(inst)
    inst["params"]["mix_amount"] = 0.0
    assert binding.values["mix_amount"] == 0.85


def test_from_instance_select_structural():
    """select 参数是 structural（缓存键成员），slider/checkbox 不是。"""
    _scan_plugins()
    binding = plugin_binding_from_instance({
        "plugin_id": "Color_Space_Transform", "enabled": True,
        "params": {"input_space": 2, "exposure": 0.5},
    })
    assert binding is not None
    assert binding.spec.structural_param_names == (
        "input_space", "input_gamma", "output_space", "output_gamma",
        "tone_mapping")
    by_name = {ub.param_name: ub for ub in binding.uniform_manifest("u_P0_")}
    assert by_name["input_space"].structural is True
    assert by_name["exposure"].structural is False
    assert by_name["enable_adaptation"].glsl_type == "int"  # checkbox ABI


def test_from_instance_failures():
    _scan_plugins()
    with pytest.raises(TypeError):
        plugin_binding_from_instance("not-a-dict")
    assert plugin_binding_from_instance({"enabled": True}) is None  # 无 id
    assert plugin_binding_from_instance(
        {"plugin_id": "no_such_plugin"}) is None               # 未找到
    # 非 dctl（AI 插件）不进入 fragment 编译入口
    assert plugin_binding_from_instance(
        {"plugin_id": "whatever", "type": "tai"}) is None
    # enabled 透传（False 进 binding，由编译器跳过）
    b = plugin_binding_from_instance(_highpass(enabled=False))
    assert b is not None and b.enabled is False


def test_from_instances_preserves_positions():
    """加载失败的插件仍占位：后续插件的 index 不前移。"""
    _scan_plugins()
    bindings = plugin_bindings_from_instances([
        _highpass(),                                # index 0
        {"plugin_id": "no_such_plugin"},            # 解析失败，占位 1
        _highpass(),                                # index 2
    ])
    assert [b.index for b in bindings] == [0, 2]


# ── 4. 编译入口集成（真实 GL context）───────────────────────────────────


@pytest.fixture(scope="module")
def gl():
    ctx = moderngl.create_standalone_context(require=330)
    yield ctx
    ctx.release()


@pytest.fixture(scope="module")
def pipeline(gl):
    _scan_plugins()
    p = ShaderPipeline(gl)
    yield p
    p.release()


def test_composition_compiles_with_bindings(gl, pipeline):
    bindings = plugin_bindings_from_instances([
        _highpass(),
        {"plugin_id": "bilateral_skin_smooth", "enabled": True,
         "params": {"strength": 0.7, "radius": 0.4, "threshold": 0.2,
                    "recover": 0.5}},
    ])
    prog = pipeline.build_composition_shader(plugins=bindings)
    assert prog is not None
    # 双插件命名空间：P0/P1 各自的 uniform 均存在（参数被 process 使用，
    # 不会被 GLSL 编译器剔除）
    for name in ("u_P0_mix_amount", "u_P1_strength"):
        assert name in prog


def test_cache_value_params_do_not_recompile(gl, pipeline):
    b1 = plugin_bindings_from_instances([_highpass()])
    b2 = plugin_bindings_from_instances(
        [_highpass(params={"smooth_radius": 0.9, "detail_preserve": 0.5,
                           "mix_amount": 0.1})])
    prog1 = pipeline.build_composition_shader(plugins=b1)
    prog2 = pipeline.build_composition_shader(plugins=b2)
    assert prog1 is prog2  # slider 是值参数：拖动不触发重编译


def test_cache_structural_and_enabled_recompile(gl, pipeline):
    cst = lambda **kw: {"plugin_id": "Color_Space_Transform", "enabled": True,
                        "params": {"input_space": 0, "input_gamma": 0,
                                   "output_space": 0, "output_gamma": 1,
                                   "tone_mapping": 0, "exposure": 0.0,
                                   "contrast": 1.0, "saturation": 1.0}, **kw}
    base = pipeline.build_composition_shader(
        plugins=plugin_bindings_from_instances([cst()]))
    # select 值变化 → structural → 重编译
    sel = pipeline.build_composition_shader(
        plugins=plugin_bindings_from_instances([cst(params={
            "input_space": 2, "input_gamma": 0, "output_space": 0,
            "output_gamma": 1, "tone_mapping": 0, "exposure": 0.0,
            "contrast": 1.0, "saturation": 1.0})]))
    assert sel is not base
    # enabled 翻转 → 重编译（并从声明/调用中剔除）
    off = pipeline.build_composition_shader(
        plugins=plugin_bindings_from_instances([cst(enabled=False)]))
    assert off is not base
    assert "u_P0_input_space" not in off


def test_entry_rejects_raw_dict(gl, pipeline):
    """G5：原始 instance dict 进入编译入口 → 显式 TypeError。"""
    with pytest.raises(TypeError, match="PluginBinding"):
        pipeline.build_composition_shader(plugins=[_highpass()])
