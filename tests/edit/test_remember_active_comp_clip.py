"""Remember the open composition on close and restore it after restart.

Covers the full "remember active composition" chain:

1. ``EditProject.set_active_comp_clip`` — the immutable setter that records
   which Comp Clip was open.
2. ``CompositeControllerMixin._remember_active_comp_clip`` — resolves the
   live graph's Comp Clip and writes it back onto the EditProject (called on
   explicit opens and on the leave/close flush).
3. ``PageManagementMixin._resolve_remembered_comp_clip_composite_id`` — the
   restart fallback that maps the persisted ``active_comp_clip_id`` back to
   the composition/graph id so the Composite page can restore it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject, MediaAsset

COMP_ID = "comp-1"
COMP_CLIP_ID = "cc-1"


def _project_with_comp_clip(active: str | None = None) -> EditProject:
    base = EditProject.create("Remember")
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
        comp_clips={COMP_CLIP_ID: {
            "comp_clip_id": COMP_CLIP_ID, "graph_id": COMP_ID}},
        root_bin_id=base.root_bin_id,
        active_comp_clip_id=active,
    )


# ---------------------------------------------------------------------------
# EditProject.set_active_comp_clip
# ---------------------------------------------------------------------------

def test_set_active_comp_clip_records_and_clears() -> None:
    p = _project_with_comp_clip()
    assert p.active_comp_clip_id is None

    p2 = p.set_active_comp_clip(COMP_CLIP_ID)
    assert p2.active_comp_clip_id == COMP_CLIP_ID
    # Every other piece of composite state must survive the rebuild.
    assert COMP_ID in p2.compositions
    assert COMP_CLIP_ID in p2.comp_clips
    assert "a1" in p2.assets

    p3 = p2.set_active_comp_clip(None)
    assert p3.active_comp_clip_id is None


def test_set_active_comp_clip_noop_when_unchanged() -> None:
    p = _project_with_comp_clip(active=COMP_CLIP_ID)
    assert p.set_active_comp_clip(COMP_CLIP_ID) is p


def test_set_active_comp_clip_rejects_unknown_id() -> None:
    p = _project_with_comp_clip()
    with pytest.raises(ValueError):
        p.set_active_comp_clip("missing-cc")


def test_remove_comp_clip_clears_active_reference() -> None:
    p = _project_with_comp_clip(active=COMP_CLIP_ID)
    p2 = p.remove_comp_clip(COMP_CLIP_ID)
    assert p2.active_comp_clip_id is None
    assert COMP_CLIP_ID not in p2.comp_clips


# ---------------------------------------------------------------------------
# CompositeControllerMixin._remember_active_comp_clip
# ---------------------------------------------------------------------------

def test_remember_active_comp_clip_writes_back_to_project() -> None:
    from src.app.controllers.composite_controller import CompositeControllerMixin

    stub = SimpleNamespace(
        _current_edit_project=_project_with_comp_clip(),
        edit_project_dirty=False,
    )
    CompositeControllerMixin._remember_active_comp_clip(stub, COMP_ID)
    assert stub._current_edit_project.active_comp_clip_id == COMP_CLIP_ID
    assert stub.edit_project_dirty is True


def test_remember_active_comp_clip_ignores_unbacked_graphs() -> None:
    from src.app.controllers.composite_controller import CompositeControllerMixin

    project = _project_with_comp_clip()
    stub = SimpleNamespace(_current_edit_project=project, edit_project_dirty=False)
    # A graph with no Comp Clip behind it (e.g. a plain timeline composition)
    # must not be recorded and must not dirty the project.
    CompositeControllerMixin._remember_active_comp_clip(stub, "no-such-graph")
    assert stub._current_edit_project is project
    assert stub.edit_project_dirty is False


def test_remember_active_comp_clip_noop_when_already_active() -> None:
    from src.app.controllers.composite_controller import CompositeControllerMixin

    project = _project_with_comp_clip(active=COMP_CLIP_ID)
    stub = SimpleNamespace(_current_edit_project=project, edit_project_dirty=False)
    CompositeControllerMixin._remember_active_comp_clip(stub, COMP_ID)
    assert stub._current_edit_project is project
    assert stub.edit_project_dirty is False


# ---------------------------------------------------------------------------
# PageManagementMixin._resolve_remembered_comp_clip_composite_id
# ---------------------------------------------------------------------------

def test_resolve_remembered_comp_clip_maps_to_graph_id() -> None:
    from src.app.controllers.page_controller import PageManagementMixin

    project = _project_with_comp_clip(active=COMP_CLIP_ID)
    resolved = PageManagementMixin._resolve_remembered_comp_clip_composite_id(
        SimpleNamespace(), project)
    assert resolved == COMP_ID


def test_resolve_remembered_comp_clip_handles_missing_state() -> None:
    from src.app.controllers.page_controller import PageManagementMixin

    # Nothing remembered at all.
    project = _project_with_comp_clip()
    assert PageManagementMixin._resolve_remembered_comp_clip_composite_id(
        SimpleNamespace(), project) == ""

    # Remembered id points at a deleted comp clip.
    dangling = _project_with_comp_clip(active="deleted-cc")
    assert PageManagementMixin._resolve_remembered_comp_clip_composite_id(
        SimpleNamespace(), dangling) == ""


def test_remember_roundtrip_survives_rebuilds() -> None:
    """Remember → unrelated media-pool ops → still resolvable after restart."""
    p = _project_with_comp_clip()
    p2 = p.set_active_comp_clip(COMP_CLIP_ID)
    p3 = p2.create_bin("Folder")

    from src.app.controllers.page_controller import PageManagementMixin
    assert PageManagementMixin._resolve_remembered_comp_clip_composite_id(
        SimpleNamespace(), p3) == COMP_ID
