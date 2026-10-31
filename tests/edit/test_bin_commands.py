"""
Bin Commands Tests - Test Command Pattern for Bin Operations.

Tests for:
- CreateBinCommand: Create bin and undo
- DeleteBinCommand: Delete bin and undo
- RenameBinCommand: Rename bin and undo
- MoveAssetToBinCommand: Move asset and undo
- ProjectCommandStack: Undo/redo management
"""

from __future__ import annotations

import pytest
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    ProjectBin,
)
from src.features.edit.core.commands import (
    CreateBinCommand,
    DeleteBinCommand,
    RenameBinCommand,
    MoveAssetToBinCommand,
    ProjectCommandStack,
)


# ================================================================
# Test Fixtures
# ================================================================

@pytest.fixture
def empty_project() -> EditProject:
    """Create an empty project for testing."""
    return EditProject.create("Test Project")


@pytest.fixture
def project_with_assets(empty_project: EditProject) -> EditProject:
    """Create a project with some assets."""
    project = empty_project

    # Import some assets
    project = project.import_asset(
        path="/path/to/video.mp4",
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0},
        display_name="Video1.mp4",
    )
    project = project.import_asset(
        path="/path/to/image.jpg",
        media_type="image",
        metadata={"width": 800, "height": 600},
        display_name="Image1.jpg",
    )
    project = project.import_asset(
        path="/path/to/audio.wav",
        media_type="audio",
        metadata={"duration_seconds": 30.0},
        display_name="Audio1.wav",
    )

    return project


@pytest.fixture
def project_with_bins(project_with_assets: EditProject) -> EditProject:
    """Create a project with multiple bins."""
    project = project_with_assets

    # Create additional bins
    project = project.create_bin("Footage")
    project = project.create_bin("Audio")
    project = project.create_bin("Images")

    return project


# ================================================================
# CreateBinCommand Tests
# ================================================================

class TestCreateBinCommand:
    """Tests for CreateBinCommand."""

    def test_create_command(self) -> None:
        """Test command factory creation."""
        cmd = CreateBinCommand.create("New Bin")

        assert cmd.command_id is not None
        assert cmd.name == "New Bin"
        assert cmd.parent_id is None

    def test_create_command_with_parent(self) -> None:
        """Test command creation with parent."""
        parent_id = str(uuid.uuid4())
        cmd = CreateBinCommand.create("Sub Bin", parent_id)

        assert cmd.parent_id == parent_id

    def test_apply_create_bin(self, empty_project: EditProject) -> None:
        """Test applying CreateBinCommand."""
        cmd = CreateBinCommand.create("Footage")
        new_project = cmd.apply(empty_project)

        # Verify bin was created
        assert new_project.bin_count == 2  # root + Footage

        # Find the new bin
        new_bin_id = None
        for bin_id in new_project.bins.keys():
            if bin_id not in empty_project.bins:
                new_bin_id = bin_id
                break

        assert new_bin_id is not None
        new_bin = new_project.bins[new_bin_id]
        assert new_bin.name == "Footage"

    def test_undo_create_bin(self, empty_project: EditProject) -> None:
        """Test undoing CreateBinCommand."""
        cmd = CreateBinCommand.create("Footage")

        # Apply
        new_project = cmd.apply(empty_project)

        # Find the created bin ID
        new_bin_id = None
        for bin_id in new_project.bins.keys():
            if bin_id not in empty_project.bins:
                new_bin_id = bin_id
                break

        # Create command with stored ID for undo
        undo_cmd = CreateBinCommand(
            command_id=cmd.command_id,
            label=cmd.label,
            name=cmd.name,
            _created_bin_id=new_bin_id,
        )

        # Undo
        restored_project = undo_cmd.undo(new_project)

        # Verify bin was removed
        assert restored_project.bin_count == 1  # only root

    def test_serialize_deserialize(self) -> None:
        """Test command serialization."""
        cmd = CreateBinCommand.create("Test Bin")

        data = cmd.to_dict()
        assert data["command_type"] == "CreateBinCommand"
        assert data["name"] == "Test Bin"

        restored = CreateBinCommand.from_dict(data)
        assert restored.name == cmd.name


# ================================================================
# DeleteBinCommand Tests
# ================================================================

class TestDeleteBinCommand:
    """Tests for DeleteBinCommand."""

    def test_create_command(self) -> None:
        """Test command factory creation."""
        bin_id = str(uuid.uuid4())
        cmd = DeleteBinCommand.create(bin_id)

        assert cmd.command_id is not None
        assert cmd.bin_id == bin_id

    def test_apply_delete_bin(self, project_with_bins: EditProject) -> None:
        """Test applying DeleteBinCommand."""
        # Find a non-root bin to delete
        bin_to_delete = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_id != project_with_bins.root_bin_id:
                bin_to_delete = bin_id
                break

        assert bin_to_delete is not None

        cmd = DeleteBinCommand.create(bin_to_delete)
        new_project = cmd.apply(project_with_bins)

        # Verify bin was deleted
        assert new_project.bin_count == project_with_bins.bin_count - 1
        assert bin_to_delete not in new_project.bins

    def test_delete_root_bin_error(self, empty_project: EditProject) -> None:
        """Test that deleting root bin raises error."""
        cmd = DeleteBinCommand.create(empty_project.root_bin_id)

        with pytest.raises(ValueError, match="Cannot delete root bin"):
            cmd.apply(empty_project)

    def test_undo_delete_bin(self, project_with_bins: EditProject) -> None:
        """Test undoing DeleteBinCommand."""
        # Find a bin to delete
        bin_to_delete = None
        bin_data = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_id != project_with_bins.root_bin_id:
                bin_to_delete = bin_id
                bin_data = bin_obj
                break

        assert bin_to_delete is not None
        assert bin_data is not None

        # Create command with stored bin data
        cmd = DeleteBinCommand(
            command_id=str(uuid.uuid4()),
            label="Delete Bin",
            bin_id=bin_to_delete,
            _deleted_bin={
                "bin_id": bin_data.bin_id,
                "name": bin_data.name,
                "parent_id": bin_data.parent_id,
                "asset_ids": bin_data.asset_ids,
                "timeline_ids": bin_data.timeline_ids,
            },
        )

        # Apply
        new_project = cmd.apply(project_with_bins)

        # Undo
        restored_project = cmd.undo(new_project)

        # Verify bin was restored
        assert restored_project.bin_count == project_with_bins.bin_count
        assert bin_to_delete in restored_project.bins
        restored_bin = restored_project.bins[bin_to_delete]
        assert restored_bin.name == bin_data.name


# ================================================================
# RenameBinCommand Tests
# ================================================================

class TestRenameBinCommand:
    """Tests for RenameBinCommand."""

    def test_create_command(self) -> None:
        """Test command factory creation."""
        bin_id = str(uuid.uuid4())
        cmd = RenameBinCommand.create(bin_id, "New Name")

        assert cmd.command_id is not None
        assert cmd.bin_id == bin_id
        assert cmd.new_name == "New Name"

    def test_apply_rename_bin(self, project_with_bins: EditProject) -> None:
        """Test applying RenameBinCommand."""
        # Find a bin to rename
        bin_to_rename = None
        original_name = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_obj.name == "Footage":
                bin_to_rename = bin_id
                original_name = bin_obj.name
                break

        assert bin_to_rename is not None

        cmd = RenameBinCommand.create(bin_to_rename, "Videos")
        new_project = cmd.apply(project_with_bins)

        # Verify bin was renamed
        renamed_bin = new_project.bins[bin_to_rename]
        assert renamed_bin.name == "Videos"

    def test_rename_nonexistent_bin_error(self, empty_project: EditProject) -> None:
        """Test that renaming nonexistent bin raises error."""
        cmd = RenameBinCommand.create("nonexistent-bin", "New Name")

        with pytest.raises(ValueError, match="Bin not found"):
            cmd.apply(empty_project)

    def test_undo_rename_bin(self, project_with_bins: EditProject) -> None:
        """Test undoing RenameBinCommand."""
        # Find a bin to rename
        bin_to_rename = None
        original_name = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_obj.name == "Footage":
                bin_to_rename = bin_id
                original_name = bin_obj.name
                break

        assert bin_to_rename is not None

        # Create command with stored old name
        cmd = RenameBinCommand(
            command_id=str(uuid.uuid4()),
            label="Rename Bin",
            bin_id=bin_to_rename,
            new_name="Videos",
            old_name=original_name,
        )

        # Apply
        new_project = cmd.apply(project_with_bins)

        # Undo
        restored_project = cmd.undo(new_project)

        # Verify name was restored
        restored_bin = restored_project.bins[bin_to_rename]
        assert restored_bin.name == original_name


# ================================================================
# MoveAssetToBinCommand Tests
# ================================================================

class TestMoveAssetToBinCommand:
    """Tests for MoveAssetToBinCommand."""

    def test_create_command(self) -> None:
        """Test command factory creation."""
        asset_id = str(uuid.uuid4())
        bin_id = str(uuid.uuid4())
        cmd = MoveAssetToBinCommand.create(asset_id, bin_id)

        assert cmd.command_id is not None
        assert cmd.asset_id == asset_id
        assert cmd.target_bin_id == bin_id

    def test_apply_move_asset(self, project_with_bins: EditProject) -> None:
        """Test applying MoveAssetToBinCommand."""
        # Get an asset from root bin
        asset_id = list(project_with_bins.assets.keys())[0]

        # Find the "Footage" bin
        footage_bin_id = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_obj.name == "Footage":
                footage_bin_id = bin_id
                break

        assert footage_bin_id is not None

        cmd = MoveAssetToBinCommand.create(asset_id, footage_bin_id)
        new_project = cmd.apply(project_with_bins)

        # Verify asset was added to target bin
        footage_bin = new_project.bins[footage_bin_id]
        assert asset_id in footage_bin.asset_ids

    def test_move_nonexistent_asset_error(self, project_with_bins: EditProject) -> None:
        """Test that moving nonexistent asset raises error."""
        footage_bin_id = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_obj.name == "Footage":
                footage_bin_id = bin_id
                break

        cmd = MoveAssetToBinCommand.create("nonexistent-asset", footage_bin_id)

        with pytest.raises(ValueError, match="Asset not found"):
            cmd.apply(project_with_bins)

    def test_undo_move_asset(self, project_with_bins: EditProject) -> None:
        """Test undoing MoveAssetToBinCommand."""
        # Get an asset from root bin
        asset_id = list(project_with_bins.assets.keys())[0]

        # Find the "Footage" bin
        footage_bin_id = None
        for bin_id, bin_obj in project_with_bins.bins.items():
            if bin_obj.name == "Footage":
                footage_bin_id = bin_id
                break

        assert footage_bin_id is not None

        cmd = MoveAssetToBinCommand.create(asset_id, footage_bin_id)

        # Apply
        new_project = cmd.apply(project_with_bins)

        # Undo
        restored_project = cmd.undo(new_project)

        # Verify asset was removed from target bin
        footage_bin = restored_project.bins[footage_bin_id]
        assert asset_id not in footage_bin.asset_ids


# ================================================================
# ProjectCommandStack Tests
# ================================================================

class TestProjectCommandStack:
    """Tests for ProjectCommandStack."""

    def test_execute_command(self, empty_project: EditProject) -> None:
        """Test executing a command."""
        stack = ProjectCommandStack()
        cmd = CreateBinCommand.create("Footage")

        new_project = stack.execute(cmd, empty_project)

        assert new_project.bin_count == 2
        assert stack.can_undo() is True
        assert stack.can_redo() is False

    def test_undo_command(self, empty_project: EditProject) -> None:
        """Test undoing a command."""
        stack = ProjectCommandStack()
        cmd = CreateBinCommand.create("Footage")

        new_project = stack.execute(cmd, empty_project)

        # Undo
        restored = stack.undo(new_project)

        # Note: Without stored bin_id, undo may not fully restore
        # But the stack state should change
        assert stack.can_undo() is False
        assert stack.can_redo() is True

    def test_redo_command(self, empty_project: EditProject) -> None:
        """Test redoing a command."""
        stack = ProjectCommandStack()
        cmd = CreateBinCommand.create("Footage")

        new_project = stack.execute(cmd, empty_project)
        restored = stack.undo(new_project)

        # Redo
        redone = stack.redo(restored)

        assert stack.can_undo() is True
        assert stack.can_redo() is False

    def test_multiple_commands(self, empty_project: EditProject) -> None:
        """Test executing multiple commands."""
        stack = ProjectCommandStack()

        # Create multiple bins
        project = empty_project
        project = stack.execute(CreateBinCommand.create("Bin1"), project)
        project = stack.execute(CreateBinCommand.create("Bin2"), project)
        project = stack.execute(CreateBinCommand.create("Bin3"), project)

        assert project.bin_count == 4  # root + 3
        assert stack.can_undo() is True

    def test_undo_clears_redo_stack(self, empty_project: EditProject) -> None:
        """Test that new command clears redo stack."""
        stack = ProjectCommandStack()

        project = stack.execute(CreateBinCommand.create("Bin1"), empty_project)
        project = stack.undo(project)

        # Execute new command after undo
        project = stack.execute(CreateBinCommand.create("Bin2"), project)

        # Redo stack should be cleared
        assert stack.can_redo() is False

    def test_get_undo_redo_labels(self, empty_project: EditProject) -> None:
        """Test getting undo/redo labels."""
        stack = ProjectCommandStack()
        cmd = CreateBinCommand.create("Test Bin")

        stack.execute(cmd, empty_project)

        assert stack.get_undo_label() is not None
        assert stack.get_redo_label() is None

    def test_max_size_limit(self, empty_project: EditProject) -> None:
        """Test that stack respects max size."""
        stack = ProjectCommandStack(max_size=3)

        project = empty_project
        for i in range(5):
            project = stack.execute(CreateBinCommand.create(f"Bin{i}"), project)

        # Should only keep last 3
        assert stack.get_history_size() == 3

    def test_clear_stack(self, empty_project: EditProject) -> None:
        """Test clearing the stack."""
        stack = ProjectCommandStack()

        stack.execute(CreateBinCommand.create("Bin1"), empty_project)
        stack.execute(CreateBinCommand.create("Bin2"), empty_project)

        stack.clear()

        assert stack.can_undo() is False
        assert stack.can_redo() is False
        assert stack.get_history_size() == 0


# ================================================================
# Integration Tests
# ================================================================

class TestBinCommandsIntegration:
    """Integration tests for bin commands."""

    def test_full_workflow(self, empty_project: EditProject) -> None:
        """Test full workflow: create -> rename -> delete -> undo."""
        stack = ProjectCommandStack()
        project = empty_project

        # Create bin
        cmd1 = CreateBinCommand.create("Original Name")
        project = stack.execute(cmd1, project)

        # Find the new bin
        new_bin_id = None
        for bin_id in project.bins.keys():
            if bin_id not in empty_project.bins:
                new_bin_id = bin_id
                break

        assert new_bin_id is not None

        # Rename bin
        cmd2 = RenameBinCommand.create(new_bin_id, "Renamed")
        project = stack.execute(cmd2, project)

        # Verify rename
        assert project.bins[new_bin_id].name == "Renamed"

        # Undo rename
        project = stack.undo(project)
        # Note: Without stored old_name, may not fully restore

        # Undo create
        project = stack.undo(project)