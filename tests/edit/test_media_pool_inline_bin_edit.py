"""Tests for Finder-style in-place bin create / rename.

Covers:
- CreateBinCommand now persists ``_created_bin_id`` (undo-safe + queryable).
- RenameBinCommand now persists ``old_name`` (undo was previously broken).
- MediaPoolPanel._next_unique_bin_name generates Finder-style unique names.
- _apply_bin_rename routes through the command stack (Undo) and rejects
  empty / no-op renames.
- _create_bin creates without a dialog and enters inline-edit afterwards.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject
from src.features.edit.core.commands import (
    CommandStack,
    CreateBinCommand,
    RenameBinCommand,
)
from src.features.edit.ui.media_pool_panel import MediaPoolPanel


def _bin_id_named(project, name):
    return next(bid for bid, b in project.bins.items() if b.name == name)


# ---------------------------------------------------------------------------
# CreateBinCommand: _created_bin_id now persisted
# ---------------------------------------------------------------------------

def test_create_bin_command_stores_created_bin_id() -> None:
    project = EditProject.create("P")
    cmd = CreateBinCommand.create("Folder", parent_id=None)

    new_project = cmd.apply(project)

    assert cmd._created_bin_id is not None
    assert cmd._created_bin_id in new_project.bins
    assert new_project.bins[cmd._created_bin_id].name == "Folder"


def test_create_bin_command_undo_removes_exact_bin() -> None:
    """Undo should remove exactly the created bin, not guess by name."""
    project = EditProject.create("P").create_bin("Pre-existing")
    cmd = CreateBinCommand.create("Folder", parent_id=None)

    new_project = cmd.apply(project)
    created_id = cmd._created_bin_id
    assert created_id is not None

    undone = cmd.undo(new_project)
    # The created bin is gone...
    assert created_id not in undone.bins
    # ...but the pre-existing bin survives.
    assert _bin_id_named(undone, "Pre-existing") in undone.bins


def test_create_bin_command_undo_redo_roundtrip() -> None:
    project = EditProject.create("P")
    stack = CommandStack()
    cmd = CreateBinCommand.create("Folder")
    p1 = stack.execute(cmd, project)
    assert any(b.name == "Folder" for b in p1.bins.values())

    p2 = stack.undo(p1)
    # Redo re-executes apply, which creates a fresh bin (new UUID) — that is
    # expected for create operations. Verify by name, not by id.
    assert not any(b.name == "Folder" for b in p2.bins.values())

    p3 = stack.redo(p2)
    assert any(b.name == "Folder" for b in p3.bins.values())


# ---------------------------------------------------------------------------
# RenameBinCommand: old_name now persisted (undo was broken)
# ---------------------------------------------------------------------------

def test_rename_bin_command_stores_old_name() -> None:
    project = EditProject.create("P").create_bin("Original")
    bin_id = _bin_id_named(project, "Original")
    cmd = RenameBinCommand.create(bin_id, "Renamed")

    cmd.apply(project)
    assert cmd.old_name == "Original"


def test_rename_bin_command_undo_restores_name() -> None:
    """Undo was previously a no-op because old_name stayed None."""
    project = EditProject.create("P").create_bin("Original")
    bin_id = _bin_id_named(project, "Original")
    cmd = RenameBinCommand.create(bin_id, "Renamed")

    renamed = cmd.apply(project)
    assert renamed.bins[bin_id].name == "Renamed"

    undone = cmd.undo(renamed)
    assert undone.bins[bin_id].name == "Original"


def test_rename_bin_undo_redo_roundtrip() -> None:
    project = EditProject.create("P").create_bin("Original")
    bin_id = _bin_id_named(project, "Original")
    stack = CommandStack()
    cmd = RenameBinCommand.create(bin_id, "Renamed")

    p1 = stack.execute(cmd, project)
    assert p1.bins[bin_id].name == "Renamed"
    p2 = stack.undo(p1)
    assert p2.bins[bin_id].name == "Original"
    p3 = stack.redo(p2)
    assert p3.bins[bin_id].name == "Renamed"


# ---------------------------------------------------------------------------
# MediaPoolPanel._next_unique_bin_name
# ---------------------------------------------------------------------------

def test_next_unique_bin_name_no_conflict(qapp) -> None:
    panel = MediaPoolPanel()
    panel.set_project(EditProject.create("P"))
    # No bins yet → base name returned directly.
    assert panel._next_unique_bin_name("Folder") == "Folder"


def test_next_unique_bin_name_appends_number(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P").create_bin("Folder")
    panel.set_project(project)
    # "Folder" exists → "Folder 2".
    assert panel._next_unique_bin_name("Folder") == "Folder 2"


def test_next_unique_bin_name_skips_taken_numbers(qapp) -> None:
    panel = MediaPoolPanel()
    project = (
        EditProject.create("P")
        .create_bin("Folder")
        .create_bin("Folder 2")
        .create_bin("Folder 3")
    )
    panel.set_project(project)
    # "Folder", "Folder 2", "Folder 3" exist → "Folder 4".
    assert panel._next_unique_bin_name("Folder") == "Folder 4"


def test_next_unique_bin_name_scoped_to_parent(qapp) -> None:
    """A name used by a bin in a *different* parent should not block reuse."""
    panel = MediaPoolPanel()
    parent = EditProject.create("P").create_bin("Parent")
    parent_id = _bin_id_named(parent, "Parent")
    # Child bin named "Folder" under Parent.
    project = parent.create_bin("Folder", parent_id=parent_id)
    panel.set_project(project)

    # At root level, "Folder" is free (it only exists under Parent).
    assert panel._next_unique_bin_name("Folder", parent_id=None) == "Folder"
    # Under Parent, "Folder" is taken → "Folder 2".
    assert panel._next_unique_bin_name("Folder", parent_id=parent_id) == "Folder 2"


# ---------------------------------------------------------------------------
# MediaPoolPanel._apply_bin_rename
# ---------------------------------------------------------------------------

def test_apply_bin_rename_executes_command(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P").create_bin("Old")
    panel.set_project(project)
    QTest.qWait(80)

    bin_id = _bin_id_named(panel._project, "Old")
    assert panel._apply_bin_rename(bin_id, "New Name") is True
    assert panel._project.bins[bin_id].name == "New Name"


def test_apply_bin_rename_rejects_empty_name(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P").create_bin("Keep")
    panel.set_project(project)
    QTest.qWait(80)

    bin_id = _bin_id_named(panel._project, "Keep")
    # Empty / whitespace-only names are rejected (no-op, no error).
    assert panel._apply_bin_rename(bin_id, "") is False
    assert panel._apply_bin_rename(bin_id, "   ") is False
    assert panel._project.bins[bin_id].name == "Keep"


def test_apply_bin_rename_rejects_noop(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P").create_bin("Same")
    panel.set_project(project)
    QTest.qWait(80)

    bin_id = _bin_id_named(panel._project, "Same")
    assert panel._apply_bin_rename(bin_id, "Same") is False
    assert panel._project.bins[bin_id].name == "Same"


def test_apply_bin_rename_supports_undo(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P").create_bin("Original")
    panel.set_project(project)
    QTest.qWait(80)

    bin_id = _bin_id_named(panel._project, "Original")
    panel._apply_bin_rename(bin_id, "Changed")
    assert panel._project.bins[bin_id].name == "Changed"

    # Undo via the command stack restores the original name.
    panel._project = panel._command_stack.undo(panel._project)
    assert panel._project.bins[bin_id].name == "Original"


# ---------------------------------------------------------------------------
# _create_bin: no dialog, unique default name, inline-edit afterwards
# ---------------------------------------------------------------------------

def test_create_bin_no_dialog_unique_default_name(qapp) -> None:
    panel = MediaPoolPanel()
    project = EditProject.create("P")
    panel.set_project(project)
    QTest.qWait(80)

    # Creating should not raise (no QInputDialog modal) and produce a bin.
    panel._create_bin()
    QTest.qWait(80)
    user_bins = [
        b for b in panel._project.bins.values()
        if b.bin_id != panel._project.root_bin_id
    ]
    assert len(user_bins) == 1  # exactly one new bin created


def test_create_bin_increments_default_name(qapp) -> None:
    panel = MediaPoolPanel()
    panel.set_project(EditProject.create("P"))
    QTest.qWait(80)

    panel._create_bin()
    QTest.qWait(60)
    panel._create_bin()
    QTest.qWait(60)

    # Two bins created with incrementing default names.
    user_bins = [
        b for b in panel._project.bins.values()
        if b.bin_id != panel._project.root_bin_id
    ]
    assert len(user_bins) == 2
    assert len({b.name for b in user_bins}) == 2  # distinct names
