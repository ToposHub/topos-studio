"""
Quick Project 功能测试。

测试 EditProject.create_quick_project() 工厂方法和 QuickProjectDialog。

测试内容：
- create_quick_project() 创建项目
- 自动导入素材
- 自动创建 timeline
- 元数据探测
"""

import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef

pytestmark = pytest.mark.edit_core


class TestEditProjectCreateQuickProject:
    """测试 EditProject.create_quick_project() 工厂方法。"""

    def test_create_quick_project_video(self):
        """测试从视频文件创建 Quick Project。"""
        # Mock 视频元数据探测
        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = 2
        mock_video_meta.audio_sample_rate = 48000

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            # 创建测试视频路径
            video_path = "/test/sample.mov"

            # 创建 Quick Project
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Test Quick Project",
                media_path=video_path,
                fps=24.0,
                width=1920,
                height=1080,
            )

            # 验证项目创建成功
            assert project is not None
            assert project.name == "Test Quick Project"

            # 验证素材导入
            assert asset_id is not None
            assert asset_id in project.assets
            asset = project.assets[asset_id]
            assert asset.path == video_path
            assert asset.media_type == "video"
            assert asset.display_name == os.path.basename(video_path)

            # 验证素材元数据
            assert asset.metadata.get("width") == 1920
            assert asset.metadata.get("height") == 1080
            assert asset.metadata.get("fps") == 24.0

            # 验证 timeline 创建
            assert timeline_id is not None
            assert timeline_id in project.timelines
            assert project.active_timeline_id == timeline_id
            timeline = project.timelines[timeline_id]
            assert timeline.name == "Main Timeline"
            assert timeline.fps == 24.0
            assert timeline.width == 1920
            assert timeline.height == 1080

    def test_create_quick_project_video_auto_resolution(self):
        """测试自动使用视频分辨率。"""
        # Mock 视频元数据（4K 视频）
        mock_video_meta = MagicMock()
        mock_video_meta.width = 3840
        mock_video_meta.height = 2160
        mock_video_meta.fps = 29.97
        mock_video_meta.duration_seconds = 60.0
        mock_video_meta.total_frames = 1798
        mock_video_meta.codec_name = "prores"
        mock_video_meta.pix_fmt = "yuv422p10le"
        mock_video_meta.audio_channels = 2
        mock_video_meta.audio_sample_rate = 48000

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/4k_video.mov"

            # 创建 Quick Project（不指定分辨率，使用视频分辨率）
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="4K Project",
                media_path=video_path,
            )

            # 验证使用视频分辨率和帧率
            timeline = project.timelines[timeline_id]
            assert timeline.width == 3840  # 4K
            assert timeline.height == 2160
            assert timeline.fps == 29.97

    def test_create_quick_project_normalizes_video_metadata(self):
        """测试 VideoMetadata 的 Rational/标准字段会规范为项目可保存的值。"""
        from src.shared.media.metadata import Rational

        mock_video_meta = MagicMock()
        mock_video_meta.width = 3840
        mock_video_meta.height = 2160
        mock_video_meta.fps = Rational(30000, 1001)
        mock_video_meta.duration_seconds = 60.0
        mock_video_meta.total_frames = 1798
        mock_video_meta.codec_name = "prores"
        mock_video_meta.pixel_format = "yuv422p10le"

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Rational Metadata Project",
                media_path="/test/rational.mov",
            )

            asset = project.assets[asset_id]
            timeline = project.timelines[timeline_id]
            assert isinstance(asset.metadata["fps"], float)
            assert asset.metadata["fps"] == pytest.approx(29.970, rel=1e-3)
            assert asset.metadata["pixel_format"] == "yuv422p10le"
            assert timeline.fps == pytest.approx(29.970, rel=1e-3)

    def test_create_quick_project_image(self):
        """测试从图片文件创建 Quick Project。"""
        # Mock 图片元数据探测
        mock_file_meta = {
            "width": 2048,
            "height": 1536,
            "format": "JPEG",
        }

        with patch('src.shared.io.file_io.FileIO.get_metadata', return_value=mock_file_meta):
            # 创建测试图片路径
            image_path = "/test/sample.jpg"

            # 创建 Quick Project
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Image Project",
                media_path=image_path,
                fps=24.0,
                width=1920,
                height=1080,
            )

            # 验证项目创建成功
            assert project is not None
            assert project.name == "Image Project"

            # 验证素材导入
            assert asset_id is not None
            asset = project.assets[asset_id]
            assert asset.media_type == "image"
            assert asset.path == image_path

            # 验证 timeline 创建
            assert timeline_id is not None
            timeline = project.timelines[timeline_id]
            assert timeline.fps == 24.0

    def test_create_quick_project_no_metadata(self):
        """测试元数据探测失败时的默认值。"""
        # Mock 元数据探测失败
        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=None):
            video_path = "/test/unknown_format.mov"

            # 创建 Quick Project（使用默认值）
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Fallback Project",
                media_path=video_path,
            )

            # 验证使用默认分辨率和帧率
            timeline = project.timelines[timeline_id]
            assert timeline.width == 1920  # 默认
            assert timeline.height == 1080
            assert timeline.fps == 24.0

    def test_create_quick_project_custom_timeline_name(self):
        """测试自定义 timeline 名称。"""
        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = None
        mock_video_meta.audio_sample_rate = None

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/video.mov"

            # 创建 Quick Project（自定义 timeline 名称）
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Custom Timeline Project",
                media_path=video_path,
                timeline_name="My Custom Timeline",
            )

            # 验证 timeline 名称
            timeline = project.timelines[timeline_id]
            assert timeline.name == "My Custom Timeline"

    def test_create_quick_project_root_bin(self):
        """测试 root bin 创建。"""
        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = None
        mock_video_meta.audio_sample_rate = None

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/video.mov"

            # 创建 Quick Project
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Bin Test Project",
                media_path=video_path,
            )

            # 验证 root bin
            assert project.root_bin_id is not None
            assert project.root_bin_id in project.bins
            root_bin = project.bins[project.root_bin_id]
            assert root_bin.name == "Root"

            # 验证素材已添加到 root bin
            assert asset_id in root_bin.asset_ids


@pytest.mark.skip(reason="需要 QApplication，在非 GUI 环境中会崩溃")
class TestQuickProjectDialog:
    """测试 QuickProjectDialog 对话框。"""

    def test_dialog_init_video(self):
        """测试对话框初始化（视频文件）。"""
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

        # Mock 视频元数据
        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = 2
        mock_video_meta.audio_sample_rate = 48000

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/sample.mov"

            # 创建对话框（不显示）
            dialog = QuickProjectDialog(video_path)

            # 验证元数据探测
            assert dialog._media_metadata["width"] == 1920
            assert dialog._media_metadata["height"] == 1080
            assert dialog._media_metadata["fps"] == 24.0
            assert dialog._media_metadata["media_type"] == "video"

            # 验证默认项目名称（使用文件名）
            expected_name = os.path.splitext(os.path.basename(video_path))[0]
            assert dialog.name_edit.text() == expected_name

            # 验证默认项目位置（媒体文件所在目录）
            expected_location = os.path.dirname(video_path)
            assert dialog.location_edit.text() == expected_location

    def test_dialog_init_image(self):
        """测试对话框初始化（图片文件）。"""
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

        # Mock 图片元数据
        mock_file_meta = {
            "width": 2048,
            "height": 1536,
        }

        with patch('src.shared.io.file_io.FileIO.get_metadata', return_value=mock_file_meta):
            image_path = "/test/sample.jpg"

            # 创建对话框
            dialog = QuickProjectDialog(image_path)

            # 验证元数据探测
            assert dialog._media_metadata["width"] == 2048
            assert dialog._media_metadata["height"] == 1536
            assert dialog._media_metadata["media_type"] == "image"

    def test_get_timeline_settings(self):
        """测试获取 Timeline 设置。"""
        from src.features.edit.ui.quick_project_dialog import QuickProjectDialog

        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = None
        mock_video_meta.audio_sample_rate = None

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/video.mov"
            dialog = QuickProjectDialog(video_path)

            # 设置自定义分辨率
            dialog.width_spin.setValue(3840)
            dialog.height_spin.setValue(2160)

            # 获取设置
            settings = dialog.get_timeline_settings()

            assert settings["width"] == 3840
            assert settings["height"] == 2160
            assert settings["auto_add_media"] == True


@pytest.mark.skip(reason="需要 QApplication，在非 GUI 环境中会崩溃")
class TestQuickProjectIntegration:
    """Quick Project 集成测试。"""

    def test_quick_project_roundtrip(self):
        """测试 Quick Project 序列化往返。"""
        from src.features.edit.core.serialization import EditProjectSerializer

        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 24.0
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = None
        mock_video_meta.audio_sample_rate = None

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            # 创建 Quick Project
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Roundtrip Test",
                media_path="/test/video.mov",
            )

            # 序列化
            json_str = EditProjectSerializer.to_json(project)

            # 反序列化
            restored = EditProjectSerializer.from_json(json_str)

            # 验证往返
            assert restored.name == project.name
            assert len(restored.assets) == len(project.assets)
            assert len(restored.timelines) == len(project.timelines)
            assert restored.active_timeline_id == project.active_timeline_id

    def test_quick_project_with_custom_fps(self):
        """测试自定义帧率。"""
        mock_video_meta = MagicMock()
        mock_video_meta.width = 1920
        mock_video_meta.height = 1080
        mock_video_meta.fps = 23.976  # 视频原始帧率
        mock_video_meta.duration_seconds = 10.0
        mock_video_meta.total_frames = 240
        mock_video_meta.codec_name = "h264"
        mock_video_meta.pix_fmt = "yuv420p"
        mock_video_meta.audio_channels = None
        mock_video_meta.audio_sample_rate = None

        with patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata', return_value=mock_video_meta):
            video_path = "/test/film.mov"

            # 创建 Quick Project，指定不同的帧率（不使用视频帧率）
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name="Film Project",
                media_path=video_path,
                fps=24.0,  # 强制使用 24fps
                width=1920,
                height=1080,
            )

            # 验证使用指定帧率
            timeline = project.timelines[timeline_id]
            assert timeline.fps == 24.0
