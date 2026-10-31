"""
Test ColorSession module (P9.1).

Unit tests for ColorSession dataclass and ColorSessionManager.
"""

import pytest
import time
from dataclasses import dataclass


# 直接导入 ColorSession 模块（避免完整导入链）
import sys
import os

# 只导入 color_session 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.features.color.core.color_session import (
    ColorSession,
    ColorSessionManager,
    SourceKind,
    SavePolicy,
)


# ================================================================
# 测试辅助
# ================================================================

@dataclass
class MockPageContext:
    """模拟 PageContext"""
    source_page: str = "Edit"
    target_page: str = "Color"
    clip_id: str | None = None
    asset_id: str | None = None
    timeline_frame: int | None = None
    source_frame: int | None = None
    clip_local_frame: int | None = None
    color_stack_id: str | None = None
    timeline_id: str | None = None


# ================================================================
# SourceKind 测试
# ================================================================

class TestSourceKind:
    """Test SourceKind enum."""

    def test_source_kind_values(self):
        """Test SourceKind has all expected values."""
        assert SourceKind.SINGLE_ASSET.value == "single_asset"
        assert SourceKind.PROJECT_ASSET.value == "project_asset"
        assert SourceKind.TIMELINE_CLIP.value == "timeline_clip"

    def test_source_kind_count(self):
        """Test we have 3 source kinds."""
        assert len(SourceKind) == 3


# ================================================================
# SavePolicy 测试
# ================================================================

class TestSavePolicy:
    """Test SavePolicy enum."""

    def test_save_policy_values(self):
        """Test SavePolicy has expected values."""
        assert SavePolicy.TOPOS_PROJECT.value == "topos_project"

    def test_save_policy_count(self):
        """Test we have 1 save policy."""
        assert len(SavePolicy) == 1


# ================================================================
# ColorSession 测试
# ================================================================

class TestColorSession:
    """Test ColorSession dataclass."""

    def test_create_single_asset_session(self):
        """Test creating single asset ColorSession."""
        session = ColorSession.create_single_asset(
            project_id="project-001",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )

        assert session.source_kind == SourceKind.SINGLE_ASSET
        assert session.project_id == "project-001"
        assert session.color_stack_id == "stack-001"
        assert session.source_path == "/path/to/image.jpg"
        assert session.asset_id is None
        assert session.clip_id is None
        assert session.save_policy == SavePolicy.TOPOS_PROJECT

    def test_create_project_asset_session(self):
        """Test creating project asset ColorSession (image only; video raises ValueError)."""
        # Image: allowed
        session = ColorSession.create_project_asset(
            project_id="project-002",
            asset_id="asset-002",
            color_stack_id="stack-002",
            source_path="/path/to/image.jpg",
            frame=42,
        )

        assert session.source_kind == SourceKind.PROJECT_ASSET
        assert session.project_id == "project-002"
        assert session.asset_id == "asset-002"
        assert session.color_stack_id == "stack-002"
        assert session.source_path == "/path/to/image.jpg"
        assert session.frame == 42
        assert session.clip_id is None

    def test_create_timeline_clip_session(self):
        """Test creating timeline clip ColorSession."""
        session = ColorSession.create_timeline_clip(
            project_id="project-003",
            asset_id="asset-003",
            clip_id="clip-003",
            color_stack_id="stack-003",
            source_path="/path/to/clip.mp4",
            timeline_id="timeline-003",
            frame=100,
        )

        assert session.source_kind == SourceKind.TIMELINE_CLIP
        assert session.project_id == "project-003"
        assert session.asset_id == "asset-003"
        assert session.clip_id == "clip-003"
        assert session.color_stack_id == "stack-003"
        assert session.source_path == "/path/to/clip.mp4"
        assert session.timeline_id == "timeline-003"
        assert session.frame == 100

    def test_session_validation_requires_project_id(self):
        """Test that project_id is required."""
        with pytest.raises(ValueError, match="project_id cannot be empty"):
            ColorSession(
                session_id="session-001",
                source_kind=SourceKind.SINGLE_ASSET,
                project_id="",  # 空
                color_stack_id="stack-001",
            )

    def test_session_validation_requires_color_stack_id(self):
        """Test that color_stack_id is required."""
        with pytest.raises(ValueError, match="color_stack_id cannot be empty"):
            ColorSession(
                session_id="session-001",
                source_kind=SourceKind.SINGLE_ASSET,
                project_id="project-001",
                color_stack_id="",  # 空
            )

    def test_project_asset_requires_asset_id(self):
        """Test that PROJECT_ASSET requires asset_id."""
        with pytest.raises(ValueError, match="asset_id required for PROJECT_ASSET"):
            ColorSession(
                session_id="session-001",
                source_kind=SourceKind.PROJECT_ASSET,
                project_id="project-001",
                color_stack_id="stack-001",
                # 缺少 asset_id
            )

    def test_timeline_clip_requires_clip_id(self):
        """Test that TIMELINE_CLIP requires clip_id."""
        with pytest.raises(ValueError, match="clip_id required for TIMELINE_CLIP"):
            ColorSession(
                session_id="session-001",
                source_kind=SourceKind.TIMELINE_CLIP,
                project_id="project-001",
                color_stack_id="stack-001",
                asset_id="asset-001",
                # 缺少 clip_id
            )

    def test_get_save_target_region(self):
        """Test get_save_target_region method."""
        # Single asset
        session1 = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        assert session1.get_save_target_region() == "color_stacks[stack-001]"

        # Project asset
        session2 = ColorSession.create_project_asset(
            project_id="p2",
            asset_id="asset-002",
            color_stack_id="stack-002",
            source_path="/path/to/image.jpg",
        )
        assert session2.get_save_target_region() == "color_stacks[stack-002]"

        # Timeline clip
        session3 = ColorSession.create_timeline_clip(
            project_id="p3",
            asset_id="asset-003",
            clip_id="clip-003",
            timeline_id="timeline-003",
            color_stack_id="stack-003",
            source_path="/path/to/clip.mp4",
        )
        assert session3.get_save_target_region() == "color_stacks[stack-003]"

    def test_is_single_asset(self):
        """Test is_single_asset method."""
        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        assert session.is_single_asset() is True
        assert session.is_project_asset() is False
        assert session.is_timeline_clip() is False

    def test_is_project_asset(self):
        """Test is_project_asset method."""
        session = ColorSession.create_project_asset(
            project_id="p2",
            asset_id="asset-002",
            color_stack_id="stack-002",
            source_path="/path/to/image.jpg",
        )
        assert session.is_single_asset() is False
        assert session.is_project_asset() is True
        assert session.is_timeline_clip() is False

    def test_is_timeline_clip(self):
        """Test is_timeline_clip method."""
        session = ColorSession.create_timeline_clip(
            project_id="p3",
            asset_id="asset-003",
            clip_id="clip-003",
            timeline_id="timeline-003",
            color_stack_id="stack-003",
            source_path="/path/to/clip.mp4",
        )
        assert session.is_single_asset() is False
        assert session.is_project_asset() is False
        assert session.is_timeline_clip() is True

    def test_has_frame(self):
        """Test has_frame method."""
        session_with_frame = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/video.mp4",
            frame=42,
        )
        assert session_with_frame.has_frame() is True

        session_no_frame = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        assert session_no_frame.has_frame() is False

    def test_to_dict_and_from_dict(self):
        """Test serialization roundtrip."""
        original = ColorSession.create_timeline_clip(
            project_id="project-001",
            asset_id="asset-001",
            clip_id="clip-001",
            color_stack_id="stack-001",
            source_path="/path/to/clip.mp4",
            timeline_id="timeline-001",
            frame=100,
        )

        data = original.to_dict()
        restored = ColorSession.from_dict(data)

        assert restored.session_id == original.session_id
        assert restored.source_kind == original.source_kind
        assert restored.project_id == original.project_id
        assert restored.asset_id == original.asset_id
        assert restored.clip_id == original.clip_id
        assert restored.color_stack_id == original.color_stack_id
        assert restored.source_path == original.source_path
        assert restored.timeline_id == original.timeline_id
        assert restored.frame == original.frame

    def test_hash_and_eq(self):
        """Test hash and equality."""
        session1 = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        session2 = ColorSession.create_single_asset(
            project_id="p2",
            color_stack_id="stack-002",
            source_path="/path/to/image.jpg",
        )

        # 不同 session_id，不相等
        assert session1 != session2

        # hash 基于 session_id
        assert hash(session1) != hash(session2)


# ================================================================
# ColorSession.from_page_context 测试
# ================================================================

class TestColorSessionFromPageContext:
    """Test ColorSession.from_page_context factory method."""

    def test_from_page_context_timeline_clip(self):
        """Test creating session from PageContext with clip_id."""
        context = MockPageContext(
            source_page="Edit",
            target_page="Color",
            clip_id="clip-001",
            asset_id="asset-001",
            timeline_id="timeline-001",
            timeline_frame=42,
            source_frame=12,
            clip_local_frame=2,
            color_stack_id="stack-001",
        )

        session = ColorSession.from_page_context(
            context=context,
            project_id="project-001",
            color_stack_id="stack-001",
            source_path="/path/to/video.mp4",
        )

        assert session.source_kind == SourceKind.TIMELINE_CLIP
        assert session.clip_id == "clip-001"
        assert session.asset_id == "asset-001"
        assert session.frame == 42
        assert session.timeline_id == "timeline-001"
        assert session.timeline_frame == 42
        assert session.source_frame == 12
        assert session.clip_local_frame == 2
        assert session.color_stack_id == "stack-001"

    def test_from_page_context_project_asset(self):
        """Test creating session from PageContext with asset_id but no clip_id (image only)."""
        context = MockPageContext(
            source_page="Edit",
            target_page="Color",
            asset_id="asset-002",
            color_stack_id="stack-002",
        )

        session = ColorSession.from_page_context(
            context=context,
            project_id="project-002",
            color_stack_id="stack-002",
            source_path="/path/to/image.jpg",
        )

        assert session.source_kind == SourceKind.PROJECT_ASSET
        assert session.asset_id == "asset-002"
        assert session.clip_id is None

    def test_from_page_context_single_asset(self):
        """Test creating session from PageContext without asset_id or clip_id."""
        context = MockPageContext(
            source_page="Library",
            target_page="Color",
        )

        session = ColorSession.from_page_context(
            context=context,
            project_id="project-003",
            color_stack_id="stack-003",
            source_path="/path/to/image.jpg",
        )

        assert session.source_kind == SourceKind.SINGLE_ASSET
        assert session.asset_id is None
        assert session.clip_id is None


# ================================================================
# Video guard 测试 — PROJECT_ASSET deprecated for video
# ================================================================

class TestVideoProjectAssetGuard:
    """
    Test that video assets are rejected from PROJECT_ASSET path.

    Product rule: video must enter Edit timeline before color grading.
    Only images may open directly in Color via PROJECT_ASSET.
    """

    def test_from_page_context_rejects_video_without_clip_id(self):
        """Video + asset_id but no clip_id raises ValueError."""
        context = MockPageContext(
            source_page="Edit",
            target_page="Color",
            asset_id="asset-video",
            # no clip_id
        )

        with pytest.raises(ValueError, match="Video must be opened from an Edit timeline clip"):
            ColorSession.from_page_context(
                context=context,
                project_id="project-001",
                color_stack_id="stack-001",
                source_path="/path/to/video.mp4",
            )

    def test_from_page_context_allows_image_without_clip_id(self):
        """Image + asset_id but no clip_id creates PROJECT_ASSET (allowed)."""
        context = MockPageContext(
            source_page="Edit",
            target_page="Color",
            asset_id="asset-image",
        )

        session = ColorSession.from_page_context(
            context=context,
            project_id="project-001",
            color_stack_id="stack-001",
            source_path="/path/to/photo.jpg",
        )

        assert session.source_kind == SourceKind.PROJECT_ASSET
        assert session.asset_id == "asset-image"
        assert session.clip_id is None

    def test_from_page_context_allows_video_with_clip_id(self):
        """Video + clip_id creates TIMELINE_CLIP (allowed)."""
        context = MockPageContext(
            source_page="Edit",
            target_page="Color",
            asset_id="asset-video",
            clip_id="clip-001",
            timeline_id="timeline-001",
        )

        session = ColorSession.from_page_context(
            context=context,
            project_id="project-001",
            color_stack_id="stack-001",
            source_path="/path/to/video.mp4",
        )

        assert session.source_kind == SourceKind.TIMELINE_CLIP
        assert session.clip_id == "clip-001"
        assert session.asset_id == "asset-video"

    def test_create_project_asset_rejects_video(self):
        """create_project_asset raises ValueError for video source_path."""
        with pytest.raises(ValueError, match="Video must be opened from an Edit timeline clip"):
            ColorSession.create_project_asset(
                project_id="p1",
                asset_id="asset-001",
                color_stack_id="stack-001",
                source_path="/path/to/clip.mp4",
            )

    def test_create_project_asset_allows_image(self):
        """create_project_asset succeeds for image source_path."""
        session = ColorSession.create_project_asset(
            project_id="p1",
            asset_id="asset-001",
            color_stack_id="stack-001",
            source_path="/path/to/photo.png",
        )
        assert session.source_kind == SourceKind.PROJECT_ASSET

    def test_from_page_context_image_various_extensions(self):
        """Various image extensions are allowed as PROJECT_ASSET."""
        image_exts = [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".exr"]
        for ext in image_exts:
            context = MockPageContext(
                source_page="Edit",
                target_page="Color",
                asset_id="asset-img",
            )
            session = ColorSession.from_page_context(
                context=context,
                project_id="project-001",
                color_stack_id="stack-001",
                source_path=f"/path/to/photo{ext}",
            )
            assert session.source_kind == SourceKind.PROJECT_ASSET, f"Failed for ext={ext}"

    def test_from_page_context_video_various_extensions_rejected(self):
        """Various video extensions are rejected as PROJECT_ASSET."""
        video_exts = [".mp4", ".mov", ".avi", ".mkv", ".mxf", ".webm"]
        for ext in video_exts:
            context = MockPageContext(
                source_page="Edit",
                target_page="Color",
                asset_id="asset-vid",
            )
            with pytest.raises(ValueError, match="Video must be opened from an Edit timeline clip"):
                ColorSession.from_page_context(
                    context=context,
                    project_id="project-001",
                    color_stack_id="stack-001",
                    source_path=f"/path/to/footage{ext}",
                )


# ================================================================
# ColorSessionManager 测试
# ================================================================

class TestColorSessionManager:
    """Test ColorSessionManager class."""

    def test_manager_init(self):
        """Test manager initialization."""
        manager = ColorSessionManager()
        assert manager.get_current_session() is None
        assert manager.has_active_session() is False

    def test_set_current_session(self):
        """Test setting current session."""
        manager = ColorSessionManager()
        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )

        manager.set_current_session(session)
        assert manager.get_current_session() == session
        assert manager.has_active_session() is True

    def test_clear_current_session(self):
        """Test clearing current session."""
        manager = ColorSessionManager()
        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )

        manager.set_current_session(session)
        manager.clear_current_session()

        assert manager.get_current_session() is None
        assert manager.has_active_session() is False

    def test_get_current_color_stack_id(self):
        """Test getting current color stack ID."""
        manager = ColorSessionManager()
        assert manager.get_current_color_stack_id() is None

        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        manager.set_current_session(session)

        assert manager.get_current_color_stack_id() == "stack-001"

    def test_get_current_source_path(self):
        """Test getting current source path."""
        manager = ColorSessionManager()

        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        manager.set_current_session(session)

        assert manager.get_current_source_path() == "/path/to/image.jpg"

    def test_get_current_source_kind(self):
        """Test getting current source kind."""
        manager = ColorSessionManager()

        session = ColorSession.create_timeline_clip(
            project_id="p1",
            asset_id="asset-001",
            clip_id="clip-001",
            timeline_id="timeline-001",
            color_stack_id="stack-001",
            source_path="/path/to/clip.mp4",
        )
        manager.set_current_session(session)

        assert manager.get_current_source_kind() == SourceKind.TIMELINE_CLIP

    def test_get_current_asset_id(self):
        """Test getting current asset ID."""
        manager = ColorSessionManager()

        session = ColorSession.create_project_asset(
            project_id="p1",
            asset_id="asset-001",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )
        manager.set_current_session(session)

        assert manager.get_current_asset_id() == "asset-001"

    def test_get_current_clip_id(self):
        """Test getting current clip ID."""
        manager = ColorSessionManager()

        session = ColorSession.create_timeline_clip(
            project_id="p1",
            asset_id="asset-001",
            clip_id="clip-001",
            timeline_id="timeline-001",
            color_stack_id="stack-001",
            source_path="/path/to/clip.mp4",
        )
        manager.set_current_session(session)

        assert manager.get_current_clip_id() == "clip-001"

    def test_get_current_frame(self):
        """Test getting current frame."""
        manager = ColorSessionManager()

        session = ColorSession.create_single_asset(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/video.mp4",
            frame=42,
        )
        manager.set_current_session(session)

        assert manager.get_current_frame() == 42

    def test_create_single_asset_session(self):
        """Test manager's create_single_asset_session method."""
        manager = ColorSessionManager()

        session = manager.create_single_asset_session(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )

        assert manager.get_current_session() == session
        assert session.source_kind == SourceKind.SINGLE_ASSET

    def test_create_project_asset_session(self):
        """Test manager's create_project_asset_session method."""
        manager = ColorSessionManager()

        session = manager.create_project_asset_session(
            project_id="p1",
            asset_id="asset-001",
            color_stack_id="stack-001",
            source_path="/path/to/image.jpg",
        )

        assert manager.get_current_session() == session
        assert session.source_kind == SourceKind.PROJECT_ASSET

    def test_create_timeline_clip_session(self):
        """Test manager's create_timeline_clip_session method."""
        manager = ColorSessionManager()

        session = manager.create_timeline_clip_session(
            project_id="p1",
            asset_id="asset-001",
            clip_id="clip-001",
            timeline_id="timeline-001",
            color_stack_id="stack-001",
            source_path="/path/to/clip.mp4",
        )

        assert manager.get_current_session() == session
        assert session.source_kind == SourceKind.TIMELINE_CLIP

    def test_session_history(self):
        """Test that sessions are stored in history."""
        manager = ColorSessionManager()

        session1 = manager.create_single_asset_session(
            project_id="p1",
            color_stack_id="stack-001",
            source_path="/path/to/image1.jpg",
        )

        session2 = manager.create_single_asset_session(
            project_id="p2",
            color_stack_id="stack-002",
            source_path="/path/to/image2.jpg",
        )

        # 当前是 session2
        assert manager.get_current_session() == session2

        # 但可以获取历史中的 session1
        assert manager.get_session_by_id(session1.session_id) == session1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
