"""Tests for ImageEngine parameter setter methods."""

import pytest
import numpy as np


class TestGradeImage:
    def test_sets_grading_params(self, image_engine):
        image_engine.grade_image(
            lift=(1, 0, 0), gamma=(0, 1, 0), gain=(0, 0, 1),
            exp=1.0, temp=0.5, tint=-0.3, contrast=0.8,
            shadow=-0.5, sat=1.2, vibrance=0.3, mid_detail=-0.2, hue_shift=0.1
        )
        layer = image_engine.get_active_layer()
        assert layer.grading_params["lift"] == (1, 0, 0)
        assert layer.grading_params["exposure"] == 1.0
        assert layer.grading_params["saturation"] == 1.2
        assert layer.grading_params["vibrance"] == 0.3
        assert layer.grading_params["hue_shift"] == 0.1

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.grade_image(
            lift=(0, 0, 0), gamma=(0, 0, 0), gain=(0, 0, 0),
            exp=0, temp=0, tint=0, contrast=0, shadow=0, sat=0
        )


class TestApplyCurves:
    def test_sets_curves_lut(self, image_engine):
        lut = np.zeros((256, 1, 3), dtype=np.float32)
        image_engine.apply_curves(lut)
        layer = image_engine.get_active_layer()
        assert layer.curves_lut is lut

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.apply_curves(np.zeros((256, 1, 3), dtype=np.float32))


class TestApplyHslCurve:
    def test_sets_hsl_curve_lut(self, image_engine):
        lut = np.zeros((6, 256), dtype=np.float32)
        image_engine.apply_hsl_curve(lut)
        layer = image_engine.get_active_layer()
        assert layer.hsl_curve_lut is lut


class TestSetMixerParams:
    def test_sets_mixer_params(self, image_engine):
        params = {"red": [0.1, 0, 0], "green": [0, 0.2, 0]}
        image_engine.set_mixer_params(params)
        layer = image_engine.get_active_layer()
        assert layer.mixer_params == params

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.set_mixer_params({"red": [1, 0, 0]})


class TestSetEffectsParams:
    def test_sets_effects_params(self, image_engine):
        params = {"enabled": True, "sharpen": 0.5}
        image_engine.set_effects_params(params)
        layer = image_engine.get_active_layer()
        assert layer.effects_params == params


class TestSetSoftClipParams:
    def test_updates_soft_clip(self, image_engine):
        image_engine.set_soft_clip_params({"enabled": True, "high": 0.8})
        layer = image_engine.get_active_layer()
        assert layer.soft_clip_params["enabled"] is True
        assert layer.soft_clip_params["high"] == 0.8

    def test_partial_update(self, image_engine):
        original = image_engine.get_active_layer().soft_clip_params.copy()
        image_engine.set_soft_clip_params({"high": 0.5})
        layer = image_engine.get_active_layer()
        assert layer.soft_clip_params["high"] == 0.5
        # Other params unchanged
        assert layer.soft_clip_params["low"] == original["low"]


class TestSetQualifierParams:
    def test_updates_qualifier(self, image_engine):
        image_engine.set_qualifier_params({"enabled": True, "hue_center": 0.3})
        layer = image_engine.get_active_layer()
        assert layer.qualifier_params["enabled"] is True
        assert layer.qualifier_params["hue_center"] == 0.3


class TestSetFilmGrainParams:
    def test_updates_film_grain(self, image_engine):
        image_engine.set_film_grain_params({"enabled": True, "intensity": 0.5})
        assert image_engine.film_grain_params["enabled"] is True
        assert image_engine.film_grain_params["intensity"] == 0.5

    def test_adds_seed_if_missing(self, image_engine):
        image_engine.set_film_grain_params({"enabled": True})
        assert "seed" in image_engine.film_grain_params


class TestApplyLut:
    def test_sets_lut_path(self, image_engine):
        result = image_engine.apply_lut("/path/to/lut.cube")
        assert result is True
        layer = image_engine.get_active_layer()
        assert layer.active_lut_path == "/path/to/lut.cube"

    def test_no_active_layer_returns_false(self, image_engine):
        image_engine.active_layer_index = 999
        result = image_engine.apply_lut("/path/to/lut.cube")
        assert result is False


class TestSetLutIntensity:
    def test_sets_intensity(self, image_engine):
        image_engine.set_lut_intensity(0.7)
        layer = image_engine.get_active_layer()
        assert layer.lut_intensity == 0.7


class TestResetLut:
    def test_resets_lut_path(self, image_engine):
        image_engine.apply_lut("/path/to/lut.cube")
        image_engine.reset_lut()
        layer = image_engine.get_active_layer()
        assert layer.active_lut_path is None


class TestSetWarperParams:
    def test_sets_warper(self, image_engine):
        grid = np.zeros((10, 10, 2), dtype=np.float32)
        image_engine.set_warper_params(grid)
        layer = image_engine.get_active_layer()
        assert layer.warper_params is grid

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.set_warper_params(np.zeros((10, 10, 2), dtype=np.float32))


class TestRenderPipelineCalled:
    def test_grade_image_does_not_call_render(self, image_engine):
        """grade_image only updates params; render timing is controlled by UI layer."""
        image_engine.render_pipeline.reset_mock()
        image_engine.grade_image(
            (0, 0, 0), (0, 0, 0), (0, 0, 0), 0, 0, 0, 0, 0, 0
        )
        image_engine.render_pipeline.assert_not_called()

    def test_apply_curves_calls_render(self, image_engine):
        image_engine.render_pipeline.reset_mock()
        image_engine.apply_curves(np.zeros((256, 1, 3), dtype=np.float32))
        image_engine.render_pipeline.assert_called_once()

    def test_set_blend_mode_calls_render(self, image_engine):
        image_engine.render_pipeline.reset_mock()
        image_engine.set_layer_blend_mode("Multiply")
        image_engine.render_pipeline.assert_called_once()

    def test_set_opacity_calls_render(self, image_engine):
        image_engine.render_pipeline.reset_mock()
        image_engine.set_layer_opacity(0.5)
        image_engine.render_pipeline.assert_called_once()
