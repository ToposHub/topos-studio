"""Tests for ImageEngine layer operation methods."""

import pytest


class TestAddLayer:
    def test_adds_layer(self, image_engine):
        assert len(image_engine.layers) == 1
        new = image_engine.add_layer()
        assert len(image_engine.layers) == 2

    def test_returns_new_layer(self, image_engine):
        new = image_engine.add_layer()
        assert new is image_engine.layers[-1]

    def test_updates_active_index(self, image_engine):
        image_engine.add_layer()
        assert image_engine.active_layer_index == 1

    def test_name_increments(self, image_engine):
        image_engine.add_layer()
        assert image_engine.layers[1].name == "Layer 2"
        image_engine.add_layer()
        assert image_engine.layers[2].name == "Layer 3"

    def test_calls_render_pipeline(self, image_engine):
        image_engine.add_layer()
        image_engine.render_pipeline.assert_called()


class TestRemoveLayer:
    def test_removes_layer(self, image_engine):
        image_engine.add_layer()
        assert len(image_engine.layers) == 2
        image_engine.remove_layer(1)
        assert len(image_engine.layers) == 1

    def test_cannot_remove_last_layer(self, image_engine):
        # Only 1 layer, cannot remove
        initial_count = len(image_engine.layers)
        image_engine.remove_layer(0)
        assert len(image_engine.layers) == initial_count

    def test_adjusts_active_index(self, image_engine):
        image_engine.add_layer()
        image_engine.add_layer()
        image_engine.active_layer_index = 2
        image_engine.remove_layer(2)
        # active_layer_index should be clamped
        assert image_engine.active_layer_index <= len(image_engine.layers) - 1


class TestSetActiveLayer:
    def test_valid_index(self, image_engine):
        image_engine.add_layer()
        image_engine.set_active_layer(1)
        assert image_engine.active_layer_index == 1

    def test_invalid_negative(self, image_engine):
        image_engine.set_active_layer(-1)
        assert image_engine.active_layer_index == 0

    def test_invalid_too_large(self, image_engine):
        image_engine.set_active_layer(999)
        assert image_engine.active_layer_index == 0

    def test_zero_is_valid(self, image_engine):
        image_engine.add_layer()
        image_engine.set_active_layer(1)
        image_engine.set_active_layer(0)
        assert image_engine.active_layer_index == 0


class TestGetActiveLayer:
    def test_returns_first_layer_by_default(self, image_engine):
        layer = image_engine.get_active_layer()
        assert layer is image_engine.layers[0]

    def test_returns_correct_layer(self, image_engine):
        image_engine.add_layer()
        image_engine.set_active_layer(1)
        layer = image_engine.get_active_layer()
        assert layer is image_engine.layers[1]

    def test_returns_none_for_invalid_index(self, image_engine):
        image_engine.active_layer_index = 999
        assert image_engine.get_active_layer() is None


class TestDuplicateLayer:
    def test_creates_copy(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 2.0
        dup = image_engine.duplicate_layer(0)
        assert dup is not None
        assert len(image_engine.layers) == 2
        assert dup.grading_params["exposure"] == 2.0

    def test_copy_suffix_in_name(self, image_engine):
        image_engine.layers[0].name = "Color Grade"
        dup = image_engine.duplicate_layer(0)
        assert dup.name == "Color Grade Copy"

    def test_different_id(self, image_engine):
        dup = image_engine.duplicate_layer(0)
        assert dup.id != image_engine.layers[0].id

    def test_invalid_index_returns_none(self, image_engine):
        result = image_engine.duplicate_layer(999)
        assert result is None

    def test_copies_visibility(self, image_engine):
        image_engine.layers[0].visible = False
        dup = image_engine.duplicate_layer(0)
        assert dup.visible is False

    def test_copies_blend_mode(self, image_engine):
        image_engine.layers[0].blend_mode = "Multiply"
        dup = image_engine.duplicate_layer(0)
        assert dup.blend_mode == "Multiply"

    def test_updates_active_index(self, image_engine):
        image_engine.duplicate_layer(0)
        assert image_engine.active_layer_index == 1


class TestCopyPasteLayer:
    def test_copy_sets_clipboard(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 1.5
        image_engine.copy_layer(0)
        assert image_engine._clipboard_layer is not None
        assert image_engine._clipboard_layer.grading_params["exposure"] == 1.5

    def test_copy_preserves_source(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 2.0
        image_engine.copy_layer(0)
        assert image_engine.layers[0].grading_params["exposure"] == 2.0

    def test_paste_applies_to_target(self, image_engine):
        image_engine.layers[0].grading_params["exposure"] = 2.0
        image_engine.add_layer()
        image_engine.copy_layer(0)
        image_engine.paste_layer(1)
        assert image_engine.layers[1].grading_params["exposure"] == 2.0

    def test_paste_empty_clipboard_returns_false(self, image_engine):
        result = image_engine.paste_layer(0)
        assert result is False

    def test_paste_invalid_index_returns_false(self, image_engine):
        image_engine.copy_layer(0)
        result = image_engine.paste_layer(999)
        assert result is False

    def test_copy_invalid_index_does_nothing(self, image_engine):
        image_engine.copy_layer(999)
        assert image_engine._clipboard_layer is None

    def test_paste_copies_blend_mode(self, image_engine):
        image_engine.layers[0].blend_mode = "Screen"
        image_engine.layers[0].opacity = 0.7
        image_engine.add_layer()
        image_engine.copy_layer(0)
        image_engine.paste_layer(1)
        assert image_engine.layers[1].blend_mode == "Screen"
        assert image_engine.layers[1].opacity == 0.7


class TestSetLayerBlendMode:
    def test_sets_blend_mode(self, image_engine):
        image_engine.set_layer_blend_mode("Multiply")
        assert image_engine.layers[0].blend_mode == "Multiply"

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.set_layer_blend_mode("Screen")  # should not crash


class TestSetLayerOpacity:
    def test_sets_opacity(self, image_engine):
        image_engine.set_layer_opacity(0.5)
        assert image_engine.layers[0].opacity == 0.5

    def test_no_active_layer_no_crash(self, image_engine):
        image_engine.active_layer_index = 999
        image_engine.set_layer_opacity(0.5)  # should not crash
