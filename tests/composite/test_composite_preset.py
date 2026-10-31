"""Test PresetManager - node preset/macro management."""
import pytest
import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.utils.preset_manager import PresetManager


class TestPresetManagerInit:
    """Test PresetManager initialization."""

    def test_init_with_custom_dir(self):
        """Initialize with custom presets directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            assert manager.get_presets_dir() == Path(tmpdir)

    def test_init_creates_presets_dir(self):
        """If presets directory doesn't exist, it should be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            presets_dir = Path(tmpdir) / "presets"
            assert presets_dir.exists() is False
            manager = PresetManager(presets_dir=str(presets_dir))
            assert presets_dir.exists() is True

    def test_init_empty_dir(self):
        """Empty presets directory should have no presets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            assert manager.list_presets() == []


class TestPresetSave:
    """Test saving presets."""

    def test_save_preset_basic(self):
        """Save a basic preset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {
                "nodes": [{"id": "blur_001", "type": "blur", "position": [100, 100]}],
                "connections": []
            }
            result = manager.save_preset("TestPreset", nodes_data, "Test description")
            assert result is True
            assert "TestPreset" in manager.list_presets()

    def test_save_preset_creates_file(self):
        """Saving preset should create a JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)
            file_path = Path(tmpdir) / "TestPreset.json"
            assert file_path.exists() is True

    def test_save_preset_with_author(self):
        """Save preset with author field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data, "Desc", "TestAuthor")
            info = manager.get_preset_info("TestPreset")
            assert info["author"] == "TestAuthor"

    def test_save_preset_overwrites_existing(self):
        """Saving preset with same name should overwrite."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data1 = {"nodes": [{"id": "n1"}], "connections": []}
            nodes_data2 = {"nodes": [{"id": "n2"}], "connections": []}
            manager.save_preset("TestPreset", nodes_data1)
            manager.save_preset("TestPreset", nodes_data2)
            preset = manager.load_preset("TestPreset")
            assert preset["nodes"] == [{"id": "n2"}]

    def test_save_preset_invalid_data_fails(self):
        """Saving preset with invalid structure should fail.
        
        Note: save_preset builds preset dict internally with nodes/connections
        from nodes_data, so validation happens on the built preset.

import pytest
pytestmark = pytest.mark.composite_core
        If nodes_data is missing 'nodes', the built preset has empty 'nodes' list,
        which passes validation. This test checks the behavior when nodes_data
        provides invalid nodes value (non-list).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            # Pass nodes that is not a list - this will fail validation
            invalid_data = {"nodes": "not a list", "connections": []}
            result = manager.save_preset("InvalidPreset", invalid_data)
            assert result is False
            assert "InvalidPreset" not in manager.list_presets()

    def test_save_preset_stores_timestamp(self):
        """Saved preset should have created_at timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)
            preset = manager.load_preset("TestPreset")
            assert "created_at" in preset
            # Timestamp should be parseable as ISO format
            datetime.fromisoformat(preset["created_at"])


class TestPresetLoad:
    """Test loading presets."""

    def test_load_existing_preset(self):
        """Load a preset that exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {
                "nodes": [{"id": "blur_001", "type": "blur"}],
                "connections": [{"src_idx": 0, "src_port": "result", "dst_idx": 1, "dst_port": "image"}]
            }
            manager.save_preset("TestPreset", nodes_data)
            loaded = manager.load_preset("TestPreset")
            assert loaded is not None
            assert loaded["nodes"] == nodes_data["nodes"]
            assert loaded["connections"] == nodes_data["connections"]

    def test_load_nonexistent_preset_returns_none(self):
        """Loading non-existent preset should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            result = manager.load_preset("NonExistent")
            assert result is None

    def test_load_after_reload(self):
        """Load preset after reloading from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)

            # Create another manager (simulate fresh load)
            manager2 = PresetManager(presets_dir=tmpdir)
            loaded = manager2.load_preset("TestPreset")
            assert loaded is not None


class TestPresetList:
    """Test listing presets."""

    def test_list_presets_empty(self):
        """Empty directory should return empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            assert manager.list_presets() == []

    def test_list_presets_single(self):
        """List single preset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("PresetA", nodes_data)
            assert manager.list_presets() == ["PresetA"]

    def test_list_presets_multiple(self):
        """List multiple presets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("PresetA", nodes_data)
            manager.save_preset("PresetB", nodes_data)
            manager.save_preset("PresetC", nodes_data)
            presets = manager.list_presets()
            assert len(presets) == 3
            assert "PresetA" in presets
            assert "PresetB" in presets
            assert "PresetC" in presets

    def test_list_presets_sorted_by_time(self):
        """Presets should be sorted by creation time (newest first)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("PresetA", nodes_data)
            manager.save_preset("PresetB", nodes_data)
            manager.save_preset("PresetC", nodes_data)
            presets = manager.list_presets()
            # Newest should be first
            assert presets[0] == "PresetC"


class TestPresetInfo:
    """Test get_preset_info."""

    def test_get_preset_info_returns_metadata(self):
        """get_preset_info should return metadata without full node data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {
                "nodes": [{"id": "n1"}, {"id": "n2"}, {"id": "n3"}],
                "connections": []
            }
            manager.save_preset("TestPreset", nodes_data, "Test desc", "Author")
            info = manager.get_preset_info("TestPreset")
            assert info["name"] == "TestPreset"
            assert info["description"] == "Test desc"
            assert info["author"] == "Author"
            assert info["node_count"] == 3
            assert "created_at" in info

    def test_get_preset_info_nonexistent_returns_none(self):
        """get_preset_info for non-existent preset returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            result = manager.get_preset_info("NonExistent")
            assert result is None


class TestPresetDelete:
    """Test deleting presets."""

    def test_delete_preset_removes_file(self):
        """Deleting preset should remove the JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)
            file_path = Path(tmpdir) / "TestPreset.json"
            assert file_path.exists() is True
            result = manager.delete_preset("TestPreset")
            assert result is True
            assert file_path.exists() is False

    def test_delete_preset_removes_from_list(self):
        """Deleting preset should remove from list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)
            manager.delete_preset("TestPreset")
            assert "TestPreset" not in manager.list_presets()

    def test_delete_nonexistent_preset(self):
        """Deleting non-existent preset should succeed (no-op)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            result = manager.delete_preset("NonExistent")
            assert result is True


class TestPresetRename:
    """Test renaming presets."""

    def test_rename_preset_basic(self):
        """Rename preset to new name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("OldName", nodes_data)
            result = manager.rename_preset("OldName", "NewName")
            assert result is True
            assert "OldName" not in manager.list_presets()
            assert "NewName" in manager.list_presets()

    def test_rename_updates_file(self):
        """Rename should rename the JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("OldName", nodes_data)
            manager.rename_preset("OldName", "NewName")
            assert (Path(tmpdir) / "OldName.json").exists() is False
            assert (Path(tmpdir) / "NewName.json").exists() is True

    def test_rename_nonexistent_fails(self):
        """Renaming non-existent preset should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            result = manager.rename_preset("NonExistent", "NewName")
            assert result is False

    def test_rename_to_existing_name_fails(self):
        """Renaming to an existing name should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("PresetA", nodes_data)
            manager.save_preset("PresetB", nodes_data)
            result = manager.rename_preset("PresetA", "PresetB")
            assert result is False
            # Original should still exist
            assert "PresetA" in manager.list_presets()

    def test_rename_preserves_data(self):
        """Rename should preserve all preset data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {
                "nodes": [{"id": "n1"}],
                "connections": [{"src_idx": 0}]
            }
            manager.save_preset("OldName", nodes_data, "Description")
            manager.rename_preset("OldName", "NewName")
            preset = manager.load_preset("NewName")
            assert preset["nodes"] == nodes_data["nodes"]
            assert preset["description"] == "Description"


class TestPresetImport:
    """Test importing presets from external files."""

    def test_import_preset_basic(self):
        """Import preset from external JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)

            # Create external preset file
            external_file = Path(tmpdir) / "external" / "ExternalPreset.json"
            external_file.parent.mkdir(exist_ok=True)
            preset_data = {
                "name": "ExternalPreset",
                "nodes": [{"id": "n1"}],
                "connections": []
            }
            with open(external_file, "w") as f:
                json.dump(preset_data, f)

            imported_name = manager.import_preset(str(external_file))
            assert imported_name == "ExternalPreset"
            assert "ExternalPreset" in manager.list_presets()

    def test_import_handles_name_conflict(self):
        """Import should handle name conflicts by adding suffix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)

            # Save existing preset
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("ExternalPreset", nodes_data)

            # Import same name
            external_file = Path(tmpdir) / "external.json"
            preset_data = {
                "name": "ExternalPreset",
                "nodes": [],
                "connections": []
            }
            with open(external_file, "w") as f:
                json.dump(preset_data, f)

            imported_name = manager.import_preset(str(external_file))
            assert imported_name == "ExternalPreset_1"
            assert "ExternalPreset_1" in manager.list_presets()

    def test_import_handles_multiple_conflicts(self):
        """Import should handle multiple name conflicts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)

            # Create conflicts
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("Preset", nodes_data)
            manager.save_preset("Preset_1", nodes_data)
            manager.save_preset("Preset_2", nodes_data)

            # Import same name
            external_file = Path(tmpdir) / "external.json"
            preset_data = {"name": "Preset", "nodes": [], "connections": []}
            with open(external_file, "w") as f:
                json.dump(preset_data, f)

            imported_name = manager.import_preset(str(external_file))
            assert imported_name == "Preset_3"

    def test_import_invalid_file_returns_none(self):
        """Import invalid JSON file should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            external_file = Path(tmpdir) / "invalid.json"
            with open(external_file, "w") as f:
                f.write("not valid json")
            result = manager.import_preset(str(external_file))
            assert result is None

    def test_import_invalid_structure_returns_none(self):
        """Import preset with invalid structure should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            external_file = Path(tmpdir) / "invalid.json"
            # Missing 'nodes' key
            preset_data = {"name": "Invalid", "connections": []}
            with open(external_file, "w") as f:
                json.dump(preset_data, f)
            result = manager.import_preset(str(external_file))
            assert result is None


class TestPresetExport:
    """Test exporting presets to external files."""

    def test_export_preset_basic(self):
        """Export preset to external file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [{"id": "n1"}], "connections": []}
            manager.save_preset("TestPreset", nodes_data)

            export_path = Path(tmpdir) / "exported.json"
            result = manager.export_preset("TestPreset", str(export_path))
            assert result is True
            assert export_path.exists() is True

            # Verify content
            with open(export_path) as f:
                exported = json.load(f)
            assert exported["nodes"] == nodes_data["nodes"]

    def test_export_nonexistent_fails(self):
        """Exporting non-existent preset should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            export_path = Path(tmpdir) / "exported.json"
            result = manager.export_preset("NonExistent", str(export_path))
            assert result is False


class TestPresetExists:
    """Test preset_exists check."""

    def test_preset_exists_true(self):
        """preset_exists returns True for existing preset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data)
            assert manager.preset_exists("TestPreset") is True

    def test_preset_exists_false(self):
        """preset_exists returns False for non-existent preset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            assert manager.preset_exists("NonExistent") is False


class TestPresetUpdate:
    """Test updating preset description."""

    def test_update_preset_description(self):
        """Update preset description."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("TestPreset", nodes_data, "Old description")
            result = manager.update_preset_description("TestPreset", "New description")
            assert result is True
            preset = manager.load_preset("TestPreset")
            assert preset["description"] == "New description"

    def test_update_description_nonexistent_fails(self):
        """Updating description for non-existent preset should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            result = manager.update_preset_description("NonExistent", "New desc")
            assert result is False


class TestPresetReload:
    """Test reload_presets functionality."""

    def test_reload_presets(self):
        """Reload presets from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            nodes_data = {"nodes": [], "connections": []}
            manager.save_preset("PresetA", nodes_data)

            # Add a preset file directly to disk (bypass manager)
            preset_file = Path(tmpdir) / "PresetB.json"
            preset_data = {"name": "PresetB", "nodes": [], "connections": []}
            with open(preset_file, "w") as f:
                json.dump(preset_data, f)

            # PresetB should not be in list yet
            assert "PresetB" not in manager.list_presets()

            # Reload
            manager.reload_presets()
            assert "PresetB" in manager.list_presets()


class TestPresetValidation:
    """Test preset validation."""

    def test_validate_preset_valid(self):
        """Valid preset should pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            valid_data = {"nodes": [], "connections": []}
            assert manager._validate_preset(valid_data) is True

    def test_validate_preset_missing_nodes(self):
        """Preset missing 'nodes' should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            invalid_data = {"connections": []}
            assert manager._validate_preset(invalid_data) is False

    def test_validate_preset_missing_connections(self):
        """Preset missing 'connections' should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            invalid_data = {"nodes": []}
            assert manager._validate_preset(invalid_data) is False

    def test_validate_preset_nodes_not_list(self):
        """Preset with nodes not being list should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            invalid_data = {"nodes": "not a list", "connections": []}
            assert manager._validate_preset(invalid_data) is False

    def test_validate_preset_connections_not_list(self):
        """Preset with connections not being list should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = PresetManager(presets_dir=tmpdir)
            invalid_data = {"nodes": [], "connections": "not a list"}
            assert manager._validate_preset(invalid_data) is False


class TestPresetCorruptedFile:
    """Test handling of corrupted preset files."""

    def test_load_with_corrupted_file(self):
        """Corrupted JSON file should be skipped without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create corrupted file
            corrupted_file = Path(tmpdir) / "Corrupted.json"
            with open(corrupted_file, "w") as f:
                f.write("not valid json {{{")

            manager = PresetManager(presets_dir=tmpdir)
            # Should not crash, should just skip the corrupted file
            assert "Corrupted" not in manager.list_presets()

    def test_load_with_invalid_structure_file(self):
        """File with invalid structure should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            invalid_file = Path(tmpdir) / "Invalid.json"
            with open(invalid_file, "w") as f:
                json.dump({"name": "Invalid", "wrong_key": []}, f)

            manager = PresetManager(presets_dir=tmpdir)
            # Should skip invalid preset
            assert "Invalid" not in manager.list_presets()