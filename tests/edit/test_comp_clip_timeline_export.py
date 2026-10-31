"""
Tests for Comp Clip Timeline Export integration.

Tests that CompositeFrameRenderer is correctly wired into
TimelineExporter for comp_clip assets.

Coverage:
- TimelineExporter creates CompositeFrameRenderer lazily
- Export uses renderer for comp_clip assets
- Export falls back to original source when graph data missing
- Cleanup releases CompositeFrameRenderer resources
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_export import (
    TimelineExporter,
    TimelineExportConfig,
    ExportSource,
    ExportRangeType,
    ExportCodec,
)
from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_render_plan import ClipLayerInfo


# ================================================================
# Fixtures
# ================================================================


def _make_clip_instance(
    asset_id: str = "asset-1",
    composite_id: str | None = None,
) -> ClipInstance:
    """Create a minimal ClipInstance for testing."""
    return ClipInstance(
        clip_id="clip-1",
        asset_id=asset_id,
        track_id="track-v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        composite_id=composite_id,
    )


def _make_comp_clip_asset(
    asset_id: str = "comp-asset-1",
    graph_id: str = "graph-1",
    original_asset_id: str = "orig-asset-1",
) -> MediaAsset:
    """Create a comp_clip MediaAsset."""
    return MediaAsset(
        asset_id=asset_id,
        path=f"topos://comp_clip/{asset_id}",
        media_type="comp_clip",
        display_name="Test Comp Clip",
        metadata={
            "graph_id": graph_id,
            "comp_clip_id": "cc-1",
            "original_asset_id": original_asset_id,
        },
    )


def _make_video_asset(
    asset_id: str = "orig-asset-1",
    path: str = "/tmp/test_video.mov",
) -> MediaAsset:
    """Create a video MediaAsset."""
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name="Test Video",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
        },
    )


def _make_layer(
    asset_id: str,
    source_frame: int = 0,
    composite_id: str | None = None,
) -> ClipLayerInfo:
    """Create a ClipLayerInfo for testing."""
    clip = _make_clip_instance(asset_id, composite_id=composite_id)
    return ClipLayerInfo(
        clip=clip,
        source_frame=source_frame,
        track_index=0,
        track_name="Video 1",
        opacity=1.0,
        transform=(0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0),
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
    )


def _make_simple_graph_data() -> dict:
    """Create a minimal CompositeGraphData dict for testing."""
    return {
        "composite_id": "graph-1",
        "source_clip_id": "clip-1",
        "source_asset_id": "orig-asset-1",
        "source_asset_path": "/tmp/test_video.mov",
        "name": "Test Composite",
        "schema_version": 6,
        "nodes": [
            {
                "id": "const-1",
                "type": "constant",
                "position": [100, 100],
                "parameters": {
                    "color": [0.5, 0.3, 0.1, 1.0],
                    "width": 1920,
                    "height": 1080,
                },
                "disabled": False,
            },
            {
                "id": "output-1",
                "type": "output",
                "position": [400, 100],
                "parameters": {
                    "format_mode": "input",
                },
                "disabled": False,
            },
        ],
        "connections": [
            {
                "src_node_id": "const-1",
                "src_port_name": "result",
                "dst_node_id": "output-1",
                "dst_port_name": "image",
            },
        ],
        "width": 1920,
        "height": 1080,
        "fps": 24.0,
        "duration_frames": 100,
    }


def _make_project_with_comp_clip():
    """Create a project with a comp_clip asset and graph data."""
    project = EditProject.create("Test Export")
    graph_id = "graph-1"

    # Import source video asset
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

    # Import comp_clip asset
    comp_asset = _make_comp_clip_asset(graph_id=graph_id)
    project = project.import_asset(
        path=comp_asset.path,
        media_type="comp_clip",
        metadata=comp_asset.metadata,
    )
    comp_asset_id = list(project.assets.keys())[-1]

    # Add graph data directly to compositions dict
    graph_data = _make_simple_graph_data()
    from dataclasses import replace
    project = replace(project, compositions={**project.compositions, graph_id: graph_data})

    return project, comp_asset_id, graph_id


# GPU availability check
gpu_available = True
try:
    from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer
except ImportError:
    gpu_available = False

requires_gpu = pytest.mark.skipif(
    not gpu_available,
    reason="GPU modules (moderngl, cv2) not available"
)


# ================================================================
# Tests: CompositeFrameRenderer Lazy Initialization
# ================================================================


class TestExporterCompFrameRendererInit:
    """Test that TimelineExporter lazily creates CompositeFrameRenderer."""

    def test_no_renderer_at_init(self):
        """_comp_frame_renderer is None at initialization."""
        project = EditProject.create("Test")
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )
        assert exporter._comp_frame_renderer is None

    @requires_gpu
    def test_get_comp_frame_renderer_creates_instance(self):
        """_get_comp_frame_renderer creates CompositeFrameRenderer."""
        project = EditProject.create("Test")
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )
        renderer = exporter._get_comp_frame_renderer()
        assert renderer is not None
        assert isinstance(renderer, CompositeFrameRenderer)

    @requires_gpu
    def test_get_comp_frame_renderer_reuses_instance(self):
        """_get_comp_frame_renderer returns same instance."""
        project = EditProject.create("Test")
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )
        r1 = exporter._get_comp_frame_renderer()
        r2 = exporter._get_comp_frame_renderer()
        assert r1 is r2


# ================================================================
# Tests: Export Uses Renderer for Comp Clip
# ================================================================


class TestExportUsesRenderer:
    """Test that export uses CompositeFrameRenderer for comp_clip assets."""

    def test_render_comp_clip_frame_with_graph_data(self):
        """_render_comp_clip_frame renders with graph data present."""
        project, comp_asset_id, graph_id = _make_project_with_comp_clip()
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        # Mock the renderer to avoid GPU dependency
        with patch.object(exporter, '_get_comp_frame_renderer') as mock_get:
            mock_renderer = MagicMock()
            mock_renderer.render_comp_clip_frame.return_value = np.zeros(
                (1080, 1920, 3), dtype=np.float32
            )
            mock_get.return_value = mock_renderer

            asset = project.get_asset_by_id(comp_asset_id)
            layer = _make_layer(asset_id=comp_asset_id)
            result = exporter._render_comp_clip_frame(layer, asset)

            # Should have called render with export quality
            mock_renderer.render_comp_clip_frame.assert_called_once()
            call_kwargs = mock_renderer.render_comp_clip_frame.call_args[1]
            assert call_kwargs['quality'] == 'export'

            # Result should be uint8 numpy array
            assert result is not None
            assert result.dtype == np.uint8

    def test_render_comp_clip_frame_no_graph_id(self):
        """_render_comp_clip_frame returns None when asset has no graph_id."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset(graph_id="")
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        comp_asset_id = list(project.assets.keys())[-1]

        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        asset = project.get_asset_by_id(comp_asset_id)
        layer = _make_layer(asset_id=comp_asset_id)
        result = exporter._render_comp_clip_frame(layer, asset)
        assert result is None

    def test_render_comp_clip_frame_no_graph_data(self):
        """_render_comp_clip_frame returns None when compositions missing."""
        project = EditProject.create("Test")
        graph_id = "graph-nonexistent"
        comp_asset = _make_comp_clip_asset(graph_id=graph_id)
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        comp_asset_id = list(project.assets.keys())[-1]

        assert graph_id not in project.compositions

        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        asset = project.get_asset_by_id(comp_asset_id)
        layer = _make_layer(asset_id=comp_asset_id)
        result = exporter._render_comp_clip_frame(layer, asset)
        assert result is None

    def test_render_comp_clip_frame_not_comp_clip(self):
        """_render_comp_clip_frame returns None for non-comp_clip assets."""
        project = EditProject.create("Test")
        video_asset = _make_video_asset()
        project = project.import_asset(
            path=video_asset.path,
            media_type="video",
            metadata=video_asset.metadata,
        )
        video_asset_id = list(project.assets.keys())[-1]

        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        asset = project.get_asset_by_id(video_asset_id)
        layer = _make_layer(asset_id=video_asset_id)
        result = exporter._render_comp_clip_frame(layer, asset)
        assert result is None

    def test_render_composite_id_video_clip_with_graph_data(self):
        """Non-comp_clip assets render through Composite when clip has composite_id."""
        project = EditProject.create("Test")
        video_asset = _make_video_asset(asset_id="orig-asset-1")
        project = project.import_asset(
            path=video_asset.path,
            media_type="video",
            metadata=video_asset.metadata,
        )
        video_asset_id = list(project.assets.keys())[-1]

        graph_data = _make_simple_graph_data()
        from dataclasses import replace
        project = replace(project, compositions={**project.compositions, "graph-1": graph_data})

        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        with patch.object(exporter, '_get_comp_frame_renderer') as mock_get:
            mock_renderer = MagicMock()
            mock_renderer.render_comp_clip_frame.return_value = np.zeros(
                (1080, 1920, 3), dtype=np.float32
            )
            mock_get.return_value = mock_renderer

            asset = project.get_asset_by_id(video_asset_id)
            layer = _make_layer(asset_id=video_asset_id, composite_id="graph-1")
            result = exporter._render_comp_clip_frame(layer, asset)

            mock_renderer.render_comp_clip_frame.assert_called_once()
            assert result is not None
            assert result.dtype == np.uint8


# ================================================================
# Tests: Fallback to Original Source
# ================================================================


class TestExportFallback:
    """Test that export falls back to original source when graph fails."""

    def test_fallback_to_original_source(self):
        """Export falls back to original source when comp render returns None."""
        project, comp_asset_id, graph_id = _make_project_with_comp_clip()
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        # Mock renderer to return None (simulating failure)
        with patch.object(exporter, '_render_comp_clip_frame') as mock_render:
            mock_render.return_value = None

            # Mock image frame methods (comp_clip with image source)
            with patch.object(exporter, '_get_image_frame') as mock_image:
                mock_image.return_value = np.zeros((1080, 1920, 3), dtype=np.uint8)

                # The original source is a video asset, so the fallback goes
                # through _get_video_frame. Patch it explicitly: leaving it to
                # the MagicMock frame provider is flaky (str(MagicMock) embeds
                # a memory address, and the high-depth pixel-format heuristic
                # matches digit substrings like "16" inside that address).
                with patch.object(exporter, '_get_video_frame') as mock_video:
                    mock_video.return_value = np.zeros((1080, 1920, 3), dtype=np.uint8)

                    # Also import the original source asset so fallback works
                    orig_asset = _make_video_asset()
                    from dataclasses import replace
                    # Add orig-asset-1 to the project assets
                    new_assets = dict(project.assets)
                    new_assets[orig_asset.asset_id] = orig_asset
                    project = replace(project, assets=new_assets)
                    # Update exporter's project reference
                    exporter._project = project

                    layer = _make_layer(asset_id=comp_asset_id)
                    result = exporter._get_layer_frame(layer)

                    # Should have tried comp render first
                    mock_render.assert_called_once()
                    # Should have fallen back to video/image frame
                    assert result is not None


# ================================================================
# Tests: Cleanup
# ================================================================


class TestExportCleanup:
    """Test resource cleanup in exporter."""

    def test_cleanup_clips_cleans_renderer(self):
        """_cleanup_clips cleans up CompositeFrameRenderer."""
        project = EditProject.create("Test")
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )
        # Set a mock renderer
        mock_renderer = MagicMock()
        exporter._comp_frame_renderer = mock_renderer

        exporter._cleanup_clips()
        mock_renderer.cleanup.assert_called_once()
        assert exporter._comp_frame_renderer is None

    def test_cleanup_safe_without_init(self):
        """_cleanup_clips is safe even if renderer was never created."""
        project = EditProject.create("Test")
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )
        assert exporter._comp_frame_renderer is None
        exporter._cleanup_clips()
        assert exporter._comp_frame_renderer is None


# ================================================================
# Tests: Quality Mode
# ================================================================


class TestExportQuality:
    """Test that export uses correct quality mode."""

    def test_export_uses_export_quality(self):
        """render_comp_clip_frame is called with quality='export'."""
        project, comp_asset_id, graph_id = _make_project_with_comp_clip()
        sequence = TimelineSequence(name="Test Seq")
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=MagicMock(),
        )

        with patch.object(exporter, '_get_comp_frame_renderer') as mock_get:
            mock_renderer = MagicMock()
            mock_renderer.render_comp_clip_frame.return_value = None
            mock_get.return_value = mock_renderer

            asset = project.get_asset_by_id(comp_asset_id)
            layer = _make_layer(asset_id=comp_asset_id)
            exporter._render_comp_clip_frame(layer, asset)

            # Verify export quality is used
            call_kwargs = mock_renderer.render_comp_clip_frame.call_args[1]
            assert call_kwargs['quality'] == 'export'
