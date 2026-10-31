"""
Tests for CompositeGraphRenderService contract.

Verifies the service interface, CPU rendering path, and GPU fallback behavior.
"""
import pytest
import numpy as np
from unittest.mock import MagicMock, patch

from src.features.composite.render.composite_graph_render_service import (
    CompositeGraphRenderService,
    GpuFrameHandle,
)
from src.shared.gpu.composite_gpu_frame_handle import CompositeGpuFrameHandle


class TestGpuFrameHandle:
    """Test GpuFrameHandle dataclass."""

    def test_valid_handle(self):
        h = GpuFrameHandle(texture_id=1, width=1920, height=1080)
        assert h.is_valid

    def test_invalid_zero_texture(self):
        h = GpuFrameHandle(texture_id=0, width=1920, height=1080)
        assert not h.is_valid

    def test_invalid_zero_dimensions(self):
        h = GpuFrameHandle(texture_id=1, width=0, height=0)
        assert not h.is_valid

    def test_frozen(self):
        h = GpuFrameHandle(texture_id=1, width=1920, height=1080)
        with pytest.raises(AttributeError):
            h.texture_id = 2


class TestCompositeGraphRenderServiceCpu:
    """Test CPU rendering path."""

    @patch("src.features.composite.render.composite_graph_render_service.CompositeFrameRenderer", create=True)
    def test_render_cpu_returns_numpy(self, MockRenderer):
        """CPU render returns BGR float32 numpy array."""
        mock_instance = MagicMock()
        mock_instance.render_comp_clip_frame.return_value = np.zeros((1080, 1920, 3), dtype=np.float32)
        MockRenderer.return_value = mock_instance

        # Patch the import inside the method
        with patch("src.features.composite.render.composite_frame_renderer.CompositeFrameRenderer", return_value=mock_instance):
            service = CompositeGraphRenderService()
            result = service.render_cpu(graph_data={"nodes": [], "connections": []})

        assert result is not None
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_render_cpu_returns_none_on_failure(self):
        """CPU render returns None when graph is invalid (mocked renderer)."""
        mock_renderer = MagicMock()
        mock_renderer.render_comp_clip_frame.return_value = None

        service = CompositeGraphRenderService()
        service._composite_renderer = mock_renderer

        result = service.render_cpu(graph_data={"nodes": [], "connections": []})
        assert result is None


class TestCompositeGraphRenderServiceGpu:
    """Test GPU rendering path and its explicit unavailable fallback."""

    def test_render_gpu_returns_none_when_no_gl_context(self):
        """GPU path returns None when no GL context is configured."""
        service = CompositeGraphRenderService()
        result = service.render_gpu(graph_data={"nodes": [], "connections": []})
        assert result is None

    def test_render_gpu_with_gl_context_uses_composite_gpu_handle(self):
        """Configured GPU service delegates to the shared handle producer."""
        mock_ctx = MagicMock()
        service = CompositeGraphRenderService(gl_context=mock_ctx)
        handle = CompositeGpuFrameHandle(
            frame=12, generation=3, texture_id=7, width=100, height=50,
        )
        renderer = MagicMock()
        renderer.render_comp_clip_gpu.return_value = handle
        service._composite_renderer = renderer

        result = service.render_gpu(
            graph_data={"nodes": [], "connections": []},
            timeline_frame=12,
            asset_id="asset-7",
            generation=3,
            trace_id="trace-gpu",
        )

        assert result is handle
        renderer.render_comp_clip_gpu.assert_called_once()
        kwargs = renderer.render_comp_clip_gpu.call_args.kwargs
        assert kwargs["timeline_frame"] == 12
        assert kwargs["asset_id"] == "asset-7"
        assert kwargs["generation"] == 3
        assert kwargs["trace_id"] == "trace-gpu"
        assert service.get_telemetry()[-1]["path"] == "gpu"
        assert service.get_telemetry()[-1]["success"] is True

    def test_render_gpu_returns_unavailable_handle_reason(self):
        """Producer failures remain observable instead of becoming a silent None."""
        service = CompositeGraphRenderService(gl_context=MagicMock())
        renderer = MagicMock()
        renderer.render_comp_clip_gpu.return_value = CompositeGpuFrameHandle(
            available=False,
            fallback_reason="context reset",
        )
        service._composite_renderer = renderer

        result = service.render_gpu(graph_data={"nodes": []})

        assert result is not None
        assert result.is_presentable() is False
        assert result.fallback_reason == "context reset"
        assert service.get_telemetry()[-1]["success"] is False
        assert service.get_telemetry()[-1]["error"] == "context reset"


class TestCompositeGraphRenderServiceTelemetry:
    """Test telemetry recording."""

    def test_telemetry_recorded_on_render(self):
        service = CompositeGraphRenderService()
        # Use mock renderer to avoid GL context crash
        mock_renderer = MagicMock()
        mock_renderer.render_comp_clip_frame.return_value = np.zeros((100, 100, 3), dtype=np.float32)
        service._composite_renderer = mock_renderer

        service.render_cpu(graph_data={"nodes": [], "connections": []})

        telemetry = service.get_telemetry()
        assert len(telemetry) >= 1
        assert "path" in telemetry[0]
        assert "elapsed_ms" in telemetry[0]

    def test_telemetry_capped_at_100(self):
        service = CompositeGraphRenderService()
        for i in range(150):
            service._record_telemetry(frame=i, path="cpu", elapsed_ms=1.0, success=True)
        telemetry = service.get_telemetry()
        assert len(telemetry) == 100

    def test_cleanup_releases_renderer(self):
        service = CompositeGraphRenderService()
        service.cleanup()
        assert service._composite_renderer is None
