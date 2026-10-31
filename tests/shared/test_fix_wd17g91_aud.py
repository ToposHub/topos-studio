"""WD17-G91 修复验证：macOS VideoToolbox 会话/压缩 + media 元数据/性能监控。

对应 REMEDIATION_PLAN.md 行 1299-1314 的 16 条审计发现：
- AUD-35200/35201  topos_surface（全局 IOSurface / 行距回查）
- AUD-35206/35207/35208  vt_compression（锁域 / Muxer 构造 / 丢包可观测）
- AUD-35301/35302/35303/35204  vt_session（CFRelease / _destroyed 复位 /
  pts 透传 / avcC 长度宽度）
- AUD-35312/35313/35314/35315  vt_zero_copy_probe（帧释放 / 早退清理 /
  空目录 / 相对导入）
- AUD-35400/35401  metadata（Rational.from_string / fps、SAR 正值）
- AUD-35404  performance_monitor（有界列表 / 锁外聚合）

全部用 monkeypatch 模拟硬件路径，不依赖真实 VideoToolbox/GL。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

import src.shared.media.macos_vt.topos_surface as ts_module
import src.shared.media.macos_vt.vt_compression as vc_module
import src.shared.media.macos_vt.vt_session as vts_module
import src.shared.media.macos_vt.vt_zero_copy_probe as probe_module
from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: F401
from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease, GpuPlaneResource
from src.shared.media.macos_vt.topos_surface import (
    ToposIOSurfacePool,
    ToposSurfaceUnavailable,
)
from src.shared.media.macos_vt.vt_compression import (
    VideoToolboxCompressionError,
    VideoToolboxCompressionSession,
    VideoToolboxPacketMuxer,
)
from src.shared.media.macos_vt.vt_session import (
    _CtypesCallbackContext,
    _CMTime,
    VtSession,
)
from src.shared.media.metadata import FPS_24, Rational, VideoMetadata
from src.shared.media.performance_monitor import VideoPerformanceMonitor


# ======================================================================
# AUD-35200 / AUD-35201 — ToposIOSurfacePool
# ======================================================================

class FakeIOSurfaceAPI:
    """记录 IOSurfaceCreate 选项并模拟每平面实际行距。"""

    kIOSurfaceWidth = "w"
    kIOSurfaceHeight = "h"
    kIOSurfacePlaneInfo = "planes"
    kIOSurfacePlaneWidth = "pw"
    kIOSurfacePlaneHeight = "ph"
    kIOSurfacePlaneBytesPerElement = "pbpe"
    kIOSurfacePlaneBytesPerRow = "pbpr"
    kIOSurfaceIsGlobal = "is_global"

    def __init__(self, row_pad: int = 64) -> None:
        self.create_options: list[dict] = []
        self.row_pad = row_pad

    def IOSurfaceCreate(self, options: dict) -> object:
        self.create_options.append(dict(options))
        return object()

    def IOSurfaceGetBytesPerRowOfPlane(self, surface: object, plane: int) -> int:
        plane_info = self.create_options[-1][self.kIOSurfacePlaneInfo]
        return plane_info[plane][self.kIOSurfacePlaneBytesPerRow] + self.row_pad


def _install_fake_iosurface(monkeypatch: pytest.MonkeyPatch, row_pad: int = 64
                            ) -> FakeIOSurfaceAPI:
    fake = FakeIOSurfaceAPI(row_pad=row_pad)
    monkeypatch.setattr(ts_module, "IOSurface", fake)
    monkeypatch.setattr(ts_module, "MACOS_TOPOS_SURFACE_AVAILABLE", True)
    return fake


def test_aud35200_surface_is_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """kIOSurfaceIsGlobal 不得出现在创建选项中（全系统暴露面）。"""
    fake = _install_fake_iosurface(monkeypatch)
    pool = ToposIOSurfacePool(64, 32, pixel_format=0, plane_count=3, ring_size=2)
    assert len(fake.create_options) == 2
    for options in fake.create_options:
        assert FakeIOSurfaceAPI.kIOSurfaceIsGlobal not in options
    pool.close()


def test_aud35201_actual_row_bytes_queried_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """slot 行距必须取 IOSurfaceGetBytesPerRowOfPlane 的实际值。"""
    fake = _install_fake_iosurface(monkeypatch, row_pad=64)
    pool = ToposIOSurfacePool(64, 32, pixel_format=0, plane_count=3, ring_size=1)
    requested = tuple(
        ts_module._aligned_row_bytes(w) for w in (64, 32, 32)
    )
    slot = pool._slots[0]
    assert slot.row_bytes == tuple(value + 64 for value in requested)
    assert all(
        actual > requested_
        for actual, requested_ in zip(slot.row_bytes, requested)
    )
    pool.close()


def test_aud35201_row_bytes_below_request_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回查值小于请求 stride 时必须 fail-loud（防止越界视图）。"""
    _install_fake_iosurface(monkeypatch, row_pad=-8)
    with pytest.raises(ToposSurfaceUnavailable, match="row bytes below"):
        ToposIOSurfacePool(64, 32, pixel_format=0, plane_count=3, ring_size=1)


# ======================================================================
# AUD-35206 / AUD-35208 — VideoToolboxCompressionSession
# ======================================================================

class FakeVTCompressLib:
    """替代 vt_compression._VT_LIB 的原生调用记录器。"""

    def __init__(self) -> None:
        self.encode_calls: list[dict] = []
        self.complete_calls: list[int] = []
        self.invalidate_calls: list[int] = []
        self.encode_status = 0
        self.encode_hook = None

    def VTCompressionSessionEncodeFrame(
        self, session, handle, pts, duration, props, ref_con, flags_out
    ) -> int:
        call = {
            "session": session.value if isinstance(session, ctypes.c_void_p) else session,
            "pts": int(pts.value),
            "duration": int(duration.value),
            "ref_con": ref_con,
        }
        self.encode_calls.append(call)
        if self.encode_hook is not None:
            self.encode_hook(call)
        return self.encode_status

    def VTCompressionSessionCompleteFrames(self, session, until) -> int:
        self.complete_calls.append(
            session.value if isinstance(session, ctypes.c_void_p) else session
        )
        return 0

    def VTCompressionSessionInvalidate(self, session) -> None:
        self.invalidate_calls.append(
            session.value if isinstance(session, ctypes.c_void_p) else session
        )


def _fake_vt_surface(width: int = 64, height: int = 32, handle: int = 0xDEADBEEF
                     ) -> GpuEncodeSurface:
    planes = tuple(
        GpuFramePlane(
            component=component,
            api="metal",
            texture_id=0,
            target="2d",
            width=width if component == "Y" else width // 2,
            height=height if component == "Y" else height // 2,
            resource=GpuPlaneResource(
                api="metal",
                native_handle=0,
                resource_format="r8unorm" if component == "Y" else "rg8unorm",
                width=width if component == "Y" else width // 2,
                height=height if component == "Y" else height // 2,
            ),
        )
        for component in ("Y", "UV")
    )
    return GpuEncodeSurface(
        GpuEncodeSurfaceDescriptor(
            backend="videotoolbox",
            pixel_format="nv12",
            width=width,
            height=height,
            bit_depth=8,
            device_identity="test",
            planes=planes,
            pts=0,
            native_handle=handle,
        ),
        GpuFrameRelease(),
        lambda _surface: None,
    )


def _make_compression_session(
    fake_vt: FakeVTCompressLib,
    on_packet=None,
) -> VideoToolboxCompressionSession:
    session = VideoToolboxCompressionSession.__new__(
        VideoToolboxCompressionSession
    )
    session.width = 64
    session.height = 32
    session.fps = 30
    session.codec = "h264"
    session._pixel_format = "nv12"
    session._on_packet = on_packet
    session._lock = threading.RLock()
    session._session_lock = threading.Lock()
    session._pending = {}
    session._next_submission_id = 1
    session._last_error = ""
    session._closed = False
    session._dropped_packets = 0
    session._session = ctypes.c_void_p(0x1234)
    session._callback = None
    session._ref_pointer = None
    session._ref_object = None
    return session


def test_aud35206_submit_after_close_never_calls_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeVTCompressLib()
    monkeypatch.setattr(vc_module, "_VT_LIB", fake)
    session = _make_compression_session(fake)
    session.close()

    assert len(fake.invalidate_calls) == 1
    assert session._session.value is None
    with pytest.raises(VideoToolboxCompressionError, match="session_closed"):
        session.submit(_fake_vt_surface())
    assert fake.encode_calls == []  # 已失效会话绝不能进入原生层


def test_aud35206_submit_snapshot_and_complete_under_session_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeVTCompressLib()
    monkeypatch.setattr(vc_module, "_VT_LIB", fake)
    session = _make_compression_session(fake)

    receipt = session.submit(_fake_vt_surface())
    assert receipt["queued"] is True
    assert fake.encode_calls[0]["session"] == 0x1234  # 稳定快照，非共享引用
    assert session.pending_count == 1

    session.complete()
    assert fake.complete_calls == [0x1234]

    # 模拟回调释放 pending，避免 close 的排空等待
    submission_id = fake.encode_calls[0]["ref_con"]
    assert int(submission_id.value) == 1
    session.close()
    assert len(fake.invalidate_calls) == 1
    assert session.pending_count == 0


def test_aud35206_concurrent_submit_close_no_invalid_session_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit 与 close 并发时，EncodeFrame 看到的会话永远有效。"""
    fake = FakeVTCompressLib()
    monkeypatch.setattr(vc_module, "_VT_LIB", fake)
    session = _make_compression_session(fake)

    # 让"提交成功"立即同步回调（sample_buffer=0 走丢包路径并释放 surface），
    # 从而 close() 的 pending 排空不会等待 10 秒。
    def immediate_callback(call: dict) -> None:
        session._handle_native_callback(
            0, int(call["ref_con"].value), 0, 0, 0
        )

    fake.encode_hook = immediate_callback

    errors: list[BaseException] = []
    stop = threading.Event()

    def submitter() -> None:
        index = 0
        while not stop.is_set() and index < 200:
            try:
                session.submit(_fake_vt_surface(handle=0x1000 + index))
            except VideoToolboxCompressionError as exc:
                # 只允许"会话已关"这一种受控失败
                if "session_closed" not in str(exc):
                    errors.append(exc)
            except BaseException as exc:  # pragma: no cover - fail loud
                errors.append(exc)
            index += 1

    thread = threading.Thread(target=submitter)
    thread.start()
    time.sleep(0.005)
    session.close()
    stop.set()
    thread.join(timeout=5.0)

    assert errors == []
    # 关键断言：每次原生调用拿到的都是有效会话指针（TOCTOU 消除）
    assert fake.encode_calls, "race window too small to exercise"
    assert all(call["session"] == 0x1234 for call in fake.encode_calls)
    assert len(fake.invalidate_calls) == 1


def test_aud35208_callback_drop_is_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回调丢包必须计数 + 记录 last_error，不再静默缺帧。"""
    fake = FakeVTCompressLib()
    monkeypatch.setattr(vc_module, "_VT_LIB", fake)
    packets: list = []
    session = _make_compression_session(fake, on_packet=packets.append)

    surface = _fake_vt_surface()
    session.submit(surface)
    assert session.pending_count == 1

    # status != 0 → 丢包
    session._handle_native_callback(0, 1, -12900, 0, 0)
    assert session.dropped_packets == 1
    assert "videotoolbox_callback_status" in session.last_error
    assert packets == []  # on_packet 未被调用
    assert surface.released  # surface 已释放

    # status == 0 但 sample buffer 缺失 → 丢包
    session.submit(_fake_vt_surface())
    session._handle_native_callback(0, 2, 0, 0, 0)
    assert session.dropped_packets == 2
    assert "sample_buffer_missing" in session.last_error


# ======================================================================
# AUD-35207 — VideoToolboxPacketMuxer 构造泄漏
# ======================================================================

class FakeAVContainer:
    def __init__(self, add_stream_raises: bool = False) -> None:
        self.closed = False
        self.add_stream_raises = add_stream_raises

    def add_stream(self, *args, **kwargs):
        if self.add_stream_raises:
            raise RuntimeError("add_stream boom")
        return SimpleNamespace(width=0, height=0, pix_fmt="", codec_context=None)

    def close(self) -> None:
        self.closed = True


def _install_fake_av(monkeypatch: pytest.MonkeyPatch, container: FakeAVContainer
                     ) -> types.ModuleType:
    fake_av = types.ModuleType("av")
    fake_av.open = lambda *args, **kwargs: container
    monkeypatch.setitem(sys.modules, "av", fake_av)
    return fake_av


def test_aud35207_invalid_pixel_format_rejected_before_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    container = FakeAVContainer()
    _install_fake_av(monkeypatch, container)
    with pytest.raises(VideoToolboxCompressionError, match="pixel_format"):
        VideoToolboxPacketMuxer(
            str(tmp_path / "out.mp4"), width=64, height=32, pixel_format="rgb24"
        )
    # av.open 未被调用：容器与输出文件句柄零泄漏
    assert container.closed is False  # 从未打开


def test_aud35207_add_stream_failure_closes_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    container = FakeAVContainer(add_stream_raises=True)
    _install_fake_av(monkeypatch, container)
    with pytest.raises(RuntimeError, match="add_stream boom"):
        VideoToolboxPacketMuxer(
            str(tmp_path / "out.mp4"),
            width=64,
            height=32,
            pixel_format="nv12",
        )
    assert container.closed is True  # 已开容器在失败路径被关闭


# ======================================================================
# AUD-35301 / AUD-35302 / AUD-35303 / AUD-35304 — VtSession
# ======================================================================

class FakeVTDecompressLib:
    def __init__(self) -> None:
        self.wait_count = 0
        self.invalidate_calls: list[int] = []

    def VTDecompressionSessionWaitForAsynchronousFrames(self, session) -> int:
        self.wait_count += 1
        return 0

    def VTDecompressionSessionInvalidate(self, session) -> None:
        self.invalidate_calls.append(
            session.value if isinstance(session, ctypes.c_void_p) else session
        )


class FakeCFLib:
    def __init__(self) -> None:
        self.release_calls: list[int] = []

    def CFRelease(self, pointer) -> None:
        self.release_calls.append(
            pointer.value if isinstance(pointer, ctypes.c_void_p) else pointer
        )


def _install_fake_vt_session_libs(monkeypatch: pytest.MonkeyPatch
                                   ) -> tuple[FakeVTDecompressLib, FakeCFLib]:
    fake_vt = FakeVTDecompressLib()
    fake_cf = FakeCFLib()
    monkeypatch.setattr(vts_module, "_VT_LIB", fake_vt)
    monkeypatch.setattr(vts_module, "_CF_LIB", fake_cf)
    monkeypatch.setattr(vts_module, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(vts_module, "_MACOS_AVAILABLE", True)
    return fake_vt, fake_cf


def test_aud35301_destroy_releases_native_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidate 之后必须 CFRelease（Create Rule 的 +1 引用）。"""
    fake_vt, fake_cf = _install_fake_vt_session_libs(monkeypatch)
    session = VtSession()
    session._ctypes_session = ctypes.c_void_p(0x999)

    session.destroy()

    assert fake_vt.invalidate_calls == [0x999]
    assert fake_cf.release_calls == [0x999]  # AUD-35301 核心
    assert session._destroyed is True
    assert not session.is_created


def test_aud35302_destroyed_session_can_be_recreated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """destroy 后同一实例 create 必须恢复可用，重配置先释放旧会话。"""
    fake_vt, _fake_cf = _install_fake_vt_session_libs(monkeypatch)
    created_sessions: list[int] = []

    def fake_impl(self, **kwargs):
        handle = 0x700 + len(created_sessions)
        created_sessions.append(handle)
        self._ctypes_session = ctypes.c_void_p(handle)
        return self._ctypes_session

    monkeypatch.setattr(vts_module.VtSession, "_create_session_impl", fake_impl)

    session = VtSession()
    assert session.create(width=64, height=32, codec="h264", extradata=b"\x01") is True
    assert session.is_created
    assert fake_vt.invalidate_calls == []

    # 存活状态下重新 create：旧会话必须先释放
    assert session.create(width=64, height=32, codec="h264", extradata=b"\x01") is True
    assert fake_vt.invalidate_calls == [0x700]

    session.destroy()
    assert session.is_created is False

    # AUD-35302 核心：destroy 后复用同一实例
    assert session.create(width=64, height=32, codec="h264", extradata=b"\x01") is True
    assert session.is_created is True  # 修复前恒为 False
    assert fake_vt.invalidate_calls == [0x700, 0x701]

    session.destroy()


def test_aud35303_submit_packet_forwards_pts(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步 submit_packet 的 pts 必须透传到 _submit_packet_raw。"""
    _install_fake_vt_session_libs(monkeypatch)
    session = VtSession()
    session._ctypes_session = ctypes.c_void_p(1)
    session._destroyed = False

    recorded: list[tuple[bytes, int | None]] = []

    def fake_raw(packet_data: bytes, pts: int | None = None) -> bool:
        recorded.append((packet_data, pts))
        return True

    session._submit_packet_raw = fake_raw  # type: ignore[assignment]
    assert session.submit_packet(b"\x00\x01", pts=777, is_keyframe=True) is True
    assert recorded == [(b"\x00\x01", 777)]  # 修复前 pts 被丢弃


def test_aud35303_callback_restores_pts_from_refcon() -> None:
    """解码回调用 frameRefCon 还原提交侧时间戳。"""

    class RecordingQueue:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        def put_pixel_buffer(self, **kwargs) -> bool:
            self.frames.append(kwargs)
            return True

    queue = RecordingQueue()
    context = _CtypesCallbackContext(queue, 64, 32, "nv12", 8)
    ref_object = ctypes.py_object(context)
    refcon = ctypes.cast(ctypes.pointer(ref_object), ctypes.c_void_p).value
    assert refcon

    invalid_cmtime = _CMTime(0, 30, 0, 0)  # flags=0 → 无效时间
    VtSession._on_ctypes_decoded(
        refcon, 12345, 0, 0, 0x1000, invalid_cmtime, invalid_cmtime
    )
    assert len(queue.frames) == 1
    assert queue.frames[0]["pts"] == 12345  # 来自 frameRefCon

    # refCon 为空 → 回退 CMTime value
    VtSession._on_ctypes_decoded(
        refcon, 0, 0, 0, 0x1001, _CMTime(42, 30, 1, 0), invalid_cmtime
    )
    assert queue.frames[1]["pts"] == 42


def _make_avcc(length_size_minus_one: int) -> bytes:
    sps = b"\x67\x64\x00\x1f" + b"\x00" * 8
    pps = b"\x68\xeb\x00\x00"
    return (
        bytes([1, 0x64, 0x00, 0x1F, 0xFC | (length_size_minus_one & 0x03), 0xE1])
        + len(sps).to_bytes(2, "big")
        + sps
        + b"\x01"
        + len(pps).to_bytes(2, "big")
        + pps
    )


def test_aud35304_parse_avcc_returns_length_size() -> None:
    session = VtSession()
    sps_list, pps_list, size = session._parse_avcc_extradata(_make_avcc(2))
    assert size == 3  # lengthSizeMinusOne=2 → 3 字节前缀
    assert len(sps_list) == 1 and len(pps_list) == 1
    _sps, _pps, size4 = session._parse_avcc_extradata(_make_avcc(3))
    assert size4 == 4


def test_aud35304_format_description_uses_parsed_length_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "kCFAllocatorDefault" not in vars(vts_module):
        pytest.skip("CoreFoundation constants unavailable")
    captured: dict = {}

    def fake_create_from_h264(allocator, count, params, sizes, nal_size, out):
        captured["count"] = count
        captured["nal_size"] = nal_size
        return 0, object()

    fake_coremedia = types.ModuleType("CoreMedia")
    fake_coremedia.CMVideoFormatDescriptionCreateFromH264ParameterSets = (
        fake_create_from_h264
    )
    monkeypatch.setitem(sys.modules, "CoreMedia", fake_coremedia)

    session = VtSession()
    format_desc = session._create_h264_format_description(
        _make_avcc(2), 64, 32
    )
    assert format_desc is not None
    assert captured["nal_size"] == 3  # 修复前硬编码 4
    assert captured["count"] == 2  # SPS + PPS

    # Annex B 分支无长度字段可解析，声明值保持 4（与提交侧约定一致）
    annexb = (
        b"\x00\x00\x00\x01\x67\x64\x00\x1f"
        + b"\x00" * 8
        + b"\x00\x00\x00\x01\x68\xeb\x00\x00"
    )
    captured.clear()
    assert session._create_h264_format_description(annexb, 64, 32) is not None
    assert captured["nal_size"] == 4


# ======================================================================
# AUD-35312 / AUD-35313 / AUD-35315 — vt_zero_copy_probe
# ======================================================================

class FakeProbeDemuxer:
    instances: list["FakeProbeDemuxer"] = []

    def __init__(self) -> None:
        self.closed = False
        FakeProbeDemuxer.instances.append(self)

    def open(self, file_path: str) -> SimpleNamespace:
        return SimpleNamespace(
            codec_name="h264",
            width=64,
            height=32,
            bit_depth=8,
            extradata=_make_avcc(3),
            total_frames=4,
            pixel_format="yuv420p",
        )

    def packets(self, max_packets=None):
        for index in range(max_packets or 4):
            yield SimpleNamespace(data=b"\x00\x00\x00\x01\x65\x88", pts=index, is_keyframe=True)

    def close(self) -> None:
        self.closed = True


class FakeProbeSession:
    instances: list["FakeProbeSession"] = []

    def __init__(self) -> None:
        self.create_result = True
        self.released_frames: list[object] = []
        self.destroyed = False
        FakeProbeSession.instances.append(self)

    def create(self, **kwargs) -> bool:
        return self.create_result

    def decode_packet(self, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(
            pixel_buffer=0x1000, pts=kwargs.get("pts"), frame_index=0
        )

    def release_decoded_frame(self, decoded) -> None:
        self.released_frames.append(decoded)

    def destroy(self) -> None:
        self.destroyed = True


class FakeProbeBridge:
    instances: list["FakeProbeBridge"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.destroyed = False
        FakeProbeBridge.instances.append(self)

    def initialize(self, **kwargs) -> bool:
        return True

    def destroy(self) -> None:
        self.destroyed = True


class FakeProbeGLContext:
    instances: list["FakeProbeGLContext"] = []

    def __init__(self) -> None:
        self.released = False
        FakeProbeGLContext.instances.append(self)

    def release(self) -> None:
        self.released = True


def _install_probe_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_cls=None,
    bridge_cls=None,
    ctx_instance=None,
) -> None:
    import src.shared.media.macos_vt.packet_demux as demux_module
    import src.shared.media.macos_vt.iosurface_gl_bridge as bridge_module

    FakeProbeSession.instances = []
    FakeProbeBridge.instances = []
    monkeypatch.setattr(demux_module, "PacketDemuxer", FakeProbeDemuxer)
    monkeypatch.setattr(vts_module, "VtSession", session_cls or FakeProbeSession)
    monkeypatch.setattr(vts_module, "is_vt_available", lambda: True)
    monkeypatch.setattr(bridge_module, "IOSurfaceGLBridge", bridge_cls or FakeProbeBridge)
    monkeypatch.setattr(probe_module, "_pixel_buffer_has_iosurface", lambda pb: True)
    if ctx_instance is not None:
        monkeypatch.setattr(
            probe_module, "_create_standalone_gl_context", lambda: ctx_instance
        )
    monkeypatch.setattr(probe_module.platform, "system", lambda: "Darwin")


def test_aud35312_decode_only_releases_every_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decode-only 阶段每个解码帧都必须 release（不再泄漏 retain 计数）。"""
    _install_probe_stubs(monkeypatch)

    result = probe_module.run_probe(
        file_path="fake.mp4", frames=3, warmup=1, stage="decode-only"
    )
    assert result.probe_success is True
    assert result.decoded_pixel_buffers == 4
    assert result.zero_copy is True
    session = FakeProbeSession.instances[0]
    # 4 个解码帧全部释放（修复前 released_frames == 0）
    assert len(session.released_frames) == 4
    assert session.destroyed is True


def test_aud35313_session_failure_cleans_bridge_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT 会话创建失败的早退路径必须清理 bridge 与 GL context。"""

    class FailingSession(FakeProbeSession):
        def create(self, **kwargs) -> bool:
            return False

    ctx = FakeProbeGLContext()
    _install_probe_stubs(
        monkeypatch, session_cls=FailingSession, ctx_instance=ctx
    )

    result = probe_module.run_probe(
        file_path="fake.mp4", frames=2, warmup=0, stage="bridge-only"
    )
    assert result.fallback_reason == "vt_session_creation_failed"
    bridge = FakeProbeBridge.instances[0]
    assert bridge.destroyed is True  # 修复前 False（泄漏 GL 纹理环）
    assert ctx.released is True  # 修复前 False（泄漏离屏上下文）


def test_aud35315_iosurface_detection_relative_import_and_no_swallow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """检测助手：相对导入优先、异常窄化、导入失败显式留痕。"""
    fake_cv = SimpleNamespace(
        CVPixelBufferGetIOSurface=lambda pointer: 0x2000 if pointer else None
    )
    monkeypatch.setattr(vts_module, "_CV_LIB", fake_cv)
    assert probe_module._pixel_buffer_has_iosurface(0x1000) is True

    fake_cv_none = SimpleNamespace(CVPixelBufferGetIOSurface=lambda pointer: None)
    monkeypatch.setattr(vts_module, "_CV_LIB", fake_cv_none)
    assert probe_module._pixel_buffer_has_iosurface(0x1000) is False

    monkeypatch.setattr(vts_module, "_CV_LIB", None)
    assert probe_module._pixel_buffer_has_iosurface(0x1000) is False

    # 两种导入路径都取不到 _CV_LIB：警告留痕，绝不静默
    monkeypatch.delattr(vts_module, "_CV_LIB")
    with caplog.at_level("WARNING"):
        assert probe_module._pixel_buffer_has_iosurface(0x1000) is False
    assert any("vt_session import failed" in record.message for record in caplog.records)


def test_aud35314_output_json_plain_filename(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path) -> None:
    """纯文件名（无目录部分）不再触发 os.makedirs('')。"""
    monkeypatch.chdir(tmp_path)
    from src.shared.media.macos_vt.vt_zero_copy_probe import ZeroCopyProbeResult

    monkeypatch.setattr(
        probe_module,
        "run_probe",
        lambda **kwargs: ZeroCopyProbeResult(probe_success=True, zero_copy=True),
    )
    monkeypatch.setattr(
        sys, "argv", ["probe", "--file", "v.mp4", "--output-json", "out.json"]
    )
    probe_module.main()
    assert (tmp_path / "out.json").exists()

    # 嵌套目录路径仍然可用
    monkeypatch.setattr(
        sys, "argv", ["probe", "--file", "v.mp4", "--output-json", "sub/dir/o.json"]
    )
    probe_module.main()
    assert (tmp_path / "sub" / "dir" / "o.json").exists()


# ======================================================================
# AUD-35400 / AUD-35401 — metadata
# ======================================================================

def test_aud35400_from_string_valid_forms() -> None:
    assert Rational.from_string("24/1") == Rational(24, 1)
    assert Rational.from_string(" 30000/1001 ") == Rational(30000, 1001)
    assert Rational.from_string("24/1").num == 24
    assert Rational.from_float(float(Rational.from_string("23.976"))) == (
        Rational.from_string("23.976")
    )
    assert Rational.from_string("24").to_float() == 24.0


def test_aud35400_from_string_rejects_extra_segments() -> None:
    with pytest.raises(ValueError, match="invalid rational string"):
        Rational.from_string("24/1/1")  # 修复前静默丢弃第三段


def test_aud35400_from_string_rejects_malformed_with_context() -> None:
    for bad in ("abc/1", "24/xyz", "abc", "", "  ", "1//2"):
        with pytest.raises(ValueError, match="invalid rational string"):
            Rational.from_string(bad)
    with pytest.raises(ValueError, match="invalid rational string"):
        Rational.from_string(None)  # type: ignore[arg-type]


def _metadata_kwargs() -> dict:
    return dict(
        width=64,
        height=32,
        fps=FPS_24,
        total_frames=48,
        codec_name="h264",
        codec_profile=None,
        pixel_format="yuv420p",
        bit_depth=8,
    )


def test_aud35401_rejects_non_positive_fps() -> None:
    kwargs = _metadata_kwargs()
    kwargs["fps"] = Rational(0, 1)
    with pytest.raises(ValueError, match="fps must be positive"):
        VideoMetadata(**kwargs)
    kwargs["fps"] = Rational(-24, 1)
    with pytest.raises(ValueError, match="fps must be positive"):
        VideoMetadata(**kwargs)


def test_aud35401_rejects_non_positive_sar() -> None:
    kwargs = _metadata_kwargs()
    kwargs["sample_aspect_ratio"] = Rational(0, 1)
    with pytest.raises(ValueError, match="sample_aspect_ratio must be positive"):
        VideoMetadata(**kwargs)
    kwargs["sample_aspect_ratio"] = Rational(-1, 1)
    with pytest.raises(ValueError, match="sample_aspect_ratio must be positive"):
        VideoMetadata(**kwargs)


def test_aud35401_valid_metadata_has_no_divide_by_zero() -> None:
    metadata = VideoMetadata(**_metadata_kwargs())
    assert metadata.duration_seconds == 2.0
    assert metadata.display_size == (64, 32)

    anamorphic = VideoMetadata(
        **{**_metadata_kwargs(), "sample_aspect_ratio": Rational(2, 1)}
    )
    assert anamorphic.display_size == (128, 32)
    narrow = VideoMetadata(
        **{**_metadata_kwargs(), "sample_aspect_ratio": Rational(1, 2)}
    )
    assert narrow.display_size == (64, 64)


# ======================================================================
# AUD-35404 — performance_monitor
# ======================================================================

_TRACKED_LIST_NAMES = (
    "_tick_times",
    "_process_times",
    "_render_submit_times",
    "_queue_depths",
    "_seek_times",
    "_read_times",
    "_cache_hits",
    "_renderer_queue_depths",
    "_held_frames",
    "_coalesced_video_commands",
    "_plane_prepare_times",
    "_texture_write_times",
    "_yuv_render_times",
    "_frame_paths",
)


def test_aud35404_lists_are_bounded() -> None:
    monitor = VideoPerformanceMonitor()
    monitor.start_session()
    monitor.set_aggregate_log_enabled(False)

    for index in range(1500):
        monitor.track_tick_time(1.0)
        monitor.track_process_time(index, 1.0)
        monitor.track_render_submit_time(index, 1.0)
        monitor.track_queue_depth(index % 4)
        monitor.track_seek_time(index, 1.0)
        monitor.track_read_time(index, 1.0)
        monitor.track_cache_hit(index, index % 2 == 0)
        monitor.track_renderer_queue_depth(index % 3)
        monitor.track_held_frames(index % 2)
        monitor.track_coalesced_video_commands(index % 5)
        monitor.track_plane_prepare_time(1.0)
        monitor.track_texture_write_time(1.0)
        monitor.track_yuv_render_time(1.0)
        monitor.track_frame_path("planar_yuv")

    limit = VideoPerformanceMonitor._MAX_TRACKED_SAMPLES
    for name in _TRACKED_LIST_NAMES:
        values = getattr(monitor, name)
        assert len(values) <= limit * 2, f"{name} unbounded"
        assert len(values) >= limit, f"{name} lost window"
    # 统计仍正确反映窗口内数据（窗口起点随裁剪相位漂移，允许 ±2%）
    stats = monitor.get_aggregate_stats()
    assert stats["tick_avg_ms"] == 1.0
    assert abs(stats["cache_hit_rate"] - 0.5) < 0.02


def test_aud35404_aggregate_log_runs_outside_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = VideoPerformanceMonitor()
    monitor.start_session()
    outputs: list[dict] = []

    def fake_output() -> None:
        owned = monitor._lock._is_owned() if hasattr(monitor._lock, "_is_owned") else False
        outputs.append({"lock_owned_during_aggregation": owned})

    monkeypatch.setattr(monitor, "_output_aggregate_log", fake_output)

    # 间隔未到：不聚合
    monitor._last_aggregate_time = time.monotonic()
    monitor.track_tick_time(5.0)
    assert outputs == []

    # 间隔已到：聚合发生，且不在 RLock 内
    monitor._last_aggregate_time = time.monotonic() - 5.0
    monitor.track_tick_time(5.0)
    assert len(outputs) == 1
    assert outputs[0]["lock_owned_during_aggregation"] is False


def test_aud35404_window_below_double_limit_keeps_all() -> None:
    monitor = VideoPerformanceMonitor()
    monitor.start_session()
    monitor.set_aggregate_log_enabled(False)
    for _ in range(700):
        monitor.track_tick_time(2.0)
    assert len(monitor._tick_times) == 700  # 未到 2x 窗口不裁剪
    assert monitor.get_aggregate_stats()["tick_count"] == 700
