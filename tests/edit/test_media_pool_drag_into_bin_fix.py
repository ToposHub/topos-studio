"""Regression tests for the asset/timeline drag-into-bin fix.

Bug: internal drags carry both the custom media-pool MIME types *and* file
URLs (asset paths). The three view drop handlers checked ``hasUrls()`` first,
so internal asset drags were misrouted to file-import and never reached the
drop-into-bin branch. The fix introduces ``_is_internal_media_pool_drag`` and
checks it *before* ``hasUrls()`` in every drag handler.
"""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt, QMimeData, QUrl, QByteArray, QPoint
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import (
    MediaPoolPanel,
    MIME_TYPE_MEDIA_POOL_ASSETS,
    MIME_TYPE_MEDIA_POOL_TIMELINES,
    _is_internal_media_pool_drag,
    build_media_pool_drag_mime,
    parse_media_pool_drag_mime,
)


def _project_with_bin_and_asset(tmp_path):
    """Project with one asset, one timeline and one (non-root) bin."""
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    project = (
        EditProject.create("Drag Into Bin")
        .import_asset(path=str(media_path), media_type="video", metadata={})
        .create_timeline("Seq 1")
        .create_bin("Footage")
    )
    return project


def _bin_id_named(project, name):
    return next(bid for bid, b in project.bins.items() if b.name == name)


def _asset_id(project):
    return next(iter(project.assets))


def _timeline_id(project):
    return next(iter(project.timelines))


# ---------------------------------------------------------------------------
# MIME helpers
# ---------------------------------------------------------------------------

def test_is_internal_drag_detects_asset_mime() -> None:
    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, QByteArray(json.dumps(["a1"]).encode()))
    assert _is_internal_media_pool_drag(mime) is True


def test_is_internal_drag_detects_timeline_mime() -> None:
    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_TIMELINES, QByteArray(json.dumps(["t1"]).encode()))
    assert _is_internal_media_pool_drag(mime) is True


def test_is_internal_drag_false_for_pure_urls() -> None:
    """A pure OS file drop (only URLs, no custom MIME) is external."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
    assert _is_internal_media_pool_drag(mime) is False


def test_is_internal_drag_true_when_urls_and_custom_mime_both_present() -> None:
    """This is the exact bug condition: asset drag has both URLs and MIME."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, QByteArray(json.dumps(["a1"]).encode()))
    assert _is_internal_media_pool_drag(mime) is True


# ---------------------------------------------------------------------------
# build / parse round-trip
# ---------------------------------------------------------------------------

def test_build_drag_mime_attaches_urls_for_assets(tmp_path) -> None:
    """Confirms the precondition of the bug: asset drags carry URLs."""
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    mime = build_media_pool_drag_mime(
        asset_ids=["a1"],
        asset_paths=[str(media_path)],
    )
    assert mime.hasFormat(MIME_TYPE_MEDIA_POOL_ASSETS)
    assert mime.hasUrls()  # this is why hasUrls() must NOT short-circuit


def test_parse_drag_mime_reads_both_types() -> None:
    mime = build_media_pool_drag_mime(
        asset_ids=["a1", "a2"],
        timeline_ids=["t1"],
    )
    asset_ids, timeline_ids = parse_media_pool_drag_mime(mime)
    assert asset_ids == ["a1", "a2"]
    assert timeline_ids == ["t1"]


# ---------------------------------------------------------------------------
# End-to-end drop-into-bin through the panel
# ---------------------------------------------------------------------------

def test_handle_drop_into_bin_moves_asset(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_bin_and_asset(tmp_path)
    panel.set_project(project)
    QTest.qWait(120)

    bin_id = _bin_id_named(project, "Footage")
    asset_id = _asset_id(project)

    ok = panel._handle_drop_into_bin(bin_id, [asset_id], [])
    assert ok is True

    panel.set_project(panel._project)
    QTest.qWait(120)
    target_bin = panel._project.bins[bin_id]
    assert asset_id in target_bin.asset_ids


def test_handle_drop_into_bin_moves_timeline(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_bin_and_asset(tmp_path)
    panel.set_project(project)
    QTest.qWait(120)

    bin_id = _bin_id_named(project, "Footage")
    timeline_id = _timeline_id(project)

    ok = panel._handle_drop_into_bin(bin_id, [], [timeline_id])
    assert ok is True

    target_bin = panel._project.bins[bin_id]
    assert timeline_id in target_bin.timeline_ids


def test_handle_drop_into_bin_rejects_root_bin(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_bin_and_asset(tmp_path)
    panel.set_project(project)
    QTest.qWait(120)

    asset_id = _asset_id(project)
    # Root bin is a no-op (implicit container).
    ok = panel._handle_drop_into_bin(project.root_bin_id, [asset_id], [])
    assert ok is False


def test_handle_drop_into_bin_is_idempotent(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_bin_and_asset(tmp_path)
    panel.set_project(project)
    QTest.qWait(120)

    bin_id = _bin_id_named(project, "Footage")
    asset_id = _asset_id(project)

    assert panel._handle_drop_into_bin(bin_id, [asset_id], []) is True
    # Second drop with the same asset is a no-op.
    assert panel._handle_drop_into_bin(bin_id, [asset_id], []) is False
