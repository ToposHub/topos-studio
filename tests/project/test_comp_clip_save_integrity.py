"""Tests for Comp Clip data integrity through save/load.

Verifies that comp_clips and active_comp_clip_id are preserved when
EditProject is rebuilt during SaveRouter operations, and that the
EditProjectSerializer roundtrips compositions correctly.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import json

from src.features.edit.core.project import EditProject
from src.features.edit.core.serialization import EditProjectSerializer


# ================================================================
# Helpers
# ================================================================

def _make_project_with_comp_clip():
    """Create a minimal EditProject with comp_clips and active_comp_clip_id."""
    project = EditProject.create("Test Project")

    comp_clip_id = "cc-001"
    comp_asset_id = "asset-cc-001"
    graph_id = "graph-001"

    comp_clips = {
        comp_clip_id: {
            "comp_clip_id": comp_clip_id,
            "asset_id": comp_asset_id,
            "graph_id": graph_id,
            "name": "Test Comp Clip",
        },
    }

    compositions = {
        graph_id: {
            "composition_id": graph_id,
            "name": "Test Composition",
            "nodes": [{"id": "n1", "type": "output"}],
            "connections": [],
        },
    }

    return EditProject(
        project_id=project.project_id,
        name=project.name,
        assets=project.assets,
        bins=project.bins,
        timelines=project.timelines,
        compositions=compositions,
        color_stacks=project.color_stacks,
        comp_clips=comp_clips,
        root_bin_id=project.root_bin_id,
        active_timeline_id=project.active_timeline_id,
        active_comp_clip_id=comp_clip_id,
        created_at=project.created_at,
        modified_at=project.modified_at,
    ), comp_clip_id, graph_id


# ================================================================
# Test: SaveRouter preserves comp_clips in EditProject rebuilds
# ================================================================

class TestSaveRouterCompClipPreservation:
    """Verify that SaveRouter._save_composite_to_edit_project and
    _save_color_to_edit_project preserve comp_clips/active_comp_clip_id
    when rebuilding EditProject.
    """

    def test_composite_save_preserves_comp_clips(self):
        """When _save_composite_to_edit_project rebuilds EditProject,
        comp_clips and active_comp_clip_id must be carried over."""
        from src.shared.project.save_router import SaveRouter

        project, comp_clip_id, graph_id = _make_project_with_comp_clip()
        assert len(project.comp_clips) == 1
        assert project.active_comp_clip_id == comp_clip_id

        router = SaveRouter()

        # Prepare a new composition data to save
        new_composite_id = "graph-002"
        new_comp_data = {
            "composition_id": new_composite_id,
            "name": "Updated Composition",
            "nodes": [{"id": "n2", "type": "color_input"}],
            "connections": [],
        }

        # Call the internal method directly
        # We need a project_path and mock the on_edit_project_updated callback
        updated_projects = []

        def capture_updated(p):
            updated_projects.append(p)

        router.set_callback("on_edit_project_updated", capture_updated)
        router.set_callback("get_ui_state", lambda: None)
        router.set_callback("get_current_timeline_sequences", lambda: None)
        router.set_callback("get_edit_project_io", lambda: None)  # Will cause _save_edit_project_to_topos to fail, but we only care about the rebuild

        # Call _save_composite_to_edit_project
        # It will fail at the final save step, but the EditProject rebuild
        # happens before that
        router._save_composite_to_edit_project(
            project_path="/tmp/test.topos",
            edit_project=project,
            composite_graph_data=new_comp_data,
            composite_viewer_state=None,
            composite_id=new_composite_id,
        )

        # The callback should have been called with the updated project
        assert len(updated_projects) == 1, "on_edit_project_updated should have been called"
        updated = updated_projects[0]

        # comp_clips must be preserved
        assert updated.comp_clips == project.comp_clips, \
            f"comp_clips lost during composite save: expected {project.comp_clips}, got {updated.comp_clips}"
        assert updated.active_comp_clip_id == project.active_comp_clip_id, \
            f"active_comp_clip_id lost during composite save: expected {project.active_comp_clip_id}, got {updated.active_comp_clip_id}"

        # The new composition must also be present
        assert new_composite_id in updated.compositions
        assert updated.compositions[new_composite_id]["name"] == "Updated Composition"

    def test_color_save_new_stack_preserves_comp_clips(self):
        """When _save_color_to_edit_project creates a new color stack,
        comp_clips and active_comp_clip_id must be carried over."""
        from src.shared.project.save_router import SaveRouter

        project, comp_clip_id, graph_id = _make_project_with_comp_clip()
        assert len(project.comp_clips) == 1
        assert project.active_comp_clip_id == comp_clip_id

        router = SaveRouter()
        updated_projects = []

        def capture_updated(p):
            updated_projects.append(p)

        router.set_callback("on_edit_project_updated", capture_updated)
        router.set_callback("get_ui_state", lambda: None)
        router.set_callback("get_current_timeline_sequences", lambda: None)
        router.set_callback("get_edit_project_io", lambda: None)

        # Call with a stack_id that does NOT exist in the project,
        # triggering the "create new stack" path
        new_stack_id = "stack-new-001"

        router._save_color_to_edit_project(
            project_path="/tmp/test.topos",
            edit_project=project,
            engine=None,  # No engine, but we just test rebuild
            asset_id="asset-001",
            clip_id="clip-001",
            stack_id=new_stack_id,
            timeline_id=None,
        )

        assert len(updated_projects) == 1, "on_edit_project_updated should have been called"
        updated = updated_projects[0]

        # comp_clips must be preserved
        assert updated.comp_clips == project.comp_clips, \
            f"comp_clips lost during color save (new stack): expected {project.comp_clips}, got {updated.comp_clips}"
        assert updated.active_comp_clip_id == project.active_comp_clip_id, \
            f"active_comp_clip_id lost during color save (new stack): expected {project.active_comp_clip_id}, got {updated.active_comp_clip_id}"

        # The new stack must exist
        assert new_stack_id in updated.color_stacks


# ================================================================
# Test: EditProjectSerializer roundtrips compositions
# ================================================================

class TestSerializerCompClipRoundtrip:
    """Verify that EditProjectSerializer correctly roundtrips
    comp_clips, active_comp_clip_id, and compositions.
    """

    def test_project_to_dict_includes_comp_clips(self):
        """_project_to_dict must include comp_clips and active_comp_clip_id."""
        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        data = EditProjectSerializer._project_to_dict(project)

        assert "comp_clips" in data, "comp_clips key missing from serialized dict"
        assert data["comp_clips"] == project.comp_clips
        assert "active_comp_clip_id" in data, "active_comp_clip_id key missing"
        assert data["active_comp_clip_id"] == comp_clip_id

    def test_project_to_dict_includes_compositions(self):
        """_project_to_dict must include compositions."""
        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        data = EditProjectSerializer._project_to_dict(project)

        assert "compositions" not in data or isinstance(data.get("compositions", {}), dict)
        # compositions is serialized via color_stacks check - verify it's present
        # Note: compositions is not in the original _project_to_dict output
        # but color_stacks is. Check the actual output.
        # Looking at the code, compositions IS included
        assert "compositions" in data, "compositions key missing from serialized dict"

    def test_roundtrip_preserves_comp_clips(self):
        """Full JSON roundtrip must preserve comp_clips and active_comp_clip_id."""
        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)

        assert restored.comp_clips == project.comp_clips, \
            f"comp_clips mismatch after roundtrip: expected {project.comp_clips}, got {restored.comp_clips}"
        assert restored.active_comp_clip_id == project.active_comp_clip_id, \
            f"active_comp_clip_id mismatch after roundtrip: expected {project.active_comp_clip_id}, got {restored.active_comp_clip_id}"

    def test_roundtrip_preserves_compositions(self):
        """Full JSON roundtrip must preserve compositions."""
        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)

        assert graph_id in restored.compositions, \
            f"composition {graph_id} missing after roundtrip"
        assert restored.compositions[graph_id]["name"] == "Test Composition"

    def test_roundtrip_empty_comp_clips(self):
        """Project without comp_clips should roundtrip cleanly."""
        project = EditProject.create("Empty Project")

        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)

        assert restored.comp_clips == {}
        assert restored.active_comp_clip_id is None

    def test_dict_to_project_reads_comp_clips(self):
        """_dict_to_project must correctly read comp_clips from dict data."""
        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        data = EditProjectSerializer._project_to_dict(project)
        restored = EditProjectSerializer._dict_to_project(data)

        assert restored.comp_clips == project.comp_clips
        assert restored.active_comp_clip_id == comp_clip_id
        assert graph_id in restored.compositions


# ================================================================
# Test: convert_topos_project_to_edit_project preserves data
# ================================================================

class TestConvertToposProjectPreservation:
    """Verify that convert_topos_project_to_edit_project preserves
    comp_clips, compositions, and active_comp_clip_id.
    """

    def test_full_data_preserves_comp_clips(self):
        """When full_data contains comp_clips, they must be carried to EditProject."""
        from src.features.edit.core.edit_project_io import convert_topos_project_to_edit_project
        from src.shared.project.topos_project import ToposProject

        project = EditProject.create("Test")

        # Build a minimal ToposProject
        topos = ToposProject(
            project_id=project.project_id,
            name=project.name,
            project_mode="edit_project",
            assets={},
            bins={},
            timelines={},
            color_stacks={},
            compositions={},
            deliver_presets={},
            ui_state={},
            settings={},
            primary_asset_id=None,
            primary_color_stack_id=None,
            active_timeline_id=None,
            created_at=project.created_at,
            modified_at=project.modified_at,
        )

        full_data = {
            "assets": {},
            "bins": {},
            "timelines": {},
            "color_stacks": {},
            "compositions": {
                "graph-001": {"composition_id": "graph-001", "name": "Comp 1"},
            },
            "comp_clips": {
                "cc-001": {"comp_clip_id": "cc-001", "name": "Clip 1"},
            },
            "root_bin_id": project.root_bin_id,
            "active_comp_clip_id": "cc-001",
        }

        result = convert_topos_project_to_edit_project(topos, full_data)

        assert "cc-001" in result.comp_clips, \
            f"comp_clips not preserved: {result.comp_clips}"
        assert result.active_comp_clip_id == "cc-001", \
            f"active_comp_clip_id not preserved: {result.active_comp_clip_id}"
        assert "graph-001" in result.compositions, \
            f"compositions not preserved: {result.compositions}"
        assert result.root_bin_id == project.root_bin_id, \
            f"root_bin_id not preserved: {result.root_bin_id}"


# ================================================================
# Test: Real .topos package roundtrip (media pool composites must
# survive closing and reopening the app)
# ================================================================

class TestToposPackageCompClipRoundtrip:
    """comp_clips and active_comp_clip_id must survive an actual
    EditProjectIO.save_project / load_project roundtrip through the
    .topos ZIP package. This is the path that previously dropped
    media-pool composites on every app restart."""

    def test_save_load_preserves_comp_clips(self, qapp, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO

        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        io = EditProjectIO()
        try:
            filepath = tmp_path / "comp_roundtrip.topos"
            assert io.save_project(
                filepath, project, None, None, create_backup=False,
                force_project_mode="edit_project",
            )

            loaded = io.load_project(filepath)
            assert loaded is not None, "project failed to load"
            restored = loaded[0]

            assert restored.comp_clips == project.comp_clips, (
                f"comp_clips lost in .topos roundtrip: "
                f"expected {project.comp_clips}, got {restored.comp_clips}"
            )
            assert restored.active_comp_clip_id == comp_clip_id, (
                f"active_comp_clip_id lost in .topos roundtrip: "
                f"expected {comp_clip_id}, got {restored.active_comp_clip_id}"
            )
            assert graph_id in restored.compositions, \
                f"composition {graph_id} missing after .topos roundtrip"
        finally:
            io.cleanup()

    def test_save_without_comp_clips_clears_stale_module(self, qapp, tmp_path):
        """Saving a project whose comp clips were all deleted must not
        resurrect them from a stale edit/comp_clips.json module."""
        from src.features.edit.core.edit_project_io import EditProjectIO

        project, comp_clip_id, graph_id = _make_project_with_comp_clip()

        io = EditProjectIO()
        try:
            filepath = tmp_path / "comp_cleared.topos"
            assert io.save_project(
                filepath, project, None, None, create_backup=False,
                force_project_mode="edit_project",
            )

            # Simulate deleting all comp clips, then saving again.
            cleared = EditProject(
                project_id=project.project_id,
                name=project.name,
                assets=project.assets,
                bins=project.bins,
                timelines=project.timelines,
                compositions=project.compositions,
                color_stacks=project.color_stacks,
                comp_clips={},
                root_bin_id=project.root_bin_id,
                active_timeline_id=project.active_timeline_id,
                active_comp_clip_id=None,
                created_at=project.created_at,
                modified_at=project.modified_at,
            )
            assert io.save_project(
                filepath, cleared, None, None, create_backup=False,
                force_project_mode="edit_project",
            )

            loaded = io.load_project(filepath)
            assert loaded is not None
            restored = loaded[0]
            assert restored.comp_clips == {}, \
                f"stale comp_clips resurrected: {restored.comp_clips}"
            assert restored.active_comp_clip_id is None
        finally:
            io.cleanup()
