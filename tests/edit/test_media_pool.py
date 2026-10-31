"""
Media Pool Tests
素材池测试。

测试范围：
- 素材导入（去重）
- Bin 管理（创建、移动素材）
- Timeline 管理
- 序列化 roundtrip
"""

from __future__ import annotations

import pytest
import tempfile
import os
from pathlib import Path

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    MediaAsset,
    ProjectBin,
    EditProject,
    TimelineSequenceRef,
    get_media_type,
)
from src.features.edit.core.serialization import EditProjectSerializer


# ================================================================
# Asset Import Tests
# ================================================================

class TestAssetImport:
    """素材导入测试"""

    def test_import_single_asset(self) -> None:
        """测试导入单个素材"""
        project = EditProject.create("Test Project")
        new_project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        
        assert new_project.asset_count == 1
        asset = list(new_project.assets.values())[0]
        assert asset.path == "/path/to/video.mp4"
        assert asset.media_type == "video"

    def test_import_multiple_assets(self) -> None:
        """测试导入多个素材"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video1.mp4",
            media_type="video",
            metadata={},
        )
        project = project.import_asset(
            path="/path/to/video2.mp4",
            media_type="video",
            metadata={},
        )
        project = project.import_asset(
            path="/path/to/image.jpg",
            media_type="image",
            metadata={},
        )
        
        assert project.asset_count == 3
        assert len(project.video_assets) == 2
        assert len(project.image_assets) == 1

    def test_import_duplicate_path(self) -> None:
        """测试导入重复路径（去重）"""
        project = EditProject.create("Test")
        
        # 第一次导入
        project1 = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920},
        )
        assert project1.asset_count == 1
        
        # 第二次导入同一路径（应该去重）
        project2 = project1.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920},  # 即使 metadata 相同
        )
        
        # 应返回同一实例（不重复导入）
        assert project2.asset_count == 1
        assert project2 == project1  # project_id 相同

    def test_import_with_display_name(self) -> None:
        """测试导入素材（自定义显示名称）"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
            display_name="My Custom Name",
        )
        
        asset = list(project.assets.values())[0]
        assert asset.display_name == "My Custom Name"

    def test_import_with_thumbnail_key(self) -> None:
        """测试导入素材（缩略图键）"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
            thumbnail_key="thumb_abc123",
        )
        
        asset = list(project.assets.values())[0]
        assert asset.thumbnail_key == "thumb_abc123"

    def test_import_to_root_bin(self) -> None:
        """测试素材导入到 root bin"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        
        root_bin = project.root_bin
        assert root_bin is not None
        assert len(root_bin.asset_ids) == 1


# ================================================================
# Bin Management Tests
# ================================================================

class TestBinManagement:
    """Bin 管理测试"""

    def test_create_bin(self) -> None:
        """测试创建素材箱"""
        project = EditProject.create("Test")
        project = project.create_bin("Footage")
        
        assert project.bin_count == 2  # root + Footage
        
        # 找到新创建的 bin
        new_bin_id = None
        for bin_id, bin_obj in project.bins.items():
            if bin_obj.name == "Footage":
                new_bin_id = bin_id
                break
        assert new_bin_id is not None

    def test_create_multiple_bins(self) -> None:
        """测试创建多个素材箱"""
        project = EditProject.create("Test")
        project = project.create_bin("Video")
        project = project.create_bin("Audio")
        project = project.create_bin("Images")
        
        assert project.bin_count == 4  # root + 3 bins
        
        bin_names = [bin_obj.name for bin_obj in project.bins.values()]
        assert "Video" in bin_names
        assert "Audio" in bin_names
        assert "Images" in bin_names

    def test_move_asset_between_bins(self) -> None:
        """测试在素材箱之间移动素材"""
        project = EditProject.create("Test")
        
        # 导入素材
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        asset_id = list(project.assets.keys())[0]
        
        # 创建新素材箱
        project = project.create_bin("Organized")
        target_bin_id = None
        for bin_id, bin_obj in project.bins.items():
            if bin_obj.name == "Organized":
                target_bin_id = bin_id
                break
        assert target_bin_id is not None
        
        # 移动素材
        project = project.move_asset_to_bin(asset_id, target_bin_id)
        
        # 验证素材在新素材箱中
        target_bin = project.get_bin_by_id(target_bin_id)
        assert target_bin is not None
        assert asset_id in target_bin.asset_ids

    def test_remove_bin(self) -> None:
        """测试移除素材箱"""
        project = EditProject.create("Test")
        project = project.create_bin("Temp")
        
        # 找到要移除的 bin
        temp_bin_id = None
        for bin_id, bin_obj in project.bins.items():
            if bin_obj.name == "Temp":
                temp_bin_id = bin_id
                break
        
        assert temp_bin_id is not None
        project = project.remove_bin(temp_bin_id)
        assert project.bin_count == 1  # 只有 root

    def test_bin_can_contain_multiple_assets(self) -> None:
        """测试素材箱可以包含多个素材"""
        project = EditProject.create("Test")
        
        # 导入多个素材
        project = project.import_asset(
            path="/path/to/video1.mp4",
            media_type="video",
            metadata={},
        )
        project = project.import_asset(
            path="/path/to/video2.mp4",
            media_type="video",
            metadata={},
        )
        
        # 创建素材箱并移动素材
        project = project.create_bin("All Videos")
        target_bin_id = None
        for bin_id, bin_obj in project.bins.items():
            if bin_obj.name == "All Videos":
                target_bin_id = bin_id
                break
        
        for asset_id in project.assets.keys():
            project = project.move_asset_to_bin(asset_id, target_bin_id)
        
        target_bin = project.get_bin_by_id(target_bin_id)
        assert target_bin is not None
        assert len(target_bin.asset_ids) == 2


# ================================================================
# Timeline Management Tests
# ================================================================

class TestTimelineManagement:
    """Timeline 管理测试"""

    def test_create_timeline_default_settings(self) -> None:
        """测试创建时间线（默认设置）"""
        project = EditProject.create("Test")
        project = project.create_timeline("Sequence 01")
        
        assert project.timeline_count == 1
        timeline = project.active_timeline
        assert timeline is not None
        assert timeline.name == "Sequence 01"
        assert timeline.fps == 24.0
        assert timeline.width == 1920
        assert timeline.height == 1080

    def test_create_timeline_custom_settings(self) -> None:
        """测试创建时间线（自定义设置）"""
        project = EditProject.create("Test")
        project = project.create_timeline(
            name="4K Sequence",
            fps=23.976,
            width=3840,
            height=2160,
        )
        
        timeline = project.active_timeline
        assert timeline is not None
        assert timeline.fps == 23.976
        assert timeline.width == 3840
        assert timeline.height == 2160

    def test_create_multiple_timelines(self) -> None:
        """测试创建多个时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("Timeline 1")
        project = project.create_timeline("Timeline 2")
        project = project.create_timeline("Timeline 3")
        
        assert project.timeline_count == 3
        # 最新创建的是活动时间线
        assert project.active_timeline.name == "Timeline 3"

    def test_switch_active_timeline(self) -> None:
        """测试切换活动时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("First")
        first_id = project.active_timeline_id
        project = project.create_timeline("Second")
        
        # 当前活动是 Second
        assert project.active_timeline.name == "Second"
        
        # 切换到 First
        project = project.set_active_timeline(first_id)
        assert project.active_timeline.name == "First"

    def test_remove_timeline(self) -> None:
        """测试移除时间线"""
        project = EditProject.create("Test")
        project = project.create_timeline("To Remove")
        timeline_id = project.active_timeline_id
        
        # 创建另一个时间线
        project = project.create_timeline("Keep This")
        
        # 移除第一个时间线
        project = project.remove_timeline(timeline_id)
        assert project.timeline_count == 1
        assert "To Remove" not in [t.name for t in project.timelines.values()]

    def test_timeline_references_in_bin(self) -> None:
        """测试时间线引用在素材箱中"""
        project = EditProject.create("Test")
        project = project.create_timeline("Sequence 01")
        timeline_id = project.active_timeline_id
        
        # 将时间线添加到素材箱
        project = project.create_bin("Timelines")
        bin_id = None
        for b_id, bin_obj in project.bins.items():
            if bin_obj.name == "Timelines":
                bin_id = b_id
                break
        
        # 使用 bin 的 with_timeline_added
        # 注意：EditProject 不直接支持这个，需要通过 ProjectBin
        target_bin = project.get_bin_by_id(bin_id)
        if target_bin:
            new_bin = target_bin.with_timeline_added(timeline_id)
            # 手动更新 bins 字典（实际应用中需要 EditProject 支持）
            # 这里只是测试 ProjectBin 的功能
            assert timeline_id in new_bin.timeline_ids


# ================================================================
# Serialization Tests
# ================================================================

class TestSerialization:
    """序列化测试"""

    def test_serialize_empty_project(self) -> None:
        """测试序列化空项目"""
        project = EditProject.create("Empty Project")
        json_str = EditProjectSerializer.to_json(project)
        
        assert "Empty Project" in json_str
        assert "version" in json_str
        assert "project_type" in json_str

    def test_serialize_project_with_assets(self) -> None:
        """测试序列化带素材的项目"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080},
        )
        
        json_str = EditProjectSerializer.to_json(project)
        assert "/path/to/video.mp4" in json_str
        assert "video" in json_str

    def test_serialize_project_with_bins(self) -> None:
        """测试序列化带素材箱的项目"""
        project = EditProject.create("Test")
        project = project.create_bin("Footage")
        
        json_str = EditProjectSerializer.to_json(project)
        assert "Footage" in json_str

    def test_serialize_project_with_timelines(self) -> None:
        """测试序列化带时间线的项目"""
        project = EditProject.create("Test")
        project = project.create_timeline("Sequence 01", fps=24.0)
        
        json_str = EditProjectSerializer.to_json(project)
        assert "Sequence 01" in json_str
        assert "24.0" in json_str

    def test_deserialize_empty_project(self) -> None:
        """测试反序列化空项目"""
        project = EditProject.create("Empty")
        json_str = EditProjectSerializer.to_json(project)
        
        restored = EditProjectSerializer.from_json(json_str)
        assert restored.name == "Empty"
        assert restored.project_id == project.project_id
        assert restored.asset_count == 0

    def test_deserialize_project_with_assets(self) -> None:
        """测试反序列化带素材的项目"""
        project = EditProject.create("Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080},
        )
        
        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)
        
        assert restored.asset_count == 1
        restored_asset = list(restored.assets.values())[0]
        assert restored_asset.path == "/path/to/video.mp4"
        assert restored_asset.media_type == "video"

    def test_roundtrip_validation(self) -> None:
        """测试 roundtrip 验证"""
        project = EditProject.create("Roundtrip Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={"fps": 24.0},
        )
        project = project.create_bin("Footage")
        project = project.create_timeline("Sequence 01")
        
        # 验证 roundtrip
        assert EditProjectSerializer.validate_roundtrip(project)

    def test_serialize_to_file(self) -> None:
        """测试保存到文件"""
        project = EditProject.create("File Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test_project.topos"
            
            success = EditProjectSerializer.save_to_file(project, filepath)
            assert success
            assert filepath.exists()

    def test_load_from_file(self) -> None:
        """测试从文件加载"""
        project = EditProject.create("File Test")
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test_project.topos"
            
            # 保存
            EditProjectSerializer.save_to_file(project, filepath)
            
            # 加载
            restored = EditProjectSerializer.load_from_file(filepath)
            assert restored is not None
            assert restored.name == "File Test"
            assert restored.asset_count == 1

    def test_full_roundtrip(self) -> None:
        """测试完整 roundtrip（复杂项目）"""
        # 创建复杂项目
        project = EditProject.create("Complex Project")
        
        # 导入多个素材
        project = project.import_asset(
            path="/videos/clip1.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        project = project.import_asset(
            path="/videos/clip2.mp4",
            media_type="video",
            metadata={"width": 1920, "height": 1080, "fps": 24.0},
        )
        project = project.import_asset(
            path="/images/photo.jpg",
            media_type="image",
            metadata={"width": 4000, "height": 3000},
        )
        
        # 创建多个素材箱
        project = project.create_bin("Videos")
        project = project.create_bin("Images")
        
        # 移动素材到素材箱
        video_bin_id = None
        image_bin_id = None
        for bin_id, bin_obj in project.bins.items():
            if bin_obj.name == "Videos":
                video_bin_id = bin_id
            elif bin_obj.name == "Images":
                image_bin_id = bin_id
        
        # 移动素材
        video_asset_ids = [a.asset_id for a in project.video_assets]
        for asset_id in video_asset_ids:
            if video_bin_id:
                project = project.move_asset_to_bin(asset_id, video_bin_id)
        
        image_asset_ids = [a.asset_id for a in project.image_assets]
        for asset_id in image_asset_ids:
            if image_bin_id:
                project = project.move_asset_to_bin(asset_id, image_bin_id)
        
        # 创建多个时间线
        project = project.create_timeline("Main Sequence")
        project = project.create_timeline("Alt Version")
        
        # 序列化 -> 反序列化
        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)
        
        # 验证数据一致性
        assert restored.name == project.name
        assert restored.project_id == project.project_id
        assert restored.asset_count == project.asset_count
        assert restored.bin_count == project.bin_count
        assert restored.timeline_count == project.timeline_count
        
        # 验证素材
        for asset_id, asset in project.assets.items():
            restored_asset = restored.assets.get(asset_id)
            assert restored_asset is not None
            assert restored_asset.path == asset.path
            assert restored_asset.media_type == asset.media_type
        
        # 验证素材箱
        for bin_id, bin_obj in project.bins.items():
            restored_bin = restored.bins.get(bin_id)
            assert restored_bin is not None
            assert set(restored_bin.asset_ids) == set(bin_obj.asset_ids)


# ================================================================
# Media Type Detection Tests
# ================================================================

class TestMediaTypeDetection:
    """媒体类型检测测试"""

    def test_detect_video_type(self) -> None:
        """测试检测视频类型"""
        assert get_media_type("/path/to/video.mp4") == "video"
        assert get_media_type("/path/to/video.mov") == "video"
        assert get_media_type("/path/to/video.avi") == "video"
        assert get_media_type("/path/to/video.mkv") == "video"

    def test_detect_image_type(self) -> None:
        """测试检测图片类型"""
        assert get_media_type("/path/to/image.jpg") == "image"
        assert get_media_type("/path/to/image.jpeg") == "image"
        assert get_media_type("/path/to/image.png") == "image"
        assert get_media_type("/path/to/image.tif") == "image"
        assert get_media_type("/path/to/raw.cr2") == "image"

    def test_detect_audio_type(self) -> None:
        """测试检测音频类型"""
        assert get_media_type("/path/to/audio.wav") == "audio"
        assert get_media_type("/path/to/audio.mp3") == "audio"
        assert get_media_type("/path/to/audio.flac") == "audio"

    def test_unknown_type_defaults_to_image(self) -> None:
        """测试未知类型默认为图片"""
        # 未知扩展名默认返回 "image"
        assert get_media_type("/path/to/file.xyz") == "image"


# ================================================================
# Project Properties Tests
# ================================================================

class TestProjectProperties:
    """项目属性测试"""

    def test_asset_count_property(self) -> None:
        """测试素材数量属性"""
        project = EditProject.create("Test")
        assert project.asset_count == 0
        
        project = project.import_asset(
            path="/path/to/video.mp4",
            media_type="video",
            metadata={},
        )
        assert project.asset_count == 1

    def test_bin_count_property(self) -> None:
        """测试素材箱数量属性"""
        project = EditProject.create("Test")
        assert project.bin_count == 1  # root bin
        
        project = project.create_bin("New Bin")
        assert project.bin_count == 2

    def test_timeline_count_property(self) -> None:
        """测试时间线数量属性"""
        project = EditProject.create("Test")
        assert project.timeline_count == 0
        
        project = project.create_timeline("Timeline 1")
        assert project.timeline_count == 1

    def test_active_timeline_property(self) -> None:
        """测试活动时间线属性"""
        project = EditProject.create("Test")
        assert project.active_timeline is None
        
        project = project.create_timeline("Timeline 1")
        assert project.active_timeline is not None
        assert project.active_timeline.name == "Timeline 1"

    def test_root_bin_property(self) -> None:
        """测试 root bin 属性"""
        project = EditProject.create("Test")
        root_bin = project.root_bin
        assert root_bin is not None
        assert root_bin.name == "Root"


# ================================================================
# Error Handling Tests
# ================================================================

class TestErrorHandling:
    """错误处理测试"""

    def test_invalid_json_deserialize(self) -> None:
        """测试无效 JSON 反序列化"""
        with pytest.raises(ValueError, match="Invalid JSON"):
            EditProjectSerializer.from_json("not valid json")

    def test_missing_version_deserialize(self) -> None:
        """测试缺少版本字段"""
        json_str = '{"project_type": "edit_project", "project": {}}'
        with pytest.raises(ValueError, match="Missing version field"):
            EditProjectSerializer.from_json(json_str)

    def test_wrong_project_type_deserialize(self) -> None:
        """测试错误的项目类型"""
        json_str = '{"version": 3, "project_type": "wrong_type", "project": {}}'
        with pytest.raises(ValueError, match="Invalid project_type"):
            EditProjectSerializer.from_json(json_str)

    def test_missing_project_data_deserialize(self) -> None:
        """测试缺少项目数据"""
        json_str = '{"version": 3, "project_type": "edit_project"}'
        with pytest.raises(ValueError, match="Missing project data"):
            EditProjectSerializer.from_json(json_str)