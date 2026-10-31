"""Clip transform NDC matrix parity with composite's TransformNode ``to_ndc``.

The edit page renders clip transforms via a vertex shader that applies a
column-major mat3 ``u_transform`` (built by ``_transform_to_ndc_mat3_bytes``)
to a fullscreen NDC quad. The composite page instead maps each image-pixel
point through ``compute_transform_handles``'s ``to_ndc`` (Y-down pixels →
NDC, with a final Y flip) and draws the result.

Both ultimately answer the same question: *"where does a given image-pixel
point land on screen (in NDC) after the transform?"* — so the edit page's
vertex matrix MUST agree with composite's ``to_ndc`` for every point. If they
disagree on a point's NDC Y, the on-canvas handle (drawn via composite math)
and the rendered image (deformed via the edit vertex matrix) drift apart,
which is exactly the "上下移动相反" (Y movement inverted / mismatched) symptom.

These tests build the edit mat3, transform a set of image points through it,
and compare against the composite ``to_ndc`` reference computed inline.
"""
from __future__ import annotations

import math
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Matrix construction only needs module-level functions; no GL context required,
# but importing the edit renderer pulls in PyQt6, so run headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.edit_gpu_preview_renderer import (  # noqa: E402
    _transform_to_ndc_mat3_bytes,
)


def _edit_mat3(
    transform_tuple: tuple[float, float, float, float, float, str, float, float],
    tex_w: int,
    tex_h: int,
    track_fbo_y_flipped: bool = False,
) -> tuple[float, ...]:
    """Return the 9 column-major floats of the edit vertex u_transform mat3."""
    raw = _transform_to_ndc_mat3_bytes(
        transform_tuple, tex_w, tex_h,
        track_fbo_y_flipped=track_fbo_y_flipped,
    )
    return struct.unpack("9f", raw)


def _edit_transform_point(img_px_x, img_px_y, transform_tuple, tex_w, tex_h,
                          ndc_rect=(-1.0, -1.0, 1.0, 1.0)):
    """Apply the edit vertex matrix to an image-pixel point.

    Mirrors what the vertex shader does: map the image pixel to its NDC quad
    position (within ``ndc_rect``), then apply ``u_transform``.

    Image pixel (Y-down, top-left origin) → NDC quad vertex (Y-up) for a
    rect-spanning quad: u = px_x/tex_w, v = 1 - px_y/tex_h, then map [0,1]
    into the display rect. For the default full-rect this is
    ndc_x = u*2-1, ndc_y = v*2-1.
    """
    m = _edit_mat3(transform_tuple, tex_w, tex_h)
    # column-major: col0=(m0,m1,m2) col1=(m3,m4,m5) col2=(m6,m7,m8)
    m00, m10, m01, m11, m02, m12 = m[0], m[1], m[3], m[4], m[6], m[7]

    u = img_px_x / tex_w
    v = 1.0 - img_px_y / tex_h
    x0, y0, x1, y1 = ndc_rect
    ndc_x = x0 + u * (x1 - x0)
    ndc_y = y0 + v * (y1 - y0)

    out_x = m00 * ndc_x + m01 * ndc_y + m02
    out_y = m10 * ndc_x + m11 * ndc_y + m12
    return out_x, out_y


def _edit_track_fbo_transform_point(
    img_px_x: float,
    img_px_y: float,
    transform_tuple: tuple[float, float, float, float, float, str, float, float],
    tex_w: int,
    tex_h: int,
) -> tuple[float, float]:
    """Apply a track-FBO transform and its final presentation Y flip.

    Edit track FBOs store the image with top at their GL bottom and the final
    compositor flips them once.  This is the exact production path used by
    zero-copy, planar, and solid clips.
    """
    m = _edit_mat3(transform_tuple, tex_w, tex_h, track_fbo_y_flipped=True)
    # Image Y-down pixel → track-FBO NDC: the track coordinate is vertically
    # flipped relative to the display-space NDC used by the composite page.
    in_x = img_px_x / tex_w * 2.0 - 1.0
    in_y = img_px_y / tex_h * 2.0 - 1.0
    track_x = m[0] * in_x + m[3] * in_y + m[6]
    track_y = m[1] * in_x + m[4] * in_y + m[7]
    # Final compositor flip: F · track.
    return track_x, -track_y


def _composite_to_ndc(img_px_x, img_px_y, tx, ty, sx, sy, rot,
                      pivot_mode, piv_x, piv_y,
                      tex_w, tex_h, ndc_rect=(-1.0, -1.0, 1.0, 1.0)):
    """Reference: composite ``compute_transform_handles`` to_ndc (inline).

    Reproduces the Y-down pixel → NDC (Y-up) mapping from
    ``src/features/composite/ui/viewer_handles.py`` so the test has no
    dependency on the full handle dataclass path. The math is identical to
    the production code's ``to_ndc`` closure.
    """
    half_w = tex_w / 2.0
    half_h = tex_h / 2.0
    if pivot_mode == "center":
        piv_px_x, piv_px_y = half_w, half_h
    else:
        piv_px_x, piv_px_y = piv_x, piv_y

    rad = math.radians(rot)
    cos_r, sin_r = math.cos(rad), math.sin(rad)

    rel_x = img_px_x - piv_px_x
    rel_y = img_px_y - piv_px_y
    sc_x = sx * rel_x
    sc_y = sy * rel_y
    rot_x = cos_r * sc_x - sin_r * sc_y
    rot_y = sin_r * sc_x + cos_r * sc_y
    off_x = rot_x + (piv_px_x - half_w) + tx
    off_y = rot_y + (piv_px_y - half_h) + ty

    px_to_ndc_x = (ndc_rect[2] - ndc_rect[0]) / tex_w
    px_to_ndc_y = (ndc_rect[3] - ndc_rect[1]) / tex_h
    img_center_ndc_x = ndc_rect[0] + half_w * px_to_ndc_x
    img_center_ndc_y = ndc_rect[1] + half_h * px_to_ndc_y

    return (img_center_ndc_x + off_x * px_to_ndc_x,
            img_center_ndc_y - off_y * px_to_ndc_y)


# Image / canvas under test: 1920x1080 (non-square, exposes aspect-ratio bugs).
W, H = 1920, 1080
# Points sampled: top-mid, bottom-mid, left-mid, right-mid, center.
SAMPLE_POINTS = [
    (W / 2, 0.0),       # top edge mid (exposes Y scale/translate)
    (W / 2, H),         # bottom edge mid
    (0.0, H / 2),       # left edge mid (exposes X)
    (W, H / 2),         # right edge mid
    (W / 2, H / 2),     # center (pivot for center mode)
]


def _assert_points_match(transform_tuple, tx, ty, sx, sy, rot,
                         pivot_mode, piv_x, piv_y, tol=1e-3):
    """Assert edit vertex matrix agrees with composite to_ndc for all samples."""
    for px, py in SAMPLE_POINTS:
        ex, ey = _edit_transform_point(px, py, transform_tuple, W, H)
        cx, cy = _composite_to_ndc(px, py, tx, ty, sx, sy, rot,
                                   pivot_mode, piv_x, piv_y, W, H)
        assert abs(ex - cx) <= tol, (
            f"X mismatch at ({px},{py}): edit={ex:.4f} composite={cx:.4f} "
            f"(Δ={ex-cx:.4f}) for transform {transform_tuple}"
        )
        assert abs(ey - cy) <= tol, (
            f"Y mismatch at ({px},{py}): edit={ey:.4f} composite={cy:.4f} "
            f"(Δ={ey-cy:.4f}) for transform {transform_tuple}"
        )


class TestTranslateParity:
    """translate must agree on every axis."""

    def test_translate_y_positive_moves_down(self):
        t = (0.0, 100.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 0.0, 100.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)

    def test_translate_x_positive_moves_right(self):
        t = (100.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 100.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)

    def test_translate_both_axes(self):
        t = (120.0, -80.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 120.0, -80.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)


class TestScaleParity:
    """scale must scale around the pivot (image center for center mode)."""

    def test_scale_y_2(self):
        t = (0.0, 0.0, 1.0, 2.0, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 0.0, 0.0, 1.0, 2.0, 0.0, "center", 0.0, 0.0)

    def test_scale_x_2(self):
        t = (0.0, 0.0, 2.0, 1.0, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 0.0, 0.0, 2.0, 1.0, 0.0, "center", 0.0, 0.0)

    def test_scale_uniform_half(self):
        t = (0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0)
        _assert_points_match(t, 0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0)


class TestRotationParity:
    """rotation must rotate around the pivot, clockwise for positive angles."""

    @pytest.mark.parametrize("angle", [30.0, -30.0, 90.0, -90.0, 45.0])
    def test_rotation_around_center(self, angle):
        t = (0.0, 0.0, 1.0, 1.0, angle, "center", 0.0, 0.0)
        _assert_points_match(t, 0.0, 0.0, 1.0, 1.0, angle, "center", 0.0, 0.0)


class TestCombinedParity:
    """rotate + scale + translate combined must agree end-to-end."""

    def test_rotate_scale_translate(self):
        t = (50.0, -30.0, 1.5, 0.8, 25.0, "center", 0.0, 0.0)
        _assert_points_match(t, 50.0, -30.0, 1.5, 0.8, 25.0, "center", 0.0, 0.0)


class TestCustomPivotParity:
    """custom pivot must place the rotation/scale origin at pivot_x/y."""

    def test_scale_around_custom_pivot(self):
        t = (0.0, 0.0, 2.0, 2.0, 0.0, "custom", 400.0, 300.0)
        _assert_points_match(t, 0.0, 0.0, 2.0, 2.0, 0.0, "custom", 400.0, 300.0)

    def test_rotate_around_custom_pivot(self):
        t = (0.0, 0.0, 1.0, 1.0, 45.0, "custom", 400.0, 300.0)
        _assert_points_match(t, 0.0, 0.0, 1.0, 1.0, 45.0, "custom", 400.0, 300.0)


class TestTrackFboPresentationParity:
    """All edit clip paths must match composite after their final FBO flip."""

    @pytest.mark.parametrize("transform_tuple", [
        (0.0, 120.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
        (40.0, -80.0, 1.3, 0.7, 35.0, "center", 0.0, 0.0),
        (25.0, 45.0, 0.8, 1.4, -28.0, "custom", 400.0, 300.0),
    ])
    def test_track_fbo_final_presentation_matches_composite(self, transform_tuple):
        tx, ty, sx, sy, rot, pivot_mode, piv_x, piv_y = transform_tuple
        for px, py in SAMPLE_POINTS:
            ex, ey = _edit_track_fbo_transform_point(
                px, py, transform_tuple, W, H)
            cx, cy = _composite_to_ndc(
                px, py, tx, ty, sx, sy, rot, pivot_mode, piv_x, piv_y, W, H)
            assert ex == pytest.approx(cx, abs=1e-3)
            assert ey == pytest.approx(cy, abs=1e-3)


def test_proxy_fbo_transform_uses_native_coordinate_size():
    """A proxy FBO must not amplify native-pixel translation."""
    transform = (192.0, 108.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
    raw = _transform_to_ndc_mat3_bytes(
        transform,
        1280,
        720,
        coordinate_width=1920,
        coordinate_height=1080,
    )
    matrix = struct.unpack("9f", raw)

    # 192 native pixels is 20% of a 1920-wide canvas, not 30% of a 1280-wide
    # proxy. The same rule applies vertically.
    assert matrix[6] == pytest.approx(0.2, abs=1e-6)
    assert matrix[7] == pytest.approx(-0.2, abs=1e-6)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
