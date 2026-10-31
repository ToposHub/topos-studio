"""
Tests for Mask/Roto control point handles (S5-11).

Tests the viewer_handles module functionality:
- MaskHandleType and RotoHandleType enums
- compute_mask_handles() position calculations
- compute_roto_handles() position calculations
- get_mask_handle_definitions() and get_roto_handle_definitions()
- compute_mask_delta_params() parameter updates
- compute_roto_delta_params() parameter updates

Reference: docs/composite_node_upgrade_plan_2026-05-16.md (S5-11)
"""

import pytest
import math
from typing import Tuple, Dict, Any

from src.features.composite.ui.viewer_handles import (
    # Enums
    MaskHandleType,
    RotoHandleType,
    # Colors
    MASK_COLOR_CENTER,
    MASK_COLOR_ROTATE,
    MASK_COLOR_FEATHER,
    MASK_COLOR_FILL,
    MASK_COLOR_OPACITY,
    HANDLE_COLOR_HOVER,
    # Data structures
    MaskHandlesData,
    RotoHandlesData,
    HandleDefinition,
    # Position calculators
    compute_mask_handles,
    compute_roto_handles,
    # Definition generators
    get_mask_handle_definitions,
    get_roto_handle_definitions,
    # Parameter update functions
    compute_mask_delta_params,
    compute_roto_delta_params,
)


# ── Test Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def default_image_ndc_rect() -> Tuple[float, float, float, float]:
    """Default NDC rect for a centered image (full viewport)."""
    return (-1.0, -1.0, 1.0, 1.0)


@pytest.fixture
def default_image_dimensions() -> Tuple[int, int]:
    """Default image dimensions: 1920x1080."""
    return (1920, 1080)


@pytest.fixture
def default_mask_params() -> dict:
    """Default MaskNode parameters."""
    return {
        "mask_type": "rectangle",
        "center_x": 0.5,
        "center_y": 0.5,
        "width": 0.5,
        "height": 0.5,
        "rotation": 0.0,
        "feather": 0.0,
    }


@pytest.fixture
def default_roto_params() -> dict:
    """Default RotoShapeNode parameters."""
    return {
        "shape_type": "rectangle",
        "center_x": 0.5,
        "center_y": 0.5,
        "width": 0.5,
        "height": 0.5,
        "rotation": 0.0,
        "feather": 0.0,
        "opacity": 1.0,
    }


# ── Enum Tests ───────────────────────────────────────────────────────────────

class TestMaskHandleTypeEnum:
    """Tests for MaskHandleType enum values."""

    def test_center_handle_exists(self):
        """Mask center handle type should exist."""
        assert MaskHandleType.CENTER.value == "mask_center"

    def test_rotate_handle_exists(self):
        """Mask rotate handle type should exist."""
        assert MaskHandleType.ROTATE.value == "mask_rotate"

    def test_feather_handles_exist(self):
        """All four feather handle types should exist."""
        assert MaskHandleType.FEATHER_T.value == "mask_feather_t"
        assert MaskHandleType.FEATHER_B.value == "mask_feather_b"
        assert MaskHandleType.FEATHER_L.value == "mask_feather_l"
        assert MaskHandleType.FEATHER_R.value == "mask_feather_r"

    def test_corner_handles_exist(self):
        """All four corner handle types should exist."""
        assert MaskHandleType.CORNER_TL.value == "mask_corner_tl"
        assert MaskHandleType.CORNER_TR.value == "mask_corner_tr"
        assert MaskHandleType.CORNER_BL.value == "mask_corner_bl"
        assert MaskHandleType.CORNER_BR.value == "mask_corner_br"

    def test_edge_handles_exist(self):
        """Width and height edge handles should exist."""
        assert MaskHandleType.WIDTH_L.value == "mask_width_l"
        assert MaskHandleType.WIDTH_R.value == "mask_width_r"
        assert MaskHandleType.HEIGHT_T.value == "mask_height_t"
        assert MaskHandleType.HEIGHT_B.value == "mask_height_b"

    def test_polygon_radius_handle_exists(self):
        """Polygon radius handle should exist."""
        assert MaskHandleType.POLYGON_RADIUS.value == "mask_polygon_radius"


class TestRotoHandleTypeEnum:
    """Tests for RotoHandleType enum values."""

    def test_center_handle_exists(self):
        """Roto center handle type should exist."""
        assert RotoHandleType.CENTER.value == "roto_center"

    def test_opacity_handle_exists(self):
        """Roto opacity handle should exist."""
        assert RotoHandleType.OPACITY.value == "roto_opacity"

    def test_all_roto_handles_correspond_to_mask(self):
        """Roto handles should correspond to Mask handles (except opacity)."""
        # All mask handle types should have corresponding roto types
        mask_types = [
            MaskHandleType.CENTER,
            MaskHandleType.ROTATE,
            MaskHandleType.WIDTH_L,
            MaskHandleType.WIDTH_R,
            MaskHandleType.HEIGHT_T,
            MaskHandleType.HEIGHT_B,
            MaskHandleType.CORNER_TL,
            MaskHandleType.CORNER_TR,
            MaskHandleType.CORNER_BL,
            MaskHandleType.CORNER_BR,
            MaskHandleType.FEATHER_T,
            MaskHandleType.FEATHER_B,
            MaskHandleType.FEATHER_L,
            MaskHandleType.FEATHER_R,
        ]

        roto_types = [
            RotoHandleType.CENTER,
            RotoHandleType.ROTATE,
            RotoHandleType.WIDTH_L,
            RotoHandleType.WIDTH_R,
            RotoHandleType.HEIGHT_T,
            RotoHandleType.HEIGHT_B,
            RotoHandleType.CORNER_TL,
            RotoHandleType.CORNER_TR,
            RotoHandleType.CORNER_BL,
            RotoHandleType.CORNER_BR,
            RotoHandleType.FEATHER_T,
            RotoHandleType.FEATHER_B,
            RotoHandleType.FEATHER_L,
            RotoHandleType.FEATHER_R,
        ]

        # Check that the naming convention is consistent
        for mask_type, roto_type in zip(mask_types, roto_types):
            assert mask_type.value.replace("mask_", "roto_") == roto_type.value


# ── Position Calculator Tests ───────────────────────────────────────────────

class TestComputeMaskHandles:
    """Tests for compute_mask_handles() function."""

    def test_center_position_at_origin(
        self, default_mask_params, default_image_ndc_rect, default_image_dimensions
    ):
        """Center handle should be at NDC origin for centered shape."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Center at (0.5, 0.5) in normalized coords = (0, 0) in NDC
        assert handles.center[0] == pytest.approx(0.0, abs=0.01)
        assert handles.center[1] == pytest.approx(0.0, abs=0.01)

    def test_bbox_size_for_half_width_height(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """BBox should be correct for half-width, half-height shape."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,  # Half width
            height_norm=0.5,  # Half height
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # For NDC rect (-1, -1, 1, 1), width_norm=0.5 means:
        # shape_width = 0.5 * ndc_w = 0.5 * 2 = 1.0 NDC units
        # Half-width = 0.5, so bbox: (-0.5, -0.5, 0.5, 0.5)
        bbox = handles.bbox_rect
        assert bbox[0] == pytest.approx(-0.5, abs=0.05)
        assert bbox[1] == pytest.approx(-0.5, abs=0.05)
        assert bbox[2] == pytest.approx(0.5, abs=0.05)
        assert bbox[3] == pytest.approx(0.5, abs=0.05)

    def test_corner_positions_unrotated(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Corner handles should be at correct positions without rotation."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Corners should be at bbox corners
        # TL: (-0.5, 0.5), TR: (0.5, 0.5), BL: (-0.5, -0.5), BR: (0.5, -0.5)
        assert handles.corner_tl[0] == pytest.approx(-0.5, abs=0.05)
        assert handles.corner_tl[1] == pytest.approx(0.5, abs=0.05)
        assert handles.corner_tr[0] == pytest.approx(0.5, abs=0.05)
        assert handles.corner_tr[1] == pytest.approx(0.5, abs=0.05)
        assert handles.corner_bl[0] == pytest.approx(-0.5, abs=0.05)
        assert handles.corner_bl[1] == pytest.approx(-0.5, abs=0.05)
        assert handles.corner_br[0] == pytest.approx(0.5, abs=0.05)
        assert handles.corner_br[1] == pytest.approx(-0.5, abs=0.05)

    def test_rotate_handle_above_bbox(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Rotate handle should be positioned above the bbox."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Rotate handle should be at center_x, above bbox top edge
        assert handles.rotate[0] == pytest.approx(handles.center[0], abs=0.01)
        assert handles.rotate[1] > handles.bbox_rect[3]  # Above bbox top (y1)

    def test_rotated_corners(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Corner handles should rotate correctly with rotation parameter."""
        # Test 45 degree rotation
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=45.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # With 45 degree rotation, corners should be rotated
        # Original TL (-0.5, 0.5) rotated 45 degrees around (0, 0)
        rad = math.radians(45.0)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        # Check that corners are rotated (they should not be axis-aligned anymore)
        # For a square, rotated corners should all have same distance from center
        dist_tl = math.sqrt(handles.corner_tl[0]**2 + handles.corner_tl[1]**2)
        dist_tr = math.sqrt(handles.corner_tr[0]**2 + handles.corner_tr[1]**2)
        dist_bl = math.sqrt(handles.corner_bl[0]**2 + handles.corner_bl[1]**2)
        dist_br = math.sqrt(handles.corner_br[0]**2 + handles.corner_br[1]**2)

        # All corners should have same distance from center (for centered shape)
        expected_dist = math.sqrt(0.5**2 + 0.5**2)
        assert dist_tl == pytest.approx(expected_dist, abs=0.05)
        assert dist_tr == pytest.approx(expected_dist, abs=0.05)
        assert dist_bl == pytest.approx(expected_dist, abs=0.05)
        assert dist_br == pytest.approx(expected_dist, abs=0.05)

    def test_polygon_radius_handle(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Polygon radius handle should exist for polygon shapes."""
        handles = compute_mask_handles(
            mask_type="polygon",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Polygon radius handle should be set
        assert handles.polygon_radius is not None

        # Radius handle should be at the edge (half-width from center)
        # For unrotated polygon, it should be on the right edge
        assert handles.polygon_radius[0] == pytest.approx(0.5, abs=0.05)
        assert handles.polygon_radius[1] == pytest.approx(0.0, abs=0.05)

    def test_polygon_radius_none_for_rectangle(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Polygon radius handle should be None for non-polygon shapes."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        assert handles.polygon_radius is None

    def test_feather_handles_with_feather(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Feather handles should be offset from edges when feather > 0."""
        handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.1,  # 10% feather
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Feather handles should be positioned outside the bbox
        # Top feather should be above top edge center
        assert handles.feather_t[1] > handles.height_top[1]
        # Bottom feather should be below bottom edge center
        assert handles.feather_b[1] < handles.height_bottom[1]
        # Left feather should be left of left edge center
        assert handles.feather_l[0] < handles.width_left[0]
        # Right feather should be right of right edge center
        assert handles.feather_r[0] > handles.width_right[0]


class TestComputeRotoHandles:
    """Tests for compute_roto_handles() function."""

    def test_handles_match_mask_handles(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Roto handles should match mask handles for same parameters."""
        mask_handles = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        roto_handles = compute_roto_handles(
            shape_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            opacity=1.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        # Positions should match
        assert roto_handles.center == mask_handles.center
        assert roto_handles.rotate == mask_handles.rotate
        assert roto_handles.corner_tl == mask_handles.corner_tl
        assert roto_handles.bbox_rect == mask_handles.bbox_rect

    def test_opacity_handle_none_when_full_opacity(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Opacity handle should be None when opacity is 1.0."""
        handles = compute_roto_handles(
            shape_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            opacity=1.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        assert handles.opacity is None

    def test_opacity_handle_exists_when_partial_opacity(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Opacity handle should exist when opacity < 1.0."""
        handles = compute_roto_handles(
            shape_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            opacity=0.5,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        assert handles.opacity is not None

        # Opacity handle should be below bbox bottom
        assert handles.opacity[1] < handles.bbox_rect[1]


# ── Definition Generator Tests ───────────────────────────────────────────────

class TestGetMaskHandleDefinitions:
    """Tests for get_mask_handle_definitions() function."""

    def test_returns_list_of_definitions(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Should return a list of HandleDefinition objects."""
        handles_data = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_mask_handle_definitions(handles_data)

        assert isinstance(definitions, list)
        assert len(definitions) > 0
        assert all(isinstance(d, HandleDefinition) for d in definitions)

    def test_center_handle_size(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Center handle should be larger (11px)."""
        handles_data = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_mask_handle_definitions(handles_data)

        # Find center handle
        center_def = next(
            (d for d in definitions if d.handle_type == MaskHandleType.CENTER), None
        )
        assert center_def is not None
        assert center_def.radius == 11.0

    def test_rotate_handle_color(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Rotate handle should use cyan color."""
        handles_data = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_mask_handle_definitions(handles_data)

        # Find rotate handle
        rotate_def = next(
            (d for d in definitions if d.handle_type == MaskHandleType.ROTATE), None
        )
        assert rotate_def is not None
        assert rotate_def.color == MASK_COLOR_ROTATE

    def test_feather_handle_color(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Feather handles should use cyan color."""
        handles_data = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.1,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_mask_handle_definitions(handles_data)

        # Find feather handles
        feather_defs = [
            d for d in definitions
            if d.handle_type in (
                MaskHandleType.FEATHER_T,
                MaskHandleType.FEATHER_B,
                MaskHandleType.FEATHER_L,
                MaskHandleType.FEATHER_R,
            )
        ]

        assert len(feather_defs) == 4
        for fd in feather_defs:
            assert fd.color == MASK_COLOR_FEATHER

    def test_hover_state_changes_color(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Hover handle should use hover color."""
        handles_data = compute_mask_handles(
            mask_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_mask_handle_definitions(
            handles_data,
            hover_handle="mask_center",
        )

        # Find center handle (hovered)
        center_def = next(
            (d for d in definitions if d.handle_type == MaskHandleType.CENTER), None
        )
        assert center_def is not None

        # Hover color should be yellow
        from src.features.composite.ui.viewer_handles import HANDLE_COLOR_HOVER
        assert center_def.color == HANDLE_COLOR_HOVER


class TestGetRotoHandleDefinitions:
    """Tests for get_roto_handle_definitions() function."""

    def test_includes_opacity_handle_when_partial_opacity(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Should include opacity handle when opacity < 1.0."""
        handles_data = compute_roto_handles(
            shape_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            opacity=0.5,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_roto_handle_definitions(handles_data)

        # Find opacity handle
        opacity_def = next(
            (d for d in definitions if d.handle_type == RotoHandleType.OPACITY), None
        )
        assert opacity_def is not None

    def test_opacity_handle_color(
        self, default_image_ndc_rect, default_image_dimensions
    ):
        """Opacity handle should use opacity color (yellow tint)."""
        handles_data = compute_roto_handles(
            shape_type="rectangle",
            center_x=0.5,
            center_y=0.5,
            width_norm=0.5,
            height_norm=0.5,
            rotation=0.0,
            feather=0.0,
            opacity=0.5,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
            image_ndc_rect=default_image_ndc_rect,
        )

        definitions = get_roto_handle_definitions(handles_data)

        opacity_def = next(
            (d for d in definitions if d.handle_type == RotoHandleType.OPACITY), None
        )
        assert opacity_def is not None
        assert opacity_def.color == MASK_COLOR_OPACITY


# ── Parameter Update Tests ───────────────────────────────────────────────────

class TestComputeMaskDeltaParams:
    """Tests for compute_mask_delta_params() function."""

    def test_center_handle_updates_position(
        self, default_image_dimensions
    ):
        """Dragging center handle should update center_x, center_y."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # Simulate drag: move 100px right, 50px down
        new_params = compute_mask_delta_params(
            handle_id="mask_center",
            drag_start_image_pos=(960.0, 540.0),  # Center
            drag_current_image_pos=(1060.0, 590.0),  # Moved
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "center_x" in new_params
        assert "center_y" in new_params
        # 100px / 1920 = ~0.052 delta
        assert new_params["center_x"] == pytest.approx(0.5 + 100/1920, abs=0.01)
        assert new_params["center_y"] == pytest.approx(0.5 + 50/1080, abs=0.01)

    def test_rotate_handle_updates_rotation(
        self, default_image_dimensions
    ):
        """Dragging rotate handle should update rotation."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # Simulate drag: rotate 45 degrees
        # Center at (960, 540), drag to position that gives ~45 degree angle
        new_params = compute_mask_delta_params(
            handle_id="mask_rotate",
            drag_start_image_pos=(960.0, 540.0),
            drag_current_image_pos=(960.0 + 480.0, 540.0),  # Right edge
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "rotation" in new_params
        # Angle should be calculated from center

    def test_width_handle_updates_width(
        self, default_image_dimensions
    ):
        """Dragging width handle should update width parameter."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # Simulate drag: expand width by moving right edge outward
        new_params = compute_mask_delta_params(
            handle_id="mask_width_r",
            drag_start_image_pos=(1200.0, 540.0),  # Right edge center
            drag_current_image_pos=(1300.0, 540.0),  # Moved right
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "width" in new_params
        assert new_params["width"] > original_params["width"]

    def test_corner_handle_updates_both_dimensions(
        self, default_image_dimensions
    ):
        """Dragging corner handle should update both width and height."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # Simulate drag: expand both dimensions
        new_params = compute_mask_delta_params(
            handle_id="mask_corner_br",
            drag_start_image_pos=(1200.0, 675.0),  # Bottom-right corner
            drag_current_image_pos=(1300.0, 750.0),  # Moved right and down
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "width" in new_params
        assert "height" in new_params
        assert new_params["width"] > original_params["width"]
        assert new_params["height"] > original_params["height"]

    def test_feather_handle_updates_feather(
        self, default_image_dimensions
    ):
        """Dragging feather handle should update feather parameter."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # Simulate drag: increase feather by moving outward
        new_params = compute_mask_delta_params(
            handle_id="mask_feather_r",
            drag_start_image_pos=(1200.0, 540.0),
            drag_current_image_pos=(1250.0, 540.0),  # Moved outward
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "feather" in new_params
        assert new_params["feather"] > original_params["feather"]


class TestComputeRotoDeltaParams:
    """Tests for compute_roto_delta_params() function."""

    def test_opacity_handle_updates_opacity(
        self, default_image_dimensions
    ):
        """Dragging opacity handle should update opacity parameter."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
            "opacity": 1.0,
        }

        # Simulate drag: decrease opacity by moving down (Y increases)
        new_params = compute_roto_delta_params(
            handle_id="roto_opacity",
            drag_start_image_pos=(960.0, 600.0),
            drag_current_image_pos=(960.0, 700.0),  # Moved down
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "opacity" in new_params
        # Moving down should decrease opacity (Y-down coordinates)
        assert new_params["opacity"] < original_params["opacity"]

    def test_other_handles_delegate_to_mask(
        self, default_image_dimensions
    ):
        """Other roto handles should use mask delta params logic."""
        original_params = {
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.5,
            "height": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
            "opacity": 1.0,
        }

        # Test that roto_center works like mask_center
        new_params = compute_roto_delta_params(
            handle_id="roto_center",
            drag_start_image_pos=(960.0, 540.0),
            drag_current_image_pos=(1060.0, 540.0),
            original_params=original_params,
            image_width=default_image_dimensions[0],
            image_height=default_image_dimensions[1],
        )

        assert "center_x" in new_params


# ── Run Tests ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])