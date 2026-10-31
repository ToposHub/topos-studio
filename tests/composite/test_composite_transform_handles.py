"""
Tests for Transform node on-viewer control handles (Fusion-style upgrade).

Covers the rotation-aware geometry & drag math in viewer_handles:
- compute_transform_handles(): ordered rotated frame corners, edge midpoints,
  center/pivot positions.
- compute_transform_delta_params(): translate / pivot / rotate / scale (corner &
  edge) / Shift-uniform / clamp, all correct under any rotation, with the
  opposite anchor held fixed in world space.

Reference: docs/superpowers/specs/2026-06-12-composite-transform-handles-design.md
"""

import math
import pytest

from src.features.composite.ui.viewer_handles import (
    TRANSFORM_ROTATE_HANDLE_INSET,
    TransformHandleType,
    TransformHandlesData,
    compute_transform_handles,
    compute_transform_delta_params,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ndc_rect():
    """Full-viewport NDC rect (-1,-1)-(1,1)."""
    return (-1.0, -1.0, 1.0, 1.0)


@pytest.fixture
def dims():
    """Square 100x100 image."""
    return (100, 100)


def default_params(**overrides):
    p = {
        "translate_x": 0.0, "translate_y": 0.0,
        "scale_x": 1.0, "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "pivot_x": 50.0, "pivot_y": 50.0,
        "filter": "linear",
    }
    p.update(overrides)
    return p


def xform_point(px, py, params, w, h):
    """Mirror of the node's image transform in pixel space (for invariant checks).

    world = R(theta) * S * (p - piv) + piv + t
    """
    if params.get("pivot_mode", "center") == "center":
        piv = (w / 2.0, h / 2.0)
    else:
        piv = (params.get("pivot_x", w / 2.0), params.get("pivot_y", h / 2.0))
    rad = math.radians(params.get("rotation", 0.0))
    c, s = math.cos(rad), math.sin(rad)
    rx, ry = px - piv[0], py - piv[1]
    scx = params.get("scale_x", 1.0) * rx
    scy = params.get("scale_y", 1.0) * ry
    wx = c * scx - s * scy + piv[0] + params.get("translate_x", 0.0)
    wy = s * scx + c * scy + piv[1] + params.get("translate_y", 0.0)
    return (wx, wy)


# ── compute_transform_handles ───────────────────────────────────────────────

class TestComputeTransformHandles:
    def test_identity_frame_corners(self, ndc_rect, dims):
        w, h = dims
        data = compute_transform_handles(0, 0, 1, 1, 0, "center", 50, 50, w, h, ndc_rect)
        # Ordered TL, TR, BR, BL → the image rectangle in NDC
        assert data.frame_corners[0] == pytest.approx((-1.0, 1.0))    # TL
        assert data.frame_corners[1] == pytest.approx((1.0, 1.0))     # TR
        assert data.frame_corners[2] == pytest.approx((1.0, -1.0))    # BR
        assert data.frame_corners[3] == pytest.approx((-1.0, -1.0))   # BL

    def test_identity_center_and_edges(self, ndc_rect, dims):
        w, h = dims
        data = compute_transform_handles(0, 0, 1, 1, 0, "center", 50, 50, w, h, ndc_rect)
        assert data.center == pytest.approx((0.0, 0.0))
        assert data.pivot == pytest.approx((0.0, 0.0))
        # Edge midpoints sit on the rotated frame edges (axis-aligned at identity)
        assert data.scale_y_top == pytest.approx((0.0, 1.0))
        assert data.scale_y_bottom == pytest.approx((0.0, -1.0))
        assert data.scale_x_left == pytest.approx((-1.0, 0.0))
        assert data.scale_x_right == pytest.approx((1.0, 0.0))

    def test_rotate_handle_sits_inside_top_pivot_guide(self, ndc_rect, dims):
        w, h = dims
        data = compute_transform_handles(0, 0, 1, 1, 0, "center", 50, 50, w, h, ndc_rect)
        expected = (
            data.scale_y_top[0] + (data.pivot[0] - data.scale_y_top[0]) * TRANSFORM_ROTATE_HANDLE_INSET,
            data.scale_y_top[1] + (data.pivot[1] - data.scale_y_top[1]) * TRANSFORM_ROTATE_HANDLE_INSET,
        )
        assert data.rotate == pytest.approx(expected)
        assert data.scale_y_top[1] > data.rotate[1] > data.pivot[1]

    def test_rotate_handle_aligns_above_custom_pivot(self, ndc_rect, dims):
        """With a custom (off-center) pivot, the rotate handle sits directly
        above the pivot on the image vertical axis — not on the old
        top-edge→pivot diagonal guide."""
        w, h = dims
        data = compute_transform_handles(0, 0, 1, 1, 0, "custom", 75, 50, w, h, ndc_rect)
        # rotation=0 ⇒ image vertical axis is straight up ⇒ handle shares pivot X
        # and sits above the pivot.
        assert data.rotate[0] == pytest.approx(data.pivot[0], abs=1e-6)
        assert data.rotate[1] > data.pivot[1]
        # And it must NOT be on the old edge_top→pivot diagonal guide.
        old_pivot_guide = (
            data.scale_y_top[0] + (data.pivot[0] - data.scale_y_top[0]) * TRANSFORM_ROTATE_HANDLE_INSET,
            data.scale_y_top[1] + (data.pivot[1] - data.scale_y_top[1]) * TRANSFORM_ROTATE_HANDLE_INSET,
        )
        assert data.rotate != pytest.approx(old_pivot_guide)

    def test_rotation_rotates_corners(self, ndc_rect, dims):
        """A 90° rotation maps the TL corner to where TR was (frame is rotated)."""
        w, h = dims
        ident = compute_transform_handles(0, 0, 1, 1, 0, "center", 50, 50, w, h, ndc_rect)
        rot90 = compute_transform_handles(0, 0, 1, 1, 90, "center", 50, 50, w, h, ndc_rect)
        # TL under 90° lands on identity TR
        assert rot90.scale_corner_tl == pytest.approx(ident.scale_corner_tr, abs=1e-6)

    def test_frame_corners_is_four_tuple(self, ndc_rect, dims):
        w, h = dims
        data = compute_transform_handles(0, 0, 1, 1, 0, "center", 50, 50, w, h, ndc_rect)
        assert isinstance(data, TransformHandlesData)
        assert len(data.frame_corners) == 4


# ── canvas vs input image sizing (handle frame hugs the input image) ───────

class TestCanvasImageSizing:
    """The on-canvas transform frame must hug the *input image*, not stretch
    across the whole composite canvas. A 1500×800 foreground on a 1920×1080
    canvas should render its frame at the lower-left of the viewport, occupying
    1500/1920 of the width and 800/1080 of the height — not the full canvas.
    """

    def test_smaller_image_hugs_lower_left_of_canvas(self):
        """1500×800 image on 1920×1080 canvas → frame sits at lower-left,
        occupying 78% width × 74% height (the image's true footprint).
        """
        data = compute_transform_handles(
            0, 0, 1, 1, 0, "center", 750, 400,
            1500, 800, (-1.0, -1.0, 1.0, 1.0),
            canvas_width=1920, canvas_height=1080,
        )
        tl, tr, br, bl = data.frame_corners
        # Image lower-left corner = canvas lower-left → NDC (-1, -1).
        # Image upper-left (y=800 in canvas Y-up) → NDC y = -1 + 800/1080*2.
        # Image right edge (x=1500) → NDC x = -1 + 1500/1920*2.
        x_right = -1.0 + 1500.0 / 1920.0 * 2.0   # 0.5625
        y_top = -1.0 + 800.0 / 1080.0 * 2.0      # 0.4815
        assert tl == pytest.approx((-1.0, y_top))
        assert tr == pytest.approx((x_right, y_top))
        assert br == pytest.approx((x_right, -1.0))
        assert bl == pytest.approx((-1.0, -1.0))
        # Frame width / height in NDC reflect the image's footprint, not 2.0.
        assert (tr[0] - tl[0]) == pytest.approx(1500.0 / 1920.0 * 2.0)
        assert (tl[1] - bl[1]) == pytest.approx(800.0 / 1080.0 * 2.0)

    def test_image_equals_canvas_fills_viewport(self):
        """Regression guard: when the image fills the canvas, the frame still
        spans the whole viewport (legacy behaviour, unchanged)."""
        data = compute_transform_handles(
            0, 0, 1, 1, 0, "center", 960, 540,
            1920, 1080, (-1.0, -1.0, 1.0, 1.0),
            canvas_width=1920, canvas_height=1080,
        )
        tl, tr, br, bl = data.frame_corners
        assert tl == pytest.approx((-1.0, 1.0))
        assert tr == pytest.approx((1.0, 1.0))
        assert br == pytest.approx((1.0, -1.0))
        assert bl == pytest.approx((-1.0, -1.0))

    def test_smaller_image_translate_moves_frame(self):
        """Translating a smaller image moves the frame within the canvas — it
        does NOT stay glued to the lower-left, and the canvas extent (2.0 NDC)
        is the reference, not the image width.
        """
        # +400 px right (x), +100 px down (y) on a 1500×800 / 1920×1080 canvas.
        data = compute_transform_handles(
            400, 100, 1, 1, 0, "center", 750, 400,
            1500, 800, (-1.0, -1.0, 1.0, 1.0),
            canvas_width=1920, canvas_height=1080,
        )
        tl_id = compute_transform_handles(
            0, 0, 1, 1, 0, "center", 750, 400,
            1500, 800, (-1.0, -1.0, 1.0, 1.0),
            canvas_width=1920, canvas_height=1080,
        ).frame_corners[0]
        # translate_x +400 px on a 1920-wide canvas → NDC delta = 400/1920*2.
        dx_ndc = 400.0 / 1920.0 * 2.0
        # translate_y +100 px (Y-down) → NDC y decreases by 100/1080*2.
        dy_ndc = -100.0 / 1080.0 * 2.0
        assert data.frame_corners[0] == pytest.approx(
            (tl_id[0] + dx_ndc, tl_id[1] + dy_ndc)
        )

    def test_default_canvas_falls_back_to_image_size(self):
        """When canvas_width/height are omitted (0), the function falls back to
        image size → centred-anchor legacy behaviour, so older callers that
        don't pass canvas dimensions are unaffected.
        """
        legacy = compute_transform_handles(
            0, 0, 1, 1, 0, "center", 50, 50,
            100, 100, (-1.0, -1.0, 1.0, 1.0),
        )
        explicit = compute_transform_handles(
            0, 0, 1, 1, 0, "center", 50, 50,
            100, 100, (-1.0, -1.0, 1.0, 1.0),
            canvas_width=100, canvas_height=100,
        )
        for a, b in zip(legacy.frame_corners, explicit.frame_corners):
            assert a == pytest.approx(b)


# ── compute_transform_delta_params ──────────────────────────────────────────

class TestTranslateAndPivot:
    def test_center_translate(self, dims):
        w, h = dims
        out = compute_transform_delta_params(
            "center", (50, 50), (70, 80), default_params(), w, h, 0)
        assert out["translate_x"] == pytest.approx(20.0)
        # Renderer translate is in Y-down image space (positive = down), the same
        # space as the drag, so a +30 down-drag yields translate_y = +30 (the
        # image follows the cursor — no Y flip).
        assert out["translate_y"] == pytest.approx(30.0)

    def test_pivot_moves_and_switches_to_custom(self, dims):
        w, h = dims
        out = compute_transform_delta_params(
            "pivot", (50, 50), (60, 55), default_params(), w, h, 0)
        assert out["pivot_mode"] == "custom"
        assert out["pivot_x"] == pytest.approx(60.0)
        assert out["pivot_y"] == pytest.approx(55.0)   # Y-down image space, no flip

    def test_pivot_drag_from_center_mode_starts_at_image_center(self, dims):
        w, h = dims
        params = default_params()
        params.update({"pivot_mode": "center", "pivot_x": 0.0, "pivot_y": 0.0})
        out = compute_transform_delta_params(
            "pivot", (50, 50), (70, 80), params, w, h, 0)
        assert out["pivot_mode"] == "custom"
        assert out["pivot_x"] == pytest.approx(70.0)
        assert out["pivot_y"] == pytest.approx(80.0)


class TestRotate:
    def test_mouse_above_pivot_is_zero(self, dims):
        w, h = dims
        # Mouse directly above the pivot -> rotation 0 (guide points up)
        out = compute_transform_delta_params(
            "rotate", (50, 50), (50, -100), default_params(), w, h, 0)
        assert out["rotation"] == pytest.approx(0.0, abs=1e-6)

    def test_mouse_right_of_pivot_is_clockwise(self, dims):
        w, h = dims
        # Dragging the rotate handle to the right of the pivot rotates clockwise.
        # The renderer is Y-down (a positive angle rotates clockwise on screen),
        # so the rotation value is +90 — the image follows the handle.
        out = compute_transform_delta_params(
            "rotate", (50, 50), (200, 50), default_params(), w, h, 0)
        assert out["rotation"] == pytest.approx(90.0, abs=1e-6)


class TestScale:
    def test_corner_br_tracks_mouse_and_fixes_opposite(self, dims):
        """Drag BR outward by 20px: scale=1.2, opposite corner TL stays put."""
        w, h = dims
        out = compute_transform_delta_params(
            "scale_corner_br", (100, 100), (120, 120), default_params(), w, h, 0)
        assert out["scale_x"] == pytest.approx(1.2)
        assert out["scale_y"] == pytest.approx(1.2)
        assert out["translate_x"] == pytest.approx(10.0)
        assert out["translate_y"] == pytest.approx(10.0)
        # Invariant: TL corner fixed in world; BR at the mouse
        new = default_params(**out)
        assert xform_point(0, 0, new, w, h) == pytest.approx((0.0, 0.0), abs=1e-6)
        assert xform_point(100, 100, new, w, h) == pytest.approx((120.0, 120.0), abs=1e-6)

    def test_edge_left_stretches_x_only(self, dims):
        """Left-edge drag changes scale_x only; right edge (anchor) stays fixed."""
        w, h = dims
        out = compute_transform_delta_params(
            "scale_x_left", (0, 50), (-20, 50), default_params(), w, h, 0)
        assert out["scale_x"] == pytest.approx(1.2)
        assert out["scale_y"] == pytest.approx(1.0)   # unchanged
        # Right-edge midpoint (anchor) stays at world (100, 50)
        new = default_params(**out)
        assert xform_point(100, 50, new, w, h) == pytest.approx((100.0, 50.0), abs=1e-6)

    def test_default_locks_aspect_shift_frees(self, dims):
        """Corner drag preserves aspect ratio by default; Shift frees x/y.

        Asymmetric drag (120, 105): the independent x/y scales would be
        1.2 / 1.05. With no modifier the corner scales uniformly (both → the
        average ≈ 1.125); holding Shift releases the lock so x and y scale
        independently.
        """
        w, h = dims
        SHIFT = 0x2000000
        # Default (no modifier): aspect locked → uniform average
        locked = compute_transform_delta_params(
            "scale_corner_br", (100, 100), (120, 105), default_params(), w, h, 0)
        assert locked["scale_x"] == pytest.approx(locked["scale_y"])
        assert locked["scale_x"] == pytest.approx(1.125, abs=1e-3)

        # Shift: free x/y scaling
        free = compute_transform_delta_params(
            "scale_corner_br", (100, 100), (120, 105), default_params(), w, h, SHIFT)
        assert free["scale_x"] == pytest.approx(1.2, abs=1e-3)
        assert free["scale_y"] == pytest.approx(1.05, abs=1e-3)

    def test_scale_clamps_to_max(self, dims):
        w, h = dims
        out = compute_transform_delta_params(
            "scale_corner_br", (100, 100), (1000, 1000), default_params(), w, h, 0)
        assert out["scale_x"] == 5.0
        assert out["scale_y"] == 5.0

    def test_scale_clamps_to_min(self, dims):
        w, h = dims
        # BR scale is measured from the TL anchor; dragging BR nearly onto TL
        # collapses the scale below the floor → clamped to 0.01.
        out = compute_transform_delta_params(
            "scale_corner_br", (100, 100), (0.5, 0.5), default_params(), w, h, 0)
        assert out["scale_x"] == 0.01
        assert out["scale_y"] == 0.01

    def test_corner_anchor_fixed_under_rotation(self, dims):
        """At 45°, BR drag keeps TL fixed; Shift (free) makes BR track the mouse.

        Under the aspect-locked default the corner scales uniformly, so it only
        tracks the mouse along the anchor→handle diagonal — the exact-tracking
        invariant is therefore verified on the free (Shift) path where x/y scale
        independently.
        """
        w, h = dims
        SHIFT = 0x2000000
        base = default_params(rotation=45.0)
        # Current world position of BR and TL under the original params
        br_world = xform_point(100, 100, base, w, h)
        tl_world = xform_point(0, 0, base, w, h)
        # Drag BR to a point 15px further out diagonally in world space
        target = (br_world[0] + 15.0, br_world[1] + 15.0)

        # Free (Shift) path: TL anchor fixed AND BR tracks the mouse exactly.
        out = compute_transform_delta_params(
            "scale_corner_br", br_world, target, base, w, h, SHIFT)
        new = default_params(**{**base, **out})
        assert xform_point(0, 0, new, w, h) == pytest.approx(tl_world, abs=1e-4)
        assert xform_point(100, 100, new, w, h) == pytest.approx(target, abs=1e-4)

        # Default (aspect-locked) path: TL anchor still fixed, and the scale is
        # uniform even though the world drag is asymmetric in local space.
        locked = compute_transform_delta_params(
            "scale_corner_br", br_world, target, base, w, h, 0)
        new_locked = default_params(**{**base, **locked})
        assert xform_point(0, 0, new_locked, w, h) == pytest.approx(tl_world, abs=1e-4)
        assert locked["scale_x"] == pytest.approx(locked["scale_y"])

    def test_edge_under_rotation_only_changes_one_axis(self, dims):
        """At 30°, a left-edge drag changes scale_x and leaves scale_y = orig."""
        w, h = dims
        base = default_params(rotation=30.0)
        br_world = xform_point(100, 100, base, w, h)  # arbitrary current handle pos region
        out = compute_transform_delta_params(
            "scale_x_left", br_world, (br_world[0] - 10, br_world[1] + 3),
            base, w, h, 0)
        assert out["scale_y"] == pytest.approx(1.0)   # single-axis handle
        assert "scale_x" in out


# ── Regression: the old is_scale misclassification (now fixed) ──────────────

class TestHandleClassificationRegression:
    @pytest.mark.parametrize("hid", [
        "scale_corner_tl", "scale_corner_tr", "scale_corner_bl", "scale_corner_br",
        "scale_x_left", "scale_x_right", "scale_y_top", "scale_y_bottom",
    ])
    def test_scale_handles_produce_scale_keys(self, hid, dims):
        """All scale handles must emit scale_x/scale_y (regression: previously dead)."""
        w, h = dims
        out = compute_transform_delta_params(
            hid, (50, 50), (60, 60), default_params(), w, h, 0)
        assert "scale_x" in out or "scale_y" in out


# ── Rotate handle vertical-axis alignment ───────────────────────────────────

def test_rotate_handle_parallel_to_local_up_under_rotation(ndc_rect, dims):
    """Under rotation, pivot→rotate must be parallel to the image's local up
    (center→top-edge), i.e. aligned with the frame's vertical axis."""
    w, h = dims
    data = compute_transform_handles(
        translate_x=0.0, translate_y=0.0, scale_x=1.0, scale_y=1.0,
        rotation=35.0,
        pivot_mode="custom", pivot_x=70.0, pivot_y=60.0,
        image_width=w, image_height=h, image_ndc_rect=ndc_rect,
    )

    up = (data.scale_y_top[0] - data.center[0], data.scale_y_top[1] - data.center[1])
    pv = (data.rotate[0] - data.pivot[0], data.rotate[1] - data.pivot[1])
    cross = up[0] * pv[1] - up[1] * pv[0]
    assert abs(cross) < 1e-6  # parallel ⇒ aligned with the vertical axis
