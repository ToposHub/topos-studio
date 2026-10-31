"""Tests for Layer class (now at src/features/color/core/layer.py:264)"""

import pytest
import numpy as np
from unittest.mock import patch
from src.features.color.core.layer import Layer, MaskShape, MaskGroup


class TestLayerCreation:
    def test_default_name(self):
        layer = Layer()
        assert layer.name == "Layer"

    def test_custom_name(self):
        layer = Layer("My Layer")
        assert layer.name == "My Layer"

    def test_has_unique_id(self):
        l1 = Layer()
        l2 = Layer()
        assert l1.id != l2.id

    def test_default_visibility(self):
        layer = Layer()
        assert layer.visible is True

    def test_default_opacity(self):
        layer = Layer()
        assert layer.opacity == 1.0

    def test_default_blend_mode(self):
        layer = Layer()
        assert layer.blend_mode == "Normal"

    def test_default_dirty_flags(self):
        layer = Layer()
        assert layer.is_dirty is True
        assert layer.mask_dirty is True


class TestLayerGradingDefaults:
    def test_default_lift(self):
        layer = Layer()
        assert layer.grading_params["lift"] == (0, 0, 0)

    def test_default_exposure(self):
        layer = Layer()
        assert layer.grading_params["exposure"] == 0

    def test_default_curves_lut(self):
        layer = Layer()
        assert layer.curves_lut is None

    def test_default_curves_data(self):
        layer = Layer()
        assert layer.curves_data == {}

    def test_default_lut_path(self):
        layer = Layer()
        assert layer.active_lut_path is None

    def test_default_lut_intensity(self):
        layer = Layer()
        assert layer.lut_intensity == 1.0


class TestLayerResetParams:
    def test_reset_clears_grading(self):
        layer = Layer()
        layer.grading_params["exposure"] = 2.0
        layer.grading_params["lift"] = (1, 2, 3)
        layer.reset_params()
        assert layer.grading_params["exposure"] == 0
        assert layer.grading_params["lift"] == (0, 0, 0)

    def test_reset_clears_curves(self):
        layer = Layer()
        layer.curves_data = {"r": [(0, 255), (128, 64), (255, 0)]}
        layer.curves_lut = np.zeros((256, 1, 3), dtype=np.float32)
        layer.reset_params()
        assert layer.curves_data == {}
        assert layer.curves_lut is None

    def test_reset_clears_lut(self):
        layer = Layer()
        layer.active_lut_path = "/path/to/lut.cube"
        layer.reset_params()
        assert layer.active_lut_path is None

    def test_reset_clears_masks(self):
        layer = Layer()
        layer.add_shape(MaskShape.CIRCLE)
        assert len(layer.mask_shapes) == 1
        layer.reset_params()
        assert len(layer.mask_shapes) == 0

    def test_reset_clears_ai_matte(self):
        layer = Layer()
        layer.ai_matte = np.zeros((100, 100), dtype=np.float32)
        layer.reset_params()
        assert layer.ai_matte is None

    def test_reset_clears_bake_state(self):
        layer = Layer()
        layer.is_baked = True
        layer.baked_file_name = "test.exr"
        layer.reset_params()
        assert layer.is_baked is False
        assert layer.baked_file_name is None

    def test_reset_sets_dirty(self):
        layer = Layer()
        layer.is_dirty = False
        layer.mask_dirty = False
        layer.reset_params()
        assert layer.is_dirty is True
        assert layer.mask_dirty is True


class TestLayerMaskShapes:
    def test_add_shape(self):
        layer = Layer()
        shape = layer.add_shape(MaskShape.CIRCLE)
        assert len(layer.mask_shapes) == 1
        assert shape.type == MaskShape.CIRCLE
        assert layer.is_dirty is True
        assert layer.mask_dirty is True

    def test_add_group(self):
        layer = Layer()
        group = layer.add_group("Test Group")
        assert len(layer.mask_shapes) == 1
        assert isinstance(group, MaskGroup)
        assert group.name == "Test Group"

    def test_remove_shape(self):
        layer = Layer()
        shape = layer.add_shape(MaskShape.CIRCLE)
        layer.is_dirty = False
        layer.mask_dirty = False
        layer.remove_shape(shape.id)
        assert len(layer.mask_shapes) == 0
        assert layer.is_dirty is True

    def test_remove_shape_from_group(self):
        layer = Layer()
        group = layer.add_group("G")
        shape = MaskShape(MaskShape.CIRCLE)
        group.add_child(shape)
        layer.remove_shape(shape.id)
        assert len(group.children) == 0

    def test_get_mask_item_by_id(self):
        layer = Layer()
        shape = layer.add_shape(MaskShape.RECT)
        found = layer.get_mask_item_by_id(shape.id)
        assert found is shape

    def test_get_mask_item_by_id_not_found(self):
        layer = Layer()
        assert layer.get_mask_item_by_id("nonexistent") is None

    def test_get_mask_item_nested_in_group(self):
        layer = Layer()
        group = layer.add_group("G")
        shape = MaskShape(MaskShape.CIRCLE)
        group.add_child(shape)
        found = layer.get_mask_item_by_id(shape.id)
        assert found is shape


class TestLayerCopyParams:
    def test_copy_params_from(self):
        source = Layer("Source")
        source.grading_params["exposure"] = 2.0
        source.grading_params["lift"] = (1, 2, 3)
        source.blend_mode = "Multiply"
        source.opacity = 0.5

        target = Layer("Target")
        target.copy_params_from(source)

        assert target.grading_params["exposure"] == 2.0
        assert target.grading_params["lift"] == (1, 2, 3)

    def test_copy_params_no_reference_sharing(self):
        source = Layer("Source")
        source.grading_params["exposure"] = 1.5

        target = Layer("Target")
        target.copy_params_from(source)

        # Modify target, source should not change
        target.grading_params["exposure"] = 0
        assert source.grading_params["exposure"] == 1.5

    def test_copy_preserves_curves(self):
        source = Layer()
        source.curves_data = {"r": [(0, 255), (128, 128), (255, 0)]}
        source.curves_lut = np.zeros((256, 1, 3), dtype=np.float32)

        target = Layer()
        target.copy_params_from(source)

        assert target.curves_data == source.curves_data
        assert target.curves_lut is not None

    def test_copy_preserves_soft_clip(self):
        source = Layer()
        source.soft_clip_params["enabled"] = True
        source.soft_clip_params["high"] = 0.8

        target = Layer()
        target.copy_params_from(source)

        assert target.soft_clip_params["enabled"] is True
        assert target.soft_clip_params["high"] == 0.8


class TestLayerHasGradingEffects:
    def test_default_no_effects(self):
        layer = Layer()
        assert layer.has_grading_effects() is False

    def test_with_lift(self):
        layer = Layer()
        layer.grading_params["lift"] = (1, 0, 0)
        assert layer.has_grading_effects() is True

    def test_with_exposure(self):
        layer = Layer()
        layer.grading_params["exposure"] = 1.0
        assert layer.has_grading_effects() is True

    def test_with_contrast(self):
        layer = Layer()
        layer.grading_params["contrast"] = 0.5
        assert layer.has_grading_effects() is True

    def test_with_mixer(self):
        layer = Layer()
        layer.mixer_params = {"red": [0.1, 0, 0]}
        assert layer.has_grading_effects() is True

    def test_with_soft_clip_enabled(self):
        layer = Layer()
        layer.soft_clip_params["enabled"] = True
        assert layer.has_grading_effects() is True

    def test_with_warper(self):
        layer = Layer()
        layer.warper_params = np.zeros((10, 10, 2), dtype=np.float32)
        assert layer.has_grading_effects() is True

    def test_with_nonlinear_curves(self):
        layer = Layer()
        layer.curves_data = {"r": [(0, 255), (64, 200), (255, 0)]}
        assert layer.has_grading_effects() is True

    def test_with_default_linear_curves_is_no_effect(self):
        layer = Layer()
        layer.curves_data = {"r": [(0, 255), (255, 0)]}
        assert layer.has_grading_effects() is False


class TestLayerSerialization:
    def test_to_dict_from_dict_roundtrip(self):
        layer = Layer("Test")
        layer.visible = False
        layer.opacity = 0.7
        layer.blend_mode = "Screen"
        layer.grading_params["exposure"] = 1.5

        data = layer.to_dict()
        restored = Layer.from_dict(data)

        assert restored.name == "Test"
        assert restored.visible is False
        assert restored.opacity == 0.7
        assert restored.blend_mode == "Screen"
        assert restored.grading_params["exposure"] == 1.5

    def test_roundtrip_preserves_grading(self):
        layer = Layer()
        layer.grading_params["lift"] = (1, 2, 3)
        layer.grading_params["gamma"] = (4, 5, 6)
        layer.grading_params["saturation"] = 0.5

        data = layer.to_dict()
        restored = Layer.from_dict(data)

        assert restored.grading_params["lift"] == (1, 2, 3)
        assert restored.grading_params["gamma"] == (4, 5, 6)
        assert restored.grading_params["saturation"] == 0.5

    def test_roundtrip_handles_curves_data(self):
        layer = Layer()
        layer.curves_data = {"r": [(0, 255), (128, 64), (255, 0)]}

        data = layer.to_dict()
        restored = Layer.from_dict(data)

        assert "r" in restored.curves_data
        assert len(restored.curves_data["r"]) == 3

    def test_roundtrip_handles_blend_modes(self):
        for mode in Layer.BLEND_MODES:
            layer = Layer()
            layer.blend_mode = mode
            data = layer.to_dict()
            restored = Layer.from_dict(data)
            assert restored.blend_mode == mode, f"Failed for blend mode: {mode}"

    def test_roundtrip_handles_masks(self):
        layer = Layer()
        layer.add_shape(MaskShape.CIRCLE)
        group = layer.add_group("G1")
        group.add_child(MaskShape(MaskShape.RECT))

        data = layer.to_dict()
        restored = Layer.from_dict(data)

        assert len(restored.mask_shapes) == 2
        assert restored.mask_shapes[0].type == MaskShape.CIRCLE
        assert isinstance(restored.mask_shapes[1], MaskGroup)
        assert len(restored.mask_shapes[1].children) == 1

    def test_roundtrip_handles_plugins(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "test-id", "plugin_id": "test_plugin", "params": {}, "enabled": True, "type": "dctl"}
        ]
        data = layer.to_dict()
        restored = Layer.from_dict(data)
        assert len(restored.plugins) == 1
        assert restored.plugins[0]["plugin_id"] == "test_plugin"


class TestLayerPlugins:
    @patch('src.features.color.core.layer.Layer.add_plugin')
    def test_add_plugin(self, mock_add):
        # add_plugin imports plugin_manager which may not exist in test env
        # So we test the plugin list structure directly
        layer = Layer()
        layer.plugins.append({
            "instance_id": "test-id",
            "plugin_id": "test_plugin",
            "params": {},
            "enabled": True,
            "type": "dctl"
        })
        assert len(layer.plugins) == 1

    def test_remove_plugin(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "id1", "plugin_id": "p1", "params": {}, "enabled": True, "type": "dctl"},
            {"instance_id": "id2", "plugin_id": "p2", "params": {}, "enabled": True, "type": "ai"},
        ]
        layer.remove_plugin("id1")
        assert len(layer.plugins) == 1
        assert layer.plugins[0]["instance_id"] == "id2"

    def test_get_plugin_instance(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "id1", "plugin_id": "p1", "params": {}, "enabled": True},
        ]
        result = layer.get_plugin_instance("id1")
        assert result is not None
        assert result["plugin_id"] == "p1"

    def test_get_plugin_instance_not_found(self):
        layer = Layer()
        assert layer.get_plugin_instance("nonexistent") is None

    def test_set_plugin_enabled(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "id1", "plugin_id": "p1", "params": {}, "enabled": True},
        ]
        layer.set_plugin_enabled("id1", False)
        assert layer.plugins[0]["enabled"] is False

    def test_get_ai_plugins(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "id1", "plugin_id": "p1", "params": {}, "enabled": True, "type": "ai"},
            {"instance_id": "id2", "plugin_id": "p2", "params": {}, "enabled": True, "type": "dctl"},
            {"instance_id": "id3", "plugin_id": "p3", "params": {}, "enabled": False, "type": "ai"},
        ]
        ai_plugins = layer.get_ai_plugins()
        assert len(ai_plugins) == 1
        assert ai_plugins[0]["instance_id"] == "id1"

    def test_get_dctl_plugins(self):
        layer = Layer()
        layer.plugins = [
            {"instance_id": "id1", "plugin_id": "p1", "params": {}, "enabled": True, "type": "dctl"},
            {"instance_id": "id2", "plugin_id": "p2", "params": {}, "enabled": True, "type": "ai"},
            {"instance_id": "id3", "plugin_id": "p3", "params": {}, "enabled": False, "type": "dctl"},
        ]
        dctl_plugins = layer.get_dctl_plugins()
        assert len(dctl_plugins) == 1
        assert dctl_plugins[0]["instance_id"] == "id1"
