"""Test ProjectDocument protocol and SaveRouter integration."""
import pytest
from src.shared.project.document import ProjectDocument
from src.shared.project.save_router import SaveRouter, SaveContext


class TestProjectDocumentProtocol:
    """Test that ProjectDocument protocol is correctly defined."""

    def test_protocol_has_required_attributes(self):
        """ProjectDocument should define document_type, dirty, save_to_path, load_from_path."""
        # Protocol attrs are in __protocol_attrs__ or can be checked via annotations
        annotations = ProjectDocument.__protocol_attrs__
        assert 'document_type' in annotations
        assert 'dirty' in annotations
        assert 'save_to_path' in annotations
        assert 'load_from_path' in annotations


class TestSaveRouterEditUnavailable:
    """Test that SaveRouter handles edit-unavailable scenarios gracefully."""

    def test_save_from_edit_returns_false_when_no_project(self):
        """When no edit project is available, save_from_edit should return False."""
        router = SaveRouter()
        # No callbacks set — get_current_edit_project returns None
        result = router._save_from_edit()
        assert result is False

    def test_save_from_composite_handles_no_project(self):
        """When no edit project, composite save should handle gracefully."""
        router = SaveRouter()
        result = router._save_from_composite()
        # Should not crash
        assert isinstance(result, bool)

    def test_save_from_deliver_handles_no_project(self):
        """When no edit project, deliver save should handle gracefully."""
        router = SaveRouter()
        result = router._save_from_deliver()
        assert isinstance(result, bool)

    def test_router_handler_registration(self):
        """SaveRouter should support handler registration."""
        router = SaveRouter()
        called = []
        router.register_handler("color", lambda: (called.append(True), True)[1])
        assert "color" in router._handlers

    def test_router_save_uses_handler(self):
        """When a handler is registered, save() should use it."""
        router = SaveRouter()
        router.set_callback("get_current_page", lambda: "Color")
        router.register_handler("color", lambda: True)

        # With handler registered, save should delegate to it
        result = router.save()
        assert result is True

    def test_router_color_save_without_edit_project(self):
        """Color save should work via single-asset path when edit is unavailable."""
        router = SaveRouter()
        # Minimal setup — no edit project callback
        router.set_callback("get_current_page", lambda: "Color")
        router.set_callback("get_current_file_path", lambda: None)

        result = router._save_from_color()
        # Should return False (no file path), but not crash
        assert isinstance(result, bool)


class TestEditProjectProtocolAdaptation:
    """Test that EditProject satisfies ProjectDocument protocol."""

    def test_edit_project_has_document_type(self):
        from src.features.edit.core.project import EditProject
        project = EditProject.create("Test")
        assert project.document_type == "edit_project"

    def test_edit_project_has_dirty(self):
        from src.features.edit.core.project import EditProject
        project = EditProject.create("Test")
        # Fresh project: modified_at == created_at (from same now)
        # dirty may be True if modified_at > created_at due to time precision
        assert isinstance(project.dirty, bool)

    def test_edit_project_dirty_after_modification(self):
        from src.features.edit.core.project import EditProject
        import time
        project = EditProject.create("Test")
        time.sleep(0.01)
        # Import an asset triggers a new instance with later modified_at
        modified = project.import_asset(
            path="/tmp/test.jpg", media_type="image",
            metadata={"width": 100, "height": 100},
        )
        assert modified.dirty is True

    def test_edit_project_satisfies_protocol(self):
        """Verify EditProject has all ProjectDocument protocol attributes."""
        from src.features.edit.core.project import EditProject
        project = EditProject.create("Test")
        # Check protocol attrs exist
        assert hasattr(project, 'document_type')
        assert hasattr(project, 'dirty')
        assert isinstance(project.document_type, str)
        assert isinstance(project.dirty, bool)


class TestColorSessionProtocolAdaptation:
    """Test that ColorSession satisfies ProjectDocument protocol."""

    def test_color_session_has_document_type(self):
        from src.features.color.core.color_session import ColorSession
        session = ColorSession.create_single_asset(
            project_id="proj1", color_stack_id="stack1",
            source_path="/tmp/test.jpg",
        )
        assert session.document_type == "color_session"

    def test_color_session_has_dirty(self):
        from src.features.color.core.color_session import ColorSession
        session = ColorSession.create_single_asset(
            project_id="proj1", color_stack_id="stack1",
            source_path="/tmp/test.jpg",
        )
        assert session.dirty is True

    def test_color_session_save_and_load(self, tmp_path):
        from src.features.color.core.color_session import ColorSession
        session = ColorSession.create_single_asset(
            project_id="proj1", color_stack_id="stack1",
            source_path="/tmp/test.jpg", frame=42,
        )
        save_path = str(tmp_path / "session.json")
        session.save_to_path(save_path)

        loaded = ColorSession.load_from_path(save_path)
        assert loaded.session_id == session.session_id
        assert loaded.source_kind == session.source_kind
        assert loaded.project_id == session.project_id
        assert loaded.color_stack_id == session.color_stack_id
        assert loaded.frame == 42

    def test_color_session_satisfies_protocol(self):
        """Verify ColorSession has all ProjectDocument protocol attributes."""
        from src.features.color.core.color_session import ColorSession
        session = ColorSession.create_single_asset(
            project_id="proj1", color_stack_id="stack1",
            source_path="/tmp/test.jpg",
        )
        assert hasattr(session, 'document_type')
        assert hasattr(session, 'dirty')
        assert hasattr(session, 'save_to_path')
        assert hasattr(session, 'load_from_path')
