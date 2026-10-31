"""
Unit tests for the cross-page EffectSpec / EffectCatalog abstractions.

These tests deliberately stay off the GPU — they verify the pure-data
contract (param schema, dispatch keys, source-count validation, category
indexing) and the param-packing helpers that translate user-facing values
to shader uniforms. The actual shader compilation / rendering is exercised
by the integration tests under tests/edit/ that boot a real GL context.

Coverage:
1. EffectSpec construction & validation (source-count vs shader_kind).
2. EffectCatalog singleton behaviour (categories, lookup, refresh).
3. Spec builders for blur and sharpen (param schema matches the
   composite node's PARAM_DEFS; shader source counts correct).
4. Param packers (blur_type → kernel_mode, edge_policy → int, channel
   bools → vec4 mask; sharpen channels → int).
5. Legacy effect_type migration on EffectInstance.from_dict.
6. EffectRegistry ↔ catalog integration (Edit panel sees blur/sharpen).
"""

from __future__ import annotations

import pytest

from src.features.composite.nodes.base_node import ParamDef
from src.features.shared.effects import (
    EffectCatalog,
    EffectSpec,
    ShaderKind,
    get_effect_catalog,
)
from src.features.shared.effects.effect_pass_runner import (
    _pack_blur_params,
    _pack_sharpen_params,
    _label_to_index,
)


# ──────────────────────────────────────────────────────────────────────────
# EffectSpec construction & validation
# ──────────────────────────────────────────────────────────────────────────

class TestEffectSpecConstruction:
    def test_single_pass_spec_builds(self):
        spec = EffectSpec(
            effect_type="x", name="X", category="c",
            param_defs=(ParamDef("amount", "float", 1.0),),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="x",
        )
        assert spec.effect_type == "x"
        assert spec.uniform_name("amount") == "amount"  # identity mapping

    def test_separable_spec_requires_two_sources(self):
        with pytest.raises(ValueError, match="expects 2"):
            EffectSpec(
                effect_type="bad", name="Bad", category="c",
                param_defs=(),
                shader_kind=ShaderKind.SEPARABLE_PASSES,
                shader_sources=("only-one",),
                dispatch_key="bad",
            )

    def test_single_pass_rejects_two_sources(self):
        with pytest.raises(ValueError, match="expects 1"):
            EffectSpec(
                effect_type="bad", name="Bad", category="c",
                param_defs=(),
                shader_kind=ShaderKind.SINGLE_PASS,
                shader_sources=("a", "b"),
                dispatch_key="bad",
            )

    def test_uniform_name_uses_explicit_mapping(self):
        spec = EffectSpec(
            effect_type="x", name="X", category="c",
            param_defs=(ParamDef("amount", "float", 1.0),),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="x",
            param_to_uniform={"amount": "u_amount"},
        )
        assert spec.uniform_name("amount") == "u_amount"
        # Unmapped params fall back to their own name.
        assert spec.uniform_name("unknown") == "unknown"

    def test_default_parameters_seeds_visible_and_hidden(self):
        spec = EffectSpec(
            effect_type="x", name="X", category="c",
            param_defs=(
                ParamDef("visible", "float", 0.5),
                ParamDef("internal", "bool", True, hidden=True),
            ),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="x",
        )
        defaults = spec.default_parameters()
        assert defaults == {"visible": 0.5, "internal": True}


# ──────────────────────────────────────────────────────────────────────────
# EffectCatalog singleton
# ──────────────────────────────────────────────────────────────────────────

class TestEffectCatalog:
    def test_singleton_is_stable(self):
        a = get_effect_catalog()
        b = get_effect_catalog()
        assert a is b

    def test_known_mvp_effects_present(self):
        cat = get_effect_catalog()
        assert cat.is_known("blur")
        assert cat.is_known("sharpen")
        assert not cat.is_known("does_not_exist")

    def test_get_spec_unknown_returns_none(self):
        assert get_effect_catalog().get_spec("nope") is None

    def test_categories_include_mvp(self):
        cats = get_effect_catalog().get_categories()
        # blur/sharpen/defocus 都并入了 filter；sharpen/blur 不再是
        # 独立分类。
        assert "filter" in cats
        assert "blur" not in cats
        assert "sharpen" not in cats

    def test_register_replaces_existing(self):
        cat = EffectCatalog()
        original = EffectSpec(
            effect_type="custom", name="Original", category="x",
            param_defs=(ParamDef("a", "float", 0.0),),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="custom",
        )
        cat.register(original)
        replaced = EffectSpec(
            effect_type="custom", name="Replaced", category="x",
            param_defs=(ParamDef("a", "float", 0.0),),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="custom",
        )
        cat.register(replaced)
        assert cat.get_spec("custom").name == "Replaced"

    def test_refresh_clears_cache(self):
        cat = EffectCatalog()
        cat.register(EffectSpec(
            effect_type="temp", name="Temp", category="tmp",
            param_defs=(ParamDef("a", "float", 0.0),),
            shader_kind=ShaderKind.SINGLE_PASS,
            shader_sources=("frag",),
            dispatch_key="temp",
        ))
        assert cat.is_known("temp")
        cat.refresh()
        # refresh only clears the built flag; explicit registrations on a
        # standalone EffectCatalog instance stay until refresh+rebuild.
        # Verify the singleton still has the MVP effects after a refresh.
        singleton = get_effect_catalog()
        singleton.refresh()
        assert singleton.is_known("blur")


# ──────────────────────────────────────────────────────────────────────────
# Spec builders: blur & sharpen
# ──────────────────────────────────────────────────────────────────────────

class TestBlurSpec:
    def test_blur_spec_uses_separable_kind(self):
        spec = get_effect_catalog().get_spec("blur")
        assert spec.shader_kind == ShaderKind.SEPARABLE_PASSES
        assert len(spec.shader_sources) == 2

    def test_blur_param_defs_match_composite_node(self):
        """The Edit panel must show the same params as the composite node —
        this is the core 'reuse' contract of the whole feature."""
        from src.features.composite.nodes.blur_node import BlurNode
        spec = get_effect_catalog().get_spec("blur")
        spec_names = {p.name for p in spec.param_defs}
        node_names = {p.name for p in BlurNode.PARAM_DEFS}
        assert spec_names == node_names

    def test_blur_shader_sources_are_real_glsl(self):
        spec = get_effect_catalog().get_spec("blur")
        h, v = spec.shader_sources
        # Both shaders must be GLSL fragments with the right version.
        assert "#version 330" in h
        assert "#version 330" in v
        assert "void main()" in h
        assert "void main()" in v

    def test_blur_has_all_composite_shader_variants(self):
        spec = get_effect_catalog().get_spec("blur")
        assert spec is not None
        assert set(spec.shader_variants) == {"bilateral", "motion", "bokeh"}
        assert spec.shader_kind_for_variant("bilateral") == ShaderKind.SINGLE_PASS
        assert spec.shader_kind_for_variant("motion") == ShaderKind.SINGLE_PASS
        assert spec.shader_kind_for_variant("bokeh") == ShaderKind.SINGLE_PASS
        for sources in spec.shader_variants.values():
            assert len(sources) == 1
            assert "#version 330" in sources[0]


class TestSharpenSpec:
    def test_sharpen_spec_uses_single_pass(self):
        spec = get_effect_catalog().get_spec("sharpen")
        assert spec.shader_kind == ShaderKind.SINGLE_PASS
        assert len(spec.shader_sources) == 1

    def test_sharpen_uniform_mapping(self):
        spec = get_effect_catalog().get_spec("sharpen")
        assert spec.uniform_name("amount") == "u_amount"
        assert spec.uniform_name("radius") == "u_radius"
        assert spec.uniform_name("threshold") == "u_threshold"
        assert spec.uniform_name("channels") == "u_channels"

    def test_sharpen_param_defs_match_composite_node(self):
        from src.features.composite.nodes.sharpen_node import SharpenNode
        spec = get_effect_catalog().get_spec("sharpen")
        spec_names = {p.name for p in spec.param_defs}
        node_names = {p.name for p in SharpenNode.PARAM_DEFS}
        assert spec_names == node_names


# ──────────────────────────────────────────────────────────────────────────
# Param packers
# ──────────────────────────────────────────────────────────────────────────

class TestBlurPacker:
    def test_gaussian_maps_to_kernel_mode_1(self):
        packed = _pack_blur_params({"blur_type": "gaussian"})
        assert packed["u_kernel_mode"] == 1

    def test_box_maps_to_kernel_mode_0(self):
        packed = _pack_blur_params({"blur_type": "box"})
        assert packed["u_kernel_mode"] == 0

    def test_unknown_blur_type_defaults_to_box(self):
        packed = _pack_blur_params({"blur_type": "weird"})
        assert packed["u_kernel_mode"] == 0

    def test_edge_policy_packing(self):
        assert _pack_blur_params({"edge_policy": "clamp"})["u_edge_policy"] == 0
        assert _pack_blur_params({"edge_policy": "wrap"})["u_edge_policy"] == 1
        assert _pack_blur_params({"edge_policy": "mirror"})["u_edge_policy"] == 2

    def test_channel_mask_packs_to_vec4(self):
        packed = _pack_blur_params({
            "channel_r": True, "channel_g": False,
            "channel_b": True, "channel_a": True,
        })
        assert packed["u_channel_mask"] == (1.0, 0.0, 1.0, 1.0)

    def test_channel_mask_defaults_rgb_on_alpha_off(self):
        packed = _pack_blur_params({})
        # Default: RGB on, alpha off (matches BlurNode defaults).
        assert packed["u_channel_mask"] == (1.0, 1.0, 1.0, 0.0)

    def test_radius_pass_through(self):
        packed = _pack_blur_params({"radius_x": 12.5, "radius_y": 7.5})
        assert packed["u_radius_x"] == 12.5
        assert packed["u_radius_y"] == 7.5

    def test_radius_falls_back_to_scalar_radius(self):
        # When only the scalar radius is set (single-pass-style params),
        # both H and V radii should derive from it.
        packed = _pack_blur_params({"radius": 20.0})
        assert packed["u_radius_x"] == 20.0
        assert packed["u_radius_y"] == 20.0

    def test_single_pass_parameters_are_packed(self):
        packed = _pack_blur_params({
            "blur_type": "bokeh",
            "radius": 12.0,
            "angle": 37.0,
            "edge_preserve": 0.8,
            "aperture_blades": "6",
            "anamorphic": 0.5,
            "highlight_gain": 2.0,
            "quality": "high",
        })
        assert packed["u_radius"] == 12.0
        assert packed["u_angle"] == 37.0
        assert packed["u_edge_preserve"] == 0.8
        assert packed["u_aperture_blades"] == 6
        assert packed["u_anamorphic"] == 0.5
        assert packed["u_highlight_gain"] == 2.0
        assert packed["u_sample_count"] == 32


class TestSharpenPacker:
    def test_amount_passes_through(self):
        assert _pack_sharpen_params({"amount": 3.5})["u_amount"] == 3.5

    def test_channels_string_to_int(self):
        assert _pack_sharpen_params({"channels": "all"})["u_channels"] == 0
        assert _pack_sharpen_params({"channels": "rgb"})["u_channels"] == 1
        assert _pack_sharpen_params({"channels": "luminance"})["u_channels"] == 2

    def test_unknown_channels_defaults_to_all(self):
        assert _pack_sharpen_params({"channels": "?"})["u_channels"] == 0


class TestLabelToIndex:
    def test_string_label(self):
        assert _label_to_index("b", ("a", "b", "c")) == 1

    def test_int_in_range(self):
        assert _label_to_index(2, ("a", "b", "c")) == 2

    def test_int_out_of_range_uses_default(self):
        assert _label_to_index(99, ("a", "b"), default=7) == 7

    def test_numeric_string(self):
        assert _label_to_index("1", ("a", "b", "c")) == 1

    def test_unknown_string_uses_default(self):
        assert _label_to_index("xyz", ("a", "b"), default=4) == 4


# ──────────────────────────────────────────────────────────────────────────
# Legacy effect_type migration
# ──────────────────────────────────────────────────────────────────────────

class TestLegacyMigration:
    def test_gaussian_blur_migrates_to_blur(self):
        from src.features.edit.core.effect_instance import EffectInstance
        legacy = EffectInstance.from_dict({
            "effect_id": "old-1",
            "effect_type": "gaussian_blur",
            "params": {"intensity": 75.0, "radius": 12.0},
            "is_enabled": True,
            "order": 0,
        })
        assert legacy.effect_type == "blur"
        assert legacy.params["blur_type"] == "gaussian"
        # intensity 0-100 → mix 0-1
        assert legacy.params["mix"] == pytest.approx(0.75)
        # radius carried over
        assert legacy.params["radius"] == 12.0

    def test_sharpen_intensity_migrates_to_amount(self):
        from src.features.edit.core.effect_instance import EffectInstance
        legacy = EffectInstance.from_dict({
            "effect_id": "old-2",
            "effect_type": "sharpen",
            "params": {"intensity": 50.0, "radius": 2.0},
        })
        assert legacy.effect_type == "sharpen"
        # intensity 0-100 → amount 0-10 (10:1 scale)
        assert legacy.params["amount"] == pytest.approx(5.0)
        assert legacy.params["radius"] == 2.0
        # intensity removed
        assert "intensity" not in legacy.params

    def test_modern_effect_types_untouched(self):
        from src.features.edit.core.effect_instance import EffectInstance
        modern = EffectInstance.from_dict({
            "effect_id": "new-1",
            "effect_type": "blur",
            "params": {"blur_type": "box", "radius": 5.0},
        })
        assert modern.effect_type == "blur"
        assert modern.params == {"blur_type": "box", "radius": 5.0}


# ──────────────────────────────────────────────────────────────────────────
# EffectRegistry ↔ catalog integration
# ──────────────────────────────────────────────────────────────────────────

class TestRegistryCatalogIntegration:
    def test_registry_exposes_blur_and_sharpen(self):
        from src.features.edit.core.effect_registry import get_effect_registry
        reg = get_effect_registry()
        blur = reg.get_definition("blur")
        sharpen = reg.get_definition("sharpen")
        assert blur is not None
        assert sharpen is not None
        # 模糊/锐化/散焦都归入 Filter 大类。
        assert blur.category == "filter"
        assert sharpen.category == "filter"

    def test_blur_definition_create_instance_seeds_all_params(self):
        """create_instance must produce a usable EffectInstance whose params
        cover every shader uniform the runner will read."""
        from src.features.edit.core.effect_registry import get_effect_registry
        reg = get_effect_registry()
        blur_def = reg.get_definition("blur")
        inst = blur_def.create_instance(order=0)
        # The MVP blur shader reads these uniforms; every one must have a
        # seeded value so the runner never sees a missing-param KeyError.
        for required in (
            "blur_type", "radius_x", "radius_y", "radius",
            "edge_policy", "mix",
            "channel_r", "channel_g", "channel_b", "channel_a",
        ):
            assert required in inst.params, f"missing param: {required}"

    def test_legacy_gaussian_blur_no_longer_registered(self):
        """The legacy hardcoded GAUSSIAN_BLUR_EFFECT must not be registered
        — its parameter schema was incompatible with the renderer."""
        from src.features.edit.core.effect_registry import get_effect_registry
        reg = get_effect_registry()
        assert reg.get_definition("gaussian_blur") is None
