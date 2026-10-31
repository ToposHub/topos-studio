"""Regression tests for Transform evaluator shader matrix math."""

import pytest

from src.features.composite.evaluators.transform_evaluator import (
    _build_transform_pixel_matrix,
    _pixel_matrix_to_uv_matrix,
    _build_transform_uv_matrix,
)


pytestmark = pytest.mark.composite_core


def _pixel_to_uv(px: float, py: float, width: int, height: int) -> tuple[float, float]:
    """Map image pixel coordinates (top-left, Y-down) to shader UV."""
    return (px / float(width), 1.0 - (py / float(height)))


def _apply_uv_matrix(
    matrix: tuple[float, float, float, float, float, float],
    uv: tuple[float, float],
) -> tuple[float, float]:
    """Apply shader matrix layout to a UV point."""
    m00, m10, m01, m11, m02, m12 = matrix
    u, v = uv
    return (
        m00 * u + m01 * v + m02,
        m10 * u + m11 * v + m12,
    )


def test_positive_translate_y_maps_to_downward_uv_motion() -> None:
    """A positive Transform translate_y must move the image down on screen."""
    matrix = _build_transform_uv_matrix(
        tx_px=0.0,
        ty_px=25.0,
        sx=1.0,
        sy=1.0,
        rotation=0.0,
        piv_px=50.0,
        piv_py=50.0,
        width=100,
        height=100,
    )

    center_uv = _pixel_to_uv(50.0, 50.0, 100, 100)
    transformed_uv = _apply_uv_matrix(matrix, center_uv)
    expected_uv = _pixel_to_uv(50.0, 75.0, 100, 100)
    assert transformed_uv == pytest.approx(expected_uv)


def test_non_square_rotation_preserves_pixel_geometry() -> None:
    """Rotation must happen in pixel space, not normalized square UV space."""
    matrix = _build_transform_uv_matrix(
        tx_px=0.0,
        ty_px=0.0,
        sx=1.0,
        sy=1.0,
        rotation=90.0,
        piv_px=100.0,
        piv_py=50.0,
        width=200,
        height=100,
    )

    top_center_uv = _pixel_to_uv(100.0, 0.0, 200, 100)
    transformed_uv = _apply_uv_matrix(matrix, top_center_uv)
    right_center_uv = _pixel_to_uv(150.0, 50.0, 200, 100)
    assert transformed_uv == pytest.approx(right_center_uv, abs=1e-6)


def test_packed_transform_bbox_uses_source_and_output_dimensions() -> None:
    """A content-sized Transform FBO must contain the whole scaled source."""
    source_w, source_h = 1500, 800
    output_w, output_h = 1126, 600
    pixel_matrix = list(_build_transform_pixel_matrix(
        0.0, 0.0, 0.75, 0.75, 0.0, source_w / 2.0, source_h / 2.0,
    ))
    # Rebase world bbox x=[187, 1313], y-up=[100, 700] into packed FBO space.
    pixel_matrix[4] -= 187.0
    pixel_matrix[5] += 700.0 - source_h
    matrix = _pixel_matrix_to_uv_matrix(
        tuple(pixel_matrix), source_w, source_h, output_w, output_h,
    )

    top_left = _apply_uv_matrix(matrix, _pixel_to_uv(0.0, 0.0, source_w, source_h))
    bottom_right = _apply_uv_matrix(
        matrix, _pixel_to_uv(float(source_w), float(source_h), source_w, source_h),
    )
    assert top_left == pytest.approx((0.5 / output_w, 1.0))
    assert bottom_right == pytest.approx((1125.5 / output_w, 0.0))
