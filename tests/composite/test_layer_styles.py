"""B08: editable layer styles — golden math, versioned data, stack policy.

The drop-shadow golden comes verbatim from the implementation plan; the
stack policy keeps unverified styles merged with diagnostics, and the CPU
reference path never double-applies the source layer opacity.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest

from src.features.composite.core.layer_styles import (
    EDITABLE_STYLE_KINDS, LayerStyleEffect, STYLE_KIND_COLOR_OVERLAY,
    STYLE_KIND_DROP_SHADOW, STYLE_KIND_INNER_SHADOW, STYLE_KIND_OUTER_GLOW,
    STYLE_KIND_STROKE, StyleStackDiagnostic, apply_style_stack_rgba,
    color_over, plan_style_stack, shadow_under, style_halo_extent,
)

pytestmark = pytest.mark.composite_core


def test_shadow_under_golden_from_plan():
    # Verbatim B08 example.
    assert shadow_under((0.5, 0.0, 0.0, 0.5), (0.0, 0.5, 0.0), 0.5) == (
        0.5, 0.125, 0.0, 0.75)


def test_shadow_under_semitransparent_edges_and_hdr():
    # Transparent foreground: the shadow shows through unfiltered (HDR
    # values pass through, no clamp).
    assert shadow_under((0.0, 0.0, 0.0, 0.0), (0.0, 8.0, 0.0), 0.25) == (
        0.0, 2.0, 0.0, 0.25)
    # Opaque foreground: the shadow is fully hidden.
    assert shadow_under((0.5, 0.5, 0.5, 1.0), (0.0, 0.0, 0.0), 1.0) == (
        0.5, 0.5, 0.5, 1.0)


def test_color_over_applies_once():
    result = color_over((0.2, 0.2, 0.2, 0.5), (1.0, 0.0, 0.0), 0.5)
    assert result == pytest.approx((0.35, 0.1, 0.1, 0.5))


def test_stack_policy_keeps_unverified_styles_merged():
    styles = (
        LayerStyleEffect(STYLE_KIND_DROP_SHADOW, 2, {"radius": 4.0}),
        LayerStyleEffect(STYLE_KIND_STROKE, 1, {}),
        LayerStyleEffect(STYLE_KIND_INNER_SHADOW, 3, {}),
        LayerStyleEffect("bevelEmboss", 0, {}),
    )
    editable, diagnostics = plan_style_stack(styles)
    assert [s.kind for s in editable] == [STYLE_KIND_DROP_SHADOW]
    assert {(d.kind, d.reason) for d in diagnostics} == {
        (STYLE_KIND_STROKE, "unverified_kind"),
        (STYLE_KIND_INNER_SHADOW, "unverified_kind"),
        ("bevelEmboss", "unknown_kind"),
    }


def test_style_effect_round_trips_with_provenance():
    style = LayerStyleEffect(
        STYLE_KIND_OUTER_GLOW, 4,
        {"color": (0.1, 0.2, 0.3), "radius": 6.0, "opacity": 0.8},
        source_description="outerGlow: {blendMode: screen}",
        conversion_version=1)
    restored = LayerStyleEffect.from_dict(style.to_dict())
    assert restored == style
    assert restored.fingerprint() == style.fingerprint()
    assert LayerStyleEffect.from_dict("nope") is None


def test_unknown_style_retains_provenance_and_requires_a_merged_fallback():
    original = LayerStyleEffect("bogus", 0, source_description="original PSD effect")
    restored = LayerStyleEffect.from_dict(original.to_dict())
    assert restored == original
    editable, diagnostics = plan_style_stack((restored,))
    assert not editable
    assert diagnostics == (StyleStackDiagnostic("bogus", "unknown_kind"),)
    with pytest.raises(ValueError, match="unknown_kind"):
        apply_style_stack_rgba(np.zeros((1, 1, 4)), (restored,))


def test_invalid_or_future_style_parameters_do_not_render_by_approximation():
    invalid = LayerStyleEffect(STYLE_KIND_COLOR_OVERLAY, 0, {"color": (1.0,)})
    future = LayerStyleEffect(STYLE_KIND_DROP_SHADOW, 1, conversion_version=999)
    assert plan_style_stack((invalid, future)) == ((), (
        StyleStackDiagnostic(STYLE_KIND_COLOR_OVERLAY, "param_mismatch"),
        StyleStackDiagnostic(STYLE_KIND_DROP_SHADOW, "unsupported_conversion_version"),
    ))


def test_apply_stack_two_styles_without_double_opacity():
    foreground = np.zeros((1, 1, 4), dtype=np.float32)
    foreground[0, 0] = (0.5, 0.0, 0.0, 0.5)
    shadow_plane = np.full((1, 1), 0.5, dtype=np.float32)
    styles = (
        # Order 0: drop shadow from an upstream-blurred alpha plane.
        LayerStyleEffect(STYLE_KIND_DROP_SHADOW, 0, {
            "color": (0.0, 0.5, 0.0), "opacity": 1.0,
            "shadow_alpha_plane": shadow_plane}),
        # Order 1: red overlay at 50 %.
        LayerStyleEffect(STYLE_KIND_COLOR_OVERLAY, 1, {
            "color": (1.0, 0.0, 0.0), "opacity": 0.5}),
    )
    result = apply_style_stack_rgba(foreground, styles)
    # Shadow first (golden values), then the overlay blend at 50 %.
    assert result[0, 0, 0] == pytest.approx(0.5 * 0.5 + 1.0 * 0.75 * 0.5)
    assert result[0, 0, 1] == pytest.approx(0.125 * 0.5)
    assert result[0, 0, 3] == pytest.approx(0.75)  # alpha untouched by overlay


def test_overlay_does_not_put_rgb_in_transparent_premultiplied_pixels():
    image = np.array([[[0.0, 0.0, 0.0, 0.0], [0.25, 0.0, 0.0, 0.25]]], dtype=np.float32)
    overlay = LayerStyleEffect(STYLE_KIND_COLOR_OVERLAY, 0, {"color": (0.0, 1.0, 0.0)})
    result = apply_style_stack_rgba(image, (overlay,))
    np.testing.assert_array_equal(result, [[[0.0, 0.0, 0.0, 0.0], [0.0, 0.25, 0.0, 0.25]]])


def test_apply_stack_disable_all_with_empty_stack():
    foreground = np.full((2, 2, 4), (0.25, 0.5, 1.0, 0.75),
                         dtype=np.float32)
    result = apply_style_stack_rgba(foreground, ())
    assert result is not foreground
    np.testing.assert_array_equal(result, foreground)


def test_halo_extent_includes_radius_and_offset():
    base = style_halo_extent({"radius": 4.0})
    assert base == pytest.approx(8.0)
    with_offset = style_halo_extent(
        {"radius": 4.0, "offset_x": 3.0, "offset_y": 4.0})
    assert with_offset == pytest.approx(8.0 + 5.0)
    assert style_halo_extent({}) == 0.0
    assert style_halo_extent({"radius": "x"}) == 0.0


def test_editable_kinds_match_verified_batch():
    assert EDITABLE_STYLE_KINDS == {
        STYLE_KIND_COLOR_OVERLAY, STYLE_KIND_DROP_SHADOW,
        STYLE_KIND_OUTER_GLOW}


def test_gpu_layer_style_uses_original_alpha_for_glow_and_shadow(monkeypatch):
    import moderngl

    from src.features.composite.nodes._gpu_readback import read_rgba
    from src.features.composite.nodes.base_node import TextureResult
    from src.features.composite.nodes.layer_style_node import LayerStyleNode
    from src.features.composite.render.composite_export_renderer import CompositeExportRenderer
    from src.features.shared.effects.effect_catalog import get_effect_catalog

    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    try:
        context = moderngl.create_standalone_context()
    except Exception as exc:
        pytest.skip(f"Standalone OpenGL unavailable: {exc}")
    renderer = CompositeExportRenderer(context=context)
    pixels = np.zeros((5, 5, 4), dtype=np.float32)
    pixels[2, 2] = (1.0, 0.0, 0.0, 0.5)
    node = LayerStyleNode()
    for name, value in {
        "overlay_opacity": 0.0, "shadow_opacity": 1.0, "glow_opacity": 1.0,
        "shadow_radius": 0.0, "glow_radius": 0.0,
        "shadow_offset_x": 1.0, "shadow_offset_y": 1.0,
        "shadow_g": 2.0, "glow_r": 0.0, "glow_g": 0.0, "glow_b": 1.0,
    }.items():
        node.set_parameter(name, value)
    try:
        renderer._ensure_gpu_resources()
        texture = context.texture((5, 5), 4, np.flipud(pixels).tobytes(), dtype="f4")
        try:
            texture_result = TextureResult(texture_id=texture.glo, width=5, height=5,
                                           alpha_premultiplied=True)
            packed = node.style_uniforms(node.parameters, True)
            actual = renderer._apply_layer_style(texture_result, packed, 5, 5)["result"]
            rgba = read_rgba(actual, context, renderer.fb_manager, renderer._resource_pool)
            assert rgba is not None
            np.testing.assert_allclose(rgba[2, 2], [1.0, 0.0, 0.25, 0.75], atol=2e-3)
            np.testing.assert_allclose(rgba[3, 3], [0.0, 1.0, 0.0, 0.5], atol=2e-3)
            np.testing.assert_allclose(rgba[1, 1], [0.0, 0.0, 0.0, 0.0], atol=2e-3)
            spec = get_effect_catalog().get_spec(node.node_type)
            assert spec is not None and tuple(node.PARAM_DEFS) == spec.param_defs
            # Photoshop mixes tone-encoded RGB then returns to scene-linear.
            # Premultiplied red 0.5 at alpha 0.5 is straight red 1.0.
            from src.shared.color.color_conversion import srgb_to_linear

            pixels[2, 2, 0] = 0.5
            texture.write(np.flipud(pixels).tobytes())
            overlay = {**node.parameters, "psd_srgb": True, "overlay_r": 0.0,
                       "overlay_g": 1.0, "overlay_b": 0.0, "overlay_opacity": 0.5,
                       "shadow_opacity": 0.0, "glow_opacity": 0.0}
            packed = node.style_uniforms(overlay, True)
            actual = renderer._apply_layer_style(texture_result, packed, 5, 5)["result"]
            rgba = read_rgba(actual, context, renderer.fb_manager, renderer._resource_pool)
            channel = srgb_to_linear(0.5) * 0.5
            np.testing.assert_allclose(rgba[2, 2], [channel, channel, 0.0, 0.5], atol=2e-3)
        finally:
            texture.release()
    finally:
        renderer.cleanup()
        context.release()
