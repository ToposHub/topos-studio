"""
macOS VideoToolbox Decompression Session
封装 VTDecompressionSession，输出 IOSurface-backed CVPixelBuffer。

## 职责

- 创建 VTDecompressionSession（H.264 / HEVC）
- 解码压缩 packet 到 CVPixelBuffer
- CVPixelBuffer 必须包含 IOSurface backing
- 输出 VtDecodedPixelBuffer

## 线程安全设计

VT 回调线程：
  - 只 retain CVPixelBuffer
  - 放入 thread-safe queue
  - 不碰 OpenGL

渲染线程：
  - 取 CVPixelBuffer
  - 调 CGLTexImageIOSurface2D
  - 转成 GpuDecodedFrame planes
  - 渲染

## 约束

- 仅 macOS 可用（PyObjC + ctypes）
- 平台检查：platform.system() == 'Darwin'
- 不调用 frame.to_ndarray() / frame.transfer()
- 输出 pixel buffer 必须有 IOSurface backing
- VTDecompressionSessionCreate 通过 ctypes 调用（PyObjC 无法正确注册回调）
"""

from __future__ import annotations

import logging
import platform
import queue
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_MACOS_AVAILABLE = platform.system() == 'Darwin'
_VT_DECODE_IMPLEMENTED = False  # Set to True below if all dependencies loaded

# ── ctypes structures for VT decode pipeline ──
# ctypes 用于 VTDecompressionSessionCreate 的回调注册。
# PyObjC 无法正确传递 output callback（回调不会被调用），
# 因此必须用 ctypes CFUNCTYPE 创建 C 回调函数。
import ctypes as _ctypes


class _CMTime(_ctypes.Structure):
    """CoreMedia CMTime structure (value, timescale, flags, epoch)."""
    _fields_ = [
        ('value', _ctypes.c_int64),
        ('timescale', _ctypes.c_int32),
        ('flags', _ctypes.c_uint32),
        ('epoch', _ctypes.c_int64),
    ]


class _VTCallbackRecord(_ctypes.Structure):
    """VTDecompressionOutputCallbackRecord (callback + refCon)."""
    _fields_ = [
        ('callback', _ctypes.c_void_p),
        ('refCon', _ctypes.c_void_p),
    ]


# VTDecompressionOutputCallback C function type
# void (*)(void* refCon, void* frameRefCon, OSStatus status,
#           VTDecodeInfoFlags infoFlags, CVImageBufferRef imageBuffer,
#           CMTime pts, CMTime duration)
_VT_DECODE_CALLBACK = _ctypes.CFUNCTYPE(
    None,
    _ctypes.c_void_p,   # refCon
    _ctypes.c_void_p,   # frameRefCon
    _ctypes.c_int32,    # status (OSStatus)
    _ctypes.c_uint32,   # infoFlags (VTDecodeInfoFlags)
    _ctypes.c_void_p,   # imageBuffer (CVImageBufferRef)
    _CMTime,            # pts
    _CMTime,            # duration
)

# ── Cached ctypes library handles (loaded once, reused) ──
_VT_LIB = None       # VideoToolbox.framework
_CV_LIB = None       # CoreVideo.framework
_CM_LIB = None       # CoreMedia.framework
_CF_LIB = None       # CoreFoundation.framework

if _MACOS_AVAILABLE:
    try:
        _VT_LIB = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox'
        )
        _CV_LIB = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreVideo.framework/CoreVideo'
        )
        _CM_LIB = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreMedia.framework/CoreMedia'
        )
        _CF_LIB = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
        )

        # Configure VT functions
        _VT_LIB.VTDecompressionSessionCreate.restype = _ctypes.c_int  # OSStatus
        _VT_LIB.VTDecompressionSessionCreate.argtypes = [
            _ctypes.c_void_p,                                    # allocator
            _ctypes.c_void_p,                                    # formatDescription
            _ctypes.c_void_p,                                    # decoderSpecification
            _ctypes.c_void_p,                                    # destinationImageBufferAttributes
            _ctypes.POINTER(_VTCallbackRecord),                  # outputCallback
            _ctypes.POINTER(_ctypes.c_void_p),                   # decompressionSessionOut
        ]
        _VT_LIB.VTDecompressionSessionDecodeFrame.restype = _ctypes.c_int
        _VT_LIB.VTDecompressionSessionDecodeFrame.argtypes = [
            _ctypes.c_void_p,   # session
            _ctypes.c_void_p,   # sampleBuffer
            _ctypes.c_uint32,   # decodeFlags
            _ctypes.c_void_p,   # sourceFrameRefCon
            _ctypes.POINTER(_ctypes.c_uint32),  # infoFlagsOut (nullable)
        ]
        _VT_LIB.VTDecompressionSessionInvalidate.restype = None
        _VT_LIB.VTDecompressionSessionInvalidate.argtypes = [_ctypes.c_void_p]
        _VT_LIB.VTDecompressionSessionWaitForAsynchronousFrames.restype = _ctypes.c_int
        _VT_LIB.VTDecompressionSessionWaitForAsynchronousFrames.argtypes = [_ctypes.c_void_p]

        # Configure CV functions
        _CV_LIB.CVPixelBufferRetain.restype = _ctypes.c_void_p
        _CV_LIB.CVPixelBufferRetain.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferRelease.restype = None
        _CV_LIB.CVPixelBufferRelease.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferGetIOSurface.restype = _ctypes.c_void_p
        _CV_LIB.CVPixelBufferGetIOSurface.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferGetWidth.restype = _ctypes.c_int
        _CV_LIB.CVPixelBufferGetWidth.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferGetHeight.restype = _ctypes.c_int
        _CV_LIB.CVPixelBufferGetHeight.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferGetPlaneCount.restype = _ctypes.c_int
        _CV_LIB.CVPixelBufferGetPlaneCount.argtypes = [_ctypes.c_void_p]
        _CV_LIB.CVPixelBufferGetBaseAddressOfPlane.restype = _ctypes.c_void_p
        _CV_LIB.CVPixelBufferGetBaseAddressOfPlane.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
        ]
        _CV_LIB.CVPixelBufferGetBytesPerRowOfPlane.restype = _ctypes.c_size_t
        _CV_LIB.CVPixelBufferGetBytesPerRowOfPlane.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
        ]
        _CV_LIB.CVPixelBufferLockBaseAddress.restype = _ctypes.c_int
        _CV_LIB.CVPixelBufferLockBaseAddress.argtypes = [_ctypes.c_void_p, _ctypes.c_uint64]
        _CV_LIB.CVPixelBufferUnlockBaseAddress.restype = _ctypes.c_int
        _CV_LIB.CVPixelBufferUnlockBaseAddress.argtypes = [_ctypes.c_void_p, _ctypes.c_uint64]

        # CoreFoundation: VTDecompressionSessionCreate 返回的会话是 +1
        # 引用（Create Rule），Invalidate 后必须 CFRelease（AUD-35301）。
        _CF_LIB.CFRelease.restype = None
        _CF_LIB.CFRelease.argtypes = [_ctypes.c_void_p]

        # Note: CM BlockBuffer/SampleBuffer creation uses PyObjC (not ctypes).
        # ctypes is only needed for VT session creation and decode frame submission.
        # PyObjC's CMBlockBufferCreateWithMemoryBlock and CMSampleBufferCreateReady
        # work correctly and return properly wrapped CF objects.

        logger.info("macOS VT: ctypes libraries loaded (VideoToolbox, CoreVideo, CoreMedia, CoreFoundation)")
    except Exception as _ctypes_err:
        logger.warning(f"macOS VT: ctypes library load failed: {_ctypes_err}")
        _VT_LIB = None
        _CV_LIB = None
        _CM_LIB = None
        _CF_LIB = None


# PyObjC imports — each import grouped so we can track what's available
_PYOBJC_AVAILABLE = False
_OBJC_LOADED = False
_COREFOUNDATION_LOADED = False
_QUARTZ_VT_LOADED = False
_COREMEDIA_LOADED = False
_COREVIDEO_LOADED = False
_QUARTZ_CV_LOADED = False
_VT_HW_ACCEL_KEY = None  # Hardware accel constant (not exported by PyObjC VideoToolbox)

if _MACOS_AVAILABLE:
    try:
        import objc as _objc_module
        try:
            from Foundation import NSObject
        except ImportError:
            from Cocoa import NSObject
        _OBJC_LOADED = True
    except ImportError:
        pass  # _OBJC_LOADED remains False

    try:
        # 可用性探测导入：名字本身不使用，成功与否决定 _COREFOUNDATION_LOADED
        from CoreFoundation import (
            CFAllocatorRef,  # noqa: F401
            kCFAllocatorDefault,  # noqa: F401
        )
        _COREFOUNDATION_LOADED = True
    except ImportError:
        pass

    try:
        # VideoToolbox core APIs are in separate VideoToolbox module (pyobjc-framework-VideoToolbox)
        # Some VT functions like VTDecompressionSessionSetProperty are not exported by PyObjC
        from VideoToolbox import (
            VTDecompressionSessionInvalidate,
            VTDecompressionSessionWaitForAsynchronousFrames,
        )
        _QUARTZ_VT_LOADED = True
        # Optional property APIs - may not be available in PyObjC
        _VT_SESSION_SET_PROPERTY = None
        _VT_PROPERTY_KEY = None
        _VT_PROPERTY_REALTIME = None
        try:
            from VideoToolbox import (
                VTDecompressionSessionSetProperty,
                kVTDecompressionProperty_Key,
                kVTDecompressionProperty_RealTime,
            )
            _VT_SESSION_SET_PROPERTY = VTDecompressionSessionSetProperty
            _VT_PROPERTY_KEY = kVTDecompressionProperty_Key
            _VT_PROPERTY_REALTIME = kVTDecompressionProperty_RealTime
        except ImportError:
            pass  # Property setting is optional
        # Hardware accel constant (not exposed via Quartz on all PyObjC builds)
        try:
            from VideoToolbox import (
                kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder,
            )
            _VT_HW_ACCEL_KEY = kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder
        except ImportError:
            try:
                from Quartz import kVTVideoDecoder_RequireHardwareAcceleratedVideoDecoder
                _VT_HW_ACCEL_KEY = kVTVideoDecoder_RequireHardwareAcceleratedVideoDecoder
            except ImportError:
                pass  # Hardware accel requirement will be skipped
    except ImportError:
        pass

    try:
        # 可用性探测导入：名字本身不使用（实际走 ctypes _CM_LIB），成功与否决定 _COREMEDIA_LOADED
        from CoreMedia import (
            CMBlockBufferCreateWithMemoryBlock,  # noqa: F401
            CMSampleTimingInfo,  # noqa: F401
            CMSampleBufferCreateReady,  # noqa: F401
            CMVideoFormatDescriptionCreateFromH264ParameterSets,  # noqa: F401
            CMVideoFormatDescriptionCreateFromHEVCParameterSets,  # noqa: F401
        )
        _COREMEDIA_LOADED = True
    except ImportError:
        pass

    try:
        # CoreVideo APIs are in Quartz.CoreVideo module
        from Quartz.CoreVideo import (
            CVPixelBufferGetIOSurface,
            CVPixelBufferRetain,
            CVPixelBufferRelease,
        )
        _COREVIDEO_LOADED = True
    except ImportError:
        pass

    try:
        # 可用性探测导入：名字本身不使用，成功与否决定 _QUARTZ_CV_LOADED
        from Quartz import (
            kCVPixelBufferIOSurfacePropertiesKey,  # noqa: F401
            kCVPixelBufferOpenGLCompatibilityKey,  # noqa: F401
            CVPixelBufferPoolCreate,  # noqa: F401
        )
        _QUARTZ_CV_LOADED = True
    except ImportError:
        pass

    _PYOBJC_AVAILABLE = (
        _OBJC_LOADED
        and _COREFOUNDATION_LOADED
        and _QUARTZ_VT_LOADED
        and _COREMEDIA_LOADED
        and _COREVIDEO_LOADED
        and _QUARTZ_CV_LOADED
        and _VT_LIB is not None       # ctypes VideoToolbox loaded
        and _CV_LIB is not None       # ctypes CoreVideo loaded
        and _CM_LIB is not None       # ctypes CoreMedia loaded
    )

    if not _PYOBJC_AVAILABLE:
        _missing = []
        if not _OBJC_LOADED:
            _missing.append("Cocoa/objc")
        if not _COREFOUNDATION_LOADED:
            _missing.append("CoreFoundation")
        if not _QUARTZ_VT_LOADED:
            _missing.append("Quartz.VT")
        if not _COREMEDIA_LOADED:
            _missing.append("CoreMedia")
        if not _COREVIDEO_LOADED:
            _missing.append("CoreVideo")
        if not _QUARTZ_CV_LOADED:
            _missing.append("Quartz.CV")
        if _VT_LIB is None:
            _missing.append("ctypes.VideoToolbox")
        if _CV_LIB is None:
            _missing.append("ctypes.CoreVideo")
        if _CM_LIB is None:
            _missing.append("ctypes.CoreMedia")
        logger.warning(f"macOS VT: PyObjC/ctypes not fully available (missing: {', '.join(_missing)}), zero-copy disabled")
    else:
        # All VT decode dependencies loaded — the decode implementation
        # uses ctypes for VTDecompressionSessionCreate (callback registration)
        # and PyObjC for format description / pixel buffer attributes creation.
        _VT_DECODE_IMPLEMENTED = True
        logger.info("macOS VT: decode implementation ready (ctypes VTDecompressionSession + PyObjC helpers + IOSurface)")
else:
    _PYOBJC_AVAILABLE = False


@dataclass
class VtDecodedPixelBuffer:
    """
    VT 解码后的 CVPixelBuffer 描述。

    不持有 CPU 数据，只引用 GPU 上的 pixel buffer。

    Attributes:
        pixel_buffer: CVPixelBufferRef（PyObjC 对象或 ctypes raw pointer int）
        pts: 展示时间戳（stream time_base 单位）
        time_seconds: 展示时间（秒）
        width: 视频宽度
        height: 视频高度
        pixel_format: 像素格式描述（nv12, p010）
        bit_depth: 位深度
        color_metadata: 颜色元数据字典
        frame_index: 帧号
        is_iosurface_backed: 是否有 IOSurface backing
    """
    pixel_buffer: object
    pts: int | None
    time_seconds: float
    width: int
    height: int
    pixel_format: str = "nv12"
    bit_depth: int = 8
    color_metadata: dict | None = None
    frame_index: int = 0
    is_iosurface_backed: bool = False


# ================================================================
# VT 解码输出队列（线程安全）
# ================================================================

class VtDecodeOutputQueue:
    """
    线程安全的 CVPixelBuffer 输出队列。

    VT 回调线程：
      - 只 retain CVPixelBuffer
      - 放入队列
      - 不碰 OpenGL

    渲染线程：
      - 取 CVPixelBuffer
      - 做 GL 绑定
    """

    def __init__(self, max_size: int = 8) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=max_size)
        # AUD-35309：保护跨线程诊断计数器（VT 回调线程写、渲染/管理线程读）。
        self._lock = threading.Lock()
        self._dropped_count: int = 0
        self._total_count: int = 0

    def put_pixel_buffer(
        self,
        pixel_buffer: object,
        pts: int | None,
        frame_index: int,
        width: int,
        height: int,
        pixel_format: str,
        bit_depth: int,
    ) -> bool:
        """
        VT 回调线程调用。
        只 retain CVPixelBuffer，放入队列。
        不碰 OpenGL。

        pixel_buffer 可以是：
        - PyObjC CVPixelBufferRef 对象（传统路径）
        - ctypes raw pointer (int)（ctypes 回调路径）

        PyObjC CF functions (CVPixelBufferRetain, CVPixelBufferGetIOSurface 等)
        接受 raw int pointer，因此两种格式都兼容下游的 IOSurfaceGLBridge。

        Args:
            pixel_buffer: CVPixelBufferRef 或 raw pointer int
            pts: 展示时间戳
            frame_index: 帧号
            width: 宽度
            height: 高度
            pixel_format: 像素格式
            bit_depth: 位深度

        Returns:
            True 成功放入队列，False 队列满丢弃
        """
        if not _PYOBJC_AVAILABLE:
            return False

        try:
            # Retain CVPixelBuffer（增加引用计数）。
            # For raw int pointers (from ctypes callback), use ctypes CVPixelBufferRetain.
            # For PyObjC objects, use PyObjC CVPixelBufferRetain.
            if isinstance(pixel_buffer, int):
                _CV_LIB.CVPixelBufferRetain(_ctypes.c_void_p(pixel_buffer))
            else:
                CVPixelBufferRetain(pixel_buffer)

            # 检查 IOSurface backing.
            # PyObjC CVPixelBufferGetIOSurface does NOT work with raw int pointers
            # (returns None even when IOSurface exists). Use ctypes version for raw pointers.
            if isinstance(pixel_buffer, int):
                io_ptr = _CV_LIB.CVPixelBufferGetIOSurface(_ctypes.c_void_p(pixel_buffer))
                is_iosurface = io_ptr is not None and io_ptr != 0
            else:
                is_iosurface = CVPixelBufferGetIOSurface(pixel_buffer) is not None

            decoded = VtDecodedPixelBuffer(
                pixel_buffer=pixel_buffer,
                pts=pts,
                time_seconds=0.0,  # 需要外部转换
                width=width,
                height=height,
                pixel_format=pixel_format,
                bit_depth=bit_depth,
                frame_index=frame_index,
                is_iosurface_backed=is_iosurface,
            )

            self._queue.put(decoded, timeout=0.1)
            # AUD-35309：+= 是读-改-写（非原子），必须持锁保护
            with self._lock:
                self._total_count += 1
            return True

        except queue.Full:
            # 队列满，丢弃并释放
            if isinstance(pixel_buffer, int):
                _CV_LIB.CVPixelBufferRelease(_ctypes.c_void_p(pixel_buffer))
            else:
                CVPixelBufferRelease(pixel_buffer)
            with self._lock:
                self._dropped_count += 1
            logger.debug(f"VtDecodeOutputQueue: queue full, dropped frame {frame_index}")
            return False

        except Exception as e:
            logger.error(f"VtDecodeOutputQueue: put error: {e}")
            return False

    def get_pixel_buffer(self, timeout: float = 0.5) -> Optional[VtDecodedPixelBuffer]:
        """
        渲染线程调用。
        取出 CVPixelBuffer，后续由渲染线程做 GL 绑定。

        Args:
            timeout: 超时时间（秒）

        Returns:
            VtDecodedPixelBuffer 或 None
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def release_pixel_buffer(self, decoded: VtDecodedPixelBuffer) -> None:
        """
        渲染线程调用。
        GPU 渲染完成后释放 CVPixelBuffer。

        Args:
            decoded: VtDecodedPixelBuffer
        """
        if decoded.pixel_buffer is not None:
            try:
                if isinstance(decoded.pixel_buffer, int):
                    # Raw pointer from ctypes callback
                    _CV_LIB.CVPixelBufferRelease(_ctypes.c_void_p(decoded.pixel_buffer))
                else:
                    # PyObjC object
                    if _PYOBJC_AVAILABLE:
                        CVPixelBufferRelease(decoded.pixel_buffer)
            except Exception as e:
                logger.warning(f"VtDecodeOutputQueue: release error: {e}")

    def clear(self) -> None:
        """清空队列，释放所有 CVPixelBuffer"""
        while not self._queue.empty():
            try:
                decoded = self._queue.get_nowait()
                self.release_pixel_buffer(decoded)
            except queue.Empty:
                break

    @property
    def dropped_count(self) -> int:
        # AUD-35309：读也持锁，保证诊断口径一致
        with self._lock:
            return self._dropped_count

    @property
    def total_count(self) -> int:
        with self._lock:
            return self._total_count

    @property
    def size(self) -> int:
        return self._queue.qsize()


if _PYOBJC_AVAILABLE:
    class VtDecompressionOutputCallback(NSObject):
        """
        VTDecompressionSession 解码输出回调对象。

        使用 PyObjC 的 Objective-C 类来接收 VT 解码回调。
        回调在 VT 内部线程执行，只做 retain 和放入队列，
        不碰 OpenGL。

        约束：
        - 回调线程不能调用 OpenGL API
        - 只 retain CVPixelBuffer，放入线程安全队列
        - 生命周期管理由 VtDecodeOutputQueue 负责
        """
        _output_queue: VtDecodeOutputQueue = None
        _width: int = 0
        _height: int = 0
        _pixel_format: str = "nv12"
        _bit_depth: int = 8
        _frame_index: int = 0

        @classmethod
        def set_output_queue(cls, queue: VtDecodeOutputQueue) -> None:
            cls._output_queue = queue

        @classmethod
        def set_video_params(cls, width: int, height: int, pixel_format: str, bit_depth: int) -> None:
            cls._width = width
            cls._height = height
            cls._pixel_format = pixel_format
            cls._bit_depth = bit_depth

        @classmethod
        def increment_frame_index(cls) -> int:
            cls._frame_index += 1
            return cls._frame_index

        @_objc_module.python_method
        def decompressionOutputCallback(
            self,
            status: int,
            flags: int,
            image_buffer: object,
            pts: int,
            duration: int,
        ) -> None:
            if status != 0:
                logger.warning(f"VtDecompressionOutputCallback: decode status={status}")
                return

            if image_buffer is None:
                logger.warning("VtDecompressionOutputCallback: image_buffer is None")
                return

            if self._output_queue is None:
                logger.warning("VtDecompressionOutputCallback: output queue not set")
                return

            frame_index = self.increment_frame_index()

            self._output_queue.put_pixel_buffer(
                pixel_buffer=image_buffer,
                pts=pts,
                frame_index=frame_index,
                width=self._width,
                height=self._height,
                pixel_format=self._pixel_format,
                bit_depth=self._bit_depth,
            )
else:
    class VtDecompressionOutputCallback:
        _output_queue = None
        _width = 0
        _height = 0
        _pixel_format = "nv12"
        _bit_depth = 8
        _frame_index = 0


class _CtypesCallbackContext:
    """Per-VT-session callback state passed through VT's output refCon."""

    def __init__(
        self,
        output_queue: VtDecodeOutputQueue,
        width: int,
        height: int,
        pixel_format: str,
        bit_depth: int,
    ) -> None:
        self.output_queue = output_queue
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.bit_depth = bit_depth
        self.frame_index = 0
        self.lock = threading.Lock()


class VtSession:
    """
    VTDecompressionSession 封装。

    创建硬件解码 session，将压缩 packet 解码为 IOSurface-backed CVPixelBuffer。

    线程安全设计：
    - VT 回调线程：只 retain CVPixelBuffer，放入队列
    - 渲染线程：取 CVPixelBuffer，做 GL 绑定

    Usage:
        session = VtSession()
        session.create(width=3840, height=2160, codec='h264', extradata=extradata)
        for packet in demuxer.packets():
            session.submit_packet(packet.data, packet.pts, packet.is_keyframe)
            decoded = session.get_decoded_frame(timeout=0.1)
            if decoded:
                # decoded.pixel_buffer 与 IOSurface 关联
                pass
        session.destroy()
    """

    _SUPPORTED_CODECS = {'h264', 'hevc', 'h265', 'avc', 'prores'}

    # ProRes 家族的容器 fourcc → CMVideoCodecType。
    # ProRes 码流没有 extradata/SPS-PPS，CMVideoFormatDescriptionCreate
    # 直接按 fourcc + 尺寸创建格式描述。
    _PRORES_CODEC_TAGS = {
        'apco': 0x6170636F,  # ProRes 422 Proxy
        'apcs': 0x61706373,  # ProRes 422 LT
        'apcn': 0x6170636E,  # ProRes 422 Standard
        'apch': 0x61706368,  # ProRes 422 HQ
        'ap4h': 0x61703468,  # ProRes 4444
        'ap4x': 0x61703478,  # ProRes 4444 XQ
    }

    # H.264 NAL unit types
    _NALU_TYPE_SPS = 7
    _NALU_TYPE_PPS = 8
    _NALU_TYPE_IDR = 5
    _NALU_TYPE_SLICE = 1

    # HEVC NAL unit types
    _HEVC_NALU_TYPE_VPS = 32
    _HEVC_NALU_TYPE_SPS = 33
    _HEVC_NALU_TYPE_PPS = 34
    _HEVC_NALU_TYPE_IDR_W = 19
    _HEVC_NALU_TYPE_IDR_NLP = 20

    def __init__(self) -> None:
        self._session: object | None = None  # PyObjC session (kept for compatibility)
        self._ctypes_session: _ctypes.c_void_p | None = None  # ctypes session pointer
        self._ctypes_callback: _VT_DECODE_CALLBACK | None = None  # MUST hold ref to prevent GC
        self._ctypes_callback_context: _CtypesCallbackContext | None = None
        self._ctypes_callback_context_ref: _ctypes.py_object | None = None
        self._format_description: object | None = None
        self._format_description_ptr: _ctypes.c_void_p | None = None  # raw pointer for ctypes
        self._width: int = 0
        self._height: int = 0
        self._codec: str = ""
        self._codec_tag: str = ""
        self._pixel_format: str = "nv12"
        self._bit_depth: int = 8
        self._frame_index: int = 0
        self._destroyed: bool = False
        self._output_queue: VtDecodeOutputQueue | None = None
        self._callback: VtDecompressionOutputCallback | None = None
        self._extradata: bytes | None = None
        self._time_base: float = 1.0 / 30.0  # 默认 30fps
        # Keep PyObjC/ctypes buffers alive during async decode.
        # VT decode is asynchronous — the sample buffer may still be in use when
        # we return from _decode_packet_impl. We maintain paired lists of
        # CFRetain'd sample buffer pointers and their backing ctypes memory
        # blocks (CMBlockBuffer references the memory WITHOUT copying).
        # Both are released together in flush/destroy.
        self._pending_decode_refs: list = []
        self._pending_decode_bufs: list = []

    @property
    def is_created(self) -> bool:
        """Session 是否已创建"""
        if self._destroyed:
            return False
        # ctypes session path
        if self._ctypes_session is not None and self._ctypes_session.value is not None and self._ctypes_session.value != 0:
            return True
        # PyObjC fallback
        return self._session is not None

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def codec(self) -> str:
        return self._codec

    def create(
        self,
        width: int,
        height: int,
        codec: str = 'h264',
        pixel_format: str = 'nv12',
        extradata: bytes | None = None,
        require_hw_accel: bool = False,
        codec_tag: str = '',
    ) -> bool:
        """
        创建 VTDecompressionSession。

        Args:
            width: 视频宽度
            height: 视频高度
            codec: 编码器名称（h264, hevc, prores）
            pixel_format: 期望输出像素格式（nv12, p010）
            extradata: 编码器额外数据
            require_hw_accel: True 时通过 decoderSpecification 强制要求硬件解码器，
                无对应硬解时创建失败（用于能力探测）。默认 False = VideoToolbox
                自行选择硬解或软解（历史行为）。
            codec_tag: 容器 fourcc（ProRes 必需，如 'apch'/'ap4h'）

        Returns:
            True 创建成功，False 创建失败
        """
        if not _PYOBJC_AVAILABLE:
            logger.warning("VtSession: PyObjC not available, cannot create session")
            return False

        if not _MACOS_AVAILABLE:
            logger.warning("VtSession: not on macOS, cannot create session")
            return False

        codec_lower = codec.lower().replace('.', '')
        if codec_lower == 'prores':
            if codec_tag not in self._PRORES_CODEC_TAGS:
                logger.warning(
                    f"VtSession: ProRes codec tag '{codec_tag}' not supported"
                )
                return False
        elif codec_lower not in self._SUPPORTED_CODECS:
            logger.warning(f"VtSession: unsupported codec '{codec}'")
            return False

        # AUD-35302：destroy 后复用同一实例必须复位 _destroyed——此前
        # create() 从不清它，重配置返回 True 但 is_created 恒为 False，
        # 会话永久不可用。重新配置时旧会话先释放（旧资源先释放）。
        if not self._destroyed and (
            self._ctypes_session is not None or self._session is not None
        ):
            self.destroy()
        self._destroyed = False

        self._width = width
        self._height = height
        self._codec = codec_lower
        self._codec_tag = codec_tag
        self._frame_index = 0

        try:
            result = self._create_session_impl(
                width=width,
                height=height,
                codec=codec_lower,
                pixel_format=pixel_format,
                extradata=extradata,
                require_hw_accel=require_hw_accel,
            )
            if result is None:
                logger.error("VtSession: failed to create VTDecompressionSession")
                return False

            logger.info(
                f"VtSession: created session for {codec_lower} "
                f"{width}x{height}, pixel_format={pixel_format}"
            )
            return True

        except Exception as e:
            logger.error(f"VtSession: failed to create session: {e}")
            self._session = None
            self._ctypes_session = None
            return False

    def probe_hardware_support(
        self,
        width: int,
        height: int,
        codec: str = 'h264',
        extradata: bytes | None = None,
        codec_tag: str = '',
    ) -> bool | None:
        """
        判断该机器是否存在覆盖此格式的硬件解码器。

        创建一个 hardware-required 的 VTDecompressionSession
        （decoderSpecification 带 RequireHardwareAcceleratedVideoDecoder）：
        无硬解覆盖该格式/位深时创建直接失败 —— 这正是普通会话静默
        落软解的条件。探测会话立即销毁，不解码任何帧。

        Returns:
            True  — 有硬件解码器
            False — 无硬件解码器（普通 VT 会话会静默用软解）
            None  — 无法判定（非 macOS / PyObjC 不可用 / 无法强制硬解要求）
        """
        if not _PYOBJC_AVAILABLE:
            return None
        if not _MACOS_AVAILABLE:
            return None
        if globals().get('_VT_HW_ACCEL_KEY') is None:
            # 无法表达硬解要求时探测不成立（不能把"建不了要求"当"没硬解"）。
            return None

        codec_lower = str(codec).lower().replace('.', '')
        if codec_lower == 'prores':
            if codec_tag not in self._PRORES_CODEC_TAGS:
                return None
        elif codec_lower not in self._SUPPORTED_CODECS:
            return None

        created = self.create(
            width=width,
            height=height,
            codec=codec_lower,
            extradata=extradata,
            require_hw_accel=True,
            codec_tag=codec_tag,
        )
        try:
            return bool(created)
        finally:
            if created:
                self.destroy()

    def _create_session_impl(
        self,
        width: int,
        height: int,
        codec: str,
        pixel_format: str,
        extradata: bytes | None,
        require_hw_accel: bool,
    ) -> object | None:
        """
        实际创建 VTDecompressionSession 的实现。

        混合路径：PyObjC 创建 format description / pixel buffer attributes，
        ctypes 创建 VTDecompressionSession（PyObjC 无法正确注册回调）。

        步骤：
        1. 用 PyObjC 创建 CMVideoFormatDescription（正常工作）
        2. 用 PyObjC 配置 pixel buffer attributes（正常工作）
        3. 用 ctypes 创建 VTDecompressionSession（回调注册）
        4. 设置 real-time / 硬件加速属性
        """
        if not _PYOBJC_AVAILABLE:
            return None

        self._pixel_format = pixel_format
        self._bit_depth = 8 if pixel_format == 'nv12' else 10
        self._extradata = extradata

        try:
            from Cocoa import NSMutableDictionary
            from Quartz.CoreVideo import (
                kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange,
                kCVPixelBufferPixelFormatTypeKey,
                kCVPixelBufferWidthKey,
                kCVPixelBufferHeightKey,
            )
            from Quartz import (
                kCVPixelBufferIOSurfacePropertiesKey,
                kCVPixelBufferOpenGLCompatibilityKey,
            )
            import objc

            # Step 1: 用 PyObjC 创建 CMVideoFormatDescription
            format_desc = self._create_format_description(codec, extradata, width, height)
            if format_desc is None:
                logger.error("VtSession: failed to create CMVideoFormatDescription")
                return None
            self._format_description = format_desc

            # 获取 format description 的 raw pointer（用于 ctypes 调用）
            self._format_description_ptr = _ctypes.c_void_p(objc.pyobjc_id(format_desc))

            # Step 2: 用 PyObjC 配置 pixel buffer attributes
            # 关键：IOSurface backing + OpenGL compatibility
            pixel_buffer_attrs = NSMutableDictionary.dictionary()

            # 选择像素格式
            if pixel_format == 'p010':
                cv_pixel_format = kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
            else:
                cv_pixel_format = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange

            # Use Quartz CFString constants, NOT Python string literals.
            # PyObjC constants have short values like 'IOSurfaceProperties',
            # not the long names like 'kCVPixelBufferIOSurfacePropertiesKey'.
            pixel_buffer_attrs[kCVPixelBufferPixelFormatTypeKey] = cv_pixel_format
            pixel_buffer_attrs[kCVPixelBufferWidthKey] = width
            pixel_buffer_attrs[kCVPixelBufferHeightKey] = height
            pixel_buffer_attrs[kCVPixelBufferIOSurfacePropertiesKey] = NSMutableDictionary.dictionary()
            pixel_buffer_attrs[kCVPixelBufferOpenGLCompatibilityKey] = True

            # Step 3: 创建输出队列
            self._output_queue = VtDecodeOutputQueue(max_size=8)

            # Step 4: 用 ctypes 创建 VTDecompressionSession
            # 关键：PyObjC VTDecompressionSessionCreate 无法正确注册 output callback，
            # 必须用 ctypes CFUNCTYPE 创建 C 回调函数。

            # 创建 ctypes 回调（MUST 存储为实例变量防止 GC）
            self._ctypes_callback = _VT_DECODE_CALLBACK(self._on_ctypes_decoded)

            # 构造 VTCallbackRecord
            cb_record = _VTCallbackRecord()
            cb_record.callback = _ctypes.cast(self._ctypes_callback, _ctypes.c_void_p)
            # Keep the py_object wrapper alive for the lifetime of the native
            # session. VT returns this pointer to every decode callback, so
            # multiple timeline tracks never overwrite each other's queue.
            self._ctypes_callback_context = _CtypesCallbackContext(
                self._output_queue,
                width,
                height,
                pixel_format,
                self._bit_depth,
            )
            self._ctypes_callback_context_ref = _ctypes.py_object(
                self._ctypes_callback_context
            )
            cb_record.refCon = _ctypes.cast(
                _ctypes.pointer(self._ctypes_callback_context_ref),
                _ctypes.c_void_p,
            )

            # 获取 pixel buffer attrs dict 的 raw pointer
            attrs_ptr = _ctypes.c_void_p(objc.pyobjc_id(pixel_buffer_attrs))

            # 严格硬解要求：通过 decoderSpecification 传入 require-HW 键。
            # 无硬件解码器覆盖该格式（如 Intel 核显上的 10-bit HEVC）时，
            # VTDecompressionSessionCreate 直接失败 —— 这是能力探测的判定依据。
            decoder_spec_ptr = _ctypes.c_void_p(None)
            if require_hw_accel:
                hw_key = globals().get('_VT_HW_ACCEL_KEY')
                if hw_key is None:
                    logger.error(
                        "VtSession: kVTVideoDecoder_RequireHardwareAcceleratedVideoDecoder "
                        "unavailable, cannot enforce hardware decode"
                    )
                    return None
                decoder_spec = NSMutableDictionary.dictionary()
                decoder_spec[hw_key] = True
                decoder_spec_ptr = _ctypes.c_void_p(objc.pyobjc_id(decoder_spec))

            # 创建 ctypes session
            self._ctypes_session = _ctypes.c_void_p()
            err = _VT_LIB.VTDecompressionSessionCreate(
                None,                                    # allocator (kCFAllocatorDefault)
                self._format_description_ptr,            # formatDescription (raw pointer)
                decoder_spec_ptr,                        # decoderSpecification (None = auto)
                attrs_ptr,                               # destinationImageBufferAttributes
                _ctypes.byref(cb_record),                # outputCallback record
                _ctypes.byref(self._ctypes_session),     # decompressionSessionOut
            )

            if err != 0:
                logger.error(f"VtSession: VTDecompressionSessionCreate (ctypes) failed with err={err}")
                self._ctypes_session = None
                return None

            # 也设置 PyObjC _session 为 None（我们用 ctypes session）
            self._session = None

            # Step 5: 设置 real-time 属性（可选）
            # 注意：VTDecompressionSessionSetProperty 也需要 ctypes，
            # 但该属性只是建议性的，不设也不影响解码功能。
            # 暂时跳过，因为 ctypes session 不能直接传给 PyObjC 函数。

            logger.info(
                f"VtSession: ctypes session created successfully, "
                f"codec={codec}, format={pixel_format}, "
                f"size={width}x{height}"
            )

            return self._ctypes_session  # Return non-None to signal success

        except Exception as e:
            logger.error(f"VtSession: _create_session_impl failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    def _on_ctypes_decoded(
        refCon: int,
        frameRefCon: int,
        status: int,
        flags: int,
        imageBuffer: int,
        pts: _CMTime,
        duration: _CMTime,
    ) -> None:
        """
        ctypes VT decode callback — called from VT internal thread.

        This is the C function pointer registered via VTDecompressionSessionCreate.
        It is called by VideoToolbox when a frame finishes decoding.

        The imageBuffer parameter is a raw CVPixelBufferRef pointer (c_void_p value).
        PyObjC CF functions (CVPixelBufferRetain, CVPixelBufferGetIOSurface, etc.)
        accept raw int pointers, so we pass imageBuffer directly as the pixel_buffer.

        IMPORTANT: This callback runs on VT's internal thread.
        - Only retain CVPixelBuffer and enqueue
        - Do NOT touch OpenGL or any non-thread-safe state
        """
        if status != 0:
            logger.debug(f"VtSession._on_ctypes_decoded: status={status}")
            return

        if imageBuffer is None or imageBuffer == 0:
            return

        if refCon is None or refCon == 0:
            return
        try:
            context = _ctypes.cast(
                refCon,
                _ctypes.POINTER(_ctypes.py_object),
            ).contents.value
        except (TypeError, ValueError):
            return
        if not isinstance(context, _CtypesCallbackContext):
            return

        with context.lock:
            context.frame_index += 1
            frame_index = context.frame_index

        # AUD-35303：frameRefCon 携带提交侧透传的 pts（见
        # _submit_packet_raw）。sample buffer 无 timing 信息时，这是该帧
        # 唯一的时间戳来源；为空再退回回调 CMTime。
        refcon_pts: int | None = None
        if frameRefCon is not None and int(frameRefCon) != 0:
            refcon_pts = int(frameRefCon)
        cmtime_pts = int(pts.value) if pts is not None and pts.value is not None else None

        context.output_queue.put_pixel_buffer(
            pixel_buffer=imageBuffer,  # raw pointer — PyObjC CF functions accept int pointers
            pts=refcon_pts if refcon_pts is not None else cmtime_pts,
            frame_index=frame_index,
            width=context.width,
            height=context.height,
            pixel_format=context.pixel_format,
            bit_depth=context.bit_depth,
        )

    def _create_format_description(
        self,
        codec: str,
        extradata: bytes | None,
        width: int,
        height: int,
    ) -> object | None:
        """
        从 extradata 创建 CMVideoFormatDescription。

        Args:
            codec: 编码器名称
            extradata: 编码器额外数据（SPS/PPS for H.264, VPS/SPS/PPS for HEVC）
            width: 视频宽度
            height: 视频高度

        Returns:
            CMVideoFormatDescription 或 None
        """
        if not _PYOBJC_AVAILABLE:
            return None

        if codec == 'prores':
            # ProRes 没有 extradata：按容器 fourcc + 尺寸直接创建格式描述。
            fourcc = self._PRORES_CODEC_TAGS.get(self._codec_tag)
            if fourcc is None:
                logger.warning(
                    f"VtSession: ProRes codec tag '{self._codec_tag}' has no fourcc mapping"
                )
                return None
            try:
                from CoreMedia import CMVideoFormatDescriptionCreate
                err, format_desc = CMVideoFormatDescriptionCreate(
                    None, fourcc, width, height, None, None,
                )
                if err != 0:
                    logger.error(
                        f"VtSession: CMVideoFormatDescriptionCreate failed for "
                        f"ProRes tag {self._codec_tag}, err={err}"
                    )
                    return None
                return format_desc
            except Exception as e:
                logger.error(f"VtSession: ProRes format description failed: {e}")
                return None

        if extradata is None or len(extradata) == 0:
            logger.warning("VtSession: no extradata, cannot create format description")
            return None

        try:
            if codec in ('h264', 'avc'):
                # H.264: 从 SPS/PPS 创建 format description
                # extradata 通常包含 avcC box 或 SPS/PPS NAL units
                return self._create_h264_format_description(extradata, width, height)

            elif codec in ('hevc', 'h265'):
                # HEVC: 从 VPS/SPS/PPS 创建 format description
                return self._create_hevc_format_description(extradata, width, height)

            else:
                logger.warning(f"VtSession: unsupported codec for format description: {codec}")
                return None

        except Exception as e:
            logger.error(f"VtSession: _create_format_description failed: {e}")
            return None

    def _create_h264_format_description(
        self,
        extradata: bytes,
        width: int,
        height: int,
    ) -> object | None:
        """
        从 H.264 extradata 创建 CMVideoFormatDescription。

        支持两种格式：
        1. avcC box 格式（带长度前缀）
        2. Annex B 格式（带 start code）

        Args:
            extradata: H.264 extradata
            width: 视频宽度
            height: 视频高度

        Returns:
            CMVideoFormatDescription 或 None
        """
        try:
            from CoreMedia import CMVideoFormatDescriptionCreateFromH264ParameterSets

            # 解析 extradata 格式
            # avcC 格式：第一个字节是 configuration version (通常为 1)
            if len(extradata) >= 7 and extradata[0] == 1:
                # avcC box 格式
                # 结构：
                # - byte 0: configuration version (1)
                # - byte 1: AVCProfileIndication
                # - byte 2: profile_compatibility
                # - byte 3: AVCLevelIndication
                # - byte 4: lengthSizeMinusOne (NALU length size - 1, 通常 3 或 4)
                # - byte 5: numOfSequenceParameterSets (SPS 数量)
                # - SPS data...
                # - PPS data...

                # 提取 SPS 和 PPS
                sps_list, pps_list, nalu_length_size = self._parse_avcc_extradata(extradata)

                if not sps_list or not pps_list:
                    logger.error("VtSession: failed to parse avcC extradata")
                    return None

                # PyObjC expects tuple of bytes, not NSArray
                # Combine SPS and PPS into single tuple
                all_params = tuple(sps_list + pps_list)
                sizes = tuple(len(p) for p in all_params)
                num_params = len(all_params)

                # 创建 format description
                # AUD-35304：NAL unit 长度前缀宽度必须用 avcC 声明的
                # 实际值（lengthSizeMinusOne+1）。此前硬编码 4，遇到
                # 3 字节前缀的流时格式描述与实际 packet 表示不一致，
                # 解码边界解析错位。
                err, format_desc = CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    kCFAllocatorDefault,
                    num_params,
                    all_params,
                    sizes,
                    nalu_length_size,
                    None,
                )

                if err != 0:
                    logger.error(f"VtSession: CMVideoFormatDescriptionCreateFromH264ParameterSets failed: {err}")
                    return None

                return format_desc

            else:
                # Annex B 格式（带 0x00000001 start code）
                sps_list, pps_list = self._parse_annex_b_extradata(extradata, codec='h264')

                if not sps_list or not pps_list:
                    logger.error("VtSession: failed to parse Annex B extradata for H.264")
                    return None

                # PyObjC expects tuple of bytes
                all_params = tuple(sps_list + pps_list)
                sizes = tuple(len(p) for p in all_params)
                num_params = len(all_params)

                # AUD-35304：Annex B 码流没有长度前缀字段可解析；提交侧
                # 保持 AVCC/4 字节前缀约定，此处声明值必须与之一致。
                err, format_desc = CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    kCFAllocatorDefault,
                    num_params,
                    all_params,
                    sizes,
                    4,
                    None,
                )

                if err != 0:
                    logger.error(f"VtSession: CMVideoFormatDescriptionCreateFromH264ParameterSets failed: {err}")
                    return None

                return format_desc

        except Exception as e:
            logger.error(f"VtSession: _create_h264_format_description failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _create_hevc_format_description(
        self,
        extradata: bytes,
        width: int,
        height: int,
    ) -> object | None:
        """
        从 HEVC extradata 创建 CMVideoFormatDescription。

        Args:
            extradata: HEVC extradata
            width: 视频宽度
            height: 视频高度

        Returns:
            CMVideoFormatDescription 或 None
        """
        try:
            from CoreMedia import CMVideoFormatDescriptionCreateFromHEVCParameterSets

            # 解析 extradata
            # hvcC 格式或 Annex B 格式
            if len(extradata) >= 23 and extradata[0] == 1:
                # hvcC box 格式
                vps_list, sps_list, pps_list = self._parse_hvcc_extradata(extradata)
            else:
                # Annex B 格式
                vps_list, sps_list, pps_list = self._parse_annex_b_extradata(extradata, codec='hevc')

            if not sps_list or not pps_list:
                logger.error("VtSession: failed to parse HEVC extradata")
                return None

            # HEVC 需要 VPS, SPS, PPS - PyObjC expects tuple of bytes
            all_params = tuple((vps_list or []) + sps_list + pps_list)
            sizes = tuple(len(p) for p in all_params)
            num_params = len(all_params)

            err, format_desc = CMVideoFormatDescriptionCreateFromHEVCParameterSets(
                kCFAllocatorDefault,
                num_params,
                all_params,
                sizes,
                4,  # NAL unit length size
                None,  # extensions
                None,  # formatDescriptionOut
            )

            if err != 0:
                logger.error(f"VtSession: CMVideoFormatDescriptionCreateFromHEVCParameterSets failed: {err}")
                return None

            return format_desc

        except Exception as e:
            logger.error(f"VtSession: _create_hevc_format_description failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _parse_avcc_extradata(self, extradata: bytes) -> tuple[list, list, int]:
        """
        解析 avcC box 格式的 H.264 extradata。

        Args:
            extradata: avcC box 数据

        Returns:
            (sps_list, pps_list, nalu_length_size) 元组。
            nalu_length_size 为 avcC 声明的 NALU 长度前缀字节数
            （lengthSizeMinusOne + 1，AUD-35304：必须回传给格式描述
            创建调用，不得丢弃）。
        """
        sps_list = []
        pps_list = []
        nalu_length_size = 4  # 缺省按 AVCDecoderConfigurationRecord 惯例

        if len(extradata) < 7:
            return sps_list, pps_list, nalu_length_size

        idx = 0
        idx += 1

        # AVCProfileIndication, profile_compatibility, AVCLevelIndication
        idx += 3

        # lengthSizeMinusOne (NALU length size - 1)
        nalu_length_size = (extradata[idx] & 0x03) + 1
        idx += 1

        # numOfSequenceParameterSets
        num_sps = extradata[idx] & 0x1f
        idx += 1

        # 解析 SPS
        for _ in range(num_sps):
            if idx + 2 > len(extradata):
                break
            sps_length = (extradata[idx] << 8) | extradata[idx + 1]
            idx += 2
            if idx + sps_length > len(extradata):
                break
            sps = extradata[idx:idx + sps_length]
            sps_list.append(sps)
            idx += sps_length

        # numOfPictureParameterSets
        if idx >= len(extradata):
            return sps_list, pps_list, nalu_length_size
        num_pps = extradata[idx]
        idx += 1

        # 解析 PPS
        for _ in range(num_pps):
            if idx + 2 > len(extradata):
                break
            pps_length = (extradata[idx] << 8) | extradata[idx + 1]
            idx += 2
            if idx + pps_length > len(extradata):
                break
            pps = extradata[idx:idx + pps_length]
            pps_list.append(pps)
            idx += pps_length

        return sps_list, pps_list, nalu_length_size

    def _parse_hvcc_extradata(self, extradata: bytes) -> tuple[list, list, list]:
        """
        解析 hvcC box 格式的 HEVC extradata。

        Args:
            extradata: hvcC box 数据

        Returns:
            (vps_list, sps_list, pps_list) 元组
        """
        vps_list = []
        sps_list = []
        pps_list = []

        if len(extradata) < 23:
            return vps_list, sps_list, pps_list

        idx = 0
        idx += 1

        # general_profile_space, general_tier_flag, general_profile_idc
        idx += 1

        # general_profile_compatibility_flags
        idx += 4

        # general_constraint_indicator_flags
        idx += 6

        # general_level_idc
        idx += 1

        # min_spatial_segmentation_idc with reserved
        idx += 2

        # parallelismType with reserved
        idx += 1

        # chromaFormat with reserved
        idx += 1

        # bitDepthLumaMinus8 with reserved
        idx += 1

        # bitDepthChromaMinus8 with reserved
        idx += 1

        # avgFrameRate
        idx += 2

        # constantFrameRate, numTemporalLayers, temporalIdNested, lengthSizeMinusOne
        idx += 1

        # numOfArrays
        num_arrays = extradata[idx]
        idx += 1

        # 解析各个 NAL unit array
        for _ in range(num_arrays):
            if idx >= len(extradata):
                break

            # array_completeness, NAL_unit_type
            nalu_type = extradata[idx] & 0x3f
            idx += 1

            # numNalus
            num_nalus = (extradata[idx] << 8) | extradata[idx + 1]
            idx += 2

            for _ in range(num_nalus):
                if idx + 2 > len(extradata):
                    break
                nalu_length = (extradata[idx] << 8) | extradata[idx + 1]
                idx += 2
                if idx + nalu_length > len(extradata):
                    break
                nalu_data = extradata[idx:idx + nalu_length]
                idx += nalu_length

                # 根据 NAL unit type 分类
                if nalu_type == 32:  # VPS
                    vps_list.append(nalu_data)
                elif nalu_type == 33:  # SPS
                    sps_list.append(nalu_data)
                elif nalu_type == 34:  # PPS
                    pps_list.append(nalu_data)

        return vps_list, sps_list, pps_list

    def _parse_annex_b_extradata(
        self,
        extradata: bytes,
        codec: str = 'h264',
    ) -> tuple[list, list] | tuple[list, list, list]:
        """
        解析 Annex B 格式的 extradata（带 start code）。

        Args:
            extradata: Annex B 格式数据
            codec: 编码器类型

        Returns:
            H.264: (sps_list, pps_list)
            HEVC: (vps_list, sps_list, pps_list)
        """
        START_CODE = b'\x00\x00\x00\x01'
        START_CODE_3 = b'\x00\x00\x01'

        sps_list = []
        pps_list = []
        vps_list = []

        # 找到所有 NAL unit
        nalu_starts = []
        i = 0
        while i < len(extradata):
            if extradata[i:i+4] == START_CODE:
                nalu_starts.append((i + 4, 4))
                i += 4
            elif extradata[i:i+3] == START_CODE_3:
                nalu_starts.append((i + 3, 3))
                i += 3
            else:
                i += 1

        # 解析每个 NAL unit
        for idx, (start, start_code_len) in enumerate(nalu_starts):
            # 找到下一个 NAL unit 的开始或数据结束
            if idx + 1 < len(nalu_starts):
                end = nalu_starts[idx + 1][0] - start_code_len
            else:
                end = len(extradata)

            if start >= end:
                continue

            nalu_data = extradata[start:end]
            if len(nalu_data) < 1:
                continue

            # NAL unit type 在第一个字节的低 5 位（H.264）或低 6 位（HEVC）
            nalu_header = nalu_data[0]

            if codec == 'h264':
                nalu_type = nalu_header & 0x1f

                if nalu_type == 7:  # SPS
                    sps_list.append(nalu_data)
                elif nalu_type == 8:  # PPS
                    pps_list.append(nalu_data)

            elif codec == 'hevc':
                nalu_type = (nalu_header >> 1) & 0x3f

                if nalu_type == 32:  # VPS
                    vps_list.append(nalu_data)
                elif nalu_type == 33:  # SPS
                    sps_list.append(nalu_data)
                elif nalu_type == 34:  # PPS
                    pps_list.append(nalu_data)

        if codec == 'h264':
            return sps_list, pps_list
        else:
            return vps_list, sps_list, pps_list

    def decode_packet(self, packet_data: bytes, pts: int | None = None, is_keyframe: bool = False) -> VtDecodedPixelBuffer | None:
        """
        解码单个压缩 packet。

        Args:
            packet_data: 压缩码流 bytes
            pts: 展示时间戳
            is_keyframe: 是否为关键帧

        Returns:
            VtDecodedPixelBuffer 或 None（解码失败）
        """
        if not self.is_created:
            logger.warning("VtSession: session not created or destroyed")
            return None

        if not _PYOBJC_AVAILABLE:
            return None

        try:
            result = self._decode_packet_impl(packet_data, pts, is_keyframe)
            if result is not None:
                self._frame_index += 1
            # 顺序解码路径不会经过 flush()，按阈值周期性释放 CFRetain 引用
            self._drain_pending_decode_refs()
            return result
        except Exception as e:
            logger.error(f"VtSession: decode_packet failed: {e}")
            return None

    def _release_pending_decode_refs(self) -> None:
        """CFRelease 全部已保留的 sample buffer 引用并清空列表。

        只能在确认 VT 不再持有对应 sample buffer 后调用（即
        WaitForAsynchronousFrames 返回之后）。释放的是 raw c_void_p
        指针，不涉及任何 PyObjC 包装对象。
        """
        if not self._pending_decode_refs:
            return
        if _CF_LIB is None:
            self._pending_decode_refs.clear()
            return
        released = 0
        for ptr in self._pending_decode_refs:
            try:
                _CF_LIB.CFRelease(ptr)
                released += 1
            except Exception:
                pass
        self._pending_decode_refs.clear()
        # 内存块与其 sample buffer 配对保活，此时一并放行
        self._pending_decode_bufs.clear()
        if released:
            logger.debug(f"VtSession: released {released} pending decode refs")

    def _drain_pending_decode_refs(self, threshold: int = 128) -> None:
        """积累超过 threshold 个引用时等待在飞解码完成并批量释放。

        未达到阈值时零开销。达到阈值后的一次 WaitForAsynchronousFrames
        成本分摊到 threshold 个包（4K 硬解下 <0.2ms/包），换来引用数
        有界——否则每包一条 CFRetain 会随会话时长无上限累积
        （实测 1 分钟拖拽 ~9600 条）。
        """
        if len(self._pending_decode_refs) < threshold:
            return
        if self._destroyed:
            self._pending_decode_refs.clear()
            self._pending_decode_bufs.clear()
            return
        try:
            if self._ctypes_session is not None and self._ctypes_session.value:
                if _VT_LIB is not None:
                    _VT_LIB.VTDecompressionSessionWaitForAsynchronousFrames(self._ctypes_session)
            elif self._session is not None and _PYOBJC_AVAILABLE:
                VTDecompressionSessionWaitForAsynchronousFrames(self._session)
            else:
                return
            self._release_pending_decode_refs()
        except Exception as e:
            logger.warning(f"VtSession: drain pending decode refs error: {e}")

    def _submit_packet_raw(self, packet_data: bytes, pts: int | None = None) -> bool:
        """
        Submit a compressed packet to VT without draining the output queue.

        PyObjC creates CMBlockBuffer/CMSampleBuffer; ctypes submits via
        VTDecompressionSessionDecodeFrame.  The decoded frame arrives via
        the ctypes callback into self._output_queue.

        Args:
            packet_data: 压缩码流 bytes
            pts: 展示时间戳。非负时经 sourceFrameRefCon 透传，VT 在该帧
                输出回调中原样返回（AUD-35303），异步取帧路径因此保住
                demuxer 时间戳；None/负值时不传（沿用 CMTime 缺省）。

        Returns:
            True if the packet was submitted to VT successfully.
        """
        if not _PYOBJC_AVAILABLE:
            return False

        if self._ctypes_session is None or self._ctypes_session.value is None or self._ctypes_session.value == 0:
            return False

        try:
            from CoreFoundation import kCFAllocatorNull
            from CoreMedia import (
                CMBlockBufferCreateWithMemoryBlock,
                CMSampleBufferCreateReady,
            )

            # Create CMBlockBuffer from packet data using PyObjC.
            # 所有权约定（CMBlockBuffer 头文件）：blockAllocator=NULL 表示
            # finalize 时用默认分配器 free(memoryBlock) —— 而 data_buf 是
            # Python pymem 内存，用 libc free 释放会触发 malloc_zone_error
            # （小包走 arena 必崩）。必须传 kCFAllocatorNull 声明"非 CoreMedia
            # 所有、不得释放"。data_buf 由 _pending_decode_bufs 与 CFRetain
            # 配对保活到释放为止。
            data_buf = _ctypes.create_string_buffer(packet_data)

            err, block_buffer = CMBlockBufferCreateWithMemoryBlock(
                None,                    # allocator
                data_buf,                # memoryBlock
                len(packet_data),        # blockLength
                kCFAllocatorNull,        # blockAllocator: 不拥有，finalize 不释放
                None,                    # customBlockSource
                0,                       # offsetToData
                len(packet_data),        # dataLength
                0,                       # flags
                None,                    # blockBufferOut
            )
            if err != 0:
                logger.error(f"VtSession: CMBlockBufferCreateWithMemoryBlock failed: {err}")
                return False

            err, sample_buffer = CMSampleBufferCreateReady(
                None,                         # allocator
                block_buffer,                 # dataBuffer
                self._format_description,     # formatDescription
                1,                            # numSamples
                0,                            # numSampleTimingEntries
                None,                         # sampleTimingArray
                0,                            # numSampleSizeEntries
                None,                         # sampleSizeArray
                None,                         # sampleBufferOut
            )
            if err != 0:
                logger.error(f"VtSession: CMSampleBufferCreateReady failed: {err}")
                return False

            # Get raw pointer from PyObjC CF object.
            # PyObjC CF types' hash() returns the raw CF object pointer value.
            sample_buf_ptr = hash(sample_buffer)

            # Extra CFRetain to keep the CF object alive through the decode.
            # We store raw c_void_p pointers (not PyObjC objects) to avoid GC issues.
            # Paired with the backing memory block in _pending_decode_bufs;
            # both are released together after WaitForAsynchronousFrames.
            _CF_LIB.CFRetain(_ctypes.c_void_p(sample_buf_ptr))
            self._pending_decode_refs.append(_ctypes.c_void_p(sample_buf_ptr))
            self._pending_decode_bufs.append(data_buf)

            # Submit decode using ctypes
            # AUD-35303：sourceFrameRefCon 透传 pts。该 sample buffer 不带
            # timing 信息（numSampleTimingEntries=0），回调的 CMTime 为缺省
            # 值；refCon 是唯一能把提交侧时间戳带到输出帧的通道。
            frame_ref_con = None
            if pts is not None and 0 <= int(pts) <= 0x7FFFFFFFFFFFFFFF:
                frame_ref_con = _ctypes.c_void_p(int(pts))
            err = _VT_LIB.VTDecompressionSessionDecodeFrame(
                self._ctypes_session,                   # session
                _ctypes.c_void_p(sample_buf_ptr),       # sampleBuffer
                0,                                      # decodeFlags
                frame_ref_con,                          # sourceFrameRefCon
                None,                                   # infoFlagsOut
            )
            if err != 0:
                logger.error(f"VtSession: VTDecompressionSessionDecodeFrame (ctypes) failed: {err}")
                return False

            return True

        except Exception as e:
            logger.error(f"VtSession: _submit_packet_raw failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _decode_packet_impl(self, packet_data: bytes, pts: int | None, is_keyframe: bool) -> VtDecodedPixelBuffer | None:
        """
        Synchronous decode: submit packet + drain output queue.

        Combines _submit_packet_raw() + get_decoded_frame() for the
        sync decode_packet() API.
        """
        if not self._submit_packet_raw(packet_data, pts):
            return None

        decoded = self._output_queue.get_pixel_buffer(timeout=0.5)
        if decoded is None:
            logger.debug(f"VtSession: no decoded frame available for pts={pts}")
            return None

        # The ctypes callback receives CoreMedia timing, but the sample buffer
        # created in this path does not currently carry explicit timing data.
        # Preserve the packet PTS supplied by the demuxer so callers can map the
        # returned pixel buffer back to the real timeline frame after seeks.
        if pts is not None:
            decoded.pts = pts

        if not decoded.is_iosurface_backed:
            logger.warning(
                f"VtSession: decoded frame {decoded.frame_index} "
                f"does not have IOSurface backing"
            )

        logger.debug(
            f"VtSession: decoded frame {decoded.frame_index}, "
            f"iosurface={decoded.is_iosurface_backed}, "
            f"size={decoded.width}x{decoded.height}"
        )
        return decoded

    def submit_packet(
        self,
        packet_data: bytes,
        pts: int | None = None,
        is_keyframe: bool = False,
    ) -> bool:
        """
        提交压缩 packet 到解码器（异步）。

        仅提交 packet 到 VT，不等待解码结果也不消费输出队列。
        解码结果通过回调放入 output_queue，由 get_decoded_frame() 获取。

        Args:
            packet_data: 压缩码流 bytes
            pts: 展示时间戳
            is_keyframe: 是否为关键帧

        Returns:
            True 提交成功，False 提交失败
        """
        if not self.is_created:
            return False

        try:
            # AUD-35303：pts 必须随包透传，否则异步取帧路径（get_decoded_frame）
            # 拿到的帧全部没有时间戳，seek 后无法映射回时间线帧。
            return self._submit_packet_raw(packet_data, pts)
        except Exception as e:
            logger.error(f"VtSession: submit_packet failed: {e}")
            return False

    def get_decoded_frame(self, timeout: float = 0.5) -> VtDecodedPixelBuffer | None:
        """
        从输出队列获取解码帧。

        渲染线程调用，取出 CVPixelBuffer 用于 GL 绑定。

        Args:
            timeout: 超时时间（秒）

        Returns:
            VtDecodedPixelBuffer 或 None
        """
        if self._output_queue is None:
            return None

        return self._output_queue.get_pixel_buffer(timeout)

    def release_decoded_frame(self, decoded: VtDecodedPixelBuffer) -> None:
        """
        释放解码帧。

        GPU 渲染完成后调用，释放 CVPixelBuffer。

        Args:
            decoded: VtDecodedPixelBuffer
        """
        if self._output_queue is None:
            return

        self._output_queue.release_pixel_buffer(decoded)

    def flush(self) -> None:
        """
        刷新解码器，等待所有异步帧完成。
        """
        if not self._destroyed:
            try:
                # ctypes session path
                if self._ctypes_session is not None and self._ctypes_session.value:
                    if _VT_LIB is not None:
                        _VT_LIB.VTDecompressionSessionWaitForAsynchronousFrames(self._ctypes_session)
                # PyObjC fallback
                elif self._session is not None and _PYOBJC_AVAILABLE:
                    VTDecompressionSessionWaitForAsynchronousFrames(self._session)
                # 等待成功后 VT 不再持有任何已提交 sample buffer，
                # 安全释放 CFRetain 引用（seek 路径每次都会走到这里）
                self._release_pending_decode_refs()
            except Exception as e:
                logger.warning(f"VtSession: flush error: {e}")

        # 清空输出队列
        if self._output_queue is not None:
            self._output_queue.clear()

    def destroy(self) -> None:
        """销毁 VT session"""
        if not self._destroyed:
            try:
                # Step 1: Wait for all async frames to complete
                if self._ctypes_session is not None and self._ctypes_session.value:
                    if _VT_LIB is not None:
                        _VT_LIB.VTDecompressionSessionWaitForAsynchronousFrames(self._ctypes_session)

                # Step 2: Invalidate + CFRelease ctypes session
                if self._ctypes_session is not None and self._ctypes_session.value:
                    if _VT_LIB is not None:
                        _VT_LIB.VTDecompressionSessionInvalidate(self._ctypes_session)
                    # AUD-35301：会话遵循 CF Create Rule（+1 引用），只
                    # Invalidate 不 CFRelease 会让每个 session 泄漏一个
                    # 原生解码器实例。WaitForAsynchronousFrames 已完成，
                    # 此处释放安全。
                    if _CF_LIB is not None:
                        _CF_LIB.CFRelease(self._ctypes_session)
                    self._ctypes_session = _ctypes.c_void_p()

                # Step 3: Invalidate PyObjC session (fallback)
                if self._session is not None and _PYOBJC_AVAILABLE:
                    VTDecompressionSessionInvalidate(self._session)
                    self._session = None
            except Exception as e:
                logger.warning(f"VtSession: destroy error: {e}")

            # Step 4: Release pending decode refs.
            # Step 1 已 WaitForAsynchronousFrames（VT 不再持有任何 sample
            # buffer），且内存块与引用配对保活，此时 CFRetain 可安全平衡
            # 释放——旧实现只 clear 不释放是为了规避当时"内存块只保活最新
            # 一个"造成的悬垂 dealloc 崩溃，根因已修复。
            self._release_pending_decode_refs()

            # Step 5: Clear callback references only after native invalidation.
            # The refCon wrapper owns the per-session output queue and must stay
            # alive until VideoToolbox can no longer invoke the callback.
            self._ctypes_callback = None
            self._ctypes_callback_context_ref = None
            self._ctypes_callback_context = None

            self._destroyed = True
            logger.info("VtSession: destroyed")

    def __del__(self) -> None:
        """析构时确保 session 销毁"""
        self.destroy()

    def __enter__(self) -> 'VtSession':
        return self

    def __exit__(self, *args) -> None:
        self.destroy()


def is_vt_available() -> bool:
    """
    检查 VT 零拷贝能力是否可用。

    Returns:
        True 如果 macOS + PyObjC 可用
    """
    return _MACOS_AVAILABLE and _PYOBJC_AVAILABLE


def is_vt_decode_implemented() -> bool:
    """返回 VT packet -> CVPixelBuffer 解码桥接是否已实现。"""
    return _VT_DECODE_IMPLEMENTED
