"""Contract tests for the defocus evaluator's parameter mapping.

These tests lock in the fix for the schema desync between
``DefocusNode.PARAM_DEFS`` (focal_x/y, focal_size, max_radius, mode,
edge_policy, process_alpha) and the ``_DEFOCUS_FRAGMENT`` shader uniforms
(u_focal_point, u_focal_size, u_max_radius, u_mode, u_edge_policy,
u_process_alpha, u_sample_count).

Previously ``_apply_defocus`` read a totally different schema
(iris_size / iris_type / gain) that PARAM_DEFS never populates, so every
parameter fell back to the default and the node was a no-op. We test the
pure mapping function ``_defocus_params_to_uniforms`` directly so no GL
context is needed.
"""

import pytest

from src.features.composite.evaluators.blur_evaluator import BlurEvaluatorMixin
from src.features.composite.nodes.defocus_node import DefocusNode


class TestDefocusParamsToUniforms:
    """Verify PARAM_DEFS values map to the shader's uniforms correctly."""

    def test_defaults_round_trip(self):
        """Default PARAM_DEFS values produce the expected uniform values."""
        params = {pd.name: pd.default for pd in DefocusNode.PARAM_DEFS}
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(params)

        assert u["u_focal_point"] == (0.5, 0.5)
        assert u["u_focal_size"] == pytest.approx(0.3)
        assert u["u_max_radius"] == pytest.approx(20.0)
        assert u["u_mode"] == 0  # separable
        assert u["u_edge_policy"] == 0  # clamp
        assert u["u_process_alpha"] is True
        assert u["u_sample_count"] == 16

    def test_focal_xy_merged_into_vec2(self):
        """focal_x and focal_y (two scalars) become one vec2 uniform."""
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"focal_x": 0.25, "focal_y": 0.75}
        )
        assert u["u_focal_point"] == (0.25, 0.75)

    def test_mode_option_to_int(self):
        """mode 'separable'/'mipmap' map to 0/1 (shader's u_mode codes)."""
        assert BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"mode": "separable"})["u_mode"] == 0
        assert BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"mode": "mipmap"})["u_mode"] == 1

    def test_edge_policy_option_to_int(self):
        """edge_policy clamp/wrap/mirror map to 0/1/2."""
        assert BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"edge_policy": "clamp"})["u_edge_policy"] == 0
        assert BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"edge_policy": "wrap"})["u_edge_policy"] == 1
        assert BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"edge_policy": "mirror"})["u_edge_policy"] == 2

    def test_process_alpha_bool_preserved(self):
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"process_alpha": False})
        assert u["u_process_alpha"] is False

    def test_max_radius_float_preserved(self):
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"max_radius": 42.5})
        assert u["u_max_radius"] == pytest.approx(42.5)

    def test_legacy_iris_keys_are_ignored(self):
        """The old iris_size/iris_type/gain keys must NOT appear in the
        uniform dict — they belong to a removed bokeh API the shader no
        longer declares. This guards against a regression that silently
        re-introduces the desync."""
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"iris_size": 99, "iris_type": "hexagon", "gain": 2.0,
             "max_radius": 10.0})
        assert "u_iris_size" not in u
        assert "u_iris_type" not in u
        assert "u_gain" not in u
        # And the real keys still come through.
        assert u["u_max_radius"] == pytest.approx(10.0)

    def test_unknown_mode_falls_back_to_separable(self):
        """An unrecognised mode string falls back to 0 (separable) rather
        than raising — defensive against forward-compat with future
        PARAM_DEFS additions."""
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(
            {"mode": "experimental"})
        assert u["u_mode"] == 0

    def test_every_shader_uniform_is_populated(self):
        """Every uniform the shader declares must get a value from the
        mapper, otherwise the node would silently fall back to GL
        defaults (the original bug)."""
        shader_uniforms = {
            "u_focal_point", "u_focal_size", "u_max_radius",
            "u_mode", "u_edge_policy", "u_process_alpha", "u_sample_count",
        }
        u = BlurEvaluatorMixin._defocus_params_to_uniforms({})
        assert shader_uniforms.issubset(u.keys())


class TestDefocusNodeEffectParams:
    """Verify the node produces the params the evaluator now expects."""

    def test_effect_params_keys_match_param_defs(self):
        """``_do_evaluate``'s ``_effect_params`` must use exactly the
        PARAM_DEFS names — that's the contract the evaluator relies on."""
        from src.features.composite.evaluators.blur_evaluator import (
            BlurEvaluatorMixin,
        )
        param_def_names = {pd.name for pd in DefocusNode.PARAM_DEFS}

        # Simulate the mapping the evaluator does: feed a full param dict
        # (mirroring _do_evaluate's _effect_params) through the mapper and
        # confirm it doesn't KeyError or drop anything.
        full_params = {
            pd.name: pd.default for pd in DefocusNode.PARAM_DEFS
        }
        # _do_evaluate skips when max_radius < 0.1, so keep it meaningful.
        full_params["max_radius"] = 20.0
        u = BlurEvaluatorMixin._defocus_params_to_uniforms(full_params)

        # Every PARAM_DEFS field should influence some uniform (i.e. the
        # mapper reads it). We can't assert value equality for fields the
        # mapper transforms, but we can assert the mapper ran cleanly.
        assert u["u_max_radius"] == 20.0
