"""Native VideoToolbox compression for IOSurface-backed GPU surfaces.

The exporter owns timeline scheduling and muxing.  This module owns only the
native compression-session boundary: a ``GpuEncodeSurface`` is submitted as a
CVPixelBuffer, and VideoToolbox packets are copied out of the callback for a
CPU muxer or packet sink.

The callback copies compressed bytes, not pixels.  It therefore stays inside
the plan's allowed CPU file-link boundary.  No GPU timing is inferred here;
callers must provide a real encoder timestamp before claiming a full GPU
trail.
"""

from __future__ import annotations

import ctypes as _ctypes
import logging
import platform
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Optional

from src.shared.export.gpu_encode_surface import GpuEncodeSurface

logger = logging.getLogger(__name__)

_MACOS_AVAILABLE = platform.system() == "Darwin"


class VideoToolboxCompressionError(RuntimeError):
    """A native VideoToolbox compression operation failed."""


class _CMTime(_ctypes.Structure):
    _fields_ = [
        ("value", _ctypes.c_int64),
        ("timescale", _ctypes.c_int32),
        ("flags", _ctypes.c_uint32),
        ("epoch", _ctypes.c_int64),
    ]


@dataclass(frozen=True)
class VideoToolboxEncodedPacket:
    """A compressed packet copied from one VideoToolbox sample buffer."""

    data: bytes
    codec: str
    pts: int | None
    dts: int | None
    duration: int | None
    timescale: int | None
    keyframe: bool
    parameter_sets: tuple[bytes, ...] = ()
    nal_unit_length_size: int = 4
    encode_info_flags: int = 0
    submission_id: int = 0


_VT_COMPRESSION_CALLBACK = _ctypes.CFUNCTYPE(
    None,
    _ctypes.c_void_p,  # outputCallbackRefCon
    _ctypes.c_void_p,  # sourceFrameRefCon
    _ctypes.c_int32,  # status
    _ctypes.c_uint32,  # VTEncodeInfoFlags
    _ctypes.c_void_p,  # CMSampleBufferRef
)


def _load_native_libraries() -> tuple[Any, Any] | tuple[None, None]:
    if not _MACOS_AVAILABLE:
        return None, None
    try:
        vt = _ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox"
        )
        cm = _ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreMedia.framework/CoreMedia"
        )
        return vt, cm
    except OSError as exc:
        logger.warning("VideoToolbox compression libraries unavailable: %s", exc)
        return None, None


_VT_LIB, _CM_LIB = _load_native_libraries()


def _configure_native_signatures() -> None:
    if _VT_LIB is None or _CM_LIB is None:
        return

    _VT_LIB.VTCompressionSessionCreate.restype = _ctypes.c_int32
    _VT_LIB.VTCompressionSessionCreate.argtypes = [
        _ctypes.c_void_p,
        _ctypes.c_int32,
        _ctypes.c_int32,
        _ctypes.c_uint32,
        _ctypes.c_void_p,
        _ctypes.c_void_p,
        _ctypes.c_void_p,
        _VT_COMPRESSION_CALLBACK,
        _ctypes.c_void_p,
        _ctypes.POINTER(_ctypes.c_void_p),
    ]
    _VT_LIB.VTCompressionSessionEncodeFrame.restype = _ctypes.c_int32
    _VT_LIB.VTCompressionSessionEncodeFrame.argtypes = [
        _ctypes.c_void_p,
        _ctypes.c_void_p,
        _CMTime,
        _CMTime,
        _ctypes.c_void_p,
        _ctypes.c_void_p,
        _ctypes.POINTER(_ctypes.c_uint32),
    ]
    _VT_LIB.VTCompressionSessionCompleteFrames.restype = _ctypes.c_int32
    _VT_LIB.VTCompressionSessionCompleteFrames.argtypes = [
        _ctypes.c_void_p,
        _CMTime,
    ]
    _VT_LIB.VTCompressionSessionInvalidate.restype = None
    _VT_LIB.VTCompressionSessionInvalidate.argtypes = [_ctypes.c_void_p]

    _CM_LIB.CMSampleBufferGetDataBuffer.restype = _ctypes.c_void_p
    _CM_LIB.CMSampleBufferGetDataBuffer.argtypes = [_ctypes.c_void_p]
    _CM_LIB.CMBlockBufferGetDataLength.restype = _ctypes.c_size_t
    _CM_LIB.CMBlockBufferGetDataLength.argtypes = [_ctypes.c_void_p]
    _CM_LIB.CMBlockBufferGetDataPointer.restype = _ctypes.c_int32
    _CM_LIB.CMBlockBufferGetDataPointer.argtypes = [
        _ctypes.c_void_p,
        _ctypes.c_size_t,
        _ctypes.POINTER(_ctypes.c_size_t),
        _ctypes.POINTER(_ctypes.c_size_t),
        _ctypes.POINTER(_ctypes.c_void_p),
    ]
    _CM_LIB.CMSampleBufferGetPresentationTimeStamp.restype = _CMTime
    _CM_LIB.CMSampleBufferGetPresentationTimeStamp.argtypes = [_ctypes.c_void_p]
    _CM_LIB.CMSampleBufferGetDecodeTimeStamp.restype = _CMTime
    _CM_LIB.CMSampleBufferGetDecodeTimeStamp.argtypes = [_ctypes.c_void_p]
    _CM_LIB.CMSampleBufferGetDuration.restype = _CMTime
    _CM_LIB.CMSampleBufferGetDuration.argtypes = [_ctypes.c_void_p]
    _CM_LIB.CMSampleBufferGetFormatDescription.restype = _ctypes.c_void_p
    _CM_LIB.CMSampleBufferGetFormatDescription.argtypes = [_ctypes.c_void_p]

    # These format-description helpers are not exported by every PyObjC
    # build, so keep the C ABI here as the source of truth.
    for name in (
        "CMVideoFormatDescriptionGetH264ParameterSetAtIndex",
        "CMVideoFormatDescriptionGetHEVCParameterSetAtIndex",
    ):
        function = getattr(_CM_LIB, name, None)
        if function is None:
            continue
        function.restype = _ctypes.c_int32
        function.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
            _ctypes.POINTER(_ctypes.c_void_p),
            _ctypes.POINTER(_ctypes.c_size_t),
            _ctypes.POINTER(_ctypes.c_size_t),
            _ctypes.POINTER(_ctypes.c_int32),
        ]


_configure_native_signatures()


def _codec_fourcc(codec: str) -> tuple[str, int]:
    normalized = str(codec).strip().lower()
    if normalized in {"h264", "avc", "avc1"}:
        return "h264", 0x61766331
    if normalized in {"hevc", "h265", "hvc1"}:
        return "hevc", 0x68766331
    raise VideoToolboxCompressionError(
        f"unsupported_videotoolbox_codec:{normalized}"
    )


def _native_pointer(value: Any) -> _ctypes.c_void_p:
    if value is None:
        raise VideoToolboxCompressionError("videotoolbox_surface_handle_missing")
    if isinstance(value, _ctypes.c_void_p):
        pointer = value.value
    elif isinstance(value, int):
        pointer = value
    else:
        try:
            import objc

            pointer = int(objc.pyobjc_id(value))
        except Exception as exc:
            # AUD-35205：取不到合法原生指针必须 fail-loud。此前这里用
            # hash(value) 兜底伪造指针，送入 VTCompressionSessionEncodeFrame
            # 会在原生层解引用野指针（hash 还可能为负）→ 进程级崩溃，
            # ctypes 回调无法拦截。
            raise VideoToolboxCompressionError(
                "videotoolbox_surface_handle_not_pointer_like"
            ) from exc
    # AUD-35205：负整数经 c_void_p 会回绕成非法地址，同样按无效指针拒绝。
    if not pointer or pointer < 0:
        raise VideoToolboxCompressionError("videotoolbox_surface_handle_null")
    return _ctypes.c_void_p(pointer)


def _time_value(value: _CMTime) -> tuple[int | None, int | None]:
    if not value or not (int(value.flags) & 1) or int(value.timescale) <= 0:
        return None, None
    return int(value.value), int(value.timescale)


def _extract_parameter_sets(
    format_description: int,
    codec: str,
) -> tuple[tuple[bytes, ...], int]:
    if _CM_LIB is None or not format_description:
        return (), 4
    function_name = (
        "CMVideoFormatDescriptionGetH264ParameterSetAtIndex"
        if codec == "h264"
        else "CMVideoFormatDescriptionGetHEVCParameterSetAtIndex"
    )
    function = getattr(_CM_LIB, function_name, None)
    if function is None:
        return (), 4

    first_pointer = _ctypes.c_void_p()
    first_size = _ctypes.c_size_t()
    count = _ctypes.c_size_t()
    nal_length_size = _ctypes.c_int32()
    status = function(
        _ctypes.c_void_p(format_description),
        0,
        _ctypes.byref(first_pointer),
        _ctypes.byref(first_size),
        _ctypes.byref(count),
        _ctypes.byref(nal_length_size),
    )
    if int(status) != 0 or not first_pointer.value:
        return (), 4

    result: list[bytes] = []
    for index in range(max(1, int(count.value))):
        pointer = _ctypes.c_void_p()
        size = _ctypes.c_size_t()
        status = function(
            _ctypes.c_void_p(format_description),
            index,
            _ctypes.byref(pointer),
            _ctypes.byref(size),
            _ctypes.byref(count),
            _ctypes.byref(nal_length_size),
        )
        if int(status) == 0 and pointer.value and size.value:
            result.append(_ctypes.string_at(pointer.value, size.value))
    return tuple(result), max(1, int(nal_length_size.value or 4))


def _packet_is_keyframe(data: bytes, codec: str, nal_length_size: int) -> bool:
    """Inspect the length-prefixed NAL units emitted by VideoToolbox."""
    offset = 0
    while offset + nal_length_size <= len(data):
        length = int.from_bytes(data[offset:offset + nal_length_size], "big")
        offset += nal_length_size
        if length <= 0 or offset + length > len(data):
            break
        nal_header = data[offset]
        nal_type = (nal_header & 0x1F) if codec == "h264" else ((nal_header >> 1) & 0x3F)
        if (codec == "h264" and nal_type == 5) or (
            codec == "hevc" and nal_type in {19, 20, 21}
        ):
            return True
        offset += length
    return False


def _build_h264_avcc(
    parameter_sets: tuple[bytes, ...],
    nal_length_size: int,
) -> bytes:
    """Build the AVCDecoderConfigurationRecord used by MP4/MOV muxers."""
    if len(parameter_sets) < 2:
        raise VideoToolboxCompressionError("h264_parameter_sets_incomplete")
    sps, pps = parameter_sets[0], parameter_sets[1]
    if len(sps) < 4 or len(pps) > 0xFFFF or len(sps) > 0xFFFF:
        raise VideoToolboxCompressionError("h264_parameter_sets_invalid")
    length_size_minus_one = max(0, min(3, int(nal_length_size) - 1))
    return (
        b"\x01"
        + sps[1:4]
        + bytes((0xFC | length_size_minus_one, 0xE1))
        + len(sps).to_bytes(2, "big")
        + sps
        + b"\x01"
        + len(pps).to_bytes(2, "big")
        + pps
    )


def _build_hevc_hvcc(
    parameter_sets: tuple[bytes, ...],
    nal_length_size: int,
    pixel_format: str = "nv12",
) -> bytes:
    """Build an HEVCDecoderConfigurationRecord from VT parameter sets."""
    if len(parameter_sets) < 3:
        raise VideoToolboxCompressionError("hevc_parameter_sets_incomplete")
    vps, sps, pps = parameter_sets[:3]
    # The general_profile_tier_level fields start after the two-byte HEVC NAL
    # header and the one-byte SPS sub-layer header. VideoToolbox returns raw
    # NAL units here, without an Annex-B start code.
    if len(sps) < 15:
        raise VideoToolboxCompressionError("hevc_sps_too_short_for_hvcc")
    profile_tier_level = sps[3:15]
    length_size_minus_one = max(0, min(3, int(nal_length_size) - 1))
    arrays = bytearray()
    for nal_type, payload in ((32, vps), (33, sps), (34, pps)):
        if not payload or len(payload) > 0xFFFF:
            raise VideoToolboxCompressionError("hevc_parameter_set_invalid")
        arrays.extend(bytes((0x80 | nal_type, 0x00, 0x01)))
        arrays.extend(len(payload).to_bytes(2, "big"))
        arrays.extend(payload)
    bit_depth_minus_8 = 2 if str(pixel_format).lower() == "p010" else 0
    return (
        b"\x01"
        + profile_tier_level
        + b"\xf0\x00"  # min_spatial_segmentation_idc
        + b"\xfc"       # parallelismType = 0
        + b"\xfd"       # chromaFormat = 1 (4:2:0)
        + bytes((0xF8 | bit_depth_minus_8,))
        + bytes((0xF8 | bit_depth_minus_8,))
        + b"\x00\x00"   # avgFrameRate
        + bytes((0x0C | length_size_minus_one, 0x03))
        + arrays
    )


class VideoToolboxCompressionSession:
    """Submit IOSurface-backed GPU surfaces to a native VT encoder.

    ``on_packet`` is called from a VideoToolbox callback thread.  It receives
    copied compressed bytes and must not assume that the callback is the
    exporter thread.  The input ``GpuEncodeSurface`` is released after its
    corresponding callback (or immediately when submission fails).
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        codec: str = "h264",
        fps: int = 30,
        pixel_format: str = "nv12",
        on_packet: Optional[Callable[[VideoToolboxEncodedPacket], Any]] = None,
        require_hardware: bool = True,
    ) -> None:
        if not _MACOS_AVAILABLE or _VT_LIB is None or _CM_LIB is None:
            raise VideoToolboxCompressionError(
                "videotoolbox_compression_unavailable"
            )
        self.width = int(width)
        self.height = int(height)
        if self.width <= 0 or self.height <= 0:
            raise VideoToolboxCompressionError("invalid_videotoolbox_dimensions")
        self._codec_name = str(codec).strip().lower()
        self.codec, codec_type = _codec_fourcc(codec)
        self._pixel_format = str(pixel_format).strip().lower()
        if self._pixel_format not in {"nv12", "p010"}:
            raise VideoToolboxCompressionError(
                "videotoolbox_session_pixel_format_unsupported:"
                f"{pixel_format}"
            )
        self.fps = max(1, int(fps))
        self._on_packet = on_packet
        # _lock 保护 Python 状态（_pending/_closed/_last_error）；
        # _session_lock 保护原生会话指针的检查-使用-Invalidate 生命周期。
        # 两锁永远按 _lock -> _session_lock 顺序获取；VT 回调线程只碰
        # _lock，因此持 _session_lock 做原生调用不会与回调互锁。
        self._lock = threading.RLock()
        self._session_lock = threading.Lock()
        self._pending: dict[int, GpuEncodeSurface] = {}
        self._next_submission_id = 1
        self._last_error = ""
        self._closed = False
        # AUD-35208：回调丢包计数。此前丢包只写 _last_error 且该字段无人
        # 消费，导出静默缺帧；计数 + warning 日志让缺帧可观测。
        self._dropped_packets = 0
        self._ref_object = _ctypes.py_object(self)
        self._ref_pointer = _ctypes.pointer(self._ref_object)
        self._callback = _VT_COMPRESSION_CALLBACK(self._handle_native_callback)

        specification = None
        if require_hardware:
            try:
                import objc
                from Foundation import NSDictionary, NSNumber
                import VideoToolbox

                specification_object = NSDictionary.dictionaryWithObject_forKey_(
                    NSNumber.numberWithBool_(True),
                    VideoToolbox.kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder,
                )
                specification = _ctypes.c_void_p(objc.pyobjc_id(specification_object))
                self._specification_object = specification_object
            except Exception as exc:
                raise VideoToolboxCompressionError(
                    "videotoolbox_hardware_specification_unavailable"
                ) from exc
        else:
            self._specification_object = None

        self._session = _ctypes.c_void_p()
        status = _VT_LIB.VTCompressionSessionCreate(
            None,
            self.width,
            self.height,
            codec_type,
            specification,
            None,
            None,
            self._callback,
            _ctypes.cast(self._ref_pointer, _ctypes.c_void_p),
            _ctypes.byref(self._session),
        )
        if int(status) != 0 or not self._session.value:
            self._session = _ctypes.c_void_p()
            raise VideoToolboxCompressionError(
                f"videotoolbox_compression_session_create_failed:{int(status)}"
            )
        # T-P0-31：交付像素格式决定 profile——P010 必须 Main10。VT 对
        # HEVC 的默认 profile 是 Main（8-bit），即便输入是 10-bit
        # CVPixelBuffer，静默产出 8-bit 就是位深造假。
        if self._codec_name == "hevc":
            level = (
                "HEVC_Main10_AutoLevel" if self._pixel_format == "p010"
                else "HEVC_Main_AutoLevel"
            )
            set_status = self._set_profile_level(level)
            if set_status != 0:
                # 本机硬件编码器拒绝 Main10（如部分 AMD dGPU 返回 -12900
                # kVTPropertyUnsupportedErr）时结构化失败，交由上层诚实
                # 回退 CPU 10-bit 编码——绝不静默用 Main 编 8-bit 冒充
                # P010 交付。（遗留：VT 软件编码器 Main10 的 mux 链路在
                # 该场景存在 EINVAL，见 T-P0-31 遗留记录，修复后可在此
                # 接入软件编码器降级。）
                self._close_session()
                raise VideoToolboxCompressionError(
                    "videotoolbox_hevc_profile_level_rejected:"
                    f"{self._pixel_format}:{set_status}"
                )

    def _set_profile_level(self, level: str) -> int:
        """Set VTCompressionPropertyKey_ProfileLevel; return the raw status."""
        cf = _ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.restype = _ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            _ctypes.c_void_p, _ctypes.c_char_p, _ctypes.c_uint32,
        ]
        # AUD-35209：CFStringCreateWithCString 产生调用方持有的
        # CFStringRef，用完必须 CFRelease——否则每次编码会话泄漏两个
        # 小 CF 对象，长期反复导出累积原生堆占用。
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [_ctypes.c_void_p]
        # kCFStringEncodingUTF8 = 0x08000100；CFString 与 VT 的
        # CFStringRef 参数 toll-free 同型。
        key_ref = _ctypes.c_void_p(cf.CFStringCreateWithCString(
            None, b"ProfileLevel", 0x08000100))
        value_ref = _ctypes.c_void_p(cf.CFStringCreateWithCString(
            None, level.encode("ascii"), 0x08000100))
        try:
            _VT_LIB.VTSessionSetProperty.restype = _ctypes.c_int32
            _VT_LIB.VTSessionSetProperty.argtypes = [
                _ctypes.c_void_p, _ctypes.c_void_p, _ctypes.c_void_p,
            ]
            return int(_VT_LIB.VTSessionSetProperty(
                _ctypes.c_void_p(self._session.value), key_ref, value_ref,
            ))
        finally:
            if key_ref.value:
                cf.CFRelease(key_ref)
            if value_ref.value:
                cf.CFRelease(value_ref)

    def _close_session(self) -> None:
        """Tear down the current compression session (idempotent)."""
        with self._session_lock:
            session = self._session
            self._session = _ctypes.c_void_p()
            if session is not None and session.value:
                _VT_LIB.VTCompressionSessionInvalidate(session)

    @property
    def is_open(self) -> bool:
        with self._lock:
            return not self._closed and bool(self._session.value)

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def dropped_packets(self) -> int:
        """Number of encoded packets lost in the VT callback (AUD-35208)."""
        with self._lock:
            return self._dropped_packets

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def submit(self, surface: GpuEncodeSurface) -> dict[str, Any]:
        """Enqueue one GPU surface without reading any pixel plane on CPU."""
        if not isinstance(surface, GpuEncodeSurface):
            raise TypeError("VideoToolboxCompressionSession requires GpuEncodeSurface")
        descriptor = surface.descriptor
        if descriptor.backend != "videotoolbox":
            raise VideoToolboxCompressionError(
                f"videotoolbox_surface_backend_mismatch:{descriptor.backend}"
            )
        if int(descriptor.width) != self.width or int(descriptor.height) != self.height:
            raise VideoToolboxCompressionError(
                "videotoolbox_surface_dimensions_mismatch"
            )
        if str(descriptor.pixel_format).lower() not in {"nv12", "p010"}:
            raise VideoToolboxCompressionError(
                f"videotoolbox_surface_format_unsupported:{descriptor.pixel_format}"
            )
        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_session_closed")
            submission_id = self._next_submission_id
            self._next_submission_id += 1
            self._pending[submission_id] = surface
            pts = descriptor.pts if descriptor.pts is not None else descriptor.frame_index
            pts = int(pts if pts is not None and pts >= 0 else submission_id - 1)
            duration = descriptor.duration_seconds
            duration_ticks = (
                max(1, int(round(float(duration) * self.fps))) if duration else 1
            )
        info_flags = _ctypes.c_uint32()
        # AUD-35206：会话指针的检查与 EncodeFrame 必须在同一 _session_lock
        # 临界区内。此前检查在 _lock 内、原生调用在锁外，close() 可在两步
        # 之间 Invalidate 并清空会话，EncodeFrame 随即作用于已失效句柄
        # （TOCTOU → 原生层未定义行为/崩溃）。
        try:
            with self._session_lock:
                session = self._session
                if not session.value:
                    raise VideoToolboxCompressionError("videotoolbox_session_closed")
                status = _VT_LIB.VTCompressionSessionEncodeFrame(
                    session,
                    _native_pointer(surface.native_handle),
                    _CMTime(pts, self.fps, 1, 0),
                    _CMTime(duration_ticks, self.fps, 1, 0),
                    None,
                    _ctypes.c_void_p(submission_id),
                    _ctypes.byref(info_flags),
                )
        except Exception:
            self._remove_pending_surface(submission_id)
            raise
        if int(status) != 0:
            self._remove_pending_surface(submission_id)
            raise VideoToolboxCompressionError(
                f"videotoolbox_compression_encode_failed:{int(status)}"
            )
        return {
            "queued": True,
            "submission_id": submission_id,
            # This is CPU enqueue information, deliberately not a GPU time.
            "gpu_submit_ms": None,
        }

    def complete(self) -> None:
        """Wait for all submitted frames and dispatch their packets."""
        # AUD-35206：与 submit() 同理，CompleteFrames 的会话检查与调用
        # 收进同一 _session_lock 临界区，close() 的 Invalidate 无法插入
        # 两者之间。VT 回调只抢 _lock，不会与本临界区互锁。
        with self._session_lock:
            session = self._session
            if not session.value:
                return
            status = _VT_LIB.VTCompressionSessionCompleteFrames(
                session,
                # A valid, very large CMTime means complete all frames up to it.
                _CMTime((1 << 62), self.fps, 1, 0),
            )
        if int(status) != 0:
            raise VideoToolboxCompressionError(
                f"videotoolbox_compression_complete_failed:{int(status)}"
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # AUD-35206：先关门再冲排。置 _closed 后 submit() 在登记阶段即
            # 被拒绝，消除"complete 排空后、Invalidate 前又有新帧挤入"的
            # 窗口。
            self._closed = True
        try:
            self.complete()
            # T-P0-31：CompleteFrames 之后，VT 仍在自己的队列上回调最后
            # 几个输出包（软件编码器帧重排尤甚）。必须等 pending 队列排
            # 空——bridge 关闭顺序是 session → muxer，若不等待，尾帧包会
            # 写进已关闭的容器（EINVAL）被静默丢弃（尾帧缺失）。
            import time as _time

            deadline = _time.monotonic() + 10.0
            while _time.monotonic() < deadline:
                with self._lock:
                    if not self._pending:
                        break
                _time.sleep(0.01)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logger.warning("VideoToolbox compression completion failed: %s", exc)
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        # AUD-35206：Invalidate 与 submit/complete 的原生调用共用
        # _session_lock，保证"会话仍有效"的判定对三者一致。
        with self._session_lock:
            session = self._session
            self._session = _ctypes.c_void_p()
            if session.value:
                _VT_LIB.VTCompressionSessionInvalidate(session)
        for surface in pending:
            surface.release()
        self._callback = None
        self._ref_pointer = None
        self._ref_object = None

    def _remove_pending_surface(self, submission_id: int) -> None:
        with self._lock:
            surface = self._pending.pop(int(submission_id), None)
        if surface is not None:
            surface.release()

    def _handle_native_callback(
        self,
        _ref_con: int,
        source_frame_ref_con: int,
        status: int,
        info_flags: int,
        sample_buffer: int,
    ) -> None:
        submission_id = int(source_frame_ref_con or 0)
        with self._lock:
            surface = self._pending.pop(submission_id, None)
        try:
            if int(status) != 0:
                # AUD-35208：丢包必须可观测——计数 + warning 留痕，导出侧
                # 可通过 dropped_packets 发现静默缺帧。
                with self._lock:
                    self._dropped_packets += 1
                    self._last_error = f"videotoolbox_callback_status:{int(status)}"
                logger.warning(
                    "VideoToolbox dropped encoded packet (submission_id=%s, "
                    "status=%s)",
                    submission_id,
                    int(status),
                )
                return
            if not sample_buffer:
                with self._lock:
                    self._dropped_packets += 1
                    self._last_error = "videotoolbox_callback_sample_buffer_missing"
                logger.warning(
                    "VideoToolbox dropped encoded packet (submission_id=%s, "
                    "sample buffer missing)",
                    submission_id,
                )
                return
            packet = self._extract_packet(
                int(sample_buffer), int(info_flags), submission_id
            )
            if self._on_packet is not None:
                self._on_packet(packet)
        except Exception as exc:  # pragma: no cover - native callback guard
            with self._lock:
                self._last_error = f"videotoolbox_callback_extract_failed:{exc}"
            logger.exception("VideoToolbox packet callback failed")
        finally:
            if surface is not None:
                surface.release()

    def _extract_packet(
        self,
        sample_buffer: int,
        info_flags: int,
        submission_id: int,
    ) -> VideoToolboxEncodedPacket:
        if _CM_LIB is None:
            raise VideoToolboxCompressionError("coremedia_unavailable")
        data_buffer = _CM_LIB.CMSampleBufferGetDataBuffer(
            _ctypes.c_void_p(sample_buffer)
        )
        if not data_buffer:
            raise VideoToolboxCompressionError("videotoolbox_packet_data_buffer_missing")
        data_length = int(_CM_LIB.CMBlockBufferGetDataLength(data_buffer))
        offset = _ctypes.c_size_t()
        total_length = _ctypes.c_size_t()
        data_pointer = _ctypes.c_void_p()
        status = _CM_LIB.CMBlockBufferGetDataPointer(
            data_buffer,
            0,
            _ctypes.byref(offset),
            _ctypes.byref(total_length),
            _ctypes.byref(data_pointer),
        )
        if int(status) != 0 or not data_pointer.value:
            raise VideoToolboxCompressionError(
                f"videotoolbox_packet_data_pointer_failed:{int(status)}"
            )
        data = _ctypes.string_at(data_pointer.value, data_length)
        pts, pts_timescale = _time_value(
            _CM_LIB.CMSampleBufferGetPresentationTimeStamp(
                _ctypes.c_void_p(sample_buffer)
            )
        )
        dts, _ = _time_value(
            _CM_LIB.CMSampleBufferGetDecodeTimeStamp(
                _ctypes.c_void_p(sample_buffer)
            )
        )
        duration, duration_timescale = _time_value(
            _CM_LIB.CMSampleBufferGetDuration(_ctypes.c_void_p(sample_buffer))
        )
        format_description = int(
            _CM_LIB.CMSampleBufferGetFormatDescription(
                _ctypes.c_void_p(sample_buffer)
            )
            or 0
        )
        parameter_sets, nal_length_size = _extract_parameter_sets(
            format_description, self.codec
        )
        return VideoToolboxEncodedPacket(
            data=data,
            codec=self.codec,
            pts=pts,
            dts=dts,
            duration=duration,
            timescale=pts_timescale or duration_timescale,
            keyframe=_packet_is_keyframe(data, self.codec, nal_length_size),
            parameter_sets=parameter_sets,
            nal_unit_length_size=nal_length_size,
            encode_info_flags=int(info_flags),
            submission_id=submission_id,
        )

    def __enter__(self) -> "VideoToolboxCompressionSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown guard
        try:
            self.close()
        except Exception:
            pass


class VideoToolboxPacketMuxer:
    """Mux H.264/HEVC packets from :class:`VideoToolboxCompressionSession`.

    VideoToolbox returns length-prefixed NAL units.  The matching
    ``*_videotoolbox`` stream keeps the caller-supplied AVCC or hvcC
    configuration record and accepts those packets directly.  This class
    never receives a pixel plane.  Audio is accepted through the same PyAV
    container boundary before the first video packet is muxed.
    """

    supports_audio = True

    def __init__(
        self,
        output_path: str,
        *,
        width: int,
        height: int,
        fps: int = 30,
        codec: str = "h264",
        pixel_format: str = "nv12",
        container_format: str | None = None,
    ) -> None:
        normalized, _ = _codec_fourcc(codec)
        if normalized not in {"h264", "hevc"}:
            raise VideoToolboxCompressionError(
                "videotoolbox_packet_muxer_codec_unsupported"
            )
        # AUD-35207：pixel_format 校验前移到 av.open 之前。此前先开容器、
        # add_stream 之后才校验，失败路径既不关容器也不删半成品输出文件，
        # 泄漏容器与文件句柄。
        normalized_pixel_format = str(pixel_format).strip().lower()
        if normalized_pixel_format not in {"nv12", "p010"}:
            raise VideoToolboxCompressionError(
                f"videotoolbox_packet_pixel_format_unsupported:{pixel_format}"
            )
        try:
            import av
        except ImportError as exc:  # pragma: no cover - project dependency guard
            raise VideoToolboxCompressionError("pyav_unavailable") from exc

        self._container = av.open(
            output_path,
            mode="w",
            format=container_format,
        )
        # AUD-35207：容器打开后的任何配置失败都必须关掉容器再抛出，
        # 不能把已打开的句柄遗留给 GC。
        try:
            self._stream = self._container.add_stream(
                f"{normalized}_videotoolbox",
                rate=Fraction(max(1, int(fps)), 1),
            )
            self._fps = max(1, int(fps))
            self._stream.width = int(width)
            self._stream.height = int(height)
            self._stream.pix_fmt = (
                "p010le" if normalized_pixel_format == "p010" else "yuv420p"
            )
        except Exception:
            self._container.close()
            raise
        self._pixel_format = normalized_pixel_format
        self._codec = normalized
        self._lock = threading.RLock()
        self._closed = False
        self._configured = False
        self._audio_stream = None
        self._has_muxed_packet = False
        self.packet_count = 0

    @property
    def stream(self) -> Any:
        return self._stream

    def write_packet(self, packet: VideoToolboxEncodedPacket) -> None:
        """Mux one compressed packet; safe for a serial VT callback queue."""
        if packet.codec != self._codec:
            raise VideoToolboxCompressionError(
                f"videotoolbox_packet_codec_mismatch:{packet.codec}!={self._codec}"
            )
        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_muxer_closed")
            if not self._configured:
                if self._codec == "h264":
                    extradata = _build_h264_avcc(
                        packet.parameter_sets,
                        packet.nal_unit_length_size,
                    )
                else:
                    extradata = _build_hevc_hvcc(
                        packet.parameter_sets,
                        packet.nal_unit_length_size,
                        self._pixel_format,
                    )
                self._stream.codec_context.extradata = extradata
                self._configured = True
            # Keep AVCC/length-prefixed payloads as emitted by VideoToolbox.
            # The matching VideoToolbox codec context and the AVC extradata
            # describe this representation to the MOV/MP4 muxer.
            payload = packet.data
            import av

            output_packet = av.Packet(payload)
            output_packet.stream = self._stream
            output_packet.pts = packet.pts
            output_packet.dts = packet.dts
            output_packet.duration = packet.duration
            output_packet.time_base = Fraction(
                1,
                packet.timescale or self._fps,
            )
            output_packet.is_keyframe = packet.keyframe
            self._container.mux(output_packet)
            self._has_muxed_packet = True
            self.packet_count += 1

    def add_audio_stream(
        self,
        codec: str = "aac",
        sample_rate: int = 48000,
        channels: int = 2,
        bitrate: int | None = None,
    ) -> None:
        """Add the TimelineExporter audio stream before video packets arrive."""
        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_muxer_closed")
            if self._has_muxed_packet:
                raise VideoToolboxCompressionError(
                    "audio_stream_must_be_added_before_video_mux"
                )
            if self._audio_stream is not None:
                raise VideoToolboxCompressionError("audio_stream_already_added")
            stream = self._container.add_stream(
                str(codec), rate=max(1, int(sample_rate))
            )
            try:
                from src.features.edit.core.channel_layout import ChannelLayout

                stream.layout = ChannelLayout.from_channel_count(
                    int(channels)
                ).av_layout_name
            except (ImportError, ValueError):
                stream.layout = (
                    "stereo" if int(channels) == 2
                    else "mono" if int(channels) == 1
                    else f"{int(channels)}c"
                )
            if bitrate:
                stream.options = {"b:a": f"{int(bitrate)}k"}
            self._audio_stream = stream

    def write_audio_samples(
        self,
        samples: Any,
        sample_rate: int = 48000,
    ) -> None:
        """Encode and mux float-planar audio samples beside VT video packets."""
        import av
        import numpy as np

        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_muxer_closed")
            if self._audio_stream is None:
                raise VideoToolboxCompressionError("audio_stream_not_configured")
            array = np.asarray(samples)
            if array.ndim == 1:
                array = array.reshape(1, -1)
            if array.ndim != 2 or array.shape[0] < 1:
                raise ValueError("audio samples must be a 1D or 2D array")
            array = np.ascontiguousarray(array, dtype=np.float32)
            channels = int(array.shape[0])
            layout = (
                "stereo" if channels == 2
                else "mono" if channels == 1
                else f"{channels}c"
            )
            for start in range(0, int(array.shape[1]), 1024):
                block = array[:, start:start + 1024]
                frame = av.AudioFrame.from_ndarray(
                    block, format="fltp", layout=layout
                )
                frame.sample_rate = max(1, int(sample_rate))
                for packet in self._audio_stream.encode(frame):
                    self._container.mux(packet)

    @property
    def audio_stream_configured(self) -> bool:
        with self._lock:
            return self._audio_stream is not None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._audio_stream is not None:
                    for packet in self._audio_stream.encode(None):
                        self._container.mux(packet)
            finally:
                self._container.close()

    def __enter__(self) -> "VideoToolboxPacketMuxer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "VideoToolboxCompressionError",
    "VideoToolboxCompressionSession",
    "VideoToolboxEncodedPacket",
    "VideoToolboxPacketMuxer",
]
