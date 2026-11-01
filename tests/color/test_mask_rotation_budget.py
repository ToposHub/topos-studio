"""Rotation keeps the existing crop semantics without oversized output warps."""
from __future__ import annotations

from unittest.mock import patch

import cv2
import numpy as np
import pytest

from src.features.color.core.layer import MaskShape


@pytest.mark.parametrize('kind,size', [
    ('circle', [.15, .267]), ('circle', [.8, .9]),
    ('rect', [.4, .7]), ('rect', [1.4, 1.6]),
])
@pytest.mark.parametrize('angle,center', [(31.0, [.5, .5]), (129.0, [.08, .82])])
def test_rotation_matches_padded_reference(image_engine, kind, size, angle, center):
    width, height = 256, 144
    shape = MaskShape(kind)
    shape.size, shape.rotation, shape.center = size, angle, center
    shape.feather = 0.0
    pad = max(width, height) * 3 // 4 + 4
    source = image_engine._generate_shape_texture(shape, width, height, 1920, pad=pad)
    matrix = cv2.getRotationMatrix2D((width * .5 + pad, height * .5 + pad), -angle, 1.0)
    rotated = cv2.warpAffine(source, matrix, (width + 2 * pad, height + 2 * pad))
    expected = rotated[pad:pad + height, pad:pad + width]
    dx, dy = (center[0] - .5) * width, (center[1] - .5) * height
    if abs(dx) > .5 or abs(dy) > .5:
        expected = cv2.warpAffine(expected, np.float32([[1, 0, dx], [0, 1, dy]]), (width, height))
    actual = image_engine._render_single_shape(shape, width, height, 1920)
    np.testing.assert_allclose(actual, expected, atol=1.0 / 255.0)


def test_rotating_tracking_window_never_warps_discarded_pixels(image_engine):
    shape = MaskShape('circle')
    shape.size = [.15, .267]
    shape.rotation = 27.0
    shape.feather = 4.13
    with patch('src.features.color.core.image_engine.cv2.warpAffine', wraps=cv2.warpAffine) as warp:
        result = image_engine._render_single_shape(shape, 1024, 576, 1920)
    assert result.shape == (576, 1024)
    assert warp.call_count == 1
    assert warp.call_args.args[0].shape == (576, 1024)
    assert warp.call_args.args[2] == (1024, 576)


def test_tracking_view_reuses_held_geometry_without_polluting_source(image_engine):
    shape = MaskShape('circle')
    shape.feather = 4.13
    geometry = {'center': [.6, .5], 'size': [.15, .267], 'rotation': 23.0}
    with patch.object(image_engine, 'get_tracked_geometry', return_value=geometry), patch.object(
        image_engine, '_generate_shape_texture', wraps=image_engine._generate_shape_texture,
    ) as generate:
        first = image_engine._render_single_shape(image_engine._tracked_shape_view(shape), 256, 144, 1920)
        held = image_engine._render_single_shape(image_engine._tracked_shape_view(shape), 256, 144, 1920)
        np.testing.assert_array_equal(first, held)
        assert generate.call_count == 1
        shape.feather = 30.0
        changed = image_engine._render_single_shape(image_engine._tracked_shape_view(shape), 256, 144, 1920)
        assert not np.array_equal(first, changed)
        assert generate.call_count == 2
    assert shape.center == [.5, .5]
    assert shape.rotation == 0.0
    assert shape._render_cache is None
    assert shape._shape_cache is None


def test_tracking_view_cache_does_not_keep_removed_shapes_alive(image_engine):
    import gc
    import weakref
    shape = MaskShape('circle')
    with patch.object(image_engine, 'get_tracked_geometry', return_value={'center': [.6, .5]}):
        image_engine._tracked_shape_view(shape)
    reference = weakref.ref(shape)
    del shape
    gc.collect()
    assert reference() is None
    assert len(image_engine._tracking_shape_views) == 0


def test_live_mask_preview_restores_still_resolution(image_engine):
    from unittest.mock import MagicMock
    image_engine.renderer = MagicMock()
    image_engine.img_full_original = np.zeros((1080, 1920, 3), dtype=np.float32)
    layer = image_engine.get_active_layer()
    layer.mask_shapes = [MaskShape('circle')]
    layer.mask_dirty = True
    image_engine._update_mask_textures(preview=True)
    assert image_engine.renderer.submit.call_args.args[1]['size'] == (512, 288)
    layer.mask_dirty = True
    image_engine._update_mask_textures()
    assert image_engine.renderer.submit.call_args.args[1]['size'] == (1024, 576)
