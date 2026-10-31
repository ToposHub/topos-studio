"""C3 Text+ tests — per-character animation and arc path layout.

Covers the plan's verification items:
- per-char animation numeric tables (5-char line, first/middle/last frames)
- arc glyph tangent angles (auto curvature + fixed spread, up/down)
- cache invalidation: content hash is frame-sensitive only while the
  animation is on, and graph cache keys include the frame (frame cache)
- serialization roundtrip + old-graph backward compatibility
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.features.composite.nodes.text_node import TextNode
from src.features.composite.core.graph_snapshot import NodeSnapshot

pytestmark = pytest.mark.composite_core


def _make_node(**overrides):
    node = TextNode(node_id="t_c3")
    node.set_parameter("text", "HELLO")
    node.set_parameter("font_size", 48)
    node.set_parameter("width", 400)
    node.set_parameter("height", 200)
    node.set_parameter("per_char_enable", True)
    node.set_parameter("per_char_mode", "fade")
    node.set_parameter("per_char_delay", 2.0)
    node.set_parameter("per_char_overlap", 0.0)
    for k, v in overrides.items():
        node.set_parameter(k, v)
    return node


def _placements(node, frame):
    """Char placements at ``frame`` in the centered-origin layout space."""
    from PyQt6.QtCore import QRectF

    class _DP:
        def device(self):
            return None

    node._current_frame = float(frame)
    font = node._build_font()
    rect = QRectF(-200.0, -100.0, 400.0, 200.0)
    return node._build_char_placements(
        _DP(), font, str(node.parameters.get("text", "")),
        rect, "center", "middle", 1.0)


def _alphas(node, frame):
    return [round(alpha, 4) for _p, alpha, _ang in _placements(node, frame)]


# ── Per-char animation numeric tables (plan: 5-char line) ──────────────────

class TestPerCharAnimationTables:
    """fade / delay=2 / overlap=0 → birth_i = 2i, duration = 2, ease-out."""

    def test_fade_numeric_table(self, qapp):
        node = _make_node()
        # Frame 0: only char 0 is at t=0 → nothing visible yet.
        assert _alphas(node, 0) == [0.0, 0.0, 0.0, 0.0, 0.0]
        # Frame 1: char 0 at t=0.5 → ease-out cubic 0.875; others unborn.
        assert _alphas(node, 1) == [0.875, 0.0, 0.0, 0.0, 0.0]
        # Frame 4: chars 0,1 done (duration 2), char 2 just starting.
        assert _alphas(node, 4) == [1.0, 1.0, 0.0, 0.0, 0.0]
        # Frame 10: (n-1)*delay + duration = 10 → everything settled.
        assert _alphas(node, 10) == [1.0, 1.0, 1.0, 1.0, 1.0]

    def test_fade_overlap_extends_duration(self, qapp):
        # overlap 1.0 → duration = 2 * delay = 4; at frame 2 char 0 sits at
        # t = 2/4 = 0.5 → 0.875 while char 1 is already born (birth 2 → 0).
        node = _make_node(per_char_overlap=1.0)
        assert _alphas(node, 2) == [0.875, 0.0, 0.0, 0.0, 0.0]
        # Serial case at the same frame: char 0 already finished.
        serial = _make_node()
        assert _alphas(serial, 2)[0] == 1.0

    def test_typewriter_is_binary(self, qapp):
        node = _make_node(per_char_mode="typewriter")
        # Births at 0/2/4/6/8 → frame 3 shows chars 0,1; frame 5 adds char 2.
        assert _alphas(node, 3) == [1.0, 1.0, 0.0, 0.0, 0.0]
        assert _alphas(node, 5) == [1.0, 1.0, 1.0, 0.0, 0.0]
        assert _alphas(node, 8) == [1.0, 1.0, 1.0, 1.0, 1.0]

    def test_slide_up_offsets_decay(self, qapp):
        node = _make_node(per_char_mode="slide_up",
                          per_char_slide_distance=40.0)
        # Ease-out: every char born with e=0 → fully transparent at frame 0.
        for _p, alpha, _ang in _placements(node, 0):
            assert alpha == 0.0
        # At the end frame slide offsets are 0: each glyph must land exactly
        # where the static (animation off) layout puts it.
        node.set_parameter("per_char_enable", False)
        static = [round(p.boundingRect().center().y(), 4)
                  for p, _a, _ang in _placements(node, 10)]
        node.set_parameter("per_char_enable", True)
        settled = [round(p.boundingRect().center().y(), 4)
                   for p, _a, _ang in _placements(node, 10)]
        assert settled == static

    def test_scale_pop_grows_to_full(self, qapp):
        node = _make_node(per_char_mode="scale_pop",
                          per_char_scale_from=0.5)
        # Char 0 at frame 1 has e=0.875 → scale 0.5 + 0.5*0.875 = 0.9375 of
        # its static glyph height.
        node.set_parameter("per_char_enable", False)
        static_h = _placements(node, 10)[0][0].boundingRect().height()
        node.set_parameter("per_char_enable", True)
        h1 = _placements(node, 1)[0][0].boundingRect().height()
        assert h1 == pytest.approx(static_h * 0.9375, rel=0.02)
        # Settled: full size again.
        h_end = _placements(node, 10)[0][0].boundingRect().height()
        assert h_end == pytest.approx(static_h, rel=0.02)

    def test_rotate_in_angle_decays(self, qapp):
        node = _make_node(per_char_mode="rotate_in", per_char_rotation=45.0)
        angles1 = [ang for _p, _a, ang in _placements(node, 1)]
        # Char 0 at e=0.875 → 45*(1-0.875) = 5.625°; others unborn → 45°.
        assert angles1[0] == pytest.approx(5.625, abs=0.01)
        assert angles1[2] == pytest.approx(45.0, abs=0.01)

    def test_whitespace_does_not_stagger(self, qapp):
        node = _make_node(text="A B")
        # The space draws nothing and must not consume a birth slot:
        # births are A=0, B=2 (had the space taken a slot, B would be 4 and
        # still invisible at frame 3). B at frame 3 sits at t=0.5 → 0.875.
        assert _alphas(node, 3) == [1.0, 0.875]


# ── Arc path layout (plan MVP (a): built-in arc parameters) ────────────────

class TestArcPathLayout:
    def test_auto_arc_angles_monotonic_and_centered(self, qapp):
        node = _make_node()
        node.set_parameter("per_char_enable", False)
        node.set_parameter("path_enable", True)
        node.set_parameter("path_radius", 400.0)
        angles = [ang for _p, _a, ang in _placements(node, 0)]
        # Left→right ascending, midpoint char ≈ 0° (apex).
        assert angles == sorted(angles)
        assert angles[0] < 0.0 < angles[-1]
        assert abs(angles[len(angles) // 2]) < 2.0

    def test_arc_down_mirrors_tangent(self, qapp):
        node = _make_node()
        node.set_parameter("per_char_enable", False)
        node.set_parameter("path_enable", True)
        node.set_parameter("path_direction", "up")
        up = [ang for _p, _a, ang in _placements(node, 0)]
        node.set_parameter("path_direction", "down")
        down = [ang for _p, _a, ang in _placements(node, 0)]
        assert all(abs(u + d) < 1e-6 for u, d in zip(up, down))

    def test_fixed_spread_spans_arc_degrees(self, qapp):
        node = _make_node(text="HELLO")
        node.set_parameter("per_char_enable", False)
        node.set_parameter("path_enable", True)
        node.set_parameter("path_arc_degrees", 90.0)
        angles = [ang for _p, _a, ang in _placements(node, 0)]
        # Total span 90° centred on the apex, distributed by arc-length
        # position. The first/last CHAR CENTRES fall short of ±45° by half
        # a glyph width (≈10° for HELLO at this size); monotonic in between.
        assert angles == sorted(angles)
        assert -45.0 < angles[0] < -25.0
        assert 25.0 < angles[-1] < 45.0

    def test_arc_alpha_untouched_without_animation(self, qapp):
        node = _make_node()
        node.set_parameter("per_char_enable", False)
        node.set_parameter("path_enable", True)
        assert _alphas(node, 0) == [1.0] * 5

    def test_animation_composes_with_arc(self, qapp):
        # Per-char fade applies on top of arc placement.
        node = _make_node()
        node.set_parameter("path_enable", True)
        alphas = _alphas(node, 1)
        assert alphas == [0.875, 0.0, 0.0, 0.0, 0.0]


# ── Rasterization + measurement ────────────────────────────────────────────

class TestPerCharRaster:
    def test_typewriter_frame0_vs_frame10_coverage(self, qapp):
        node = _make_node(per_char_mode="typewriter")
        node._update_current_frame(type("Ctx", (), {"frame_number": 0})())
        bb = node._measure_text_bbox()
        w, h = max(1, int(round(bb[2]))), max(1, int(round(bb[3])))
        d0 = node._render_text_pixels(w, h)
        node._update_current_frame(type("Ctx", (), {"frame_number": 10})())
        d10 = node._render_text_pixels(w, h)
        cov0 = int((d0[..., 3] > 0.5).sum())
        cov10 = int((d10[..., 3] > 0.5).sum())
        # Frame 0: only "H" born; frame 10: whole word ("H" is roughly a
        # quarter of the total coverage, so 3x is a safe lower bound).
        assert 0 < cov0 < cov10
        assert cov10 > 3 * max(cov0, 1)

    def test_frame_plumbing_via_time_range_origin(self, qapp):
        node = _make_node(per_char_mode="typewriter")
        node.time_range = {"enabled": True, "comp_start": 100}
        ctx = type("Ctx", (), {"frame_number": 100})()
        node._update_current_frame(ctx)
        assert node._current_frame == pytest.approx(0.0)
        ctx.frame_number = 104
        node._update_current_frame(ctx)
        assert node._current_frame == pytest.approx(4.0)

    def test_empty_text_with_per_char_renders_transparent(self, qapp):
        node = _make_node(text="   ")
        data = node._render_text_pixels(64, 64)
        assert float(data[..., 3].max()) < 0.01

    def test_arc_bbox_and_render(self, qapp):
        node = _make_node()
        node.set_parameter("per_char_enable", False)
        node.set_parameter("path_enable", True)
        node.set_parameter("path_radius", 300.0)
        bb = node._measure_text_bbox()
        assert bb[2] > 0 and bb[3] > 0
        data = node._render_text_pixels(
            max(1, int(round(bb[2]))), max(1, int(round(bb[3]))))
        assert int((data[..., 3] > 0.5).sum()) > 100

    def test_pil_fallback_degrades_to_static(self, qapp):
        # The headless PIL renderer must not crash with per-char on; it
        # returns the static whole-block layout instead.
        node = _make_node(per_char_mode="typewriter")
        data = node._render_text_pixels_pil(256, 128)
        assert data.shape == (128, 256, 4)
        assert int((data[..., 3] > 0.5).sum()) > 100


# ── Cache invalidation ──────────────────────────────────────────────────────

class TestFrameCacheInvalidation:
    def test_content_hash_frame_sensitive_only_when_enabled(self, qapp):
        node = _make_node()
        node._current_frame = 0.0
        h0 = node._content_hash()
        node._current_frame = 5.0
        assert node._content_hash() != h0
        node.set_parameter("per_char_enable", False)
        node._current_frame = 0.0
        h_off = node._content_hash()
        node._current_frame = 99.0
        assert node._content_hash() == h_off

    def test_content_hash_param_sensitivity(self, qapp):
        node = _make_node()
        node._current_frame = 0.0
        h = node._content_hash()
        node.set_parameter("per_char_delay", 3.0)
        assert node._content_hash() != h
        node.set_parameter("per_char_delay", 2.0)
        h2 = node._content_hash()
        node.set_parameter("path_radius", 500.0)
        assert node._content_hash() != h2

    def test_params_require_frame_cache_hook(self):
        assert TextNode.params_require_frame_cache(
            {"per_char_enable": True}) is True
        assert TextNode.params_require_frame_cache(
            {"per_char_enable": False}) is False
        assert TextNode.params_require_frame_cache(
            {"path_enable": True}) is False
        assert TextNode.params_require_frame_cache({}) is False

    def test_graph_cache_key_includes_frame(self):
        from src.features.composite.evaluators.base_evaluator import (
            _node_needs_frame_cache,
        )
        ns_on = NodeSnapshot(
            node_id="t", node_type="text",
            parameters={"per_char_enable": True},
            disabled=False, input_ports=[], output_ports=[])
        ns_off = NodeSnapshot(
            node_id="t", node_type="text",
            parameters={"per_char_enable": False},
            disabled=False, input_ports=[], output_ports=[])
        assert _node_needs_frame_cache(ns_on) is True
        assert _node_needs_frame_cache(ns_off) is False


# ── Serialization ───────────────────────────────────────────────────────────

class TestPerCharSerialization:
    def test_roundtrip_preserves_new_params(self, qapp):
        node = _make_node(per_char_mode="slide_up", per_char_delay=3.5,
                          per_char_overlap=0.5, path_enable=True,
                          path_radius=350.0, path_arc_degrees=120.0,
                          path_direction="down")
        data = node.to_dict()
        clone = TextNode.from_dict(dict(data))
        for key, expect in (
                ("per_char_enable", True),
                ("per_char_mode", "slide_up"),
                ("per_char_delay", 3.5),
                ("per_char_overlap", 0.5),
                ("path_enable", True),
                ("path_radius", 350.0),
                ("path_arc_degrees", 120.0),
                ("path_direction", "down")):
            assert clone.parameters.get(key) == expect, key

    def test_legacy_graph_gets_defaults(self, qapp):
        node = _make_node()
        data = node.to_dict()
        for key in ("per_char_enable", "per_char_mode", "per_char_delay",
                    "per_char_overlap", "per_char_slide_distance",
                    "per_char_rotation", "per_char_scale_from",
                    "path_enable", "path_radius", "path_arc_degrees",
                    "path_direction"):
            data["parameters"].pop(key, None)
        clone = TextNode.from_dict(data)
        assert clone.parameters.get("per_char_enable") is False
        assert clone.parameters.get("per_char_mode") == "fade"
        assert clone.parameters.get("path_enable") is False
        assert clone.parameters.get("path_radius") == 400.0

    def test_visibility_and_aliases(self):
        node = _make_node(per_char_enable=False)
        hidden_off = TextNode.param_visibility(node.parameters)
        assert "per_char_delay" in hidden_off
        assert "path_radius" in hidden_off
        node.set_parameter("per_char_enable", True)
        node.set_parameter("path_enable", True)
        hidden_on = TextNode.param_visibility(node.parameters)
        assert "per_char_delay" not in hidden_on
        assert "path_radius" not in hidden_on
        # Plan: alias registry updated for 逐字 / 弧形.
        assert "逐字" in TextNode.SEARCH_ALIASES
        assert "弧形" in TextNode.SEARCH_ALIASES
