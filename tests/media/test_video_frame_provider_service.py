"""
Tests for VideoFrameProviderService (P10.1)

验证：
1. 所有接口有完整类型注解
2. 同一视频文件被多个页面引用时只打开一次
3. proxy/master 切换正确
4. 性能统计可查询
"""

import pytest
import tempfile
import os
from unittest.mock import Mock, MagicMock, patch

from src.shared.media.proxy import ProxyConfig, ProxyStrategy
from src.shared.media.source import MediaOpenError
from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.video.frame_provider_service import (
    VideoFrameProviderService,
    ClipSession,
    ClipInfo,
    FrameQuality,
    get_frame_provider_service,
    reset_frame_provider_service,
)

pytestmark = pytest.mark.edit_core


class TestFrameQuality:
    """测试 FrameQuality 枚举"""

    def test_from_string(self) -> None:
        """测试字符串转换"""
        assert FrameQuality.from_string('proxy') == FrameQuality.PROXY
        assert FrameQuality.from_string('master') == FrameQuality.MASTER
        assert FrameQuality.from_string('auto') == FrameQuality.AUTO
        assert FrameQuality.from_string('PROXY') == FrameQuality.PROXY  # 大小写不敏感

    def test_to_string(self) -> None:
        """测试转换为字符串"""
        assert FrameQuality.PROXY.to_string() == 'proxy'
        assert FrameQuality.MASTER.to_string() == 'master'
        assert FrameQuality.AUTO.to_string() == 'auto'


class TestClipSession:
    """测试 ClipSession"""

    def test_creation(self) -> None:
        """测试创建"""
        session = ClipSession(
            clip_id='test_clip',
            path='/path/to/video.mp4',
        )
        assert session.clip_id == 'test_clip'
        assert session.path == '/path/to/video.mp4'
        assert session.ref_count == 1
        assert not session.is_open

    def test_ref_count(self) -> None:
        """测试引用计数"""
        session = ClipSession(clip_id='test', path='/path.mp4')
        assert session.ref_count == 1

        session.add_ref()
        assert session.ref_count == 2

        session.release_ref()
        assert session.ref_count == 1

        session.release_ref()
        assert session.ref_count == 0
        assert session.should_cleanup()

    def test_touch(self) -> None:
        """测试更新时间戳"""
        import time
        session = ClipSession(clip_id='test', path='/path.mp4')
        old_accessed = session.last_accessed
        time.sleep(0.01)
        session.touch()
        assert session.last_accessed > old_accessed

    def test_cache_operations(self) -> None:
        """测试缓存操作"""
        session = ClipSession(
            clip_id='test',
            path='/path.mp4',
            max_proxy_cache=4,
            max_master_cache=2,
        )

        # 创建模拟帧
        mock_frame_proxy = Mock()
        mock_frame_proxy.frame_index = 0
        mock_frame_master = Mock()
        mock_frame_master.frame_index = 0

        # 缓存 proxy 帧
        session.cache_frame(0, mock_frame_proxy, FrameQuality.PROXY)
        assert session.get_cached_frame(0, FrameQuality.PROXY) == mock_frame_proxy

        # 缓存 master 帧
        session.cache_frame(0, mock_frame_master, FrameQuality.MASTER)
        assert session.get_cached_frame(0, FrameQuality.MASTER) == mock_frame_master

        # 缓存未命中
        assert session.get_cached_frame(99, FrameQuality.PROXY) is None

    def test_evict_old_frames(self) -> None:
        """测试旧帧清理"""
        session = ClipSession(
            clip_id='test',
            path='/path.mp4',
            max_proxy_cache=3,
            max_master_cache=2,
        )

        # 填充缓存
        for i in range(5):
            mock_frame = Mock()
            mock_frame.frame_index = i
            session.cache_frame(i, mock_frame, FrameQuality.PROXY)

        assert len(session.frame_cache_proxy) == 5

        # 清理（保留最近的 3 帧）
        session.evict_old_frames(4, FrameQuality.PROXY)

        assert len(session.frame_cache_proxy) == 3
        # 应该保留最近的帧（3, 4, 5）
        assert 4 in session.frame_cache_proxy
        assert 3 in session.frame_cache_proxy

    def test_record_request_stats(self) -> None:
        """测试记录请求统计"""
        session = ClipSession(clip_id='test', path='/path.mp4')

        session.record_request(FrameQuality.PROXY, cache_hit=True, decode_ms=1.0)
        session.record_request(FrameQuality.PROXY, cache_hit=False, decode_ms=10.0)
        session.record_request(FrameQuality.MASTER, cache_hit=False, decode_ms=20.0)

        assert session._stats_frames_requested == 3
        assert session._stats_proxy_requests == 2
        assert session._stats_master_requests == 1
        assert session._stats_cache_hits == 1
        assert session._stats_cache_misses == 2
        assert abs(session._stats_total_decode_ms - 31.0) < 0.01

    def test_get_stats(self) -> None:
        """测试获取统计"""
        session = ClipSession(clip_id='test', path='/path.mp4')
        session.is_open = True
        session.ref_count = 2

        session.record_request(FrameQuality.PROXY, cache_hit=True, decode_ms=1.0)

        stats = session.get_stats()
        assert stats['clip_id'] == 'test'
        assert stats['path'] == '/path.mp4'
        assert stats['ref_count'] == 2
        assert stats['is_open'] is True
        assert stats['frames_requested'] == 1
        assert stats['cache_hits'] == 1
        assert stats['cache_hit_rate'] == 1.0


class TestClipInfo:
    """测试 ClipInfo"""

    def test_from_metadata_none(self) -> None:
        """测试从 None metadata 创建"""
        info = ClipInfo.from_metadata(
            clip_id='test',
            path='/path.mp4',
            metadata=None,
        )
        assert info.clip_id == 'test'
        assert info.path == '/path.mp4'
        assert info.width == 0
        assert info.height == 0

    def test_from_metadata(self) -> None:
        """测试从 metadata 创建"""
        mock_metadata = Mock()
        mock_metadata.width = 1920
        mock_metadata.height = 1080
        mock_metadata.fps.to_fps.return_value = 24.0
        mock_metadata.total_frames = 240
        mock_metadata.duration = 10.0
        mock_metadata.bit_depth = 8
        mock_metadata.is_hdr = False
        mock_metadata.codec_name = 'h264'
        mock_metadata.color_primaries = 'bt709'
        mock_metadata.color_transfer = 'bt709'
        mock_metadata.color_matrix = 'bt709'

        info = ClipInfo.from_metadata(
            clip_id='test',
            path='/path.mp4',
            metadata=mock_metadata,
        )

        assert info.width == 1920
        assert info.height == 1080
        assert info.fps == 24.0
        assert info.total_frames == 240
        assert info.duration == 10.0
        assert info.codec_name == 'h264'

    def test_from_real_video_metadata_without_legacy_duration_fields(self) -> None:
        """ClipInfo 应兼容当前 VideoMetadata 的 duration_seconds / 无 is_hdr 字段。"""
        metadata = VideoMetadata(
            width=3840,
            height=2160,
            fps=Rational(25, 1),
            total_frames=250,
            codec_name='prores',
            codec_profile=None,
            pixel_format='yuv444p12le',
            bit_depth=12,
            color_primaries='bt709',
            color_transfer='bt709',
            color_matrix='bt709',
        )

        info = ClipInfo.from_metadata(
            clip_id='test',
            path='/path.mov',
            metadata=metadata,
        )

        assert info.fps == 25.0
        assert info.total_frames == 250
        assert info.duration == 10.0
        assert info.is_hdr is False
        assert info.codec_name == 'prores'


class TestVideoFrameProviderService:
    """测试 VideoFrameProviderService"""

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_initialization(self) -> None:
        """测试初始化"""
        service = VideoFrameProviderService()
        assert service.total_clips_opened == 0
        assert service.total_clips_closed == 0
        assert service.total_frames_served == 0
        assert service.cache_hit_rate == 0.0
        assert service.active_clip_count == 0

    def test_request_metal_frame_prefers_videotoolbox_texture_and_falls_back(self) -> None:
        service = VideoFrameProviderService()
        session = ClipSession(clip_id="metal_clip", path="/path/to/video.mp4")
        gpu_frame = Mock(name="metal_frame")
        session.vt_source = Mock()
        session.vt_source.read_frame_metal.return_value = gpu_frame
        service._clips[session.clip_id] = session

        assert service.request_metal_frame(session.clip_id, 12) is gpu_frame
        session.vt_source.read_frame_metal.assert_called_once_with(12)

        session.vt_source.read_frame_metal.return_value = None
        cpu_frame = Mock(name="cpu_frame")
        with patch.object(service, "request_frame", return_value=cpu_frame) as request:
            assert service.request_metal_frame(session.clip_id, 13) is cpu_frame
        request.assert_called_once_with(
            clip_id=session.clip_id, frame_index=13, quality="master")

    def test_open_clip_new(self) -> None:
        """测试打开新剪辑"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            # 模拟打开成功
            clip_id = service.open_clip('/path/to/video.mp4')

            assert clip_id.startswith('clip_')
            assert service.has_clip(clip_id)
            assert service.total_clips_opened == 1
            assert service.active_clip_count == 1

    def test_open_clip_wraps_open_failures_as_media_open_error(self) -> None:
        """打开源失败时应抛出 MediaOpenError，而不是错误模块导入失败。"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources', side_effect=RuntimeError("boom")):
            with pytest.raises(MediaOpenError):
                service.open_clip('/path/to/video.mp4')

    def test_calculate_proxy_size_for_default_auto(self) -> None:
        """共享帧服务应按 ProxyConfig 正确计算内存 proxy 尺寸。"""
        service = VideoFrameProviderService()
        metadata = MagicMock(width=3840, height=2160)

        assert service._calculate_proxy_size_for_metadata(metadata) == (1920, 1080)

    def test_calculate_proxy_size_for_custom_config(self) -> None:
        service = VideoFrameProviderService(
            proxy_config=ProxyConfig(
                strategy=ProxyStrategy.CUSTOM,
                max_resolution=(1280, 720),
            )
        )
        metadata = MagicMock(width=3840, height=2160)

        assert service._calculate_proxy_size_for_metadata(metadata) == (1280, 720)

    def test_should_preserve_proxy_planes_for_yuv_source(self) -> None:
        """YUV proxy 应保留 planar 数据，避免高位深素材走 CPU RGB 转换。"""
        service = VideoFrameProviderService()
        metadata = MagicMock(pixel_format='yuv444p12le')

        assert service._should_preserve_proxy_planes(metadata) is True

    def test_should_not_preserve_proxy_planes_for_rgb_source(self) -> None:
        service = VideoFrameProviderService()
        metadata = MagicMock(pixel_format='rgb24')

        assert service._should_preserve_proxy_planes(metadata) is False

    def test_create_memory_proxy_source_preserves_yuv_planes(self) -> None:
        service = VideoFrameProviderService()
        session = ClipSession(clip_id='clip_yuv', path='/path/to/video.mov')
        session.metadata = MagicMock(width=3840, height=2160, pixel_format='yuv444p12le')
        proxy_source = MagicMock()

        with patch('src.shared.media.factory.MediaSourceFactory.create', return_value=proxy_source) as mock_create:
            with patch('src.shared.media.factory.MediaSourceFactory.open_source'):
                service._create_memory_proxy_source(session)

        assert session.proxy_source is proxy_source
        assert mock_create.call_args.kwargs['target_size'] == (1920, 1080)
        assert mock_create.call_args.kwargs['preserve_planes'] is True

    def test_open_clip_can_mark_memory_proxy_deferred(self) -> None:
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            clip_id = service.open_clip('/path/to/video.mp4', defer_memory_proxy=True)

        session = service.get_clip_session(clip_id)
        assert session is not None
        assert session.defer_memory_proxy is True
        mock_open.assert_called_once()

    def test_open_clip_does_not_start_disk_proxy_generation_by_default(self) -> None:
        """Opening a timeline clip must not compete with playback by transcoding."""
        service = VideoFrameProviderService()
        proxy_info = MagicMock()
        proxy_info.needs_generation = True
        proxy_info.status.name = "MISSING"
        proxy_service = MagicMock()
        proxy_service.get_disk_proxy_info.return_value = proxy_info

        with patch(
            "src.shared.media.disk_proxy_service.get_disk_proxy_service",
            return_value=proxy_service,
        ):
            with patch.object(service, "_open_clip_sources"):
                service.open_clip("/path/to/video.mp4")

        proxy_service.generate_proxy.assert_not_called()

    def test_open_clip_sources_defers_memory_proxy_creation(self) -> None:
        service = VideoFrameProviderService()
        session = ClipSession(
            clip_id='clip_deferred',
            path='/path/to/video.mp4',
            defer_memory_proxy=True,
        )
        metadata = VideoMetadata(
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='h264',
            codec_profile=None,
            pixel_format='yuv420p',
            bit_depth=8,
        )
        master_source = MagicMock()
        master_source.probe.return_value = metadata

        with patch('os.path.isfile', return_value=True):
            with patch('src.shared.media.factory.MediaSourceFactory.create', return_value=master_source) as mock_create:
                with patch('src.shared.media.factory.MediaSourceFactory.open_source'):
                    with patch.object(service, '_schedule_deferred_memory_proxy_source') as mock_schedule:
                        service._open_clip_sources(session)

        assert session.is_open is True
        assert session.master_source is master_source
        assert session.proxy_source is None
        mock_schedule.assert_called_once_with('clip_deferred')
        assert mock_create.call_count == 1

    def test_open_clip_shared(self) -> None:
        """测试共享剪辑（同一文件不重复打开）"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            # 第一次打开
            clip_id1 = service.open_clip('/path/to/video.mp4')

            # 第二次打开同一文件
            clip_id2 = service.open_clip('/path/to/video.mp4')

            # 应该返回相同的 clip_id
            assert clip_id1 == clip_id2

            # 引用计数应该增加
            session = service.get_clip_session(clip_id1)
            assert session is not None
            assert session.ref_count == 2

            # 只打开了一次媒体源
            assert mock_open.call_count == 1

    def test_close_clip(self) -> None:
        """测试关闭剪辑"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            # 模拟打开成功，设置 is_open=True
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 验证剪辑已打开
        session = service.get_clip_session(clip_id)
        assert session is not None
        assert session.is_open is True
        assert session.ref_count == 1

        # 增加引用计数（模拟另一个消费者）
        session.add_ref()
        assert session.ref_count == 2

        # 第一次关闭（减少引用计数到 1）
        result = service.close_clip(clip_id)
        assert result is True
        assert service.active_clip_count == 1  # 仍然活跃（引用计数=1）
        assert service.has_clip(clip_id)

        # 第二次关闭（引用计数=0，真正关闭）
        result = service.close_clip(clip_id)
        assert result is True
        assert service.active_clip_count == 0
        assert not service.has_clip(clip_id)
        assert service.total_clips_closed == 1

    def test_close_clip_not_found(self) -> None:
        """测试关闭不存在的剪辑"""
        service = VideoFrameProviderService()
        result = service.close_clip('nonexistent_clip')
        assert result is False

    def test_request_frame_cache_hit(self) -> None:
        """测试帧请求 - 缓存命中"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 手动缓存一帧
        session = service.get_clip_session(clip_id)
        assert session is not None
        mock_frame = Mock()
        mock_frame.frame_index = 0
        session.cache_frame(0, mock_frame, FrameQuality.PROXY)

        # 请求帧（应该命中缓存）
        frame = service.request_frame(clip_id, 0, 'proxy')
        assert frame == mock_frame
        assert service.total_frames_served == 1
        assert service._total_cache_hits == 1

    def test_request_frame_cache_miss(self) -> None:
        """测试帧请求 - 缓存未命中"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 模拟解码
        mock_frame = Mock()
        mock_frame.frame_index = 0
        with patch.object(service, '_decode_frame', return_value=mock_frame):
            frame = service.request_frame(clip_id, 0, 'proxy')

        assert frame == mock_frame
        assert service.total_frames_served == 1
        assert service._total_cache_misses == 1

    def test_concurrent_same_frame_miss_is_coalesced(self) -> None:
        """同一素材的并发消费者不应为同一帧重复 seek/decode。"""
        import threading
        from types import SimpleNamespace

        service = VideoFrameProviderService()
        decode_started = threading.Event()
        release_decode = threading.Event()
        decoded = Mock()

        def read_frame(frame_index: int):
            assert frame_index == 7
            decode_started.set()
            assert release_decode.wait(timeout=2.0)
            return decoded

        source = MagicMock()
        source.read_frame.side_effect = read_frame
        session = ClipSession(
            clip_id="shared-clip",
            path="/path/to/video.mp4",
            master_source=source,
            metadata=SimpleNamespace(total_frames=0),
            is_open=True,
        )
        service._clips[session.clip_id] = session

        results: list[object] = []
        errors: list[BaseException] = []

        def request() -> None:
            try:
                results.append(service.request_frame(
                    session.clip_id, 7, "master"))
            except BaseException as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        first = threading.Thread(target=request)
        second = threading.Thread(target=request)
        first.start()
        assert decode_started.wait(timeout=2.0)
        second.start()
        release_decode.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        assert not errors
        assert results == [decoded, decoded]
        assert source.read_frame.call_count == 1
        assert service._total_cache_misses == 1
        assert service._total_cache_hits == 1

    def test_prefetch_rechecks_cache_before_decode(self) -> None:
        """前台先填缓存后，后台预取不得按旧 miss 列表重复解码。"""
        import threading
        from types import SimpleNamespace

        service = VideoFrameProviderService()
        session = ClipSession(
            clip_id="prefetch-clip",
            path="/path/to/video.mp4",
            metadata=SimpleNamespace(total_frames=0),
            is_open=True,
        )
        service._clips[session.clip_id] = session

        decode_started = threading.Event()
        release_decode = threading.Event()
        decoded = Mock()

        def decode(_session, _frame_index, _quality):
            decode_started.set()
            assert release_decode.wait(timeout=2.0)
            return decoded

        with patch.object(service, "_decode_frame", side_effect=decode) as mock_decode:
            prefetch = threading.Thread(
                target=service.prefetch,
                args=(session.clip_id, 7, 7, "master"),
            )
            prefetch.start()
            assert decode_started.wait(timeout=2.0)

            foreground_result: list[object] = []
            foreground = threading.Thread(
                target=lambda: foreground_result.append(
                    service.request_frame(session.clip_id, 7, "master")),
            )
            foreground.start()
            release_decode.set()
            prefetch.join(timeout=2.0)
            foreground.join(timeout=2.0)

            assert foreground_result == [decoded]
            assert mock_decode.call_count == 1

    def test_foreground_cache_hit_bypasses_prefetch_decode_lock(self) -> None:
        """前台缓存命中不得被持锁中的后台预取解码阻塞。

        回归锁定：把命中检查收进 decode_lock 后，命中请求曾要在预取
        解码（持锁中）之后排队，最坏等一整次 master 解码。
        """
        import threading
        from types import SimpleNamespace

        service = VideoFrameProviderService()
        session = ClipSession(
            clip_id="hit-clip",
            path="/path/to/video.mp4",
            metadata=SimpleNamespace(total_frames=0),
            is_open=True,
        )
        service._clips[session.clip_id] = session
        cached = Mock()
        session.cache_frame(7, cached, FrameQuality.MASTER)

        decode_started = threading.Event()
        release_decode = threading.Event()

        def decode(_session, _frame_index, _quality):
            decode_started.set()
            assert release_decode.wait(timeout=2.0)
            return Mock()

        with patch.object(service, "_decode_frame", side_effect=decode):
            prefetch = threading.Thread(
                target=service.prefetch,
                args=(session.clip_id, 8, 8, "master"),
            )
            prefetch.start()
            assert decode_started.wait(timeout=2.0)

            result: list[object] = []
            foreground = threading.Thread(
                target=lambda: result.append(
                    service.request_frame(session.clip_id, 7, "master")),
            )
            foreground.start()
            foreground.join(timeout=0.5)
            # 命中必须立即返回：预取仍持锁解帧 8 时不得等待
            assert result == [cached]
            release_decode.set()
            prefetch.join(timeout=2.0)

    def test_prefetch_yields_to_foreground_decode_waiter(self) -> None:
        """预取扫描在前台 miss 排队等锁时终止，不堆积后续帧解码。"""
        import threading
        import time as _time
        from types import SimpleNamespace

        service = VideoFrameProviderService()
        session = ClipSession(
            clip_id="yield-clip",
            path="/path/to/video.mp4",
            metadata=SimpleNamespace(total_frames=0),
            is_open=True,
        )
        service._clips[session.clip_id] = session

        decode_started = threading.Event()
        release_decode = threading.Event()

        def decode(_session, frame_index, _quality):
            if int(frame_index) == 10:
                decode_started.set()
                assert release_decode.wait(timeout=2.0)
            return Mock()

        with patch.object(service, "_decode_frame", side_effect=decode) as mock_decode:
            prefetch = threading.Thread(
                target=service.prefetch,
                args=(session.clip_id, 10, 12, "master"),
            )
            prefetch.start()
            assert decode_started.wait(timeout=2.0)

            foreground_result: list[object] = []
            foreground = threading.Thread(
                target=lambda: foreground_result.append(
                    service.request_frame(session.clip_id, 5, "master")),
            )
            foreground.start()
            # 等前台真正进入 decode_lock 等待（waiters=1）再放行预取
            deadline = _time.monotonic() + 2.0
            while session.decode_waiters == 0 and _time.monotonic() < deadline:
                _time.sleep(0.005)
            assert session.decode_waiters == 1
            release_decode.set()
            prefetch.join(timeout=2.0)
            foreground.join(timeout=2.0)

            assert foreground_result and foreground_result[0] is not None
            frames_decoded = [
                int(call.args[1]) for call in mock_decode.call_args_list
            ]
            assert 10 in frames_decoded   # 进行中的一帧解码完成
            assert 5 in frames_decoded    # 前台 miss 正常解码
            assert 11 not in frames_decoded  # 让路：后续帧不再解码
            assert 12 not in frames_decoded

    def test_request_frame_clip_not_found(self) -> None:
        """测试请求不存在的剪辑的帧"""
        service = VideoFrameProviderService()
        frame = service.request_frame('nonexistent', 0, 'proxy')
        assert frame is None

    def test_prefetch(self) -> None:
        """测试预取帧"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 模拟解码
        mock_frame = Mock()
        with patch.object(service, '_decode_frame', return_value=mock_frame):
            service.prefetch(clip_id, 0, 4, 'proxy')

        # 验证帧已缓存
        session = service.get_clip_session(clip_id)
        assert session is not None
        assert len(session.frame_cache_proxy) == 5

    def test_get_clip_info(self) -> None:
        """测试获取剪辑信息"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        info = service.get_clip_info(clip_id)
        assert info is not None
        assert info.clip_id == clip_id
        assert info.path == '/path/to/video.mp4'

    def test_get_clip_info_not_found(self) -> None:
        """测试获取不存在剪辑的信息"""
        service = VideoFrameProviderService()
        info = service.get_clip_info('nonexistent')
        assert info is None

    def test_get_stats(self) -> None:
        """测试获取统计信息"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 请求一些帧
        mock_frame = Mock()
        with patch.object(service, '_decode_frame', return_value=mock_frame):
            service.request_frame(clip_id, 0, 'proxy')
            service.request_frame(clip_id, 1, 'proxy')
            service.request_frame(clip_id, 0, 'proxy')  # 缓存命中

        stats = service.get_stats()

        assert 'global' in stats
        assert 'clips' in stats
        assert stats['global']['total_clips_opened'] == 1
        assert stats['global']['total_frames_served'] == 3
        assert stats['global']['total_cache_hits'] == 1
        assert stats['global']['total_cache_misses'] == 2
        assert len(stats['clips']) == 1

    def test_get_shared_clips_info(self) -> None:
        """测试获取共享剪辑信息"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id1 = service.open_clip('/path/to/video.mp4')
            clip_id2 = service.open_clip('/path/to/video.mp4')  # 共享
            clip_id3 = service.open_clip('/path/to/other.mp4')

        shared = service.get_shared_clips_info()

        assert '/path/to/video.mp4' in shared
        assert len(shared['/path/to/video.mp4']) == 1  # 只有一个 clip_id（共享）
        assert '/path/to/other.mp4' in shared

    def test_proxy_master_quality(self) -> None:
        """测试 proxy/master 质量切换"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        mock_proxy_frame = Mock()
        mock_master_frame = Mock()

        with patch.object(service, '_decode_frame') as mock_decode:
            mock_decode.side_effect = lambda s, f, q: (
                mock_proxy_frame if q == FrameQuality.PROXY
                else mock_master_frame
            )

            # 请求 proxy 帧
            proxy_frame = service.request_frame(clip_id, 0, 'proxy')
            assert proxy_frame == mock_proxy_frame

            # 请求 master 帧
            master_frame = service.request_frame(clip_id, 0, 'master')
            assert master_frame == mock_master_frame

    def test_shutdown(self) -> None:
        """测试关闭服务"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            service.open_clip('/path/to/video1.mp4')
            service.open_clip('/path/to/video2.mp4')

        service.shutdown()

        assert service.active_clip_count == 0
        assert service.total_clips_opened == 2  # 累计打开数不变

    def test_global_service_singleton(self) -> None:
        """测试全局服务单例"""
        reset_frame_provider_service()

        service1 = get_frame_provider_service()
        service2 = get_frame_provider_service()

        assert service1 is service2

    def test_reset_global_service(self) -> None:
        """测试重置全局服务"""
        service1 = get_frame_provider_service()
        reset_frame_provider_service()
        service2 = get_frame_provider_service()

        assert service1 is not service2


class TestEditFrameCoordinator:
    """测试 EditFrameCoordinator"""

    def test_import(self) -> None:
        """测试导入"""
        from src.features.edit.core.edit_frame_coordinator import (
            EditFrameCoordinator,
            ClipHandle,
            CoordinatorStats,
            create_edit_frame_coordinator,
        )
        assert EditFrameCoordinator is not None
        assert ClipHandle is not None
        assert CoordinatorStats is not None

    def test_coordinator_stats(self) -> None:
        """测试协调器统计"""
        from src.features.edit.core.edit_frame_coordinator import CoordinatorStats

        stats = CoordinatorStats()
        stats.record_request('proxy', cache_hit=True, decode_ms=1.0, wait_ms=1.0)
        stats.record_request('master', cache_hit=False, decode_ms=10.0, wait_ms=10.0)

        assert stats.total_frame_requests == 2
        assert stats.proxy_requests == 1
        assert stats.master_requests == 1
        assert stats.cache_hits == 1
        assert stats.cache_misses == 1

        stats_dict = stats.to_dict()
        assert 'total_frame_requests' in stats_dict
        assert 'cache_hit_rate' in stats_dict

    def test_clip_handle(self) -> None:
        """测试剪辑句柄"""
        from src.features.edit.core.edit_frame_coordinator import ClipHandle

        handle = ClipHandle(
            asset_id='asset1',
            clip_id='clip1',
            path='/path/to/video.mp4',
        )

        assert handle.asset_id == 'asset1'
        assert handle.ref_count == 1

        handle.add_ref()
        assert handle.ref_count == 2

        handle.release_ref()
        assert handle.ref_count == 1

        handle.release_ref()
        assert handle.should_cleanup()


class TestTypeAnnotations:
    """测试类型注解完整性"""

    def test_service_methods_have_annotations(self) -> None:
        """测试服务方法有类型注解"""
        import inspect

        service_class = VideoFrameProviderService
        methods_to_check = [
            'open_clip',
            'close_clip',
            'request_frame',
            'prefetch',
            'get_clip_info',
            'has_clip',
            'get_stats',
            'shutdown',
        ]

        for method_name in methods_to_check:
            method = getattr(service_class, method_name)
            annotations = method.__annotations__

            # 应该有返回类型注解
            assert 'return' in annotations, f"{method_name} missing return annotation"

    def test_coordinator_methods_have_annotations(self) -> None:
        """测试协调器方法有类型注解"""
        import inspect

        from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

        methods_to_check = [
            'add_clip',
            'remove_clip',
            'request_frame',
            'get_stats',
            'cleanup',
        ]

        for method_name in methods_to_check:
            method = getattr(EditFrameCoordinator, method_name)
            annotations = method.__annotations__

            # 应该有返回类型注解
            assert 'return' in annotations, f"{method_name} missing return annotation"


class TestIntegration:
    """集成测试"""

    def test_shared_clip_multiple_consumers(self) -> None:
        """测试多个消费者共享剪辑"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            # 消费者 1 打开剪辑
            clip_id1 = service.open_clip('/path/to/video.mp4')

            # 消费者 2 打开同一剪辑
            clip_id2 = service.open_clip('/path/to/video.mp4')

            # 应该返回相同的 clip_id
            assert clip_id1 == clip_id2

            # 引用计数应该为 2
            session = service.get_clip_session(clip_id1)
            assert session is not None
            assert session.ref_count == 2

            # 消费者 1 关闭
            service.close_clip(clip_id1)
            assert service.active_clip_count == 1

            # 消费者 2 关闭
            service.close_clip(clip_id2)
            assert service.active_clip_count == 0

    def test_proxy_master_cache_separation(self) -> None:
        """测试 proxy/master 缓存分离"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        session = service.get_clip_session(clip_id)
        assert session is not None

        # 缓存 proxy 帧
        mock_proxy = Mock()
        session.cache_frame(0, mock_proxy, FrameQuality.PROXY)

        # 缓存 master 帧
        mock_master = Mock()
        session.cache_frame(0, mock_master, FrameQuality.MASTER)

        # 验证缓存分离
        assert session.get_cached_frame(0, FrameQuality.PROXY) == mock_proxy
        assert session.get_cached_frame(0, FrameQuality.MASTER) == mock_master
        assert len(session.frame_cache_proxy) == 1
        assert len(session.frame_cache_master) == 1

    def test_performance_stats_tracking(self) -> None:
        """测试性能统计追踪"""
        service = VideoFrameProviderService()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        mock_frame = Mock()
        with patch.object(service, '_decode_frame', return_value=mock_frame):
            # 请求一些帧
            service.request_frame(clip_id, 0, 'proxy')
            service.request_frame(clip_id, 1, 'proxy')
            service.request_frame(clip_id, 0, 'proxy')  # 缓存命中
            service.request_frame(clip_id, 0, 'master')  # master 请求

        # 验证全局统计
        stats = service.get_stats()
        assert stats['global']['total_frames_served'] == 4
        assert stats['global']['total_cache_hits'] == 1
        assert stats['global']['total_cache_misses'] == 3

        # 验证 per-clip 统计
        assert len(stats['clips']) == 1
        clip_stat = stats['clips'][0]
        assert clip_stat['frames_requested'] == 4
        assert clip_stat['proxy_requests'] == 3
        assert clip_stat['master_requests'] == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
