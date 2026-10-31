"""Tests for ImageEngine undo/redo flow."""

import pytest
import numpy as np
from unittest.mock import patch, MagicMock


def _setup_image(engine, img):
    """Set up engine with an image, bypassing GPU upload."""
    engine.img_full_original = img.copy()
    engine.img_source_full = img.copy()
    engine.set_image = MagicMock()


class TestUndoRedoBasic:
    def test_undo_returns_false_when_no_history(self, image_engine):
        result = image_engine.undo()
        assert result is False

    def test_redo_returns_false_when_no_history(self, image_engine):
        result = image_engine.redo()
        assert result is False


class TestUndoRedoWithTransforms:
    @patch('src.shared.io.file_io.FileIO')
    def test_undo_after_crop(self, MockFileIO, image_engine, sample_image_bgr):
        """Undo after crop should restore img_full_original."""
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()
        image_engine.apply_crop({
            'center': (0.5, 0.5), 'size': (0.5, 0.5), 'rotation': 0.0
        })
        assert image_engine.img_full_original.shape[:2] == (50, 50)

        result = image_engine.undo()
        assert result is True
        np.testing.assert_array_equal(image_engine.img_full_original, original)

    @patch('src.shared.io.file_io.FileIO')
    def test_redo_after_undo_crop(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        image_engine.apply_crop({
            'center': (0.5, 0.5), 'size': (0.5, 0.5), 'rotation': 0.0
        })
        image_engine.undo()
        result = image_engine.redo()
        assert result is True
        assert image_engine.img_full_original.shape[:2] == (50, 50)

    @patch('src.shared.io.file_io.FileIO')
    def test_undo_after_flip_restores_image(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()
        image_engine.flip_image(horizontal=True)
        image_engine.undo()
        np.testing.assert_array_equal(image_engine.img_full_original, original)

    @patch('src.shared.io.file_io.FileIO')
    def test_undo_after_rotate_restores_image(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()
        image_engine.rotate_image(clockwise=True)
        image_engine.undo()
        np.testing.assert_array_equal(image_engine.img_full_original, original)


class TestCanUndoRedoFlags:
    @patch('src.shared.io.file_io.FileIO')
    def test_can_undo_after_operation(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        assert image_engine.can_undo() is False
        image_engine.resize_image(50, 50)
        assert image_engine.can_undo() is True

    @patch('src.shared.io.file_io.FileIO')
    def test_can_redo_after_undo(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.resize_image(50, 50)
        image_engine.undo()
        assert image_engine.can_redo() is True

    @patch('src.shared.io.file_io.FileIO')
    def test_push_clears_redo(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.resize_image(50, 50)
        image_engine.resize_image(30, 30)
        image_engine.undo()
        assert image_engine.can_redo() is True

        image_engine.resize_image(40, 40)
        assert image_engine.can_redo() is False


class TestUndoRedoFullCycle:
    @patch('src.shared.io.file_io.FileIO')
    def test_crop_undo_redo_cycle(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()

        # 1. Crop
        image_engine.apply_crop({
            'center': (0.5, 0.5), 'size': (0.5, 0.5), 'rotation': 0.0
        })
        assert image_engine.img_full_original.shape[:2] == (50, 50)

        # 2. Undo -> original
        image_engine.undo()
        np.testing.assert_array_equal(image_engine.img_full_original, original)

        # 3. Redo -> cropped
        image_engine.redo()
        assert image_engine.img_full_original.shape[:2] == (50, 50)

        # 4. Undo again -> original
        image_engine.undo()
        np.testing.assert_array_equal(image_engine.img_full_original, original)

    @patch('src.shared.io.file_io.FileIO')
    def test_multiple_crop_undo(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        image_engine.apply_crop({'center': (0.5, 0.5), 'size': (0.8, 0.8), 'rotation': 0.0})
        image_engine.apply_crop({'center': (0.5, 0.5), 'size': (0.5, 0.5), 'rotation': 0.0})
        image_engine.apply_crop({'center': (0.5, 0.5), 'size': (0.3, 0.3), 'rotation': 0.0})

        # Undo 3 times to get back to original
        image_engine.undo()
        assert image_engine.img_full_original.shape[:2] == (50, 50)

        image_engine.undo()
        assert image_engine.img_full_original.shape[:2] == (80, 80)

        image_engine.undo()
        assert image_engine.img_full_original.shape[:2] == sample_image_bgr.shape[:2]
