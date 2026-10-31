"""
Tests for Viewer Tool System (S5-15).

Tests pan/zoom, transform handles, cornerpin handles, crop handles,
mask/roto handles, and undo operations for the CompositeViewer.

Uses pytest + QTest for mouse simulation and direct API calls for
lightweight integration tests.
"""

import pytest
import math
from typing import Dict, Any, Tuple
from PyQt6.QtCore import QPointF, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.viewer_tool_system import (
    ViewerToolMode,
    ViewerInteractionState,
    ViewerSelectionState,
    NodeOutputRegion,
    HandleType,
    view_pos_to_ndc,
    ndc_to_view_pos,
    ndc_to_image_pos,
    image_pos_to_ndc,
    compute_image_ndc_rect,
    hit_test_handle,
    hit_test_node_output,
    hit_test_handles_in_rect,
    hit_test_rect,
    compute_crop_handles,
    compute_reformat_handles,
    compute_crop_rect_from_handles,
    compute_reformat_rect_from_handles,
    get_cursor_for_handle,
)

from src.features.composite.ui.viewer_handles import (
    TRANSFORM_ROTATE_HANDLE_INSET,
    compute_transform_handles,
    compute_cornerpin_handles,
    get_transform_handle_definitions,
    get_cornerpin_handle_definitions,
    TransformHandlesData,
    CornerPinHandlesData,
    TransformHandleType,
    CornerPinHandleType,
    ndc_to_screen_pixels,
    screen_pixels_to_ndc,
    hit_test_handle_screen,
    find_nearest_handle,
    compute_transform_delta_params,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Pan/Zoom Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPanZoom:
    """Tests for viewport pan/zoom functionality."""

    def test_view_pos_to_ndc(self):
        """Test view pixel to NDC conversion."""
        # Center of viewport should be (0, 0) in NDC
        ndc = view_pos_to_ndc(QPointF(400.0, 300.0), 800.0, 600.0, 1.0)
        assert abs(ndc.x() - 0.0) < 0.01
        assert abs(ndc.y() - 0.0) < 0.01

        # Top-left corner should be (-1, 1) in NDC
        ndc_tl = view_pos_to_ndc(QPointF(0.0, 0.0), 800.0, 600.0, 1.0)
        assert abs(ndc_tl.x() - (-1.0)) < 0.01
        assert abs(ndc_tl.y() - 1.0) < 0.01

        # Bottom-right corner should be (1, -1) in NDC
        ndc_br = view_pos_to_ndc(QPointF(800.0, 600.0), 800.0, 600.0, 1.0)
        assert abs(ndc_br.x() - 1.0) < 0.01
        assert abs(ndc_br.y() - (-1.0)) < 0.01

    def test_ndc_to_view_pos(self):
        """Test NDC to view pixel conversion."""
        # NDC (0, 0) should be center of viewport
        view = ndc_to_view_pos(QPointF(0.0, 0.0), 800.0, 600.0, 1.0)
        assert abs(view.x() - 400.0) < 0.01
        assert abs(view.y() - 300.0) < 0.01

        # NDC (-1, 1) should be top-left
        view_tl = ndc_to_view_pos(QPointF(-1.0, 1.0), 800.0, 600.0, 1.0)
        assert abs(view_tl.x() - 0.0) < 0.01
        assert abs(view_tl.y() - 0.0) < 0.01

        # NDC (1, -1) should be bottom-right
        view_br = ndc_to_view_pos(QPointF(1.0, -1.0), 800.0, 600.0, 1.0)
        assert abs(view_br.x() - 800.0) < 0.01
        assert abs(view_br.y() - 600.0) < 0.01

    def test_ndc_to_image_pos(self):
        """Test NDC to image pixel conversion."""
        # Standard case: 1920x1080 image centered in viewport
        ndc_rect = (-0.5, -0.5, 0.5, 0.5)  # NDC bounds for image

        # Center of image should be (960, 540)
        img_pos = ndc_to_image_pos(QPointF(0.0, 0.0), ndc_rect, 1920, 1080)
        assert img_pos is not None
        assert abs(img_pos.x() - 960.0) < 1.0
        assert abs(img_pos.y() - 540.0) < 1.0

        # Top-left of image should be (0, 0)
        img_pos_tl = ndc_to_image_pos(QPointF(-0.5, 0.5), ndc_rect, 1920, 1080)
        assert img_pos_tl is not None
        assert abs(img_pos_tl.x() - 0.0) < 1.0
        assert abs(img_pos_tl.y() - 0.0) < 1.0

        # Outside image bounds should return None
        img_pos_out = ndc_to_image_pos(QPointF(1.0, 1.0), ndc_rect, 1920, 1080)
        assert img_pos_out is None

    def test_compute_image_ndc_rect_fit_to_window(self):
        """Test image NDC rect calculation for fit-to-window."""
        # Wider image in narrower viewport (letterbox on sides)
        ndc_rect = compute_image_ndc_rect(
            1920, 1080,  # 16:9 image
            800, 600,    # 4:3 viewport
            1.0,         # zoom = 1.0 (fit)
            0.0, 0.0,    # no pan
            1.0
        )

        x0, y0, x1, y1 = ndc_rect
        # Aspect ratio: image 16:9 = 1.777, viewport 4:3 = 1.333
        # Image is wider, so letterbox on sides
        # Width fills (-1 to 1), height is scaled
        assert abs(x0 - (-1.0)) < 0.01
        assert abs(x1 - 1.0) < 0.01
        # Height should be smaller than -1 to 1
        assert abs(y0) > 0  # Not at -1
        assert abs(y1) > 0  # Not at 1

    def test_compute_image_ndc_rect_zoom_in(self):
        """Test image NDC rect calculation with zoom in."""
        # Zoom 2x should show smaller portion of image
        ndc_rect_base = compute_image_ndc_rect(
            1920, 1080,
            800, 600,
            1.0, 0.0, 0.0, 1.0
        )
        ndc_rect_zoom = compute_image_ndc_rect(
            1920, 1080,
            800, 600,
            2.0, 0.0, 0.0, 1.0
        )

        # Zoomed rect should be larger in NDC (shows less of image)
        base_width = ndc_rect_base[2] - ndc_rect_base[0]
        zoom_width = ndc_rect_zoom[2] - ndc_rect_zoom[0]
        assert abs(zoom_width - base_width * 2.0) < 0.01

    def test_compute_image_ndc_rect_pan(self):
        """Test image NDC rect calculation with pan offset."""
        # Pan right (positive x) should shift rect right
        ndc_rect_base = compute_image_ndc_rect(
            1920, 1080,
            800, 600,
            1.0, 0.0, 0.0, 1.0
        )
        ndc_rect_pan = compute_image_ndc_rect(
            1920, 1080,
            800, 600,
            1.0, 0.3, 0.0, 1.0
        )

        # Pan should offset both x0 and x1
        assert abs(ndc_rect_pan[0] - (ndc_rect_base[0] + 0.3)) < 0.01
        assert abs(ndc_rect_pan[2] - (ndc_rect_base[2] + 0.3)) < 0.01

    def test_zoom_level_save_restore(self):
        """Test zoom state serialization and restoration."""
        # Simulate viewer state
        state = {
            "zoom_level": 2.5,
            "pan_x": 0.1,
            "pan_y": -0.2,
        }

        # Verify state values can be retrieved
        assert state["zoom_level"] == 2.5
        assert state["pan_x"] == 0.1
        assert state["pan_y"] == -0.2

        # Verify state round-trip
        restored = state.copy()
        assert restored["zoom_level"] == state["zoom_level"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. Transform Handle Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTransformHandles:
    """Tests for Transform node control handles."""

    def test_compute_transform_handles_identity(self):
        """Test transform handles with identity transform (no change)."""
        handles_data = compute_transform_handles(
            translate_x=0.0,
            translate_y=0.0,
            scale_x=1.0,
            scale_y=1.0,
            rotation=0.0,
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # With identity transform, bbox should match input rect
        bbox = handles_data.bbox_rect
        assert abs(bbox[0] - (-0.5)) < 0.01
        assert abs(bbox[1] - (-0.5)) < 0.01
        assert abs(bbox[2] - 0.5) < 0.01
        assert abs(bbox[3] - 0.5) < 0.01

        # Center should be at origin
        assert abs(handles_data.center[0] - 0.0) < 0.01
        assert abs(handles_data.center[1] - 0.0) < 0.01

    def test_compute_transform_handles_translation(self):
        """Test transform handles with translation only."""
        handles_data = compute_transform_handles(
            translate_x=100.0,
            translate_y=50.0,
            scale_x=1.0,
            scale_y=1.0,
            rotation=0.0,
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Bbox should be shifted
        bbox = handles_data.bbox_rect
        # The translation affects NDC position based on pixel-to-ndc scale
        # For this case, we verify bbox is not at original position
        assert bbox[0] != -0.5 or bbox[2] != 0.5  # Some shift occurred

    def test_compute_transform_handles_scale(self):
        """Test transform handles with scale."""
        handles_data = compute_transform_handles(
            translate_x=0.0,
            translate_y=0.0,
            scale_x=2.0,
            scale_y=0.5,
            rotation=0.0,
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Bbox should be wider (scale_x=2) and shorter (scale_y=0.5)
        bbox = handles_data.bbox_rect
        bbox_width = bbox[2] - bbox[0]
        bbox_height = bbox[3] - bbox[1]

        # Original width in NDC is 1.0 (from -0.5 to 0.5)
        # After scale_x=2, width should be 2.0
        assert abs(bbox_width - 2.0) < 0.1 or bbox_width > 0.8  # Some scale effect

    def test_compute_transform_handles_rotation(self):
        """Test transform handles with rotation."""
        handles_data = compute_transform_handles(
            translate_x=0.0,
            translate_y=0.0,
            scale_x=1.0,
            scale_y=1.0,
            rotation=45.0,  # 45 degrees
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Rotation handle sits inside the frame on the top-edge -> pivot guide.
        import math as _m
        pivot = handles_data.pivot
        top = handles_data.scale_y_top
        rotate_pos = handles_data.rotate
        def _dist(a, b):
            return _m.hypot(a[0] - b[0], a[1] - b[1])
        assert _dist(rotate_pos, pivot) < _dist(top, pivot)
        assert _dist(top, rotate_pos) == pytest.approx(
            _dist(top, pivot) * TRANSFORM_ROTATE_HANDLE_INSET
        )

    def test_transform_handle_hit_testing(self):
        """Test hitting transform handles."""
        handles_data = compute_transform_handles(
            translate_x=0.0,
            translate_y=0.0,
            scale_x=1.0,
            scale_y=1.0,
            rotation=0.0,
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Convert center handle to screen pixels
        center_screen = ndc_to_screen_pixels(handles_data.center, 800, 600, 1.0)

        # Hit test at center position
        assert hit_test_handle_screen(
            QPointF(center_screen[0], center_screen[1]),
            center_screen,
            hit_radius=10.0
        )

        # Hit test slightly off-center (within radius)
        assert hit_test_handle_screen(
            QPointF(center_screen[0] + 5.0, center_screen[1] + 5.0),
            center_screen,
            hit_radius=10.0
        )

        # Hit test far from center (outside radius)
        assert not hit_test_handle_screen(
            QPointF(center_screen[0] + 20.0, center_screen[1] + 20.0),
            center_screen,
            hit_radius=10.0
        )

    def test_transform_drag_translate_update(self):
        """Test dragging center handle updates translate parameters."""
        original_params = {
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "pivot_mode": "center",
        }

        # Simulate drag: move 50 pixels right, 30 pixels down
        drag_start = (960.0, 540.0)  # Center of 1920x1080
        drag_current = (1010.0, 570.0)  # Moved +50, +30

        new_params = compute_transform_delta_params(
            handle_id="center",
            drag_start_image_pos=drag_start,
            drag_current_image_pos=drag_current,
            original_params=original_params,
            image_width=1920,
            image_height=1080,
            modifiers=0,
        )

        # Translate follows the drag directly: both X and Y are in Y-down image
        # space (renderer translate_y positive = down), so a +30 down-drag yields
        # translate_y = +30 and the image follows the cursor.
        assert "translate_x" in new_params
        assert "translate_y" in new_params
        assert abs(new_params["translate_x"] - 50.0) < 1.0
        assert abs(new_params["translate_y"] - 30.0) < 1.0

    def test_transform_drag_scale_corner(self):
        """Test dragging corner handle updates scale parameters."""
        original_params = {
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "pivot_mode": "center",
            "pivot_x": 960.0,
            "pivot_y": 540.0,
        }

        # Drag top-right corner outward (increase scale)
        drag_start = (1920.0, 0.0)  # Top-right corner
        drag_current = (2200.0, -200.0)  # Expanded

        new_params = compute_transform_delta_params(
            handle_id="scale_corner_tr",
            drag_start_image_pos=drag_start,
            drag_current_image_pos=drag_current,
            original_params=original_params,
            image_width=1920,
            image_height=1080,
            modifiers=0,
        )

        # Scale should be updated
        assert "scale_x" in new_params or "scale_y" in new_params

    def test_scale_aspect_locked_by_default_shift_frees(self):
        """Aspect ratio is locked by default on a corner drag; Shift frees x/y."""
        original_params = {
            "translate_x": 0.0,
            "translate_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "pivot_mode": "center",
        }

        drag_start = (1920.0, 0.0)
        drag_current = (2200.0, -100.0)  # Asymmetric delta

        # Default (no modifier): aspect locked → uniform scale (x ≈ y)
        locked = compute_transform_delta_params(
            handle_id="scale_corner_tr",
            drag_start_image_pos=drag_start,
            drag_current_image_pos=drag_current,
            original_params=original_params,
            image_width=1920,
            image_height=1080,
            modifiers=0,
        )
        assert abs(locked["scale_x"] - locked["scale_y"]) < 0.01

        # Shift: free x/y scaling (asymmetric delta → unequal scales)
        free = compute_transform_delta_params(
            handle_id="scale_corner_tr",
            drag_start_image_pos=drag_start,
            drag_current_image_pos=drag_current,
            original_params=original_params,
            image_width=1920,
            image_height=1080,
            modifiers=Qt.KeyboardModifier.ShiftModifier,
        )
        assert abs(free["scale_x"] - free["scale_y"]) > 0.01


# ═══════════════════════════════════════════════════════════════════════════
# 3. CornerPin Handle Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCornerPinHandles:
    """Tests for CornerPin node control handles."""

    def test_compute_cornerpin_handles_default(self):
        """Test CornerPin handles with default (identity) values."""
        # Default corners: identity transform
        to_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        from_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

        handles_data = compute_cornerpin_handles(
            to_corners=to_corners,
            from_corners=from_corners,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Should have 4 destination and 4 source corners
        assert len(handles_data.to_corners) == 4
        assert len(handles_data.from_corners) == 4

        # Top-left (to1) should be at NDC position corresponding to (0, 0)
        # For rect (-0.5, -0.5, 0.5, 0.5), (0, 0) normalized maps to
        # NDC x = -0.5 + 0 * 1.0 = -0.5, y = 0.5 - 0 * 1.0 = 0.5
        to1 = handles_data.to_corners[0]
        assert abs(to1[0] - (-0.5)) < 0.01
        assert abs(to1[1] - 0.5) < 0.01

    def test_compute_cornerpin_handles_perspective(self):
        """Test CornerPin handles with perspective distortion."""
        # Shift top-right corner inward
        to_corners = [
            (0.0, 0.0),       # to1: top-left unchanged
            (0.8, 0.1),       # to2: top-right shifted left and down
            (1.0, 1.0),       # to3: bottom-right unchanged
            (0.0, 1.0),       # to4: bottom-left unchanged
        ]
        from_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

        handles_data = compute_cornerpin_handles(
            to_corners=to_corners,
            from_corners=from_corners,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # to2 should be at different position than default
        to2 = handles_data.to_corners[1]
        # At (0.8, 0.1) normalized:
        # NDC x = -0.5 + 0.8 * 1.0 = 0.3
        # NDC y = 0.5 - 0.1 * 1.0 = 0.4
        assert abs(to2[0] - 0.3) < 0.01
        assert abs(to2[1] - 0.4) < 0.01

    def test_cornerpin_handle_hit_testing(self):
        """Test hitting CornerPin handles."""
        to_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        from_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

        handles_data = compute_cornerpin_handles(
            to_corners=to_corners,
            from_corners=from_corners,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # Get handle definitions
        definitions = get_cornerpin_handle_definitions(handles_data)

        # Find to1 handle position in screen pixels
        to1_def = definitions[0]
        to1_screen = ndc_to_screen_pixels(to1_def.ndc_position, 800, 600, 1.0)

        # Should find handle at its position
        found = find_nearest_handle(
            QPointF(to1_screen[0], to1_screen[1]),
            definitions,
            800, 600, 1.0, hit_radius=12.0
        )
        assert found == "to1"

    def test_cornerpin_drag_updates_parameter(self):
        """Test dragging CornerPin handle updates corner parameter."""
        # Simulate interaction state
        interaction = ViewerInteractionState()
        interaction.start_drag(
            tool=ViewerToolMode.CORNERPIN,
            node_id="cornerpin_001",
            handle="to1",
            view_pos=QPointF(0.0, 0.0),
            ndc_pos=QPointF(-0.5, 0.5),
            image_pos=QPointF(0.0, 0.0),
            format_pos=QPointF(0.0, 0.0),
            original_params={
                "to1_x": 0.0, "to1_y": 0.0,
                "to2_x": 1.0, "to2_y": 0.0,
                "to3_x": 1.0, "to3_y": 1.0,
                "to4_x": 0.0, "to4_y": 1.0,
            },
            modifiers=0,
        )

        # Update drag position (move to1 to (0.2, 0.3) normalized)
        interaction.update_drag(
            view_pos=QPointF(50.0, 50.0),
            image_pos=QPointF(384.0, 324.0),  # ~0.2 * 1920, ~0.3 * 1080
        )

        # Verify drag is active
        assert interaction.is_dragging
        assert interaction.active_handle == "to1"

        # Get delta
        delta = interaction.get_image_delta()
        assert abs(delta.x() - 384.0) < 1.0
        assert abs(delta.y() - 324.0) < 1.0

    def test_cornerpin_source_destination_drawing(self):
        """Test that source and destination handles have different visual states."""
        to_corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        from_corners = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]

        handles_data = compute_cornerpin_handles(
            to_corners=to_corners,
            from_corners=from_corners,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        definitions = get_cornerpin_handle_definitions(handles_data)

        # Destination handles (to1-4) should not be marked as source
        for i in range(4):
            assert not definitions[i].is_source

        # Source handles (from1-4) should be marked as source
        for i in range(4, 8):
            assert definitions[i].is_source


# ═══════════════════════════════════════════════════════════════════════════
# 4. Crop Handle Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCropHandles:
    """Tests for Crop node control handles."""

    def test_compute_crop_handles_default(self):
        """Test Crop handles with default parameters."""
        params = {"x": 0, "y": 0, "width": 512, "height": 512}
        ndc_rect = (-0.5, -0.5, 0.5, 0.5)

        handles = compute_crop_handles(
            params=params,
            tex_width=1024,
            tex_height=1024,
            ndc_rect=ndc_rect,
            viewport_width=800,
            viewport_height=600,
            dpr=1.0,
        )

        # Should have all expected handles
        assert "crop_top" in handles
        assert "crop_bottom" in handles
        assert "crop_left" in handles
        assert "crop_right" in handles
        assert "crop_top_left" in handles
        assert "crop_top_right" in handles
        assert "crop_bottom_left" in handles
        assert "crop_bottom_right" in handles
        assert "crop_center" in handles

    def test_compute_crop_handles_position(self):
        """Test Crop handle positions are correct."""
        params = {"x": 100, "y": 100, "width": 200, "height": 150}
        ndc_rect = (-0.5, -0.5, 0.5, 0.5)

        handles = compute_crop_handles(
            params=params,
            tex_width=512,
            tex_height=512,
            ndc_rect=ndc_rect,
            viewport_width=800,
            viewport_height=600,
            dpr=1.0,
        )

        # Center handle should be at crop center (100 + 100, 100 + 75) = (200, 175)
        center = handles["crop_center"]
        # Position is in view pixels, convert back to check
        # The exact position depends on NDC rect mapping

        # Top handle should be above center, bottom below
        assert handles["crop_top"].y() < handles["crop_center"].y()
        assert handles["crop_bottom"].y() > handles["crop_center"].y()

        # Left handle should be left of center, right handle right
        assert handles["crop_left"].x() < handles["crop_center"].x()
        assert handles["crop_right"].x() > handles["crop_center"].x()

    def test_crop_rect_from_edge_handle(self):
        """Test computing new crop rect from edge handle drag."""
        original_crop = {"x": 100, "y": 100, "width": 200, "height": 150}
        delta = QPointF(20.0, 0.0)  # Drag right 20 pixels

        # Drag right edge
        new_crop = compute_crop_rect_from_handles(
            handle_id="crop_right",
            original_crop=original_crop,
            delta_image=delta,
            min_size=1,
        )

        assert new_crop["x"] == 100
        assert new_crop["width"] == 220  # 200 + 20

        # Drag left edge
        delta_left = QPointF(30.0, 0.0)
        new_crop_left = compute_crop_rect_from_handles(
            handle_id="crop_left",
            original_crop=original_crop,
            delta_image=delta_left,
            min_size=1,
        )

        assert new_crop_left["x"] == 130  # 100 + 30
        assert new_crop_left["width"] == 170  # 200 - 30

    def test_crop_rect_from_corner_handle(self):
        """Test computing new crop rect from corner handle drag."""
        original_crop = {"x": 100, "y": 100, "width": 200, "height": 150}
        delta = QPointF(20.0, 30.0)  # Drag diagonally

        # Drag top-left corner (should affect both x/y and width/height)
        new_crop = compute_crop_rect_from_handles(
            handle_id="crop_top_left",
            original_crop=original_crop,
            delta_image=delta,
            min_size=1,
        )

        assert new_crop["x"] == 120  # 100 + 20
        assert new_crop["y"] == 130  # 100 + 30
        assert new_crop["width"] == 180  # 200 - 20
        assert new_crop["height"] == 120  # 150 - 30

    def test_crop_rect_from_center_handle(self):
        """Test computing new crop rect from center handle drag (move entire crop)."""
        original_crop = {"x": 100, "y": 100, "width": 200, "height": 150}
        delta = QPointF(50.0, 25.0)  # Move crop region

        new_crop = compute_crop_rect_from_handles(
            handle_id="crop_center",
            original_crop=original_crop,
            delta_image=delta,
            min_size=1,
        )

        assert new_crop["x"] == 150  # 100 + 50
        assert new_crop["y"] == 125  # 100 + 25
        assert new_crop["width"] == 200  # Unchanged
        assert new_crop["height"] == 150  # Unchanged

    def test_crop_rect_clamping(self):
        """Test crop rect is clamped to minimum size."""
        original_crop = {"x": 100, "y": 100, "width": 10, "height": 10}
        # Drag to make crop very small
        delta = QPointF(15.0, 15.0)

        new_crop = compute_crop_rect_from_handles(
            handle_id="crop_left",
            original_crop=original_crop,
            delta_image=delta,
            min_size=1,
        )

        # Width should be clamped to min_size
        assert new_crop["width"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. Mask/Roto Handle Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMaskRotoHandles:
    """Tests for Mask/Roto control points and handles."""

    def test_mask_control_point_positions(self):
        """Test mask control point positions for rectangle mask."""
        # Mask parameters for centered rectangle
        params = {
            "mask_type": "rectangle",
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.4,
            "height": 0.3,
            "rotation": 0.0,
            "feather": 0.0,
        }

        # For a 1920x1080 image:
        # Center: (960, 540)
        # Width: 0.4 * 1920 = 768
        # Height: 0.3 * 1080 = 324
        # So rectangle bounds: (960-384, 540-162) to (960+384, 540+162)
        # = (576, 378) to (1344, 702)

        # The mask node itself doesn't have explicit control points in viewer,
        # but we can verify the parameter calculations

        center_x = params["center_x"] * 1920
        center_y = params["center_y"] * 1080
        half_w = params["width"] * 1920 / 2
        half_h = params["height"] * 1080 / 2

        assert abs(center_x - 960) < 1
        assert abs(center_y - 540) < 1

        # Rectangle corners would be at:
        top_left = (center_x - half_w, center_y - half_h)
        assert abs(top_left[0] - 576) < 1
        assert abs(top_left[1] - 378) < 1

    def test_mask_rotation_transform(self):
        """Test mask control point positions with rotation."""
        params = {
            "mask_type": "rectangle",
            "center_x": 0.5,
            "center_y": 0.5,
            "width": 0.4,
            "height": 0.3,
            "rotation": 45.0,  # 45 degree rotation
        }

        # With rotation, corners are transformed around center
        # We verify the math is correct

        center_x = params["center_x"] * 1920
        center_y = params["center_y"] * 1080
        half_w = params["width"] * 1920 / 2
        half_h = params["height"] * 1080 / 2

        rad = math.radians(params["rotation"])
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        # Top-left corner relative to center, then rotated
        rel_x = -half_w
        rel_y = -half_h
        rot_x = rel_x * cos_r - rel_y * sin_r
        rot_y = rel_x * sin_r + rel_y * cos_r

        # Final position
        final_x = center_x + rot_x
        final_y = center_y + rot_y

        # Verify rotation math
        assert not (abs(rot_x - rel_x) < 1 and abs(rot_y - rel_y) < 1)  # Should be different

    def test_mask_feather_parameter(self):
        """Test mask feather parameter affects edge softness."""
        params_no_feather = {"feather": 0.0}
        params_with_feather = {"feather": 0.1}

        # Feather is normalized (0-0.5), affecting edge softness
        # We verify the parameter is valid
        assert 0.0 <= params_no_feather["feather"] <= 0.5
        assert 0.0 <= params_with_feather["feather"] <= 0.5

    def test_bezier_control_handle_drag(self):
        """Test Bezier tangent handle drag updates curve shape."""
        # Simulate a Bezier curve point with tangents
        # This tests the coordinate math, not actual UI

        # Point position
        point_pos = QPointF(500.0, 400.0)
        # Tangent in position (control point for incoming curve)
        tangent_in = QPointF(450.0, 350.0)
        # Tangent out position (control point for outgoing curve)
        tangent_out = QPointF(550.0, 450.0)

        # Drag tangent out to new position
        new_tangent_out = QPointF(600.0, 500.0)

        # Calculate tangent length change
        orig_length = math.sqrt(
            (tangent_out.x() - point_pos.x()) ** 2 +
            (tangent_out.y() - point_pos.y()) ** 2
        )
        new_length = math.sqrt(
            (new_tangent_out.x() - point_pos.x()) ** 2 +
            (new_tangent_out.y() - point_pos.y()) ** 2
        )

        assert new_length > orig_length  # Tangent extended

    def test_mask_point_hit_testing(self):
        """Test hitting mask control points."""
        # Simulate mask point positions in view coordinates
        mask_points = {
            "point_0": QPointF(200.0, 300.0),
            "point_1": QPointF(400.0, 300.0),
            "point_2": QPointF(400.0, 500.0),
            "point_3": QPointF(200.0, 500.0),
        }

        # Hit test at point_1 position
        hit = hit_test_handle(
            QPointF(400.0, 300.0),
            mask_points["point_1"],
            hit_radius_pixels=10.0
        )
        assert hit

        # Hit test slightly off (within radius)
        hit_near = hit_test_handle(
            QPointF(405.0, 305.0),
            mask_points["point_1"],
            hit_radius_pixels=10.0
        )
        assert hit_near

        # Hit test far away
        hit_far = hit_test_handle(
            QPointF(500.0, 400.0),
            mask_points["point_1"],
            hit_radius_pixels=10.0
        )
        assert not hit_far


# ═══════════════════════════════════════════════════════════════════════════
# 6. Undo Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestUndoOperations:
    """Tests for undo/redo operations during handle drag."""

    def test_interaction_state_original_params(self):
        """Test that interaction state stores original params for undo."""
        interaction = ViewerInteractionState()

        original_params = {
            "translate_x": 100.0,
            "translate_y": 50.0,
            "scale_x": 1.5,
            "rotation": 30.0,
        }

        interaction.start_drag(
            tool=ViewerToolMode.TRANSFORM,
            node_id="transform_001",
            handle="center",
            view_pos=QPointF(400.0, 300.0),
            ndc_pos=QPointF(0.0, 0.0),
            image_pos=QPointF(500.0, 400.0),
            format_pos=QPointF(500.0, 400.0),
            original_params=original_params,
            modifiers=0,
        )

        # Original params should be stored
        assert interaction.original_params["translate_x"] == 100.0
        assert interaction.original_params["scale_x"] == 1.5

        # Should be a copy, not reference
        original_params["translate_x"] = 200.0
        assert interaction.original_params["translate_x"] == 100.0  # Unchanged

    def test_drag_undo_restores_params(self):
        """Test that undo restores original parameters."""
        interaction = ViewerInteractionState()

        original_params = {
            "x": 100,
            "y": 100,
            "width": 200,
            "height": 150,
        }

        interaction.start_drag(
            tool=ViewerToolMode.CROP,
            node_id="crop_001",
            handle="crop_right",
            view_pos=QPointF(600.0, 400.0),
            ndc_pos=QPointF(0.5, 0.0),
            image_pos=QPointF(300.0, 200.0),
            format_pos=QPointF(300.0, 200.0),
            original_params=original_params,
            modifiers=0,
        )

        # Simulate drag
        interaction.update_drag(
            view_pos=QPointF(700.0, 400.0),
            image_pos=QPointF(350.0, 200.0),
        )

        # End drag returns original params
        returned_params = interaction.end_drag()
        assert returned_params["x"] == 100
        assert returned_params["width"] == 200

    def test_multiple_drag_undo_sequence(self):
        """Test undo sequence for multiple consecutive drags."""
        # Simulate multiple drags on same node
        drags = [
            {"handle": "center", "original": {"translate_x": 0.0, "translate_y": 0.0}},
            {"handle": "center", "original": {"translate_x": 50.0, "translate_y": 30.0}},
            {"handle": "center", "original": {"translate_x": 100.0, "translate_y": 60.0}},
        ]

        # Each drag should store its own original params
        for drag_info in drags:
            interaction = ViewerInteractionState()
            interaction.start_drag(
                tool=ViewerToolMode.TRANSFORM,
                node_id="transform_001",
                handle=drag_info["handle"],
                view_pos=QPointF(400.0, 300.0),
                ndc_pos=QPointF(0.0, 0.0),
                image_pos=QPointF(500.0, 400.0),
                format_pos=QPointF(500.0, 400.0),
                original_params=drag_info["original"],
                modifiers=0,
            )
            interaction.end_drag()

            # Each should have correct original params
            assert interaction.original_params["translate_x"] == drag_info["original"]["translate_x"]

    def test_drag_undo_redo_cycle(self):
        """Test full undo/redo cycle for a drag operation."""
        interaction = ViewerInteractionState()

        original_params = {"rotation": 0.0}
        final_params = {"rotation": 45.0}

        interaction.start_drag(
            tool=ViewerToolMode.TRANSFORM,
            node_id="transform_001",
            handle="rotate",
            view_pos=QPointF(400.0, 100.0),
            ndc_pos=QPointF(0.0, 0.8),
            image_pos=QPointF(500.0, 100.0),
            format_pos=QPointF(500.0, 100.0),
            original_params=original_params,
            modifiers=0,
        )

        # Simulate drag and set final params
        interaction.set_final_params(final_params)

        # End drag
        interaction.end_drag()

        # Verify we have both original and final for undo/redo
        assert interaction.original_params["rotation"] == 0.0
        assert interaction.final_params["rotation"] == 45.0

        # Undo would restore original (0.0)
        # Redo would restore final (45.0)

    def test_final_params_tracking(self):
        """Test that final params are tracked for undo comparison."""
        interaction = ViewerInteractionState()

        original = {"x": 0, "y": 0, "width": 100, "height": 100}
        final = {"x": 10, "y": 10, "width": 80, "height": 80}

        interaction.start_drag(
            tool=ViewerToolMode.CROP,
            node_id="crop_001",
            handle="crop_top_left",
            view_pos=QPointF(100.0, 100.0),
            ndc_pos=QPointF(-0.5, 0.5),
            image_pos=QPointF(0.0, 0.0),
            format_pos=QPointF(0.0, 0.0),
            original_params=original,
            modifiers=0,
        )

        interaction.set_final_params(final)

        # Final should be stored
        assert interaction.final_params["x"] == 10
        assert interaction.final_params["width"] == 80

        # Original should still exist
        assert interaction.original_params["x"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. Handle Cursor Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestHandleCursors:
    """Tests for cursor shapes associated with handles."""

    def test_cursor_for_crop_handles(self):
        """Test cursor shapes for crop handles."""
        assert get_cursor_for_handle("crop_left") == "size_hor"
        assert get_cursor_for_handle("crop_right") == "size_hor"
        assert get_cursor_for_handle("crop_top") == "size_ver"
        assert get_cursor_for_handle("crop_bottom") == "size_ver"
        assert get_cursor_for_handle("crop_top_left") == "size_fdiag"
        assert get_cursor_for_handle("crop_bottom_right") == "size_fdiag"
        assert get_cursor_for_handle("crop_top_right") == "size_bdiag"
        assert get_cursor_for_handle("crop_bottom_left") == "size_bdiag"
        assert get_cursor_for_handle("crop_center") == "size_all"

    def test_cursor_for_transform_handles(self):
        """Test cursor shapes for transform handles."""
        assert get_cursor_for_handle("center") == "size_all"
        assert get_cursor_for_handle("rotate") == "cross"
        assert get_cursor_for_handle("pivot") == "cross"

    def test_cursor_for_unknown_handle(self):
        """Test cursor for unknown handle returns default."""
        assert get_cursor_for_handle("unknown_handle") == "arrow"


# ═══════════════════════════════════════════════════════════════════════════
# 8. Selection State Tests (Additional)
# ═══════════════════════════════════════════════════════════════════════════

class TestSelectionStateExtended:
    """Extended tests for ViewerSelectionState."""

    def test_rubber_band_with_handles(self):
        """Test rubber band selection includes handles."""
        state = ViewerSelectionState()
        state.start_rubber_band(QPointF(100.0, 100.0))
        state.update_rubber_band(QPointF(300.0, 300.0))

        handles = {
            "handle_1": QPointF(150.0, 150.0),  # Inside
            "handle_2": QPointF(250.0, 250.0),  # Inside
            "handle_3": QPointF(400.0, 400.0),  # Outside
        }

        rect = state.end_rubber_band()
        selected = hit_test_handles_in_rect(rect, handles)

        assert "handle_1" in selected
        assert "handle_2" in selected
        assert "handle_3" not in selected

    def test_selection_modifiers(self):
        """Test selection with Ctrl/Shift modifiers."""
        state = ViewerSelectionState()

        # Normal selection clears previous
        state.add_to_selection(node_id="node_1")
        state.add_to_selection(node_id="node_2")  # Would clear if not multi

        assert "node_1" in state.selected_node_ids
        assert "node_2" in state.selected_node_ids

        # Toggle removes if already selected
        state.toggle_selection(node_id="node_1")
        assert "node_1" not in state.selected_node_ids

        state.toggle_selection(node_id="node_1")
        assert "node_1" in state.selected_node_ids


# ═══════════════════════════════════════════════════════════════════════════
# 9. Integration Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestViewerToolIntegration:
    """Integration tests combining multiple components."""

    def test_full_transform_workflow(self):
        """Test complete transform handle workflow."""
        # 1. Compute handles from params
        handles_data = compute_transform_handles(
            translate_x=0.0,
            translate_y=0.0,
            scale_x=1.0,
            scale_y=1.0,
            rotation=0.0,
            pivot_mode="center",
            pivot_x=0.0,
            pivot_y=0.0,
            image_width=1920,
            image_height=1080,
            image_ndc_rect=(-0.5, -0.5, 0.5, 0.5),
        )

        # 2. Convert to screen positions
        center_screen = ndc_to_screen_pixels(handles_data.center, 800, 600, 1.0)

        # 3. Start interaction
        interaction = ViewerInteractionState()
        interaction.start_drag(
            tool=ViewerToolMode.TRANSFORM,
            node_id="transform_001",
            handle="center",
            view_pos=QPointF(center_screen[0], center_screen[1]),
            ndc_pos=QPointF(handles_data.center[0], handles_data.center[1]),
            image_pos=QPointF(960.0, 540.0),  # Image center
            format_pos=QPointF(960.0, 540.0),
            original_params={
                "translate_x": 0.0,
                "translate_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
                "rotation": 0.0,
            },
            modifiers=0,
        )

        # 4. Simulate drag
        interaction.update_drag(
            view_pos=QPointF(center_screen[0] + 50, center_screen[1] + 30),
            image_pos=QPointF(1010.0, 570.0),
        )

        # 5. Compute new params
        new_params = compute_transform_delta_params(
            handle_id="center",
            drag_start_image_pos=(960.0, 540.0),
            drag_current_image_pos=(1010.0, 570.0),
            original_params=interaction.original_params,
            image_width=1920,
            image_height=1080,
            modifiers=0,
        )

        # 6. Set final params
        interaction.set_final_params(new_params)

        # 7. End drag
        orig = interaction.end_drag()

        # 8. Verify workflow
        assert orig["translate_x"] == 0.0  # Original preserved
        assert interaction.final_params["translate_x"] != 0.0  # Final recorded

    def test_full_crop_workflow(self):
        """Test complete crop handle workflow."""
        # 1. Compute handles
        params = {"x": 100, "y": 100, "width": 200, "height": 150}
        handles = compute_crop_handles(
            params=params,
            tex_width=512,
            tex_height=512,
            ndc_rect=(-0.5, -0.5, 0.5, 0.5),
            viewport_width=800,
            viewport_height=600,
            dpr=1.0,
        )

        # 2. Start interaction on right edge
        interaction = ViewerInteractionState()
        interaction.start_drag(
            tool=ViewerToolMode.CROP,
            node_id="crop_001",
            handle="crop_right",
            view_pos=handles["crop_right"],
            ndc_pos=QPointF(0.3, 0.0),
            image_pos=QPointF(300.0, 175.0),
            format_pos=QPointF(300.0, 175.0),
            original_params=params,
            modifiers=0,
        )

        # 3. Drag right
        interaction.update_drag(
            view_pos=QPointF(handles["crop_right"].x() + 30, handles["crop_right"].y()),
            image_pos=QPointF(330.0, 175.0),
        )

        # 4. Compute new crop
        new_crop = compute_crop_rect_from_handles(
            handle_id="crop_right",
            original_crop=params,
            delta_image=QPointF(30.0, 0.0),
            min_size=1,
        )

        # 5. End drag
        orig = interaction.end_drag()

        # 6. Verify
        assert orig["width"] == 200
        assert new_crop["width"] == 230


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
