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
    assert shadow_under((8.0, 0.0, 0.0, 0.0), (0.0, 4.0, 0.0), 0.25) == (
        8.0, 1.0, 0.0, 0.25)
    # Opaque foreground: the shadow is fully hidden.
    assert shadow_under((0.5, 0.5, 0.5, 1.0), (0.0, 0.0, 0.0), 1.0) == (
        0.5, 0.5, 0.5, 1.0)


def test_color_over_applies_once():
    result = color_over((0.2, 0.2, 0.2, 0.5), (1.0, 0.0, 0.0), 0.5)
    assert result == pytest.approx((0.6, 0.1, 0.1, 0.5))


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
    assert LayerStyleEffect.from_dict({"kind": "bogus"}) is None
    assert LayerStyleEffect.from_dict("nope") is None


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
    assert result[0, 0, 0] == pytest.approx(0.5 * 0.5 + 1.0 * 0.5)
    assert result[0, 0, 1] == pytest.approx(0.125 * 0.5)
    assert result[0, 0, 3] == pytest.approx(0.75)  # alpha untouched by overlay


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
