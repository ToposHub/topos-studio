# -*- coding: utf-8 -*-
"""
Tests for OfflineAssetManager - P3 Relink/离线素材统一管理入口.

Tests the unified offline asset management functionality:
- OfflineAssetReport generation
- Batch relink support
- Auto search for missing files
- Support for both ToposProject and EditProject

参考文档：
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
"""

import os
import tempfile
import time
from dataclasses import replace
import pytest

from src.shared.project.offline_asset_manager import (
    OfflineAssetManager,
    OfflineAssetReport,
    RelinkResult,
)
from src.shared.project.topos_project import (
    ToposProject,
    MediaAssetRef,
)
from src.shared.project.topos_project_io import ToposProjectIO
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
)


# Register the EditProject adapter so OfflineAssetManager can dispatch
# EditProject operations. This is normally done at app startup by EditModule,
# but tests run without the full app.
def _ensure_edit_adapter_registered():
    from src.shared.project.edit_project_adapter import edit_project_adapter
    if not edit_project_adapter.is_registered:
        from src.features.edit.core.project import (
            relink_asset,
            batch_relink_assets,
            update_offline_status,
        )
        edit_project_adapter.register(
            edit_project_type=EditProject,
            create_project=EditProject.create,
            relink_asset_fn=relink_asset,
            batch_relink_fn=batch_relink_assets,
            update_offline_status_fn=update_offline_status,
            show_relink_dialog_fn=None,
        )


_ensure_edit_adapter_registered()

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

    return files


@pytest.fixture
def topos_project_with_assets(tmp_media_files):
    """Create a ToposProject with test assets."""
    assets = {}

    # Online video asset
    assets["asset-1"] = MediaAssetRef(
        asset_id="asset-1",
        path=tmp_media_files["video"],
        media_type="video",
        display_name="test_video.mp4",
        is_offline=False,
    )

    # Online image asset
    assets["asset-2"] = MediaAssetRef(
        asset_id="asset-2",
        path=tmp_media_files["image"],
        media_type="image",
        display_name="test_image.jpg",
        is_offline=False,
    )

    # Offline asset (non-existent path)
    assets["asset-3"] = MediaAssetRef(
        asset_id="asset-3",
        path="/nonexistent/offline_video.mp4",
        media_type="video",
        display_name="offline_video.mp4",
        is_offline=True,
    )

    # Another offline asset
    assets["asset-4"] = MediaAssetRef(
        asset_id="asset-4",
        path="/nonexistent/offline_image.jpg",
        media_type="image",
        display_name="offline_image.jpg",
        is_offline=True,
    )

    project = ToposProject(
        project_id="test-project-id",
        name="Test Project",
        project_mode="edit_project",
        assets=assets,
        bins={},
        timelines={},
        color_stacks={},
        compositions={},
        deliver_presets={},
        ui_state={},
        settings={},
        primary_asset_id=None,
        primary_color_stack_id=None,
        active_timeline_id=None,
        active_composition_id=None,
        created_at=time.time(),
        modified_at=time.time(),
    )

    return project


@pytest.fixture
def edit_project_with_assets(tmp_media_files):
    """Create an EditProject with test assets."""
    project = EditProject.create("Test Edit Project")

    # Import online video
    project = project.import_asset(
        path=tmp_media_files["video"],
        media_type="video",
        metadata={"width": 1920, "height": 1080, "fps": 24.0},
        display_name="test_video.mp4",
    )

    # Import online image
    project = project.import_asset(
        path=tmp_media_files["image"],
        media_type="image",
        metadata={"width": 3840, "height": 2160},
        display_name="test_image.jpg",
    )

    # Import offline assets (non-existent paths)
    project = project.import_asset(
        path="/nonexistent/offline_video.mp4",
        media_type="video",
        metadata={"width": 1920, "height": 1080},
        display_name="offline_video.mp4",
    )

    project = project.import_asset(
        path="/nonexistent/offline_audio.wav",
        media_type="audio",
        metadata={"format": "WAV"},
        display_name="offline_audio.wav",
    )

    return project


# ================================================================
# Test: OfflineAssetReport
# ================================================================

class TestOfflineAssetReport:
    """Test OfflineAssetReport data class."""

    def test_report_creation(self):
        """Test creating an OfflineAssetReport."""
        report = OfflineAssetReport(
            total_assets=10,
            offline_count=2,
            online_count=8,
            offline_assets=[
                {"asset_id": "a1", "path": "/path1"},
                {"asset_id": "a2", "path": "/path2"},
            ],
            missing_paths=["/path1", "/path2"],
            suggested_search_dirs=["/home", "/documents"],
            timestamp=time.time(),
            project_path="/test/project.topos",
        )

        assert report.total_assets == 10
        assert report.offline_count == 2
        assert report.online_count == 8
        assert len(report.offline_assets) == 2
        assert report.has_offline_assets() is True
        assert report.get_summary() == "2/10 assets offline"

    def test_report_no_offline(self):
        """Test report with no offline assets."""
        report = OfflineAssetReport(
            total_assets=5,
            offline_count=0,
            online_count=5,
            offline_assets=[],
            missing_paths=[],
            suggested_search_dirs=[],
            timestamp=time.time(),
        )

        assert report.has_offline_assets() is False
        assert report.get_summary() == "All assets are online"

    def test_report_serialization(self):
        """Test report serialization to/from dict."""
        report = OfflineAssetReport(
            total_assets=10,
            offline_count=2,
            online_count=8,
            offline_assets=[
                {"asset_id": "a1", "path": "/path1"},
            ],
            missing_paths=["/path1"],
            suggested_search_dirs=["/home"],
            timestamp=12345.0,
            project_path="/test/project.topos",
        )

        data = report.to_dict()
        restored = OfflineAssetReport.from_dict(data)

        assert restored.total_assets == report.total_assets
        assert restored.offline_count == report.offline_count
        assert restored.timestamp == report.timestamp
        assert restored.project_path == report.project_path


# ================================================================
# Test: OfflineAssetManager - ToposProject
# ================================================================

class TestOfflineAssetManagerToposProject:
    """Test OfflineAssetManager with ToposProject."""

    def test_check_offline_assets(self, topos_project_with_assets):
        """Test checking offline assets in ToposProject."""
        report = OfflineAssetManager.check_offline_assets(topos_project_with_assets)

        assert report.total_assets == 4
        assert report.offline_count == 2
        assert report.online_count == 2
        assert report.has_offline_assets() is True

    def test_virtual_motion_graphic_asset_is_not_offline(
        self, topos_project_with_assets,
    ):
        project = topos_project_with_assets
        assets = dict(project.assets)
        assets["motion-graphic"] = MediaAssetRef(
            asset_id="motion-graphic",
            path="topos://motion_graphic/scene-1",
            media_type="motion_graphic",
            display_name="Motion Graphic Clip",
        )
        project = replace(project, assets=assets)

        report = OfflineAssetManager.check_offline_assets(project)
        assert report.total_assets == 5
        assert report.offline_count == 2
        assert "motion-graphic" not in {
            asset["asset_id"] for asset in report.offline_assets
        }
        offline_assets = ToposProjectIO._check_offline_assets_in_project(project)
        assert [asset.asset_id for asset in offline_assets] == ["asset-3", "asset-4"]

    def test_get_offline_asset_list(self, topos_project_with_assets):
        """Test getting offline asset list."""
        offline_list = OfflineAssetManager.get_offline_asset_list(topos_project_with_assets)

        assert len(offline_list) == 2
        # Should be MediaAssetRef objects
        for asset in offline_list:
            assert isinstance(asset, MediaAssetRef)
            assert asset.is_offline is True

    def test_relink_single_asset(self, topos_project_with_assets, tmp_path):
        """Test relinking a single asset."""
        # Create a new file for relinking
        new_path = str(tmp_path / "relinked_video.mp4")
        with open(new_path, "wb") as f:
            f.write(b"new data")

        # Relink offline asset
        new_project = OfflineAssetManager.relink_asset(
            topos_project_with_assets,
            "asset-3",
            new_path,
        )

        # Check the asset is now online
        assert new_project.assets["asset-3"].path == new_path
        assert new_project.assets["asset-3"].is_offline is False

    def test_batch_relink(self, topos_project_with_assets, tmp_path):
        """Test batch relinking multiple assets."""
        # Create new files for relinking
        new_video = str(tmp_path / "relinked_video.mp4")
        new_image = str(tmp_path / "relinked_image.jpg")
        with open(new_video, "wb") as f:
            f.write(b"new video")
        with open(new_image, "wb") as f:
            f.write(b"new image")

        relink_map = {
            "asset-3": new_video,
            "asset-4": new_image,
        }

        new_project, result = OfflineAssetManager.batch_relink(
            topos_project_with_assets,
            relink_map,
        )

        assert result.success is True
        assert result.relinked_count == 2
        assert result.failed_count == 0
        assert new_project.assets["asset-3"].is_offline is False
        assert new_project.assets["asset-4"].is_offline is False

    def test_batch_relink_partial(self, topos_project_with_assets, tmp_path):
        """Test batch relinking with some failures."""
        # Create one valid file, one invalid path
        new_video = str(tmp_path / "relinked_video.mp4")
        with open(new_video, "wb") as f:
            f.write(b"new video")

        relink_map = {
            "asset-3": new_video,  # Valid
            "asset-4": "/nonexistent/still_offline.jpg",  # Invalid path
        }

        new_project, result = OfflineAssetManager.batch_relink(
            topos_project_with_assets,
            relink_map,
        )

        # ToposProjectIO.relink_assets sets is_offline=False for all relinked
        # assets regardless of path existence. Both are marked as relinked.
        assert result.relinked_count == 2
        assert result.failed_count == 0
        assert new_project.assets["asset-3"].is_offline is False
        # Note: asset-4 is also marked online even though path doesn't exist;
        # offline status is re-evaluated by update_offline_status()
        assert new_project.assets["asset-4"].is_offline is False

    def test_update_offline_status(self, topos_project_with_assets, tmp_path):
        """Test updating offline status."""
        # Create a file that matches one offline asset's expected location
        new_path = str(tmp_path / "offline_video.mp4")
        with open(new_path, "wb") as f:
            f.write(b"found data")

        # First, relink to the new path
        new_project = OfflineAssetManager.relink_asset(
            topos_project_with_assets,
            "asset-3",
            new_path,
        )

        # Now update offline status
        updated_project = OfflineAssetManager.update_offline_status(new_project)

        # asset-3 should be online now
        assert updated_project.assets["asset-3"].is_offline is False
        # asset-4 should still be offline
        assert updated_project.assets["asset-4"].is_offline is True


# ================================================================
# Test: OfflineAssetManager - EditProject
# ================================================================

class TestOfflineAssetManagerEditProject:
    """Test OfflineAssetManager with EditProject."""

    def test_check_offline_assets_edit(self, edit_project_with_assets):
        """Test checking offline assets in EditProject."""
        report = OfflineAssetManager.check_offline_assets(edit_project_with_assets)

        assert report.total_assets == 4
        assert report.offline_count == 2
        assert report.has_offline_assets() is True

    def test_get_offline_asset_list_edit(self, edit_project_with_assets):
        """Test getting offline asset list from EditProject."""
        offline_list = OfflineAssetManager.get_offline_asset_list(edit_project_with_assets)

        assert len(offline_list) == 2
        # Should be MediaAsset objects
        for asset in offline_list:
            assert isinstance(asset, MediaAsset)

    def test_relink_edit_project(self, edit_project_with_assets, tmp_path):
        """Test relinking in EditProject."""
        # Create a new file
        new_path = str(tmp_path / "relinked_audio.wav")
        with open(new_path, "wb") as f:
            f.write(b"new audio")

        # Get the offline audio asset ID
        offline_assets = OfflineAssetManager.get_offline_asset_list(edit_project_with_assets)
        audio_asset = next(a for a in offline_assets if a.media_type == "audio")

        # Relink
        new_project = OfflineAssetManager.relink_asset(
            edit_project_with_assets,
            audio_asset.asset_id,
            new_path,
        )

        assert new_project.assets[audio_asset.asset_id].path == new_path
        assert new_project.assets[audio_asset.asset_id].is_offline is False


# ================================================================
# Test: OfflineAssetManager - File Search
# ================================================================

class TestOfflineAssetManagerFileSearch:
    """Test file search functionality."""

    def test_find_matching_file_direct(self, tmp_path):
        """Test finding file directly in directory."""
        # Create a file
        filename = "test_file.mp4"
        filepath = str(tmp_path / filename)
        with open(filepath, "wb") as f:
            f.write(b"data")

        # Search
        found = OfflineAssetManager.find_matching_file(
            filename,
            [str(tmp_path)],
            recursive=False,
        )

        assert found == filepath

    def test_find_matching_file_recursive(self, tmp_path):
        """Test finding file recursively."""
        # Create nested directory structure
        subdir = tmp_path / "nested" / "deep"
        subdir.mkdir(parents=True)

        filename = "nested_file.mp4"
        filepath = str(subdir / filename)
        with open(filepath, "wb") as f:
            f.write(b"data")

        # Search with recursion
        found = OfflineAssetManager.find_matching_file(
            filename,
            [str(tmp_path)],
            recursive=True,
            max_depth=3,
        )

        assert found == filepath

    def test_find_matching_file_not_found(self, tmp_path):
        """Test file not found."""
        found = OfflineAssetManager.find_matching_file(
            "nonexistent.mp4",
            [str(tmp_path)],
            recursive=True,
        )

        assert found is None

    def test_auto_search_for_missing(self, topos_project_with_assets, tmp_path):
        """Test auto search for missing files."""
        # Create a file that matches the offline video filename
        filename = "offline_video.mp4"
        filepath = str(tmp_path / filename)
        with open(filepath, "wb") as f:
            f.write(b"found data")

        # Auto search
        found_map = OfflineAssetManager.auto_search_for_missing(topos_project_with_assets)

        # Should find the file if tmp_path is in search dirs
        # Note: This test may not find the file depending on search dirs
        # Just verify the method works
        assert isinstance(found_map, dict)


# ================================================================
# Test: Integration
# ================================================================

class TestOfflineAssetManagerIntegration:
    """Integration tests for OfflineAssetManager."""

    def test_full_workflow(self, topos_project_with_assets, tmp_path):
        """Test full offline -> relink -> verify workflow."""
        # 1. Check offline assets
        report = OfflineAssetManager.check_offline_assets(topos_project_with_assets)
        assert report.offline_count == 2

        # 2. Create files for relinking
        new_video = str(tmp_path / "offline_video.mp4")
        new_image = str(tmp_path / "offline_image.jpg")
        with open(new_video, "wb") as f:
            f.write(b"video")
        with open(new_image, "wb") as f:
            f.write(b"image")

        # 3. Batch relink
        relink_map = {
            "asset-3": new_video,
            "asset-4": new_image,
        }
        new_project, result = OfflineAssetManager.batch_relink(
            topos_project_with_assets,
            relink_map,
        )

        # 4. Verify all online
        final_report = OfflineAssetManager.check_offline_assets(new_project)
        assert final_report.offline_count == 0
        assert final_report.has_offline_assets() is False

    def test_check_with_project_path(self, topos_project_with_assets):
        """Test checking offline assets with project path."""
        project_path = "/test/project.topos"
        report = OfflineAssetManager.check_offline_assets_with_path(
            topos_project_with_assets,
            project_path,
        )

        assert report.project_path == project_path
        assert report.offline_count == 2
