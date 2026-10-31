"""Runtime-facing tests for the Model3D Scene Outliner."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt

from src.features.composite.ui.scene_outliner_panel import SceneOutlinerPanel


def _summary() -> dict:
    return {
        "node_id": "root-id",
        "name": "Scene",
        "node_type": "root",
        "children": [
            {
                "node_id": "body-id",
                "name": "Body",
                "node_type": "mesh",
                "triangle_count": 12,
                "material_name": "BodyMat",
                "children": [],
            },
            {
                "node_id": "parts-id",
                "name": "Parts",
                "node_type": "group",
                "children": [
                    {
                        "node_id": "head-id",
                        "name": "Head",
                        "node_type": "skinned",
                        "triangle_count": 8,
                        "children": [],
                    },
                ],
            },
        ],
    }


def test_outliner_builds_tree_and_tracks_hidden_ids(qapp) -> None:
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_summary(), hidden_ids=("head-id",))

    assert panel._tree.topLevelItemCount() == 1
    assert panel._items["head-id"].checkState(0) == Qt.CheckState.Unchecked
    assert panel.hidden_mesh_ids() == ("head-id",)

    panel._items["body-id"].setCheckState(0, Qt.CheckState.Unchecked)
    assert panel.hidden_mesh_ids() == ("body-id", "head-id")
    panel._items["body-id"].setCheckState(0, Qt.CheckState.Checked)
    assert panel.hidden_mesh_ids() == ("head-id",)


def test_outliner_group_visibility_updates_mesh_children(qapp) -> None:
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_summary())
    changes: list[tuple[str, bool]] = []
    panel.visibility_changed.connect(lambda node_id, visible: changes.append(
        (node_id, visible)))

    panel._items["parts-id"].setCheckState(0, Qt.CheckState.Unchecked)

    assert panel.hidden_mesh_ids() == ("head-id",)
    assert changes == [("head-id", False)]


def test_outliner_selection_emits_stable_scene_id(qapp) -> None:
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_summary())
    selected: list[str] = []
    panel.scene_node_selected.connect(selected.append)

    panel._items["head-id"].setSelected(True)
    qapp.processEvents()

    assert selected == ["head-id"]


def test_outliner_tracks_lock_and_single_solo_target(qapp) -> None:
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_summary(), locked_ids=("body-id",), solo_id="head-id")
    locks: list[tuple[str, bool]] = []
    solos: list[str] = []
    panel.lock_changed.connect(
        lambda node_id, locked: locks.append((node_id, locked)))
    panel.solo_changed.connect(solos.append)

    assert panel._items["body-id"].checkState(4) == Qt.CheckState.Checked
    assert panel._items["head-id"].checkState(5) == Qt.CheckState.Checked

    panel._items["body-id"].setCheckState(4, Qt.CheckState.Unchecked)
    panel._items["body-id"].setCheckState(5, Qt.CheckState.Checked)

    assert locks == [("body-id", False)]
    assert solos == ["body-id"]
    assert panel._items["head-id"].checkState(5) == Qt.CheckState.Unchecked


def test_outliner_locked_parent_blocks_visibility_edit(qapp) -> None:
    panel = SceneOutlinerPanel()
    panel.set_hierarchy(_summary(), locked_ids=("parts-id",))
    changes: list[tuple[str, bool]] = []
    panel.visibility_changed.connect(
        lambda node_id, visible: changes.append((node_id, visible)))

    panel._items["head-id"].setCheckState(0, Qt.CheckState.Unchecked)

    assert panel._items["head-id"].checkState(0) == Qt.CheckState.Checked
    assert panel.hidden_mesh_ids() == ()
    assert changes == []
