from __future__ import annotations

from src.features.edit.core.edit_project_io import (
    convert_edit_project_to_topos_project,
    convert_topos_project_to_edit_project,
)
from src.features.edit.core.project import EditProject
from src.shared.project.topos_project import ProjectBinRef, ToposProject


def _bin_data(bin_id: str, name: str, parent_id: str | None = None) -> dict:
    return {
        "bin_id": bin_id,
        "name": name,
        "parent_id": parent_id,
        "asset_ids": [],
        "timeline_ids": [],
    }


def test_root_bin_identity_is_persisted_in_topos_settings() -> None:
    project = EditProject.create("Root persistence")
    topos_project = convert_edit_project_to_topos_project(project)

    assert topos_project.settings["root_bin_id"] == project.root_bin_id

    restored = convert_topos_project_to_edit_project(
        topos_project,
        full_data={
            "bins": {
                project.root_bin_id: _bin_data(project.root_bin_id, "Root"),
            }
        },
    )
    assert restored.root_bin_id == project.root_bin_id
    assert list(restored.bins) == [project.root_bin_id]


def test_loading_duplicate_root_bins_keeps_one_canonical_root() -> None:
    root_id = "root-canonical"
    duplicate_id = "root-duplicate"
    child_id = "child-bin"
    topos_project = ToposProject(
        project_id="project-1",
        name="Duplicate roots",
        project_mode="edit_project",
        bins={
            root_id: ProjectBinRef(root_id, "Root", None, 0),
            duplicate_id: ProjectBinRef(duplicate_id, "Root", None, 0),
            child_id: ProjectBinRef(child_id, "Footage", duplicate_id, 0),
        },
    )

    restored = convert_topos_project_to_edit_project(
        topos_project,
        full_data={
            "bins": {
                root_id: _bin_data(root_id, "Root"),
                duplicate_id: _bin_data(duplicate_id, "Root"),
                child_id: _bin_data(child_id, "Footage", duplicate_id),
            }
        },
    )

    assert restored.root_bin_id == root_id
    assert duplicate_id not in restored.bins
    assert restored.bins[child_id].parent_id == root_id
