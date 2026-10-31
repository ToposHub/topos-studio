"""
Tests for P10.3 Disk Proxy Integration.

Tests the professional NLE proxy cache system:
- ProxyStatus lifecycle
- DiskProxyInfo validation
- Frame provider service with disk proxy
- SourceManager with disk proxy
"""

import os
import tempfile
import time
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

from src.shared.media.proxy import (
    ProxyStatus,
    DiskProxyInfo,
    DiskProxyConfig,
    ProxyFormat,
    ProxyResolution,
    validate_disk_proxy,
    get_proxy_playback_priority,
    DEFAULT_DISK_PROXY_INFO,
)
from src.shared.video.frame_provider_service import (
    VideoFrameProviderService,
    ClipSession,
    FrameQuality,
)


# =============================================================================
# ProxyStatus Tests
# =============================================================================

class TestProxyStatus:
    """Test ProxyStatus enum."""

    def test_status_values(self) -> None:
        """Test status enum values."""
        assert ProxyStatus.MISSING.value == 'missing'
        assert ProxyStatus.GENERATING.value == 'generating'
        assert ProxyStatus.READY.value == 'ready'
        assert ProxyStatus.STALE.value == 'stale'
        assert ProxyStatus.FAILED.value == 'failed'

    def test_can_play_with_proxy(self) -> None:
        """Test can_play_with_proxy property."""
        assert ProxyStatus.READY.can_play_with_proxy is True
        assert ProxyStatus.MISSING.can_play_with_proxy is False
        assert ProxyStatus.GENERATING.can_play_with_proxy is False
        assert ProxyStatus.STALE.can_play_with_proxy is False
        assert ProxyStatus.FAILED.can_play_with_proxy is False

    def test_needs_generation(self) -> None:
        """Test needs_generation property."""
        assert ProxyStatus.MISSING.needs_generation is True
        assert ProxyStatus.STALE.needs_generation is True
        assert ProxyStatus.FAILED.needs_generation is True
        assert ProxyStatus.READY.needs_generation is False
        assert ProxyStatus.GENERATING.needs_generation is False

    def test_from_string(self) -> None:
        """Test from_string conversion."""
        assert ProxyStatus.from_string('missing') == ProxyStatus.MISSING
        assert ProxyStatus.from_string('generating') == ProxyStatus.GENERATING
        assert ProxyStatus.from_string('ready') == ProxyStatus.READY
        assert ProxyStatus.from_string('stale') == ProxyStatus.STALE
        assert ProxyStatus.from_string('failed') == ProxyStatus.FAILED
        # Legacy compatibility
        assert ProxyStatus.from_string('none') == ProxyStatus.MISSING
        assert ProxyStatus.from_string('pending') == ProxyStatus.GENERATING

    def test_to_string(self) -> None:
        """Test to_string conversion."""
        assert ProxyStatus.MISSING.to_string() == 'missing'
        assert ProxyStatus.READY.to_string() == 'ready'


# =============================================================================
# DiskProxyInfo Tests
# =============================================================================

class TestDiskProxyInfo:
    """Test DiskProxyInfo dataclass."""

    def test_default_values(self) -> None:
        """Test default values."""
        info = DiskProxyInfo()
        assert info.status == ProxyStatus.MISSING
        assert info.proxy_path is None
        assert info.proxy_resolution is None
        assert info.generation_progress == 0.0

    def test_is_ready(self) -> None:
        """Test is_ready property."""
        info1 = DiskProxyInfo(status=ProxyStatus.READY, proxy_path='/path/to/proxy.mov')
        assert info1.is_ready is True

        info2 = DiskProxyInfo(status=ProxyStatus.READY, proxy_path=None)
        assert info2.is_ready is False

        info3 = DiskProxyInfo(status=ProxyStatus.MISSING)
        assert info3.is_ready is False

    def test_with_status(self) -> None:
        """Test with_status method."""
        info = DiskProxyInfo(
            status=ProxyStatus.GENERATING,
            source_mtime=1234567890.0,
            source_size=1000000,
        )
        new_info = info.with_status(ProxyStatus.READY)
        assert new_info.status == ProxyStatus.READY
        assert new_info.source_mtime == 1234567890.0
        assert new_info.source_size == 1000000

    def test_with_progress(self) -> None:
        """Test with_progress method."""
        info = DiskProxyInfo(status=ProxyStatus.GENERATING)
        new_info = info.with_progress(50.0)
        assert new_info.status == ProxyStatus.GENERATING
        assert new_info.generation_progress == 50.0

    def test_with_proxy_path(self) -> None:
        """Test with_proxy_path method."""
        info = DiskProxyInfo(source_mtime=1234567890.0)
        new_info = info.with_proxy_path('/path/to/proxy.mov', (1920, 1080))
        assert new_info.status == ProxyStatus.READY
        assert new_info.proxy_path == '/path/to/proxy.mov'
        assert new_info.proxy_resolution == (1920, 1080)
        assert new_info.generation_progress == 100.0


# =============================================================================
# validate_disk_proxy Tests
# =============================================================================

class TestValidateDiskProxy:
    """Test validate_disk_proxy function."""

    def test_generating_status_unchanged(self) -> None:
        """Test GENERATING status is not validated."""
        info = DiskProxyInfo(status=ProxyStatus.GENERATING)
        result = validate_disk_proxy(info, '/path/to/video.mp4')
        assert result.status == ProxyStatus.GENERATING

    def test_missing_proxy_path(self) -> None:
        """Test missing proxy_path returns MISSING."""
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path=None,
        )
        result = validate_disk_proxy(info, '/path/to/video.mp4')
        assert result.status == ProxyStatus.MISSING

    def test_proxy_file_not_exists(self) -> None:
        """Test proxy file not exists returns MISSING."""
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/nonexistent/proxy.mov',
            source_mtime=1234567890.0,
            source_size=1000000,
        )
        result = validate_disk_proxy(info, '/path/to/video.mp4')
        assert result.status == ProxyStatus.MISSING

    def test_proxy_file_exists_and_valid(self) -> None:
        """Test valid proxy file."""
        with tempfile.NamedTemporaryFile(suffix='.mov', delete=False) as proxy_file:
            proxy_path = proxy_file.name
            proxy_file.write(b'test proxy content')

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as source_file:
            source_path = source_file.name
            source_file.write(b'test source content')

        try:
            source_mtime, source_size = os.stat(source_path).st_mtime, os.stat(source_path).st_size

            info = DiskProxyInfo(
                status=ProxyStatus.READY,
                proxy_path=proxy_path,
                proxy_resolution=(1920, 1080),
                proxy_format=ProxyFormat.PRORES_PROXY,
                source_mtime=source_mtime,
                source_size=source_size,
            )

            result = validate_disk_proxy(info, source_path)
            assert result.status == ProxyStatus.READY

        finally:
            os.unlink(proxy_path)
            os.unlink(source_path)

    def test_source_modified_returns_stale(self) -> None:
        """Test modified source returns STALE."""
        with tempfile.NamedTemporaryFile(suffix='.mov', delete=False) as proxy_file:
            proxy_path = proxy_file.name
            proxy_file.write(b'test proxy content')

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as source_file:
            source_path = source_file.name
            source_file.write(b'test source content')

        try:
            # Get old mtime
            old_mtime = os.stat(source_path).st_mtime - 100

            info = DiskProxyInfo(
                status=ProxyStatus.READY,
                proxy_path=proxy_path,
                source_mtime=old_mtime,
                source_size=1000,  # Different size
            )

            result = validate_disk_proxy(info, source_path)
            assert result.status == ProxyStatus.STALE

        finally:
            os.unlink(proxy_path)
            os.unlink(source_path)


# =============================================================================
# get_proxy_playback_priority Tests
# =============================================================================

class TestGetProxyPlaybackPriority:
    """Test get_proxy_playback_priority function."""

    def test_export_always_master(self) -> None:
        """Test export mode always uses master."""
        info = DiskProxyInfo(status=ProxyStatus.READY, proxy_path='/path/to/proxy.mov')
        priority = get_proxy_playback_priority(info, 'export')
        assert priority == 'master'

    def test_paused_always_master(self) -> None:
        """Test paused mode always uses master."""
        info = DiskProxyInfo(status=ProxyStatus.READY, proxy_path='/path/to/proxy.mov')
        priority = get_proxy_playback_priority(info, 'paused')
        assert priority == 'master'

    def test_preview_ready_uses_proxy(self) -> None:
        """Test preview mode with ready proxy."""
        info = DiskProxyInfo(status=ProxyStatus.READY, proxy_path='/path/to/proxy.mov')
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'proxy'

    def test_preview_generating_uses_master_downscaled(self) -> None:
        """Test preview mode with generating proxy."""
        info = DiskProxyInfo(status=ProxyStatus.GENERATING)
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'master_downscaled'

    def test_preview_missing_uses_master_downscaled(self) -> None:
        """Test preview mode with missing proxy."""
        info = DiskProxyInfo(status=ProxyStatus.MISSING)
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'master_downscaled'


# =============================================================================
# ClipSession Disk Proxy Tests
# =============================================================================

class TestClipSessionDiskProxy:
    """Test ClipSession with disk proxy support."""

    def test_clip_session_disk_proxy_fields(self) -> None:
        """Test ClipSession has disk proxy fields."""
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
        )
        assert session.disk_proxy_source is None
        assert session.disk_proxy_info is None
        assert session.playback_mode == 'preview'

    def test_clip_session_with_disk_proxy_info(self) -> None:
        """Test ClipSession with disk proxy info."""
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/path/to/proxy.mov',
            proxy_resolution=(1920, 1080),
        )
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
            disk_proxy_info=info,
        )
        assert session.disk_proxy_info == info
        assert session.disk_proxy_info.is_ready

    def test_get_active_proxy_source_no_disk_proxy(self) -> None:
        """Test get_active_proxy_source without disk proxy."""
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
            proxy_source=MagicMock(),
        )
        source = session.get_active_proxy_source()
        assert source == session.proxy_source

    def test_get_active_proxy_source_with_disk_proxy_ready(self) -> None:
        """Test get_active_proxy_source with ready disk proxy."""
        mock_disk_source = MagicMock()
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/path/to/proxy.mov',
        )
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
            disk_proxy_source=mock_disk_source,
            disk_proxy_info=info,
            proxy_source=MagicMock(),  # Should not be used
        )
        source = session.get_active_proxy_source()
        assert source == mock_disk_source

    def test_get_source_for_quality_master(self) -> None:
        """Test get_source_for_quality with MASTER quality."""
        mock_master = MagicMock()
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
            master_source=mock_master,
        )
        source = session.get_source_for_quality(FrameQuality.MASTER)
        assert source == mock_master

    def test_get_source_for_quality_auto_export(self) -> None:
        """Test get_source_for_quality AUTO with export mode."""
        mock_master = MagicMock()
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
            master_source=mock_master,
            playback_mode='export',
        )
        source = session.get_source_for_quality(FrameQuality.AUTO)
        assert source == mock_master

    def test_set_playback_mode(self) -> None:
        """Test set_playback_mode."""
        session = ClipSession(
            clip_id='test-clip',
            path='/path/to/video.mp4',
        )
        session.set_playback_mode('paused')
        assert session.playback_mode == 'paused'

        session.set_playback_mode('export')
        assert session.playback_mode == 'export'

        # Invalid mode should not change
        session.set_playback_mode('invalid')
        assert session.playback_mode == 'export'


# =============================================================================
# VideoFrameProviderService Disk Proxy Tests
# =============================================================================

class TestVideoFrameProviderServiceDiskProxy:
    """Test VideoFrameProviderService with disk proxy."""

    def test_open_clip_with_disk_proxy_missing(self) -> None:
        """Test open_clip_with_disk_proxy with MISSING status."""
        info = DiskProxyInfo(status=ProxyStatus.MISSING)

        service = VideoFrameProviderService()

        # Mock the actual opening to avoid file system dependency
        with patch.object(service, '_open_clip_sources') as mock_open:
            mock_open.return_value = None
            clip_id = service.open_clip_with_disk_proxy('/path/to/video.mp4', info)

        assert clip_id.startswith('clip_')
        session = service.get_clip_session(clip_id)
        assert session is not None
        assert session.disk_proxy_info == info

    def test_open_clip_with_disk_proxy_ready(self) -> None:
        """Test open_clip_with_disk_proxy with READY status."""
        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/path/to/proxy.mov',
            proxy_resolution=(1920, 1080),
        )

        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            mock_open.return_value = None
            clip_id = service.open_clip_with_disk_proxy('/path/to/video.mp4', info)

        session = service.get_clip_session(clip_id)
        assert session.disk_proxy_info == info

    def test_update_disk_proxy_info(self) -> None:
        """Test update_disk_proxy_info."""
        service = VideoFrameProviderService()

        # Create a session
        with patch.object(service, '_open_clip_sources'):
            clip_id = service.open_clip('/path/to/video.mp4')

        session = service.get_clip_session(clip_id)

        # Update with new proxy info
        new_info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/path/to/proxy.mov',
        )

        # Mock the disk proxy source creation
        with patch('src.shared.media.factory.MediaSourceFactory.create') as mock_create:
            with patch('src.shared.media.factory.MediaSourceFactory.open_source'):
                mock_source = MagicMock()
                mock_create.return_value = mock_source
                result = service.update_disk_proxy_info(clip_id, new_info)

        assert result is True
        assert session.disk_proxy_info == new_info


# =============================================================================
# SourceManager Disk Proxy Tests
# =============================================================================

class TestSourceManagerDiskProxy:
    """Test SourceManager with disk proxy."""

    def test_source_manager_disk_proxy_params(self) -> None:
        """Test SourceManager constructor with disk proxy params."""
        from src.shared.video.playback_clock import SourceManager

        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path='/path/to/proxy.mov',
        )

        manager = SourceManager(
            path='/path/to/video.mp4',
            disk_proxy_path='/path/to/proxy.mov',
            disk_proxy_info=info,
        )

        assert manager._disk_proxy_path == '/path/to/proxy.mov'
        assert manager._disk_proxy_info == info

    def test_source_manager_open_with_disk_proxy_missing(self) -> None:
        """Test SourceManager.open with MISSING proxy."""
        from src.shared.video.playback_clock import SourceManager

        info = DiskProxyInfo(status=ProxyStatus.MISSING)

        manager = SourceManager(
            path='/path/to/video.mp4',
            disk_proxy_info=info,
        )

        # Mock the actual opening
        with patch.object(manager, '_source_factory') as mock_factory:
            mock_master = MagicMock()
            mock_master.open.return_value = None
            mock_master.probe.return_value = MagicMock(
                width=3840,
                height=2160,
                pixel_format='yuv420p',
                fps=MagicMock(to_fps=lambda: 24.0),
                total_frames=240,
            )
            mock_factory.return_value = mock_master

            result = manager.open()

        # Should have master_source but no disk_proxy_source
        assert manager._master_source is not None
        assert manager._disk_proxy_source is None


# =============================================================================
# Integration Tests
# =============================================================================

class TestDiskProxyIntegration:
    """Integration tests for disk proxy system."""

    def test_proxy_status_lifecycle(self) -> None:
        """Test complete proxy status lifecycle."""
        # 1. Initial state: MISSING
        info = DiskProxyInfo(status=ProxyStatus.MISSING)
        assert info.needs_generation

        # 2. Start generation: GENERATING
        info = info.with_status(ProxyStatus.GENERATING)
        assert info.status.is_generating
        assert not info.needs_generation

        # 3. Update progress
        info = info.with_progress(50.0)
        assert info.generation_progress == 50.0

        # 4. Complete: READY
        info = info.with_proxy_path('/path/to/proxy.mov', (1920, 1080))
        assert info.is_ready
        assert info.generation_progress == 100.0

        # 5. Source modified: STALE
        info = info.with_status(ProxyStatus.STALE)
        assert info.needs_generation

    def test_playback_priority_flow(self) -> None:
        """Test playback priority flow."""
        # MISSING: fallback
        info = DiskProxyInfo(status=ProxyStatus.MISSING)
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'master_downscaled'

        # GENERATING: waiting
        info = info.with_status(ProxyStatus.GENERATING)
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'master_downscaled'

        # READY: use proxy
        info = info.with_proxy_path('/path/to/proxy.mov', (1920, 1080))
        priority = get_proxy_playback_priority(info, 'preview')
        assert priority == 'proxy'

        # paused: always master
        priority = get_proxy_playback_priority(info, 'paused')
        assert priority == 'master'

        # export: always master
        priority = get_proxy_playback_priority(info, 'export')
        assert priority == 'master'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])