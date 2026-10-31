from __future__ import annotations

import math

from src.features.edit.ui.preview_view_transform import PreviewViewTransform


def test_fit_rect_preserves_aspect_ratio_in_wide_viewport() -> None:
    transform = PreviewViewTransform()

    x, y, w, h = transform.display_rect_px(
        texture_w=1920,
        texture_h=1080,
        viewport_w=1000,
        viewport_h=1000,
    )

    assert math.isclose(w, 1000.0, abs_tol=1e-9)
    assert math.isclose(h, 562.5, abs_tol=1e-9)
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, 218.75, abs_tol=1e-9)


def test_zoom_at_keeps_anchor_on_same_image_point() -> None:
    transform = PreviewViewTransform()

    before = transform.display_rect_px(1920, 1080, 1000, 1000)
    anchor_x = 750.0
    anchor_y = 500.0
    rel_x = (anchor_x - before[0]) / before[2]
    rel_y = (anchor_y - before[1]) / before[3]

    transform.zoom_at(
        factor=2.0,
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        viewport_w=1000,
        viewport_h=1000,
        texture_w=1920,
        texture_h=1080,
    )

    after = transform.display_rect_px(1920, 1080, 1000, 1000)
    assert transform.mode == "manual"
    assert math.isclose((anchor_x - after[0]) / after[2], rel_x)
    assert math.isclose((anchor_y - after[1]) / after[3], rel_y)


def test_pan_by_switches_to_manual_mode() -> None:
    transform = PreviewViewTransform()

    transform.pan_by(120.0, -40.0)

    assert transform.mode == "manual"
    assert transform.pan_x_px == 120.0
    assert transform.pan_y_px == -40.0


def test_actual_size_uses_texture_pixels_relative_to_fit_size() -> None:
    transform = PreviewViewTransform()

    transform.set_actual_size(
        texture_w=1920,
        texture_h=1080,
        viewport_w=960,
        viewport_h=540,
    )
    x, y, w, h = transform.display_rect_px(1920, 1080, 960, 540)

    assert transform.mode == "actual"
    assert math.isclose(transform.zoom, 2.0)
    assert math.isclose(w, 1920.0)
    assert math.isclose(h, 1080.0)
    assert math.isclose(x, -480.0)
    assert math.isclose(y, -270.0)
