"""Tests for ImageEngine image transform methods (resize, rotate, flip, crop)."""

import pytest
import numpy as np
from unittest.mock import patch, MagicMock


def _setup_image(engine, img):
    """Set up engine with an image, bypassing GPU upload."""
    engine.img_full_original = img.copy()
    engine.img_source_full = img.copy()
    # Neutralize set_image to avoid GPU upload
    engine.set_image = MagicMock()


class TestResizeImage:
    @patch('src.shared.io.file_io.FileIO')
    def test_resize_calls_set_image_with_resized(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.resize_image(50, 50)
        # set_image should be called with the resized image
        image_engine.set_image.assert_called_once()
        args = image_engine.set_image.call_args[0]
        assert args[0].shape[:2] == (50, 50)

    @patch('src.shared.io.file_io.FileIO')
    def test_resize_pushes_history(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.resize_image(50, 50)
        assert image_engine.can_undo() is True

    def test_resize_none_image_no_crash(self, image_engine):
        image_engine.img_full_original = None
        image_engine.resize_image(50, 50)  # should not crash

    @patch('src.shared.io.file_io.FileIO')
    def test_resize_preserves_channels(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.resize_image(50, 50)
        args = image_engine.set_image.call_args[0]
        assert args[0].shape[2] == 3


class TestRotateImage:
    @patch('src.shared.io.file_io.FileIO')
    def test_rotate_clockwise(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)
        original_shape = sample_image_bgr.shape[:2]

        image_engine.rotate_image(clockwise=True)
        args = image_engine.set_image.call_args[0]
        assert args[0].shape[:2] == (original_shape[1], original_shape[0])

    @patch('src.shared.io.file_io.FileIO')
    def test_rotate_counterclockwise(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)
        original_shape = sample_image_bgr.shape[:2]

        image_engine.rotate_image(clockwise=False)
        args = image_engine.set_image.call_args[0]
        assert args[0].shape[:2] == (original_shape[1], original_shape[0])

    @patch('src.shared.io.file_io.FileIO')
    def test_rotate_pushes_history(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.rotate_image()
        assert image_engine.can_undo() is True

    def test_rotate_none_image_no_crash(self, image_engine):
        image_engine.img_full_original = None
        image_engine.rotate_image()


class TestFlipImage:
    @patch('src.shared.io.file_io.FileIO')
    def test_flip_horizontal(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()
        image_engine.flip_image(horizontal=True)
        args = image_engine.set_image.call_args[0]
        flipped = args[0]
        # Horizontal flip: left-right mirror
        np.testing.assert_array_equal(flipped[0, -1], original[0, 0])

    @patch('src.shared.io.file_io.FileIO')
    def test_flip_vertical(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        original = sample_image_bgr.copy()
        image_engine.flip_image(horizontal=False)
        args = image_engine.set_image.call_args[0]
        flipped = args[0]
        # Vertical flip: top-bottom mirror
        np.testing.assert_array_equal(flipped[-1, 0], original[0, 0])

    @patch('src.shared.io.file_io.FileIO')
    def test_flip_pushes_history(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        _setup_image(image_engine, sample_image_bgr)

        image_engine.flip_image()
        assert image_engine.can_undo() is True

    def test_flip_none_image_no_crash(self, image_engine):
        image_engine.img_full_original = None
        image_engine.flip_image()


class TestApplyCrop:
    @patch('src.shared.io.file_io.FileIO')
    def test_basic_crop(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        params = {
            'center': (0.5, 0.5),
            'size': (0.5, 0.5),
            'rotation': 0.0,
        }
        image_engine.apply_crop(params)
        # apply_crop DOES update img_full_original
        assert image_engine.img_full_original.shape[:2] == (50, 50)

    @patch('src.shared.io.file_io.FileIO')
    def test_crop_pushes_history(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        params = {
            'center': (0.5, 0.5),
            'size': (0.5, 0.5),
            'rotation': 0.0,
        }
        image_engine.apply_crop(params)
        assert image_engine.can_undo() is True

    def test_crop_none_source_no_crash(self, image_engine):
        image_engine.img_source_full = None
        image_engine.apply_crop({'center': (0.5, 0.5), 'size': (0.5, 0.5)})

    @patch('src.shared.io.file_io.FileIO')
    def test_crop_stores_params(self, MockFileIO, image_engine, sample_image_bgr):
        MockFileIO._resize_to_fit.side_effect = lambda img, size: img
        MockFileIO.update_cache = MagicMock()
        _setup_image(image_engine, sample_image_bgr)

        params = {
            'center': (0.25, 0.75),
            'size': (0.3, 0.4),
            'rotation': 15.0,
        }
        image_engine.apply_crop(params)
        assert image_engine.crop_params == params
