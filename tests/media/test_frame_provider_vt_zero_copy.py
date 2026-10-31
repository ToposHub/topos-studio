"""
Tests for VT zero-copy integration in the shared frame provider service.

覆盖（调色页/合成页共用的解码加速层）：
1. ClipSession.vt_source 附接策略（仅 4:2:0 HEVC + 硬件判定通过）
2. request_gpu_frame 的延迟导入帧路径与 CPU 回退
3. decode_lock 串行化并发 read_frame（PyAV 解码器线程安全边界）
4. VideoFrameNode 后台邻域预取（渲染线程只调度，worker 填充缓存）
"""

import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import sys
import pytest

from src.shared.video.frame_provider_service import (
    VideoFrameProviderService,
    ClipSession,
    FrameQuality,
)
from src.shared.media.gpu_frame import GpuDecodedFrame

pytestmark = pytest.mark.edit_core


_darwin_only_w01 = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 专属逻辑（VT/Metal）；W01 复跑确认在 Windows 为测试缺平台门"
)

def _meta(codec: str = 'hevc', pix_fmt: str = 'yuv420p',
          width: int = 3840, height: int = 2160, bit_depth: int = 8):
    meta = MagicMock()
    meta.codec_name = codec
    meta.pixel_format = pix_fmt
    meta.width = width
    meta.height = height
    meta.bit_depth = bit_depth
    return meta


def _service_with_clip(**session_kwargs) -> tuple[VideoFrameProviderService, ClipSession]:
    svc = VideoFrameProviderService()
    session = ClipSession(clip_id='c1', path='/fake/video.mov', **session_kwargs)
    session.is_open = True
    svc._clips['c1'] = session
    svc._path_to_clips['/fake/video.mov'] = ['c1']
    return svc, session


@_darwin_only_w01
class TestVtAttachPolicy :
    """_try_open_vt_source 策略门控"""

    def test_h264_hardware_attached(self):
        """H.264 4:2:0 也走 VT（8-bit 全线硬解；与剪辑页同策略）。

        合成页播放管线实测 4K H264 CPU planar 仅 ~15fps，VT 零拷贝 175fps+。
        """
        svc = VideoFrameProviderService()
        vt = MagicMock()
        vt.open.return_value = True
        with patch('src.shared.media.macos_vt.hardware_capability.playback_should_prefer_ffmpeg',
                   return_value=False), \
             patch('src.shared.media.macos_vt.source.MacOSVtZeroCopySource', return_value=vt):
            got = svc._try_open_vt_source('/v.mp4', _meta(codec='h264'))
        assert got is vt

    def test_h264_prefer_ffmpeg_not_attached(self):
        svc = VideoFrameProviderService()
        with patch('src.shared.media.macos_vt.hardware_capability.playback_should_prefer_ffmpeg',
                   return_value=True):
            assert svc._try_open_vt_source('/v.mp4', _meta(codec='h264')) is None

    def test_prores_not_attached(self):
        svc = VideoFrameProviderService()
        assert svc._try_open_vt_source('/v.mov', _meta(codec='prores', pix_fmt='yuv422p10le')) is None

    def test_hevc_422_not_attached(self):
        svc = VideoFrameProviderService()
        assert svc._try_open_vt_source('/v.mov', _meta(codec='hevc', pix_fmt='yuv422p10le')) is None

    def test_hevc_prefer_ffmpeg_not_attached(self):
        svc = VideoFrameProviderService()
        with patch('src.shared.media.macos_vt.hardware_capability.playback_should_prefer_ffmpeg',
                   return_value=True):
            assert svc._try_open_vt_source('/v.mov', _meta(codec='hevc')) is None

    def test_hevc_hardware_attached(self):
        svc = VideoFrameProviderService()
        vt = MagicMock()
        vt.open.return_value = True
        with patch('src.shared.media.macos_vt.hardware_capability.playback_should_prefer_ffmpeg',
                   return_value=False), \
             patch('src.shared.media.macos_vt.source.MacOSVtZeroCopySource', return_value=vt):
            got = svc._try_open_vt_source('/v.mov', _meta(codec='hevc'))
        assert got is vt
        vt.open.assert_called_once_with('/v.mov')

    def test_vt_open_failure_returns_none(self):
        svc = VideoFrameProviderService()
        vt = MagicMock()
        vt.open.return_value = False
        vt.fallback_reason = 'reason'
        with patch('src.shared.media.macos_vt.hardware_capability.playback_should_prefer_ffmpeg',
                   return_value=False), \
             patch('src.shared.media.macos_vt.source.MacOSVtZeroCopySource', return_value=vt):
            assert svc._try_open_vt_source('/v.mov', _meta(codec='hevc')) is None
        vt.close.assert_called_once()

    def test_session_defaults(self):
        session = ClipSession(clip_id='c', path='/v')
        assert session.vt_source is None
        assert isinstance(session.vt_lock, type(threading.Lock()))
        # 502b5290（VT 桥）改用 RLock：同线程重入（surface 提交内再取锁）
        assert isinstance(session.decode_lock, type(threading.RLock()))


def _gpu_frame(frame_index: int = 10) -> GpuDecodedFrame:
    return GpuDecodedFrame(
        frame_index=frame_index,
        pts=0,
        time_seconds=0.0,
        width=64,
        height=64,
        visible_rect=(0, 0, 64, 64),
        pixel_format='nv12',
        bit_depth=8,
        planes=(),
        zero_copy=False,
    )


class TestRequestGpuFrame:
    """request_gpu_frame 延迟导入路径与回退"""

    def test_vt_deferred_frame_returned(self):
        svc, session = _service_with_clip()
        frame = _gpu_frame(7)
        session.vt_source = MagicMock()
        session.vt_source.read_frame_gpu_deferred.return_value = frame

        got = svc.request_gpu_frame('c1', 7, quality='master')
        assert got is frame
        session.vt_source.read_frame_gpu_deferred.assert_called_once_with(7)

    def test_vt_exception_falls_back_to_cpu(self):
        svc, session = _service_with_clip()
        session.vt_source = MagicMock()
        session.vt_source.read_frame_gpu_deferred.side_effect = RuntimeError('boom')
        cpu_frame = MagicMock()
        with patch.object(svc, 'request_frame', return_value=cpu_frame) as rf:
            got = svc.request_gpu_frame('c1', 7, quality='proxy')
        assert got is cpu_frame
        rf.assert_called_once_with(clip_id='c1', frame_index=7, quality='proxy')

    def test_no_vt_source_uses_cpu(self):
        svc, session = _service_with_clip()
        assert session.vt_source is None
        cpu_frame = MagicMock()
        with patch.object(svc, 'request_frame', return_value=cpu_frame) as rf:
            assert svc.request_gpu_frame('c1', 3) is cpu_frame
        rf.assert_called_once()


class TestDecodeLockSerialization:
    """decode_lock 必须串行化同一 session 的 CPU read_frame"""

    def test_concurrent_reads_serialized(self):
        svc, session = _service_with_clip()
        active = 0
        max_active = 0
        lock_stats = threading.Lock()

        class FakeSource:
            def read_frame(self, frame_index):
                nonlocal active, max_active
                with lock_stats:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock_stats:
                    active -= 1
                return MagicMock()

        session.master_source = FakeSource()

        def worker():
            svc._decode_frame(session, 1, FrameQuality.MASTER)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert max_active == 1, f"read_frame 并发了：max_active={max_active}"

    def test_close_releases_vt_source(self):
        svc, session = _service_with_clip()
        vt = MagicMock()
        session.vt_source = vt
        svc._close_clip_sources(session)
        vt.close.assert_called_once()
        assert session.vt_source is None


class TestVideoFrameNodeBackgroundPreload:
    """VideoFrameNode 邻域预取移出渲染线程"""

    def _make_node(self):
        from src.features.composite.nodes.video_frame_node import VideoFrameNode

        node = VideoFrameNode('vid-1')
        node.parameters['total_frames'] = 30
        node._video_metadata = MagicMock()
        node._video_metadata.fps.to_fps.return_value = 24.0
        node._video_metadata.total_frames = 30
        return node

    def test_preload_is_scheduled_not_inline(self):
        node = self._make_node()
        calls = []

        def fake_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            calls.append((threading.get_ident(), frame_number, background))
            return np.zeros((4, 4, 4), dtype=np.float32)

        node._decode_frame = fake_decode
        t0 = time.perf_counter()
        node._preload_frames(10)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.05, "渲染线程内的 _preload_frames 应为纯调度，立即返回"

        deadline = time.perf_counter() + 2.0
        while not calls and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert calls, "后台预取线程未执行"
        bg_idents = {t for t, _, _ in calls}
        assert threading.get_ident() not in bg_idents, "解码不应在调度线程执行"
        assert all(bg for _, _, bg in calls), "后台解码必须带 background=True"

        node._stop_preload_thread(join=True)

    def test_preload_fills_cache_window(self):
        node = self._make_node()

        def fake_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            return np.full((4, 4, 4), frame_number / 30.0, dtype=np.float32)

        node._decode_frame = fake_decode
        node._preload_frames(10)
        deadline = time.perf_counter() + 2.0
        while len(node._frame_cache) < 11 and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert set(node._frame_cache.keys()) == set(range(5, 16))
        node._stop_preload_thread(join=True)

    def test_generation_invalidation_blocks_stale_store(self):
        node = self._make_node()

        started = threading.Event()

        def slow_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            started.set()
            time.sleep(0.15)
            return np.zeros((4, 4, 4), dtype=np.float32)

        node._decode_frame = slow_decode
        node._preload_frames(10)
        assert started.wait(1.0)
        node._clear_cache()  # 换源：代际 +1 且清缓存
        time.sleep(0.3)      # 等待 worker 的过期写入被拒绝
        assert node._frame_cache == {}, "换源后不得残留旧 clip 的帧"
        node._stop_preload_thread(join=True)

    def test_upload_frame_consumes_cache_without_decode(self):
        node = self._make_node()

        class FakeCtx:
            def texture(self, size, components, data=None, dtype='f4'):
                tex = MagicMock()
                tex.glo = 1
                return tex

        with patch.object(node, '_decode_frame') as dec:
            with node._cache_lock:
                node._frame_cache[5] = np.zeros((4, 4, 4), dtype=np.float32)
            assert node.upload_frame(FakeCtx(), 5, 'preview')
            dec.assert_not_called()

    def test_background_evict_does_not_touch_textures(self):
        node = self._make_node()
        with node._cache_lock:
            for f in (0, 6, 14, 21):
                node._frame_cache[f] = np.zeros((4, 4, 4), dtype=np.float32)
        tex = MagicMock()
        node._texture_cache[0] = tex
        node._evict_old_frames(10, 5, release_textures=False)
        assert 0 not in node._frame_cache and 21 not in node._frame_cache
        assert 6 in node._frame_cache and 14 in node._frame_cache
        tex.release.assert_not_called()
        assert 0 in node._texture_cache


class TestVideoFrameNodeHoldLastFrame:
    """预览持帧：当前帧异步解码，渲染线程不阻塞"""

    def _make_node(self):
        from src.features.composite.nodes.video_frame_node import VideoFrameNode

        node = VideoFrameNode('hold-1')
        node.parameters['total_frames'] = 30
        node._video_metadata = MagicMock()
        node._video_metadata.fps.to_fps.return_value = 24.0
        node._video_metadata.total_frames = 30
        return node

    def _fake_ctx(self):
        ctx = MagicMock()

        class Tex:
            glo = 1
            filter = None
            def write(self, data):
                pass
            def release(self):
                pass

        ctx.texture = lambda *a, **k: Tex()
        return ctx

    def test_preview_miss_schedules_async_and_notifies(self):
        import threading

        node = self._make_node()
        node._clip_id = 'fake-clip'  # 持帧路径要求服务/源可用
        notified = threading.Event()

        def fake_handler():
            notified.set()

        ctx_mock = MagicMock()
        ctx_mock.notify_frame_ready.side_effect = fake_handler
        # 非 VT 素材：CPU 解码结果是确定性的，立即放行上屏（VT 挂载的
        # 素材需连续两次未服务才放行，会多一次解码往返）
        ctx_mock.has_vt_source_for_node.return_value = False
        node._video_context = ctx_mock

        def fake_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            assert background, "持帧路径的当前帧解码必须在后台线程"
            # 模拟真实 _decode_frame：非 VT 素材解码后标记允许上屏
            node._vt_cpu_ok.add(frame_number)
            return np.full((4, 4, 4), frame_number / 30.0, dtype=np.float32)
        node._decode_frame = fake_decode
        t0 = time.perf_counter()
        ok = node.upload_frame(self._fake_ctx(), 7, 'preview')
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.05, "预览 miss 不应同步解码"
        assert ok is False, "无旧纹理时首帧 miss 返回 False"

        assert notified.wait(2.0), "解码完成未回调 notify_frame_ready"
        assert node.upload_frame(self._fake_ctx(), 7, 'preview') is True
        assert node._current_frame == 7
        node._stop_preload_thread(join=True)

    def test_preview_miss_holds_last_texture(self):
        node = self._make_node()
        node._clip_id = 'fake-clip'  # 持帧路径要求服务/源可用
        node._video_context = MagicMock()
        node._video_context.has_vt_source_for_node.return_value = False

        def fake_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            # 模拟真实 _decode_frame：非 VT 素材解码后标记允许上屏
            node._vt_cpu_ok.add(frame_number)
            return np.full((4, 4, 4), frame_number / 30.0, dtype=np.float32)

        node._decode_frame = fake_decode
        ctx = self._fake_ctx()
        with node._cache_lock:
            node._frame_cache[3] = np.zeros((4, 4, 4), dtype=np.float32)
            node._vt_cpu_ok.add(3)  # 种入缓存视作已放行的 CPU 帧
        assert node.upload_frame(ctx, 3, 'preview')
        assert node._current_frame == 3

        # 新位置 miss：应立即返回 True（持旧纹理）且 _current_frame 不变
        t0 = time.perf_counter()
        ok = node.upload_frame(ctx, 10, 'preview')
        assert (time.perf_counter() - t0) < 0.05
        assert ok is True, "有旧纹理时 miss 应持帧返回 True"
        assert node._current_frame == 3, "持帧期间当前帧号不应推进"

        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            with node._cache_lock:
                if 10 in node._frame_cache:
                    break
            time.sleep(0.01)
        assert node.upload_frame(ctx, 10, 'preview')
        assert node._current_frame == 10
        node._stop_preload_thread(join=True)

    def test_export_mode_decodes_inline(self):
        node = self._make_node()
        node._clip_id = 'c'  # 模拟可用服务
        with patch.object(node, '_decode_frame') as dec:
            dec.return_value = np.zeros((4, 4, 4), dtype=np.float32)
            assert node.upload_frame(self._fake_ctx(), 5, 'export')
            dec.assert_called_once()  # 同步解码，无调度往返
            args, kwargs = dec.call_args
            assert args[0] == 5

    def test_failed_position_falls_back_to_sync(self):
        node = self._make_node()
        node._clip_id = 'c'
        node._video_context = MagicMock()
        calls = []

        def flaky_decode(frame_number, render_mode='preview', *, background=False, scrub=None, sync_vt=False):
            calls.append(frame_number)
            if frame_number == 4 and calls.count(4) <= 2:
                return None  # frame 4 的前两次（后台）失败，其它帧正常
            return np.zeros((4, 4, 4), dtype=np.float32)

        node._decode_frame = flaky_decode
        # 两次独立调度 → frame 4 两次后台失败 → 标记失败位置
        node._preload_frames(4)
        deadline = time.perf_counter() + 2.0
        while calls.count(4) < 1 and time.perf_counter() < deadline:
            time.sleep(0.01)
        node._preload_frames(4)
        deadline = time.perf_counter() + 2.0
        while 4 not in node._failed_async_frames and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert 4 in node._failed_async_frames, "连续失败后应标记失败位置"

        # 标记后的预览 miss：回退同步解码（frame 4 第 3 次调用，inline 成功）
        assert node.upload_frame(self._fake_ctx(), 4, 'preview')
        assert calls.count(4) == 3
        node._stop_preload_thread(join=True)

    def test_run_preload_decodes_center_first(self):
        node = self._make_node()
        order = []

        def fake_decode(frame_number, render_mode='preview', *, background=False, scrub=None):
            order.append(frame_number)
            return np.zeros((4, 4, 4), dtype=np.float32)

        node._decode_frame = fake_decode
        node._run_preload(10, 'preview')
        assert order[0] == 10, "当前帧必须先于邻域解码"
        assert set(order) == set(range(5, 16))
