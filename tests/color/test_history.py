"""Tests for HistoryManager class (now at src/features/color/core/history.py)"""

import pytest
import numpy as np
from src.features.color.core.history import HistoryManager
from src.features.color.core.layer import Layer


class TestHistoryPush:
    def test_push_state_increases_undo_stack(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        assert len(history_manager.undo_stack) == 1

    def test_push_state_clears_redo_stack(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        # Simulate undo to populate redo stack
        history_manager.undo(sample_image_bgr, [layer], 0)
        assert len(history_manager.redo_stack) == 1
        # Push again should clear redo
        history_manager.push_state(sample_image_bgr, [layer], 0)
        assert len(history_manager.redo_stack) == 0

    def test_push_none_image_is_noop(self, history_manager):
        history_manager.push_state(None, [], 0)
        assert len(history_manager.undo_stack) == 0

    def test_non_destructive_snapshot_keeps_source_reference(
        self, history_manager, sample_image_bgr
    ):
        """Grading-only undo snapshots avoid a large GUI-thread frame copy."""
        history_manager.push_state(sample_image_bgr, [], 0, copy_image=False)
        assert history_manager.undo_stack[-1]['image'] is sample_image_bgr


class TestHistoryUndoRedo:
    def test_undo_returns_previous_state(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)

        # Modify image
        modified = sample_image_bgr.copy()
        modified[:] = 128
        history_manager.push_state(modified, [layer], 0)

        state = history_manager.undo(modified, [layer], 0)
        assert state is not None
        assert state['active_layer_index'] == 0

    def test_undo_empty_stack_returns_none(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        result = history_manager.undo(sample_image_bgr, [layer], 0)
        assert result is None

    def test_redo_returns_next_state(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        modified = sample_image_bgr.copy()
        modified[:] = 128
        history_manager.push_state(modified, [layer], 0)

        # Undo then redo
        state1 = history_manager.undo(modified, [layer], 0)
        state2 = history_manager.redo(state1['image'], [], 0)
        assert state2 is not None

    def test_redo_empty_stack_returns_none(self, history_manager, sample_image_bgr):
        result = history_manager.redo(sample_image_bgr, [], 0)
        assert result is None


class TestHistoryFlags:
    def test_can_undo(self, history_manager, sample_image_bgr):
        assert history_manager.can_undo() is False
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        assert history_manager.can_undo() is True

    def test_can_redo_after_undo(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        history_manager.undo(sample_image_bgr, [layer], 0)
        assert history_manager.can_redo() is True

    def test_can_redo_false_initially(self, history_manager):
        assert history_manager.can_redo() is False


class TestHistoryMaxSteps:
    def test_max_steps_limit(self):
        manager = HistoryManager(max_steps=3)
        img = np.zeros((10, 10, 3), dtype=np.uint8)
        layer = Layer("L")
        for i in range(5):
            manager.push_state(img, [layer], 0)
        assert len(manager.undo_stack) <= 3


class TestHistoryData:
    def test_undo_preserves_image_data(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)

        modified = np.ones_like(sample_image_bgr) * 42
        state = history_manager.undo(modified, [layer], 0)
        # The popped state should be the original
        np.testing.assert_array_equal(state['image'], sample_image_bgr)

    def test_undo_preserves_layer_index(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 2)
        state = history_manager.undo(sample_image_bgr, [layer], 0)
        assert state['active_layer_index'] == 2

    def test_clear(self, history_manager, sample_image_bgr):
        layer = Layer("Layer 1")
        history_manager.push_state(sample_image_bgr, [layer], 0)
        history_manager.undo(sample_image_bgr, [layer], 0)
        history_manager.clear()
        assert len(history_manager.undo_stack) == 0
        assert len(history_manager.redo_stack) == 0


class TestHistoryLayerSerialization:
    def test_deserialize_layers(self, sample_image_bgr):
        layer = Layer("Test")
        layer.grading_params["exposure"] = 1.5
        layer.blend_mode = "Multiply"

        manager = HistoryManager()
        manager.push_state(sample_image_bgr, [layer], 0)
        state = manager.undo(sample_image_bgr, [], 0)

        restored_layers = HistoryManager.deserialize_layers(state['layers'])
        assert len(restored_layers) == 1
        assert restored_layers[0].name == "Test"
        assert restored_layers[0].grading_params["exposure"] == 1.5
        assert restored_layers[0].blend_mode == "Multiply"
