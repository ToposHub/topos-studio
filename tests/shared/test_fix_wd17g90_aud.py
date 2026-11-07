"""WD17-G90 修复验证：batch-347/348/349/350/351 shared-media（解码源 + macOS VT 桥）审计发现。

覆盖（每条含正例与反例）：
- AUD-34700  _extract_total_frames 时长换算（time_base 应作乘数）
- AUD-34701  _decode_to_target overshoot 必须抛 SeekError（不静默返回）
- AUD-34702  seek 会话续读推进 _main_resync_pts（会话耗尽不回卷重放）
- AUD-34704  planar 模式 nv12/p010 半平面显式去交织（Y/U/V 三平面）
- AUD-34805  FrameCache.put 更新已存在键同样过内存上限/驱逐检查
- AUD-34808  DecodeSurfaceLease.release 回调异常时状态仍落位（无双释放）
- AUD-34901  hwaccel 重开失败后恢复 CPU 容器（CPU 回退真实可用）
- AUD-34902  解码中途回退 CPU：关 hwaccel 容器 + seek 回当前帧号
- AUD-34903  _check_vaapi 对 /dev/dri 缺失/无权限不抛异常
- AUD-34904  QSV 探测不回退 ffmpeg 构建项检查；Linux 需 Intel DRM 设备
- AUD-35005  CVMetalVideoBridge.initialize 加锁双检（cache 恰好创建一次）
- AUD-35006  destroy/import 竞态：锁内 snapshot cache，绝不 NULL 进 FFI
- AUD-35007  MetalVideoFrameRing 按条目身份匹配（重复帧号不跨提交置完成）
- AUD-35101  packets() max_packets 判断先于取包（不多消费一个包）
- AUD-35120  IOSurfaceGLBridge.destroy 属主线程守卫（跨线程拒绝）
- AUD-35130  _read_next_decoded_pixel_buffer 持续喂包 + 队列排空（不当 EOF）

硬件路径全部用 monkeypatch/fake 模拟，不依赖真实 GPU/VT 设备。
"""

from __future__ import annotations

import fractions
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

# Metal 必须在任何 objc stub 安装前完成真实导入（PyObjC 元数据依赖）。
try:  # pragma: no cover - 平台差异
    import Metal  # noqa: F401

    HAS_METAL = True
except Exception:  # pragma: no cover - 非 macOS / 无 PyObjC
    HAS_METAL = False

import src.shared.media.hwaccel as hwaccel_mod
import src.shared.media.macos_vt.cv_metal_bridge as cvmb
import src.shared.media.macos_vt.iosurface_gl_bridge as iosb
from src.shared.media.errors import SeekError
from src.shared.media.ffmpeg_source import FFmpegMediaSource, _SEMIPLANAR_LAYOUTS
from src.shared.media.frame import DecodedFrame
from src.shared.media.frame_cache import FrameCache, calculate_frame_memory
from src.shared.media.frame_path import (
    DecodeSurfaceLease,
    DecodeSurfaceState,
)
from src.shared.media.gpu_frame import GpuFrameRelease
from src.shared.media.hwaccel import (
    FFmpegHwaccelSource,
    HardwareCapability,
    HardwareDecodeConfig,
    HardwareDecoder,
)
from src.shared.media.macos_vt.cv_metal_bridge import (
    CVMetalVideoBridge,
    MetalVideoFrameRing,
)
from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge
from src.shared.media.macos_vt.packet_demux import PacketDemuxer
from src.shared.media.macos_vt.source import MacOSVtZeroCopySource


# ======================================================================
# 共享 fake
# ======================================================================


class FakePtsFrame:
    """最小 av frame 替身（只有 pts）。"""

    def __init__(self, pts: int) -> None:
        self.pts = pts


def make_ffmpeg_source() -> FFmpegMediaSource:
    src = FFmpegMediaSource("/nonexistent-aud347xx.mp4")
    src._pts_to_frame = lambda pts: pts  # type: ignore[assignment]
    src._frame_to_pts = lambda idx: idx  # type: ignore[assignment]
    return src


# ======================================================================
# AUD-34700：total_frames 时长换算
# ======================================================================


class FakeStreamForFrames:
    def __init__(
        self,
        frames: int,
        duration: int,
        time_base: fractions.Fraction,
        average_rate: fractions.Fraction,
    ) -> None:
        self.frames = frames
        self.duration = duration
        self.time_base = time_base
        self.average_rate = average_rate


class TestAud34700TotalFrames:
    def test_duration_path_multiplies_time_base(self):
        """正例：tb=1/1000、duration=10000 ticks、24fps → 10s*24 = 240 帧。"""
        src = make_ffmpeg_source()
        stream = FakeStreamForFrames(
            frames=0,
            duration=10000,
            time_base=fractions.Fraction(1, 1000),
            average_rate=fractions.Fraction(24, 1),
        )
        assert src._extract_total_frames(stream, container=None) == 240

    def test_old_formula_would_inflate_by_1_over_tb_squared(self):
        """反例（回归防线）：旧公式 ÷time_base 会得到 2.4e8——断言远小于。"""
        src = make_ffmpeg_source()
        stream = FakeStreamForFrames(
            frames=0,
            duration=10000,
            time_base=fractions.Fraction(1, 1000),
            average_rate=fractions.Fraction(24, 1),
        )
        result = src._extract_total_frames(stream, container=None)
        assert result < 1000  # 旧值 240_000_000

    def test_stream_frames_wins_when_present(self):
        src = make_ffmpeg_source()
        stream = FakeStreamForFrames(
            frames=1234,
            duration=10_000_000,
            time_base=fractions.Fraction(1, 1000),
            average_rate=fractions.Fraction(24, 1),
        )
        assert src._extract_total_frames(stream, container=None) == 1234


# ======================================================================
# AUD-34701：_decode_to_target overshoot → SeekError
# ======================================================================


class TestAud34701OvershootRaises:
    def test_overshoot_raises_seek_error(self):
        """反例（修复点）：越过目标帧必须抛 SeekError，不得静默返回。"""
        src = make_ffmpeg_source()
        src._current_frame = 0
        src._frame_iterator = iter([FakePtsFrame(0), FakePtsFrame(1), FakePtsFrame(5)])
        with pytest.raises(SeekError) as exc_info:
            src._decode_to_target(target_frame=3, max_frames=100)
        assert exc_info.value.target_frame == 3
        assert exc_info.value.actual_frame == 5
        # 落地帧不设置——绝不缓存错误内容的帧
        assert src._pending_av_frame is None

    def test_exact_landing_caches_pending_frame(self):
        """正例：精确落在目标帧上——缓存落地帧，无异常。"""
        src = make_ffmpeg_source()
        src._current_frame = 0
        target = FakePtsFrame(3)
        src._frame_iterator = iter(
            [FakePtsFrame(0), FakePtsFrame(1), FakePtsFrame(2), target]
        )
        src._decode_to_target(target_frame=3, max_frames=100)
        assert src._pending_av_frame is target
        assert src._current_frame == 3

    def test_cannot_reach_target_raises(self):
        """反例：迭代器先耗尽且未到目标——按既有契约抛 SeekError。"""
        src = make_ffmpeg_source()
        src._current_frame = 0
        src._frame_iterator = iter([FakePtsFrame(0), FakePtsFrame(1)])
        with pytest.raises(SeekError):
            src._decode_to_target(target_frame=5, max_frames=100)


# ======================================================================
# AUD-34702：_main_resync_pts 随续读推进
# ======================================================================


class TestAud34702MainResyncPts:
    def test_pull_advances_resync_pts(self):
        """正例：每取一帧，回同步点推进到该帧 pts。"""
        src = make_ffmpeg_source()
        src._main_resync_pts = 50  # seek 落地点
        src._gop_seek_iterator = iter([FakePtsFrame(100), FakePtsFrame(200)])
        pulled = src._pull_next_frame_from_seek_session()
        assert pulled is not None and pulled.pts == 100
        assert src._main_resync_pts == 100
        pulled = src._pull_next_frame_from_seek_session()
        assert pulled is not None and pulled.pts == 200
        assert src._main_resync_pts == 200

    def test_exhaustion_keeps_last_served_pts_not_seek_landing(self):
        """反例（回归防线）：耗尽后回同步点停在最后已服务帧（200），
        而不是 seek 落地点（50）——否则主会话回卷重放已播帧。"""
        src = make_ffmpeg_source()
        src._main_resync_pts = 50
        src._gop_seek_iterator = iter([FakePtsFrame(100), FakePtsFrame(200)])
        src._pull_next_frame_from_seek_session()
        src._pull_next_frame_from_seek_session()
        assert src._pull_next_frame_from_seek_session() is None
        assert src._gop_seek_iterator is None
        assert src._main_resync_pts == 200
        assert src._main_resync_pts != 50

    def test_resync_main_session_discards_served_frame(self):
        """正例：主会话回同步 seek 到回同步点，丢弃已服务帧，
        迭代器定位于下一帧。"""
        src = make_ffmpeg_source()
        served = FakePtsFrame(200)
        following = FakePtsFrame(300)
        calls: list[int] = []

        fake_stream = SimpleNamespace(name="v")

        def fake_seek(pts, stream=None, any_frame=False):
            calls.append(pts)

        def fake_decode(stream):
            calls.append("decode")
            return iter([served, following])

        src._container = SimpleNamespace(seek=fake_seek, decode=fake_decode)
        src._video_stream = fake_stream
        src._main_resync_pts = 200
        src._resync_main_session_if_needed()
        assert 200 in calls and "decode" in calls
        assert src._main_resync_pts is None
        assert src._frame_iterator is not None
        assert next(src._frame_iterator) is following


# ======================================================================
# AUD-34704：半平面格式显式去交织
# ======================================================================


class FakeAVPlane(bytes):
    """带 line_size/height 元数据的 bytes（np.frombuffer 可直接读）。"""

    def __new__(cls, data: bytes, line_size: int, height: int):
        obj = super().__new__(cls, data)
        obj.line_size = line_size  # type: ignore[attr-defined]
        obj.height = height  # type: ignore[attr-defined]
        return obj


class FakeAVFrame:
    def __init__(self, width: int, height: int, planes: list) -> None:
        self.width = width
        self.height = height
        self.planes = planes
        self.format = SimpleNamespace(name="semiplanar")


class TestAud34704Semiplanar:
    def _nv12_frame(self) -> FakeAVFrame:
        width, height = 8, 4
        y_bytes = bytes([10] * (width * height))
        uv_row = bytes([1, 2] * (width // 2))  # U=1, V=2 交织
        uv_bytes = uv_row * (height // 2)
        y_plane = FakeAVPlane(y_bytes, line_size=width, height=height)
        uv_plane = FakeAVPlane(uv_bytes, line_size=width, height=height // 2)
        return FakeAVFrame(width, height, [y_plane, uv_plane])

    def test_nv12_extracted_as_three_planes(self):
        """正例：nv12 → (Y, U, V) 三个平面，色度正确去交织。"""
        src = make_ffmpeg_source()
        planes, infos = src._extract_planes_from_frame(self._nv12_frame(), "nv12")
        assert len(planes) == 3
        y, u, v = planes
        assert y.shape == (4, 8) and np.all(y == 10)
        assert u.shape == (2, 4) and np.all(u == 1)  # 交织残留会是 [1,2,1,2]
        assert v.shape == (2, 4) and np.all(v == 2)
        assert [i.component for i in infos] == ["Y", "U", "V"]
        assert [i.index for i in infos] == [0, 1, 2]
        assert infos[0].bit_depth == 8

    def test_nv21_swapped_order(self):
        """反例（顺序契约）：nv21 交织序为 VU——U/V 内容仍正确解出。"""
        width, height = 8, 4
        y_plane = FakeAVPlane(bytes([9] * (width * height)), width, height)
        uv_row = bytes([2, 1] * (width // 2))  # V=2 在偶数位
        uv_plane = FakeAVPlane(uv_row * (height // 2), width, height // 2)
        src = make_ffmpeg_source()
        planes, infos = src._extract_planes_from_frame(
            FakeAVFrame(width, height, [y_plane, uv_plane]), "nv21"
        )
        assert np.all(planes[1] == 1) and np.all(planes[2] == 2)

    def test_p010_16bit_samples(self):
        """正例：p010le（10-bit，uint16 容器）同样三平面去交织。"""
        width, height = 4, 2
        y = np.full((height, width), 100, dtype="<u2")
        uv = np.array([[30, 40, 30, 40]], dtype="<u2")  # 1 行 × 4 样本
        y_plane = FakeAVPlane(y.tobytes(), line_size=width * 2, height=height)
        uv_plane = FakeAVPlane(uv.tobytes(), line_size=width * 2, height=1)
        src = make_ffmpeg_source()
        planes, infos = src._extract_planes_from_frame(
            FakeAVFrame(width, height, [y_plane, uv_plane]), "p010le"
        )
        assert len(planes) == 3
        assert planes[0].dtype == np.uint16
        assert planes[1].shape == (1, 2) and np.all(planes[1] == 30)
        assert planes[2].shape == (1, 2) and np.all(planes[2] == 40)
        assert infos[0].bit_depth == 10

    def test_422_semiplanar_full_chroma_height(self):
        """正例：nv16（4:2:2）色度高度不减半。"""
        width, height = 4, 4
        y_plane = FakeAVPlane(bytes([7] * (width * height)), width, height)
        uv_row = bytes([1, 2] * (width // 2))
        uv_plane = FakeAVPlane(uv_row * height, width, height)
        src = make_ffmpeg_source()
        planes, _ = src._extract_planes_from_frame(
            FakeAVFrame(width, height, [y_plane, uv_plane]), "nv16"
        )
        assert planes[1].shape == (4, 2)
        assert planes[2].shape == (4, 2)

    def test_malformed_semiplanar_frame_fails_explicitly(self):
        """反例：声明半平面但只有 1 个缓冲平面——显式契约错误。"""
        width, height = 8, 4
        y_plane = FakeAVPlane(bytes([10] * (width * height)), width, height)
        src = make_ffmpeg_source()
        from src.shared.media.source import MediaDecodeError

        with pytest.raises(MediaDecodeError):
            src._extract_planes_from_frame(
                FakeAVFrame(width, height, [y_plane]), "nv12"
            )

    def test_layout_table_covers_declared_semiplanar_formats(self):
        """反例（表完备性）：_is_yuv_pixel_format 声明支持的半平面格式
        必须都有布局表条目。"""
        declared = {
            "nv12", "nv21", "p010le", "p010be", "p016le", "p016be",
            "nv16", "nv24", "p210le", "p210be", "p216le", "p216be",
        }
        assert declared <= set(_SEMIPLANAR_LAYOUTS)


# ======================================================================
# AUD-34805：FrameCache.put 更新路径过限额检查
# ======================================================================


def make_cache_frame(index: int, nbytes: int) -> DecodedFrame:
    # 2-D：DecodedFrame.__post_init__ 解包 data.shape[:2]
    return DecodedFrame(
        frame_index=index,
        pts=index,
        time_seconds=0.0,
        data=np.zeros((1, nbytes), dtype=np.uint8),
    )


class TestAud34805CachePutUpdate:
    def _cache(self) -> FrameCache:
        # 1MB 上限、条目充裕、可立即驱逐（idle=0）
        return FrameCache(max_memory_mb=1, max_entries=100, min_idle_time_for_eviction=0.0)

    def test_update_with_oversized_frame_rejected(self):
        """反例（修复点）：更新已存在键不能绕过内存上限。"""
        cache = self._cache()
        small = make_cache_frame(0, 1000)
        assert cache.put(small) is True

        big = make_cache_frame(0, 3 * 1024 * 1024)  # 同帧号、更大
        assert cache.put(big) is False  # 超限拒绝
        assert cache._current_memory <= cache._max_memory  # 内存不破限
        entry = cache.get_entry(0)
        assert entry is not None
        assert entry.frame is small  # 回滚保留旧帧
        assert calculate_frame_memory(big) > cache._max_memory  # 前置合理性

    def test_update_with_fitting_frame_succeeds(self):
        """正例：更新尺寸可容纳——成功且记账正确。"""
        cache = self._cache()
        assert cache.put(make_cache_frame(1, 1000)) is True
        medium = make_cache_frame(1, 100_000)
        assert cache.put(medium) is True
        assert cache.get_entry(1).frame is medium
        assert cache._current_memory == calculate_frame_memory(medium)

    def test_update_evicts_others_to_make_room(self):
        """正例：更新放不下时驱逐旧条目腾位（不再免检）。"""
        cache = FrameCache(
            max_memory_mb=1, max_entries=1000, min_idle_time_for_eviction=0.0
        )
        for i in range(4):
            assert cache.put(make_cache_frame(i, 200_000)) is True
        # 更新帧 0 为大帧：需驱逐其他帧腾位后成功
        assert cache.put(make_cache_frame(0, 700_000)) is True
        assert cache._current_memory <= cache._max_memory

    def test_force_update_still_allowed(self):
        """正例：force=True 语义保留（跳过检查强制写入）。"""
        cache = self._cache()
        cache.put(make_cache_frame(0, 1000))
        assert cache.put(make_cache_frame(0, 3 * 1024 * 1024), force=True) is True


# ======================================================================
# AUD-34808：DecodeSurfaceLease.release 状态落位
# ======================================================================


class TestAud34808ReleaseState:
    def _leased(self, callback) -> DecodeSurfaceLease:
        lease = DecodeSurfaceLease(
            backend="aud34808",
            memory_type=0,
            map_callback=lambda: ["view"],
            release_callback=callback,
        )
        lease.acquire()
        lease.map()
        lease.unmap()
        return lease

    def test_callback_exception_still_marks_released(self):
        """正例（修复点）：回调抛异常，状态仍落位 RELEASED。"""
        calls: list[int] = []

        def bad_release() -> None:
            calls.append(1)
            raise ValueError("release failed")

        lease = self._leased(bad_release)
        with pytest.raises(ValueError, match="release failed"):
            lease.release()
        assert lease.state is DecodeSurfaceState.RELEASED

        # 重试 release：状态已 RELEASED → 幂等返回，不再调用回调
        lease.release()
        assert calls == [1]  # 双释放防护：回调只执行一次

    def test_successful_release_idempotent(self):
        """正例（对照）：正常释放 + 二次调用幂等。"""
        calls: list[int] = []
        lease = self._leased(lambda: calls.append(1))
        lease.release()
        assert lease.state is DecodeSurfaceState.RELEASED
        lease.release()
        assert calls == [1]


# ======================================================================
# AUD-34901 / AUD-34902：hwaccel 容器重建
# ======================================================================


class FakeHWStream:
    def __init__(self, name: str = "h264") -> None:
        self.codec_context = SimpleNamespace(name=name, thread_count=0, options={})
        self.thread_type = None


class FakeHWContainer:
    def __init__(self, streams: list | None = None) -> None:
        self.streams = SimpleNamespace(video=streams if streams else [FakeHWStream()])
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def decode(self, stream):  # pragma: no cover - 本测试未走完整解码
        return iter([])


@pytest.fixture()
def fake_av(monkeypatch: pytest.MonkeyPatch):
    """替换 sys.modules['av']：hwaccel 重开抛错、CPU 重开成功。"""
    cpu_container = FakeHWContainer()
    opened: list[dict] = []

    class _HWDeviceType:
        videotoolbox = "vt"
        cuda = "cuda"
        vaapi = "vaapi"
        qsv = "qsv"
        d3d11va = "d3d11va"

    class _HWAccel:
        def __init__(self, device_type):
            self.device_type = device_type

    def fake_open(path, hwaccel=None):
        opened.append({"path": path, "hwaccel": hwaccel})
        if hwaccel is not None:
            raise RuntimeError("hwaccel device lost")
        return cpu_container

    av_mod = types.ModuleType("av")
    av_mod.open = fake_open
    codec_mod = types.ModuleType("av.codec")
    hwaccel_mod_ns = types.ModuleType("av.codec.hwaccel")
    hwaccel_mod_ns.HWAccel = _HWAccel
    hwaccel_mod_ns.HWDeviceType = _HWDeviceType
    codec_mod.hwaccel = hwaccel_mod_ns
    av_mod.codec = codec_mod
    monkeypatch.setitem(sys.modules, "av", av_mod)
    monkeypatch.setitem(sys.modules, "av.codec", codec_mod)
    monkeypatch.setitem(sys.modules, "av.codec.hwaccel", hwaccel_mod_ns)
    return SimpleNamespace(cpu_container=cpu_container, opened=opened)


class TestAud34901HwaccelReopenFallback:
    def test_configure_failure_recovers_cpu_container(self, fake_av):
        """正例（修复点）：hwaccel 重开失败后 CPU 容器被恢复——
        而不是 container=None + _is_open=True 的假开状态。"""
        src = FFmpegHwaccelSource("/aud34901.mp4")
        old_container = FakeHWContainer()
        src._is_open = True
        src._container = old_container
        src._video_stream = FakeHWStream()
        src._current_frame = 0

        with pytest.raises(RuntimeError, match="hwaccel device lost"):
            src._configure_hwaccel(HardwareDecoder.VIDEOTOOLBOX)

        assert old_container.closed  # 旧容器已关
        assert src._container is fake_av.cpu_container  # CPU 容器已恢复
        assert src._container.closed is False
        assert src._video_stream is not None
        assert src._is_open is True

    def test_cpu_read_after_configure_failure(self, fake_av, monkeypatch):
        """反例（端到端）：回退后读取不再抛
        RuntimeError("Container or video stream not initialized")。"""
        src = FFmpegHwaccelSource("/aud34901.mp4")
        src._is_open = True
        src._container = FakeHWContainer()
        src._video_stream = FakeHWStream()
        src._current_frame = 0
        src._metadata = SimpleNamespace(
            codec_name="h264", total_frames=None
        )

        with pytest.raises(RuntimeError):
            src._configure_hwaccel(HardwareDecoder.VIDEOTOOLBOX)
        assert src._container is fake_av.cpu_container

        # 模拟 _try_enable_hwaccel 的回退决策
        src._using_hwaccel = False
        sentinel = SimpleNamespace(name="cpu-frame")
        monkeypatch.setattr(
            FFmpegMediaSource, "read_next_frame", lambda self: sentinel
        )
        assert src.read_next_frame() is sentinel  # 容器/流校验通过

    def test_try_enable_hwaccel_end_to_end(self, fake_av, monkeypatch):
        """反例（端到端）：_try_enable_hwaccel 捕获失败宣称回退 CPU，
        且容器确实可用（旧行为是假回退）。"""
        src = FFmpegHwaccelSource(
            "/aud34901.mp4",
            config=HardwareDecodeConfig(decoder=HardwareDecoder.VIDEOTOOLBOX),
        )
        src._is_open = True
        src._container = FakeHWContainer()
        src._video_stream = FakeHWStream()
        src._current_frame = 0
        src._metadata = SimpleNamespace(codec_name="h264", total_frames=None)

        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability,
            "check_codec_support",
            staticmethod(lambda decoder, codec: True),
        )
        # W01：Windows 上 detect_available_decoders 不含 videotoolbox，
        # _try_enable_hwaccel 在能力门就早退（不动容器）——为走到被测的
        # "配置失败→CPU 重开"分支，与 macOS 同形地伪造解码器可用。
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability,
            "detect_available_decoders",
            staticmethod(lambda: {hwaccel_mod.HardwareDecoder.VIDEOTOOLBOX}),
        )
        src._try_enable_hwaccel()
        assert src._using_hwaccel is False
        assert src._container is fake_av.cpu_container
        assert src._is_open is True


class TestAud34902MidDecodeFallback:
    def _source(self) -> FFmpegHwaccelSource:
        src = FFmpegHwaccelSource("/aud34902.mp4")
        src._is_open = True
        src._container = FakeHWContainer()
        src._video_stream = FakeHWStream()
        src._current_frame = 5
        src._using_hwaccel = True
        src._metadata = SimpleNamespace(codec_name="h264", total_frames=None)
        src._pts_to_frame = lambda pts: pts  # type: ignore[assignment]
        src._frame_to_pts = lambda idx: idx  # type: ignore[assignment]
        return src

    def test_fallback_reopens_cpu_and_reseeks(self, monkeypatch):
        """正例（修复点）：中途失败 → 关 hwaccel 容器 + CPU 重开 +
        seek 回失败时正在交付的帧号，而非只翻标志位丢迭代器。"""
        src = self._source()
        hw_container = src._container
        reopen_calls: list[dict] = []
        sentinel = SimpleNamespace(name="cpu-frame")

        def fake_decode(stream):
            return iter([FakePtsFrame(5)])

        hw_container.decode = fake_decode

        def failing_convert(frame):
            raise RuntimeError("gpu decode died")

        def fake_reopen(resync_frame=None):
            reopen_calls.append({"resync_frame": resync_frame})
            src._container = FakeHWContainer()
            src._video_stream = FakeHWStream()

        monkeypatch.setattr(src, "_convert_hwaccel_frame", failing_convert)
        monkeypatch.setattr(src, "_reopen_cpu_container", fake_reopen)
        monkeypatch.setattr(
            FFmpegMediaSource, "read_next_frame", lambda self: sentinel
        )

        result = src.read_next_frame()
        assert result is sentinel
        assert src._using_hwaccel is False  # 后续读取走 CPU
        assert reopen_calls == [{"resync_frame": 5}]  # seek 回当前帧号
        assert src._container is not hw_container  # 不再在 hwaccel 容器上解码

    def test_reopen_cpu_container_purges_stale_state(self, fake_av):
        """正例：_reopen_cpu_container 清掉迭代器/落地帧/seek 会话，
        并把 _current_frame 归零与容器头对齐（防 seek 短路错位）。"""
        src = self._source()
        src._pending_av_frame = FakePtsFrame(99)  # 残留陈旧落地帧
        src._gop_seek_iterator = iter([FakePtsFrame(98)])
        src._main_resync_pts = 98
        src._current_frame = 5

        src._reopen_cpu_container(resync_frame=0)

        assert src._pending_av_frame is None
        assert src._gop_seek_iterator is None
        assert src._main_resync_pts is None
        assert src._container is fake_av.cpu_container
        assert src._current_frame == 0  # 与流头一致

    def test_reopen_resyncs_to_served_frame(self, fake_av, monkeypatch):
        """正例：resync_frame=5 → 重开容器 seek 到帧 5 并缓存落地帧。"""
        src = self._source()
        src._current_frame = 5
        landed = FakePtsFrame(5)

        def fake_decode(stream):
            return iter([landed, FakePtsFrame(6)])

        fake_av.cpu_container.decode = fake_decode
        fake_av.cpu_container.seek = lambda pts, stream=None, any_frame=False: None

        # 跳过 GOP 索引构建（磁盘 IO），由 fake 容器承载重定位
        monkeypatch.setattr(src, "_ensure_gop_index", lambda: None)
        src._reopen_cpu_container(resync_frame=5)
        assert src._pending_av_frame is landed
        assert src._current_frame == 5

    def test_no_fallback_when_disabled(self, monkeypatch):
        """反例：fallback_to_cpu=False → 原样抛 MediaDecodeError。"""
        src = self._source()
        src._hwaccel_config = HardwareDecodeConfig(fallback_to_cpu=False)

        def fake_decode(stream):
            return iter([FakePtsFrame(5)])

        src._container.decode = fake_decode
        monkeypatch.setattr(
            src,
            "_convert_hwaccel_frame",
            lambda frame: (_ for _ in ()).throw(RuntimeError("gpu died")),
        )
        from src.shared.media.source import MediaDecodeError

        with pytest.raises(MediaDecodeError):
            src.read_next_frame()


# ======================================================================
# AUD-34903 / AUD-34904：能力探测加固
# ======================================================================


@pytest.fixture()
def capability_clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        hwaccel_mod.HardwareCapability, "_vaapi_available", None
    )
    monkeypatch.setattr(hwaccel_mod.HardwareCapability, "_qsv_available", None)
    yield


class TestAud34903VaapiListdir:
    def test_missing_dev_dri_returns_false(self, monkeypatch, capability_clean):
        """正例（修复点）：/dev/dri 不存在时按"无设备"处理，不抛异常。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.os,
            "listdir",
            lambda p: (_ for _ in ()).throw(FileNotFoundError(p)),
        )
        monkeypatch.setattr(
            hwaccel_mod, "_ffmpeg_hwaccels_output", lambda: ""
        )
        assert HardwareCapability._check_vaapi() is False

    def test_permission_error_returns_false(self, monkeypatch, capability_clean):
        """反例：权限不足同样不击穿探测。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.os,
            "listdir",
            lambda p: (_ for _ in ()).throw(PermissionError(p)),
        )
        monkeypatch.setattr(
            hwaccel_mod, "_ffmpeg_hwaccels_output", lambda: ""
        )
        assert HardwareCapability._check_vaapi() is False

    def test_render_node_present_returns_true(self, monkeypatch, capability_clean):
        """正例：有 renderD 设备 → True。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.os, "listdir", lambda p: ["card0", "renderD128"]
        )
        monkeypatch.setattr(
            hwaccel_mod, "_ffmpeg_hwaccels_output", lambda: ""
        )
        assert HardwareCapability._check_vaapi() is True


class TestAud34904QsvDetection:
    def test_darwin_ignores_ffmpeg_build_item(self, monkeypatch, capability_clean):
        """反例（修复点）：ffmpeg 构建含 'qsv' 不能宣称 macOS 有 QSV。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            hwaccel_mod,
            "_ffmpeg_hwaccels_output",
            lambda: "Hardware acceleration methods:\nqsv\nvideotoolbox",
        )
        assert HardwareCapability._check_qsv() is False

    def test_linux_requires_intel_gpu(self, monkeypatch, capability_clean):
        """正例：Linux 上 VAAPI 可用 + Intel DRM 设备 → QSV 可用。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability, "_check_vaapi", staticmethod(lambda: True)
        )
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability,
            "_has_intel_drm_device",
            staticmethod(lambda: True),
        )
        monkeypatch.setattr(
            hwaccel_mod, "_ffmpeg_hwaccels_output", lambda: ""
        )
        assert HardwareCapability._check_qsv() is True

    def test_linux_amd_gpu_no_qsv(self, monkeypatch, capability_clean):
        """反例：AMD 平台 VAAPI 可用但无 Intel 设备 → QSV 不可用。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability, "_check_vaapi", staticmethod(lambda: True)
        )
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability,
            "_has_intel_drm_device",
            staticmethod(lambda: False),
        )
        monkeypatch.setattr(
            hwaccel_mod,
            "_ffmpeg_hwaccels_output",
            lambda: "Hardware acceleration methods:\nqsv\nvaapi",
        )
        assert HardwareCapability._check_qsv() is False

    def test_linux_without_vaapi_no_qsv(self, monkeypatch, capability_clean):
        """反例：无 VAAPI（即便 ffmpeg 构建含 qsv）→ QSV 不可用。"""
        monkeypatch.setattr(hwaccel_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            hwaccel_mod.HardwareCapability, "_check_vaapi", staticmethod(lambda: False)
        )
        monkeypatch.setattr(
            hwaccel_mod,
            "_ffmpeg_hwaccels_output",
            lambda: "Hardware acceleration methods:\nqsv",
        )
        assert HardwareCapability._check_qsv() is False

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="Intel DRM 探测走 sysfs /dev/drm（AUD-34904 为 Linux QSV 机制）；其它平台产品侧平台门先返回 False")
    def test_intel_drm_device_vendor_check(self, monkeypatch, capability_clean):
        """正例：sysfs PCI vendor 0x8086 = Intel → True；AMD → False。"""
        real_listdir = hwaccel_mod.os.listdir

        def fake_listdir(path):
            if str(path) == "/sys/class/drm":
                return ["card0", "renderD128"]
            return real_listdir(path)

        monkeypatch.setattr(hwaccel_mod.os, "listdir", fake_listdir)
        # 只对 sysfs vendor 文件伪造内容
        import pathlib

        real_read = pathlib.Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if str(self).startswith("/sys/class/drm/renderD128/device/vendor"):
                return "0x8086\n"
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", fake_read_text)
        assert HardwareCapability._has_intel_drm_device() is True

        def fake_read_text_amd(self, *args, **kwargs):
            if str(self).startswith("/sys/class/drm/renderD128/device/vendor"):
                return "0x1002\n"
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", fake_read_text_amd)
        assert HardwareCapability._has_intel_drm_device() is False


# ======================================================================
# AUD-35005 / AUD-35006 / AUD-35007：CVMetalVideoBridge 与 ring
# ======================================================================


class FakeCVLib:
    """CoreVideo FFI 替身：记录调用参数与 cache 指针。"""

    def __init__(self) -> None:
        self.cache_creates = 0
        self.cache_pointers: list[int] = []
        self.tex_from_image_calls: list[dict] = []
        self.ffi_block_event: threading.Event | None = None

    def CVMetalTextureCacheCreate(self, a, b, dev, d, out):
        self.cache_creates += 1
        ptr = 0x1234 + self.cache_creates
        self.cache_pointers.append(ptr)
        out._obj.value = ptr
        return 0

    def CVMetalTextureCacheCreateTextureFromImage(
        self, allocator, cache, pb, desc, fmt, w, h, plane, out
    ):
        self.tex_from_image_calls.append(
            {"cache": cache.value if cache else None, "plane": plane}
        )
        if self.ffi_block_event is not None:
            self.ffi_block_event.wait(timeout=5)
        out._obj.value = 0x2000 + int(plane)
        return 0

    def CVMetalTextureGetTexture(self, cv_tex):
        return 0x3001

    def CVPixelBufferGetWidth(self, pb):
        return 8

    def CVPixelBufferGetHeight(self, pb):
        return 4

    def CVPixelBufferGetPixelFormatType(self, pb):
        return int.from_bytes(b"420v", "big")


class FakeCFLib:
    def __init__(self) -> None:
        self.retains: list[int] = []
        self.releases: list[int] = []

    def CFRetain(self, ptr):
        self.retains.append(ptr.value if ptr else None)

    def CFRelease(self, ptr):
        self.releases.append(ptr.value if ptr else None)


@pytest.fixture()
def objc_stub(monkeypatch: pytest.MonkeyPatch):
    stub = types.ModuleType("objc")
    stub.pyobjc_id = lambda obj: 0xA001
    stub.objc_object = lambda c_void_p=None: SimpleNamespace(handle=c_void_p)
    monkeypatch.setitem(sys.modules, "objc", stub)
    return stub


@pytest.fixture()
def bridge_env(monkeypatch: pytest.MonkeyPatch, objc_stub):
    """初始化好的 bridge + FFI 记录器（Metal 可用时）。"""
    if not HAS_METAL:
        pytest.skip("PyObjC Metal 不可用")
    cv_lib = FakeCVLib()
    cf_lib = FakeCFLib()
    monkeypatch.setattr(cvmb, "_cv", cv_lib)
    monkeypatch.setattr(cvmb, "_cf", cf_lib)
    monkeypatch.setattr(cvmb, "cv_metal_bridge_available", lambda: (True, "ok"))

    class FakeDevice:
        pass

    metal_ctx = SimpleNamespace(device=FakeDevice(), device_name="fake")
    bridge = CVMetalVideoBridge(metal_ctx=metal_ctx, ring_size=4)
    assert bridge.initialize() is True
    return SimpleNamespace(bridge=bridge, cv=cv_lib, cf=cf_lib)


class TestAud35005InitializeLock:
    def test_concurrent_initialize_creates_cache_once(
        self, monkeypatch, objc_stub
    ):
        """正例（修复点）：并发首次 initialize 只创建一个
        CVMetalTextureCache（锁 + 双检）。"""
        if not HAS_METAL:
            pytest.skip("PyObjC Metal 不可用")
        cv_lib = FakeCVLib()
        cf_lib = FakeCFLib()
        monkeypatch.setattr(cvmb, "_cv", cv_lib)
        monkeypatch.setattr(cvmb, "_cf", cf_lib)
        monkeypatch.setattr(
            cvmb, "cv_metal_bridge_available", lambda: (True, "ok")
        )
        bridge = CVMetalVideoBridge(
            metal_ctx=SimpleNamespace(device=object(), device_name="fake"),
            ring_size=4,
        )

        barrier = threading.Barrier(8, timeout=5)

        def worker():
            barrier.wait()
            return bridge.initialize()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: worker(), range(8)))
        assert all(results) is True
        assert cv_lib.cache_creates == 1  # 恰好一次（旧行为会 >1 泄漏 CF 对象）
        assert bridge.is_initialized() is True

    def test_double_initialize_reuses_cache(self, bridge_env):
        """正例：已初始化后再次 initialize 复用，不重建。"""
        assert bridge_env.bridge.initialize() is True
        assert bridge_env.cv.cache_creates == 1


class TestAud35006DestroyImportRace:
    def test_import_passes_valid_cache_to_ffi(self, bridge_env):
        """正例：导入时 FFI 收到的是非空 cache snapshot。"""
        result = bridge_env.bridge.import_pixel_buffer(0x9999, 0, "nv12", 8)
        assert result is not None
        planes, gpu_release = result
        assert len(planes) == 2
        assert bridge_env.cv.tex_from_image_calls
        for call in bridge_env.cv.tex_from_image_calls:
            assert call["cache"] is not None
            assert call["cache"] == bridge_env.cv.cache_pointers[0]

    def test_import_after_destroy_fails_closed(self, bridge_env, monkeypatch):
        """反例：导入已通过入口检查、但 cache 已被 destroy 换出为 None
        （竞态中间态）——锁内 snapshot 检查显式失败，绝不把 NULL cache
        送进 CoreVideo FFI。"""
        # 禁止 import 路径的惰性 initialize，并合成"已过检 + cache 已
        # 换出"的竞态中间态（旧实现会把 None 直接传给 FFI）
        monkeypatch.setattr(bridge_env.bridge, "initialize", lambda: False)
        bridge_env.bridge.destroy()
        bridge_env.bridge._initialized = True  # 模拟已通过入口检查
        calls_before = len(bridge_env.cv.tex_from_image_calls)
        result = bridge_env.bridge.import_pixel_buffer(0x9999, 0, "nv12", 8)
        assert result is None
        assert bridge_env.bridge.last_fallback_reason() == "destroyed_during_import"
        assert len(bridge_env.cv.tex_from_image_calls) == calls_before  # FFI 未被调用
        assert bridge_env.cf.releases  # cache 被 CFRelease

    def test_destroy_blocked_while_import_in_flight(self, bridge_env):
        """正例（竞态防线）：import 在锁内使用 cache 期间，destroy 的
        换出被阻塞——不会释放正在使用的 cache。"""
        release_ffi = threading.Event()
        bridge_env.cv.ffi_block_event = release_ffi
        import_done = threading.Event()
        import_result: list = []

        def run_import():
            import_result.append(
                bridge_env.bridge.import_pixel_buffer(0x9999, 0, "nv12", 8)
            )
            import_done.set()

        thread = threading.Thread(target=run_import)
        thread.start()

        destroy_finished: list = []

        def run_destroy():
            bridge_env.bridge.destroy()
            destroy_finished.append(True)

        destroyer = threading.Thread(target=run_destroy)
        destroyer.start()
        destroyer.join(timeout=0.3)
        # import 持锁期间 destroy 尚未完成（cache 仍在线）
        assert not destroy_finished
        assert bridge_env.bridge._cache is not None

        release_ffi.set()
        assert import_done.wait(timeout=5)
        thread.join(timeout=5)
        destroyer.join(timeout=5)
        assert destroy_finished == [True]
        assert import_result[0] is not None  # import 用 snapshot 安全完成
        assert bridge_env.bridge._cache is None


class TestAud35007RingIdentity:
    def _release(self, counter: dict, key: str) -> GpuFrameRelease:
        def _cb() -> None:
            counter[key] += 1

        return GpuFrameRelease(release_callback=_cb)

    def test_track_returns_entry_ids(self):
        """正例：track 返回条目 ID（int，真值）；满员返回 False。"""
        ring = MetalVideoFrameRing(max_in_flight=2)
        counter: dict = {}
        id1 = ring.track(0, self._release(counter, "a"))
        assert isinstance(id1, int) and id1 >= 1
        id2 = ring.track(0, self._release(counter, "b"))  # 重复帧号允许登记
        assert id2 != id1
        assert ring.track(2, self._release(counter, "c")) is False

    def test_duplicate_frame_index_no_cross_completion(self):
        """正例（修复点）：重复 frame_index 的两个条目分属两次提交，
        提交 1 的 completion 只完成条目 1——旧实现按帧号匹配会把条目 2
        一并置完成（GPU 尚未用完就提前释放）。"""
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter: dict = {"a": 0, "b": 0}
        entry_a = ring.track(7, self._release(counter, "a"))
        # 提交 1 只认领条目 a
        submission_a = ring.mark_submitted((7,))
        assert submission_a == (entry_a,)

        # seek 后同帧号重新导入（条目 b），提交 2 认领
        entry_b = ring.track(7, self._release(counter, "b"))
        submission_b = ring.mark_submitted((7,))
        assert submission_b == (entry_b,)
        assert entry_b != entry_a

        # 提交 1 的命令缓冲完成：只完成条目 a
        ring.on_gpu_complete(submission_a)
        states = {e["entry_id"]: e["completed"] for e in ring._entries}
        assert states[entry_a] is True
        assert states[entry_b] is False  # 未被跨提交提前置完成

        assert ring.release_completed() == 1
        assert counter == {"a": 1, "b": 0}  # b 的表面仍在 GPU 使用，未释放

        # 提交 2 完成后 b 才可释放
        ring.on_gpu_complete(submission_b)
        assert ring.release_completed() == 1
        assert counter == {"a": 1, "b": 1}

    def test_legacy_semantics_preserved(self):
        """反例（回归防线）：常规单帧流语义不变。"""
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter: dict = {"x": 0}
        ring.track(0, self._release(counter, "x"))
        selected = ring.mark_submitted()
        ring.on_gpu_complete(selected)
        assert ring.release_completed() == 1
        assert counter == {"x": 1}

    def test_failed_submission_unmarks(self):
        """正例：scheduler.submit 抛错 → 条目退回未提交，可再次认领。"""
        ring = MetalVideoFrameRing(max_in_flight=4)
        counter: dict = {"x": 0}
        ring.track(0, self._release(counter, "x"))

        class BadScheduler:
            def submit(self, *, on_complete):
                raise RuntimeError("encode failed")

        with pytest.raises(RuntimeError):
            ring.submit_pending(BadScheduler(), (0,))
        entry = ring._entries[0]
        assert entry["submitted"] is False

        # 退回后可被新提交认领并正常完成
        class OkScheduler:
            def submit(self, *, on_complete):
                return object()

        assert ring.submit_pending(OkScheduler(), (0,)) is not None
        ring.on_gpu_complete()
        assert ring.release_completed() == 1


# ======================================================================
# AUD-35101：packets() 限额守卫先于取包
# ======================================================================


class FakeDemuxPacket:
    """最小 CompressedPacket 输入替身（source/packet_demux 消费的字段）。"""

    def __init__(
        self,
        pts: int,
        dts: int | None = None,
        duration: int | None = 1,
        is_keyframe: bool = False,
        data: bytes = b"",
        buffer_ptr=None,
        buffer_size: int = 0,
    ) -> None:
        self.pts = pts
        self.dts = dts if dts is not None else pts
        self.duration = duration
        self.is_keyframe = is_keyframe
        self.data = data
        self.buffer_ptr = buffer_ptr
        self.buffer_size = buffer_size


class FakeDemuxContainer:
    """记录实际从 demuxer 拉取了多少个包（含被丢弃的）。"""

    def __init__(self, packets: list) -> None:
        self._packets = packets
        self.pulled = 0

    def demux(self, stream):
        def gen():
            for pkt in self._packets:
                self.pulled += 1
                yield pkt

        return gen()


class TestAud35101MaxPackets:
    def _demuxer(self, count: int) -> tuple[PacketDemuxer, FakeDemuxContainer]:
        import ctypes

        # Real compressed samples have a nonempty backing buffer. A zero-size
        # packet is PyAV's EOF flush marker and must never count as a sample.
        buffers = [ctypes.create_string_buffer(bytes([i])) for i in range(count)]
        packets = [
            FakeDemuxPacket(
                buffer_ptr=ctypes.addressof(buffers[i]), buffer_size=1, pts=i, dts=i,
                duration=1, is_keyframe=(i == 0),
            )
            for i in range(count)
        ]
        container = FakeDemuxContainer(packets)
        container.buffers = buffers
        demuxer = PacketDemuxer()
        demuxer._container = container
        demuxer._stream = object()
        return demuxer, container

    def test_max_packets_does_not_overconsume(self):
        """正例（修复点）：要 3 个包，demuxer 恰好被拉取 3 个
        （旧实现先取第 4 个再 break，永久丢一个压缩包）。"""
        demuxer, container = self._demuxer(5)
        out = list(demuxer.packets(max_packets=3))
        assert len(out) == 3
        assert [p.pts for p in out] == [0, 1, 2]
        assert container.pulled == 3

    def test_unlimited_reads_all(self):
        """正例：max_packets=None 读全部。"""
        demuxer, container = self._demuxer(5)
        out = list(demuxer.packets())
        assert len(out) == 5
        assert container.pulled == 5

    def test_max_packets_larger_than_stream(self):
        """反例：限额超过流长度——按流长为准，不多拉。"""
        demuxer, container = self._demuxer(2)
        out = list(demuxer.packets(max_packets=10))
        assert len(out) == 2
        assert container.pulled == 2

    def test_frame_index_tracks_yielded_packets(self):
        demuxer, _ = self._demuxer(5)
        list(demuxer.packets(max_packets=3))
        assert demuxer._frame_index == 3


# ======================================================================
# AUD-35120：IOSurfaceGLBridge.destroy 属主线程守卫
# ======================================================================


class FakeRingBuffer:
    def __init__(self, *args, **kwargs) -> None:
        self.destroyed = 0

    def initialize(self) -> bool:
        return True

    def destroy(self) -> None:
        self.destroyed += 1


class TestAud35120DestroyOwnerThread:
    def test_owner_thread_destroy_succeeds(self, monkeypatch):
        """正例：属主线程销毁正常执行。"""
        monkeypatch.setattr(
            iosb, "IOSurfaceGLTextureRingBuffer", FakeRingBuffer
        )
        bridge = IOSurfaceGLBridge(ring_size=2)
        assert bridge.initialize() is True
        ring = bridge._ring_buffer
        assert bridge.destroy() is True
        assert ring.destroyed == 1
        assert bridge.is_initialized is False

    def test_cross_thread_destroy_refused(self, monkeypatch):
        """反例（修复点）：非属主线程调用 destroy 被拒绝——
        不执行 glDeleteTextures（否则无上下文线程上静默泄漏）。"""
        monkeypatch.setattr(
            iosb, "IOSurfaceGLTextureRingBuffer", FakeRingBuffer
        )
        bridge = IOSurfaceGLBridge(ring_size=2)
        assert bridge.initialize() is True
        ring = bridge._ring_buffer

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(bridge.destroy).result(timeout=5)

        assert result is False
        assert ring.destroyed == 0  # GL 删除未执行
        assert bridge.is_initialized is True  # 状态未被误置为已销毁
        assert bridge.last_fallback_reason == "bridge_thread_mismatch"

        # 属主线程稍后仍可真正销毁（延迟安全路径）
        assert bridge.destroy() is True
        assert ring.destroyed == 1


# ======================================================================
# AUD-35130：_read_next_decoded_pixel_buffer 持续喂包
# ======================================================================


class FakeVtDecoded:
    def __init__(self, pts: int, pixel_buffer: object = 0x7B10) -> None:
        self.pts = pts
        self.pixel_buffer = pixel_buffer
        self.frame_index = None
        self.time_seconds = None


class FakeVTSession:
    def __init__(self, queued=(), packet_results=()) -> None:
        self.queue = list(queued)
        self.packet_results = list(packet_results)
        self.decode_calls: list = []
        self.get_timeouts: list = []

    def get_decoded_frame(self, timeout: float = 0.5):
        self.get_timeouts.append(timeout)
        return self.queue.pop(0) if self.queue else None

    def decode_packet(self, packet_data, pts=None, is_keyframe=False):
        self.decode_calls.append((pts, is_keyframe))
        return self.packet_results.pop(0) if self.packet_results else None


class FakeVTDemuxer:
    def __init__(self, packets=()) -> None:
        self.packets_list = list(packets)
        self.max_packets_calls: list = []
        self.yielded = 0

    def packets(self, max_packets=None):
        self.max_packets_calls.append(max_packets)
        budget = len(self.packets_list)
        if max_packets is not None:
            budget = min(max_packets, budget)
        for pkt in self.packets_list[:budget]:
            self.yielded += 1
            yield pkt


def make_vt_source(session, demuxer) -> MacOSVtZeroCopySource:
    source = MacOSVtZeroCopySource()
    source._is_open = True
    source._vt_session = session
    source._demuxer = demuxer
    source._current_frame = 0
    source._pts_to_frame = lambda pts: pts  # type: ignore[assignment]
    source._pts_to_seconds = lambda pts: float(pts) / 1000.0  # type: ignore[assignment]
    return source


class TestAud35130FeedUntilOutput:
    def test_reorder_delay_keeps_feeding(self):
        """正例（修复点）：首包"暂无输出"（重排/解码器延迟）继续喂包
        直到取出帧——旧实现单包提交把 None 当 EOF 提前终止。"""
        frame20 = FakeVtDecoded(20)
        session = FakeVTSession(
            queued=(), packet_results=[None, frame20]
        )
        demuxer = FakeVTDemuxer(
            packets=[FakeDemuxPacket(pts=10, dts=10, duration=1,
                                     buffer_ptr=None, buffer_size=0,
                                     is_keyframe=True),
                     FakeDemuxPacket(pts=20, dts=20, duration=1,
                                     buffer_ptr=None, buffer_size=0,
                                     is_keyframe=False)]
        )
        source = make_vt_source(session, demuxer)

        result = source._read_next_decoded_pixel_buffer()
        assert result is frame20
        assert len(session.decode_calls) == 2  # 两个包都提交了
        assert demuxer.yielded == 2  # 无丢包：取了多少用多少
        assert result.frame_index == 20  # finalize 补齐时间线元数据
        assert result.time_seconds == pytest.approx(0.02)

    def test_delayed_output_served_from_queue_without_new_packet(self):
        """正例：上一包的延迟输出在队列里——直接取出，不消费新包。"""
        frame15 = FakeVtDecoded(15)
        session = FakeVTSession(queued=[frame15])
        demuxer = FakeVTDemuxer()
        source = make_vt_source(session, demuxer)

        result = source._read_next_decoded_pixel_buffer()
        assert result is frame15
        assert session.decode_calls == []  # 未提交新包
        assert demuxer.max_packets_calls == []  # 未触碰 demuxer
        assert session.get_timeouts[0] == 0.0  # 非阻塞探测

    def test_demuxer_exhaustion_drains_tail(self):
        """正例：流耗尽后做终态排空（等在飞帧落地），尾帧不丢。"""
        tail = FakeVtDecoded(30)
        session = FakeVTSession(queued=[])
        timeouts: list[float] = []

        def fake_get(timeout: float = 0.5):
            timeouts.append(timeout)
            if timeout >= 0.2:  # 只有终态排空（阻塞等待）才给尾帧
                return tail
            return None

        session.get_decoded_frame = fake_get
        demuxer = FakeVTDemuxer(packets=[])
        source = make_vt_source(session, demuxer)

        result = source._read_next_decoded_pixel_buffer()
        assert result is tail
        assert timeouts == [0.0, pytest.approx(0.2)]  # 先非阻塞探测，后排空等待

    def test_true_eof_returns_none(self):
        """反例：无包、无队列、无在飞帧 → None（真 EOF 语义保留）。"""
        session = FakeVTSession(queued=[], packet_results=[])
        demuxer = FakeVTDemuxer(packets=[])
        source = make_vt_source(session, demuxer)
        assert source._read_next_decoded_pixel_buffer() is None
        assert session.get_timeouts == [0.0, pytest.approx(0.2)]

    def test_not_open_returns_none(self):
        source = MacOSVtZeroCopySource()
        assert source._read_next_decoded_pixel_buffer() is None

    def test_budget_bounded(self):
        """反例：坏流下连续 32 包无输出 → 返回 None（有界 stall 防护）。"""
        session = FakeVTSession(packet_results=[None] * 64)
        packets = [
            FakeDemuxPacket(pts=i, dts=i, duration=1, buffer_ptr=None,
                            buffer_size=0, is_keyframe=False)
            for i in range(64)
        ]
        demuxer = FakeVTDemuxer(packets=packets)
        source = make_vt_source(session, demuxer)
        result = source._read_next_decoded_pixel_buffer()
        assert result is None
        assert demuxer.max_packets_calls == [source._MAX_DECODE_PACKETS_PER_READ]
        assert demuxer.yielded == source._MAX_DECODE_PACKETS_PER_READ
