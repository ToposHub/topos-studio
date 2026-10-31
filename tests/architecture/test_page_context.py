"""
Test Page Context Management (P9).

Tests for PageContext, PageContextManager, and MainWindow page switching coordination.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass

# Import from main_window module
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app.main_window import PageContext, PageContextManager, PageType


class TestPageType:
    """Test PageType enum."""

    def test_page_type_values(self):
        """Test that PageType has all expected values."""
        assert PageType.LIBRARY.value == "Library"
        assert PageType.EDIT.value == "Edit"
        assert PageType.COLOR.value == "Color"
        assert PageType.COMPOSITE.value == "Composite"
        assert PageType.DELIVER.value == "Deliver"

    def test_page_type_count(self):
        """Test that we have 5 page types."""
        assert len(PageType) == 5


class TestPageContext:
    """Test PageContext dataclass."""

    def test_default_context(self):
        """Test default PageContext values."""
        ctx = PageContext()
        assert ctx.source_page == PageType.COLOR
        assert ctx.target_page == PageType.COLOR
        assert ctx.clip_id is None
        assert ctx.asset_id is None
        assert ctx.timeline_frame is None
        assert ctx.source_frame is None
        assert ctx.clip_local_frame is None
        assert ctx.color_stack_id is None
        assert ctx.composite_id is None
        assert ctx.timeline_id is None
        assert ctx.deliver_source == "color"

    def test_context_with_values(self):
        """Test PageContext with custom values."""
        ctx = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
            clip_id="clip-123",
            asset_id="asset-456",
            timeline_frame=42,
            source_frame=12,
            clip_local_frame=2,
            timeline_id="timeline-001",
            color_stack_id="stack-789",
        )
        assert ctx.source_page == PageType.EDIT
        assert ctx.target_page == PageType.COLOR
        assert ctx.clip_id == "clip-123"
        assert ctx.asset_id == "asset-456"
        assert ctx.timeline_frame == 42
        assert ctx.source_frame == 12
        assert ctx.clip_local_frame == 2
        assert ctx.timeline_id == "timeline-001"
        assert ctx.color_stack_id == "stack-789"

    def test_context_to_dict(self):
        """Test PageContext serialization to dict."""
        ctx = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COMPOSITE,
            clip_id="clip-001",
            composite_id="comp-002",
        )
        data = ctx.to_dict()
        assert data["source_page"] == "Edit"
        assert data["target_page"] == "Composite"
        assert data["clip_id"] == "clip-001"
        assert data["composite_id"] == "comp-002"

    def test_context_from_dict(self):
        """Test PageContext deserialization from dict."""
        data = {
            "source_page": "Edit",
            "target_page": "Deliver",
            "clip_id": "clip-abc",
            "timeline_id": "timeline-def",
            "deliver_source": "timeline",
        }
        ctx = PageContext.from_dict(data)
        assert ctx.source_page == PageType.EDIT
        assert ctx.target_page == PageType.DELIVER
        assert ctx.clip_id == "clip-abc"
        assert ctx.timeline_id == "timeline-def"
        assert ctx.deliver_source == "timeline"

    def test_context_roundtrip(self):
        """Test PageContext serialization roundtrip."""
        original = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
            clip_id="clip-xyz",
            asset_id="asset-uvw",
            timeline_frame=100,
            source_frame=25,
            clip_local_frame=10,
            timeline_id="timeline-xyz",
            color_stack_id="stack-rst",
        )
        data = original.to_dict()
        restored = PageContext.from_dict(data)
        assert restored.source_page == original.source_page
        assert restored.target_page == original.target_page
        assert restored.clip_id == original.clip_id
        assert restored.asset_id == original.asset_id
        assert restored.timeline_frame == original.timeline_frame
        assert restored.source_frame == original.source_frame
        assert restored.clip_local_frame == original.clip_local_frame
        assert restored.timeline_id == original.timeline_id
        assert restored.color_stack_id == original.color_stack_id


class TestPageContextManager:
    """Test PageContextManager class."""

    def test_manager_init(self):
        """Test PageContextManager initialization."""
        manager = PageContextManager()
        assert manager.get_current_context() is None
        # All pages should be clean initially
        for page in PageType:
            assert manager.is_page_dirty(page) is False

    def test_set_current_context(self):
        """Test setting current context."""
        manager = PageContextManager()
        ctx = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
            clip_id="clip-001",
        )
        manager.set_current_context(ctx)
        assert manager.get_current_context() == ctx

    def test_get_last_context(self):
        """Test getting last context for a page."""
        manager = PageContextManager()
        ctx = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
        )
        manager.set_current_context(ctx)
        last_ctx = manager.get_last_context(PageType.COLOR)
        assert last_ctx == ctx

    def test_page_dirty_state(self):
        """Test page dirty state management."""
        manager = PageContextManager()
        # Set Edit page dirty
        manager.set_page_dirty(PageType.EDIT, True)
        assert manager.is_page_dirty(PageType.EDIT) is True
        assert manager.is_page_dirty(PageType.COLOR) is False

        # Clean Edit page
        manager.set_page_dirty(PageType.EDIT, False)
        assert manager.is_page_dirty(PageType.EDIT) is False

    def test_any_page_dirty(self):
        """Test checking if any page is dirty."""
        manager = PageContextManager()
        assert manager.any_page_dirty() is False

        manager.set_page_dirty(PageType.COLOR, True)
        assert manager.any_page_dirty() is True

        manager.set_page_dirty(PageType.COLOR, False)
        assert manager.any_page_dirty() is False

    def test_create_context_for_edit_to_color(self):
        """Test creating Edit → Color context."""
        manager = PageContextManager()
        ctx = manager.create_context_for_edit_to_color(
            clip_id="clip-001",
            asset_id="asset-002",
            timeline_id="timeline-003",
            timeline_frame=50,
            source_frame=20,
            clip_local_frame=5,
            color_stack_id="stack-003",
        )
        assert ctx.source_page == PageType.EDIT
        assert ctx.target_page == PageType.COLOR
        assert ctx.clip_id == "clip-001"
        assert ctx.asset_id == "asset-002"
        assert ctx.timeline_id == "timeline-003"
        assert ctx.timeline_frame == 50
        assert ctx.source_frame == 20
        assert ctx.clip_local_frame == 5
        assert ctx.color_stack_id == "stack-003"

    def test_create_context_for_edit_to_composite(self):
        """Test creating Edit → Composite context."""
        manager = PageContextManager()
        ctx = manager.create_context_for_edit_to_composite(
            clip_id="clip-004",
            composite_id="comp-005",
            create_new=False,
        )
        assert ctx.source_page == PageType.EDIT
        assert ctx.target_page == PageType.COMPOSITE
        assert ctx.clip_id == "clip-004"
        assert ctx.composite_id == "comp-005"

    def test_create_context_for_to_deliver(self):
        """Test creating Deliver context."""
        manager = PageContextManager()

        # Test with color source
        ctx = manager.create_context_for_to_deliver(source="color")
        assert ctx.source_page == PageType.COLOR
        assert ctx.target_page == PageType.DELIVER
        assert ctx.deliver_source == "color"

        # Test with timeline source
        ctx = manager.create_context_for_to_deliver(
            source="timeline",
            timeline_id="timeline-006",
        )
        assert ctx.source_page == PageType.EDIT
        assert ctx.target_page == PageType.DELIVER
        assert ctx.timeline_id == "timeline-006"
        assert ctx.deliver_source == "timeline"


class TestPageContextIntegration:
    """Integration tests for page context with MainWindow."""

    @pytest.fixture
    def mock_main_window(self):
        """Create a mock MainWindow for testing."""
        # Create minimal mock with necessary attributes
        mock = MagicMock()
        mock._page_context_manager = PageContextManager()
        mock.current_page = "Color"
        mock.page_layouts = {}
        mock.status_bar = MagicMock()
        mock.canvas = MagicMock()
        mock.canvas.cancel_active_tools = MagicMock()
        mock.canvas.set_sam_mode = MagicMock()
        mock.canvas.clear_sam_points = MagicMock()
        mock.mask_panel = MagicMock()
        mock.mask_panel.btn_brush = MagicMock()
        mock.qualifier_panel = MagicMock()
        mock.qualifier_panel.set_picker_active = MagicMock()
        mock.page_buttons = {}
        mock.engine = MagicMock()
        mock.engine.reset_sam_points = MagicMock()

        # Mock page methods
        mock.leave_page_hook = MagicMock()
        mock.enter_page_hook = MagicMock()
        mock._handle_enter_color_with_context = MagicMock()
        mock._handle_enter_composite_with_context = MagicMock()
        mock._handle_enter_deliver_with_context = MagicMock()
        mock._handle_enter_edit_with_context = MagicMock()
        mock.saveState = MagicMock(return_value=MagicMock())
        mock.restoreState = MagicMock()
        mock.update_all_dock_titlebars = MagicMock()

        return mock

    def test_switch_to_page_with_context(self, mock_main_window):
        """Test switch_to_page_with_context method."""
        # We need to bind the real method to the mock
        from src.app.main_window import MainWindow

        # Get the unbound method and bind it
        method = MainWindow.switch_to_page_with_context
        bound_method = method.__get__(mock_main_window, MainWindow)

        ctx = PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
            clip_id="clip-001",
            asset_id="asset-002",
        )

        # Call the method
        bound_method(ctx)

        # Verify context was set
        assert mock_main_window._page_context_manager.get_current_context() == ctx


class TestExportPanelMethods:
    """Test ExportPanel methods for deliver source."""

    def test_set_export_source(self):
        """Test setting export source."""
        from src.shared.export.export_panel import ExportPanel

        # Create panel without Qt app
        panel = ExportPanel.__new__(ExportPanel)

        # Test set_export_source method
        panel._export_source = "color"
        panel.set_export_source("timeline")

        assert panel.get_export_source() == "timeline"

    def test_set_timeline_id(self):
        """Test setting timeline ID."""
        from src.shared.export.export_panel import ExportPanel

        panel = ExportPanel.__new__(ExportPanel)

        panel._timeline_id = None
        panel.set_timeline_id("timeline-001")

        assert panel.get_timeline_id() == "timeline-001"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
