# -*- coding: utf-8 -*-
"""
Tests for P6.3 Offline/Relink/Proxy functionality.

Tests the offline detection, relink, and proxy features
in the project data model layer.
"""

import os
import tempfile
import time
import pytest

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    check_media_availability,
    get_offline_assets,
    update_offline_status,
    relink_asset,
    batch_relink_assets,
    update_asset_proxy,
)

pytestmark = pytest.mark.edit_core


# ================================================================
# Fixtures
# ================================================================

@pytest.fixture
def tmp_media_files(tmp_path):
    """Create temporary media files for testing."""
    files = {}
    # Video file
    video_path = str(tmp_path / "test_video.mp4")
    with open(video_path, "wb") as f:
        f.write(b"fake video data")
    files["video"] = video_path

    # Image file
    image_path = str(tmp_path / "test_image.jpg")
    with open(image_path, "wb") as f:
        f.write(b"fake image data")
    files["image"] = image_path

    # Audio file
    audio_path = str(tmp_path / "test_audio.wav")
    with open(audio_path, "wb") as f:
        f.write(b"fake audio data")
    files["audio"] = audio_path

    return files


@pytest.fixture
def project_with_assets(tmp_media_files):
    """Create a project with test assets."""
    project = EditProject.create("Test Project")

    # Import video
    project = project.import_asset(
        path=tmp_media_files["video"],
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0},
        display_name="test_video.mp4",
    )

    # Import image
    project = project.import_asset(
        path=tmp_media_files["image"],
        media_type="image",
        metadata={"width": 3840, "height": 2160},
        display_name="test_image.jpg",
    )

    # Import audio
    project = project.import_asset(
        path=tmp_media_files["audio"],
        media_type="audio",
        metadata={"format": "WAV"},
        display_name="test_audio.wav",
    )

    return project


@pytest.fixture
def project_with_offline_assets():
    """Create a project with offline (non-existent) assets."""
    project = EditProject.create("Offline Test Project")

    # Import assets pointing to non-existent paths
    project = project.import_asset(
        path="/nonexistent/video_offline.mp4",
        media_type="video",
        metadata={"width": 1920, "height": 1080},
        display_name="video_offline.mp4",
    )

    project = project.import_asset(
        path="/nonexistent/image_offline.jpg",
        media_type="image",
        metadata={"width": 3840, "height": 2160},
        display_name="image_offline.jpg",
    )

    return project


# ================================================================
# Test: MediaAsset offline and proxy properties
# ================================================================

class TestMediaAssetOfflineProxy:
    """Test MediaAsset offline and proxy properties."""

    def test_asset_default_not_offline(self):
        """New asset should not be offline by default."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        assert asset.is_offline is False

    def test_asset_offline_status(self):
        """Asset can be marked as offline."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/nonexistent/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            is_offline=True,
        )
        assert asset.is_offline is True

    def test_with_offline_status_factory(self):
        """with_offline_status creates new instance with updated offline flag."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        offline_asset = asset.with_offline_status(True)
        assert offline_asset.is_offline is True
        assert asset.is_offline is False  # Original unchanged (frozen)
        assert offline_asset.asset_id == asset.asset_id

    def test_has_proxy_default_false(self):
        """New asset should not have proxy by default."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        assert asset.has_proxy is False

    def test_has_proxy_with_ready_status_and_path(self):
        """Asset with proxy_status='ready' and proxy_path should have proxy."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_path="/some/file_proxy.mp4",
            proxy_status="ready",
        )
        assert asset.has_proxy is True

    def test_has_proxy_false_without_path(self):
        """Asset with proxy_status='ready' but no proxy_path should not have proxy."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_status="ready",
            proxy_path=None,
        )
        assert asset.has_proxy is False

    def test_has_proxy_false_with_pending_status(self):
        """Asset with proxy_status='pending' should not have proxy."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_path="/some/file_proxy.mp4",
            proxy_status="pending",
        )
        assert asset.has_proxy is False

    def test_with_proxy_status_factory(self):
        """with_proxy_status creates new instance."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        updated = asset.with_proxy_status("pending")
        assert updated.proxy_status == "pending"
        assert asset.proxy_status == "none"  # Original unchanged

    def test_with_proxy_path_factory(self):
        """with_proxy_path creates new instance."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        updated = asset.with_proxy_path("/some/file_proxy.mp4")
        assert updated.proxy_path == "/some/file_proxy.mp4"
        assert asset.proxy_path is None  # Original unchanged

    def test_with_path_factory(self):
        """with_path creates new instance with updated path."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/old/path.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        updated = asset.with_path("/new/path.mp4")
        assert updated.path == "/new/path.mp4"
        assert asset.path == "/old/path.mp4"  # Original unchanged

    def test_effective_path_prefers_proxy(self):
        """effective_path should return proxy_path when proxy is available."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_path="/some/file_proxy.mp4",
            proxy_status="ready",
        )
        assert asset.effective_path == "/some/file_proxy.mp4"

    def test_effective_path_falls_back_to_source(self):
        """effective_path should return source path when no proxy."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/some/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        assert asset.effective_path == "/some/file.mp4"

    def test_effective_path_source_when_offline(self):
        """effective_path should return source path when asset is offline even with proxy."""
        asset = MediaAsset(
            asset_id="test-1",
            path="/nonexistent/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_path="/some/file_proxy.mp4",
            proxy_status="ready",
            is_offline=True,
        )
        # Offline assets should use source path, not proxy
        assert asset.effective_path == "/nonexistent/file.mp4"

    def test_invalid_proxy_status_raises(self):
        """Invalid proxy_status should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid proxy_status"):
            MediaAsset(
                asset_id="test-1",
                path="/some/file.mp4",
                media_type="video",
                display_name="file.mp4",
                metadata={},
                proxy_status="invalid_status",
            )

    def test_proxy_status_valid_values(self):
        """All valid proxy_status values should work."""
        for status in ("none", "pending", "ready", "failed"):
            asset = MediaAsset(
                asset_id="test-1",
                path="/some/file.mp4",
                media_type="video",
                display_name="file.mp4",
                metadata={},
                proxy_status=status,
            )
            assert asset.proxy_status == status


# ================================================================
# Test: Offline detection
# ================================================================

class TestOfflineDetection:
    """Test offline detection functionality."""

    def test_check_media_availability_online(self, project_with_assets):
        """All existing files should be marked as available."""
        availability = check_media_availability(project_with_assets)
        assert len(availability) == 3
        for asset_id, is_available in availability.items():
            assert is_available is True, f"Asset {asset_id} should be available"

    def test_check_media_availability_offline(self, project_with_offline_assets):
        """Non-existent files should be marked as unavailable."""
        availability = check_media_availability(project_with_offline_assets)
        assert len(availability) == 2
        for asset_id, is_available in availability.items():
            assert is_available is False, f"Asset {asset_id} should be offline"

    def test_get_offline_assets_online_project(self, project_with_assets):
        """Online project should have no offline assets."""
        offline = get_offline_assets(project_with_assets)
        assert len(offline) == 0

    def test_get_offline_assets_offline_project(self, project_with_offline_assets):
        """Project with missing files should list offline assets."""
        offline = get_offline_assets(project_with_offline_assets)
        assert len(offline) == 2
        for asset in offline:
            assert not os.path.exists(asset.path)

    def test_update_offline_status_marks_missing(self, project_with_offline_assets):
        """update_offline_status should mark missing files as offline."""
        updated_project = update_offline_status(project_with_offline_assets)
        for asset in updated_project.assets.values():
            assert asset.is_offline is True

    def test_update_offline_status_marks_existing(self, project_with_assets):
        """update_offline_status should mark existing files as online."""
        updated_project = update_offline_status(project_with_assets)
        for asset in updated_project.assets.values():
            assert asset.is_offline is False

    def test_update_offline_status_immutable(self, project_with_offline_assets):
        """update_offline_status should not modify the original project."""
        original_assets = dict(project_with_offline_assets.assets)
        _ = update_offline_status(project_with_offline_assets)
        # Original project should be unchanged
        for asset_id, asset in project_with_offline_assets.assets.items():
            assert asset.is_offline == original_assets[asset_id].is_offline

    def test_update_offline_status_file_deleted(self, project_with_assets):
        """Deleting a file after import should mark it as offline."""
        # Get the video asset path
        video_asset = project_with_assets.video_assets[0]
        video_path = video_asset.path

        # Delete the file
        os.remove(video_path)

        # Update offline status
        updated_project = update_offline_status(project_with_assets)
        updated_asset = updated_project.assets.get(video_asset.asset_id)
        assert updated_asset.is_offline is True

    def test_update_offline_status_file_restored(self, project_with_offline_assets, tmp_path):
        """Creating a file at a new location and relinking should mark it as online."""
        # Get first offline asset
        offline_asset = list(project_with_offline_assets.assets.values())[0]

        # Instead of trying to create file at /nonexistent (which may be read-only),
        # verify the offline status detection, then relink to a valid path
        updated_project = update_offline_status(project_with_offline_assets)
        assert updated_project.assets[offline_asset.asset_id].is_offline is True

        # Create a new file and relink
        new_path = str(tmp_path / "restored_file.mp4")
        with open(new_path, "wb") as f:
            f.write(b"restored data")

        updated_project = relink_asset(updated_project, offline_asset.asset_id, new_path)
        restored_asset = updated_project.assets.get(offline_asset.asset_id)
        assert restored_asset.is_offline is False
        assert restored_asset.path == new_path

    def test_edit_project_offline_assets_property(self, project_with_offline_assets):
        """EditProject.offline_assets property should return offline assets."""
        # Note: this property checks os.path.exists at call time
        offline = project_with_offline_assets.offline_assets
        assert len(offline) == 2


# ================================================================
# Test: Relink
# ================================================================

class TestRelink:
    """Test relink functionality."""

    def test_relink_asset_updates_path(self, project_with_offline_assets, tmp_path):
        """Relinking should update the asset path."""
        # Get first offline asset
        offline_asset = list(project_with_offline_assets.assets.values())[0]
        asset_id = offline_asset.asset_id

        # Create new file
        new_path = str(tmp_path / "relocated_video.mp4")
        with open(new_path, "wb") as f:
            f.write(b"relocated data")

        # Relink
        updated_project = relink_asset(project_with_offline_assets, asset_id, new_path)

        # Verify
        updated_asset = updated_project.assets.get(asset_id)
        assert updated_asset.path == new_path
        assert updated_asset.is_offline is False  # File now exists

    def test_relink_asset_to_nonexistent_path(self, project_with_offline_assets):
        """Relinking to a non-existent path should mark asset as offline."""
        offline_asset = list(project_with_offline_assets.assets.values())[0]
        asset_id = offline_asset.asset_id

        # Relink to another non-existent path
        new_path = "/another/nonexistent/path.mp4"
        updated_project = relink_asset(project_with_offline_assets, asset_id, new_path)

        updated_asset = updated_project.assets.get(asset_id)
        assert updated_asset.path == new_path
        assert updated_asset.is_offline is True  # Still offline

    def test_relink_asset_raises_for_unknown_id(self, project_with_assets):
        """Relinking a non-existent asset should raise ValueError."""
        with pytest.raises(ValueError, match="Asset not found"):
            relink_asset(project_with_assets, "nonexistent-id", "/some/path.mp4")

    def test_relink_asset_immutable(self, project_with_offline_assets, tmp_path):
        """Relinking should not modify the original project."""
        offline_asset = list(project_with_offline_assets.assets.values())[0]
        original_path = offline_asset.path

        new_path = str(tmp_path / "relocated.mp4")
        with open(new_path, "wb") as f:
            f.write(b"relocated data")

        _ = relink_asset(project_with_offline_assets, offline_asset.asset_id, new_path)

        # Original should be unchanged
        original_asset = project_with_offline_assets.assets.get(offline_asset.asset_id)
        assert original_asset.path == original_path

    def test_batch_relink_assets(self, project_with_offline_assets, tmp_path):
        """Batch relinking should update multiple assets."""
        assets = list(project_with_offline_assets.assets.values())

        # Create new files for each offline asset
        relink_map = {}
        for asset in assets:
            new_filename = os.path.basename(asset.path)
            new_path = str(tmp_path / new_filename)
            with open(new_path, "wb") as f:
                f.write(b"batch relocated data")
            relink_map[asset.asset_id] = new_path

        # Batch relink
        updated_project = batch_relink_assets(project_with_offline_assets, relink_map)

        # Verify all assets are now online
        for asset_id, new_path in relink_map.items():
            updated_asset = updated_project.assets.get(asset_id)
            assert updated_asset.path == new_path
            assert updated_asset.is_offline is False

    def test_batch_relink_partial(self, project_with_offline_assets, tmp_path):
        """Batch relinking only some assets should leave others unchanged."""
        assets = list(project_with_offline_assets.assets.values())

        # Only relink the first asset
        first_asset = assets[0]
        new_path = str(tmp_path / os.path.basename(first_asset.path))
        with open(new_path, "wb") as f:
            f.write(b"partial relocated data")

        relink_map = {first_asset.asset_id: new_path}
        updated_project = batch_relink_assets(project_with_offline_assets, relink_map)

        # First asset should be updated
        updated_first = updated_project.assets.get(first_asset.asset_id)
        assert updated_first.path == new_path
        assert updated_first.is_offline is False

        # Second asset should be unchanged
        second_asset = assets[1]
        updated_second = updated_project.assets.get(second_asset.asset_id)
        assert updated_second.path == second_asset.path  # Path unchanged
        # Note: offline status is based on file existence at the original path

    def test_batch_relink_empty_map(self, project_with_assets):
        """Batch relinking with empty map should return equivalent project."""
        updated_project = batch_relink_assets(project_with_assets, {})
        # Assets should be the same
        assert len(updated_project.assets) == len(project_with_assets.assets)


# ================================================================
# Test: Proxy
# ================================================================

class TestProxy:
    """Test proxy functionality."""

    def test_update_asset_proxy_sets_path_and_status(self, project_with_assets):
        """update_asset_proxy should set proxy path and status."""
        video_asset = project_with_assets.video_assets[0]
        proxy_path = "/tmp/test_proxy.mp4"

        updated_project = update_asset_proxy(
            project_with_assets, video_asset.asset_id, proxy_path, "ready"
        )

        updated_asset = updated_project.assets.get(video_asset.asset_id)
        assert updated_asset.proxy_path == proxy_path
        assert updated_asset.proxy_status == "ready"
        assert updated_asset.has_proxy is True

    def test_update_asset_proxy_pending_status(self, project_with_assets):
        """update_asset_proxy with pending status should not show has_proxy."""
        video_asset = project_with_assets.video_assets[0]

        updated_project = update_asset_proxy(
            project_with_assets, video_asset.asset_id, None, "pending"
        )

        updated_asset = updated_project.assets.get(video_asset.asset_id)
        assert updated_asset.proxy_status == "pending"
        assert updated_asset.has_proxy is False

    def test_update_asset_proxy_failed_status(self, project_with_assets):
        """update_asset_proxy with failed status should not show has_proxy."""
        video_asset = project_with_assets.video_assets[0]

        updated_project = update_asset_proxy(
            project_with_assets, video_asset.asset_id, None, "failed"
        )

        updated_asset = updated_project.assets.get(video_asset.asset_id)
        assert updated_asset.proxy_status == "failed"
        assert updated_asset.has_proxy is False

    def test_update_asset_proxy_clear(self, project_with_assets):
        """update_asset_proxy can clear proxy status."""
        video_asset = project_with_assets.video_assets[0]

        # Set proxy first
        project_with_proxy = update_asset_proxy(
            project_with_assets, video_asset.asset_id, "/tmp/proxy.mp4", "ready"
        )
        assert project_with_proxy.assets[video_asset.asset_id].has_proxy is True

        # Clear proxy
        cleared_project = update_asset_proxy(
            project_with_proxy, video_asset.asset_id, None, "none"
        )
        assert cleared_project.assets[video_asset.asset_id].has_proxy is False
        assert cleared_project.assets[video_asset.asset_id].proxy_path is None

    def test_update_asset_proxy_raises_for_unknown_id(self, project_with_assets):
        """update_asset_proxy should raise for unknown asset_id."""
        with pytest.raises(ValueError, match="Asset not found"):
            update_asset_proxy(project_with_assets, "nonexistent-id", "/tmp/proxy.mp4", "ready")

    def test_update_asset_proxy_immutable(self, project_with_assets):
        """update_asset_proxy should not modify the original project."""
        video_asset = project_with_assets.video_assets[0]

        _ = update_asset_proxy(
            project_with_assets, video_asset.asset_id, "/tmp/proxy.mp4", "ready"
        )

        # Original should be unchanged
        original_asset = project_with_assets.assets.get(video_asset.asset_id)
        assert original_asset.proxy_status == "none"
        assert original_asset.proxy_path is None

    def test_proxy_workflow_full_cycle(self, project_with_assets):
        """Test the full proxy workflow: none -> pending -> ready -> delete."""
        video_asset = project_with_assets.video_assets[0]
        asset_id = video_asset.asset_id

        # Initially no proxy
        assert project_with_assets.assets[asset_id].has_proxy is False

        # Start proxy generation (pending)
        project = update_asset_proxy(project_with_assets, asset_id, None, "pending")
        assert project.assets[asset_id].proxy_status == "pending"
        assert project.assets[asset_id].has_proxy is False

        # Proxy generated (ready)
        proxy_path = "/tmp/test_proxy.mp4"
        project = update_asset_proxy(project, asset_id, proxy_path, "ready")
        assert project.assets[asset_id].proxy_status == "ready"
        assert project.assets[asset_id].proxy_path == proxy_path
        assert project.assets[asset_id].has_proxy is True

        # Delete proxy
        project = update_asset_proxy(project, asset_id, None, "none")
        assert project.assets[asset_id].proxy_status == "none"
        assert project.assets[asset_id].has_proxy is False

    def test_proxy_workflow_generation_failed(self, project_with_assets):
        """Test proxy workflow when generation fails."""
        video_asset = project_with_assets.video_assets[0]
        asset_id = video_asset.asset_id

        # Start proxy generation (pending)
        project = update_asset_proxy(project_with_assets, asset_id, None, "pending")

        # Generation failed
        project = update_asset_proxy(project, asset_id, None, "failed")
        assert project.assets[asset_id].proxy_status == "failed"
        assert project.assets[asset_id].has_proxy is False


# ================================================================
# Test: Integration - Offline + Relink + Proxy
# ================================================================

class TestIntegration:
    """Integration tests for offline/relink/proxy workflow."""

    def test_relink_then_check_online(self, project_with_offline_assets, tmp_path):
        """Relinking an offline asset should make it online."""
        offline_asset = list(project_with_offline_assets.assets.values())[0]
        asset_id = offline_asset.asset_id

        # Verify offline
        assert offline_asset.is_offline is False  # Default; not yet checked

        # Update offline status first
        project = update_offline_status(project_with_offline_assets)
        assert project.assets[asset_id].is_offline is True

        # Create file and relink
        new_path = str(tmp_path / os.path.basename(offline_asset.path))
        with open(new_path, "wb") as f:
            f.write(b"relinked data")

        project = relink_asset(project, asset_id, new_path)
        assert project.assets[asset_id].is_offline is False
        assert project.assets[asset_id].path == new_path

    def test_offline_then_relink_then_proxy(self, project_with_offline_assets, tmp_path):
        """Full workflow: offline -> relink -> generate proxy."""
        offline_asset = list(project_with_offline_assets.assets.values())[0]
        asset_id = offline_asset.asset_id

        # Update offline status
        project = update_offline_status(project_with_offline_assets)
        assert project.assets[asset_id].is_offline is True

        # Relink
        new_path = str(tmp_path / os.path.basename(offline_asset.path))
        with open(new_path, "wb") as f:
            f.write(b"relinked data")

        project = relink_asset(project, asset_id, new_path)
        assert project.assets[asset_id].is_offline is False

        # Generate proxy
        project = update_asset_proxy(project, asset_id, None, "pending")
        assert project.assets[asset_id].proxy_status == "pending"

        proxy_path = str(tmp_path / "test_proxy.mp4")
        with open(proxy_path, "wb") as f:
            f.write(b"proxy data")

        project = update_asset_proxy(project, asset_id, proxy_path, "ready")
        assert project.assets[asset_id].has_proxy is True
        assert project.assets[asset_id].effective_path == proxy_path

    def test_proxy_after_offline_uses_source(self, project_with_assets, tmp_path):
        """When an asset with proxy goes offline, effective_path should use source."""
        video_asset = project_with_assets.video_assets[0]
        asset_id = video_asset.asset_id

        # Generate proxy
        proxy_path = str(tmp_path / "test_proxy.mp4")
        with open(proxy_path, "wb") as f:
            f.write(b"proxy data")

        project = update_asset_proxy(project_with_assets, asset_id, proxy_path, "ready")
        assert project.assets[asset_id].has_proxy is True
        assert project.assets[asset_id].effective_path == proxy_path

        # Mark as offline (simulate file moved)
        project = update_offline_status(project)
        # File still exists, so it's not offline
        assert project.assets[asset_id].is_offline is False

        # Manually set offline to test the behavior
        offline_asset = project.assets[asset_id].with_offline_status(True)
        new_assets = dict(project.assets)
        new_assets[asset_id] = offline_asset
        from src.features.edit.core.project import EditProject as EP
        project = EP(
            project_id=project.project_id,
            name=project.name,
            assets=new_assets,
            bins=project.bins,
            timelines=project.timelines,
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            created_at=project.created_at,
            modified_at=project.modified_at,
        )
        # When offline, effective_path falls back to source
        assert project.assets[asset_id].effective_path == video_asset.path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ================================================================
# M12.3 · 重连对话框可靠匹配接线（对话框 helper 层）
# ================================================================

class TestRelinkDialogReliableMatch:
    def _dialog(self):
        from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog
        # 最小构造：不调用真实 __init__（UI 重），只测 helper
        dlg = RelinkMediaDialog.__new__(RelinkMediaDialog)
        return dlg

    def test_auto_unique_exact_match(self, tmp_path):
        dlg = self._dialog()
        (tmp_path / "a.mov").write_bytes(b"content-A" * 100)
        # 原文件在别处（不在扫描目录）——真实缺失场景
        elsewhere = tmp_path.parent / "origin_dir"
        elsewhere.mkdir(exist_ok=True)
        missing = elsewhere / "a.mov"
        missing.write_bytes(b"content-A" * 100)
        found, resolution = dlg._reliable_match(
            str(tmp_path), str(missing), None)
        assert found == str(tmp_path / "a.mov")
        assert resolution == "auto"

    def test_same_name_different_content_goes_manual(self, tmp_path):
        dlg = self._dialog()
        # 原文件在别处（真实内容），扫描目录有同名异内容文件
        elsewhere = tmp_path.parent / "origin_dir2"
        elsewhere.mkdir(exist_ok=True)
        missing = elsewhere / "b.mov"
        missing.write_bytes(b"content-B" * 100)
        (tmp_path / "b.mov").write_bytes(b"REPLACED" * 100)
        found, resolution = dlg._reliable_match(
            str(tmp_path), str(missing), None)
        assert found == str(tmp_path / "b.mov")
        assert resolution == "manual"

    def test_no_candidate_none(self, tmp_path):
        dlg = self._dialog()
        found, resolution = dlg._reliable_match(
            str(tmp_path), "/gone/x.mov", None)
        assert found is None and resolution == "none"
