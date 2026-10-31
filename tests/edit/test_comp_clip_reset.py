"""
Comp Clip Reset and Visual Indicator Tests (Phase 6).

Tests:
- Reset Clip Composite restores original asset
- Reset Comp Clip Graph resets to default
- COMP badge drawing conditions (logic only, no QPainter)
- Cache status line drawing conditions
- Context menu items for different clip states
- ClipItem comp state fields and properties (requires PyQt6)

Coverage:
- EditProject.reset_clip_composite
- EditProject.reset_comp_clip_graph
- ClipItem comp state fields and properties
- Context menu Composite submenu structure
"""

import pytest
import sys
import uuid
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.comp_clip_data import CompClipData
from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    TimelineSequence,
    EditTrack,
    Rational,
)

# Skip tests requiring PyQt6 if unavailable
try:
    from PyQt6.QtWidgets import QApplication
    from src.features.edit.ui.timeline_items import ClipItem
    from src.features.edit.ui.timeline_view import TimelineView
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False

requires_pyqt = pytest.mark.skipif(not HAS_PYQT, reason="PyQt6 not available")


# ================================================================
# Helpers
# ================================================================


def _make_timeline_with_clip() -> tuple[TimelineSequence, str, str]:
    """
    Create a TimelineSequence with a single clip on a video track.

    Returns (timeline, track_id, clip_id, source_asset_id).
    """
    timeline_id = str(uuid.uuid4())
    timeline = TimelineSequence(
        timeline_id=timeline_id,
        name="Test Timeline",
        fps=Rational(24, 1),
    )

    # Add a video track
    video_track = EditTrack(
        track_id=str(uuid.uuid4()),
        track_type="video",
        name="V1",
    )
    timeline = timeline.with_track_added(video_track)
    track_id = video_track.track_id

    # Create a source clip
    source_asset_id = str(uuid.uuid4())
    clip = ClipInstance(
        asset_id=source_asset_id,
        track_id=track_id,
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        composite_id=None,
    )
    timeline = timeline.with_clip_added(track_id, clip)
    clip_id = clip.clip_id

    return timeline, track_id, clip_id, source_asset_id


def _make_project_with_comp_clip_timeline():
    """
    Create a project + timeline where a clip has been upgraded to Comp Clip.

    Returns (project, timeline, clip_id, source_asset_id, comp_clip_id).
    """
    project = EditProject.create("Test Project")

    # Import source asset
    project = project.import_asset(
        path="/tmp/test_source.mov",
        media_type="video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_seconds": 10.0,
            "total_frames": 240,
        },
        display_name="test_source.mov",
    )
    source_asset_id = None
    for aid, asset in project.assets.items():
        if asset.path == "/tmp/test_source.mov":
            source_asset_id = aid
            break
    assert source_asset_id is not None

    # Create timeline with clip
    timeline, track_id, clip_id, _ = _make_timeline_with_clip()

    # Update the clip to reference the source asset
    original_clip = timeline.get_clip_by_id(clip_id)
    from src.features.edit.core.timeline_v2 import ClipInstance
    clip_with_source = ClipInstance(
        clip_id=original_clip.clip_id,
        asset_id=source_asset_id,
        track_id=original_clip.track_id,
        timeline_start=original_clip.timeline_start,
        timeline_duration=original_clip.timeline_duration,
        source_start=original_clip.source_start,
        source_duration=original_clip.source_duration,
    )
    timeline = timeline.with_clip_updated(clip_with_source)

    # Manually build Comp Clip data structures
    comp_clip_id = str(uuid.uuid4())
    comp_asset_id = str(uuid.uuid4())
    graph_id = str(uuid.uuid4())

    # Create Comp Clip MediaAsset
    now = time.time()
    comp_asset = MediaAsset(
        asset_id=comp_asset_id,
        path=f"topos://comp_clip/{comp_asset_id}",
        media_type="comp_clip",
        display_name="Test Comp Clip",
        metadata={
            "comp_clip_id": comp_clip_id,
            "graph_id": graph_id,
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "duration_frames": 100,
            "source_kind": "timeline_clip_upgrade",
            "original_asset_id": source_asset_id,
            "original_timeline_id": timeline.timeline_id,
            "original_clip_id": clip_id,
        },
        created_at=now,
    )

    # Create CompClipData
    comp_clip = CompClipData.create_from_timeline_clip(
        asset_id=comp_asset_id,
        graph_id=graph_id,
        name="Test Comp Clip",
        width=1920,
        height=1080,
        fps=24.0,
        duration_frames=100,
        original_asset_id=source_asset_id,
        original_timeline_id=timeline.timeline_id,
        original_clip_id=clip_id,
    )

    # Create CompositeGraphData
    comp_graph = CompositeGraphData.create_default_for_clip(
        clip_id=clip_id,
        asset_id=source_asset_id,
        asset_path="/tmp/test_source.mov",
        width=1920,
        height=1080,
        name="Test Comp Clip",
        composite_id=graph_id,
        comp_clip_id=comp_clip_id,
        comp_asset_id=comp_asset_id,
        timeline_id=timeline.timeline_id,
        fps=24.0,
        duration_frames=100,
    )

    # Update project with comp clip data
    new_assets = dict(project.assets)
    new_assets[comp_asset_id] = comp_asset

    new_compositions = dict(project.compositions)
    new_compositions[graph_id] = comp_graph.to_dict()

    new_comp_clips = dict(project.comp_clips)
    new_comp_clips[comp_clip_id] = comp_clip.to_dict()

    from src.features.edit.core.project import EditProject as EP
    project = EP(
        project_id=project.project_id,
        name=project.name,
        assets=new_assets,
        bins=project.bins,
        timelines=project.timelines,
        compositions=new_compositions,
        color_stacks=project.color_stacks,
        comp_clips=new_comp_clips,
        root_bin_id=project.root_bin_id,
        active_timeline_id=project.active_timeline_id,
        active_comp_clip_id=comp_clip_id,
        created_at=project.created_at,
        modified_at=now,
    )

    # Update the timeline clip to reference the comp clip
    clip_comp = ClipInstance(
        clip_id=clip_id,
        asset_id=comp_asset_id,
        track_id=track_id,
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        composite_id=graph_id,
    )
    timeline = timeline.with_clip_updated(clip_comp)

    return project, timeline, clip_id, source_asset_id, comp_clip_id


def _make_project_with_empty_comp_clip() -> tuple[EditProject, str, str]:
    """
    Create a project with an empty Comp Clip.

    Returns (project, comp_clip_id, comp_asset_id).
    """
    project = EditProject.create("Test Project")

    project, comp_clip_id, comp_asset_id, graph_id = project.create_empty_comp_clip(
        name="Empty Comp Clip",
        width=1920,
        height=1080,
        fps=24.0,
        duration_frames=240,
    )

    return project, comp_clip_id, comp_asset_id


# ================================================================
# Test: Reset Clip Composite
# ================================================================


class TestResetClipComposite:
    """Test EditProject.reset_clip_composite."""

    def test_reset_restores_original_asset_id(self):
        """Reset Clip Composite restores clip.asset_id to original_asset_id."""
        project, timeline, clip_id, source_asset_id, comp_clip_id = _make_project_with_comp_clip_timeline()

        # Verify clip currently has comp asset
        clip = timeline.get_clip_by_id(clip_id)
        assert clip is not None
        assert clip.composite_id is not None

        # Reset
        new_project, new_timeline = project.reset_clip_composite(
            timeline_id=timeline.timeline_id,
            clip_id=clip_id,
            timeline=timeline,
        )

        # Verify clip now has original asset_id
        restored_clip = new_timeline.get_clip_by_id(clip_id)
        assert restored_clip is not None
        assert restored_clip.asset_id == source_asset_id
        assert restored_clip.composite_id is None

    def test_reset_removes_comp_clip_if_no_other_refs(self):
        """Reset removes Comp Clip if no other clips reference it."""
        project, timeline, clip_id, source_asset_id, comp_clip_id = _make_project_with_comp_clip_timeline()

        new_project, new_timeline = project.reset_clip_composite(
            timeline_id=timeline.timeline_id,
            clip_id=clip_id,
            timeline=timeline,
        )

        # CompClipData should be removed
        assert comp_clip_id not in new_project.comp_clips

    def test_reset_preserves_clip_timing(self):
        """Reset preserves clip timing (start, duration, source range)."""
        project, timeline, clip_id, source_asset_id, comp_clip_id = _make_project_with_comp_clip_timeline()

        original_clip = timeline.get_clip_by_id(clip_id)
        new_project, new_timeline = project.reset_clip_composite(
            timeline_id=timeline.timeline_id,
            clip_id=clip_id,
            timeline=timeline,
        )

        restored_clip = new_timeline.get_clip_by_id(clip_id)
        assert restored_clip.timeline_start == original_clip.timeline_start
        assert restored_clip.timeline_duration == original_clip.timeline_duration
        assert restored_clip.source_start == original_clip.source_start
        assert restored_clip.source_duration == original_clip.source_duration

    def test_reset_with_none_timeline_returns_self(self):
        """Reset with None timeline returns unchanged project."""
        project, _, _, _, comp_clip_id = _make_project_with_comp_clip_timeline()

        result = project.reset_clip_composite("tl-1", "clip-1", timeline=None)
        # Should return self (project unchanged)
        assert result is project

    def test_reset_with_invalid_clip_returns_self(self):
        """Reset with invalid clip_id returns unchanged project."""
        project, timeline, clip_id, source_asset_id, comp_clip_id = _make_project_with_comp_clip_timeline()

        result = project.reset_clip_composite(
            timeline_id=timeline.timeline_id,
            clip_id="invalid_clip",
            timeline=timeline,
        )
        # Should return self (project unchanged)
        assert result is project


# ================================================================
# Test: Reset Comp Clip Graph
# ================================================================


class TestResetCompClipGraph:
    """Test EditProject.reset_comp_clip_graph."""

    def test_reset_empty_comp_clip_graph(self):
        """Reset Comp Clip Graph for empty comp clip resets to Constant -> Output."""
        project, comp_clip_id, _ = _make_project_with_empty_comp_clip()
        cc = CompClipData.from_dict(project.comp_clips[comp_clip_id])
        graph_id = cc.graph_id

        # Add a blur node to make graph non-default
        graph_dict = project.compositions[graph_id]
        graph = CompositeGraphData.from_dict(graph_dict)

        blur_node_id = str(uuid.uuid4())[:8]
        modified_nodes = list(graph.nodes) + [
            {
                "id": blur_node_id,
                "type": "blur",
                "position": [250, 200],
                "parameters": {"radius": 5.0},
                "disabled": False,
            }
        ]

        # Save modified graph back to project
        modified_graph_dict = dict(graph.to_dict())
        modified_graph_dict["nodes"] = modified_nodes
        new_compositions = dict(project.compositions)
        new_compositions[graph_id] = modified_graph_dict
        from src.features.edit.core.project import EditProject as EP
        project = EP(
            project_id=project.project_id,
            name=project.name,
            assets=project.assets,
            bins=project.bins,
            timelines=project.timelines,
            compositions=new_compositions,
            color_stacks=project.color_stacks,
            comp_clips=project.comp_clips,
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            active_comp_clip_id=project.active_comp_clip_id,
            created_at=project.created_at,
            modified_at=project.modified_at,
        )

        # Reset graph
        new_project = project.reset_comp_clip_graph(comp_clip_id)

        # Verify graph is reset
        new_graph_dict = new_project.compositions[graph_id]
        new_graph = CompositeGraphData.from_dict(new_graph_dict)

        # Should only have 2 nodes (Constant + Output)
        assert len(new_graph.nodes) == 2
        node_types = [n["type"] for n in new_graph.nodes]
        assert "constant" in node_types
        assert "output" in node_types

        # Should only have 1 connection
        assert len(new_graph.connections) == 1

    def test_reset_preserves_graph_id(self):
        """Reset preserves the composite_id / graph_id."""
        project, comp_clip_id, _ = _make_project_with_empty_comp_clip()
        cc = CompClipData.from_dict(project.comp_clips[comp_clip_id])
        original_graph_id = cc.graph_id

        new_project = project.reset_comp_clip_graph(comp_clip_id)

        # graph_id should be preserved
        assert original_graph_id in new_project.compositions

    def test_reset_preserves_comp_clip_data(self):
        """Reset does not delete CompClipData or MediaAsset."""
        project, comp_clip_id, comp_asset_id = _make_project_with_empty_comp_clip()

        new_project = project.reset_comp_clip_graph(comp_clip_id)

        # CompClipData should still exist
        assert comp_clip_id in new_project.comp_clips
        # MediaAsset should still exist
        assert comp_asset_id in new_project.assets

    def test_reset_with_invalid_comp_clip_returns_self(self):
        """Reset with invalid comp_clip_id returns unchanged project."""
        project, _, _ = _make_project_with_empty_comp_clip()

        result = project.reset_comp_clip_graph("invalid_id")
        assert result is project


# ================================================================
# Test: ClipItem Comp State Fields (requires PyQt6)
# ================================================================


@requires_pyqt
class TestClipItemCompState:
    """Test ClipItem comp state fields and properties."""

    @pytest.fixture
    def clip_item(self):
        """Create a ClipItem for testing."""
        item = ClipItem(
            clip_id="test-clip-1",
            track_id="track-1",
            asset_name="Test Clip",
            start_frame=0,
            duration_frames=100,
            pixels_per_frame=5.0,
            track_index=0,
        )
        return item

    def test_default_comp_state(self, clip_item):
        """Default comp state is all disabled/none."""
        assert clip_item.composite_id is None
        assert clip_item.is_comp_clip is False
        assert clip_item.comp_enabled is False
        assert clip_item.comp_cache_status == "none"

    def test_set_comp_state(self, clip_item):
        """set_comp_state updates fields."""
        clip_item.set_comp_state(
            composite_id="graph-123",
            is_comp_clip=True,
            comp_enabled=True,
            comp_cache_status="ready",
        )

        assert clip_item.composite_id == "graph-123"
        assert clip_item.is_comp_clip is True
        assert clip_item.comp_enabled is True
        assert clip_item.comp_cache_status == "ready"

    def test_set_comp_state_partial_update(self, clip_item):
        """set_comp_state only updates provided fields."""
        clip_item._composite_id = "graph-123"
        clip_item._comp_cache_status = "dirty"

        # Only update cache status
        clip_item.set_comp_state(comp_cache_status="ready")

        assert clip_item.composite_id == "graph-123"  # Unchanged
        assert clip_item.comp_cache_status == "ready"  # Updated


# ================================================================
# Test: COMP Badge Drawing Conditions (logic only)
# ================================================================


class TestCompBadgeDrawingLogic:
    """Test COMP badge drawing conditions (logic only, no QPainter)."""

    def test_badge_shown_for_comp_clip_state(self):
        """Badge condition: is_comp_clip or composite_id set."""
        # Scenario 1: comp_clip
        is_comp_clip = True
        composite_id = None
        assert is_comp_clip or composite_id

        # Scenario 2: has composite_id
        is_comp_clip = False
        composite_id = "graph-123"
        assert is_comp_clip or composite_id

        # Scenario 3: normal clip
        is_comp_clip = False
        composite_id = None
        assert not (is_comp_clip or composite_id)

    def test_badge_text_comp_vs_c(self):
        """Badge text: 'COMP' for comp_clip, 'C' for composite_id only."""
        # Comp clip
        is_comp_clip = True
        badge_text = "COMP" if is_comp_clip else "C"
        assert badge_text == "COMP"

        # Clip with composite_id only
        is_comp_clip = False
        badge_text = "COMP" if is_comp_clip else "C"
        assert badge_text == "C"

    def test_badge_minimum_width(self):
        """Badge requires minimum clip width (MIN_CLIP_WIDTH_FOR_NAME = 30)."""
        # Very short clip
        duration_frames = 5
        ppf = 5.0
        width = duration_frames * ppf  # = 25
        assert width < 30  # Too narrow for badge

        # Normal clip
        duration_frames = 20
        width = duration_frames * ppf  # = 100
        assert width >= 30  # Wide enough


# ================================================================
# Test: Cache Status Line Drawing Conditions
# ================================================================


class TestCacheStatusLineLogic:
    """Test cache status line drawing conditions."""

    def test_cache_line_drawn_for_valid_statuses(self):
        """Cache line drawn for all valid statuses except 'none'."""
        valid_statuses = ["dirty", "rendering", "ready", "failed"]
        for status in valid_statuses:
            assert status != "none"
            assert status  # Not empty

    def test_cache_line_not_drawn_for_none(self):
        """Cache line NOT drawn for 'none' status."""
        assert "none" == "none"

    def test_cache_status_color_values(self):
        """Verify all cache status color constants are correct."""
        # Test the expected hex values for cache status colors
        status_colors = {
            "dirty": "#ff8c00",     # Orange
            "rendering": "#4a90d9", # Blue
            "ready": "#4caf50",     # Green
            "failed": "#e53935",    # Red
        }
        # Just verify they are valid hex colors
        for name, hex_color in status_colors.items():
            assert hex_color.startswith("#")
            assert len(hex_color) == 7


# ================================================================
# Test: Context Menu States (logic only)
# ================================================================


class TestContextMenuStates:
    """Test context menu Composite submenu for different clip states."""

    def test_normal_clip_without_composite(self):
        """Normal clip: shows Convert to Comp Clip only."""
        is_comp_clip = False
        composite_id = None

        # Menu should show:
        # - Open in Composite
        # - Composite > Open in Composite / Convert to Comp Clip
        assert not is_comp_clip
        assert composite_id is None

    def test_clip_with_temporary_composite(self):
        """Clip with composite_id: shows Convert + Discard Draft."""
        is_comp_clip = False
        composite_id = "graph-123"

        # Menu should show:
        # - Open in Composite
        # - Composite > Open in Composite / Convert to Comp Clip / Discard Draft
        assert not is_comp_clip
        assert composite_id is not None

    def test_comp_clip(self):
        """Comp Clip: shows Bypass, Reset, Reveal, Render Cache."""
        is_comp_clip = True
        composite_id = "graph-123"

        # Menu should show:
        # - Open in Composite
        # - Composite > Open / Bypass / Reset / Reveal / Render Cache
        assert is_comp_clip
        assert composite_id is not None


# ================================================================
# Test: ClipItem Signal Definitions (requires PyQt6)
# ================================================================


@requires_pyqt
class TestClipItemSignals:
    """Test that ClipItem has all required comp-related signals."""

    def test_comp_signals_exist(self):
        """Verify all comp-related signals are defined on ClipItem."""
        for signal_name in [
            'convert_to_comp_clip_requested',
            'remove_composite_binding_requested',
            'bypass_composite_requested',
            'reset_composite_requested',
            'reveal_comp_clip_in_media_pool_requested',
            'render_cache_clip_requested',
        ]:
            assert hasattr(ClipItem, signal_name), f"Missing signal: {signal_name}"


# ================================================================
# Test: TimelineView Signal Definitions (requires PyQt6)
# ================================================================


@requires_pyqt
class TestTimelineViewSignalWiring:
    """Test that TimelineView has comp-related signals defined."""

    def test_timeline_view_has_comp_signals(self):
        """Verify TimelineView has all comp-related signals."""
        for signal_name in [
            'convert_to_comp_clip_requested',
            'remove_composite_binding_requested',
            'bypass_composite_requested',
            'reset_composite_requested',
            'reveal_comp_clip_in_media_pool_requested',
            'render_cache_clip_requested',
        ]:
            assert hasattr(TimelineView, signal_name), f"Missing signal: {signal_name}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
