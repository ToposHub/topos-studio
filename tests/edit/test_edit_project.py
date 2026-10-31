"""
EditProject Data Model Tests
EditProject 数据模型测试。

测试范围：
- MediaAsset 创建和验证
- ProjectBin 创建和操作
- TimelineSequenceRef 创建和验证
- EditProject 创建和操作
- 素材导入（去重）
- Bin 管理（创建、移动素材）
- Timeline 管理（创建、设置活动）
"""

from __future__ import annotations

import pytest
import time
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    MediaAsset,
    ProjectBin,
    EditProject,
    TimelineSequenceRef,
    batch_relink_assets,
    relink_asset,
    update_asset_proxy,
)


# ================================================================
# MediaAsset Tests
# ================================================================

class TestMediaAsset:
    """MediaAsset 数据模型测试"""

    def test_create_asset_with_required_fields(self) -> None:
        """测试创建素材（必填字段）"""
        asset = MediaAsset(
            asset_id=str(uuid.uuid4()),
            path="/path/to/video.mp4",
            media_type="video",
            display_name="video.mp4",
            metadata={"width": 1920, "height": 1080},
        )
        assert asset.asset_id is not None
        assert asset.path == "/path/to/video.mp4"
        assert asset.media_type == "video"
        assert asset.display_name == "video.mp4"
        assert asset.metadata["width"] == 1920

    def test_create_asset_with_all_fields(self) -> None:
        """测试创建素材（完整字段）"""
        now = time.time()
        asset = MediaAsset(
            asset_id="test-asset-id",
            path="/path/to/image.jpg",
            media_type="image",
            display_name="image.jpg",
            metadata={"width": 800, "height": 600},
            thumbnail_key="thumb-key-123",
            proxy_status="ready",
            color_metadata={"white_balance": 6500},
            created_at=now,
        )
        assert asset.thumbnail_key == "thumb-key-123"
        assert asset.proxy_status == "ready"
        assert asset.color_metadata["white_balance"] == 6500
        assert asset.created_at == now

    def test_asset_validation_empty_id(self) -> None:
        """测试验证：空 asset_id"""
        with pytest.raises(ValueError, match="asset_id cannot be empty"):
            MediaAsset(
                asset_id="",
                path="/path/to/file.mp4",
                media_type="video",
                display_name="file.mp4",
                metadata={},
            )

    def test_asset_validation_empty_path(self) -> None:
        """测试验证：空 path"""
        with pytest.raises(ValueError, match="path cannot be empty"):
            MediaAsset(
                asset_id=str(uuid.uuid4()),
                path="",
                media_type="video",
                display_name="file.mp4",
                metadata={},
            )

    def test_asset_validation_invalid_media_type(self) -> None:
        """测试验证：无效 media_type"""
        with pytest.raises(ValueError, match="Invalid media_type"):
            MediaAsset(
                asset_id=str(uuid.uuid4()),
                path="/path/to/file.txt",
                media_type="document",  # 无效类型
                display_name="file.txt",
                metadata={},
            )

    def test_asset_validation_invalid_proxy_status(self) -> None:
        """测试验证：无效 proxy_status"""
        with pytest.raises(ValueError, match="Invalid proxy_status"):
            MediaAsset(
                asset_id=str(uuid.uuid4()),
                path="/path/to/file.mp4",
                media_type="video",
                display_name="file.mp4",
                metadata={},
                proxy_status="invalid_status",  # 无效状态
            )

    def test_asset_type_properties(self) -> None:
        """测试素材类型属性"""
        video_asset = MediaAsset(
            asset_id="video-1",
            path="/video.mp4",
            media_type="video",
            display_name="video.mp4",
            metadata={"fps": 24.0, "total_frames": 1000},
        )
        assert video_asset.is_video is True
        assert video_asset.is_image is False
        assert video_asset.is_audio is False
        assert video_asset.fps == 24.0
        assert video_asset.total_frames == 1000

        image_asset = MediaAsset(
            asset_id="image-1",
            path="/image.jpg",
            media_type="image",
            display_name="image.jpg",
            metadata={"width": 800, "height": 600},
        )
        assert image_asset.is_image is True
        assert image_asset.is_video is False
        assert image_asset.fps is None

    def test_asset_resolution_str(self) -> None:
        """测试分辨率字符串"""
        asset = MediaAsset(
            asset_id="test",
            path="/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={"width": 1920, "height": 1080},
        )
        assert asset.resolution_str == "1920x1080"

        # 无分辨率信息
        asset_no_res = MediaAsset(
            asset_id="test2",
            path="/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
        )
        assert asset_no_res.resolution_str == "-"

    def test_asset_with_display_name(self) -> None:
        """测试修改显示名称"""
        asset = MediaAsset(
            asset_id="test",
            path="/file.mp4",
            media_type="video",
            display_name="original.mp4",
            metadata={},
        )
        new_asset = asset.with_display_name("renamed.mp4")
        assert new_asset.display_name == "renamed.mp4"
        assert new_asset.asset_id == asset.asset_id  # ID 不变

    def test_asset_with_proxy_status(self) -> None:
        """测试修改代理状态"""
        asset = MediaAsset(
            asset_id="test",
            path="/file.mp4",
            media_type="video",
            display_name="file.mp4",
            metadata={},
            proxy_status="none",
        )
        new_asset = asset.with_proxy_status("ready")
        assert new_asset.proxy_status == "ready"

    def test_asset_equality_and_hash(self) -> None:
        """测试相等比较和哈希"""
        asset1 = MediaAsset(
            asset_id="same-id",
            path="/path1.mp4",
            media_type="video",
            display_name="path1.mp4",
            metadata={},
        )
        asset2 = MediaAsset(
            asset_id="same-id",
            path="/path2.mp4",  # 不同路径
            media_type="video",
            display_name="path2.mp4",
            metadata={},
        )
        # 基于 ID 比较
        assert asset1 == asset2
        assert hash(asset1) == hash(asset2)


# ================================================================
# ProjectBin Tests
# ================================================================

class TestProjectBin:
    """ProjectBin 数据模型测试"""

    def test_create_bin_with_required_fields(self) -> None:
        """测试创建素材箱（必填字段）"""
        bin_obj = ProjectBin(
            bin_id=str(uuid.uuid4()),
            name="Footage",
            parent_id=None,
        )
        assert bin_obj.bin_id is not None
        assert bin_obj.name == "Footage"
        assert bin_obj.parent_id is None
        assert bin_obj.asset_ids == ()

    def test_create_bin_with_assets(self) -> None:
        """测试创建带素材的素材箱"""
        bin_obj = ProjectBin(
            bin_id="bin-1",
            name="Footage",
            parent_id=None,
            asset_ids=("asset-1", "asset-2"),
        )
        assert len(bin_obj.asset_ids) == 2
        assert "asset-1" in bin_obj.asset_ids

    def test_bin_validation_empty_id(self) -> None:
        """测试验证：空 bin_id"""
        with pytest.raises(ValueError, match="bin_id cannot be empty"):
            ProjectBin(
                bin_id="",
                name="Footage",
                parent_id=None,
            )

    def test_bin_validation_empty_name(self) -> None:
        """测试验证：空 name"""
        with pytest.raises(ValueError, match="name cannot be empty"):
            ProjectBin(
                bin_id=str(uuid.uuid4()),
                name="",
                parent_id=None,
            )

    def test_bin_with_asset_added(self) -> None:
        """测试添加素材"""
        bin_obj = ProjectBin(
            bin_id="bin-1",
            name="Footage",
            parent_id=None,
            asset_ids=("asset-1",),
        )
        new_bin = bin_obj.with_asset_added("asset-2")
        assert len(new_bin.asset_ids) == 2
        assert "asset-2" in new_bin.asset_ids

        # 重复添加不生效
        same_bin = bin_obj.with_asset_added("asset-1")
        assert same_bin.asset_ids == bin_obj.asset_ids

    def test_bin_with_asset_removed(self) -> None:
        """测试移除素材"""
        bin_obj = ProjectBin(
            bin_id="bin-1",
            name="Footage",
            parent_id=None,
            asset_ids=("asset-1", "asset-2"),
        )
        new_bin = bin_obj.with_asset_removed("asset-1")
        assert len(new_bin.asset_ids) == 1
        assert "asset-1" not in new_bin.asset_ids

    def test_bin_with_timeline_added(self) -> None:
        """测试添加时间线引用"""
        bin_obj = ProjectBin(
            bin_id="bin-1",
            name="Timelines",
            parent_id=None,
        )
        new_bin = bin_obj.with_timeline_added("timeline-1")
        assert len(new_bin.timeline_ids) == 1
        assert "timeline-1" in new_bin.timeline_ids

    def test_bin_equality_and_hash(self) -> None:
        """测试相等比较和哈希"""
        bin1 = ProjectBin(bin_id="same-id", name="Bin1", parent_id=None)
        bin2 = ProjectBin(bin_id="same-id", name="Bin2", parent_id=None)
        assert bin1 == bin2
        assert hash(bin1) == hash(bin2)


# ================================================================
# TimelineSequenceRef Tests
# ================================================================

class TestTimelineSequenceRef:
    """TimelineSequenceRef 数据模型测试"""

    def test_create_timeline_ref_with_defaults(self) -> None:
        """测试创建时间线引用（默认值）"""
        timeline = TimelineSequenceRef(
            timeline_id=str(uuid.uuid4()),
            name="Timeline 1",
        )
        assert timeline.timeline_id is not None
        assert timeline.name == "Timeline 1"
        assert timeline.fps == 24.0
        assert timeline.width == 1920
        assert timeline.height == 1080
        assert timeline.sample_rate == 48000

    def test_create_timeline_ref_with_custom_settings(self) -> None:
        """测试创建时间线引用（自定义设置）"""
        timeline = TimelineSequenceRef(
            timeline_id="timeline-1",
            name="4K Timeline",
            fps=23.976,
            width=3840,
            height=2160,
            sample_rate=96000,
        )
        assert timeline.fps == 23.976
        assert timeline.width == 3840
        assert timeline.height == 2160
        assert timeline.sample_rate == 96000

    def test_timeline_ref_validation(self) -> None:
        """测试时间线引用验证"""
        # 空 timeline_id
        with pytest.raises(ValueError, match="timeline_id cannot be empty"):
            TimelineSequenceRef(timeline_id="", name="Timeline")

        # 空 name
        with pytest.raises(ValueError, match="name cannot be empty"):
            TimelineSequenceRef(timeline_id="id", name="")

        # 无效分辨率
        with pytest.raises(ValueError, match="Invalid resolution"):
            TimelineSequenceRef(timeline_id="id", name="Timeline", width=0, height=1080)

        # 无效帧率
        with pytest.raises(ValueError, match="Invalid fps"):
            TimelineSequenceRef(timeline_id="id", name="Timeline", fps=0)

    def test_timeline_ref_with_name(self) -> None:
        """测试修改名称"""
        timeline = TimelineSequenceRef(
            timeline_id="id",
            name="Original",
        )
        new_timeline = timeline.with_name("Renamed")
        assert new_timeline.name == "Renamed"
        assert new_timeline.timeline_id == timeline.timeline_id

    def test_timeline_ref_with_resolution(self) -> None:
        """测试修改分辨率"""
        timeline = TimelineSequenceRef(
            timeline_id="id",
            name="Timeline",
            width=1920,
            height=1080,
        )
        new_timeline = timeline.with_resolution(3840, 2160)
        assert new_timeline.width == 3840
        assert new_timeline.height == 2160

    def test_timeline_ref_with_fps(self) -> None:
        """测试修改帧率"""
        timeline = TimelineSequenceRef(
            timeline_id="id",
            name="Timeline",
            fps=24.0,
        )
        new_timeline = timeline.with_fps(29.97)
        assert new_timeline.fps == 29.97


# ================================================================
# EditProject Tests
# ================================================================

class TestEditProject:
    """EditProject 数据模型测试"""

    def test_create_project(self) -> None:
        """测试创建项目"""
        project = EditProject.create("Test Project")
        assert project.project_id is not None
        assert project.name == "Test Project"
        assert project.root_bin_id is not None
        assert project.root_bin is not None
        assert project.asset_count == 0
        assert project.bin_count == 1  # 只有 root bin
        assert project.timeline_count == 0

    def test_project_validation_empty_id(self) -> None:
        """测试验证：空 project_id"""
        with pytest.raises(ValueError, match="project_id cannot be empty"):
            EditProject(
                project_id="",
                name="Test",
            )

    def test_project_validation_empty_name(self) -> None:
        """测试验证：空 name"""
        with pytest.raises(ValueError, match="name cannot be empty"):
            EditProject(
                project_id=str(uuid.uuid4()),
                name="",
            )

    def test_import_asset(self) -> None:
        """测试导入素材"""
        project = EditProject.create("Test")
        new_project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        assert new_project.asset_count == 1
        # 素材已添加到 root bin
        root_bin = new_project.root_bin
        assert root_bin is not None
        assert len(root_bin.asset_ids) == 1

    def test_import_asset_duplicate(self) -> None:
        """测试导入素材去重"""
        project = EditProject.create("Test")
        # 第一次导入
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        assert project.asset_count == 1

        # 第二次导入同一路径
        project2 = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        # 应该返回同一实例（不重复导入）
        assert project2.asset_count == 1
        assert project2.project_id == project.project_id

    def test_get_asset_by_path(self) -> None:
        """测试根据路径查找素材"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        asset = project.get_asset_by_path("/path/to/video.mp4")
        assert asset is not None
        assert asset.path == "/path/to/video.mp4"

        # 不存在的路径
        assert project.get_asset_by_path("/nonexistent.mp4") is None

    def test_has_asset_path(self) -> None:
        """测试路径已导入检查"""
        project = EditProject.create("Test")
        assert not project.has_asset_path("/path/to/video.mp4")

        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        assert project.has_asset_path("/path/to/video.mp4")

    def test_remove_asset(self) -> None:
        """测试移除素材"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        assert project.asset_count == 1

        asset_id = list(project.assets.keys())[0]
        new_project = project.remove_asset(asset_id)
        assert new_project.asset_count == 0
        # 素材应从 root bin 移除
        root_bin = new_project.root_bin
        assert root_bin is not None
        assert len(root_bin.asset_ids) == 0

    def test_asset_path_and_proxy_updates_preserve_project_regions(self) -> None:
        """素材重连和代理更新不应丢失项目中的合成、调色等区域。"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        asset_id = next(iter(project.assets))
        project = EditProject(
            project_id=project.project_id,
            name=project.name,
            assets=project.assets,
            bins=project.bins,
            timelines=project.timelines,
            compositions={"comp-1": {"nodes": []}},
            color_stacks={"stack-1": object()},
            comp_clips={"clip-1": {"name": "Comp Clip"}},
            render_cache_manifests={"cache-1": {"asset_id": asset_id}},
            render_cache_settings={"enabled": True},
            root_bin_id=project.root_bin_id,
            active_timeline_id=project.active_timeline_id,
            active_comp_clip_id="clip-1",
            created_at=project.created_at,
            modified_at=project.modified_at,
        )

        updated_projects = [
            relink_asset(project, asset_id, "/path/to/relinked.mov"),
            batch_relink_assets(project, {asset_id: "/path/to/batch-relinked.mov"}),
            update_asset_proxy(project, asset_id, "/path/to/proxy.mov", "ready"),
        ]

        for updated in updated_projects:
            assert updated.compositions == project.compositions
            assert updated.color_stacks == project.color_stacks
            assert updated.comp_clips == project.comp_clips
            assert updated.render_cache_manifests == project.render_cache_manifests
            assert updated.render_cache_settings == project.render_cache_settings
            assert updated.active_comp_clip_id == project.active_comp_clip_id

    def test_create_bin(self) -> None:
        """测试创建素材箱"""
        project = EditProject.create("Test")
        new_project = project.create_bin("Footage")
        assert new_project.bin_count == 2  # root + Footage

        footage_bin = new_project.get_bin_by_id(
            [id for id in new_project.bins.keys() if id != new_project.root_bin_id][0]
        )
        assert footage_bin is not None
        assert footage_bin.name == "Footage"

    def test_move_asset_to_bin(self) -> None:
        """测试移动素材到素材箱"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        project = project.create_bin("Footage")

        asset_id = list(project.assets.keys())[0]
        bin_id = [id for id in project.bins.keys() if id != project.root_bin_id][0]

        new_project = project.move_asset_to_bin(asset_id, bin_id)
        footage_bin = new_project.get_bin_by_id(bin_id)
        assert footage_bin is not None
        assert asset_id in footage_bin.asset_ids

    def test_move_asset_to_bin_errors(self) -> None:
        """测试移动素材错误情况"""
        project = EditProject.create("Test")
        project = project.create_bin("Footage")

        # 不存在的素材
        with pytest.raises(ValueError, match="Asset not found"):
            project.move_asset_to_bin("nonexistent-asset", project.root_bin_id)

        # 不存在的素材箱
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        asset_id = list(project.assets.keys())[0]
        with pytest.raises(ValueError, match="Bin not found"):
            project.move_asset_to_bin(asset_id, "nonexistent-bin")

    def test_remove_bin(self) -> None:
        """测试移除素材箱"""
        project = EditProject.create("Test")
        project = project.create_bin("Footage")
        assert project.bin_count == 2

        bin_id = [id for id in project.bins.keys() if id != project.root_bin_id][0]
        new_project = project.remove_bin(bin_id)
        assert new_project.bin_count == 1

    def test_remove_root_bin_error(self) -> None:
        """测试移除 root bin 错误"""
        project = EditProject.create("Test")
        with pytest.raises(ValueError, match="Cannot remove root bin"):
            project.remove_bin(project.root_bin_id)

    def test_rename_bin(self) -> None:
        """测试重命名素材箱"""
        project = EditProject.create("Test")
        project = project.create_bin("Original Name")
        assert project.bin_count == 2

        bin_id = [id for id in project.bins.keys() if id != project.root_bin_id][0]

        # Rename
        new_project = project.rename_bin(bin_id, "New Name")
        renamed_bin = new_project.bins.get(bin_id)
        assert renamed_bin is not None
        assert renamed_bin.name == "New Name"

    def test_rename_nonexistent_bin_error(self) -> None:
        """测试重命名不存在的素材箱"""
        project = EditProject.create("Test")
        with pytest.raises(ValueError, match="Bin not found"):
            project.rename_bin("nonexistent-bin", "New Name")

    def test_create_timeline(self) -> None:
        """测试创建时间线"""
        project = EditProject.create("Test")
        new_project = project.create_timeline("Timeline 1", fps=24.0, width=1920, height=1080)
        assert new_project.timeline_count == 1
        assert new_project.active_timeline_id is not None

        timeline = new_project.active_timeline
        assert timeline is not None
        assert timeline.name == "Timeline 1"

    def test_create_timeline_custom_settings(self) -> None:
        """测试创建时间线（自定义设置）"""
        project = EditProject.create("Test")
        new_project = project.create_timeline(
            name="4K Timeline",
            fps=23.976,
            width=3840,
            height=2160,
        )
        timeline = new_project.active_timeline
        assert timeline is not None
        assert timeline.fps == 23.976
        assert timeline.width == 3840
        assert timeline.height == 2160

    def test_set_active_timeline(self) -> None:
        """测试设置活动时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("Timeline 1")
        project = project.create_timeline("Timeline 2")

        timeline_ids = list(project.timelines.keys())
        first_timeline_id = timeline_ids[0]
        second_timeline_id = timeline_ids[1]

        # 应该最后一个创建的是活动时间线
        assert project.active_timeline_id == second_timeline_id

        # 切换到第一个
        new_project = project.set_active_timeline(first_timeline_id)
        assert new_project.active_timeline_id == first_timeline_id

        # 清除活动时间线
        cleared_project = new_project.set_active_timeline(None)
        assert cleared_project.active_timeline_id is None

    def test_set_active_timeline_error(self) -> None:
        """测试设置不存在的活动时间线"""
        project = EditProject.create("Test")
        with pytest.raises(ValueError, match="Timeline not found"):
            project.set_active_timeline("nonexistent-timeline")

    def test_remove_timeline(self) -> None:
        """测试移除时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("Timeline 1")
        project = project.create_timeline("Timeline 2")

        timeline_ids = list(project.timelines.keys())
        first_timeline_id = timeline_ids[0]

        # 移除第一个时间线
        new_project = project.remove_timeline(first_timeline_id)
        assert new_project.timeline_count == 1

        # 活动时间线应该不变（移除的不是活动时间线）
        assert new_project.active_timeline_id == timeline_ids[1]

    def test_remove_active_timeline(self) -> None:
        """测试移除活动时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("Timeline 1")
        active_id = project.active_timeline_id

        new_project = project.remove_timeline(active_id)
        assert new_project.timeline_count == 0
        assert new_project.active_timeline_id is None

    def test_project_asset_type_filters(self) -> None:
        """测试素材类型过滤"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/video.mp4",
            media_type="video",
            metadata={},
        )
        project = project.import_asset(
            path="/image.jpg",
            media_type="image",
            metadata={},
        )
        project = project.import_asset(
            path="/audio.wav",
            media_type="audio",
            metadata={},
        )

        assert len(project.video_assets) == 1
        assert len(project.image_assets) == 1
        assert len(project.audio_assets) == 1

    def test_project_equality_and_hash(self) -> None:
        """测试项目相等比较"""
        project1 = EditProject.create("Test")
        project2 = EditProject(project_id=project1.project_id, name="Different Name")
        assert project1 == project2
        assert hash(project1) == hash(project2)


# ================================================================
# Color Stack Tests
# ================================================================

class TestColorStackData:
    """ColorStackData 数据模型测试"""

    def test_update_color_stack_preserves_full_color_payload(self) -> None:
        """更新调色栈时应保存 curves/HSL/mixer/soft clip/LUT。"""
        project = EditProject.create("Color Test")
        project, stack_id = project.create_color_stack(asset_id="asset-1")

        updated = project.update_color_stack(
            stack_id=stack_id,
            grading_params={"exposure": 0.5, "contrast": 12},
            curves_data={"rgb": [[0.0, 0.0], [1.0, 1.0]]},
            hsl_curve_data={"red": {"hue": 0.1}},
            mixer_params={"red": [1.0, 0.0, 0.0]},
            soft_clip_params={"enabled": True, "high": 0.95},
            lut_path="/looks/test.cube",
            lut_intensity=0.75,
        )

        stack = updated.get_color_stack_by_id(stack_id)
        assert stack is not None
        assert stack.grading_params["exposure"] == 0.5
        assert stack.curves_data["rgb"][1] == [1.0, 1.0]
        assert stack.hsl_curve_data["red"]["hue"] == 0.1
        assert stack.mixer_params["red"] == [1.0, 0.0, 0.0]
        assert stack.soft_clip_params["enabled"] is True
        assert stack.lut_path == "/looks/test.cube"
        assert stack.lut_intensity == 0.75


# ================================================================
# Integration Tests
# ================================================================

class TestEditProjectIntegration:
    """EditProject 集成测试"""

    def test_full_workflow(self) -> None:
        """测试完整工作流：导入 -> 创建 bin -> 创建 timeline"""
        # 创建项目
        project = EditProject.create("My Project")
        assert project.name == "My Project"

        # 导入素材
        project = project.import_asset(
            path="/path/to/clip1.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
            display_name="Clip 1",
        )
        project = project.import_asset(
            path="/path/to/clip2.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
            display_name="Clip 2",
        )
        assert project.asset_count == 2

        # 创建素材箱
        project = project.create_bin("Main Footage")

        # 移动素材到新素材箱
        asset_ids = list(project.assets.keys())
        bin_ids = [id for id in project.bins.keys() if id != project.root_bin_id]
        for asset_id in asset_ids:
            project = project.move_asset_to_bin(asset_id, bin_ids[0])

        # 创建时间线
        project = project.create_timeline("Sequence 01")
        assert project.timeline_count == 1
        assert project.active_timeline_id is not None

        # 验证最终状态
        assert project.asset_count == 2
        assert project.bin_count == 2
        assert project.timeline_count == 1

    def test_multiple_timelines(self) -> None:
        """测试多个时间线管理"""
        project = EditProject.create("Test")

        # 创建多个时间线
        project = project.create_timeline("Timeline A")
        timeline_a_id = project.active_timeline_id
        project = project.create_timeline("Timeline B")
        timeline_b_id = project.active_timeline_id

        assert project.timeline_count == 2
        assert project.active_timeline_id == timeline_b_id

        # 切换活动时间线
        project = project.set_active_timeline(timeline_a_id)
        assert project.active_timeline_id == timeline_a_id

        # 获取时间线
        timeline_a = project.get_timeline_by_id(timeline_a_id)
        assert timeline_a is not None
        assert timeline_a.name == "Timeline A"
