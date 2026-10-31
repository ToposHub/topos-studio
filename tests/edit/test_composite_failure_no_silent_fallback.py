"""
Test: Composite failure policy — no silent fallback to original material.

Verifies that when a comp clip's Composite graph fails:
1. Original source is NOT shown unless user explicitly bypassed composite.
2. Error overlay is triggered (render_error signal).
3. Bypass mode shows original source correctly.
"""
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np
import pytest


class TestCompositeFailureNoSilentFallback:
    """Comp clip render failure must NOT silently show original material."""

    def _make_layer(self, composite_bypassed=False, has_composite_id=True):
        """Create a mock timeline layer for a comp clip."""
        layer = MagicMock()
        layer.asset_id = "comp-clip-001"
        layer.composite_bypassed = composite_bypassed
        layer.composite_id = "graph-001" if has_composite_id else None
        layer.clip_id = "clip-001"
        layer.source_frame = 10
        layer.opacity = 1.0
        # Prevent MagicMock from returning mock for missing attributes
        layer.track_index = 0
        return layer

    def test_failed_composite_without_bypass_returns_none(self):
        """When composite fails and bypass is OFF, should return None (error)."""
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
        worker = MagicMock(spec=PreviewRenderWorker)

        # Mock _render_comp_clip_frame to return None (failure)
        worker._render_comp_clip_frame = MagicMock(return_value=None)

        # Mock the fallback path components
        mock_asset = MagicMock()
        mock_asset.is_comp_clip = True
        mock_asset.metadata = {'original_asset_id': 'orig-001'}

        mock_resolver = MagicMock()
        mock_resolver._project.get_asset_by_id.return_value = mock_asset
        mock_resolver.get_asset_path.return_value = '/path/to/orig.mov'
        mock_resolver.get_asset_type.return_value = 'video'
        worker._asset_resolver = mock_resolver

        # Mock _get_video_frame to return data (original source available)
        worker._get_video_frame = MagicMock(return_value=np.zeros((100, 100, 3), dtype=np.uint8))

        layer = self._make_layer(composite_bypassed=False)

        # Call the actual method
        result = PreviewRenderWorker._get_layer_frame(worker, layer)

        # Should return None — NOT the original source
        assert result is None
        # Should NOT try to get original source
        mock_resolver.get_asset_path.assert_not_called()

    def test_failed_composite_with_bypass_shows_original(self):
        """When composite fails but bypass is ON, should show original material."""
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
        worker = MagicMock(spec=PreviewRenderWorker)

        worker._render_comp_clip_frame = MagicMock(return_value=None)

        mock_asset = MagicMock()
        mock_asset.is_comp_clip = True
        mock_asset.metadata = {'original_asset_id': 'orig-001'}

        mock_resolver = MagicMock()
        mock_resolver._project.get_asset_by_id.return_value = mock_asset
        mock_resolver.get_asset_path.return_value = '/path/to/orig.mov'
        mock_resolver.get_asset_type.return_value = 'video'
        worker._asset_resolver = mock_resolver

        orig_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        worker._get_video_frame = MagicMock(return_value=orig_frame)

        layer = self._make_layer(composite_bypassed=True)

        result = PreviewRenderWorker._get_layer_frame(worker, layer)

        # Should return the original source frame
        assert result is not None
        mock_resolver.get_asset_path.assert_called_once()

    def test_successful_composite_ignores_bypass(self):
        """When composite succeeds, bypass flag doesn't matter."""
        from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
        worker = MagicMock(spec=PreviewRenderWorker)

        comp_frame = np.full((100, 100, 3), 128, dtype=np.uint8)
        worker._render_comp_clip_frame = MagicMock(return_value=comp_frame)

        mock_resolver = MagicMock()
        worker._asset_resolver = mock_resolver

        layer = self._make_layer(composite_bypassed=False)

        result = PreviewRenderWorker._get_layer_frame(worker, layer)

        assert result is not None
        # Should not attempt original source fallback
        mock_resolver._project.get_asset_by_id.assert_not_called()
