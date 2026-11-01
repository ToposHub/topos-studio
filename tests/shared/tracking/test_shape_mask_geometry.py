"""Tracking feature masks cover the same window geometry as preview rendering."""
from __future__ import annotations

import numpy as np
import pytest

from src.shared.tracking.shape_mask import rasterize_shape_mask


def _bounds(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def test_circle_size_is_radius_and_rotates_in_pixel_space():
    geometry = {"type": "circle", "center": [.3, .5], "size": [.2, .1], "rotation": 0}
    mask = rasterize_shape_mask(geometry, 400, 200)
    assert mask is not None
    assert _bounds(mask) == (40, 80, 200, 120)
    assert mask[100, 190] == 255  # well outside the old half-radius ellipse
    rotated = rasterize_shape_mask({**geometry, "rotation": 90}, 400, 200)
    assert rotated is not None
    xmin, ymin, xmax, ymax = _bounds(rotated)
    assert (xmin, ymin, xmax, ymax) == pytest.approx((100, 20, 140, 180), abs=1)
    assert rotated[170, 120] == 255
    assert rotated[100, 190] == 0


def test_curve_rotates_clockwise_about_its_own_off_center_pivot():
    anchors = [[.2, .2], [.3, .2], [.2, .4]]
    geometry = {"type": "curve", "center": [.2, .2], "rotation": 90,
                "points": [{"pos": point, "c1": point, "c2": point} for point in anchors]}
    mask = rasterize_shape_mask(geometry, 400, 200)
    assert mask is not None
    assert _bounds(mask) == pytest.approx((40, 40, 80, 80), abs=1)
    assert mask[50, 70] == 255
    assert mask[50, 100] == 0


@pytest.mark.parametrize("shape_type,rotation", [("circle", 0), ("circle", 37),
                                                ("rect", 31), ("curve", 73)])
def test_tracking_mask_matches_engine_window_extent(qapp, shape_type, rotation):
    from src.features.color.core.image_engine import ImageEngine
    from src.features.color.core.layer import MaskShape
    from src.features.color.tracking.tracking_evaluator import capture_base_geometry

    shape = MaskShape(shape_type)
    shape.center = [.34, .53]
    shape.size = [.18, .14] if shape_type == "circle" else [.28, .3]
    shape.rotation = rotation
    shape.feather = 0
    if shape_type == "curve":
        anchors = [[.2, .25], [.48, .2], [.41, .65], [.19, .55]]
        shape.points = [{"pos": point[:], "c1": point[:], "c2": point[:]} for point in anchors]
    width, height = 640, 320
    tracking = rasterize_shape_mask(capture_base_geometry(shape), width, height)
    engine = ImageEngine.__new__(ImageEngine)
    preview = ImageEngine._render_single_shape(engine, shape, width, height, width)
    assert tracking is not None
    actual, expected = tracking > 0, preview > .5
    # PIL and antialiased QPainter may disagree by one boundary pixel.
    assert _bounds(actual) == pytest.approx(_bounds(expected), abs=2)
    assert np.count_nonzero(actual & expected) / np.count_nonzero(actual | expected) > .97


def test_unsupported_and_too_small_geometry_keep_fallback_contract():
    assert rasterize_shape_mask({"type": "gradient"}, 640, 320) is None
    assert rasterize_shape_mask({"type": "circle", "size": [.001, .001]}, 640, 320) is None
    assert rasterize_shape_mask({"type": "curve", "points": []}, 640, 320) is None
