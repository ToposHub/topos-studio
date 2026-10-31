"""
Tests for P10.3 Disk Proxy functionality.

Tests proxy configuration, key generation, resolution calculation,
and ProxyManager lifecycle.
"""

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from src.shared.media.proxy import (
    ProxyFormat,
    ProxyResolution,
    DiskProxyConfig,
    generate_proxy_key,
    get_source_file_info,
    DEFAULT_DISK_PROXY_CONFIG,
    get_disk_proxy_rejection_reason,
)
from src.shared.media.proxy_manager import ProxyManager, ProxyTask, ProxyStats


class TestProxyFormat:
    """Test ProxyFormat enum."""

    def test_extensions(self) -> None:
        """Test file extensions for each format."""
        assert ProxyFormat.PRORES_PROXY.extension == '.mov'
        assert ProxyFormat.H264.extension == '.mp4'
        assert ProxyFormat.IMAGE_SEQUENCE.extension == ''

    def test_codec_names(self) -> None:
        """Test codec names for each format."""
        assert ProxyFormat.PRORES_PROXY.codec_name == 'prores_ks'
        assert ProxyFormat.H264.codec_name == 'libx264'
        assert ProxyFormat.IMAGE_SEQUENCE.codec_name == 'png'

    def test_intra_only(self) -> None:
        """Test intra-only flag."""
        assert ProxyFormat.PRORES_PROXY.is_intra_only is True
        assert ProxyFormat.IMAGE_SEQUENCE.is_intra_only is True
        assert ProxyFormat.H264.is_intra_only is False


class TestProxyResolution:
    """Test ProxyResolution enum."""

    def test_scale_factors(self) -> None:
        """Test scale factors."""
        assert ProxyResolution.FULL.scale_factor == 1.0
        assert ProxyResolution.HALF.scale_factor == 0.5
        assert ProxyResolution.QUARTER.scale_factor == 0.25
        assert ProxyResolution.EIGHTH.scale_factor == 0.125

    def test_calculate_size(self) -> None:
        """Test resolution calculation."""
        # 4K -> 1/2
        size = ProxyResolution.HALF.calculate_size((3840, 2160))
        assert size == (1920, 1080)

        # 4K -> 1/4
        size = ProxyResolution.QUARTER.calculate_size((3840, 2160))
        assert size == (960, 540)

        # 4K -> 1/8
        size = ProxyResolution.EIGHTH.calculate_size((3840, 2160))
        assert size == (480, 270)

        # 1080p -> 1/2
        size = ProxyResolution.HALF.calculate_size((1920, 1080))
        assert size == (960, 540)

        # Ensure even numbers
        size = ProxyResolution.HALF.calculate_size((1921, 1081))
        assert size[0] % 2 == 0
        assert size[1] % 2 == 0

        # Minimum size enforcement
        size = ProxyResolution.EIGHTH.calculate_size((640, 360))
        assert size[0] >= 320
        assert size[1] >= 180


class TestDiskProxyConfig:
    """Test DiskProxyConfig."""

    def test_default_config(self) -> None:
        """Test default configuration."""
        config = DiskProxyConfig()
        assert config.format == ProxyFormat.PRORES_PROXY
        assert config.resolution == ProxyResolution.HALF
        assert config.prores_profile == '422'
        assert config.h264_crf == 23
        assert config.image_format == 'png'
        assert config.cache_dir == '.cache/video_proxy'
        assert config.target_height is None

    def test_fixed_target_height_preserves_aspect_and_even_dimensions(self) -> None:
        """The edit proxy profile can target 1080p independently of scale."""
        config = DiskProxyConfig(target_height=1080)

        assert config.calculate_target_size((3840, 2160)) == (1920, 1080)
        assert config.calculate_target_size((2048, 1080)) == (2048, 1080)
        assert config.calculate_target_size((1280, 720)) == (1280, 720)

    def test_custom_config(self) -> None:
        """Test custom configuration."""
        config = DiskProxyConfig(
            format=ProxyFormat.H264,
            resolution=ProxyResolution.QUARTER,
            h264_crf=28,
        )
        assert config.format == ProxyFormat.H264
        assert config.resolution == ProxyResolution.QUARTER
        assert config.h264_crf == 28

    def test_ffmpeg_args_prores(self) -> None:
        """Test FFmpeg args for ProRes."""
        config = DiskProxyConfig(format=ProxyFormat.PRORES_PROXY)
        args = config.get_ffmpeg_args((1920, 1080))
        assert '-c:v' in args
        assert 'prores_ks' in args
        assert '-profile:v' in args
        profile_index = args.index('-profile:v') + 1
        assert args[profile_index] == 'standard'
        # Check scale is present (may include extra options)
        assert any('scale=1920:1080' in arg for arg in args)

    def test_ffmpeg_args_h264(self) -> None:
        """Test FFmpeg args for H.264."""
        config = DiskProxyConfig(
            format=ProxyFormat.H264,
            h264_crf=23,
        )
        args = config.get_ffmpeg_args((1920, 1080))
        assert '-c:v' in args
        assert 'libx264' in args
        assert '-crf' in args
        assert '23' in args

    def test_ffmpeg_args_image_sequence(self) -> None:
        """Test FFmpeg args for image sequence."""
        config = DiskProxyConfig(
            format=ProxyFormat.IMAGE_SEQUENCE,
            image_format='png',
        )
        args = config.get_ffmpeg_args((1920, 1080))
        assert '-c:v' in args
        assert 'png' in args

    def test_output_filename_video(self) -> None:
        """Test output filename for video formats."""
        config = DiskProxyConfig(format=ProxyFormat.PRORES_PROXY)
        path = config.get_output_filename('/path/to/video.mp4', 'abc123')
        assert 'video_abc123.mov' in path
        assert '.cache/video_proxy' in path

    def test_output_filename_image_sequence(self) -> None:
        """Test output filename for image sequence."""
        config = DiskProxyConfig(format=ProxyFormat.IMAGE_SEQUENCE)
        path = config.get_output_filename('/path/to/video.mp4', 'abc123')
        assert 'video_abc123' in path
        assert '%06d.png' in path


class TestDiskProxyMediaValidation:
    """Realtime playback must reject truncated and full-resolution proxies."""

    def test_rejects_truncated_proxy_before_playback_reaches_eof(self) -> None:
        source = MagicMock(width=3840, height=2160, total_frames=120)
        proxy = MagicMock(width=1920, height=1080, total_frames=28)

        reason = get_disk_proxy_rejection_reason(source, proxy, (1920, 1080))

        assert reason == "truncated frame count 28 < 120"

    def test_rejects_full_resolution_proxy_for_4k_preview(self) -> None:
        source = MagicMock(width=3840, height=2160, total_frames=120)
        proxy = MagicMock(width=3840, height=2160, total_frames=120)

        reason = get_disk_proxy_rejection_reason(source, proxy, (1920, 1080))

        assert reason is not None
        assert "full-resolution proxy" in reason

    def test_accepts_complete_downscaled_proxy(self) -> None:
        source = MagicMock(width=3840, height=2160, total_frames=120)
        proxy = MagicMock(width=1920, height=1080, total_frames=120)

        assert get_disk_proxy_rejection_reason(source, proxy, (1920, 1080)) is None


class TestProxyKey:
    """Test proxy key generation."""

    def test_key_generation(self) -> None:
        """Test proxy key is generated consistently."""
        config = DiskProxyConfig()
        key1 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567890.0,
            source_size=1000000,
            proxy_config=config,
        )
        key2 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567890.0,
            source_size=1000000,
            proxy_config=config,
        )
        assert key1 == key2
        assert len(key1) == 32  # MD5 hex digest

    def test_key_changes_with_mtime(self) -> None:
        """Test key changes when mtime changes."""
        config = DiskProxyConfig()
        key1 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567890.0,
            source_size=1000000,
            proxy_config=config,
        )
        key2 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567891.0,  # Different mtime
            source_size=1000000,
            proxy_config=config,
        )
        assert key1 != key2

    def test_key_changes_with_config(self) -> None:
        """Test key changes when config changes."""
        config1 = DiskProxyConfig(resolution=ProxyResolution.HALF)
        config2 = DiskProxyConfig(resolution=ProxyResolution.QUARTER)
        key1 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567890.0,
            source_size=1000000,
            proxy_config=config1,
        )
        key2 = generate_proxy_key(
            source_path='/path/to/video.mp4',
            source_mtime=1234567890.0,
            source_size=1000000,
            proxy_config=config2,
        )
        assert key1 != key2


class TestGetSourceFileInfo:
    """Test get_source_file_info function."""

    def test_get_file_info(self) -> None:
        """Test getting file info."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'test content')
            temp_path = f.name

        try:
            mtime, size = get_source_file_info(temp_path)
            assert size == 12  # len(b'test content')
            assert mtime > 0
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self) -> None:
        """Test file not found error."""
        with pytest.raises(FileNotFoundError):
            get_source_file_info('/nonexistent/file.mp4')


class TestProxyManager:
    """Test ProxyManager."""

    def test_initialization(self) -> None:
        """Test manager initialization."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        assert manager.max_concurrent == 2
        assert len(manager._tasks) == 0

    def test_custom_max_concurrent(self) -> None:
        """Test custom max concurrent."""
        config = DiskProxyConfig()
        manager = ProxyManager(config, max_concurrent=4)
        assert manager.max_concurrent == 4

    def test_is_proxy_valid_no_proxy(self) -> None:
        """Test is_proxy_valid with no proxy."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        assert manager.is_proxy_valid('/path/to/video.mp4') is False

    def test_get_valid_proxy_path_no_proxy(self) -> None:
        """Test get_valid_proxy_path with no proxy."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        assert manager.get_valid_proxy_path('/path/to/video.mp4') is None

    def test_get_stats_empty(self) -> None:
        """Test get_stats with no tasks."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        stats = manager.get_stats()
        assert stats.total_assets == 0
        assert stats.assets_with_proxy == 0
        assert stats.pending_tasks == 0

    def test_cancel_all_tasks_empty(self) -> None:
        """Test cancel_all_tasks with no tasks."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        cancelled = manager.cancel_all_tasks()
        assert cancelled == 0

    def test_cleanup_expired_no_proxies(self) -> None:
        """Test cleanup with no proxies."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        cleaned = manager.cleanup_expired_proxies()
        assert cleaned == 0

    def test_cleanup_all_no_proxies(self) -> None:
        """Test cleanup_all with no proxies."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        cleaned = manager.cleanup_all_proxies()
        assert cleaned == 0

    def test_get_pending_tasks_empty(self) -> None:
        """Test get_pending_tasks with no tasks."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        tasks = manager.get_pending_tasks()
        assert len(tasks) == 0

    def test_get_task_by_asset_id_not_found(self) -> None:
        """Test get_task_by_asset_id with no matching task."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        task = manager.get_task_by_asset_id('nonexistent')
        assert task is None

    def test_get_task_status_not_found(self) -> None:
        """Test get_task_status with invalid task ID."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        status = manager.get_task_status('invalid-id')
        assert status is None

    def test_get_task_progress_not_found(self) -> None:
        """Test get_task_progress with invalid task ID."""
        config = DiskProxyConfig()
        manager = ProxyManager(config)
        progress = manager.get_task_progress('invalid-id')
        assert progress is None


class TestProxyTask:
    """Test ProxyTask dataclass."""

    def test_default_values(self) -> None:
        """Test default task values."""
        task = ProxyTask(
            task_id='test-001',
            asset_id='asset-001',
            source_path='/path/to/video.mp4',
        )
        assert task.status == 'queued'
        assert task.generator is None
        assert task.result is None
        assert task.progress.percent == 0.0
        assert task.created_at > 0
        assert task.started_at is None
        assert task.completed_at is None


class TestProxyStats:
    """Test ProxyStats dataclass."""

    def test_default_values(self) -> None:
        """Test default stats values."""
        stats = ProxyStats()
        assert stats.total_assets == 0
        assert stats.assets_with_proxy == 0
        assert stats.pending_tasks == 0
        assert stats.generating_tasks == 0
        assert stats.failed_tasks == 0
        assert stats.total_proxy_size_bytes == 0
        assert stats.cache_hit_rate == 0.0
