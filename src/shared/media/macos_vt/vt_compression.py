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
import math
import numbers
import platform
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from math import lcm
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from src.shared.export.gpu_encode_surface import GpuEncodeSurface

if TYPE_CHECKING:
    from src.shared.media.metadata import Rational

logger = logging.getLogger(__name__)

_MACOS_AVAILABLE = platform.system() == "Darwin"
_H264_PROFILE_LEVELS = {
    "baseline": "H264_Baseline_AutoLevel",
    "main": "H264_Main_AutoLevel",
    "high": "H264_High_AutoLevel",
}
# CMTime.timescale and FFmpeg's AVRational are int32 fields.
_MAX_TIMESCALE = (1 << 31) - 1
_DEFAULT_FRAME_RATE = Fraction(30)

# An exact frame rate: an integer, a ``Fraction``, or the plan-side rational
# (``Rational`` with ``num``/``den``).
FrameRate = Union[int, Fraction, "Rational"]


class VideoToolboxCompressionError(RuntimeError):
    """A native VideoToolbox compression operation failed."""


def coerce_frame_rate(value: FrameRate) -> Fraction:
    """Return one exact positive frame rate in frames per second.

    Fractional rates such as 30000/1001 stay exact.  An integral ``float`` is
    accepted for convenience, but a non-integral one is rejected rather than
    truncated into a different frame rate.
    """
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, bool):
        # ``bool`` is an Integral and a Real; ``True`` is not "1 fps".
        raise VideoToolboxCompressionError(
            f"videotoolbox_frame_rate_unsupported:{value!r}")
    elif isinstance(value, numbers.Integral):
        rate = Fraction(int(value))
    elif isinstance(value, numbers.Real):
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise VideoToolboxCompressionError(
                f"videotoolbox_frame_rate_not_exact:{value!r}")
        rate = Fraction(int(number))
    else:
        try:
            rate = Fraction(int(getattr(value, "num")), int(getattr(value, "den")))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError,
                OverflowError) as exc:
            raise VideoToolboxCompressionError(
                f"videotoolbox_frame_rate_unsupported:{value!r}") from exc
    if rate <= 0 or rate.numerator > _MAX_TIMESCALE:
        raise VideoToolboxCompressionError(
            f"videotoolbox_frame_rate_unsupported:{value!r}")
    return rate


def _cm_time_base(rate: Fraction) -> tuple[int, int]:
    """Return ``(timescale, frame_ticks)`` for one exact frame rate.

    ``timescale`` is the reduced numerator and each frame lasts ``frame_ticks``
    ticks, so frame ``n`` sits at exactly ``n * frame_ticks / timescale``
    seconds: 25 fps keeps the historical timescale 25, while 30000/1001 uses
    timescale 30000 with 1001 ticks per frame.
    """
    return rate.numerator, rate.denominator


def rescale_coremedia_timing(
    packet: "VideoToolboxEncodedPacket",
    fallback_frame_rate: FrameRate,
) -> tuple[Fraction, Optional[int], Optional[int], Optional[int]]:
    """Move PTS, DTS and duration from their own timescales into one time base.

    Returns ``(time_base, pts, dts, duration)``.  The three values are exact
    integer ticks of ``time_base``; a component whose CoreMedia time was
    invalid stays ``None``.  Each value is scaled by its own timescale.  A
    component without a dedicated timescale falls back to the packet's shared
    timescale and then to the frame-rate timescale, so packets built before
    per-component timescales existed keep their meaning.  The common base is
    the least common multiple of the timescales in use, which is the single
    packet timescale for everything VideoToolbox emits today.
    """
    shared = packet.timescale or coerce_frame_rate(fallback_frame_rate).numerator
    components: tuple[tuple[Optional[int], int], ...] = (
        (packet.pts, packet.timescale or shared),
        (packet.dts, packet.dts_timescale or shared),
        (packet.duration, packet.duration_timescale or shared),
    )
    stamps: list[tuple[int, int]] = []
    scales: set[int] = set()
    for value, scale in components:
        if value is None:
            continue
        scale = int(scale)
        if scale <= 0 or scale > _MAX_TIMESCALE:
            raise VideoToolboxCompressionError(
                f"videotoolbox_packet_timescale_invalid:{scale}")
        scales.add(scale)
        stamps.append((int(value), scale))
    unified = lcm(*sorted(scales)) if scales else int(shared)
    if unified <= 0 or unified > _MAX_TIMESCALE:
        raise VideoToolboxCompressionError(
            "videotoolbox_packet_time_base_unrepresentable:"
            f"{sorted(scales)}")
    index = 0
    converted: list[Optional[int]] = []
    for value, _scale in components:
        if value is not None:
            stamp_value, stamp_scale = stamps[index]
            index += 1
            converted.append(stamp_value * (unified // stamp_scale))
        else:
            converted.append(None)
    return Fraction(1, unified), converted[0], converted[1], converted[2]


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
    # ``timescale`` is the CoreMedia timescale of ``pts`` (or of ``duration``
    # when the PTS was invalid) and is kept for packet sinks built before
    # per-component timescales existed.  ``dts_timescale`` and
    # ``duration_timescale`` carry each component's own CoreMedia timescale
    # so the muxer can convert them independently.
    timescale: int | None
    keyframe: bool
    parameter_sets: tuple[bytes, ...] = ()
    nal_unit_length_size: int = 4
    encode_info_flags: int = 0
    submission_id: int = 0
    dts_timescale: int | None = None
    duration_timescale: int | None = None


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
    _VT_LIB.VTSessionSetProperty.restype = _ctypes.c_int32
    _VT_LIB.VTSessionSetProperty.argtypes = [
        _ctypes.c_void_p, _ctypes.c_void_p, _ctypes.c_void_p,
    ]

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


def _compression_core_foundation() -> Any:
    cf = _ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFStringCreateWithCString.restype = _ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [
        _ctypes.c_void_p, _ctypes.c_char_p, _ctypes.c_uint32,
    ]
    cf.CFNumberCreate.restype = _ctypes.c_void_p
    cf.CFNumberCreate.argtypes = [
        _ctypes.c_void_p, _ctypes.c_long, _ctypes.c_void_p,
    ]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [_ctypes.c_void_p]
    return cf


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
        fps: FrameRate = 30,
        pixel_format: str = "nv12",
        on_packet: Optional[Callable[[VideoToolboxEncodedPacket], Any]] = None,
        require_hardware: bool = True,
        h264_profile: str | None = None,
        bit_rate: int | None = None,
        bt709_limited: bool = False,
    ) -> None:
        if not _MACOS_AVAILABLE or _VT_LIB is None or _CM_LIB is None:
            raise VideoToolboxCompressionError(
                "videotoolbox_compression_unavailable"
            )
        self.width = int(width)
        self.height = int(height)
        if self.width <= 0 or self.height <= 0:
            raise VideoToolboxCompressionError("invalid_videotoolbox_dimensions")
        self.codec, codec_type = _codec_fourcc(codec)
        self._pixel_format = str(pixel_format).strip().lower()
        if self._pixel_format not in {"nv12", "p010"}:
            raise VideoToolboxCompressionError(
                "videotoolbox_session_pixel_format_unsupported:"
                f"{pixel_format}"
            )
        profile = h264_profile.strip().lower() if h264_profile is not None else "auto"
        if h264_profile is not None and (self.codec != "h264"
                or (profile != "auto" and profile not in _H264_PROFILE_LEVELS)):
            raise VideoToolboxCompressionError(
                f"videotoolbox_h264_profile_unsupported:{h264_profile}")
        if bit_rate is not None and (type(bit_rate) is not int or not 0 < bit_rate < 1 << 63):
            raise VideoToolboxCompressionError("videotoolbox_invalid_average_bit_rate")
        self._bt709_limited = bt709_limited
        # Exact frame rate. CoreMedia timestamps use the reduced numerator as
        # their timescale and the denominator as the tick count of one frame,
        # so 30000/1001 keeps 1001-tick frames instead of being truncated to
        # an integer rate that drifts against the audio clock.
        self.fps = coerce_frame_rate(fps)
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
        self._cf = _compression_core_foundation()
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
            self._close_session()
            raise VideoToolboxCompressionError(
                f"videotoolbox_compression_session_create_failed:{int(status)}"
            )
        try:
            # P010 must use Main10 so a rejected profile cannot silently
            # reduce precision to the native encoder's 8-bit default.
            level = _H264_PROFILE_LEVELS.get(profile)
            if self.codec == "hevc":
                level = ("HEVC_Main10_AutoLevel" if self._pixel_format == "p010"
                         else "HEVC_Main_AutoLevel")
            if level is not None:
                set_status = self._set_profile_level(level)
                if set_status:
                    raise VideoToolboxCompressionError(
                        f"videotoolbox_{self.codec}_profile_level_rejected:"
                        f"{self._pixel_format}:{set_status}")
            settings: dict[str, str | int] = {}
            if bit_rate is not None:
                settings["AverageBitRate"] = bit_rate
            if bt709_limited:
                settings.update(dict.fromkeys(
                    ("ColorPrimaries", "TransferFunction", "YCbCrMatrix"), "ITU_R_709_2"))
            for key, value in settings.items():
                set_status = self._set_property(key, value)
                if set_status:
                    raise VideoToolboxCompressionError(
                        f"videotoolbox_compression_property_rejected:{key}:{set_status}")
        except BaseException:
            self._close_session()
            raise

    def _set_profile_level(self, level: str) -> int:
        """Set VTCompressionPropertyKey_ProfileLevel; return the raw status."""
        return self._set_property("ProfileLevel", level)

    def _set_property(self, key: str, value: str | int) -> int:
        """Set a CFString or 64-bit CFNumber compression property."""
        cf = self._cf
        encoding_utf8 = 0x08000100
        key_ref = cf.CFStringCreateWithCString(None, key.encode("ascii"), encoding_utf8)
        value_ref = None
        try:
            if isinstance(value, str):
                value_ref = cf.CFStringCreateWithCString(None, value.encode("ascii"), encoding_utf8)
            else:
                number = _ctypes.c_int64(value)
                value_ref = cf.CFNumberCreate(None, 4, _ctypes.byref(number))  # kCFNumberSInt64Type
            if not key_ref or not value_ref:
                raise VideoToolboxCompressionError(f"videotoolbox_property_allocation_failed:{key}")
            return int(_VT_LIB.VTSessionSetProperty(self._session, key_ref, value_ref))
        finally:
            if key_ref:
                cf.CFRelease(key_ref)
            if value_ref:
                cf.CFRelease(value_ref)

    def _close_session(self) -> None:
        """Tear down the current compression session (idempotent)."""
        with self._session_lock:
            session = self._session
            self._session = _ctypes.c_void_p()
            if session is not None and session.value:
                try:
                    _VT_LIB.VTCompressionSessionInvalidate(session)
                finally:
                    cf = getattr(self, "_cf", None)
                    if cf is not None:
                        cf.CFRelease(session)

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
        if getattr(self, "_bt709_limited", False) and (
                str(descriptor.color_range).lower() != "limited"
                or str(descriptor.color_matrix).lower() != "bt709"):
            raise VideoToolboxCompressionError("videotoolbox_surface_color_metadata_mismatch")
        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_session_closed")
            submission_id = self._next_submission_id
            self._next_submission_id += 1
            self._pending[submission_id] = surface
            rate = coerce_frame_rate(getattr(self, "fps", _DEFAULT_FRAME_RATE))
            timescale, frame_ticks = _cm_time_base(rate)
            pts = descriptor.pts if descriptor.pts is not None else descriptor.frame_index
            pts = int(pts if pts is not None and pts >= 0 else submission_id - 1)
            pts *= frame_ticks
            duration = descriptor.duration_seconds
            duration_ticks = (
                max(1, int(round(Fraction(float(duration)) * rate)))
                if duration else frame_ticks
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
                    _CMTime(pts, timescale, 1, 0),
                    _CMTime(duration_ticks, timescale, 1, 0),
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
            timescale, _frame_ticks = _cm_time_base(coerce_frame_rate(
                getattr(self, "fps", _DEFAULT_FRAME_RATE)))
            status = _VT_LIB.VTCompressionSessionCompleteFrames(
                session,
                # A valid, very large CMTime means complete all frames up to it.
                _CMTime((1 << 62), timescale, 1, 0),
            )
        if int(status) != 0:
            raise VideoToolboxCompressionError(
                f"videotoolbox_compression_complete_failed:{int(status)}"
            )

    def close(self) -> None:
        self._finish(flush=True)

    def abort(self) -> None:
        self._finish(flush=False)

    def _finish(self, *, flush: bool) -> None:
        with self._lock:
            if self._closed:
                return
            # AUD-35206：先关门再冲排。置 _closed 后 submit() 在登记阶段即
            # 被拒绝，消除"complete 排空后、Invalidate 前又有新帧挤入"的
            # 窗口。
            self._closed = True
        try:
            if flush:
                try:
                    self.complete()
                    deadline = time.monotonic() + 10.0
                    while time.monotonic() < deadline:
                        if self.pending_count == 0:
                            break
                        time.sleep(0.01)
                    if self.pending_count:
                        raise VideoToolboxCompressionError("videotoolbox_compression_flush_timeout")
                except Exception as exc:
                    with self._lock:
                        self._last_error = str(exc)
                    logger.warning("VideoToolbox compression completion failed: %s", exc)
        finally:
            try:
                self._close_session()
            finally:
                with self._lock:
                    pending = list(self._pending.values())
                    self._pending.clear()
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
            surface = self._pending.get(submission_id)
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
                self._dropped_packets += 1
                self._last_error = f"videotoolbox_callback_extract_failed:{exc}"
            logger.exception("VideoToolbox packet callback failed")
        finally:
            # Leave the entry pending until the packet sink returns. Flush
            # must not close the muxer while a callback is still writing.
            try:
                if surface is not None:
                    surface.release()
            finally:
                with self._lock:
                    self._pending.pop(submission_id, None)

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
        dts, dts_timescale = _time_value(
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
            dts_timescale=dts_timescale,
            duration_timescale=duration_timescale,
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

    ``fps`` is exact: an integer, a ``Fraction``, or the plan-side rational.
    CoreMedia PTS, DTS and duration are each converted from their own
    timescale into one packet time base before the muxer rescales them to the
    track timescale, so 24000/1001 and 30000/1001 timelines keep their frame
    spacing without end-of-file drift. Audio frames carry cumulative
    sample-count PTS at ``1 / sample_rate``, which lets the container write
    the AAC priming edit list exactly like the regular software encoder.
    """

    supports_audio = True

    def __init__(
        self,
        output_path: str,
        *,
        width: int,
        height: int,
        fps: FrameRate = 30,
        codec: str = "h264",
        pixel_format: str = "nv12",
        container_format: str | None = None,
        bt709_limited: bool = False,
    ) -> None:
        normalized, _ = _codec_fourcc(codec)
        if normalized not in {"h264", "hevc"}:
            raise VideoToolboxCompressionError(
                "videotoolbox_packet_muxer_codec_unsupported"
            )
        # AUD-35207：pixel_format 校验前移到 av.open 之前。此前先开容器、
        # add_stream 之后才校验，失败路径既不关容器也不删半成品输出文件，
        # 泄漏容器与文件句柄。帧率同理：非法帧率不得先建容器再失败。
        normalized_pixel_format = str(pixel_format).strip().lower()
        if normalized_pixel_format not in {"nv12", "p010"}:
            raise VideoToolboxCompressionError(
                f"videotoolbox_packet_pixel_format_unsupported:{pixel_format}"
            )
        frame_rate = coerce_frame_rate(fps)
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
                rate=frame_rate,
            )
            self._fps = frame_rate
            self._stream.width = int(width)
            self._stream.height = int(height)
            self._stream.pix_fmt = (
                "p010le" if normalized_pixel_format == "p010" else "yuv420p"
            )
            if bt709_limited:
                # FFmpeg's BT.709 and MPEG range enumerators are all 1.
                self._stream.color_primaries = 1
                self._stream.color_trc = 1
                self._stream.colorspace = 1
                self._stream.color_range = 1
        except Exception:
            self._container.close()
            raise
        self._pixel_format = normalized_pixel_format
        self._codec = normalized
        self._lock = threading.RLock()
        self._closed = False
        self._configured = False
        self._audio_stream = None
        self._audio_layout_name = ""
        self._audio_channels = 0
        self._audio_sample_rate = 0
        self._audio_samples_written = 0
        self._audio_packets_muxed = 0
        self._audio_required = False
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

            time_base, pts, dts, duration = rescale_coremedia_timing(
                packet, self._fps)
            output_packet = av.Packet(payload)
            output_packet.stream = self._stream
            output_packet.pts = pts
            output_packet.dts = dts
            output_packet.duration = duration
            output_packet.time_base = time_base
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
        *,
        required: bool = False,
    ) -> None:
        """Add the TimelineExporter audio stream before video packets arrive.

        ``required`` marks a delivery that must not be published without this
        track: :meth:`close` then refuses to finalize when no audio sample
        reached the container.
        """
        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_muxer_closed")
            if self._has_muxed_packet:
                raise VideoToolboxCompressionError(
                    "audio_stream_must_be_added_before_video_mux"
                )
            if self._audio_stream is not None:
                raise VideoToolboxCompressionError("audio_stream_already_added")
            sample_rate = max(1, int(sample_rate))
            channels = int(channels)
            if channels < 1:
                raise VideoToolboxCompressionError(
                    f"videotoolbox_audio_channel_count_invalid:{channels}")
            stream = self._container.add_stream(str(codec), rate=sample_rate)
            try:
                from src.features.edit.core.channel_layout import ChannelLayout

                layout = ChannelLayout.from_channel_count(channels).av_layout_name
            except (ImportError, ValueError):
                layout = (
                    "stereo" if channels == 2
                    else "mono" if channels == 1
                    else f"{channels}c"
                )
            stream.layout = layout
            if bitrate:
                # AUD-33505: the AVOption name is ``b``. The CLI spelling
                # ``b:a`` carries a stream specifier that avcodec_open2
                # ignores, which silently left AAC at the encoder default.
                stream.options = {"b": f"{int(bitrate)}k"}
            self._audio_stream = stream
            self._audio_layout_name = layout
            self._audio_channels = channels
            self._audio_sample_rate = sample_rate
            self._audio_samples_written = 0
            self._audio_packets_muxed = 0
            self._audio_required = bool(required)

    def write_audio_samples(
        self,
        samples: Any,
        sample_rate: int = 48000,
    ) -> int:
        """Encode and mux float-planar audio samples beside VT video packets.

        ``samples`` is ``(channels, samples)`` (or 1-D for a mono stream) at
        the rate and channel count declared by :meth:`add_audio_stream`. The
        stream's declared layout names the channel order, so the planes keep
        the regular exporter's ordering. Every 1024-sample frame is stamped
        with the cumulative sample offset at ``1 / sample_rate``; the AAC
        priming therefore lands in a container edit list instead of delaying
        the programme by one frame.

        Returns the next free sample offset, which is the ``start_sample`` of
        a following call.
        """
        import av
        import numpy as np

        with self._lock:
            if self._closed:
                raise VideoToolboxCompressionError("videotoolbox_muxer_closed")
            if self._audio_stream is None:
                raise VideoToolboxCompressionError("audio_stream_not_configured")
            if int(sample_rate) != self._audio_sample_rate:
                raise VideoToolboxCompressionError(
                    "videotoolbox_audio_sample_rate_mismatch:"
                    f"{sample_rate}!={self._audio_sample_rate}")
            array = np.asarray(samples)
            if array.ndim == 1:
                array = array.reshape(1, -1)
            if array.ndim != 2 or array.shape[0] < 1:
                raise ValueError("audio samples must be a 1D or 2D array")
            if int(array.shape[0]) != self._audio_channels:
                raise VideoToolboxCompressionError(
                    "videotoolbox_audio_channel_mismatch:"
                    f"{array.shape[0]}!={self._audio_channels}")
            array = np.ascontiguousarray(array, dtype=np.float32)
            time_base = Fraction(1, self._audio_sample_rate)
            start_sample = self._audio_samples_written
            for offset in range(0, int(array.shape[1]), 1024):
                block = array[:, offset:offset + 1024]
                frame = av.AudioFrame.from_ndarray(
                    block, format="fltp", layout=self._audio_layout_name
                )
                frame.sample_rate = self._audio_sample_rate
                frame.time_base = time_base
                frame.pts = start_sample + offset
                for packet in self._audio_stream.encode(frame):
                    self._container.mux(packet)
                    self._audio_packets_muxed += 1
            self._audio_samples_written = start_sample + int(array.shape[1])
            return self._audio_samples_written

    @property
    def audio_stream_configured(self) -> bool:
        with self._lock:
            return self._audio_stream is not None

    @property
    def audio_samples_written(self) -> int:
        """Cumulative sample offset fed to the audio encoder."""
        with self._lock:
            return self._audio_samples_written

    @property
    def audio_packets_muxed(self) -> int:
        """Number of encoded audio packets that reached the container."""
        with self._lock:
            return self._audio_packets_muxed

    def close(self) -> None:
        self._close_container(flush_audio=True)

    def abort(self) -> None:
        self._close_container(flush_audio=False)

    def _close_container(self, *, flush_audio: bool) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if flush_audio and self._audio_stream is not None:
                    for packet in self._audio_stream.encode(None):
                        self._container.mux(packet)
                        self._audio_packets_muxed += 1
                    if self._audio_required and self._audio_packets_muxed == 0:
                        # A delivery that asked for this track is not finalized
                        # without it. Checked after the drain so an encoder that
                        # produced no packet counts as a missing track as well.
                        raise VideoToolboxCompressionError(
                            "videotoolbox_audio_track_missing")
            finally:
                self._container.close()

    def __enter__(self) -> "VideoToolboxPacketMuxer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "FrameRate",
    "VideoToolboxCompressionError",
    "VideoToolboxCompressionSession",
    "VideoToolboxEncodedPacket",
    "VideoToolboxPacketMuxer",
    "coerce_frame_rate",
    "rescale_coremedia_timing",
]
