"""Tests for un-filed asset/timeline display at root level.

When an asset/timeline is dropped into a bin, it should disappear from the
root-level view (Finder-like behaviour), while remaining reachable inside
the bin. The multi-bin reference semantics are preserved — the same item
can still live in several bins.
"""
from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import (
    MEDIA_POOL_PREVIEW_ASSET_ID_KEY,
    MEDIA_POOL_PREVIEW_FRAME_KEY,
)
from src.features.edit.ui.media_pool_panel import MediaPoolPanel, ViewMode


def _project_with_two_assets_and_bin(tmp_path):
    p1 = tmp_path / "a.mp4"
    p2 = tmp_path / "b.mp4"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x")
    project = (
        EditProject.create("Un-filed Test")
        .import_asset(path=str(p1), media_type="video", metadata={})
        .import_asset(path=str(p2), media_type="video", metadata={})
        .create_bin("Footage")
    )
    return project


def _bin_id_named(project, name):
    return next(bid for bid, b in project.bins.items() if b.name == name)


def _asset_ids(project):
    return list(project.assets.keys())


# ---------------------------------------------------------------------------
# EditProject.get_unfiled_asset_ids / get_unfiled_timeline_ids
# ---------------------------------------------------------------------------

def test_unfiled_asset_ids_all_when_no_user_bin(tmp_path) -> None:
    project = _project_with_two_assets_and_bin(tmp_path)
    # Before moving anything into "Footage", both assets are un-filed.
    project_with_empty_bin = project  # bin exists but is empty
    assert sorted(project_with_empty_bin.get_unfiled_asset_ids()) == sorted(
        _asset_ids(project_with_empty_bin)
    )


def test_unfiled_asset_ids_excludes_filed_assets(tmp_path) -> None:
    project = _project_with_two_assets_and_bin(tmp_path)
    bin_id = _bin_id_named(project, "Footage")
    ids = _asset_ids(project)
    a1, a2 = ids[0], ids[1]

    moved = project.move_asset_to_bin(a1, bin_id)
    # a1 is now filed; only a2 remains un-filed.
    assert moved.get_unfiled_asset_ids() == [a2]


def test_unfiled_asset_ids_multi_bin_reference(tmp_path) -> None:
    """An asset filed in one bin disappears from root, but can be in two bins."""
    project = _project_with_two_assets_and_bin(tmp_path)
    bin1 = _bin_id_named(project, "Footage")
    project = project.create_bin("Favorites", parent_id=None)
    bin2 = _bin_id_named(project, "Favorites")
    a1 = _asset_ids(project)[0]

    # File a1 into both bins.
    project = project.move_asset_to_bin(a1, bin1)
    project = project.move_asset_to_bin(a1, bin2)

    # Root view should NOT show a1 (it is filed).
    assert a1 not in project.get_unfiled_asset_ids()
    # But both bins still reference a1.
    assert a1 in project.bins[bin1].asset_ids
    assert a1 in project.bins[bin2].asset_ids


def test_unfiled_timeline_ids_excludes_filed_timelines(tmp_path) -> None:
    project = _project_with_two_assets_and_bin(tmp_path).create_timeline("Seq 1")
    bin_id = _bin_id_named(project, "Footage")
    tl_id = next(iter(project.timelines))

    moved = project.move_timeline_to_bin(tl_id, bin_id)
    assert moved.get_unfiled_timeline_ids() == []


def test_unfiled_timeline_ids_shows_unfiled(tmp_path) -> None:
    project = (
        _project_with_two_assets_and_bin(tmp_path)
        .create_timeline("Seq 1")
        .create_timeline("Seq 2")
    )
    bin_id = _bin_id_named(project, "Footage")
    tl_ids = list(project.timelines.keys())
    tl1, tl2 = tl_ids[0], tl_ids[1]

    moved = project.move_timeline_to_bin(tl1, bin_id)
    assert moved.get_unfiled_timeline_ids() == [tl2]


# ---------------------------------------------------------------------------
# Panel-level integration: root view reflects the filter
# ---------------------------------------------------------------------------

def test_root_view_hides_asset_after_drop_into_bin(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_two_assets_and_bin(tmp_path)
    panel.set_project(project)
    QTest.qWait(120)

    ids = _asset_ids(project)
    a1, a2 = ids[0], ids[1]
    bin_id = _bin_id_named(project, "Footage")

    # Root view shows both assets initially.
    assert sorted(a.asset_id for a in panel._get_current_assets()) == sorted([a1, a2])

    # Drop a1 into the bin.
    assert panel._handle_drop_into_bin(bin_id, [a1], []) is True
    panel.set_project(panel._project)
    QTest.qWait(120)

    # Root view should now show only a2.
    shown = [a.asset_id for a in panel._get_current_assets()]
    assert shown == [a2]
    assert a1 not in shown

    # And inside the bin, a1 is visible.
    panel._enter_bin(bin_id)
    QTest.qWait(80)
    inside = [a.asset_id for a in panel._get_current_assets()]
    assert inside == [a1]


def test_root_view_hides_timeline_after_drop_into_bin(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_two_assets_and_bin(tmp_path).create_timeline("Seq 1")
    panel.set_project(project)
    QTest.qWait(120)

    tl_id = next(iter(project.timelines))
    bin_id = _bin_id_named(project, "Footage")

    # Root view shows the timeline initially.
    assert [t.timeline_id for t in panel._get_current_timelines()] == [tl_id]

    assert panel._handle_drop_into_bin(bin_id, [], [tl_id]) is True
    panel.set_project(panel._project)
    QTest.qWait(120)

    # Root view no longer shows it.
    assert panel._get_current_timelines() == []

    # Inside the bin it is visible.
    panel._enter_bin(bin_id)
    QTest.qWait(80)
    assert [t.timeline_id for t in panel._get_current_timelines()] == [tl_id]


def test_implicit_root_bin_is_not_rendered_as_a_media_pool_item(qapp, tmp_path) -> None:
    """The project root is a container, never a deletable media-pool card."""
    panel = MediaPoolPanel()
    project = (
        EditProject.create("Implicit Root Test")
        .import_asset(path=str(tmp_path / "clip.mp4"), media_type="video", metadata={})
        .create_bin("New Folder", parent_id=None)
    )
    panel.set_project(project)
    QTest.qWait(150)

    model = panel.asset_model
    assert model is not None
    visible_ids = {
        model.data(model.index(row, 0), model.ITEM_ID_ROLE)
        for row in range(model.rowCount())
    }
    assert project.root_bin_id not in visible_ids

    panel.set_view_mode(ViewMode.LIST)
    QTest.qWait(50)
    list_ids = {
        panel.tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)
        for row in range(panel.tree.topLevelItemCount())
    }
    assert project.root_bin_id not in list_ids


def test_timeline_card_accepts_async_preview_frame(qapp, tmp_path, monkeypatch) -> None:
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    project = EditProject.create("Timeline Preview")
    project = project.import_asset(
        path=str(media_path),
        media_type="video",
        metadata={"total_frames": 80, "fps": 25.0},
    )
    asset = project.get_asset_by_path(str(media_path))
    assert asset is not None
    timeline = TimelineSequenceRef(
        timeline_id="timeline-preview",
        name="Timeline 1",
        format_settings={
            MEDIA_POOL_PREVIEW_ASSET_ID_KEY: asset.asset_id,
            MEDIA_POOL_PREVIEW_FRAME_KEY: 17,
        },
    )
    project = replace(
        project,
        timelines={timeline.timeline_id: timeline},
        active_timeline_id=timeline.timeline_id,
    )

    panel = MediaPoolPanel()
    monkeypatch.setattr(
        panel._thumb_provider,
        "get_frame_thumbnail",
        lambda *args, **kwargs: None,
    )
    panel.set_project(project)
    QTest.qWait(150)

    preview = QPixmap(32, 18)
    preview.fill(Qt.GlobalColor.white)
    panel._on_timeline_preview_frame_ready(str(media_path), 17, preview)

    model = panel.asset_model
    assert model is not None
    timeline_index = next(
        model.index(row, 0)
        for row in range(model.rowCount())
        if model.data(model.index(row, 0), model.ITEM_TYPE_ROLE) == "timeline"
    )
    stored = model.data(timeline_index, model.TIMELINE_PREVIEW_ROLE)
    assert isinstance(stored, QPixmap)
    assert not stored.isNull()


def test_timeline_without_preview_source_uses_first_media_thumbnail(
    qapp, tmp_path, monkeypatch
) -> None:
    """New timelines still show a media preview before a random frame arrives."""
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    project = EditProject.create("Timeline Fallback").import_asset(
        path=str(media_path),
        media_type="video",
        metadata={"total_frames": 80, "fps": 25.0},
    )
    timeline = TimelineSequenceRef(timeline_id="timeline-fallback", name="Timeline 1")
    project = replace(
        project,
        timelines={timeline.timeline_id: timeline},
        active_timeline_id=timeline.timeline_id,
    )

    fallback = QPixmap(32, 18)
    fallback.fill(Qt.GlobalColor.darkGray)
    panel = MediaPoolPanel()
    monkeypatch.setattr(
        panel._thumb_provider,
        "get_cached_thumbnail",
        lambda *args, **kwargs: fallback,
    )
    monkeypatch.setattr(
        panel._thumb_provider,
        "get_frame_thumbnail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        panel._thumb_provider,
        "get_thumbnail",
        lambda *args, **kwargs: None,
    )
    panel.set_project(project)
    QTest.qWait(150)

    model = panel.asset_model
    assert model is not None
    timeline_index = next(
        model.index(row, 0)
        for row in range(model.rowCount())
        if model.data(model.index(row, 0), model.ITEM_TYPE_ROLE) == "timeline"
    )
    stored = model.data(timeline_index, model.TIMELINE_PREVIEW_ROLE)
    assert isinstance(stored, QPixmap)
    assert not stored.isNull()
