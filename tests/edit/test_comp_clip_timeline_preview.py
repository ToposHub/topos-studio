"""
Tests for Comp Clip Timeline Preview integration.

Tests that CompositeFrameRenderer is correctly wired into
TimelinePreviewRenderer for comp_clip assets.

Coverage:
- CompositeFrameRenderer creation (mocked GPU)
- Preview detects comp_clip and delegates to renderer
- Fallback to original source when graph data missing
- GPU path falls back to CPU for comp_clip assets
- Cleanup releases CompositeFrameRenderer resources
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_preview_renderer import (
    PreviewRenderWorker,
    AssetResolver,
    GLContextManager,
)
from src.features.edit.core.edit_gpu_preview_renderer import (
    EditGpuPreviewRenderer,
    _ExternalTextureProxy,
)
from src.features.edit.core.timeline_render_plan import ClipLayerInfo
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.project import EditProject, MediaAsset


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


# Helper to skip tests when GPU modules are unavailable
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
# Tests: CompositeFrameRenderer Creation (GPU required)
# ================================================================


@requires_gpu
class TestCompositeFrameRendererCreation:
    """Test that CompositeFrameRenderer can be created and initialized."""

    def test_import(self):
        """CompositeFrameRenderer can be imported."""
        assert CompositeFrameRenderer is not None

    def test_creation(self):
        """CompositeFrameRenderer can be instantiated without GPU."""
        renderer = CompositeFrameRenderer()
        assert renderer is not None
        assert renderer._export_renderer is None

    def test_cleanup_without_init(self):
        """cleanup() is safe to call even without GPU initialization."""
        renderer = CompositeFrameRenderer()
        renderer.cleanup()  # Should not raise


# ================================================================
# Tests: CompositeFrameRenderer Snapshot Building (GPU required)
# ================================================================


@requires_gpu
class TestCompositeFrameRendererSnapshot:
    """Test CompositeFrameRenderer snapshot building."""

    def test_build_snapshot_creates_valid_snapshot(self):
        """_build_snapshot creates a GraphSnapshot from graph_data."""
        from src.features.composite.core.graph_snapshot import GraphSnapshot

        renderer = CompositeFrameRenderer()
        graph_data = _make_simple_graph_data()
        snapshot = renderer._build_snapshot(graph_data, frame_number=0)

        assert snapshot is not None
        assert isinstance(snapshot, GraphSnapshot)
        assert "const-1" in snapshot.nodes
        assert "output-1" in snapshot.nodes
        assert len(snapshot.connections) == 1
        assert snapshot.frame_number == 0

    def test_build_snapshot_with_frame_number(self):
        """_build_snapshot passes frame_number to the snapshot."""
        renderer = CompositeFrameRenderer()
        graph_data = _make_simple_graph_data()
        snapshot = renderer._build_snapshot(graph_data, frame_number=42)

        assert snapshot.frame_number == 42

    def test_build_snapshot_reuses_graph_for_same_revision(self):
        """A stable producer revision keeps deserialization off the hot path."""
        renderer = CompositeFrameRenderer()
        graph_data = _make_simple_graph_data()
        graph_data["graph_revision"] = 7

        with patch(
            "src.features.composite.render.composite_frame_renderer.NodeGraph.from_dict"
        ) as from_dict:
            node_graph = MagicMock()
            node_graph.nodes = {}
            node_graph.snapshot.side_effect = [object(), object()]
            from_dict.return_value = node_graph

            renderer._build_snapshot(graph_data, frame_number=0)
            renderer._build_snapshot(graph_data, frame_number=1)

        assert from_dict.call_count == 1

    def test_build_snapshot_rebuilds_graph_when_revision_changes(self):
        """An in-place graph update must invalidate the deserialized graph cache."""
        renderer = CompositeFrameRenderer()
        graph_data = _make_simple_graph_data()
        graph_data["graph_revision"] = 7

        with patch(
            "src.features.composite.render.composite_frame_renderer.NodeGraph.from_dict"
        ) as from_dict:
            first_graph = MagicMock()
            first_graph.nodes = {}
            first_graph.snapshot.return_value = object()
            second_graph = MagicMock()
            second_graph.nodes = {}
            second_graph.snapshot.return_value = object()
            from_dict.side_effect = [first_graph, second_graph]

            renderer._build_snapshot(graph_data, frame_number=0)
            graph_data["graph_revision"] = 8
            renderer._build_snapshot(graph_data, frame_number=1)

        assert from_dict.call_count == 2

    def test_render_returns_none_for_empty_graph_data(self):
        """render_comp_clip_frame returns None for empty graph_data."""
        renderer = CompositeFrameRenderer()
        result = renderer.render_comp_clip_frame(graph_data={})
        assert result is None

    def test_render_returns_none_for_no_nodes(self):
        """render_comp_clip_frame returns None when nodes list is empty."""
        renderer = CompositeFrameRenderer()
        # Mock the export renderer to avoid GPU context creation in tests
        mock_export = MagicMock()
        mock_export.render_and_readback.return_value = MagicMock(error="no output node", image=None)
        renderer._export_renderer = mock_export
        result = renderer.render_comp_clip_frame(
            graph_data={"nodes": [], "connections": [], "composite_id": "g1"}
        )
        # No nodes means no output node, should return None without crash
        assert result is None


# ================================================================
# Tests: Preview Worker Comp Clip Detection
# ================================================================


class TestPreviewWorkerCompClipDetection:
    """Test that PreviewRenderWorker detects comp_clip assets."""

    def test_invalidate_composite_cache_keeps_other_graph_frames(self):
        """Editing one Composite graph must not replay its stale cached frame."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        frame_a = np.zeros((2, 2, 3), dtype=np.uint8)
        frame_b = np.ones((2, 2, 3), dtype=np.uint8)
        worker._comp_clip_cache = {
            ("graph-a", 10): frame_a,
            ("graph-b", 10): frame_b,
        }

        worker.invalidate_composite_cache("graph-a")

        assert ("graph-a", 10) not in worker._comp_clip_cache
        assert worker._comp_clip_cache[("graph-b", 10)] is frame_b

    def test_comp_clip_fallback_cache_uses_byte_budgeted_lru(self):
        """4K fallback frames must be bounded by bytes, not only frame count."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        worker._comp_clip_cache_max = 10
        worker._comp_clip_cache_max_bytes = 24
        frame = np.zeros((2, 2, 3), dtype=np.uint8)  # 12 bytes

        worker._store_comp_clip_frame(("graph-a", 1, ""), frame)
        worker._store_comp_clip_frame(("graph-a", 2, ""), frame.copy())

        assert len(worker._comp_clip_cache) == 2
        assert worker._comp_clip_cache_bytes == 24
        worker._store_comp_clip_frame(("graph-a", 3, ""), frame.copy())

        assert len(worker._comp_clip_cache) == 2
        assert worker._comp_clip_cache_bytes == 24
        assert ("graph-a", 1, "") not in worker._comp_clip_cache
        assert ("graph-a", 3, "") in worker._comp_clip_cache

    def test_comp_clip_fallback_cache_hit_refreshes_lru_order(self):
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        worker._store_comp_clip_frame(
            ("graph-a", 1, ""), np.zeros((1, 1, 3), dtype=np.uint8)
        )
        worker._store_comp_clip_frame(
            ("graph-a", 2, ""), np.ones((1, 1, 3), dtype=np.uint8)
        )

        cached = worker._get_comp_clip_cached_frame(("graph-a", 1, ""))
        assert cached is not None
        assert list(worker._comp_clip_cache)[-1] == ("graph-a", 1, "")

    @requires_gpu
    def test_get_comp_frame_renderer_creates_instance(self):
        """_get_comp_frame_renderer creates CompositeFrameRenderer."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        renderer = worker._get_comp_frame_renderer()
        assert renderer is not None
        assert isinstance(renderer, CompositeFrameRenderer)

    @requires_gpu
    def test_get_comp_frame_renderer_reuses_instance(self):
        """_get_comp_frame_renderer returns the same instance."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        r1 = worker._get_comp_frame_renderer()
        r2 = worker._get_comp_frame_renderer()
        assert r1 is r2

    def test_render_comp_clip_frame_no_graph_id(self):
        """_render_comp_clip_frame returns None when asset has no graph_id."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset(graph_id="")
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
        )
        layer = _make_layer(asset_id=asset_id)
        result = worker._render_comp_clip_frame(layer)
        assert result is None

    def test_render_comp_clip_frame_no_graph_data(self):
        """_render_comp_clip_frame returns None when graph_data not in compositions."""
        project = EditProject.create("Test")
        graph_id = "graph-nonexistent"
        comp_asset = _make_comp_clip_asset(graph_id=graph_id)
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
        )
        layer = _make_layer(asset_id=asset_id)
        result = worker._render_comp_clip_frame(layer)
        assert result is None

    @requires_gpu
    def test_render_comp_clip_frame_with_graph_data(self):
        """_render_comp_clip_frame attempts rendering when graph data exists."""
        project = EditProject.create("Test")
        graph_id = "graph-1"

        # Create comp_clip asset
        comp_asset = _make_comp_clip_asset(graph_id=graph_id)
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        # Add graph data to compositions
        graph_data = _make_simple_graph_data()
        from dataclasses import replace
        project = replace(project, compositions={**project.compositions, graph_id: graph_data})
        assert graph_id in project.compositions

        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
        )
        layer = _make_layer(asset_id=asset_id)

        # Mock the CompositeFrameRenderer to avoid GPU dependency
        with patch.object(worker, '_get_comp_frame_renderer') as mock_get:
            mock_renderer = MagicMock()
            mock_renderer.render_comp_clip_frame.return_value = np.zeros(
                (1080, 1920, 3), dtype=np.float32
            )
            mock_get.return_value = mock_renderer

            result = worker._render_comp_clip_frame(layer)

            # Should have called render
            mock_renderer.render_comp_clip_frame.assert_called_once()
            # Result should be uint8 numpy array
            assert result is not None
            assert result.dtype == np.uint8
            assert result.shape == (1080, 1920, 3)

    @requires_gpu
    def test_render_composite_id_video_clip_with_graph_data(self):
        """Normal video clips with composite_id render through Composite."""
        project = EditProject.create("Test")
        video_asset = _make_video_asset(asset_id="orig-asset-1")
        project = project.import_asset(
            path=video_asset.path,
            media_type="video",
            metadata=video_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        graph_data = _make_simple_graph_data()
        from dataclasses import replace
        project = replace(project, compositions={**project.compositions, "graph-1": graph_data})

        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
        )
        layer = _make_layer(asset_id=asset_id, composite_id="graph-1")

        with patch.object(worker, '_get_comp_frame_renderer') as mock_get:
            mock_renderer = MagicMock()
            mock_renderer.render_comp_clip_frame.return_value = np.zeros(
                (1080, 1920, 3), dtype=np.float32
            )
            mock_get.return_value = mock_renderer

            result = worker._render_comp_clip_frame(layer)

            mock_renderer.render_comp_clip_frame.assert_called_once()
            assert result is not None
            assert result.dtype == np.uint8


# ================================================================
# Tests: GPU Path Fallback
# ================================================================


class TestGPUPatFallback:
    """TL-C01/TL-P0-03: comp_clip GPU-direct is capability-gated.

    The historical behaviour ("GPU path always skips comp_clip") was a
    migration gate, not a success path: with the declared capability the
    layer is submitted as a CompositeGpuFrameHandle (zero readback);
    without it the documented CPU bridge fallback still applies.
    """

    @staticmethod
    def _make_comp_worker(project):
        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
            gpu_decode_manager=MagicMock(),
            gpu_renderer=MagicMock(),
        )
        return worker

    def test_gpu_path_skips_comp_clip_without_declared_capability(self):
        """No declared capability → comp_clip still bridges via CPU."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        layer = _make_layer(asset_id=asset_id, composite_id="graph-1")
        result = worker._try_gpu_render(0, (layer,), (), 0)
        assert result[0] is False
        assert result[1]["fallback_reason"] == "comp_clip_requires_cpu_bridge"

    def test_gpu_path_skips_comp_clip_with_mock_renderer(self):
        """A Mock renderer (truthy but not ``is True``) must NOT enable the
        GPU-direct path — strict capability comparison guards test doubles."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True  # producer flag on
        layer = _make_layer(asset_id=asset_id)
        result = worker._try_gpu_render(0, (layer,), (), 0)
        # MagicMock().has_comp_clip_gpu_support() returns a truthy Mock,
        # not True — the strict gate must keep the CPU fallback.
        assert result[0] is False

    def test_gpu_path_submits_comp_clip_handle_when_capability_declared(self):
        """Capability declared → the comp layer is submitted as a GPU
        handle with zero CPU frames (readback_count == 0 by construction:
        no numpy frame is produced anywhere in the flow)."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True

        class _FakeHandle:
            available = True
            fallback_reason = ""
            texture_id = 17
            released = False

            def release(self):
                self.released = True

        handle = _FakeHandle()
        produced = {}

        def _fake_decode(layer, frame):
            produced["layer"] = layer
            produced["frame"] = frame
            return {
                "ok": True,
                "frame_type": "comp_gpu_handle",
                "gpu_handle": handle,
                "track_info": {"track_index": 0, "opacity": 1.0},
            }

        worker._decode_comp_clip_gpu_layer = _fake_decode

        # Production render plans carry the graph id on the ClipInstance;
        # this must still reach the comp-clip GPU lane.
        layer = _make_layer(asset_id=asset_id, composite_id="graph-1")
        result = worker._try_gpu_render(0, (layer,), (), 0)
        assert result[0] is True
        assert result[1].get("decode_path") == "comp_gpu_direct"
        # The producer was invoked for the comp layer
        assert produced["frame"] == 0
        # No CPU frame was produced: no numpy arrays anywhere in the flow
        submitted_task = worker._gpu_renderer.submit.call_args[0][0]
        assert len(submitted_task.layers) == 1
        assert submitted_task.layers[0]["frame_type"] == "comp_gpu_handle"
        assert "decoded_frame" not in submitted_task.layers[0]
        assert submitted_task.layers[0]["gpu_handle"] is handle

    def test_comp_gpu_producer_stays_on_worker_gl_thread(self):
        """Comp GPU producers must not run on the generic decode pool."""
        import threading
        from types import SimpleNamespace

        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        comp_asset_id = list(project.assets.keys())[-1]
        video_asset = _make_video_asset()
        project = project.import_asset(
            path=video_asset.path,
            media_type="video",
            metadata=video_asset.metadata,
        )
        video_asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True
        worker._gpu_renderer.submit.return_value = True
        worker._gpu_decode_manager.get_frame.return_value = (
            MagicMock(), "planar")

        comp_thread = {}

        def fake_comp_decode(layer, frame):
            comp_thread["thread"] = threading.current_thread()
            return {
                "ok": True,
                "frame_type": "comp_gpu_handle",
                "gpu_handle": SimpleNamespace(release=lambda: None),
                "track_info": {"track_index": 0, "opacity": 1.0},
            }

        worker._decode_comp_clip_gpu_layer = fake_comp_decode

        class RecordingExecutor:
            def __init__(self):
                self.submitted = []

            def submit(self, fn, *args, **kwargs):
                self.submitted.append(args[0])
                return SimpleNamespace(
                    result=lambda: fn(*args, **kwargs),
                )

        worker._decode_executor.shutdown(
            wait=False, cancel_futures=True)
        executor = RecordingExecutor()
        worker._decode_executor = executor

        comp_layer = _make_layer(asset_id=comp_asset_id, composite_id="graph-1")
        video_layer = _make_layer(asset_id=video_asset_id)
        ok, _ = worker._try_gpu_render(
            frame=0,
            layers=(comp_layer, video_layer),
            transitions=(),
            generation_id=1,
        )

        assert ok is True
        assert comp_thread["thread"] is threading.current_thread()
        assert len(executor.submitted) == 1
        assert executor.submitted[0][1] != "__comp_gpu__"
        worker.stop()

    def test_comp_gpu_batch_reuses_one_current_context(self):
        """Multiple Comp Clip producers share one worker GL context lease."""
        from types import SimpleNamespace

        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True
        worker._gpu_renderer.submit.return_value = True
        worker._comp_gl_ready = True
        worker._comp_gl_context = MagicMock()
        worker._comp_gl_context.makeCurrent.return_value = True
        worker._comp_gl_surface = MagicMock()
        lease_state = []

        def fake_comp_decode(layer, frame):
            lease_state.append(worker._comp_gl_context_current)
            return {
                "ok": True,
                "frame_type": "comp_gpu_handle",
                "gpu_handle": SimpleNamespace(release=lambda: None),
                "track_info": {"track_index": 0, "opacity": 1.0},
            }

        worker._decode_comp_clip_gpu_layer = fake_comp_decode
        worker._decode_executor.shutdown(
            wait=False, cancel_futures=True)

        mutex = MagicMock()
        gl_manager = SimpleNamespace(mutex=mutex)
        layers = (
            _make_layer(asset_id=asset_id, composite_id="graph-1"),
            _make_layer(asset_id=asset_id, composite_id="graph-1"),
        )
        with patch.object(GLContextManager, "instance", return_value=gl_manager):
            ok, _ = worker._try_gpu_render(
                frame=0, layers=layers, transitions=(), generation_id=1)

        assert ok is True
        assert lease_state == [True, True]
        worker._comp_gl_context.makeCurrent.assert_called_once_with(
            worker._comp_gl_surface)
        worker._comp_gl_context.doneCurrent.assert_called_once_with()
        assert mutex.lock.call_count == 1
        assert mutex.unlock.call_count == 1
        assert worker._comp_gl_context_current is False
        worker.stop()

    def test_gpu_path_falls_back_when_comp_gpu_handle_unavailable(self):
        """Handle production failure → explicit fallback with reason,
        never a silent CPU composite."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True

        def _failing_decode(layer, frame):
            return {
                "ok": False,
                "fallback_reason": "comp_gpu_unavailable: graph dead",
                "layer": layer,
            }

        worker._decode_comp_clip_gpu_layer = _failing_decode

        layer = _make_layer(asset_id=asset_id)
        result = worker._try_gpu_render(0, (layer,), (), 0)
        assert result[0] is False
        assert result[1]["fallback_reason"] == \
            "comp_gpu_unavailable: graph dead"

    def test_submit_failure_releases_comp_gpu_handle(self):
        """P0-5 lease contract: a handle produced but never submitted (queue
        full / renderer gone) must be released, not leaked."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True
        worker._gpu_renderer.submit.return_value = False

        class _FakeHandle:
            available = True
            fallback_reason = ""
            texture_id = 17
            released = False

            def release(self):
                self.released = True

        handle = _FakeHandle()
        worker._decode_comp_clip_gpu_layer = lambda layer, frame: {
            "ok": True,
            "frame_type": "comp_gpu_handle",
            "gpu_handle": handle,
            "track_info": {"track_index": 0, "opacity": 1.0},
        }

        layer = _make_layer(asset_id=asset_id)
        result = worker._try_gpu_render(0, (layer,), (), 0)
        assert result[0] is False
        # The unsubmitted lease went back to the producer.
        assert handle.released is True

    def test_later_layer_resources_released_when_earlier_layer_fails(self):
        """Regression: the decode-result loop must keep scanning after a
        failed layer — decode jobs after it already ran and hold GPU frames
        / handle leases that would be abandoned unreleased."""
        project = EditProject.create("Test")
        comp_asset = _make_comp_clip_asset()
        project = project.import_asset(
            path=comp_asset.path,
            media_type="comp_clip",
            metadata=comp_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        worker = self._make_comp_worker(project)
        worker._comp_gpu_direct_enabled = True
        worker._gpu_renderer.has_comp_clip_gpu_support.return_value = True

        class _FakeHandle:
            available = True
            fallback_reason = ""
            texture_id = 17
            released = False

            def release(self):
                self.released = True

        handle = _FakeHandle()
        layer_bad = _make_layer(asset_id=asset_id)
        layer_ok = _make_layer(asset_id=asset_id)

        def _fake_decode(layer, frame):
            if layer is layer_bad:
                return {
                    "ok": False,
                    "fallback_reason": "comp_gpu_unavailable: graph dead",
                    "layer": layer,
                }
            return {
                "ok": True,
                "frame_type": "comp_gpu_handle",
                "gpu_handle": handle,
                "track_info": {"track_index": 0, "opacity": 1.0},
            }

        worker._decode_comp_clip_gpu_layer = _fake_decode

        result = worker._try_gpu_render(0, (layer_bad, layer_ok), (), 0)
        assert result[0] is False
        # The ok-after-failed layer's lease was collected and released.
        assert handle.released is True

    def test_gpu_path_skips_composite_id_video_clip(self):
        """GPU path returns False when a normal video clip has composite_id."""
        project = EditProject.create("Test")
        video_asset = _make_video_asset(asset_id="orig-asset-1")
        project = project.import_asset(
            path=video_asset.path,
            media_type="video",
            metadata=video_asset.metadata,
        )
        asset_id = list(project.assets.keys())[-1]

        resolver = AssetResolver(project)
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=resolver,
            video_renderer=None,
            gpu_decode_manager=MagicMock(),
            gpu_renderer=MagicMock(),
        )

        layer = _make_layer(asset_id=asset_id, composite_id="graph-1")
        result = worker._try_gpu_render(0, (layer,), (), 0)
        assert result[0] is False


class TestCompGpuHandleConsumer:
    """TL-P1-01: Edit GPU consumer validates and samples comp handles."""

    def test_capability_requires_live_blend_pipeline(self):
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        renderer._initialized = True
        renderer.ctx = object()
        renderer._blend_program = object()
        renderer._blend_vao = object()

        assert renderer.has_comp_clip_gpu_support() is True

        renderer._blend_vao = None
        assert renderer.has_comp_clip_gpu_support() is False

    class _Handle:
        def __init__(self, *, generation=4, backend="opengl", fence_ns=0):
            self.generation = generation
            self.backend = backend
            self.fence_ns = fence_ns
            self.texture_id = 77
            self.width = 640
            self.height = 360
            self.released = False

        def is_presentable(self, generation):
            return self.generation == generation

        def release(self):
            self.released = True

    def test_consumer_returns_non_owning_texture_proxy_without_readback(self):
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        handle = self._Handle()

        texture, reason = renderer._prepare_comp_gpu_texture(handle, 4)

        assert reason is None
        assert isinstance(texture, _ExternalTextureProxy)
        assert texture.glo == handle.texture_id
        assert texture.size == (handle.width, handle.height)
        assert handle.released is False

    def test_consumer_rejects_stale_generation(self):
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        handle = self._Handle(generation=3)

        texture, reason = renderer._prepare_comp_gpu_texture(handle, 4)

        assert texture is None
        assert reason == "comp_gpu_handle_stale_or_unavailable"

    def test_consumer_rejects_non_opengl_backend(self):
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        handle = self._Handle(backend="d3d11")

        texture, reason = renderer._prepare_comp_gpu_texture(handle, 4)

        assert texture is None
        assert reason == "comp_gpu_handle_backend_mismatch"

    def test_release_task_sources_releases_comp_handle(self):
        renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
        handle = self._Handle()
        task = type("Task", (), {
            "layers": [{"frame_type": "comp_gpu_handle", "gpu_handle": handle}],
        })()

        renderer._release_task_sources(task)

        assert handle.released is True


# ================================================================
# Tests: Cleanup
# ================================================================


class TestCleanup:
    """Test resource cleanup."""

    def test_stop_cleans_up_comp_frame_renderer_none(self):
        """stop() is safe when _comp_frame_renderer is None."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        assert worker._comp_frame_renderer is None
        worker.stop()
        assert worker._comp_frame_renderer is None

    def test_cleanup_clips_cleans_up_none(self):
        """cleanup_clips() is safe when _comp_frame_renderer is None."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        assert worker._comp_frame_renderer is None
        worker.cleanup_clips()
        assert worker._comp_frame_renderer is None

    def test_stop_cleans_up_mocked_renderer(self):
        """stop() calls cleanup() on the CompositeFrameRenderer."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        mock_renderer = MagicMock()
        worker._comp_frame_renderer = mock_renderer
        worker.stop()
        mock_renderer.cleanup.assert_called_once()
        assert worker._comp_frame_renderer is None

    def test_cleanup_clips_cleans_up_mocked_renderer(self):
        """cleanup_clips() calls cleanup() on the CompositeFrameRenderer."""
        worker = PreviewRenderWorker(
            frame_provider=MagicMock(),
            asset_resolver=MagicMock(),
            video_renderer=None,
        )
        mock_renderer = MagicMock()
        worker._comp_frame_renderer = mock_renderer
        worker.cleanup_clips()
        mock_renderer.cleanup.assert_called_once()
        assert worker._comp_frame_renderer is None
