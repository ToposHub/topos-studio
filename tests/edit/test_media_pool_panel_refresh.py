from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject, update_offline_status
from src.features.edit.ui.media_pool_panel import (
    MediaPoolPanel,
    SORT_BY_DURATION,
    ViewMode,
)


def _project_with_asset(tmp_path) -> EditProject:
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"placeholder")
    return EditProject.create("Media Pool UI").import_asset(
        path=str(media_path),
        media_type="video",
        metadata={"duration_seconds": 2.0},
    )


def test_set_project_schedules_content_refresh(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path)

    panel.set_project(project)
    QTest.qWait(150)

    assert panel.asset_model.rowCount() == 1
    asset = panel.asset_model.index(0).data(Qt.ItemDataRole.UserRole)
    assert asset.asset_id in project.assets


def test_active_timeline_only_project_change_skips_pool_rebuild(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path).create_timeline("Sequence 1")
    panel.set_project(project)
    QTest.qWait(150)

    panel.set_project(project.set_active_timeline(project.active_timeline_id))

    assert panel._refresh_timer.isActive() is False

    panel.refresh()

    assert panel._refresh_timer.isActive() is True


def test_bin_selection_survives_bin_tree_refresh(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path).create_bin("Footage")
    footage_id = next(
        bin_id for bin_id, bin_obj in project.bins.items()
        if bin_obj.name == "Footage"
    )
    panel.set_project(project)
    panel._current_bin_id = footage_id
    panel._current_bin_is_smart = False

    panel._refresh_bin_tree()

    current = panel.bin_tree.currentItem()
    assert current is not None
    assert current.data(0, panel.bin_tree.BIN_ID_ROLE) == footage_id


def test_bin_tree_preserves_nested_bin_hierarchy(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path)
    project = project.create_bin("Footage", parent_id=project.root_bin_id)
    footage_id = next(
        bin_id for bin_id, bin_obj in project.bins.items()
        if bin_obj.name == "Footage"
    )
    project = project.create_bin("Day 01", parent_id=footage_id)
    day_id = next(
        bin_id for bin_id, bin_obj in project.bins.items()
        if bin_obj.name == "Day 01"
    )

    panel.set_project(project)

    assert panel.bin_tree.select_bin(day_id, False) is True
    current = panel.bin_tree.currentItem()
    assert current is not None
    assert current.parent().data(0, panel.bin_tree.BIN_ID_ROLE) == footage_id


def test_asset_selection_survives_content_refresh(qapp, tmp_path) -> None:
    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path)
    asset_id = next(iter(project.assets))
    panel.set_project(project)
    QTest.qWait(150)
    panel.set_view_mode(ViewMode.LIST)

    asset_item = next(
        panel.tree.topLevelItem(index)
        for index in range(panel.tree.topLevelItemCount())
        if panel.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) == asset_id
    )
    panel.tree.setCurrentItem(asset_item)
    panel._refresh_tree()
    QTest.qWait(150)

    selected = panel.get_selected_asset()
    assert selected is not None
    assert selected.asset_id == asset_id


def test_duration_sort_keeps_untimed_assets_last_in_both_directions() -> None:
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    panel._sort_by = SORT_BY_DURATION
    assets = [
        SimpleNamespace(display_name="still", duration_seconds=None),
        SimpleNamespace(display_name="long", duration_seconds=10.0),
        SimpleNamespace(display_name="short", duration_seconds=2.0),
    ]

    panel._sort_order = "asc"
    assert [asset.display_name for asset in panel._sort_assets(assets)] == [
        "short", "long", "still",
    ]

    panel._sort_order = "desc"
    assert [asset.display_name for asset in panel._sort_assets(assets)] == [
        "long", "short", "still",
    ]


def test_offline_status_preserves_project_identity_when_nothing_changed(tmp_path) -> None:
    project = _project_with_asset(tmp_path)

    assert update_offline_status(project) is project

    media_path = tmp_path / "clip.mp4"
    media_path.unlink()
    changed = update_offline_status(project)
    asset = next(iter(changed.assets.values()))
    assert changed is not project
    assert asset.is_offline is True


def test_panel_refresh_detects_source_removed_while_project_is_open(
    qapp, monkeypatch, tmp_path
) -> None:
    """The panel's periodic scan publishes a newly offline source.

    AUD-20202（wd12-g66）：可用性扫描已移出 UI 线程（OfflineScanWorker +
    QThreadPool）。本测试用内联线程池同步驱动同一链路（start → run →
    finished → _on_offline_scan_finished），锁定"扫描结果会发布新工程"的
    端到端语义；生产行为仍是异步 worker。
    """
    import src.features.edit.ui.media_pool_panel as mpp

    class _InlineThreadPool:
        @staticmethod
        def start(worker) -> None:
            worker.run()

    monkeypatch.setattr(
        mpp.QThreadPool, "globalInstance", staticmethod(lambda: _InlineThreadPool())
    )

    panel = MediaPoolPanel()
    project = _project_with_asset(tmp_path)
    media_path = tmp_path / "clip.mp4"
    panel.set_project(project)

    media_path.unlink()
    panel._refresh_offline_status()

    assert panel._project is not None
    asset = next(iter(panel._project.assets.values()))
    assert asset.is_offline is True
    panel.close()


def test_4k_video_starts_a_background_full_hd_proxy(qapp, tmp_path) -> None:
    """4K media is prepared for stable 1080p multi-track playback on import."""
    media_path = tmp_path / "master.mov"
    media_path.write_bytes(b"placeholder")
    project = EditProject.create("4K proxy").import_asset(
        path=str(media_path),
        media_type="video",
        metadata={"width": 3840, "height": 2160},
    )
    asset = next(iter(project.assets.values()))
    panel = MediaPoolPanel()
    missing_info = SimpleNamespace(
        is_ready=False,
        status=SimpleNamespace(value="missing"),
    )
    service = MagicMock()
    service.get_disk_proxy_info.return_value = missing_info

    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    service.generate_proxy.assert_called_once_with(
        source_path=str(media_path),
        asset_id=asset.asset_id,
    )


def test_batch_import_connects_new_4k_assets_to_proxy_preparation(qapp, tmp_path) -> None:
    media_path = tmp_path / "batch_master.mov"
    media_path.write_bytes(b"placeholder")
    project = EditProject.create("Batch proxy")
    panel = MediaPoolPanel()
    panel.set_project(project)
    panel._active_import_batch_id = "batch-4k"
    panel._active_import_target_bin_id = None

    with patch.object(panel, "_maybe_auto_ensure_disk_proxy") as ensure_proxy:
        panel._on_import_batch_finished(
            "batch-4k",
            project.project_id,
            [(str(media_path), "video", {"width": 3840, "height": 2160}, None)],
        )

    ensure_proxy.assert_called_once()
    assert ensure_proxy.call_args.args[0].path == str(media_path)


def test_full_hd_video_does_not_start_an_unneeded_proxy(qapp, tmp_path) -> None:
    media_path = tmp_path / "full_hd.mov"
    media_path.write_bytes(b"placeholder")
    project = EditProject.create("1080 proxy").import_asset(
        path=str(media_path),
        media_type="video",
        metadata={"width": 1920, "height": 1080},
    )
    asset = next(iter(project.assets.values()))
    panel = MediaPoolPanel()

    with patch("src.shared.media.disk_proxy_service.get_disk_proxy_service") as get_service:
        panel._maybe_auto_ensure_disk_proxy(asset)

    get_service.assert_not_called()
