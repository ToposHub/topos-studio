"""Tests for the Phase 2 Filter specs (denoise / edge_detect /
erode_dilate / defocus / glow).

Verifies:
1. All 5 new specs are registered in the catalog with correct kind.
2. Each spec's param_defs match the corresponding composite node's
   PARAM_DEFS (the "identical controls" contract).
3. Each packer correctly translates user params → shader uniforms
   (option→int, multi-param→vec, iterations extraction, etc.).
4. Shader sources are real GLSL with the right version directive.
5. The Edit param widget factory renders each ptype correctly.
"""

from __future__ import annotations

import pytest

from src.features.composite.nodes.base_node import ParamDef
from src.features.shared.effects import get_effect_catalog, ShaderKind
from src.features.shared.effects.effect_pass_runner import (
    _pack_denoise_params,
    _pack_edge_detect_params,
    _pack_erode_dilate_params,
    _pack_defocus_params,
    _pack_glow_params,
    _PARAM_PACKERS,
)


# ──────────────────────────────────────────────────────────────────────────
# Catalog registration
# ──────────────────────────────────────────────────────────────────────────

PHASE2_SPECS = ["denoise", "edge_detect", "erode_dilate", "defocus", "glow"]


class TestCatalogRegistration:
    def test_all_phase2_specs_registered(self):
        cat = get_effect_catalog()
        for et in PHASE2_SPECS:
            assert cat.is_known(et), f"{et} should be registered"
            spec = cat.get_spec(et)
            assert spec is not None
            assert spec.shader_kind in (ShaderKind.SINGLE_PASS, ShaderKind.SEPARABLE_PASSES)

    def test_glow_is_separable_others_single_pass(self):
        cat = get_effect_catalog()
        assert cat.get_spec("glow").shader_kind == ShaderKind.SEPARABLE_PASSES
        assert cat.get_spec("glow").shader_sources.__len__() == 2
        for et in ("denoise", "edge_detect", "erode_dilate", "defocus"):
            assert cat.get_spec(et).shader_kind == ShaderKind.SINGLE_PASS
            assert len(cat.get_spec(et).shader_sources) == 1

    def test_all_specs_have_real_glsl(self):
        cat = get_effect_catalog()
        for et in PHASE2_SPECS:
            for src in cat.get_spec(et).shader_sources:
                assert "#version 330" in src, f"{et} shader missing #version"
                assert "void main()" in src, f"{et} shader missing main()"

    @pytest.mark.parametrize("effect_type,node_module,node_class", [
        ("denoise", "src.features.composite.nodes.denoise_node", "DenoiseNode"),
        ("edge_detect", "src.features.composite.nodes.edge_detect_node", "EdgeDetectNode"),
        ("erode_dilate", "src.features.composite.nodes.erode_dilate_node", "ErodeDilateNode"),
        ("defocus", "src.features.composite.nodes.defocus_node", "DefocusNode"),
        ("glow", "src.features.composite.nodes.glow_node", "GlowNode"),
    ])
    def test_param_defs_match_composite_node(self, effect_type, node_module, node_class):
        """The Edit panel must show the same params as the composite node."""
        import importlib
        mod = importlib.import_module(node_module)
        NodeClass = getattr(mod, node_class)
        spec = get_effect_catalog().get_spec(effect_type)
        spec_names = {p.name for p in spec.param_defs}
        node_names = {p.name for p in NodeClass.PARAM_DEFS}
        assert spec_names == node_names, (
            f"{effect_type} spec params {spec_names} != node params {node_names}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Packer registry
# ──────────────────────────────────────────────────────────────────────────

class TestPackerRegistry:
    def test_all_phase2_packers_registered(self):
        for et in PHASE2_SPECS:
            assert et in _PARAM_PACKERS, f"{et} packer missing from registry"


# ──────────────────────────────────────────────────────────────────────────
# denoise packer
# ──────────────────────────────────────────────────────────────────────────

class TestDenoisePacker:
    def test_direct_passthrough(self):
        packed = _pack_denoise_params({"strength": 0.8, "radius": 5})
        assert packed == {
            "u_strength": 0.8, "u_radius": 5,
            "u_luminance_only": False, "u_chroma_only": False,
        }

    def test_radius_clamped_to_min_1(self):
        assert _pack_denoise_params({"radius": 0})["u_radius"] == 1

    def test_bool_flags(self):
        packed = _pack_denoise_params({
            "luminance_only": True, "chroma_only": True,
        })
        assert packed["u_luminance_only"] is True
        assert packed["u_chroma_only"] is True


# ──────────────────────────────────────────────────────────────────────────
# edge_detect packer
# ──────────────────────────────────────────────────────────────────────────

class TestEdgeDetectPacker:
    def test_method_indexed(self):
        # ["sobel", "laplacian", "prewitt", "canny"]
        assert _pack_edge_detect_params({"method": "sobel"})["u_method"] == 0
        assert _pack_edge_detect_params({"method": "canny"})["u_method"] == 3

    def test_direction_indexed(self):
        # ["both", "horizontal", "vertical"]
        assert _pack_edge_detect_params({"direction": "vertical"})["u_direction"] == 2

    def test_output_mode_indexed(self):
        # ["edges", "gradient", "direction"]
        assert _pack_edge_detect_params({"output_mode": "direction"})["u_output_mode"] == 2

    def test_channel_indexed(self):
        # ["luminance", "r", "g", "b", "a"]
        assert _pack_edge_detect_params({"channel": "a"})["u_channel"] == 4

    def test_floats_passed_through(self):
        packed = _pack_edge_detect_params({"threshold": 0.25, "preblur": 1.5})
        assert packed["u_threshold"] == 0.25
        assert packed["u_preblur"] == 1.5

    def test_bool_and_int(self):
        packed = _pack_edge_detect_params({"invert": True, "kernel_size": 5})
        assert packed["u_invert"] is True
        assert packed["u_kernel_size"] == 5

    def test_unknown_method_falls_back_to_zero(self):
        assert _pack_edge_detect_params({"method": "nonexistent"})["u_method"] == 0


# ──────────────────────────────────────────────────────────────────────────
# erode_dilate packer
# ──────────────────────────────────────────────────────────────────────────

class TestErodeDilatePacker:
    def test_operation_indexed(self):
        # ["erode", "dilate"] → 0, 1
        assert _pack_erode_dilate_params({"operation": "erode"})["u_operation"] == 0
        assert _pack_erode_dilate_params({"operation": "dilate"})["u_operation"] == 1

    def test_edge_mode_indexed(self):
        # ["extend", "reflect", "wrap"]
        assert _pack_erode_dilate_params({"edge_mode": "wrap"})["u_edge_mode"] == 2

    def test_channels_indexed(self):
        # ["alpha", "all"] → 0, 1
        assert _pack_erode_dilate_params({"channels": "all"})["u_channels"] == 1

    def test_iterations_extracted_separately(self):
        """iterations is NOT a uniform — it controls the runner's loop."""
        packed = _pack_erode_dilate_params({"iterations": 4})
        assert packed["_iterations"] == 4
        # No u_iterations uniform should be present.
        assert "u_iterations" not in packed

    def test_size_clamped_to_min_1(self):
        assert _pack_erode_dilate_params({"size": 0})["u_size"] == 1


# ──────────────────────────────────────────────────────────────────────────
# defocus packer
# ──────────────────────────────────────────────────────────────────────────

class TestDefocusPacker:
    def test_focal_xy_packed_to_vec2(self):
        packed = _pack_defocus_params({"focal_x": 0.3, "focal_y": 0.7})
        assert packed["u_focal_point"] == (0.3, 0.7)

    def test_max_radius_passthrough(self):
        assert _pack_defocus_params({"max_radius": 42.0})["u_max_radius"] == 42.0

    def test_mode_indexed(self):
        # ["separable", "mipmap"] → 0, 1
        assert _pack_defocus_params({"mode": "separable"})["u_mode"] == 0
        assert _pack_defocus_params({"mode": "mipmap"})["u_mode"] == 1

    def test_edge_policy_indexed(self):
        # ["clamp", "wrap", "mirror"]
        assert _pack_defocus_params({"edge_policy": "mirror"})["u_edge_policy"] == 2

    def test_process_alpha_bool(self):
        packed = _pack_defocus_params({"process_alpha": False})
        assert packed["u_process_alpha"] is False

    def test_sample_count_default(self):
        # No quality param in defocus PARAM_DEFS; packer supplies a default.
        assert _pack_defocus_params({})["u_sample_count"] == 16


# ──────────────────────────────────────────────────────────────────────────
# glow packer
# ──────────────────────────────────────────────────────────────────────────

class TestGlowPacker:
    def test_tint_rgb_merged_to_vec3(self):
        packed = _pack_glow_params({"tint_r": 1.0, "tint_g": 0.5, "tint_b": 0.2})
        assert packed["u_tint"] == (1.0, 0.5, 0.2)
        # No separate tint_r/g/b uniforms.
        assert "u_tint_r" not in packed

    def test_quality_to_sample_count(self):
        # {"fast": 8, "normal": 16, "high": 32}
        assert _pack_glow_params({"quality": "fast"})["u_sample_count"] == 8
        assert _pack_glow_params({"quality": "high"})["u_sample_count"] == 32

    def test_defaults(self):
        packed = _pack_glow_params({})
        assert packed["u_threshold"] == 0.7
        assert packed["u_radius"] == 15.0
        assert packed["u_intensity"] == 1.5
        assert packed["u_mix"] == 1.0


# ──────────────────────────────────────────────────────────────────────────
# Edit param widget factory (ParamDef-based)
# ──────────────────────────────────────────────────────────────────────────

class TestParamWidgetFromPdef:
    """Verify the new ParamDef-based factory builds the right widget type."""

    def _make_panel(self, qapp):
        from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
        from unittest.mock import MagicMock
        panel = EditPropertiesPanel()
        # The factory references self._current_clips[0] for signal emit;
        # give it a mock so it doesn't crash on valueChanged.
        mock_clip = MagicMock()
        mock_clip.clip_id = "c1"
        panel._current_clips = [mock_clip]
        return panel

    def test_float_builds_slider_and_dragvalue(self, qapp):
        panel = self._make_panel(qapp)
        pdef = ParamDef("radius", "float", 5.0, min_val=0.0, max_val=100.0, step=0.5)
        widget = panel._create_effect_param_widget_from_pdef(pdef, 10.0, "e1")
        assert widget is not None
        # The row should contain a ToposClickableSlider + DragValue.
        from src.shared.ui.widgets.topos_controls import ToposClickableSlider
        from src.shared.ui.widgets.drag_value import DragValue
        sliders = widget.findChildren(ToposClickableSlider)
        drags = widget.findChildren(DragValue)
        assert len(sliders) == 1
        assert len(drags) == 1

    def test_int_builds_slider_with_int_range(self, qapp):
        panel = self._make_panel(qapp)
        pdef = ParamDef("size", "int", 3, min_val=1, max_val=10)
        widget = panel._create_effect_param_widget_from_pdef(pdef, 5, "e1")
        assert widget is not None
        from src.shared.ui.widgets.topos_controls import ToposClickableSlider
        sliders = widget.findChildren(ToposClickableSlider)
        assert len(sliders) == 1
        # int range uses setRange(int, int) directly
        assert sliders[0].minimum() == 1
        assert sliders[0].maximum() == 10

    def test_bool_builds_checkbox(self, qapp):
        panel = self._make_panel(qapp)
        pdef = ParamDef("invert", "bool", False)
        widget = panel._create_effect_param_widget_from_pdef(pdef, True, "e1")
        from PyQt6.QtWidgets import QCheckBox
        checkboxes = widget.findChildren(QCheckBox)
        assert len(checkboxes) == 1
        assert checkboxes[0].isChecked() is True

    def test_option_builds_combo_with_labels(self, qapp):
        panel = self._make_panel(qapp)
        pdef = ParamDef("method", "option", "sobel", labels=["sobel", "canny"])
        widget = panel._create_effect_param_widget_from_pdef(pdef, "canny", "e1")
        # ChevronComboBox is used; check it has the right items.
        combos = widget.findChildren(__import__(
            "src.shared.ui.widgets.chevron_combo", fromlist=["ChevronComboBox"]
        ).ChevronComboBox)
        assert len(combos) == 1
        assert combos[0].count() == 2
        assert combos[0].currentText() == "canny"

    def test_unknown_ptype_falls_back_to_readonly(self, qapp):
        panel = self._make_panel(qapp)
        pdef = ParamDef("vec", "vec2", (0.0, 0.0))
        widget = panel._create_effect_param_widget_from_pdef(pdef, (1.0, 2.0), "e1")
        # Read-only fallback: contains a QLabel showing the value.
        from PyQt6.QtWidgets import QLabel
        labels = widget.findChildren(QLabel)
        # Should have the param-name label + the value label.
        assert any("(1.0, 2.0)" in l.text() or "1.0" in l.text() for l in labels)
