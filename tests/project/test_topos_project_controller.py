"""
Tests for ToposProjectController.

Tests for unified dirty state management, auto-save mechanism,
and SaveRouter coordination.

Test Categories:
- Dirty state management: mark_dirty, clear_dirty, is_dirty, is_page_dirty
- Save coordination: save, save_page, save_callback
- Auto-save mechanism: start_auto_save, stop_auto_save, timer behavior
- Signal behavior: dirty_changed, auto_save_triggered, save_completed
- State serialization: to_dict, from_dict
"""

import pytest
import time
from unittest.mock import MagicMock, patch
from PyQt6.QtCore import QTimer

# Import from project module
from src.shared.project.topos_project_controller import (
    ToposProjectController,
    PageType,
    DirtyState,
    DirtyStateChanged,
)


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


class TestDirtyState:
    """Test DirtyState dataclass."""

    def test_default_state(self):
        """Test default DirtyState values."""
        state = DirtyState(page=PageType.COLOR, is_dirty=False)
        assert state.page == PageType.COLOR
        assert state.is_dirty is False
        assert state.reason is None
        assert state.timestamp == 0.0

    def test_state_with_values(self):
        """Test DirtyState with custom values."""
        state = DirtyState(
            page=PageType.EDIT,
            is_dirty=True,
            reason="Added clip",
            timestamp=12345.0,
        )
        assert state.page == PageType.EDIT
        assert state.is_dirty is True
        assert state.reason == "Added clip"
        assert state.timestamp == 12345.0

    def test_state_frozen(self):
        """Test that DirtyState is frozen (immutable)."""
        state = DirtyState(page=PageType.COLOR, is_dirty=True)
        with pytest.raises(Exception):  # FrozenInstanceError
            state.is_dirty = False


class TestDirtyStateChanged:
    """Test DirtyStateChanged event."""

    def test_event_creation(self):
        """Test DirtyStateChanged creation."""
        event = DirtyStateChanged(
            page=PageType.COLOR,
            old_state=False,
            new_state=True,
            reason="Parameter change",
        )
        assert event.page == PageType.COLOR
        assert event.old_state is False
        assert event.new_state is True
        assert event.reason == "Parameter change"


class TestToposProjectControllerInit:
    """Test ToposProjectController initialization."""

    def test_init_default_state(self, qapp):
        """Test default initialization state."""
        controller = ToposProjectController()
        assert controller.is_dirty() is False
        assert controller._save_callback is None
        assert controller._auto_save_enabled is False
        assert controller._last_save_timestamp == 0.0

    def test_init_all_pages_clean(self, qapp):
        """Test that all pages start clean."""
        controller = ToposProjectController()
        for page in PageType:
            assert controller.is_page_dirty(page) is False

    def test_init_with_parent(self, qapp):
        """Test initialization with parent QObject."""
        controller = ToposProjectController(parent=qapp)
        assert controller.parent() == qapp


class TestDirtyStateManagement:
    """Test dirty state management methods."""

    def test_mark_dirty_single_page(self, qapp):
        """Test marking a single page dirty."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Parameter adjustment")

        assert controller.is_dirty() is True
        assert controller.is_page_dirty(PageType.COLOR) is True
        assert controller.is_page_dirty(PageType.EDIT) is False

    def test_mark_dirty_multiple_pages(self, qapp):
        """Test marking multiple pages dirty."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Color change")
        controller.mark_dirty(PageType.EDIT, "Added clip")

        assert controller.is_dirty() is True
        assert controller.is_page_dirty(PageType.COLOR) is True
        assert controller.is_page_dirty(PageType.EDIT) is True

    def test_mark_dirty_already_dirty_page(self, qapp):
        """Test marking an already dirty page."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "First change")
        controller.mark_dirty(PageType.COLOR, "Second change")

        # Should still be dirty
        assert controller.is_page_dirty(PageType.COLOR) is True

        # Check reason is updated
        state = controller.get_dirty_state(PageType.COLOR)
        assert state.reason == "Second change"

    def test_clear_dirty_single_page(self, qapp):
        """Test clearing dirty for a single page."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.mark_dirty(PageType.EDIT, "Change")

        controller.clear_dirty(PageType.COLOR)

        assert controller.is_page_dirty(PageType.COLOR) is False
        assert controller.is_page_dirty(PageType.EDIT) is True
        assert controller.is_dirty() is True

    def test_clear_dirty_all_pages(self, qapp):
        """Test clearing all dirty pages."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.mark_dirty(PageType.EDIT, "Change")

        controller.clear_all_dirty()

        assert controller.is_dirty() is False
        for page in PageType:
            assert controller.is_page_dirty(page) is False

    def test_clear_dirty_already_clean_page(self, qapp):
        """Test clearing an already clean page."""
        controller = ToposProjectController()
        controller.clear_dirty(PageType.COLOR)  # Already clean

        assert controller.is_page_dirty(PageType.COLOR) is False

    def test_get_dirty_pages(self, qapp):
        """Test getting list of dirty pages."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.mark_dirty(PageType.EDIT, "Change")

        dirty_pages = controller.get_dirty_pages()
        assert PageType.COLOR in dirty_pages
        assert PageType.EDIT in dirty_pages
        assert len(dirty_pages) == 2

    def test_get_dirty_state(self, qapp):
        """Test getting complete dirty state for a page."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Test reason")

        state = controller.get_dirty_state(PageType.COLOR)
        assert state is not None
        assert state.page == PageType.COLOR
        assert state.is_dirty is True
        assert state.reason == "Test reason"

    def test_get_all_dirty_states(self, qapp):
        """Test getting all dirty states."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")

        states = controller.get_all_dirty_states()
        assert len(states) == 5
        assert states[PageType.COLOR].is_dirty is True
        assert states[PageType.EDIT].is_dirty is False


class TestSaveCoordination:
    """Test save coordination methods."""

    def test_set_save_callback(self, qapp):
        """Test setting save callback."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)

        assert controller._save_callback == callback

    def test_save_no_callback(self, qapp):
        """Test save without callback set."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")

        success = controller.save()
        assert success is False
        assert controller.is_dirty() is True  # Dirty state preserved

    def test_save_no_dirty_state(self, qapp):
        """Test save when no dirty state."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)

        success = controller.save()
        assert success is True
        # Callback should not be called (no dirty state)
        callback.assert_not_called()

    def test_save_success(self, qapp):
        """Test successful save."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")

        success = controller.save()
        assert success is True
        callback.assert_called_once()
        assert controller.is_dirty() is False  # Dirty cleared

    def test_save_failure_preserves_dirty(self, qapp):
        """Test that save failure preserves dirty state."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=False)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")

        success = controller.save()
        assert success is False
        callback.assert_called_once()
        assert controller.is_dirty() is True  # Dirty preserved

    def test_save_exception_preserves_dirty(self, qapp):
        """Test that save exception preserves dirty state."""
        controller = ToposProjectController()
        callback = MagicMock(side_effect=Exception("Save failed"))
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")

        success = controller.save()
        assert success is False
        assert controller.is_dirty() is True  # Dirty preserved

    def test_save_page(self, qapp):
        """Test saving specific page."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")

        success = controller.save_page(PageType.COLOR)
        assert success is True
        assert controller.is_dirty() is False


class TestAutoSave:
    """Test auto-save mechanism."""

    def test_start_auto_save(self, qapp):
        """Test starting auto-save."""
        controller = ToposProjectController()
        controller.start_auto_save(interval_minutes=5)

        assert controller.is_auto_save_enabled() is True
        assert controller._auto_save_timer is not None
        assert controller._auto_save_interval_ms == 5 * 60 * 1000

    def test_stop_auto_save(self, qapp):
        """Test stopping auto-save."""
        controller = ToposProjectController()
        controller.start_auto_save(interval_minutes=5)
        controller.stop_auto_save()

        assert controller.is_auto_save_enabled() is False
        assert not controller._auto_save_timer.isActive()

    def test_set_auto_save_interval(self, qapp):
        """Test setting auto-save interval."""
        controller = ToposProjectController()
        controller.start_auto_save(interval_minutes=5)
        controller.set_auto_save_interval(10)

        assert controller._auto_save_interval_ms == 10 * 60 * 1000
        # Timer interval should be updated
        assert controller._auto_save_timer.interval() == 10 * 60 * 1000

    def test_auto_save_timer_skips_clean(self, qapp):
        """Test that auto-save timer skips when no dirty state."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.start_auto_save(interval_minutes=1)

        # No dirty state, timer should not trigger save
        controller._on_auto_save_timer()
        callback.assert_not_called()

    def test_auto_save_timer_triggers_on_dirty(self, qapp):
        """Test that auto-save timer triggers save when dirty."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.start_auto_save(interval_minutes=1)

        # Dirty state exists, timer should trigger save
        controller._on_auto_save_timer()
        callback.assert_called_once()
        assert controller.is_dirty() is False

    def test_get_last_save_timestamp(self, qapp):
        """Test getting last save timestamp."""
        controller = ToposProjectController()
        assert controller.get_last_save_timestamp() == 0.0

        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.save()

        assert controller.get_last_save_timestamp() > 0.0


class TestSignals:
    """Test signal behavior."""

    def test_dirty_changed_signal(self, qapp):
        """Test dirty_changed signal emission."""
        controller = ToposProjectController()
        signal_handler = MagicMock()
        controller.dirty_changed.connect(signal_handler)

        controller.mark_dirty(PageType.COLOR, "Change")

        signal_handler.assert_called_once()
        event = signal_handler.call_args[0][0]
        assert isinstance(event, DirtyStateChanged)
        assert event.page == PageType.COLOR
        assert event.old_state is False
        assert event.new_state is True

    def test_global_dirty_changed_signal(self, qapp):
        """Test global_dirty_changed signal emission."""
        controller = ToposProjectController()
        signal_handler = MagicMock()
        controller.global_dirty_changed.connect(signal_handler)

        controller.mark_dirty(PageType.COLOR, "Change")
        signal_handler.assert_called_with(True)

        controller.clear_dirty(PageType.COLOR)
        signal_handler.assert_called_with(False)

    def test_save_completed_signal_success(self, qapp):
        """Test save_completed signal on success."""
        controller = ToposProjectController()
        signal_handler = MagicMock()
        controller.save_completed.connect(signal_handler)

        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.save()

        signal_handler.assert_called_with(True)

    def test_save_completed_signal_failure(self, qapp):
        """Test save_completed signal on failure."""
        controller = ToposProjectController()
        signal_handler = MagicMock()
        controller.save_completed.connect(signal_handler)

        callback = MagicMock(return_value=False)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.save()

        signal_handler.assert_called_with(False)

    def test_auto_save_triggered_signal(self, qapp):
        """Test auto_save_triggered signal."""
        controller = ToposProjectController()
        signal_handler = MagicMock()
        controller.auto_save_triggered.connect(signal_handler)

        controller.mark_dirty(PageType.COLOR, "Change")
        controller._on_auto_save_timer()

        signal_handler.assert_called_once()


class TestStateSerialization:
    """Test state serialization methods."""

    def test_to_dict(self, qapp):
        """Test serialization to dict."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.start_auto_save(interval_minutes=10)

        data = controller.to_dict()

        assert "page_dirty_states" in data
        assert data["page_dirty_states"]["Color"]["is_dirty"] is True
        assert data["page_dirty_states"]["Edit"]["is_dirty"] is False
        assert data["auto_save_enabled"] is True
        assert data["auto_save_interval_ms"] == 10 * 60 * 1000

    def test_from_dict(self, qapp):
        """Test deserialization from dict."""
        controller = ToposProjectController()

        data = {
            "page_dirty_states": {
                "Color": {
                    "is_dirty": True,
                    "reason": "Test",
                    "timestamp": 100.0,
                },
                "Edit": {
                    "is_dirty": False,
                    "reason": None,
                    "timestamp": 0.0,
                },
            },
            "auto_save_enabled": True,
            "auto_save_interval_ms": 10 * 60 * 1000,
            "last_save_timestamp": 50.0,
        }

        controller.from_dict(data)

        assert controller.is_page_dirty(PageType.COLOR) is True
        assert controller.is_page_dirty(PageType.EDIT) is False
        assert controller.is_auto_save_enabled() is True
        assert controller.get_last_save_timestamp() == 50.0

    def test_serialization_roundtrip(self, qapp):
        """Test serialization roundtrip."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.mark_dirty(PageType.EDIT, "Clip")

        data = controller.to_dict()
        new_controller = ToposProjectController()
        new_controller.from_dict(data)

        assert new_controller.is_page_dirty(PageType.COLOR) is True
        assert new_controller.is_page_dirty(PageType.EDIT) is True
        assert new_controller.is_page_dirty(PageType.COMPOSITE) is False


class TestReset:
    """Test reset functionality."""

    def test_reset_clears_dirty(self, qapp):
        """Test that reset clears dirty states."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.reset()

        assert controller.is_dirty() is False

    def test_reset_stops_auto_save(self, qapp):
        """Test that reset stops auto-save."""
        controller = ToposProjectController()
        controller.start_auto_save(interval_minutes=5)
        controller.reset()

        assert controller.is_auto_save_enabled() is False

    def test_reset_clears_timestamp(self, qapp):
        """Test that reset clears save timestamp."""
        controller = ToposProjectController()
        callback = MagicMock(return_value=True)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "Change")
        controller.save()
        assert controller.get_last_save_timestamp() > 0.0

        controller.reset()
        assert controller.get_last_save_timestamp() == 0.0


class TestDirtySummary:
    """Test dirty summary generation."""

    def test_summary_no_dirty(self, qapp):
        """Test summary when no dirty pages."""
        controller = ToposProjectController()
        summary = controller.get_dirty_summary()
        assert summary == "No unsaved changes"

    def test_summary_single_dirty(self, qapp):
        """Test summary with single dirty page."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Parameter change")
        summary = controller.get_dirty_summary()
        assert "Color" in summary
        assert "Parameter change" in summary

    def test_summary_multiple_dirty(self, qapp):
        """Test summary with multiple dirty pages."""
        controller = ToposProjectController()
        controller.mark_dirty(PageType.COLOR, "Color change")
        controller.mark_dirty(PageType.EDIT, "Added clip")
        summary = controller.get_dirty_summary()
        assert "Color" in summary
        assert "Edit" in summary


if __name__ == "__main__":
    pytest.main([__file__, "-v"])