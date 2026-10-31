"""Regression tests for the Graph Editor cleanup fixes (batch 4).

Pure-data tests (no Qt widget) locking the code-hygiene fixes:

1. BUG-1: apply_scale_transform no longer has dead tspan/vspan variables
   that the docstring falsely claimed protected against division-by-zero.

2. BUG-4: _AXIS_SUFFIXES is now the single source of truth used by
   param_channel / param_base_name (previously dead code).

3. BUG-14: _CHANNEL_COLORS no longer has an unreachable 'w' key (param_channel
   never returns 'w').

4. BUG-6: _on_spline_graph_mode_redirect doesn't re-raise the dock when the
   Keyframes panel is already in graph mode (verified structurally to avoid
   instantiating the full CompositePage under offscreen Qt).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import inspect

import pytest

from src.features.composite.ui import graph_model
from src.features.composite.ui.graph_model import (
    apply_scale_transform, GraphPoint, param_channel, param_base_name,
    channel_color,
)


pytestmark = pytest.mark.composite_core


# ── BUG-1: apply_scale_transform has no dead span variables ───────────


class TestApplyScaleNoDeadCode:
    def test_no_dead_tspan_vspan(self):
        """BUG-1: the tspan/vspan locals that the docstring falsely claimed
        guarded against NaN are gone."""
        src = inspect.getsource(apply_scale_transform)
        assert "tspan" not in src, (
            "apply_scale_transform must not carry the dead tspan variable "
            "(BUG-1).")
        assert "vspan" not in src, (
            "apply_scale_transform must not carry the dead vspan variable "
            "(BUG-1).")

    def test_docstring_no_false_divbyzero_claim(self):
        src = inspect.getsource(apply_scale_transform)
        # The old docstring promised "A zero span divides by max(1e-9, span)
        # so a single-frame box can't NaN" — which was untrue. Ensure that
        # misleading claim is gone.
        assert "can't NaN" not in src

    def test_function_still_correct_after_cleanup(self):
        """Sanity: apply_scale_transform still scales anchor-relative."""
        pts = [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=10.0, value=5.0, speed=None,
                          keyframe_index=0, interpolation_in="linear",
                          interpolation_out="linear")]
        bbox = (0.0, 20.0, 0.0, 10.0)
        anchor = (0.0, 0.0)
        out = apply_scale_transform(pts, bbox, anchor, 2.0, 3.0)
        assert len(out) == 1
        _pt, nf, nv = out[0]
        assert nf == 20.0   # 0 + (10-0)*2
        assert nv == 15.0   # 0 + (5-0)*3

    def test_degenerate_bbox_does_not_nan(self):
        """A zero-span bbox must not produce NaN (the caller-side guard in
        _do_box_scale_drag clamps the denominator; this just confirms the
        model function itself is safe with a degenerate bbox)."""
        pts = [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                          channel=None, frame=5.0, value=5.0, speed=None,
                          keyframe_index=0, interpolation_in="linear",
                          interpolation_out="linear")]
        bbox = (5.0, 5.0, 5.0, 5.0)  # zero span on both axes
        anchor = (5.0, 5.0)
        out = apply_scale_transform(pts, bbox, anchor, 2.0, 3.0)
        _pt, nf, nv = out[0]
        assert nf == 5.0  # anchor == point → stays at anchor
        assert nv == 5.0
        # No NaN.
        import math
        assert not math.isnan(nf) and not math.isnan(nv)


# ── BUG-4: _AXIS_SUFFIXES is the single source of truth ───────────────


class TestAxisSuffixesUsed:
    def test_param_channel_uses_axis_suffixes(self):
        """BUG-4: param_channel reads _AXIS_SUFFIXES instead of a hard-coded
        duplicate tuple."""
        src = inspect.getsource(param_channel)
        assert "_AXIS_SUFFIXES" in src, (
            "param_channel must use the _AXIS_SUFFIXES constant (BUG-4), not "
            "a hard-coded duplicate.")

    def test_param_base_name_uses_axis_suffixes(self):
        src = inspect.getsource(param_base_name)
        assert "_AXIS_SUFFIXES" in src

    def test_axis_suffixes_only_xyz(self):
        """_AXIS_SUFFIXES contains exactly _x/_y/_z (no bare x/y/z, no _w)."""
        assert graph_model._AXIS_SUFFIXES == ("_x", "_y", "_z")

    def test_channel_detection_still_works(self):
        """Sanity: the refactor didn't change detection behaviour."""
        assert param_channel("translate_x") == "x"
        assert param_channel("scale_y") == "y"
        assert param_channel("rotation_z") == "z"
        assert param_channel("rotation") is None
        assert param_channel("max_xy") is None
        assert param_base_name("translate_x") == "translate"
        assert param_base_name("rotation") == "rotation"


# ── BUG-14: _CHANNEL_COLORS has no unreachable 'w' key ────────────────


class TestChannelColorsNoDeadW:
    def test_no_w_key(self):
        """BUG-14: the 'w' key was unreachable (param_channel never returns
        'w') and is removed."""
        assert "w" not in graph_model._CHANNEL_COLORS

    def test_only_xyz_keys(self):
        assert set(graph_model._CHANNEL_COLORS.keys()) == {"x", "y", "z"}

    def test_channel_color_xyz_resolves(self):
        """Sanity: x/y/z still resolve to their fixed colours."""
        assert channel_color("x") == "#ff8a3d"
        assert channel_color("y") == "#3dd97a"
        assert channel_color("z") == "#3d9cff"

    def test_channel_color_w_falls_back(self):
        """A hypothetical 'w' now hits the fallback (no longer a dead key)."""
        assert channel_color("w") == "#bdbdbd"  # fallback


# ── BUG-6: redirect doesn't re-raise when already in graph mode ───────


class TestRedirectDedupe:
    def test_redirect_handler_checks_already_graph(self):
        """BUG-6: _on_spline_graph_mode_redirect must skip the dock raise
        when the Keyframes panel is already in graph mode (so repeated Spline
        re-shows don't steal focus). Verified structurally."""
        from src.features.composite.ui.composite_page import CompositePage
        src = inspect.getsource(CompositePage._on_spline_graph_mode_redirect)
        assert "already_graph" in src or "_view_mode" in src, (
            "The redirect handler must check whether graph mode is already "
            "active before re-raising the dock (BUG-6).")
