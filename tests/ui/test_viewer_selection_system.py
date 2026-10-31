"""
Tests for Viewer Selection and Hit Testing System (S5-10).

Tests the new selection and hit testing functionality added to
viewer_tool_system.py and composite_viewport.py.
"""

import pytest
from PyQt6.QtCore import QPointF

from src.features.composite.ui.viewer_tool_system import (
    NodeOutputRegion,
    ViewerSelectionState,
    hit_test_node_output,
    hit_test_handles_in_rect,
    hit_test_handle,
    hit_test_rect,
)


class TestNodeOutputRegion:
    """Tests for NodeOutputRegion dataclass."""

    def test_create_region(self):
        """Test creating a NodeOutputRegion."""
        region = NodeOutputRegion(
            node_id="test_node_001",
            node_type="merge",
            bbox=(100.0, 200.0, 300.0, 400.0),  # x, y, width, height
            label="Merge 1",
            z_order=5,
        )

        assert region.node_id == "test_node_001"
        assert region.node_type == "merge"
        assert region.bbox == (100.0, 200.0, 300.0, 400.0)
        assert region.label == "Merge 1"
        assert region.z_order == 5

    def test_region_defaults(self):
        """Test default values for optional fields."""
        region = NodeOutputRegion(
            node_id="node_002",
            node_type="transform",
            bbox=(0.0, 0.0, 1920.0, 1080.0),
        )

        assert region.label is None
        assert region.z_order == 0


class TestHitTestNodeOutput:
    """Tests for hit_test_node_output function."""

    def test_hit_inside_region(self):
        """Test hit when position is inside a region."""
        regions = [
            NodeOutputRegion("node_1", "merge", (100.0, 100.0, 200.0, 200.0)),
            NodeOutputRegion("node_2", "transform", (400.0, 400.0, 100.0, 100.0)),
        ]

        # Point inside first region
        hit = hit_test_node_output(QPointF(150.0, 150.0), regions)
        assert hit is not None
        assert hit.node_id == "node_1"

        # Point inside second region
        hit = hit_test_node_output(QPointF(450.0, 450.0), regions)
        assert hit is not None
        assert hit.node_id == "node_2"

    def test_hit_outside_all_regions(self):
        """Test hit when position is outside all regions."""
        regions = [
            NodeOutputRegion("node_1", "merge", (100.0, 100.0, 200.0, 200.0)),
        ]

        # Point outside
        hit = hit_test_node_output(QPointF(500.0, 500.0), regions)
        assert hit is None

        # Point exactly at edge (should be outside, since we use < for width/height)
        hit = hit_test_node_output(QPointF(100.0 + 200.0, 100.0), regions)  # x = 300
        assert hit is None

    def test_hit_empty_list(self):
        """Test hit with empty region list."""
        hit = hit_test_node_output(QPointF(100.0, 100.0), [])
        assert hit is None

    def test_hit_z_order_priority(self):
        """Test that higher z_order regions are returned when overlapping."""
        regions = [
            NodeOutputRegion("node_low", "merge", (100.0, 100.0, 200.0, 200.0), z_order=1),
            NodeOutputRegion("node_high", "transform", (100.0, 100.0, 200.0, 200.0), z_order=5),
        ]

        # Should return node_high due to prefer_top=True
        hit = hit_test_node_output(QPointF(150.0, 150.0), regions, prefer_top=True)
        assert hit is not None
        assert hit.node_id == "node_high"

        # Should return node_low if prefer_top=False
        hit = hit_test_node_output(QPointF(150.0, 150.0), regions, prefer_top=False)
        assert hit is not None
        assert hit.node_id == "node_low"


class TestHitTestHandlesInRect:
    """Tests for hit_test_handles_in_rect function."""

    def test_handles_inside_rect(self):
        """Test finding handles inside a selection rectangle."""
        handles = {
            "handle_1": QPointF(100.0, 100.0),
            "handle_2": QPointF(200.0, 100.0),
            "handle_3": QPointF(100.0, 200.0),
            "handle_4": QPointF(300.0, 300.0),  # Outside
        }

        # Rect covering first three handles
        rect = (50.0, 50.0, 250.0, 250.0)
        selected = hit_test_handles_in_rect(rect, handles)

        assert len(selected) == 3
        assert "handle_1" in selected
        assert "handle_2" in selected
        assert "handle_3" in selected
        assert "handle_4" not in selected

    def test_handles_empty(self):
        """Test with empty handles dict."""
        rect = (50.0, 50.0, 250.0, 250.0)
        selected = hit_test_handles_in_rect(rect, {})
        assert selected == []

    def test_handles_with_hit_radius(self):
        """Test hit radius expansion."""
        handles = {
            "handle_1": QPointF(100.0, 100.0),
        }

        # Rect that doesn't contain handle, but with hit_radius it should be included
        rect = (90.0, 90.0, 95.0, 95.0)  # Handle is at (100, 100), rect ends at (95, 95)
        selected = hit_test_handles_in_rect(rect, handles, hit_radius_pixels=10.0)
        assert "handle_1" in selected  # Should be included due to radius

    def test_handles_reversed_rect(self):
        """Test that rect is normalized when coordinates are reversed."""
        handles = {
            "handle_1": QPointF(150.0, 150.0),
        }

        # Rect with reversed coordinates (x1 < x0)
        rect = (250.0, 250.0, 50.0, 50.0)
        selected = hit_test_handles_in_rect(rect, handles)
        assert "handle_1" in selected  # Should still work after normalization


class TestViewerSelectionState:
    """Tests for ViewerSelectionState dataclass."""

    def test_initial_state(self):
        """Test initial state is empty."""
        state = ViewerSelectionState()
        assert len(state.selected_node_ids) == 0
        assert len(state.selected_handles) == 0
        assert state.hovered_node_id is None
        assert state.hovered_handle is None
        assert not state.rubber_band_active

    def test_clear_selection(self):
        """Test clearing selection."""
        state = ViewerSelectionState()
        state.selected_node_ids.add("node_1")
        state.selected_handles.add("handle_1")

        state.clear_selection()
        assert len(state.selected_node_ids) == 0
        assert len(state.selected_handles) == 0

    def test_rubber_band(self):
        """Test rubber band lifecycle."""
        state = ViewerSelectionState()

        # Start rubber band
        state.start_rubber_band(QPointF(100.0, 100.0))
        assert state.rubber_band_active
        assert state.rubber_band_start == QPointF(100.0, 100.0)
        assert state.rubber_band_end == QPointF(100.0, 100.0)

        # Update
        state.update_rubber_band(QPointF(200.0, 200.0))
        assert state.rubber_band_end == QPointF(200.0, 200.0)

        # End
        rect = state.end_rubber_band()
        assert not state.rubber_band_active
        assert rect == (100.0, 100.0, 200.0, 200.0)

    def test_rubber_band_normalized(self):
        """Test rubber band returns normalized rect."""
        state = ViewerSelectionState()

        # Start and end with reversed coordinates
        state.start_rubber_band(QPointF(200.0, 200.0))
        state.update_rubber_band(QPointF(100.0, 100.0))
        rect = state.end_rubber_band()

        # Should be normalized
        assert rect == (100.0, 100.0, 200.0, 200.0)

    def test_add_to_selection(self):
        """Test adding to selection."""
        state = ViewerSelectionState()

        state.add_to_selection(node_id="node_1")
        assert "node_1" in state.selected_node_ids

        state.add_to_selection(handle_id="handle_1")
        assert "handle_1" in state.selected_handles

    def test_remove_from_selection(self):
        """Test removing from selection."""
        state = ViewerSelectionState()
        state.selected_node_ids.add("node_1")
        state.selected_handles.add("handle_1")

        state.remove_from_selection(node_id="node_1")
        assert "node_1" not in state.selected_node_ids

        state.remove_from_selection(handle_id="handle_1")
        assert "handle_1" not in state.selected_handles

    def test_toggle_selection(self):
        """Test toggling selection."""
        state = ViewerSelectionState()

        # Toggle on
        state.toggle_selection(node_id="node_1")
        assert "node_1" in state.selected_node_ids

        # Toggle off
        state.toggle_selection(node_id="node_1")
        assert "node_1" not in state.selected_node_ids


class TestHitTestHandle:
    """Tests for hit_test_handle function."""

    def test_hit_inside_radius(self):
        """Test hit when within radius."""
        view_pos = QPointF(100.0, 100.0)
        handle_pos = QPointF(105.0, 105.0)  # Distance ~7.07, within radius 10

        assert hit_test_handle(view_pos, handle_pos, hit_radius_pixels=10.0)

    def test_hit_outside_radius(self):
        """Test no hit when outside radius."""
        view_pos = QPointF(100.0, 100.0)
        handle_pos = QPointF(120.0, 120.0)  # Distance ~28.28, outside radius 10

        assert not hit_test_handle(view_pos, handle_pos, hit_radius_pixels=10.0)

    def test_hit_exact_boundary(self):
        """Test hit at exact boundary."""
        view_pos = QPointF(100.0, 100.0)
        handle_pos = QPointF(110.0, 100.0)  # Distance exactly 10

        assert hit_test_handle(view_pos, handle_pos, hit_radius_pixels=10.0)


class TestHitTestRect:
    """Tests for hit_test_rect function."""

    def test_hit_inside_rect(self):
        """Test hit inside rectangle."""
        view_pos = QPointF(150.0, 150.0)
        rect = (100.0, 100.0, 200.0, 200.0)

        assert hit_test_rect(view_pos, rect)

    def test_hit_outside_rect(self):
        """Test hit outside rectangle."""
        view_pos = QPointF(300.0, 300.0)
        rect = (100.0, 100.0, 200.0, 200.0)

        assert not hit_test_rect(view_pos, rect)

    def test_hit_on_boundary(self):
        """Test hit on boundary (should be inside)."""
        view_pos = QPointF(100.0, 100.0)  # On top-left corner
        rect = (100.0, 100.0, 200.0, 200.0)

        assert hit_test_rect(view_pos, rect)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])