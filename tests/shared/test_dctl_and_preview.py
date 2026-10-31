"""Phase 3 tests: DCTL (.tglsl) wrapper + catalog aggregation + plugin
packer + preview-cost degradation.

These cover the round-3 work that lets the Edit page drop a Color-page
``.tglsl`` plugin onto a clip (running it through EffectPassRunner) and
that downscales expensive effects during playback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.shared.effects.dctl_wrapper import (
    wrap_dctl_process,
    default_uniform_values,
)
from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind
from src.features.shared.effects.effect_catalog import (
    EffectCatalog,
    _build_dctl_plugin_specs,
    _dctl_param_to_pdef,
)
from src.features.composite.nodes.base_node import ParamDef


# ──────────────────────────────────────────────────────────────────────────
# dctl_wrapper.wrap_dctl_process
# ──────────────────────────────────────────────────────────────────────────

FILM_HALATION_SRC = """// --- Topos Plugin Metadata ---
// @name Film Halation
// @param slider blur_radius "Blur Radius" 2.5 0.0 5.0
// @param checkbox enable_tint "Enable Red Tint" 1
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    return color;
}
"""

FILM_HALATION_PARAMS = [
    {"type": "slider", "var_name": "blur_radius", "label": "Blur Radius",
     "default": 2.5, "min": 0.0, "max": 5.0},
    {"type": "checkbox", "var_name": "enable_tint", "label": "Enable Red Tint",
     "default": 1},
]


class TestWrapDctlProcess:
    def test_produces_complete_fragment_shader(self):
        out = wrap_dctl_process(FILM_HALATION_SRC, FILM_HALATION_PARAMS)
        assert "#version 330" in out
        assert "uniform sampler2D u_texture;" in out
        assert "in vec2 v_uv;" in out
        assert "out vec4 fragColor;" in out

    def test_process_function_preserved(self):
        out = wrap_dctl_process(FILM_HALATION_SRC, FILM_HALATION_PARAMS)
        assert "vec3 process(vec3 color, vec2 uv)" in out

    def test_param_uniforms_declared(self):
        """Every @param becomes a uniform with the u_{var_name} name."""
        out = wrap_dctl_process(FILM_HALATION_SRC, FILM_HALATION_PARAMS)
        assert "uniform float u_blur_radius;" in out
        assert "uniform int u_enable_tint;" in out

    def test_main_calls_process_and_writes_fragcolor(self):
        out = wrap_dctl_process(FILM_HALATION_SRC, FILM_HALATION_PARAMS)
        assert "process(src.rgb, v_uv)" in out
        assert "fragColor = vec4(result, src.a);" in out

    def test_metadata_block_stripped(self):
        """The // --- Topos Plugin Metadata --- block must not leak into
        the shader source (it would be dead comments, but also the @param
        lines could confuse a naive parser)."""
        out = wrap_dctl_process(FILM_HALATION_SRC, FILM_HALATION_PARAMS)
        assert "Topos Plugin Metadata" not in out
        assert "@param" not in out

    def test_color_param_becomes_vec3(self):
        src = '''// @param color tint "Tint" 1.0 0.5 0.2
vec3 process(vec3 c, vec2 u){return c;}'''
        params = [{"type": "color", "var_name": "tint", "default": [1.0, 0.5, 0.2]}]
        out = wrap_dctl_process(src, params)
        assert "uniform vec3 u_tint;" in out

    def test_select_param_becomes_int(self):
        src = '''// @param select mode "Mode" 0 "a" "b"
vec3 process(vec3 c, vec2 u){return c;}'''
        params = [{"type": "select", "var_name": "mode", "default": 0,
                   "options": ["a", "b"]}]
        out = wrap_dctl_process(src, params)
        assert "uniform int u_mode;" in out

    def test_separator_param_skipped(self):
        """separator is a UI-only directive with no shader presence."""
        src = '''vec3 process(vec3 c, vec2 u){return c;}'''
        params = [{"type": "separator", "var_name": "sep"}]
        out = wrap_dctl_process(src, params)
        assert "u_sep" not in out


class TestDefaultUniformValues:
    def test_slider_default_float(self):
        out = default_uniform_values(FILM_HALATION_PARAMS)
        assert out["u_blur_radius"] == pytest.approx(2.5)

    def test_checkbox_default_int(self):
        out = default_uniform_values(FILM_HALATION_PARAMS)
        assert out["u_enable_tint"] == 1

    def test_color_default_vec3(self):
        params = [{"type": "color", "var_name": "tint", "default": [0.1, 0.2, 0.3]}]
        out = default_uniform_values(params)
        assert out["u_tint"] == pytest.approx((0.1, 0.2, 0.3))


# ──────────────────────────────────────────────────────────────────────────
# _dctl_param_to_pdef
# ──────────────────────────────────────────────────────────────────────────

class TestDctlParamToPdef:
    def test_slider_to_float_pdef(self):
        pdef = _dctl_param_to_pdef(
            {"type": "slider", "var_name": "r", "default": 2.5,
             "min": 0.0, "max": 5.0})
        assert pdef is not None
        assert pdef.ptype == "float"
        assert pdef.default == pytest.approx(2.5)
        assert pdef.min_val == pytest.approx(0.0)
        assert pdef.max_val == pytest.approx(5.0)
        assert pdef.is_animatable() is True  # float → animatable

    def test_checkbox_to_bool_pdef(self):
        pdef = _dctl_param_to_pdef(
            {"type": "checkbox", "var_name": "en", "default": 1})
        assert pdef.ptype == "bool"
        assert pdef.default is True

    def test_select_to_option_pdef(self):
        pdef = _dctl_param_to_pdef(
            {"type": "select", "var_name": "mode", "default": 1,
             "options": ["a", "b", "c"]})
        assert pdef.ptype == "option"
        assert pdef.labels == ["a", "b", "c"]
        assert pdef.default == "b"  # index 1

    def test_separator_returns_none(self):
        assert _dctl_param_to_pdef({"type": "separator", "var_name": "s"}) is None


# ──────────────────────────────────────────────────────────────────────────
# EffectPassRunner._pack_plugin_params (static, no GL needed)
# ──────────────────────────────────────────────────────────────────────────

class TestPackPluginParams:
    def test_float_passthrough(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:test",
            name="Test", category="plugin",
            param_defs=(ParamDef(name="r", ptype="float", default=1.0,
                                 min_val=0.0, max_val=5.0),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("fragment",),
            dispatch_key="plugin:test",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {"r": 3.5})
        assert packed["u_r"] == pytest.approx(3.5)

    def test_bool_to_int(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="en", ptype="bool", default=False),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {"en": True})
        assert packed["u_en"] == 1

    def test_option_label_to_index(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="mode", ptype="option", default="a",
                                 labels=["a", "b", "c"]),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {"mode": "c"})
        assert packed["u_mode"] == 2

    def test_missing_param_uses_default(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="r", ptype="float", default=2.0,
                                 min_val=0.0, max_val=5.0),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {})
        assert packed["u_r"] == pytest.approx(2.0)


# ──────────────────────────────────────────────────────────────────────────
# Catalog DCTL aggregation (uses real plugins/ dir)
# ──────────────────────────────────────────────────────────────────────────

class TestCatalogDctlAggregation:
    def test_build_dctl_plugin_specs_returns_list(self):
        """_build_dctl_plugin_specs returns a list (possibly empty if no
        plugins dir is reachable in this env, but never raises)."""
        specs = _build_dctl_plugin_specs()
        assert isinstance(specs, list)

    def test_dctl_spec_has_correct_shape_when_present(self):
        """If any DCTL plugins were discovered, each spec follows the
        plugin:* convention. (Skipped when the plugins dir isn't found.)"""
        specs = _build_dctl_plugin_specs()
        if not specs:
            pytest.skip("no .tglsl plugins discovered in this environment")
        for spec in specs:
            assert spec.effect_type.startswith("plugin:")
            assert spec.dispatch_key == spec.effect_type
            assert spec.shader_kind == ShaderKind.DCTL_PROCESS
            assert len(spec.shader_sources) == 1
            # DCTL 插件分类从技术术语 "dctl" 改为面向用户的 "plugin"。
            assert spec.category == "plugin"

    def test_catalog_get_all_specs_includes_dctl(self):
        """The catalog's get_all_specs aggregates DCTL specs alongside the
        built-in composite filters."""
        cat = EffectCatalog()
        cat.refresh()  # ensure clean rebuild
        all_specs = cat.get_all_specs()
        effect_types = {s.effect_type for s in all_specs}
        # Built-ins are always present.
        assert "blur" in effect_types
        assert "sharpen" in effect_types
        # Any DCTL plugins appear as plugin:* entries.
        dctl_types = [t for t in effect_types if t.startswith("plugin:")]
        # We can't assert specific plugin ids (env-dependent), just that
        # the aggregation ran without crashing.


# ──────────────────────────────────────────────────────────────────────────
# Preview-cost degradation
# ──────────────────────────────────────────────────────────────────────────

class TestPreviewDegradation:
    def test_high_cost_blur_radius_capped(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = EffectSpec(
            effect_type="blur", name="Blur", category="filter",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="blur",
            preview_cost="high",
        )
        out = _degrade_params_for_preview(spec, {"radius": 50.0, "mix": 1.0})
        assert out["radius"] == 8.0  # capped
        assert out["mix"] == 1.0     # untouched (no cap defined)

    def test_low_cost_untouched(self):
        # FX-M04 起降级改为按 effect_type 的声明式注册表（DegradationDecl），
        # spec.preview_cost 不再参与 _degrade_params_for_preview 决策——
        # low 与 high 走同一条 cap 路径（与 test_high_cost_capped 一致）。
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = EffectSpec(
            effect_type="blur", name="Blur", category="filter",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="blur",
            preview_cost="low",
        )
        out = _degrade_params_for_preview(spec, {"radius": 50.0})
        assert out["radius"] == 8.0  # capped（声明式 cap，与 preview_cost 无关）

    def test_below_cap_unchanged(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = EffectSpec(
            effect_type="blur", name="Blur", category="filter",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="blur",
            preview_cost="high",
        )
        out = _degrade_params_for_preview(spec, {"radius": 3.0})
        assert out["radius"] == 3.0  # below cap, unchanged

    def test_unknown_effect_untouched(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = EffectSpec(
            effect_type="plugin:mystery", name="Mystery", category="plugin",
            param_defs=(), shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:mystery",
            preview_cost="high",
        )
        out = _degrade_params_for_preview(spec, {"x": 99.0})
        assert out["x"] == 99.0  # no caps registered for this effect_type

    def test_does_not_mutate_input(self):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _degrade_params_for_preview,
        )
        spec = EffectSpec(
            effect_type="blur", name="Blur", category="filter",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="blur",
            preview_cost="high",
        )
        original = {"radius": 50.0}
        _degrade_params_for_preview(spec, original)
        assert original["radius"] == 50.0  # input dict untouched


# ──────────────────────────────────────────────────────────────────────────
# Review fixes: color packer, structural key, catalog dedup
# ──────────────────────────────────────────────────────────────────────────

class TestPackPluginParamsColor:
    """The color @param must pack to a vec3 tuple, not a scalar (the
    wrapper declares ``uniform vec3`` — a scalar would be silently
    dropped by moderngl / _set_uniform, killing the param)."""

    def test_color_default_packs_to_vec3(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="tint", ptype="color",
                                 default=(0.1, 0.2, 0.3)),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {})
        assert packed["u_tint"] == pytest.approx((0.1, 0.2, 0.3))

    def test_color_supplied_value_packs_to_vec3(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="tint", ptype="color",
                                 default=(1.0, 1.0, 1.0)),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(
            spec, {"tint": (0.5, 0.6, 0.7)})
        assert packed["u_tint"] == pytest.approx((0.5, 0.6, 0.7))

    def test_color_scalar_input_padded_to_vec3(self):
        """A scalar value (legacy / malformed) is padded to vec3 rather
        than crashing the packer."""
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="tint", ptype="color",
                                 default=(1.0, 1.0, 1.0)),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
        )
        packed = EffectPassRunner._pack_plugin_params(spec, {"tint": 0.5})
        assert packed["u_tint"] == pytest.approx((0.5, 0.5, 0.5))


class TestStructuralKeyFromPacked:
    """_structural_key_from_packed drives the program-cache key for specs
    whose select params switch compile-time branches."""

    def test_no_structural_params_returns_empty(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="blur", name="Blur", category="filter",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="blur",
            structural_param_names=(),
        )
        assert EffectPassRunner._structural_key_from_packed(spec, {}) == ()

    def test_select_value_in_key(self):
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="mode", ptype="option",
                                 default="a", labels=["a", "b", "c"]),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
            structural_param_names=("mode",),
        )
        # Packed form uses u_<var> naming with int index.
        key = EffectPassRunner._structural_key_from_packed(
            spec, {"u_mode": 2})
        assert key == (2,)

    def test_different_select_values_yield_different_keys(self):
        """Changing the select option must change the cache key, so the
        runner recompiles (the whole point of structural_param_names)."""
        from src.features.shared.effects.effect_pass_runner import EffectPassRunner
        spec = EffectSpec(
            effect_type="plugin:t", name="T", category="plugin",
            param_defs=(ParamDef(name="mode", ptype="option",
                                 default="a", labels=["a", "b"]),),
            shader_kind=ShaderKind.DCTL_PROCESS,
            shader_sources=("f",), dispatch_key="plugin:t",
            structural_param_names=("mode",),
        )
        k0 = EffectPassRunner._structural_key_from_packed(spec, {"u_mode": 0})
        k1 = EffectPassRunner._structural_key_from_packed(spec, {"u_mode": 1})
        assert k0 != k1


class TestCatalogRegisterDedup:
    """Re-registering an existing effect_type must not duplicate it in
    the category index (would make get_specs_by_category return twice)."""

    def test_register_same_id_twice_no_duplicate(self):
        cat = EffectCatalog()
        cat.refresh()
        spec1 = EffectSpec(
            effect_type="custom:x", name="X", category="custom",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="custom:x",
        )
        cat.register(spec1)
        cat.register(spec1)  # re-register
        by_cat = cat.get_specs_by_category("custom")
        # Exactly one entry, not two.
        assert len(by_cat) == 1
        assert by_cat[0].effect_type == "custom:x"

    def test_register_replacement_updates_spec(self):
        cat = EffectCatalog()
        cat.refresh()
        v1 = EffectSpec(
            effect_type="custom:y", name="Y1", category="custom",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="custom:y",
        )
        v2 = EffectSpec(
            effect_type="custom:y", name="Y2", category="custom",
            param_defs=(), shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("f",), dispatch_key="custom:y",
        )
        cat.register(v1)
        cat.register(v2)
        assert cat.get_spec("custom:y").name == "Y2"
        assert len(cat.get_specs_by_category("custom")) == 1


class TestDctlColorParamDef:
    """catalog _dctl_param_to_pdef keeps color as vec3 (ptype="color"),
    not downgraded to float — otherwise the packer can't tell it needs
    a 3-tuple."""

    def test_color_becomes_color_ptype(self):
        pdef = _dctl_param_to_pdef(
            {"type": "color", "var_name": "tint", "default": [0.1, 0.2, 0.3]})
        assert pdef.ptype == "color"
        assert pdef.default == pytest.approx((0.1, 0.2, 0.3))

    def test_color_animatable(self):
        """color 已纳入关键帧动画契约（ParamDef.is_animatable 的原子值
        语义：color 作为原子三元组整体插值），is_animatable 为 True。"""
        pdef = _dctl_param_to_pdef(
            {"type": "color", "var_name": "tint", "default": [1.0, 1.0, 1.0]})
        assert pdef.is_animatable() is True

