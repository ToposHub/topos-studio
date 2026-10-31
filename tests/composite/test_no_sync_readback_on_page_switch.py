"""
Test: No sync readback on page switch / playback hot paths.

Verifies that page switching and frame display do NOT call
the blocking request_output_readback() method.
"""
from unittest.mock import MagicMock, patch, PropertyMock
import pytest


class TestNoSyncReadbackOnPageSwitch:
    """Ensure composite_controller._on_composite_frame_ready never calls
    request_output_readback (sync) — it should use async instead."""

    def test_frame_ready_with_texture_uses_shared_texture(self):
        """When tex_id > 0, should call set_texture directly."""
        controller = MagicMock()
        controller._composite_adapter = None
        controller.current_page = "Composite"
        controller.composite_viewer = MagicMock()
        controller.composite_page = MagicMock()

        # Import the mixin method
        from src.app.controllers.composite_controller import CompositeControllerMixin
        method = CompositeControllerMixin._on_composite_frame_ready

        method(controller, request_id=1, tex_id=42, width=1920, height=1080)

        controller.composite_viewer.set_texture.assert_called_once_with(42, 1920, 1080)
        controller.composite_page.request_output_readback.assert_not_called()

    def test_frame_ready_without_texture_uses_async_not_sync(self):
        """When tex_id is 0, should use async readback, NOT sync readback."""
        controller = MagicMock()
        controller._composite_adapter = None
        controller.current_page = "Composite"
        controller.composite_viewer = MagicMock()

        # Set up composite_page with a mock renderer
        mock_renderer = MagicMock()
        mock_page = MagicMock()
        mock_page._renderer = mock_renderer
        mock_page.get_preview_image.return_value = None  # no preview image available
        controller.composite_page = mock_page

        from src.app.controllers.composite_controller import CompositeControllerMixin
        method = CompositeControllerMixin._on_composite_frame_ready

        method(controller, request_id=2, tex_id=0, width=1920, height=1080)

        # Must NOT call sync readback
        mock_page.request_output_readback.assert_not_called()

        # Should submit async readback to renderer instead
        mock_renderer.submit.assert_called_once()

    def test_frame_ready_with_adapter_delegates(self):
        """When _composite_adapter is set, delegate entirely to adapter."""
        controller = MagicMock()
        controller._composite_adapter = MagicMock()
        controller.composite_viewer = MagicMock()
        controller.composite_page = MagicMock()

        from src.app.controllers.composite_controller import CompositeControllerMixin
        method = CompositeControllerMixin._on_composite_frame_ready

        method(controller, request_id=3, tex_id=0, width=1920, height=1080)

        controller._composite_adapter.handle_frame_ready.assert_called_once_with(3, 0, 1920, 1080)
        controller.composite_page.request_output_readback.assert_not_called()
        controller.composite_viewer.set_texture.assert_not_called()

    def test_frame_ready_not_on_composite_page_skips(self):
        """When not on Composite page, do nothing."""
        controller = MagicMock()
        controller._composite_adapter = None
        controller.current_page = "Edit"
        controller.composite_viewer = MagicMock()

        from src.app.controllers.composite_controller import CompositeControllerMixin
        method = CompositeControllerMixin._on_composite_frame_ready

        method(controller, request_id=4, tex_id=42, width=1920, height=1080)

        controller.composite_viewer.set_texture.assert_not_called()
