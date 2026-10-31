"""EditProject rebuilds must never drop composition / comp-clip state.

Regression guard for the "compositions disappear from the Media Pool" bug:
EditProject is an immutable dataclass, and several ``with_*``-style methods
rebuilt it without carrying ``color_stacks`` / ``comp_clips`` /
``active_comp_clip_id``. Any media-pool operation after creating a comp clip
then silently emptied those dicts, so the next project save lost the
compositions entirely. Every rebuild must preserve them.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    ColorStackData,
    EditProject,
    MediaAsset,
)

COMP_ID = "comp-1"
COMP_CLIP_ID = "cc-1"
STACK_ID = "stack-1"


def _project_with_composite_work() -> EditProject:
    """A project that already contains a composition, comp clip and stack."""
    base = EditProject.create("Persistence")
    asset = MediaAsset(
        asset_id="a1",
        path="/tmp/clip.mp4",
        media_type="video",
        display_name="clip.mp4",
        metadata={"width": 1920, "height": 1080},
    )
    root_bin = base.bins[base.root_bin_id].with_asset_added("a1")
    return EditProject(
        project_id=base.project_id,
        name=base.name,
        assets={"a1": asset},
        bins={base.root_bin_id: root_bin},
        timelines={},
        compositions={COMP_ID: {"composite_id": COMP_ID, "nodes": []}},
        color_stacks={STACK_ID: ColorStackData(
            stack_id=STACK_ID, asset_id="a1", name="Grade")},
        comp_clips={COMP_CLIP_ID: {
            "comp_clip_id": COMP_CLIP_ID, "graph_id": COMP_ID}},
        root_bin_id=base.root_bin_id,
        active_comp_clip_id=COMP_CLIP_ID,
    )


def _assert_preserved(project: EditProject) -> None:
    assert COMP_ID in project.compositions, "compositions were dropped"
    assert COMP_CLIP_ID in project.comp_clips, "comp clips were dropped"
    assert STACK_ID in project.color_stacks, "color stacks were dropped"
    assert project.active_comp_clip_id == COMP_CLIP_ID, (
        "active_comp_clip_id was dropped")


def _new_bin_id(project: EditProject, exclude: EditProject) -> str:
    base_ids = set(exclude.bins.keys())
    return next(b for b in project.bins if b not in base_ids)


def test_bin_operations_preserve_composite_state() -> None:
    p = _project_with_composite_work()

    p2 = p.create_bin("Folder")
    _assert_preserved(p2)

    bin_id = _new_bin_id(p2, exclude=p)
    p3 = p2.move_asset_to_bin("a1", bin_id)
    _assert_preserved(p3)

    p4 = p3.rename_bin(bin_id, "Renamed")
    _assert_preserved(p4)

    p5 = p4.rename_asset("a1", "renamed.mp4")
    _assert_preserved(p5)

    p6 = p5.remove_asset_from_bin("a1", bin_id)
    _assert_preserved(p6)

    p7 = p6.remove_bin(bin_id)
    _assert_preserved(p7)


def test_timeline_operations_preserve_composite_state() -> None:
    p = _project_with_composite_work()

    p2 = p.create_timeline("Main")
    _assert_preserved(p2)

    timeline_id = p2.active_timeline_id
    assert timeline_id is not None

    p3 = p2.set_active_timeline(None)
    _assert_preserved(p3)

    p4 = p3.remove_timeline(timeline_id)
    _assert_preserved(p4)


def test_create_composition_keeps_existing_comp_clips() -> None:
    # This was the exact user-visible bug: creating a second composition
    # rebuilt the project without comp_clips, emptying the Media Pool.
    p = _project_with_composite_work()
    p2 = p.create_composition(
        composite_id="comp-2",
        source_clip_id="clip-2",
        source_asset_id="a1",
        source_asset_path="/tmp/clip.mp4",
    )
    _assert_preserved(p2)
    assert "comp-2" in p2.compositions


def test_update_and_remove_composition_preserve_state() -> None:
    p = _project_with_composite_work()

    p2 = p.update_composition(COMP_ID, nodes=[{"id": "n1", "type": "output"}])
    _assert_preserved(p2)

    p3 = p2.create_composition(
        composite_id="comp-3",
        source_clip_id="clip-3",
        source_asset_id="a1",
        source_asset_path="/tmp/clip.mp4",
    )
    p4 = p3.remove_composition("comp-3")
    _assert_preserved(p4)


def test_color_stack_operations_preserve_comp_clips() -> None:
    p = _project_with_composite_work()

    # create_color_stack returns (project, stack_id).
    p2, new_stack_id = p.create_color_stack(asset_id="a1", name="Second Grade")
    _assert_preserved(p2)
    p3 = p2.update_color_stack(new_stack_id, grading_params={"gain": 1.1})
    _assert_preserved(p3)

    p4 = p3.delete_color_stack(new_stack_id)
    _assert_preserved(p4)
