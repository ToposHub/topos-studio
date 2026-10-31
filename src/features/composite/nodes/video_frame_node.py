"""
Video Frame Node - Extract frames from a video file.

Supports:
- Video file loading (MP4/MOV/AVI/MKV and sequence frames)
- MediaSource-based frame extraction (FFmpeg/GStreamer backends)
- Frame cache: pre-read +/- N frames to memory
- Dual positioning: frame_number and time_offset modes
- Loop playback option
- Texture upload aligned with ImageInputNode architecture
- Color metadata propagation (Rec.709, Rec.2020, HDR tags)

P6 Migration Notes:
- Replaced cv2.VideoCapture with MediaSourceFactory
- Added VideoMetadata propagation to TextureResult
- Added bit_depth field for HDR processing
- Fallback to VideoDecoderAdapter if MediaSource fails

P7.A Migration Notes:
- VideoFrameNode 不直接创建 MediaSourceFactory，而是通过 VideoFrameProviderService 获取帧
- Composite 预览用 proxy quality，最终导出用 master quality
- 多个节点引用同一个视频文件时共享 source/cache
- 支持 source in/out、time offset、speed、loop/hold

Async loading flow:
1. file_path changes -> VideoContext.open_clip() called
2. frame_number changes -> frame requested via service
3. Render thread calls upload_frame() -> GPU texture created
4. _do_evaluate() returns the uploaded texture with metadata

Frame caching strategy:
- VideoFrameProviderService manages shared source and frame cache
- Node maintains local GPU texture cache
- Pre-read via VideoContext.prefetch_for_node()
- Quality determined by render_mode (preview -> proxy, export -> master)

Cache key strategy:
- Key = "{path}:{frame_index}:{quality}"
- Prevents cross-contamination when switching frames or quality
"""

import os
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Any, List, Tuple, TYPE_CHECKING

import numpy as np
import moderngl

if TYPE_CHECKING:
    from src.shared.media import MediaSource, VideoMetadata, DecodedFrame

logger = logging.getLogger(__name__)

from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace


@dataclass(frozen=True)
class _PlanarFrameData:
    """GPU 直上路径的 planar 帧缓存条目。

    预览播放中后台 worker 解码出的 YUV 平面直接缓存（不做 CPU
    YUV→RGB float32 转换——那是播放卡顿的主要成本），渲染线程求值时
    经 YUVGPUUploader 上传平面纹理并由 shader 转换进 f4 FBO。内存约为
    float32 RGBA 的 1/10（YUV420 8-bit），下游契约不变（仍是一张 f4
    纹理）。矩阵/范围语义与 CPU 转换路径一致（BT.709/2020/601 +
    limited/full + 位深归一化，不 clamp 保留 HDR 高光）。

    P7-Alpha：YUVA 第四平面（Topos a8/a10/a12/a16 / FFmpeg yuva*）
    一路带到 GPU；alpha 位深独立于 luma（plane_infos[3] 权威），
    alpha_premultiplied 为源标志（shader 输出统一转 straight）。
    """
    y: np.ndarray
    u: np.ndarray
    v: np.ndarray
    bit_depth: int
    # YUVGPUUploader.MATRIX_BT709/BT2020/BT601 与 RANGE_LIMITED/RANGE_FULL
    # 的常量值（int，避免节点持有 uploader 引用）。
    yuv_matrix: int
    color_range: int
    width: int
    height: int
    a: object = None
    alpha_bit_depth: int = 8
    alpha_premultiplied: bool = False


@dataclass
class _VtFrameData:
    """VT 零拷贝帧（延迟导入，仅渲染线程消费）。

    由 MacOSVtZeroCopySource.read_frame_gpu_deferred / 交互式（拖拽
    coarse）读取产出的 GpuDecodedFrame：planes 为空、deferred_vt_frame
    保留 CVPixelBuffer。worker 解码后经单槽 _vt_pending 递交渲染线程，
    由 IOSurfaceGLBridge 绑定为外部纹理并经 shader 转换进 f4 FBO。
    CVPixelBuffer 是稀缺资源（VT 输出池有限），最多保留 pending +
    pinned 两帧，不入 _frame_cache。yuv_matrix 采用节点 VT shader 编号
    （0=BT.601，1=BT.709，2=BT.2020）。local_frame 为返回帧对应的
    时间线本地帧号（精确路径 == 请求帧；拖拽 coarse 可能是邻近关键帧，
    用于诚实的上屏/单调性记账）。
    """
    gpu_frame: Any
    yuv_matrix: int
    color_range: int
    local_frame: int = -1


# ── Constants ──

# Supported video extensions
from src.shared.media.format_registry import VIDEO_EXTENSIONS as _VIDEO_EXT_SET
_VIDEO_EXTENSIONS = tuple(_VIDEO_EXT_SET)

# P2-8: VideoFrameProviderService open-failure classification (per file
# path, process-wide so sibling nodes share the verdict):
# - "unsupported"  — MediaOpenError (codec/container unsupported or corrupt
#   media). The service will never open this file; skip it silently and
#   keep the direct-source fallback permanent for the session.
# - transient count — other exceptions (service not ready, contention).
#   Retried up to _SERVICE_TRANSIENT_MAX times, then demoted to
#   session-permanent to stop per-frame retry storms.
_SERVICE_TRANSIENT_MAX = 3
_SERVICE_OPEN_UNSUPPORTED: set = set()
_SERVICE_OPEN_TRANSIENT_COUNT: Dict[str, int] = {}
_SERVICE_CLASSIFY_LOCK = threading.Lock()

# Frame load states
class VideoLoadState:
    MISSING = "missing"       # No file_path set
    LOADING = "loading"       # Video opening in progress
    READY_CPU = "ready_cpu"   # Frame decoded, awaiting GPU upload
    UPLOADED_GPU = "uploaded_gpu"  # Texture on GPU, ready for rendering
    FAILED = "failed"         # Video open/decode error


class VideoFrameNode(Node):
    """Load a single frame from a video file.

    Uses MediaSource (FFmpeg/GStreamer) for video decoding, with automatic
    fallback to VideoDecoderAdapter if primary backend fails.

    Parameters
    ----------
    file_path : str
        Path to the video file (MP4/MOV/AVI/MKV etc).
    frame_number : int
        Current frame number to display (0-based).
    fps : float (read-only)
        Video frame rate, read from file.
    total_frames : int (read-only)
        Total number of frames in the video.
    time_offset : float
        Time offset in seconds (alternative to frame_number).
    cache_frames : int
        Number of frames to pre-cache around current frame.
    loop : bool
        Loop playback when frame_number exceeds total_frames.
    color_space : str
        Colour-space tag for the loaded pixels.
    premultiplied : bool
        True if the RGB channels are premultiplied by alpha.
    missing_policy : str
        What to do when file cannot be found: "error", "black", "checker".
    """

    node_type = "video_frame"
    node_title = "Video Frame"
    header_color = "#5b7fb5"  # Blue (similar to ImageInputNode)

    # Metadata
    SEARCH_ALIASES = ["video", "frame", "视频", "帧", "movie", "mp4", "mov"]
    DESCRIPTION = "从视频文件提取单帧"
    TAGS = ["input", "source", "video"]
    IS_PLACEHOLDER = False  # Now fully implemented

    PARAM_DEFS = [
        ParamDef("file_path", "str", ""),
        ParamDef("asset_id", "str", ""),  # P7.2: Asset-backed node support
        ParamDef("media_role", "option", "plate",
                 labels=["plate", "bg", "fg", "matte", "depth", "other"]),
        ParamDef("frame_number", "int", 0, min_val=0, max_val=10000000),
        ParamDef("time_offset", "float", 0.0, min_val=0.0, max_val=36000.0),
        ParamDef("source_in", "int", 0, min_val=0, max_val=10000000),
        ParamDef("source_out", "int", -1, min_val=-1, max_val=10000000),
        ParamDef("speed", "float", 1.0, min_val=0.1, max_val=4.0),
        ParamDef("hold", "bool", False),
        ParamDef("cache_frames", "int", 5, min_val=1, max_val=30),
        ParamDef("loop", "bool", False),
        ParamDef("color_space", "option", "auto",
                 labels=["auto", "srgb", "linear", "rec709",
                         "aces_cg", "aces2065_1"]),
        ParamDef("premultiplied", "bool", False),
        ParamDef("missing_policy", "option", "error",
                 labels=["error", "black", "checker"]),
        # Read-only metadata parameters (not editable by user)
        ParamDef("fps", "float", 0.0, min_val=0.0, max_val=240.0, hidden=True),
        ParamDef("total_frames", "int", 0, min_val=0, max_val=10000000, hidden=True),
        ParamDef("video_width", "int", 0, min_val=0, max_val=16384, hidden=True),
        ParamDef("video_height", "int", 0, min_val=0, max_val=16384, hidden=True),
        ParamDef("bit_depth", "int", 8, min_val=1, max_val=32, hidden=True),
        ParamDef("is_hdr", "bool", False, hidden=True),
        ParamDef("clip_id", "str", "", hidden=True),  # P7.A: clip identifier from service
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "file_path": "",
            "asset_id": "",  # P7.2: Asset-backed node support
            "media_role": "plate",  # P7.2: Media role (plate/bg/fg/matte/depth/other)
            "frame_number": 0,
            "time_offset": 0.0,
            "source_in": 0,
            "source_out": -1,
            "speed": 1.0,
            "hold": False,
            "cache_frames": 5,
            "loop": False,
            "color_space": "auto",
            "premultiplied": False,
            "missing_policy": "error",
            "fps": 0.0,
            "total_frames": 0,
            "video_width": 0,
            "video_height": 0,
            "bit_depth": 8,
            "is_hdr": False,
            "clip_id": "",  # P7.A: clip identifier from VideoFrameProviderService
        }

        # GPU texture for current frame
        self._gpu_texture: Optional[moderngl.Texture] = None
        self._width: int = 0
        self._height: int = 0
        self._revision: int = 0

        # GPU YUV 直上路径（preview planar 帧，仅渲染线程触碰）：平面纹理
        # 池由 YUVGPUUploader 管理；_gpu_texture 同时作为 FBO 颜色附件被
        # shader 转换写入（对下游仍是普通的 f4 纹理）。
        self._yuv_uploader: Optional[Any] = None
        self._yuv_uploader_ctx: Optional[moderngl.Context] = None
        self._gpu_fbo: Optional[moderngl.Framebuffer] = None
        self._yuv_upload_serial: int = 0
        # planar 直上的中转 FBO：uploader 的渲染 quad 方向与本节点约定相反
        # （其 FBO 底行=图像顶行），先渲到中转再翻转 blit 到最终纹理
        self._gpu_mid_fbo: Optional[moderngl.Framebuffer] = None
        # AUD-7603: ModernGL Framebuffer.release() 不释放颜色附件——mid 的
        # f4 纹理必须显式持有引用，否则换尺寸/清缓存路径整张泄漏（4K≈132MB）。
        self._gpu_mid_tex: Optional[moderngl.Texture] = None
        self._flip_prog: Optional[moderngl.Program] = None
        self._flip_vbo: Optional[moderngl.Buffer] = None
        # AUD-7603: VAO.release() 同样不释放 index buffer——flip IBO 需显式持有。
        self._flip_ibo: Optional[moderngl.Buffer] = None
        self._flip_vao: Optional[Any] = None
        # VT 零拷贝路径（HEVC 4:2:0，macOS）：worker 单槽递交延迟导入帧，
        # 渲染线程 IOSurface→GL 绑定 + shader 转换。失败 ≥3 次自动禁用，
        # 回退 planar/CPU 路径。
        self._vt_lock: threading.Lock = threading.Lock()
        self._vt_pending: Optional[Tuple[int, _VtFrameData]] = None
        self._vt_pinned: Optional[Tuple[Any, Any]] = None  # (bridge_release, gpu_frame)
        # worker 上一次着手解码的 VT 目标帧号（时间线本地）。VT 游标在该
        # 解码后即越过目标帧，同帧重复解码会触发关键帧回退 seek（百毫秒
        # 级）。必须是 worker 侧的"精确目标"记录——不能用 max() 累积的
        # 已交付帧号：往回拖拽时（目标 < 历史最大交付帧）会把新目标误判
        # 为重复请求，导致定格帧永远不解码。
        self._vt_last_decoded_target: Optional[int] = None
        self._vt_bridge: Optional[Any] = None
        self._vt_bridge_ctx: Optional[moderngl.Context] = None
        self._vt_gpu_disabled: bool = False
        self._vt_fail_count: int = 0
        # VT 可用源下允许上屏的 CPU 帧号集合与"VT 未服务"计数：VT 连续
        # 两次未服务某帧（游标越过的瞬时 exact miss 常见，重试即恢复）
        # 才标记放行。upload_frame 的 VT 单通道门控据此逐帧放行，避免
        # VT/CPU 两条颜色特性不同的解码链混显（详见 upload_frame）。
        self._vt_cpu_ok: set = set()
        self._vt_cpu_miss: Dict[int, int] = {}
        self._vt_quad_vbo: Optional[moderngl.Buffer] = None
        self._vt_programs: Dict[str, Any] = {}

        # MediaSource state (replaces cv2.VideoCapture)
        # P7.A: Use VideoFrameProviderService instead of direct MediaSource
        # _source is now optional - kept for fallback mode only
        self._load_state: str = VideoLoadState.MISSING
        self._source: Optional["MediaSource"] = None  # Fallback mode only
        self._video_metadata: Optional["VideoMetadata"] = None
        self._decode_policy: str = "auto"  # Quality policy for decoding

        # P7.A: clip_id for service mode
        self._clip_id: Optional[str] = None
        self._use_service: bool = True  # Default to using VideoFrameProviderService
        self._video_context: Optional[Any] = None  # VideoContext reference
        # P2-8: why the service path was abandoned for this source
        # ('unsupported_or_corrupt', 'transient_xN', ...). Diagnostics only.
        self._service_fallback_reason: str = ""

        # Frame cache (CPU-side decoded frames)
        # Format: {frame_number: np.ndarray}
        self._frame_cache: Dict[int, np.ndarray] = {}
        self._cache_size: int = 5  # Default cache_frames

        # GPU texture cache (optional, for multi-frame reuse)
        # Format: {frame_number: moderngl.Texture}
        self._texture_cache: Dict[int, moderngl.Texture] = {}

        # 后台预取（原为渲染线程同步预取）。邻域 ±cache_frames 的解码与
        # float32 转换由守护线程填充 _frame_cache；渲染线程每帧只处理当前
        # 帧，拖拽/跳转不再被整窗同步预取阻塞。_texture_cache 只允许渲染
        # 线程触碰（GL 对象），后台淘汰仅清 CPU 缓存。
        self._cache_lock: threading.Lock = threading.Lock()
        self._preload_thread: Optional[threading.Thread] = None
        self._preload_wakeup: threading.Event = threading.Event()
        self._preload_stop: bool = False
        self._preload_target: Optional[Tuple[int, str]] = None
        # 媒体代际：换源/清缓存时 +1，后台预取按代际拒绝过期帧写入缓存
        self._media_generation: int = 0
        # 渲染线程最近一次内联解码时间：连续拖拽/播放期间（解码节拍密集）
        # 抑制后台预取，避免两者在共享服务的 decode_lock 上互相阻塞
        self._last_render_decode_at: float = 0.0
        self._last_render_decode_ms: float = 0.0
        # 播放/拖拽检测：求值请求连续变化帧号 = 播放中。用于（a）抑制邻域
        # 预取，把解码带宽留给当前帧；（b）缓存 miss 时展示最新已解码帧
        # （present-latest）而非冻结在旧纹理上等精确帧。
        self._prev_frame_request_frame: Optional[int] = None
        self._last_frame_request_frame: Optional[int] = None
        self._last_frame_request_at: float = 0.0
        # 请求变化时间戳与步长（近期突发检测：150ms 内 ≥2 次变化且含跳变/
        # 换向 = 拖拽/快梭，走 VT 交互式 coarse 读取；播放的 +1 单调节奏
        # 和单帧步进（~200ms 间隔）不算）
        self._request_change_times: List[float] = []
        self._request_change_deltas: List[int] = []
        # 异步当前帧解码失败追踪：同一位置连续失败 ≥2 次后标记为失败位置，
        # upload_frame 对其回退同步解码兜底，避免坏帧位置无限空转调度。
        self._async_fail_counts: Dict[int, int] = {}
        self._failed_async_frames: set = set()

        # Current displayed frame
        self._current_frame: int = -1
        self._is_fallback: bool = False

        # Error handling
        self.error_message: str = ""

        # AUD-7605: the weakref.finalize "GC 后备释放" backstop was removed.
        # It was dead code — nothing was ever appended to _gc_resources, so
        # the finalizer iterated an empty list on every collection while the
        # comments promised texture/MediaSource release. Wiring it up for
        # real is NOT the fix either: GL objects must never be released from
        # the GC thread (no context is current there — see the deferred-
        # release discipline). Resource teardown is owned by the explicit
        # cleanup() path, dispatched on the render thread by the node graph
        # (composite_renderer._handle_cleanup_node / renderer teardown) and
        # by the context-manager protocol below.

    # ── Load State ──

    @property
    def load_state(self) -> str:
        return self._load_state

    @property
    def is_ready(self) -> bool:
        """True if GPU texture is available for rendering."""
        return self._gpu_texture is not None and self._load_state == VideoLoadState.UPLOADED_GPU

    @property
    def is_fallback(self) -> bool:
        """True if current texture is a placeholder (missing file)."""
        return self._is_fallback

    @property
    def video_fps(self) -> float:
        """Video frame rate (read-only)."""
        if self._video_metadata:
            return self._video_metadata.fps.to_fps()
        return 0.0

    @property
    def total_frames(self) -> int:
        """Total frames in video (read-only)."""
        if self._video_metadata and self._video_metadata.total_frames:
            return self._video_metadata.total_frames
        # Media-drop/open paths publish the length into the parameter before
        # any decode happens; honour it so range clamps work pre-open.
        return self._get_param_int("total_frames", 0)

    @property
    def video_metadata(self) -> Optional["VideoMetadata"]:
        """Get cached VideoMetadata."""
        return self._video_metadata

    # ── MediaSource Creation ──

    # P7.A: Primary method - use VideoFrameProviderService
    def _get_video_context(self) -> Any:
        """Get or create VideoContext instance.

        Returns:
            VideoContext instance.
        """
        if self._video_context is None:
            from src.features.composite.core.video_context import VideoContext
            self._video_context = VideoContext.get_instance()
        return self._video_context

    def _open_video_via_service(self, file_path: str) -> bool:
        """Open video via VideoFrameProviderService (P7.A primary method).

        VideoFrameNode 不直接创建 MediaSourceFactory，而是通过 service 拿帧。
        多个节点引用同一个视频文件时共享 source/cache。

        Args:
            file_path: Path to video file.

        Returns:
            True if clip opened successfully.
        """
        if not file_path or not os.path.isfile(file_path):
            self._load_state = VideoLoadState.FAILED
            self.error_message = f"File not found: {file_path}"
            return False

        # P2-8: a session-permanent verdict skips the service instantly —
        # no per-frame retry storm, no repeated warning (log noise).
        with _SERVICE_CLASSIFY_LOCK:
            known_unsupported = file_path in _SERVICE_OPEN_UNSUPPORTED
        if known_unsupported:
            logger.debug(
                "VideoFrameProviderService skipped for %s: previously "
                "classified unsupported/corrupt", file_path)
            return False

        try:
            video_ctx = self._get_video_context()

            # Open clip via VideoContext (which uses VideoFrameProviderService)
            # 注：历史上这里传 self.node_id（不存在的属性，必抛 AttributeError），
            # 导致服务路径永远失败、每个节点都回退到独立 MediaSource。基类
            # 的节点标识是 self.id。
            clip_id = video_ctx.open_clip(file_path, self.id)

            # Get clip info
            clip_info = video_ctx.get_clip_info(clip_id)
            if clip_info is None:
                self._load_state = VideoLoadState.FAILED
                self.error_message = f"Failed to get clip info: {clip_id}"
                return False

            # Update parameters from clip info
            self._clip_id = clip_id
            self.parameters["clip_id"] = clip_id
            self.parameters["fps"] = clip_info.fps
            self.parameters["total_frames"] = clip_info.total_frames
            self.parameters["video_width"] = clip_info.width
            self.parameters["video_height"] = clip_info.height
            self.parameters["bit_depth"] = clip_info.bit_depth
            self.parameters["is_hdr"] = clip_info.is_hdr

            # Create VideoMetadata for downstream use (from ClipInfo)
            if clip_info.total_frames > 0:
                from src.shared.media.metadata import Rational, VideoMetadata
                fps_rational = Rational.from_float(clip_info.fps, max_den=1001)
                # 注：VideoMetadata 无 is_hdr 字段（HDR 由 primaries/transfer
                # 表达）；历史上传 is_hdr 必抛 TypeError，使服务路径失败。
                self._video_metadata = VideoMetadata(
                    width=clip_info.width,
                    height=clip_info.height,
                    fps=fps_rational,
                    total_frames=clip_info.total_frames,
                    codec_name=clip_info.codec_name,
                    codec_profile=None,
                    pixel_format='rgba',
                    bit_depth=clip_info.bit_depth,
                    color_primaries=clip_info.color_primaries,
                    color_transfer=clip_info.color_transfer,
                    color_matrix=clip_info.color_matrix,
                )

            # Clear local cache
            self._clear_cache()

            self._load_state = VideoLoadState.LOADING
            self._current_frame = -1
            self._is_fallback = False
            self._use_service = True

            # A previous transient failure recovered — clear the verdict.
            with _SERVICE_CLASSIFY_LOCK:
                _SERVICE_OPEN_TRANSIENT_COUNT.pop(file_path, None)

            logger.info(
                f"VideoFrameNode: Clip opened via service: {file_path} -> {clip_id}, "
                f"{clip_info.width}x{clip_info.height}, {clip_info.fps:.2f}fps"
            )
            return True

        except Exception as e:
            # P2-8: classify the failure. MediaOpenError means the service
            # can never open this file (unsupported codec/container or
            # corrupt media) — mark it session-permanent. Anything else is
            # potentially transient (service starting up, contention) and
            # gets a bounded retry budget before being demoted.
            from src.shared.media.source import MediaOpenError
            with _SERVICE_CLASSIFY_LOCK:
                if isinstance(e, MediaOpenError):
                    first = file_path not in _SERVICE_OPEN_UNSUPPORTED
                    _SERVICE_OPEN_UNSUPPORTED.add(file_path)
                    classification = "unsupported_or_corrupt"
                else:
                    count = _SERVICE_OPEN_TRANSIENT_COUNT.get(file_path, 0) + 1
                    _SERVICE_OPEN_TRANSIENT_COUNT[file_path] = count
                    if count >= _SERVICE_TRANSIENT_MAX:
                        first = file_path not in _SERVICE_OPEN_UNSUPPORTED
                        _SERVICE_OPEN_UNSUPPORTED.add(file_path)
                        classification = (
                            f"transient_x{count}_demoted_permanent")
                    else:
                        first = count == 1
                        classification = f"transient_x{count}"
            log = logger.warning if first else logger.debug
            log(
                "VideoFrameProviderService failed (%s), falling back to "
                "direct source: %s", classification, e)
            self._use_service = False
            self._service_fallback_reason = classification
            return False

    def _close_video_via_service(self) -> None:
        """Close video via VideoFrameProviderService (P7.A)."""
        if self._clip_id is not None and self._video_context is not None:
            try:
                self._video_context.close_clip_for_node(self.id)
                logger.info(f"VideoFrameNode: Clip closed via service: {self._clip_id}")
            except Exception as e:
                logger.warning(f"Failed to close clip via service: {e}")

            self._clip_id = None
            self.parameters["clip_id"] = ""

    def _create_source(self, file_path: str) -> Optional["MediaSource"]:
        """Create MediaSource with fallback support (P7.A fallback method).

        P6.1: Replaces cv2.VideoCapture with MediaSourceFactory.
        P7.A: This is now a fallback method when VideoFrameProviderService fails.

        Args:
            file_path: Path to video file.

        Returns:
            MediaSource instance, or None on failure.
        """
        from src.shared.media import (
            MediaSourceFactory,
            MediaOpenError,
            VideoDecoderAdapter,
        )

        if not file_path or not os.path.isfile(file_path):
            self._load_state = VideoLoadState.FAILED
            self.error_message = f"File not found: {file_path}"
            return None

        # Check extension
        lower_path = file_path.lower()
        if not lower_path.endswith(_VIDEO_EXTENSIONS):
            # Try anyway - backend might support more formats
            pass

        try:
            # Primary: Use MediaSourceFactory
            source = MediaSourceFactory.create(file_path, backend='auto')
            source.open()

            # Probe metadata
            self._video_metadata = source.probe()

            # Update read-only parameters
            self.parameters["fps"] = self._video_metadata.fps.to_fps()
            self.parameters["total_frames"] = self._video_metadata.total_frames or 0
            self.parameters["video_width"] = self._video_metadata.width
            self.parameters["video_height"] = self._video_metadata.height
            self.parameters["bit_depth"] = self._video_metadata.bit_depth
            self.parameters["is_hdr"] = self._video_metadata.is_hdr

            # Clear frame cache (video changed)
            self._clear_cache()

            self._load_state = VideoLoadState.LOADING
            self._current_frame = -1
            self._is_fallback = False

            logger.info(f"VideoFrameNode: MediaSource created for {file_path}")
            return source

        except MediaOpenError as e:
            logger.warning(f"MediaSource failed, trying VideoDecoderAdapter fallback: {e}")

            # Fallback: Use VideoDecoderAdapter (wraps old VideoDecoder)
            try:
                adapter = VideoDecoderAdapter(file_path)
                # Adapter uses MediaSource internally
                adapter._source.open()
                self._video_metadata = adapter._source.probe()

                # Update parameters from adapter
                self.parameters["fps"] = adapter.fps
                self.parameters["total_frames"] = adapter.total_frames
                self.parameters["video_width"] = adapter.width
                self.parameters["video_height"] = adapter.height
                # Adapter may not have bit_depth info
                self.parameters["bit_depth"] = 8
                self.parameters["is_hdr"] = False

                self._clear_cache()
                self._load_state = VideoLoadState.LOADING
                self._current_frame = -1

                logger.info(f"VideoFrameNode: Using VideoDecoderAdapter fallback for {file_path}")
                return adapter._source

            except Exception as fallback_e:
                self._load_state = VideoLoadState.FAILED
                self.error_message = f"All backends failed: {fallback_e}"
                return None

        except Exception as e:
            self._load_state = VideoLoadState.FAILED
            self.error_message = str(e)
            return None

    def _open_video(self, file_path: str) -> bool:
        """Open video file using VideoFrameProviderService or fallback.

        P7.A: Primary method uses VideoFrameProviderService.
        Fallback to direct MediaSource if service fails.

        Args:
            file_path: Path to video file.

        Returns:
            True if video opened successfully.
        """
        # P7.A: Try service first
        if self._open_video_via_service(file_path):
            return True

        # Fallback: Direct MediaSource creation
        self._use_service = False
        self._source = self._create_source(file_path)
        return self._source is not None

    def _close_video(self) -> None:
        """Close video and release resources.

        P7.A: Close via service if using service mode.
        Otherwise close direct MediaSource.
        """
        # P7.A: Close via service first if using service mode
        if self._use_service and self._clip_id is not None:
            self._close_video_via_service()

        # Close direct source (fallback mode)
        if self._source is not None:
            try:
                self._source.close()
            except Exception as e:
                logger.warning(f"Failed to close MediaSource: {e}")
            self._source = None

        self._clear_cache()
        self._load_state = VideoLoadState.MISSING
        self._video_metadata = None
        self._clip_id = None
        self.parameters["clip_id"] = ""
        self._use_service = True  # Reset to default

    # ── Frame Extraction ──

    def _request_decoded_frame_via_service(
        self,
        frame_number: int,
        render_mode: str = 'preview',
    ) -> Optional["DecodedFrame"]:
        """Request raw DecodedFrame via VideoFrameProviderService (P7.A).

        返回原始解码帧：预览 + planar 帧由调用方走 GPU 直上路径（见
        _decode_frame），packed/export 帧再做 CPU float32 转换。支持
        source in/out、time offset、speed、loop/hold。

        Args:
            frame_number: Frame index (timeline frame).
            render_mode: 'preview' or 'export'.

        Returns:
            DecodedFrame，或 None on error.
        """
        if self._clip_id is None or self._video_context is None:
            return None

        try:
            # Get parameters for frame request
            source_in = self.parameters.get("source_in", 0)
            source_out = self.parameters.get("source_out", -1)
            time_offset = self.parameters.get("time_offset", 0.0)
            speed = self.parameters.get("speed", 1.0)
            loop = self.parameters.get("loop", False)
            hold = self.parameters.get("hold", False)

            # Request frame via VideoContext
            decoded_frame = self._video_context.request_frame_for_node(
                clip_id=self._clip_id,
                source_in=source_in,
                source_out=source_out,
                timeline_frame=frame_number,
                fps=self.parameters.get("fps", 24.0),
                time_offset=time_offset,
                speed=speed,
                loop=loop,
                hold=hold,
                render_mode=render_mode
            )
            return decoded_frame

        except Exception as e:
            logger.warning(f"Frame request via service error at {frame_number}: {e}")
            return None

    @staticmethod
    def _yuv_matrix_const(meta: Any) -> int:
        """色彩矩阵标签 → YUVGPUUploader.MATRIX_* 常量值。

        与 _convert_decoded_frame 的 CPU 矩阵判定保持一致。
        """
        m = str(getattr(meta, 'color_matrix', '') or '').lower()
        if 'bt2020' in m:
            return 1  # MATRIX_BT2020
        if 'smpte170m' in m or 'bt601' in m or 'bt470' in m:
            return 2  # MATRIX_BT601
        return 0  # MATRIX_BT709

    @staticmethod
    def _color_range_const(meta: Any) -> int:
        """色彩范围标签 → YUVGPUUploader.RANGE_* 常量值。"""
        r = str(getattr(meta, 'color_range', '') or '').lower()
        if r in ('full', 'pc', '1'):
            return 1  # RANGE_FULL
        return 0  # RANGE_LIMITED

    def _planar_frame_data(self, frame: "DecodedFrame") -> Optional[_PlanarFrameData]:
        """DecodedFrame(planar) → GPU 直上缓存条目。

        仅接受独立 Y/U/V 2D 平面（ProRes/DNxHR/H264 planar 输出）；NV12
        2 平面交错等其它布局返回 None 走 CPU 转换路径。YUVA 第四平面
        （全分辨率 2D）随行携带（P7-Alpha）。
        """
        if not frame.is_planar or not frame.planes or len(frame.planes) < 3:
            return None
        y, u, v = frame.planes[0], frame.planes[1], frame.planes[2]
        if y.ndim != 2 or u.ndim != 2 or v.ndim != 2:
            return None
        h, w = y.shape[:2]
        if w <= 0 or h <= 0:
            return None

        # YUVA 第四平面：alpha 位深独立于 luma（plane_infos[3] 权威）
        a = None
        alpha_bit_depth = int(frame.bit_depth or 8)
        alpha_premult = False
        if len(frame.planes) >= 4:
            candidate = frame.planes[3]
            if candidate.ndim == 2 and candidate.shape[:2] == (h, w):
                a = candidate
                infos = getattr(frame, 'plane_infos', None)
                if infos and len(infos) >= 4:
                    alpha_bit_depth = int(getattr(infos[3], 'bit_depth', alpha_bit_depth)
                                          or alpha_bit_depth)
                alpha_premult = bool(
                    (getattr(frame, 'extra', None) or {}).get(
                        'topos_alpha_premultiplied', False))

        meta = frame.metadata
        return _PlanarFrameData(
            y=y,
            u=u,
            v=v,
            bit_depth=int(frame.bit_depth or 8),
            yuv_matrix=self._yuv_matrix_const(meta),
            color_range=self._color_range_const(meta),
            width=w,
            height=h,
            a=a,
            alpha_bit_depth=alpha_bit_depth,
            alpha_premultiplied=alpha_premult,
        )

    @staticmethod
    def _vt_matrix_const(name: Any) -> int:
        """色彩矩阵名 → 节点 VT shader 编号（0=BT.601, 1=BT.709, 2=BT.2020）。

        注意与 YUVGPUUploader 编号（0=709）不同：VT 路径移植自剪辑页的
        内联 shader，编号沿用其约定。
        """
        n = str(name or 'bt709').lower().strip()
        if any(k in n for k in ('bt601', 'smpte170m', 'bt470', 'fcc')):
            return 0
        if 'bt2020' in n:
            return 2
        return 1

    @staticmethod
    def _vt_range_const(name: Any) -> int:
        """色彩范围名 → shader 编号（0=limited, 1=full）。"""
        return 0 if str(name or 'limited').lower().strip() in ('limited', 'tv', '0') else 1

    def _record_vt_cpu_gate(self, frame_number: int, attached: bool) -> bool:
        """Record a VT/CPU gate miss for ``frame_number`` (AUD-7606).

        The worker thread (``_decode_frame``) calls this when a VT-capable
        source served a CPU frame.  Two consecutive misses of an attached
        VT source (or an unattached/CPU-only source) release the frame for
        display.  Bookkeeping runs strictly under ``_cache_lock`` — the
        same discipline as ``_frame_cache`` and ``_clear_cache`` — so a
        concurrent cache clear can no longer be followed by a stale
        worker re-mark that flips a purged frame back to "ok" (the
        one-frame VT/CPU colour mixing race).

        Returns True when the frame is released for display.
        """
        with self._cache_lock:
            miss = self._vt_cpu_miss.get(frame_number, 0) + 1
            if len(self._vt_cpu_miss) > 1024:
                self._vt_cpu_miss.clear()
            self._vt_cpu_miss[frame_number] = miss
            if (not attached) or miss >= 2:
                if len(self._vt_cpu_ok) > 512:
                    self._vt_cpu_ok.clear()
                self._vt_cpu_ok.add(frame_number)
                return True
            return False

    def _decode_frame(
        self,
        frame_number: int,
        render_mode: str = 'preview',
        *,
        background: bool = False,
        scrub: Optional[bool] = None,
        sync_vt: bool = False,
    ) -> Optional[np.ndarray]:
        """Decode a single frame from video.

        P7.A: Primary method uses VideoFrameProviderService.
        Fallback to direct MediaSource if service not available.

        Args:
            frame_number: Frame index (0-based).
            render_mode: 'preview' or 'export' (P7.A).
            background: True 表示由后台预取线程调用（使用独立转换缓冲）。
            sync_vt: export 同步路径也优先 VT 硬解（时间线 comp clip 预览：
                无 UI 回环的逐帧同步渲染，VT 精确读取当场出帧，语义与 CPU
                同步解码等价但快 ~5 倍。真正的导出不设此标志，保持 CPU
                确定性路径）。

        Returns:
            RGBA float32 array, or None on error.
        """
        # P7.A: Try service first
        if self._use_service and self._clip_id is not None:
            is_export = 'export' in str(render_mode).lower()
            decoded: Optional["DecodedFrame"] = None
            # VT 零拷贝优先（仅预览，HEVC/H.264 4:2:0 且有 VT 源）：硬件解码 +
            # IOSurface 直通 GL，CVPixelBuffer 以延迟导入形式递交渲染线程。
            # 未命中/禁用时 request_gpu_frame 内部退化为普通 CPU 请求。
            if ((not is_export or sync_vt)
                    and not self._vt_gpu_disabled
                    and self._video_context is not None):
                # 拖拽/快梭：目标注册时已采样（scrub 参数；None 时兜底自采
                # 样，用于同步兜底等直接调用方）。回拖/大跳用 coarse（关键
                # 帧+1 帧，避免逐帧回退 walk），前向短跳用 deadline 顺序走
                # （平滑跟手，nearest±2 兜底）；停拖后回到精确路径。
                scrubbing = self._scrubbing() if scrub is None else scrub
                prev_req = self._prev_frame_request_frame
                backward = scrubbing and prev_req is not None and frame_number < prev_req
                # coarse 仅用于回拖/超远前跳：前向 ≤120 帧的跳变（含播放
                # 定时器被阻塞后的追帧）由 VT 顺序走间隙处理（不 seek、
                # 不闪画面）；coarse 会跳到 GOP 关键帧 = 播放中插入无关画面。
                far_forward = (
                    scrubbing and prev_req is not None
                    and frame_number - prev_req > 120
                )
                use_coarse = backward or far_forward
                source_in = self.parameters.get("source_in", 0)
                try:
                    gpu = self._video_context.request_gpu_frame_for_node(
                        clip_id=self._clip_id,
                        source_in=source_in,
                        source_out=self.parameters.get("source_out", -1),
                        timeline_frame=frame_number,
                        fps=self.parameters.get("fps", 24.0),
                        time_offset=self.parameters.get("time_offset", 0.0),
                        speed=self.parameters.get("speed", 1.0),
                        loop=self.parameters.get("loop", False),
                        hold=self.parameters.get("hold", False),
                        render_mode=render_mode,
                        # 拖拽走交互式（deadline 兜底 + is_stale 中止被取代的
                        # 回拖解码）；播放/前向短跳保持精确顺序解码。
                        interactive=scrubbing,
                        deadline_s=0.05 if scrubbing else None,
                        is_stale=self._decode_superseded if backward else None,
                        coarse=use_coarse,
                    )
                except Exception as e:
                    logger.warning(f"VT gpu frame request error at {frame_number}: {e}")
                    gpu = None
                if gpu is not None and getattr(gpu, 'deferred_import', '') == 'macos_vt_pixel_buffer':
                    # 交互式 coarse 返回的可能是邻近帧：按返回帧的实际源帧号
                    # 换算时间线本地号（speed=1 线性映射；非恒等映射参数时退回
                    # 请求帧号，拖拽预览下 speed 几乎恒为 1）。
                    try:
                        identity = (
                            int(source_in) == 0
                            and abs(float(self.parameters.get("speed", 1.0)) - 1.0) < 1e-6
                            and float(self.parameters.get("time_offset", 0.0)) <= 0.0
                            and not self.parameters.get("loop", False)
                        )
                        local_frame = (
                            int(gpu.frame_index) - int(source_in) if identity else frame_number
                        )
                    except Exception:
                        # 后处理异常不得丢弃已保留的 CVPixelBuffer（泄漏）
                        from src.shared.media.gpu_frame import safe_release_gpu_frame
                        safe_release_gpu_frame(gpu)
                        gpu = None
                    else:
                        return _VtFrameData(
                            gpu_frame=gpu,
                            yuv_matrix=self._vt_matrix_const(getattr(gpu, 'color_matrix', 'bt709')),
                            color_range=self._vt_range_const(getattr(gpu, 'color_range', 'limited')),
                            local_frame=local_frame,
                        )
                # CPU 回退帧（VT 未挂载）：与普通请求同语义
                decoded = gpu
            if decoded is None:
                decoded = self._request_decoded_frame_via_service(frame_number, render_mode)
            if decoded is None:
                return None
            if (
                not is_export
                and not self._vt_gpu_disabled
                and self._use_service
                and self._clip_id is not None
                and self._video_context is not None
            ):
                # VT 可用源但本帧拿到 CPU 结果（provider 内 VT 未服务）。
                # VT 挂载时游标越过的 exact miss 是瞬时的（重试会 seek 回来
                # 服务），连续两次未服务才放行，避免 VT/CPU 颜色混显；
                # VT 未挂载（ProRes 等 CPU 素材）结果是确定性的，立即放行
                # 避免每帧重复解码。
                attached = False
                try:
                    attached = bool(
                        self._video_context.has_vt_source_for_node(self._clip_id)
                    )
                except Exception:
                    attached = False
                # AUD-7606: gate bookkeeping moved into _record_vt_cpu_gate —
                # the counters now mutate strictly under _cache_lock (same
                # discipline as _frame_cache and _clear_cache), so a racing
                # clear cannot be followed by a stale worker re-mark.
                self._record_vt_cpu_gate(frame_number, attached)
            # 预览 + 独立 Y/U/V 平面 → GPU 直上条目（渲染线程 shader 转换，
            # 跳过 CPU YUV→RGB float32）。export 必须保持 CPU 确定性路径。
            if not is_export:
                planar = self._planar_frame_data(decoded)
                if planar is not None:
                    return planar
            return self._convert_decoded_frame(decoded, background=background)

        # Fallback: Direct MediaSource decoding
        if self._source is None:
            return None

        total = self.total_frames
        # Mirror VideoContext._compute_source_frame for the non-service path:
        # apply the source_in/out playback range with loop/hold semantics.
        source_in = self._get_param_int("source_in", 0)
        source_out = self._get_param_int("source_out", -1)
        if source_out < 0 and total > 0:
            source_out = total - 1
        hold = self.parameters.get("hold", False)
        loop = self.parameters.get("loop", False)

        frame_number += source_in

        range_len = source_out - source_in + 1
        if range_len > 0 and frame_number > source_out:
            if loop:
                frame_number = source_in + (frame_number - source_out - 1) % range_len
            elif hold:
                frame_number = source_out
            else:
                return None

        # Handle loop mode
        if loop and total > 0:
            frame_number = frame_number % total

        # Clamp to valid range
        if frame_number < 0:
            frame_number = 0
        if frame_number >= total and total > 0:
            frame_number = total - 1

        try:
            # Use MediaSource.read_frame()
            from src.shared.media import DecodedFrame
            decoded: DecodedFrame = self._source.read_frame(frame_number)

            # Convert DecodedFrame to RGBA float32
            rgba_float = self._convert_decoded_frame(decoded)

            return rgba_float

        except ValueError as e:
            # Frame out of range
            logger.warning(f"Frame {frame_number} out of range: {e}")
            return None

        except Exception as e:
            logger.warning(f"Frame decode error at {frame_number}: {e}")
            return None

    def _convert_decoded_frame(
        self,
        frame: "DecodedFrame",
        *,
        background: bool = False,
    ) -> Optional[np.ndarray]:
        """Convert DecodedFrame to RGBA float32 format for GPU upload.

        Uses pre-allocated buffer when possible to reduce per-frame allocation.
        渲染线程与后台预取线程各自持有独立缓冲（_rgba_buffer /
        _bg_rgba_buffer），避免并发转换共用同一 scratch 造成画面串帧。

        Args:
            frame: DecodedFrame from MediaSource.
            background: True 使用后台线程专属缓冲。

        Returns:
            RGBA float32 array (H, W, 4), normalized to [0, 1].
        """
        if frame.is_packed and frame.data is not None:
            data = frame.data
            h, w = data.shape[:2]
            channels = data.shape[2] if data.ndim == 3 else 1

            # Reuse pre-allocated buffer if size matches
            buf_attr = '_bg_rgba_buffer' if background else '_rgba_buffer'
            buf = getattr(self, buf_attr, None)
            if buf is None or buf.shape[0] != h or buf.shape[1] != w:
                buf = np.zeros((h, w, 4), dtype=np.float32)
                setattr(self, buf_attr, buf)
            else:
                buf[:] = 0  # Clear alpha

            result = buf

            if data.dtype == np.uint8:
                # uint8 [0, 255] -> float32 [0, 1]
                if channels >= 3:
                    np.divide(data[:, :, :3], 255.0, out=result[:, :, :3], dtype=np.float32)
                if channels == 4 or frame.has_alpha:
                    if channels == 4:
                        np.divide(data[:, :, 3], 255.0, out=result[:, :, 3], dtype=np.float32)
                    else:
                        result[:, :, 3] = 1.0
                else:
                    result[:, :, 3] = 1.0

            elif data.dtype == np.uint16:
                # uint16 [0, 65535] -> float32 [0, 1]
                pix_fmt = frame.pixel_format.lower() if frame.pixel_format else ''
                is_packed_rgb = pix_fmt in ('rgb48le', 'rgb48be', 'rgba64le', 'rgba64be',
                                            'bgr48le', 'bgr48be', 'bgra64le', 'bgra64be')

                if is_packed_rgb:
                    max_val = 65535.0
                elif frame.bit_depth == 10:
                    max_val = 1023.0
                elif frame.bit_depth == 12:
                    max_val = 4095.0
                else:
                    max_val = 65535.0

                if channels >= 3:
                    np.divide(data[:, :, :3], max_val, out=result[:, :, :3], dtype=np.float32)
                if channels == 4:
                    np.divide(data[:, :, 3], max_val, out=result[:, :, 3], dtype=np.float32)
                else:
                    result[:, :, 3] = 1.0

            elif data.dtype == np.float32 or data.dtype == np.float16:
                if channels >= 3:
                    result[:, :, :3] = data[:, :, :3].astype(np.float32)
                if channels == 4:
                    result[:, :, 3] = data[:, :, 3].astype(np.float32)
                else:
                    result[:, :, 3] = 1.0

            else:
                logger.warning(f"Unknown dtype {data.dtype}, treating as uint8")
                np.divide(data[:, :, :3], 255.0, out=result[:, :, :3], dtype=np.float32)
                result[:, :, 3] = 1.0

            return np.ascontiguousarray(result)

        elif frame.is_planar and frame.planes:
            # Planar format (YUV) - convert to RGBA float32 via ColorConverter。
            # 服务路径（VideoFrameProviderService）的 proxy 源启用 planar 输出，
            # 含 yuv420p10le 等 10-bit 格式；DecodedFrame.to_rgb() 只支持
            # packed 格式，这里用矩阵转换（正确处理色度上采样/limited range/
            # 位深归一化，保留 10-bit 精度直达 float32）。
            planes = frame.planes
            if len(planes) >= 3:
                y, u, v = planes[0], planes[1], planes[2]
            elif len(planes) == 2:
                uv = planes[1]
                if uv.ndim == 3 and uv.shape[-1] == 2:
                    u, v = uv[..., 0], uv[..., 1]
                elif uv.ndim == 2:
                    u, v = uv[:, 0::2], uv[:, 1::2]
                else:
                    logger.warning(f"Planar format {frame.pixel_format}: unexpected UV shape")
                    return None
            else:
                logger.warning(f"Planar format {frame.pixel_format} has too few planes")
                return None

            from src.shared.color.types import ColorMatrix, ColorRange
            from src.shared.media.color_decode import get_color_converter

            matrix = ColorMatrix.BT709
            range_ = ColorRange.LIMITED
            meta = frame.metadata
            if meta is not None:
                m = str(getattr(meta, 'color_matrix', '') or '').lower()
                if 'bt2020' in m:
                    matrix = ColorMatrix.BT2020_NC
                elif 'smpte170m' in m or 'bt601' in m or 'bt470' in m:
                    matrix = ColorMatrix.BT601
                r = str(getattr(meta, 'color_range', '') or '').lower()
                if r in ('full', 'pc', '1'):
                    range_ = ColorRange.FULL

            rgb = get_color_converter().yuv_to_rgb(
                y, u, v,
                matrix=matrix,
                range_=range_,
                bit_depth=int(frame.bit_depth or 8),
            )

            # P7-Alpha：YUVA 第四平面。Alpha 归一化用 alpha 自己的位深
            # （plane_infos[3]，Topos a8/a10/a12/a16 独立于 luma 的 10）；
            # 源为预乘 Alpha 时转 straight（节点对下游声明 straight）。
            alpha_arr = None
            if len(planes) >= 4:
                cand = planes[3]
                if cand.ndim == 2 and cand.shape[:2] == rgb.shape[:2]:
                    alpha_bit_depth = 8
                    infos = getattr(frame, 'plane_infos', None)
                    if infos and len(infos) >= 4:
                        alpha_bit_depth = int(getattr(infos[3], 'bit_depth', 8) or 8)
                    alpha_arr = np.clip(
                        cand.astype(np.float32) / float((1 << alpha_bit_depth) - 1),
                        0.0, 1.0)
                    premult = bool(
                        (getattr(frame, 'extra', None) or {}).get(
                            'topos_alpha_premultiplied', False))
                    if premult:
                        safe = np.maximum(alpha_arr, 1.0 / 65535.0)
                        rgb = np.where(
                            alpha_arr > 1.0 / 65535.0, rgb / safe[..., None], 0.0)
            h, w = rgb.shape[:2]
            buf_attr = '_bg_rgba_buffer' if background else '_rgba_buffer'
            result = getattr(self, buf_attr, None)
            if result is None or result.shape[0] != h or result.shape[1] != w:
                result = np.zeros((h, w, 4), dtype=np.float32)
                setattr(self, buf_attr, result)
            result[:, :, :3] = rgb
            result[:, :, 3] = alpha_arr if alpha_arr is not None else 1.0
            return np.ascontiguousarray(result)

        return None

    def _own_cache_frame_data(self, frame_data: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """AUD-7601 修复：入缓存前解别名——缓存条目不得与转换 scratch 缓冲相同。

        packed / CPU-YUV 路径的 ``_convert_decoded_frame`` 复用预分配
        scratch（``_rgba_buffer`` / ``_bg_rgba_buffer``），返回值即缓冲本身
        （``np.ascontiguousarray`` 对 C 连续数组原样返回同一对象）。若把该
        对象按帧号写入 ``_frame_cache``，同尺寸下多个帧号会指向同一缓冲，
        内容恒为"最后一次解码的帧"：邻域缓存命中 / present-latest 上屏的
        是最近解码的邻居帧而非请求帧，短循环导出第二遍精确命中缓存时输出
        循环末帧像素；渲染线程 ``np.flipud`` 读取期间 worker 复写同一
        ``_bg_rgba_buffer``（转换不持 `_cache_lock`）还会撕裂出混合帧。
        与 scratch 身份相同（is）的 ndarray 拷贝解别名；planar / VT 条目
        持有独立数组，原样返回。
        """
        if frame_data is getattr(self, '_rgba_buffer', None) or \
                frame_data is getattr(self, '_bg_rgba_buffer', None):
            return frame_data.copy()
        return frame_data

    def _prefetch_via_service(
        self,
        center_frame: int,
        cache_size: int,
        render_mode: str = 'preview'
    ) -> None:
        """Prefetch frames via VideoFrameProviderService (P7.A).

        Args:
            center_frame: Current frame number.
            cache_size: Number of frames to prefetch on each side.
            render_mode: 'preview' or 'export'.
        """
        if self._clip_id is None or self._video_context is None:
            return

        total = self.total_frames
        if total == 0:
            return

        # Calculate range
        start_frame = max(0, center_frame - cache_size)
        end_frame = min(total - 1, center_frame + cache_size)

        # Prefetch via VideoContext
        source_in = self.parameters.get("source_in", 0)
        source_out = self.parameters.get("source_out", -1)
        time_offset = self.parameters.get("time_offset", 0.0)
        speed = self.parameters.get("speed", 1.0)

        self._video_context.prefetch_for_node(
            clip_id=self._clip_id,
            timeline_start=start_frame,
            timeline_end=end_frame,
            source_in=source_in,
            source_out=source_out,
            fps=self.parameters.get("fps", 24.0),
            time_offset=time_offset,
            speed=speed,
            render_mode=render_mode
        )

    def _record_frame_request(self, center_frame: int) -> None:
        """记录帧号请求轨迹（渲染线程调用；worker 只读判定）。

        驱动 _requests_advancing()/_scrubbing()：播放/拖拽判定与回跳
        coarse 快解。异步注册（_preload_frames）与同步解码兜底
        （upload_frame export/sync_vt 分支）都必须记录，否则同步路径
        的回跳（播放头重锚、seek 折返）永远走精确路径，触发 VT
        flush+关键帧 seek 风暴（百毫秒级长尾）。
        """
        now = time.perf_counter()
        if center_frame != self._last_frame_request_frame:
            prev = self._last_frame_request_frame
            self._prev_frame_request_frame = prev
            self._last_frame_request_frame = center_frame
            self._last_frame_request_at = now
            self._request_change_times.append(now)
            if len(self._request_change_times) > 4:
                self._request_change_times.pop(0)
            if prev is not None:
                self._request_change_deltas.append(int(center_frame) - int(prev))
                if len(self._request_change_deltas) > 4:
                    self._request_change_deltas.pop(0)

    def _preload_frames(self, center_frame: int, render_mode: str = 'preview') -> None:
        """Schedule background pre-load of frames around current position.

        P7.A: Uses service prefetch when available.

        渲染线程只登记最新目标（latest-wins）并唤醒预取线程；邻域的解码与
        float32 转换全部在后台完成，渲染线程每帧只处理当前帧。目标被更新
        时旧目标自然作废（单槽覆盖）。

        Args:
            center_frame: Current frame number.
            render_mode: 'preview' or 'export' (P7.A).
        """
        # 请求轨迹：帧号变化的时间戳驱动 _requests_advancing()（播放/拖拽
        # 判定），供邻域抑制与 present-latest 使用。
        self._record_frame_request(center_frame)
        # 拖拽判定在注册时（UI 线程时序）一次性采样并随目标传递：worker
        # 可能滞后多个请求才处理目标，届时重新采样会得到与注册时不同的
        # 结果（交互式解码返回邻近帧却被精确标记，显示与跳过逻辑双双错乱）。
        scrub = self._scrubbing()
        # 直连源回退模式（_use_service=False）禁止后台预取：FFmpegSource
        # 单实例不可重入，服务模式的 ClipSession.decode_lock 串行化在此
        # 不存在，worker 与渲染线程同步解码并发进入同一实例会在 libav
        # 原生层崩溃（SIGABRT）。该模式下 upload_frame 走同步解码分支，
        # 不依赖 worker 落帧。
        if not self._use_service:
            return
        with self._cache_lock:
            self._preload_target = (int(center_frame), render_mode, scrub)
        self._ensure_preload_thread()
        self._preload_wakeup.set()

    def _ensure_preload_thread(self) -> None:
        """启动（或复用）后台预取守护线程。"""
        thread = self._preload_thread
        if thread is not None and thread.is_alive():
            return
        self._preload_stop = False
        self._preload_wakeup.clear()
        thread = threading.Thread(
            target=self._preload_worker,
            name=f"VideoFramePreload-{self.id}",
            daemon=True,
        )
        self._preload_thread = thread
        thread.start()

    def _preload_worker(self) -> None:
        """后台预取主循环：等待新目标 → 填充邻域缓存。"""
        while not self._preload_stop:
            self._preload_wakeup.wait(timeout=0.5)
            self._preload_wakeup.clear()
            if self._preload_stop:
                break
            with self._cache_lock:
                target = self._preload_target
                self._preload_target = None
            if target is None:
                continue
            # 目标三元组 (帧号, render_mode, 注册时的拖拽判定)
            scrub = target[2] if len(target) > 2 else False
            try:
                self._run_preload(target[0], target[1], scrub)
            except Exception as e:
                logger.warning(
                    f"VideoFrameNode: background preload failed "
                    f"(center={target[0]}, mode={target[1]}): {e}"
                )

    # 连续解码节拍间隔小于该值时判定"交互中"（拖拽/播放），抑制后台"邻域"
    # 预取：后台与渲染线程会在共享服务的 decode_lock 上串行排队，反而拖慢
    # 当前帧。当前帧本身的优先解码不受此门控影响。
    _PRELOAD_SUPPRESS_WINDOW_S: float = 0.25
    # 渲染线程最近一次内联解码慢于该值时，才在交互窗口内抑制邻域预取
    # （快解码资产允许前瞻命中，慢资产避免锁竞争）。
    _PRELOAD_SUPPRESS_DECODE_MS: float = 80.0
    # 求值请求的帧号在最近该时长内发生过变化 → 判定"播放/拖拽中"。
    _PLAYBACK_ADVANCE_WINDOW_S: float = 0.5

    def _requests_advancing(self) -> bool:
        """求值请求是否在连续变化帧号（播放/拖拽进行中）。

        播放时每个 tick 请求的帧号都不同；静止时帧号不变。旧门控依赖
        _last_render_decode_ms（仅 export 同步兜底路径更新，预览播放恒
        为 0）导致邻域抑制在预览播放期间从不生效，±cache_frames 窗口的
        同步批量解码会抢占当前帧解码。
        """
        return (
            self._last_frame_request_frame is not None
            and self._prev_frame_request_frame is not None
            and self._last_frame_request_frame != self._prev_frame_request_frame
            and (time.perf_counter() - self._last_frame_request_at)
            < self._PLAYBACK_ADVANCE_WINDOW_S
        )

    def _scrubbing(self) -> bool:
        """拖拽/快梭进行中：请求帧号突发变化（150ms 内 ≥2 次）且步长特征
        不是播放——含明显跳变（|Δ|>4，容忍播放 tick 的 +2/+4 时序补偿：
        误判会走 coarse 关键帧快梭，播放中插入无关 GOP 画面）或方向反转。
        单帧步进（~200ms 间隔）不算，保持精确顺序解码。
        """
        times = self._request_change_times
        deltas = self._request_change_deltas
        if len(times) < 2 or (time.perf_counter() - times[-2]) >= 0.15:
            return False
        if not deltas:
            return False
        recent = deltas[-3:]
        has_jump = any(abs(d) > 4 for d in recent)
        has_reversal = any(d1 * d2 < 0 for d1, d2 in zip(recent, recent[1:]))
        return has_jump or has_reversal

    def _decode_superseded(self) -> bool:
        """当前 worker 解码是否已被更新的目标取代（交互式中止判据）。"""
        with self._cache_lock:
            return self._preload_target is not None

    def _neighbors_suppressed(self) -> bool:
        """邻域预取是否应让位（当前帧优先解码不受影响）。"""
        if self._requests_advancing():
            # 播放/拖拽中：解码带宽全部留给当前帧，邻域窗口等静止后再预热。
            return True
        if (
            time.perf_counter() - self._last_render_decode_at
            < self._PRELOAD_SUPPRESS_WINDOW_S
            and self._last_render_decode_ms > self._PRELOAD_SUPPRESS_DECODE_MS
        ):
            return True
        return False

    def _notify_frame_ready(self) -> None:
        """当前帧后台解码完成 → 通知页面重求值（线程安全）。"""
        ctx = self._video_context
        notify = getattr(ctx, 'notify_frame_ready', None) if ctx is not None else None
        if notify is not None:
            try:
                notify()
            except Exception as e:
                logger.debug(f"VideoFrameNode: frame-ready notify failed: {e}")

    def _decode_and_store(
        self,
        frame_num: int,
        render_mode: str,
        generation: int,
        *,
        notify: bool,
        scrub: bool = False,
    ) -> Optional[bool]:
        """后台解码单帧并写入缓存。

        Returns:
            True=已写入缓存；False=解码失败；None=无需处理（已缓存/代际
            过期/已停止）。notify=True 时无论成败都会回调页面（持帧的
            上传路径依赖完成信号重求值）。
        """
        with self._cache_lock:
            if self._media_generation != generation:
                return None
            cached = frame_num in self._frame_cache
            # AUD-7606: gate read under the same lock (was unlocked).
            vt_cpu_ok = frame_num in self._vt_cpu_ok
        if cached:
            # VT 门控通道（upload_frame 对 VT 可用源的 CPU 缓存帧持帧等待
            # VT）需要绕过缓存短路重解一次：VT 服务则递交 pending，未服务
            # 则 _decode_frame 标记 _vt_cpu_ok 放行上屏。非此情形维持短路。
            vt_recheck = (
                not self._vt_gpu_disabled
                and self._use_service
                and self._clip_id is not None
                and self._video_context is not None
                and 'export' not in str(render_mode).lower()
                and not vt_cpu_ok
            )
            if not vt_recheck:
                return None
        with self._vt_lock:
            if self._vt_pending is not None and self._vt_pending[0] == frame_num:
                return None
            pending_empty = self._vt_pending is None
            last_target = self._vt_last_decoded_target
        # 重复请求防护：notify 触发的重求值会再次注册刚交付/刚上屏的
        # 同一帧（pending 可能已被渲染线程消费）。VT 游标已越过该帧，
        # 重解会触发关键帧回退 seek（百毫秒级）。只跳过"与 worker 上次
        # 解码目标完全相同"的帧——新的回跳目标（拖拽往回）会正常解码。
        if pending_empty and frame_num == last_target:
            return None

        frame_data = self._decode_frame(frame_num, render_mode, background=True, scrub=scrub)

        if frame_data is None:
            # 拖拽/播放中（请求快速变化）交互式解码被 superseded 而返回
            # None 是正常节流，不计入失败（否则会误触渲染线程同步兜底）。
            if not self._requests_advancing():
                with self._cache_lock:
                    count = self._async_fail_counts.get(frame_num, 0) + 1
                    self._async_fail_counts[frame_num] = count
                    if count >= 2:
                        self._failed_async_frames.add(frame_num)
            if notify:
                self._notify_frame_ready()
            return False

        if isinstance(frame_data, _VtFrameData):
            # VT 帧不入 _frame_cache（CVPixelBuffer 稀缺）：单槽递交渲染
            # 线程，覆盖时立即释放旧帧的像素缓冲。store 时（消费之前）记录
            # 解码目标——覆盖"pending 被渲染线程消费后同帧重复请求"的窗口。
            # pending 键用返回帧的真实本地号（coarse 拖拽可能是邻近关键帧）。
            # 注意只有精确解码才标记目标完成：coarse 结果显示的是邻近帧，
            # 停拖后的精确补解不能被重复请求防护拦截。
            stale: Optional[Tuple[int, _VtFrameData]] = None
            key = (frame_data.local_frame
                   if frame_data.local_frame >= 0 else frame_num)
            with self._vt_lock:
                if self._media_generation == generation:
                    # 同 key 覆盖也必须释放旧条目（vt_recheck 重解同帧等
                    # 场景）：单槽只保留最新一帧，旧 CVPixelBuffer 直接
                    # 丢弃会随 GC 泄漏（曾以每上屏帧 1 个的速率泄漏）。
                    if self._vt_pending is not None:
                        stale = self._vt_pending
                    self._vt_pending = (key, frame_data)
                    if not scrub:
                        self._vt_last_decoded_target = frame_num
                else:
                    stale = (frame_num, frame_data)
            if stale is not None:
                self._release_vt_entry(stale[1])
            if notify:
                self._notify_frame_ready()
            # 播放前瞻流水线：worker 刚解完本帧（VT 游标=下一帧），顺手
            # 把后续顺序帧预取进 reorder 缓冲——下一轮请求直接命中缓冲，
            # 把"每帧冷启动解码+双 UI 往返"的乒乓延迟摊平为后台吞吐。
            # 预取深度 3：播放头超前于解码游标时（UI 往返跟不上源帧率，
            # 请求帧号逐轮前跳），单帧预取填不平缺口，落后量正反馈放大，
            # 直至 >120 帧触发 seek+GOP 重解（播放中途冻结的根源）。
            # prefetch_vt_sequential 只解"恰好为游标+1"的帧，循环即依次
            # 预取 3 帧（reorder 缓冲上限 16，仍在窗口内）。仅顺序推进时
            # 预取（拖拽走 coarse、静止走邻域预取，均不适用）。
            if not scrub and self._requests_advancing():
                for ahead in range(1, 4):
                    if not self._kick_vt_prefetch(frame_num + ahead):
                        break
            return True

        with self._cache_lock:
            if self._media_generation == generation:
                # AUD-7601：worker 的解码结果是共享 _bg_rgba_buffer 的
                # 别名，入缓存前拷贝解别名（缓存条目不得与 scratch 相同）。
                self._frame_cache[frame_num] = self._own_cache_frame_data(frame_data)
                self._async_fail_counts.pop(frame_num, None)
            else:
                return None
        if notify:
            self._notify_frame_ready()
        return True

    def _run_preload(self, center_frame: int, render_mode: str, scrub: bool = False) -> None:
        """在后台线程解码当前帧（优先）并填充 ±cache_frames 邻域缓存。"""
        cache_size = self.parameters.get("cache_frames", 5)
        cache_size = max(1, min(30, cache_size))

        with self._cache_lock:
            generation = self._media_generation

        total = self.total_frames
        if total == 0:
            return

        # 直连源回退模式不进 worker（解码串行契约，见 _preload_frames 的
        # 门控说明）；此防御覆盖门控前已排队的陈旧目标（换源/重开瞬间）。
        if not self._use_service:
            return

        # 1) 当前帧优先：预览持帧路径（upload_frame 异步 miss）依赖它尽快
        #    落地，完成后回调页面重求值。不受邻域抑制门控影响。
        # 注：VT 源自身的 _decode_until_present_frame 已按需走间隙顺序解码
        # （实测 4K P010 7ms/帧），额外的后台顺序预取线程会与 worker 在
        # vt_lock/GIL 上争用，实测负收益，故不启用。
        self._decode_and_store(center_frame, render_mode, generation, notify=True, scrub=scrub)

        # 2) 邻域前瞻（交互抑制门控保护，避免与渲染线程抢 decode_lock）。
        #    播放/拖拽中跳过整窗预热（同步批量解码不可抢占，会饿死后续
        #    tick 的当前帧），并回收窗口外的旧帧——播放中 worker 每轮
        #    提前返回，若不在此淘汰，_frame_cache 会随播放时长无界增长。
        if self._neighbors_suppressed():
            self._evict_old_frames(center_frame, cache_size, release_textures=False)
            return

        with self._cache_lock:
            newer_target_queued = self._preload_target is not None
        if newer_target_queued:
            # 已有更新目标排队（很可能是持帧路径的当前帧）：跳过本轮邻域，
            # 让 worker 立即转向。
            return

        # P7.A: Prefetch via service（共享服务缓存，线程安全）。
        # 注意这是一段同步批量解码（不可中途抢占），上面的排队检查必须在
        # 它之前执行，否则旧位置的整窗预热会推迟新位置当前帧的就绪。
        if self._use_service and self._clip_id is not None:
            self._prefetch_via_service(center_frame, cache_size, render_mode)

        # Local conversion cache pre-load (float32 RGBA for GPU upload)
        # VT 路径激活时跳过：CPU packed 转换与 VT 硬解的颜色特性不同
        # （packed rgb48le 回退链存在过饱和问题），混入 _frame_cache 会被
        # 精确命中展示，造成静止/步进时颜色跳变；VT 源自身处理间隙解码。
        vt_active = self._vt_bridge is not None and not self._vt_gpu_disabled
        if vt_active:
            return
        start_frame = max(0, center_frame - cache_size)
        end_frame = min(total - 1, center_frame + cache_size)
        neighbors = sorted(
            (f for f in range(start_frame, end_frame + 1) if f != center_frame),
            key=lambda f: abs(f - center_frame),
        )

        for frame_num in neighbors:
            if self._preload_stop:
                return
            with self._cache_lock:
                if self._preload_target is not None:
                    # 新目标（可能是持帧路径的当前帧）已排队：立即中止本轮
                    # 邻域填充，让 worker 转向最新目标。
                    return
            # 渲染线程在预取过程中开始内联解码（export 等）：立即让位
            if self._neighbors_suppressed():
                return
            result = self._decode_and_store(
                frame_num, render_mode, generation, notify=False
            )
            if result is None:
                return  # 换源/清缓存，本轮预取作废

        # Evict frames outside range（后台线程不触碰 GL 纹理缓存）
        self._evict_old_frames(center_frame, cache_size, release_textures=False)

    def _stop_preload_thread(self, join: bool = True, timeout: float = 1.0) -> None:
        """停止后台预取线程（节点换源/清理时调用）。

        Args:
            join: True 时等待线程退出（仅节点销毁路径使用）；渲染线程内的
                换源/清缓存路径传 False，避免被正在进行的解码阻塞——过期
                写入由 _media_generation 代际校验兜底。
            timeout: join 超时（秒）。
        """
        self._preload_stop = True
        self._preload_wakeup.set()
        thread = self._preload_thread
        if (
            join
            and thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout)
        self._preload_thread = None

    def _evict_old_frames(
        self,
        center_frame: int,
        cache_size: int,
        *,
        release_textures: bool = True,
    ) -> None:
        """Remove frames outside cache range.

        Args:
            center_frame: Current frame position.
            cache_size: Cache range size.
            release_textures: 是否同时释放 GPU 纹理缓存。GL 对象只能在
                渲染线程释放；后台预取线程必须传 False。
        """
        range_start = center_frame - cache_size
        range_end = center_frame + cache_size

        # Evict frames outside range
        with self._cache_lock:
            to_remove = [f for f in self._frame_cache.keys()
                         if f < range_start or f > range_end]
            for frame_num in to_remove:
                self._frame_cache.pop(frame_num, None)

        if not release_textures:
            return

        for frame_num in to_remove:
            # Also evict GPU texture if cached
            if frame_num in self._texture_cache:
                tex = self._texture_cache.pop(frame_num)
                if tex is not None:
                    tex.release()

    def _clear_cache(self) -> None:
        """Clear all cached frames and textures."""
        # 换源/重开：作废后台预取（不阻塞渲染线程 join），提升媒体代际，
        # worker 侧按代际拒绝过期写入。
        self._stop_preload_thread(join=False)
        self._media_generation += 1
        with self._cache_lock:
            self._frame_cache.clear()
            self._async_fail_counts.clear()
            self._failed_async_frames.clear()
            self._vt_cpu_ok.clear()
            self._vt_cpu_miss.clear()

        # VT 递交帧立即释放（CVPixelBuffer 稀缺）；桥接/程序按渲染线程
        # 资源处理（_clear_cache 的调用方 _open_video/cleanup 均在渲染线程）
        with self._vt_lock:
            stale_vt = self._vt_pending
            self._vt_pending = None
            self._vt_last_decoded_target = None
        if stale_vt is not None:
            self._release_vt_entry(stale_vt[1])
        self._release_vt_bridge()
        # AUD-7602 fix: `_vt_programs` 的值有两种形态——`'_ibo'` 键是 Buffer，
        # `'2d'`/`'rect'` 键是 (program, vao) 元组。旧代码对元组调 `.release()`
        # 抛 AttributeError 且被空 except 吞掉 → VT 转换 program/VAO 从不释放，
        # 随会话内换源次数无界累积。按形态分派释放；失败记 debug 而非静默。
        for obj in self._vt_programs.values():
            resources = obj if isinstance(obj, tuple) else (obj,)
            for res in resources:
                if res is None:
                    continue
                try:
                    res.release()
                except Exception as e:
                    logger.debug(f"VT program resource release failed: {e}")
        self._vt_programs.clear()
        if self._vt_quad_vbo is not None:
            try:
                self._vt_quad_vbo.release()
            except Exception as e:
                logger.debug(f"VT quad VBO release failed: {e}")
            self._vt_quad_vbo = None

        # Clear GPU texture cache
        for tex in self._texture_cache.values():
            if tex is not None:
                tex.release()
        self._texture_cache.clear()

        # GPU 直上路径的 FBO 与平面纹理池（与 _gpu_texture 同线程释放）
        if self._gpu_fbo is not None:
            try:
                self._gpu_fbo.release()
            except Exception:
                pass
            self._gpu_fbo = None
        if self._gpu_mid_fbo is not None:
            try:
                self._gpu_mid_fbo.release()
            except Exception as e:
                logger.debug(f"mid FBO release failed: {e}")
            self._gpu_mid_fbo = None
        # AUD-7603: FBO 释放不连带附件——mid 纹理单独释放
        if self._gpu_mid_tex is not None:
            try:
                self._gpu_mid_tex.release()
            except Exception as e:
                logger.debug(f"mid texture release failed: {e}")
            self._gpu_mid_tex = None
        if self._flip_vao is not None:
            try:
                self._flip_vao.release()
            except Exception as e:
                logger.debug(f"flip VAO release failed: {e}")
            self._flip_vao = None
        # AUD-7603: 旧路径只把 _flip_vbo/_flip_prog 置 None 不 release，
        # flip IBO 是局部变量全文件无释放点——三者全部显式释放。
        if self._flip_ibo is not None:
            try:
                self._flip_ibo.release()
            except Exception as e:
                logger.debug(f"flip IBO release failed: {e}")
            self._flip_ibo = None
        if self._flip_vbo is not None:
            try:
                self._flip_vbo.release()
            except Exception as e:
                logger.debug(f"flip VBO release failed: {e}")
            self._flip_vbo = None
        if self._flip_prog is not None:
            try:
                self._flip_prog.release()
            except Exception as e:
                logger.debug(f"flip program release failed: {e}")
            self._flip_prog = None
        if self._yuv_uploader is not None:
            try:
                self._yuv_uploader.release()
            except Exception:
                pass
            self._yuv_uploader = None
            self._yuv_uploader_ctx = None

        # Clear current GPU texture
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None

    # ── Cache Key ──

    def _get_cache_key(self, frame_index: int, render_mode: str = 'preview') -> str:
        """Generate cache key for frame.

        P6.2: Key includes decode_policy to prevent cross-contamination.
        P7.A: Key includes quality from render_mode.

        Args:
            frame_index: Frame number.
            render_mode: 'preview' or 'export'.

        Returns:
            Cache key string.
        """
        path = self.parameters.get("file_path", "")
        quality = 'proxy' if render_mode == 'preview' else 'master'
        return f"{path}:{frame_index}:{quality}:{self._decode_policy}"

    # ── Frame Selection ──

    def _apply_frame_override(self, eval_ctx: Any,
                              eval_frame: Optional[int]) -> Optional[int]:
        """Remap the playhead through a downstream time node (P0-04).

        The evaluator publishes ``ctx.node_frame_overrides`` each frame;
        when this node sits upstream of a time_offset/frame_hold/retime
        node, its remap (already composed across the chain) is applied to
        the playhead. Missing/invalid entries leave the frame untouched.
        """
        if eval_frame is None:
            return eval_frame
        overrides = getattr(eval_ctx, "node_frame_overrides", None)
        if not overrides:
            return eval_frame
        remap = overrides.get(self.id)
        if remap is None:
            return eval_frame
        try:
            return int(remap.apply(eval_frame))
        except Exception:
            return eval_frame

    def _resolve_frame_number(self, timeline_frame: Optional[int] = None) -> int:
        """Resolve frame number from parameters.

        Uses either frame_number or time_offset to determine
        the actual frame to display.

        Args:
            timeline_frame: Current composition playhead frame from the
                eval context. When provided, the node is timeline-driven:
                the local frame is the playhead relative to the node's
                time_range comp_start (source_in/speed/loop/hold are then
                applied downstream by VideoContext._compute_source_frame).

        Returns:
            Frame number (0-based, timeline-local when driven).
        """
        if timeline_frame is not None:
            comp_start = 0
            tr = getattr(self, "time_range", None)
            if isinstance(tr, dict) and tr.get("enabled", True):
                try:
                    comp_start = int(tr.get("comp_start", 0) or 0)
                except (TypeError, ValueError):
                    comp_start = 0
            return max(0, int(timeline_frame) - comp_start)

        # Priority: explicit frame_number
        frame_number = self._get_param_int("frame_number", 0)

        # Alternative: time_offset
        time_offset = self._get_param_clamped("time_offset", min_val=0.0)

        if time_offset > 0 and self.video_fps > 0:
            # Convert time to frame number
            frame_from_time = int(time_offset * self.video_fps)
            # Use whichever is larger (prioritize explicit frame_number if set)
            if frame_number == 0:
                frame_number = frame_from_time

        return frame_number

    # ── GPU Upload ──

    def upload_frame(
        self,
        ctx: moderngl.Context,
        frame_number: int,
        render_mode: str = 'preview'
    ) -> bool:
        """Upload a frame to GPU texture.

        P7.A: Uses render_mode to determine quality.

        Args:
            ctx: ModernGL context.
            frame_number: Frame to upload.
            render_mode: 'preview' or 'export'.

        Returns:
            True if upload succeeded.
        """
        is_export = 'export' in str(render_mode).lower()
        # VT 可用源的单通道显示：VT 递交帧是预览播放的唯一上屏来源。
        # CPU 解码路径（邻域预取/兜底解的 packed rgb48le、planar YUV）与
        # VT 硬解的颜色特性不同（打包回退链过饱和），同帧内容经两条链
        # 渲染的差异高达 ~0.17 平均色偏，混显即用户报告的「播放中插入
        # 其他画面」。被门控的 CPU 缓存帧持旧纹理，交 worker 以 VT 重解
        # （_decode_and_store 对此类帧绕过缓存短路）；VT 确实未服务该帧
        # 时 _decode_frame 标记 _vt_cpu_ok 逐帧放行。export 保持 CPU
        # 确定性路径，不受门控。
        vt_gate = (
            not is_export
            and not self._vt_gpu_disabled
            and self._use_service
            and self._clip_id is not None
            and self._video_context is not None
        )
        # 递交槽先于 CPU 缓存精确命中：缓存里可能已有同号 CPU 帧（邻域
        # 预取先落地），pending 里是刚解好的 VT 帧——必须取 VT。
        vt_taken = self._take_vt_pending(frame_number) if vt_gate else None
        if vt_taken is not None:
            vt_num, vt_entry = vt_taken
            if self._upload_frame_gpu_vt(ctx, vt_entry):
                self._revision += 1
                self._current_frame = vt_num
                self._load_state = VideoLoadState.UPLOADED_GPU
                self._is_fallback = False
                return True
            # 绑定/渲染失败：entry 已释放，持旧帧等待回退路径
            return self._gpu_texture is not None

        # Get frame data（与后台预取线程共享 _frame_cache，需持锁读写）
        with self._cache_lock:
            frame_data = self._frame_cache.get(frame_number)
            # AUD-7606: gate read inside the lock (was an unlocked follow-up
            # read that could observe a cleared-then-re-marked set).
            vt_cpu_ok = frame_number in self._vt_cpu_ok
        if frame_data is not None and vt_gate and not vt_cpu_ok:
            frame_data = None  # VT 可用源的 CPU 缓存帧暂不上屏，等待 VT 重解

        # present-latest 实际上屏的帧号（None = 精确命中 frame_number）
        present_latest_frame: Optional[int] = None
        if frame_data is None:
            can_async = (
                not is_export
                and self._use_service
                and (self._clip_id is not None or self._source is not None)
                and frame_number not in self._failed_async_frames
            )
            if can_async:
                # 预览持帧：当前帧交给后台优先解码，本求值沿用旧纹理（不冻结
                # 交互）；解码完成经 VideoContext.notify_frame_ready 回调页面
                # 重求值。export 路径必须同步精确，不走此分支。
                self._preload_frames(frame_number, render_mode)
                # 播放中（解码落后于播放头）按 present-latest 展示：取缓存里
                # 最新且不超前于本次请求的已解码帧上屏。否则异步解码永远
                # 慢 tick 一拍，每次求值都 miss 在"未来的帧"上，画面会冻结
                # 在旧纹理（请求 N 时缓存只有 <N 的已解码帧）。静止求值保持
                # 原持帧语义，notify 后的求值会精确命中 frame_number。
                # VT 可用源时 CPU 缓存帧同样不参与混显（同上 vt_gate）。
                if self._requests_advancing():
                    with self._cache_lock:
                        newer = None
                        for f in self._frame_cache:
                            if self._current_frame < f <= frame_number and (
                                newer is None or f > newer
                            ):
                                newer = f
                        if newer is not None and vt_gate and newer not in self._vt_cpu_ok:
                            newer = None
                        if newer is not None:
                            frame_data = self._frame_cache[newer]
                            present_latest_frame = newer
                if frame_data is None:
                    return self._gpu_texture is not None
                # present-latest 命中：直接进入下方上传段。注意不能落入后面
                # 的 export/兜底 else——那是 can_async=False 时才走的路径。
            elif is_export or not self._use_service or frame_number in self._failed_async_frames:
                # 导出确定性优先；直连源回退模式无后台 worker（解码串行
                # 契约所限，见 _preload_frames），同步解码；连续后台失败的
                # 位置也回退同步解码兜底一次
                self._last_render_decode_at = time.perf_counter()
                # 记录请求轨迹：sync_vt 的回跳（播放头重锚/seek 折返）依赖
                # scrubbing 判定走 coarse 关键帧快解，避免 VT seek 风暴长尾
                self._record_frame_request(frame_number)
                t0 = time.perf_counter()
                frame_data = self._decode_frame(
                    frame_number, render_mode,
                    sync_vt=bool(self.parameters.get("timeline_sync_vt", False)),
                )
                self._last_render_decode_ms = (time.perf_counter() - t0) * 1000
                if frame_data is None:
                    return False
                if isinstance(frame_data, _VtFrameData):
                    # VT 帧不进 _frame_cache，直接绑定上屏
                    if self._upload_frame_gpu_vt(ctx, frame_data):
                        self._revision += 1
                        self._current_frame = frame_number
                        self._load_state = VideoLoadState.UPLOADED_GPU
                        self._is_fallback = False
                        return True
                    return False
                with self._cache_lock:
                    # AUD-7601：同步解码结果别名渲染线程的 _rgba_buffer，
                    # 入缓存前拷贝解别名（本地上传继续用原缓冲，线程独占安全）。
                    self._frame_cache[frame_number] = self._own_cache_frame_data(frame_data)
            else:
                # 服务/源均不可用：保持旧语义（失败）
                return False

        if isinstance(frame_data, _PlanarFrameData):
            # GPU 直上：平面纹理上传 + shader YUV→RGB 转换写进 f4 FBO。
            # _gpu_texture 既是 FBO 颜色附件也是对下游输出的纹理（契约不变）。
            if not self._upload_frame_gpu(ctx, frame_data):
                return False
            self._revision += 1
            # present-latest 时记录实际上屏的帧号（而非超前于画面的请求帧
            # 号），保证后续扫描的单调性与"下一帧"判断正确。
            self._current_frame = (
                present_latest_frame if present_latest_frame is not None else frame_number
            )
            self._load_state = VideoLoadState.UPLOADED_GPU
            self._is_fallback = False
            return True

        try:
            h, w = frame_data.shape[:2]
            components = frame_data.shape[2] if frame_data.ndim == 3 else 1

            # Decoders produce CPU arrays with a top-left image origin, while
            # the composite GL pipeline samples textures with bottom-left UV
            # origin. Flip once at the CPU->GL upload boundary (same contract
            # as ImageInputNode.upload_from_cpu).
            data = np.ascontiguousarray(np.flipud(frame_data))

            # Check if size changed
            if self._gpu_texture is not None:
                if self._width != w or self._height != h:
                    self._gpu_texture.release()
                    self._gpu_texture = None
                    # Clear texture cache on size change
                    for tex in self._texture_cache.values():
                        if tex is not None:
                            tex.release()
                    self._texture_cache.clear()

            # Create texture if needed
            if self._gpu_texture is None:
                self._gpu_texture = ctx.texture(
                    (w, h), components,
                    data=data,
                    dtype='f4',
                )
                self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                self._width = w
                self._height = h

            else:
                # Update existing texture（numpy 数组直接走 buffer protocol，
                # 省一次 4K float32 ≈132MB 的 tobytes 整帧拷贝）
                self._gpu_texture.write(data)

            self._revision += 1
            # present-latest 时记录实际上屏的帧号（而非超前于画面的请求帧
            # 号），保证后续扫描的单调性与"下一帧"判断正确。
            self._current_frame = (
                present_latest_frame if present_latest_frame is not None else frame_number
            )
            self._load_state = VideoLoadState.UPLOADED_GPU
            self._is_fallback = False

            return True

        except Exception as e:
            logger.warning(f"GPU upload error: {e}")
            self._load_state = VideoLoadState.FAILED
            self.error_message = str(e)
            return False

    def _upload_frame_gpu(self, ctx: moderngl.Context, entry: _PlanarFrameData) -> bool:
        """平面帧 GPU 直上：上传 Y/U/V 平面纹理并经 shader 转换进 f4 FBO。

        与调色/剪辑页共享同一 YUVGPUUploader（含平面纹理池、r8/r16ui
        位深自适应与 BT.709/2020/601 + limited/full 转换 shader），矩阵/
        范围/位深语义与 CPU 转换路径一致（不 clamp，保留 HDR 高光）。
        仅允许渲染线程调用（GL 对象归属 CompositeRenderThread）。
        """
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        try:
            if self._yuv_uploader is None or self._yuv_uploader_ctx is not ctx:
                if self._yuv_uploader is not None:
                    self._yuv_uploader.release()
                self._yuv_uploader = YUVGPUUploader(ctx, pool_size=3)
                self._yuv_uploader_ctx = ctx
                self._gpu_fbo = None

            if not self._ensure_gpu_target(ctx, entry.width, entry.height):
                return False

            # 子采样布局由 U 平面与 Y 平面的尺寸关系决定
            uh, uw = entry.u.shape[:2]
            if uh == entry.height and uw == entry.width:
                subsampling = '444'
            elif uh == entry.height:
                subsampling = '422'
            else:
                subsampling = '420'

            self._yuv_uploader.set_yuv_matrix(entry.yuv_matrix)
            self._yuv_uploader.set_color_range(entry.color_range)
            # 单调 serial 作为纹理池 key：节点对同一帧不会重复上传（上层
            # 有 frame_number != _current_frame 守卫），池按 last_used 淘汰。
            self._yuv_upload_serial += 1
            # r16ui 直传路径的 a_plane 是 usampler2D：alpha dtype 必须与
            # 主平面一致（uint16），不一致时丢弃 alpha 而不是整帧失败。
            a_plane = entry.a
            if (
                a_plane is not None
                and self._yuv_uploader.get_upload_mode(entry.bit_depth) == 'r16ui_direct'
                and a_plane.dtype != np.uint16
            ):
                logger.debug("GPU YUV direct: alpha dtype mismatch on r16ui path, dropping alpha")
                a_plane = None
            texes = self._yuv_uploader.upload_yuv_planar(
                entry.y, entry.u, entry.v,
                self._yuv_upload_serial,
                bit_depth=entry.bit_depth,
                subsampling=subsampling,
                a=a_plane,
                alpha_bit_depth=entry.alpha_bit_depth,
            )
            # uploader 的渲染 quad 方向与本节点相反：先渲中转，再翻转 blit
            self._yuv_uploader.render_yuv_to_rgb(
                texes[0], texes[1], texes[2],
                self._gpu_mid_fbo,
                bit_depth=entry.bit_depth,
                a_tex=texes[3] if len(texes) >= 4 else None,
                alpha_bit_depth=entry.alpha_bit_depth,
                alpha_premultiplied=entry.alpha_premultiplied,
            )
            self._flip_blit(ctx)
            return True

        except Exception as e:
            logger.warning(f"GPU YUV direct upload error: {e}")
            self._load_state = VideoLoadState.FAILED
            self.error_message = str(e)
            return False

    def _ensure_gpu_target(self, ctx: moderngl.Context, w: int, h: int) -> bool:
        """确保 _gpu_texture/_gpu_fbo 与 planar 中转 FBO 就绪。渲染线程。"""
        if self._gpu_texture is not None and (self._width != w or self._height != h):
            # 尺寸变化（换源/换质量）：释放旧纹理与 FBO 重建
            # AUD-7603 fix: 旧路径直接把 _gpu_fbo/_gpu_mid_fbo 置 None 不
            # release，mid 的 f4 附件纹理随之无主泄漏（FBO 对象本身也泄漏）。
            self._gpu_texture.release()
            self._gpu_texture = None
            for old_fbo in (self._gpu_fbo, self._gpu_mid_fbo):
                if old_fbo is not None:
                    try:
                        old_fbo.release()
                    except Exception as e:
                        logger.debug(f"stale FBO release on resize failed: {e}")
            self._gpu_fbo = None
            self._gpu_mid_fbo = None
            if self._gpu_mid_tex is not None:
                try:
                    self._gpu_mid_tex.release()
                except Exception as e:
                    logger.debug(f"stale mid texture release on resize failed: {e}")
                self._gpu_mid_tex = None
            for tex in self._texture_cache.values():
                if tex is not None:
                    tex.release()
            self._texture_cache.clear()
        if self._gpu_texture is None:
            self._gpu_texture = ctx.texture((w, h), 4, dtype='f4')
            self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._width = w
            self._height = h
        if self._gpu_fbo is None:
            self._gpu_fbo = ctx.framebuffer(self._gpu_texture)
        if self._gpu_mid_fbo is None:
            mid_tex = ctx.texture((w, h), 4, dtype='f4')
            mid_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._gpu_mid_fbo = ctx.framebuffer(mid_tex)
            # AUD-7603: 附件纹理显式持有引用（Framebuffer.release 不释放附件）
            self._gpu_mid_tex = mid_tex
        return True

    _FLIP_VERT_SHADER = """
    #version 330
    in vec2 in_vert;
    in vec2 in_texcoord;
    out vec2 v_uv;
    void main() {
        gl_Position = vec4(in_vert, 0.0, 1.0);
        v_uv = in_texcoord;
    }
    """

    _FLIP_FRAG_SHADER = """
    #version 330
    uniform sampler2D u_tex;
    in vec2 v_uv;
    out vec4 f_color;
    void main() {
        // 中转 FBO 底行=图像顶行 → 翻 v 后与 flipud 上传的标准 GL 布局一致
        f_color = texture(u_tex, vec2(v_uv.x, 1.0 - v_uv.y));
    }
    """

    def _flip_blit(self, ctx: moderngl.Context) -> None:
        """把中转 FBO 内容垂直翻转 blit 进最终 _gpu_fbo。渲染线程。"""
        try:
            if self._flip_prog is None:
                self._flip_prog = ctx.program(
                    vertex_shader=self._FLIP_VERT_SHADER,
                    fragment_shader=self._FLIP_FRAG_SHADER,
                )
            if self._flip_vbo is None:
                import numpy as _np
                self._flip_vbo = ctx.buffer(_np.array([
                    -1.0, -1.0, 0.0, 0.0,
                     1.0, -1.0, 1.0, 0.0,
                     1.0,  1.0, 1.0, 1.0,
                    -1.0,  1.0, 0.0, 1.0,
                ], dtype='f4').tobytes())
                ibo = ctx.buffer(_np.array([0, 1, 2, 0, 2, 3], dtype='i4').tobytes())
                # AUD-7603: IBO 显式持有（VAO.release 不释放 index buffer）
                self._flip_ibo = ibo
                self._flip_vao = ctx.vertex_array(
                    self._flip_prog,
                    [(self._flip_vbo, '2f 2f', 'in_vert', 'in_texcoord')],
                    ibo,
                )
            mid_tex = self._gpu_mid_fbo.color_attachments[0]
            mid_tex.use(0)
            self._flip_prog['u_tex'].value = 0
            self._gpu_fbo.use()
            self._flip_vao.render(moderngl.TRIANGLES, vertices=6)
        except Exception as e:
            logger.warning(f"flip blit failed: {e}")

    def _take_vt_pending(
        self, frame_number: int
    ) -> Optional[Tuple[int, "_VtFrameData"]]:
        """取出可用的 VT 递交帧（精确命中或播放中 present-latest）。

        判定与清槽必须在同一临界区内完成：否则 worker 覆盖旧槽并释放
        其 CVPixelBuffer 的同时，渲染线程可能正把同一帧拿去绑定，
        造成 use-after-release / 双重释放。

        拖拽中（coarse 交互式解码）放宽为"新鲜即上屏"：返回的邻近关键帧
        可能落后于已显示帧（往回拖）或与请求同 GOP（同号）——播放用的
        单调前进守卫会把它们全部判为不可用，导致拖拽画面静止。
        """
        with self._vt_lock:
            pending = self._vt_pending
            if pending is None:
                return None
            p_num, _p_entry = pending
            if self._scrubbing():
                self._vt_pending = None
                return pending
            usable = (
                p_num == frame_number
                or (
                    self._requests_advancing()
                    and self._current_frame < p_num <= frame_number
                )
            )
            if not usable:
                return None
            self._vt_pending = None
            return pending

    def _kick_vt_prefetch(self, next_frame: int) -> bool:
        """播放中前瞻预取 VT 下一顺序帧（摊平间隙补齐解码）。

        worker 解码目标 N 后（游标=N+1），立即请求预取 N+1 入 reorder
        缓冲；下一轮目标 N+k 命中缓冲或只需少量顺序解码，把"按需解码
        延迟"变成后台吞吐（与剪辑页 EditGpuDecodeManager 同款策略）。
        返回 False 表示预取放弃（无 VT 源/非顺序目标），调用方据此停止
        加深预取窗口。
        """
        if self._vt_gpu_disabled or self._clip_id is None or self._video_context is None:
            return False
        try:
            return bool(self._video_context.prefetch_vt_sequential_for_node(
                clip_id=self._clip_id,
                timeline_frame=next_frame,
                source_in=self.parameters.get("source_in", 0),
                source_out=self.parameters.get("source_out", -1),
                fps=self.parameters.get("fps", 24.0),
                time_offset=self.parameters.get("time_offset", 0.0),
                speed=self.parameters.get("speed", 1.0),
            ))
        except Exception as e:
            logger.debug(f"VT sequential prefetch failed: {e}")
            return False

    @staticmethod
    def _release_vt_entry(entry: "_VtFrameData") -> None:
        """释放 VT 帧的 CVPixelBuffer（任意线程安全）。"""
        from src.shared.media.gpu_frame import safe_release_gpu_frame
        try:
            safe_release_gpu_frame(entry.gpu_frame)
        except Exception as e:
            logger.debug(f"VT entry release failed: {e}")

    @staticmethod
    def _release_vt_pinned(pinned: Optional[Tuple[Any, Any]]) -> None:
        """释放渲染线程 pin 的（桥接租约, gpu_frame）对。渲染线程。"""
        if pinned is None:
            return
        bridge_release, gpu_frame = pinned
        try:
            bridge_release.release()
        except Exception as e:
            logger.debug(f"VT bridge lease release failed: {e}")
        from src.shared.media.gpu_frame import safe_release_gpu_frame
        try:
            safe_release_gpu_frame(gpu_frame)
        except Exception as e:
            logger.debug(f"VT pinned frame release failed: {e}")

    # VT 转换 shader：移植自 EditGpuPreviewRenderer 的内联 2-plane YUV
    # shader（RECTANGLE/2D 双变体），去掉 clamp 以保留 HDR 高光（与 CPU /
    # YUVGPUUploader 路径语义一致），矩阵编号沿用其约定（0=601,1=709,2=2020）。
    _VT_VERT_SHADER = """
    #version 330
    in vec2 in_vert;
    in vec2 in_texcoord;
    out vec2 v_texcoord;
    void main() {
        gl_Position = vec4(in_vert, 0.0, 1.0);
        v_texcoord = in_texcoord;
    }
    """

    @staticmethod
    def _vt_yuv_body() -> str:
        return """
            float range_mul = u_color_range == 1 ? 1.0 : (219.0 / 255.0);
            float range_off = u_color_range == 1 ? 0.0 : (16.0 / 255.0);
            y = (y - range_off) / range_mul;
            y = max(y, 0.0);
            float c_off = u_color_range == 1 ? 0.5 : (128.0 / 255.0);
            float c_scale = u_color_range == 1 ? 1.0 : (255.0 / 224.0);
            cb = (cb - c_off) * c_scale;
            cr = (cr - c_off) * c_scale;
            float r, g, b;
            if (u_color_matrix == 0) {
                r = y + 1.402 * cr;
                g = y - 0.344136 * cb - 0.714136 * cr;
                b = y + 1.772 * cb;
            } else if (u_color_matrix == 1) {
                r = y + 1.5748 * cr;
                g = y - 0.1873 * cb - 0.4681 * cr;
                b = y + 1.8556 * cb;
            } else {
                r = y + 1.4746 * cr;
                g = y - 0.1646 * cb - 0.5714 * cr;
                b = y + 1.8814 * cb;
            }
            f_color = vec4(r, g, b, 1.0);
        """

    def _ensure_vt_program(self, ctx: moderngl.Context, use_rect: bool) -> Optional[Tuple[Any, Any]]:
        """编译/缓存 VT 转换程序与 quad VAO，返回 (program, vao)。渲染线程。"""
        key = 'rect' if use_rect else '2d'
        cached = self._vt_programs.get(key)
        if cached is not None:
            return cached

        if use_rect:
            frag = """
            #version 330
            #extension GL_ARB_texture_rectangle : enable
            uniform sampler2DRect u_y_plane;
            uniform sampler2DRect u_uv_plane;
            uniform vec2 u_y_size;
            uniform vec2 u_uv_size;
            uniform int u_color_range;
            uniform int u_color_matrix;
            uniform int u_uv_component_mode;
            in vec2 v_texcoord;
            out vec4 f_color;
            void main() {
                float y = texture(u_y_plane, v_texcoord * u_y_size).r;
                vec4 uv_sample = texture(u_uv_plane, v_texcoord * u_uv_size);
                vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
                float cb = uv.r;
                float cr = uv.g;
                BODY
            }
            """.replace('BODY', self._vt_yuv_body())
        else:
            frag = """
            #version 330
            uniform sampler2D u_y_plane;
            uniform sampler2D u_uv_plane;
            uniform int u_color_range;
            uniform int u_color_matrix;
            uniform int u_uv_component_mode;
            in vec2 v_texcoord;
            out vec4 f_color;
            void main() {
                float y = texture(u_y_plane, v_texcoord).r;
                vec4 uv_sample = texture(u_uv_plane, v_texcoord);
                vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
                float cb = uv.r;
                float cr = uv.g;
                BODY
            }
            """.replace('BODY', self._vt_yuv_body())

        try:
            prog = ctx.program(vertex_shader=self._VT_VERT_SHADER, fragment_shader=frag)
        except Exception as e:
            logger.warning(f"VT yuv shader compile failed ({key}): {e}")
            return None

        # quad 顶点（uv 的 v 与 uploader 相反：flipud 上传后的纹理是标准
        # GL 布局——v=0=图像底行；直上渲染必须把图像顶行画到 FBO 顶行
        # （v=1 侧），否则下游按标准 GL 采样会上下颠倒——真实渲染器
        # 基线对照确认原始 CPU 路径为正向）
        if self._vt_quad_vbo is None:
            import numpy as _np
            vertices = _np.array([
                # x, y, u, v
                -1.0, -1.0, 0.0, 1.0,
                 1.0, -1.0, 1.0, 1.0,
                 1.0,  1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 0.0,
            ], dtype='f4')
            self._vt_quad_vbo = ctx.buffer(vertices.tobytes())
            ibo = ctx.buffer(_np.array([0, 1, 2, 0, 2, 3], dtype='i4').tobytes())
            self._vt_programs['_ibo'] = ibo
        vao = ctx.vertex_array(
            prog,
            [(self._vt_quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')],
            self._vt_programs['_ibo'],
        )
        self._vt_programs[key] = (prog, vao)
        return prog, vao

    def _upload_frame_gpu_vt(self, ctx: moderngl.Context, entry: "_VtFrameData") -> bool:
        """VT 零拷贝帧上屏：IOSurface→外部纹理绑定 + shader 转换进 f4 FBO。

        仅渲染线程调用。绑定成功后 pin 新帧、释放上一帧的桥接租约（与
        调色页 _previous_gpu_frame 相同的 pin 模式，保证采样期间
        IOSurface 不被回收）。连续失败 ≥3 次禁用 VT 路径回退 CPU。
        """
        from src.shared.media.gpu_frame import safe_release_gpu_frame

        gpu_frame = entry.gpu_frame
        vt_frame = getattr(gpu_frame, 'deferred_vt_frame', None)
        if vt_frame is None:
            safe_release_gpu_frame(gpu_frame)
            return False

        # AUD-7604 fix: bind_pixel_buffer 成功后至 pin 前的异常路径（uniform
        # 缺失 KeyError、渲染段 GL 错误等）旧代码只释放 gpu_frame、不还桥接
        # 租约——IOSurfaceGLBridge ring_size=4，每次泄漏占一个槽位，连续 3 次
        # 即触发熔断永久禁用 VT 路径。以局部标志配对释放。
        bridge_release = None
        pinned = False
        try:
            if self._vt_bridge is None or self._vt_bridge_ctx is not ctx:
                from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge
                if self._vt_bridge is not None:
                    self._release_vt_bridge()
                bridge = IOSurfaceGLBridge(ring_size=4)
                if not bridge.initialize(planes_per_frame=2):
                    try:
                        bridge.destroy()
                    except Exception:
                        pass
                    # 与 bind 失败相同的 3 次熔断：init 失败可能是瞬时状态
                    # （如 sticky GL 错误误报），不应一次失败就永久禁用。
                    self._vt_fail_count += 1
                    if self._vt_fail_count >= 3:
                        self._vt_gpu_disabled = True
                        logger.warning(
                            "VideoFrameNode: VT bridge init failed repeatedly, "
                            "VT path disabled"
                        )
                    else:
                        logger.warning(
                            "VideoFrameNode: VT bridge init failed, "
                            f"will retry ({self._vt_fail_count}/3)"
                        )
                    safe_release_gpu_frame(gpu_frame)
                    return False
                self._vt_bridge = bridge
                self._vt_bridge_ctx = ctx

            result = self._vt_bridge.bind_pixel_buffer(
                pixel_buffer=vt_frame.pixel_buffer,
                frame_index=vt_frame.frame_index,
                pixel_format=vt_frame.pixel_format,
                bit_depth=vt_frame.bit_depth,
            )
            if result is None:
                self._vt_fail_count += 1
                if self._vt_fail_count >= 3:
                    self._vt_gpu_disabled = True
                    logger.warning(
                        "VideoFrameNode: VT bind failed repeatedly, VT path disabled "
                        f"(reason={getattr(self._vt_bridge, 'last_fallback_reason', None)})"
                    )
                safe_release_gpu_frame(gpu_frame)
                return False

            planes, bridge_release = result
            if (len(planes) < 2
                    or any(str(getattr(p, 'internal_format', '')).endswith('UI')
                           for p in planes[:2])):
                # 整数纹理（R16UI 等）需要 usampler 变体，当前不支持 → 禁用
                self._vt_gpu_disabled = True
                logger.warning(
                    "VideoFrameNode: VT planes use integer textures, "
                    f"unsupported (ifmt={[getattr(p, 'internal_format', '') for p in planes]})"
                )
                bridge_release.release()
                safe_release_gpu_frame(gpu_frame)
                return False

            w = int(gpu_frame.width or 0)
            h = int(gpu_frame.height or 0)
            if w <= 0 or h <= 0 or not self._ensure_gpu_target(ctx, w, h):
                bridge_release.release()
                safe_release_gpu_frame(gpu_frame)
                return False

            from OpenGL.GL import (
                GL_TEXTURE0, GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE,
                glActiveTexture, glBindTexture, glGetUniformLocation,
                glUniform1i, glUseProgram,
            )
            y_plane, uv_plane = planes[0], planes[1]
            use_rect = (str(getattr(y_plane, 'target', '2d')) == 'rectangle')
            pair = self._ensure_vt_program(ctx, use_rect)
            if pair is None:
                bridge_release.release()
                safe_release_gpu_frame(gpu_frame)
                return False
            prog, vao = pair

            y_target = GL_TEXTURE_RECTANGLE if use_rect else GL_TEXTURE_2D
            glUseProgram(prog.glo)
            glUniform1i(glGetUniformLocation(prog.glo, 'u_y_plane'), 0)
            glUniform1i(glGetUniformLocation(prog.glo, 'u_uv_plane'), 1)
            prog['u_color_range'].value = int(entry.color_range)
            prog['u_color_matrix'].value = int(entry.yuv_matrix)
            prog['u_uv_component_mode'].value = (
                1 if str(getattr(uv_plane, 'internal_format', '')) == 'GL_LUMINANCE_ALPHA' else 0
            )
            if use_rect:
                prog['u_y_size'].value = (float(y_plane.width), float(y_plane.height))
                prog['u_uv_size'].value = (float(uv_plane.width), float(uv_plane.height))

            glActiveTexture(GL_TEXTURE0)
            glBindTexture(y_target, y_plane.texture_id)
            glActiveTexture(GL_TEXTURE0 + 1)
            glBindTexture(y_target, uv_plane.texture_id)

            self._gpu_fbo.use()
            self._gpu_fbo.viewport = (0, 0, w, h)
            vao.render(moderngl.TRIANGLES, vertices=6)

            # pin 新帧（保持 IOSurface 存活到下一帧绑定），释放旧 pin
            prev_pinned = self._vt_pinned
            self._vt_pinned = (bridge_release, gpu_frame)
            pinned = True
            if prev_pinned is not None:
                self._release_vt_pinned(prev_pinned)
            self._vt_fail_count = 0
            return True

        except Exception as e:
            logger.warning(f"VT zero-copy upload error: {e}")
            self._vt_fail_count += 1
            if self._vt_fail_count >= 3:
                self._vt_gpu_disabled = True
            if not pinned:
                # 未 pin：租约与帧都归本路径所有，成对释放（见上方 AUD-7604
                # 注释）。已 pin 时 (bridge_release, gpu_frame) 已移交
                # _vt_pinned，由下一帧 swap / _release_vt_bridge 释放，此处
                # 再 release 会双重释放。
                if bridge_release is not None:
                    try:
                        bridge_release.release()
                    except Exception as rel_err:
                        logger.debug(f"VT bridge lease release failed: {rel_err}")
                safe_release_gpu_frame(gpu_frame)
            return False

    def _release_vt_bridge(self) -> None:
        """销毁 IOSurface 桥接（渲染线程）。"""
        if self._vt_bridge is None:
            return
        self._release_vt_pinned(self._vt_pinned)
        self._vt_pinned = None
        try:
            self._vt_bridge.destroy()
        except Exception as e:
            logger.debug(f"VT bridge destroy failed: {e}")
        self._vt_bridge = None
        self._vt_bridge_ctx = None

    def needs_upload(self) -> bool:
        """Check if frame needs to be uploaded to GPU."""
        return self._load_state in (VideoLoadState.LOADING, VideoLoadState.READY_CPU)

    # ── Fallback Texture ──

    def _upload_fallback(self, ctx: moderngl.Context) -> bool:
        """Create a placeholder texture based on missing_policy.

        Returns True if a placeholder was uploaded.
        """
        policy = self.parameters.get("missing_policy", "error")
        if policy == "error":
            return False

        size = 64

        if policy == "black":
            data = np.zeros((size, size, 4), dtype=np.float32)
            data[:, :, 3] = 1.0
        elif policy == "checker":
            data = np.zeros((size, size, 4), dtype=np.float32)
            block = 8
            for y in range(size):
                for x in range(size):
                    if ((x // block) + (y // block)) % 2 == 0:
                        data[y, x] = [0.6, 0.6, 0.6, 1.0]
                    else:
                        data[y, x] = [0.3, 0.3, 0.3, 1.0]
        else:
            return False

        data = np.ascontiguousarray(data)

        # Release old texture
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None

        self._gpu_texture = ctx.texture(
            (size, size), 4,
            data=data.tobytes(),
            dtype='f4',
        )
        self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._width = size
        self._height = size
        self._revision += 1
        self._load_state = VideoLoadState.UPLOADED_GPU
        self._is_fallback = True

        return True

    # ── Evaluation ──

    def _do_evaluate(
        self,
        inputs: Dict[str, TextureResult],
        ctx: moderngl.Context,
        fb_manager: Any,
        resource_pool: Any = None,
        render_mode: str = "preview"
    ) -> Dict[str, TextureResult]:
        """Evaluate node - upload current frame to GPU and return texture.

        P6.2: Output includes bit_depth and frame_metadata.
        P7.A: Uses render_mode to determine quality (preview -> proxy, export -> master).

        This node has no inputs - it loads video frames from disk.
        """
        # Unwrap CompositeEvalContext to get the actual ModernGL context.
        # Source nodes receive CompositeEvalContext for business-level context
        # (runtime_context, timeline_clip_gpu_provider), but GL operations
        # like ctx.texture() need the real moderngl.Context.
        from src.features.composite.core.eval_context import CompositeEvalContext
        eval_frame: Optional[int] = None
        if isinstance(ctx, CompositeEvalContext):
            # Timeline-driven playback: the evaluator writes the composition
            # playhead into ctx.frame_number every request, so the node shows
            # the frame under the playhead instead of a static frame_number.
            try:
                eval_frame = int(getattr(ctx, "frame_number", 0) or 0)
            except (TypeError, ValueError):
                eval_frame = None
            # P0-04: apply a downstream time node's frame remap (offset /
            # hold / speed) to the playhead before resolving the decode
            # frame. Without this, time_offset/frame_hold/retime would be
            # pure pass-throughs.
            eval_frame = self._apply_frame_override(ctx, eval_frame)
            ctx = ctx.gl

        file_path = self.parameters.get("file_path", "")

        # Open video if needed
        if file_path and self._load_state == VideoLoadState.MISSING:
            if not self._open_video(file_path):
                # Try fallback
                if not self._upload_fallback(ctx):
                    return {}

        # Resolve frame number (timeline-driven when an eval frame exists)
        frame_number = self._resolve_frame_number(eval_frame)

        # Pre-load surrounding frames (background task)
        # P7.A: Use _clip_id or _source to check if video is open
        if self._clip_id is not None or self._source is not None:
            self._preload_frames(frame_number, render_mode)

        # Check if frame changed
        if frame_number != self._current_frame or self._gpu_texture is None:
            # Upload new frame
            # P7.A: Use _clip_id or _source to check if video is open
            if self._clip_id is not None or self._source is not None:
                if not self.upload_frame(ctx, frame_number, render_mode):
                    # Try fallback
                    if not self._upload_fallback(ctx):
                        return {}

        # If still no texture, try fallback
        if self._gpu_texture is None:
            if not self._upload_fallback(ctx):
                return {}

        # Build output
        fmt = ImageFormat(
            width=self._width,
            height=self._height,
            name="Video Frame",
        ) if self._width > 0 and self._height > 0 else None

        bbox = BBox.full_frame(self._width, self._height) if fmt else None

        premultiplied = self.parameters.get("premultiplied", False)

        # Get color info from metadata
        color_space = self._detect_color_space()
        bit_depth = self._get_bit_depth()

        return {
            "image": TextureResult(
                texture_id=self._gpu_texture.glo,
                width=self._width,
                height=self._height,
                revision=self._revision,
                format=fmt,
                bbox=bbox,
                alpha_premultiplied=premultiplied,
                color_space=color_space,
                bit_depth=bit_depth,
                frame_metadata=self._video_metadata,  # P6.2: Pass VideoMetadata
            )
        }

    def _detect_color_space(self) -> str:
        """Infer colour space from VideoMetadata or parameters.

        Returns a color_space TAG describing the transfer function the pixel
        data actually carries (before the IDT decodes it to linear). This
        drives the IDT's choice of inverse transfer function.

        Distinct tags for sRGB vs Rec.709:
          - sRGB stills (IEC 61966-2-1) → "srgb"
          - Rec.709 video (BT.709 OETF) → "bt709"
        Both share BT.709 primaries, but the transfer curves differ (sRGB
        ~gamma 2.2 vs BT.709 ~gamma 2.4), which is exactly the image-vs-video
        contrast difference the color system must handle.

        Priority:
        1. Explicit user override (color_space != "auto")
        2. VideoMetadata color info (HDR -> linear, Rec.709 transfer -> bt709)
        3. File extension heuristic
        4. Fallback to srgb (most consumer video is effectively sRGB-range)
        """
        override = self.parameters.get("color_space", "auto")
        if override and override != "auto":
            return override

        # Use VideoMetadata if available
        if self._video_metadata:
            # The TRANSFER function determines the IDT decode curve. Tag the
            # actual transfer so the IDT applies the right inverse:
            #   Rec.709 video  → "bt709" (BT.709 inverse OETF)
            #   HDR10 (PQ)     → "pq"    (ST.2084 inverse EOTF; linear=nits)
            #   HLG            → "hlg"   (ARIB STD-B67 inverse OETF)
            transfer = self._video_metadata.color_transfer
            if transfer:
                if transfer in ('bt709', 'bt1886'):
                    return "bt709"          # Rec.709 SDR video
                elif transfer == 'smpte2084':  # PQ (HDR10)
                    return "pq"
                elif transfer == 'arib-std-b67':  # HLG (broadcast HDR)
                    return "hlg"

            # Fall back to primaries only if transfer is unknown.
            primaries = self._video_metadata.color_primaries
            if primaries:
                if primaries in ('bt709', 'smpte170m'):
                    return "bt709"          # BT.709/601 gamut, assume Rec.709 video
                elif primaries in ('bt2020', 'bt2020nc'):
                    # BT.2020 without an HDR transfer: wide gamut SDR. Tag
                    # bt709 transfer (best-effort) — gamut conversion is
                    # handled by the descriptor transform at export/preview.
                    return "bt709"

        # File extension heuristic
        path = self.parameters.get("file_path", "").lower()
        if path.endswith(".exr") or path.endswith(".hdr"):
            return ColorSpace.LINEAR

        # Default: consumer video without metadata is treated as Rec.709
        # (the broadcast/web video standard), NOT sRGB. This is the key fix:
        # a video node's default is "bt709", distinct from an image node's
        # "srgb" default.
        return "bt709"

    def _get_bit_depth(self) -> int:
        """Get bit depth from VideoMetadata or fallback.

        Returns:
            Bit depth (8, 10, 12, 16, 32).
        """
        if self._video_metadata:
            return self._video_metadata.bit_depth

        # Fallback to parameter
        return self.parameters.get("bit_depth", 8)

    def cleanup(self) -> None:
        """Release all GPU resources."""
        # 先停后台预取线程（join），避免清理期间 worker 继续解码/写缓存
        self._stop_preload_thread(join=True)
        self._close_video()

        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None

        self._clear_cache()
        self._load_state = VideoLoadState.MISSING
        self._is_fallback = False
    
    # ================================================================
    # 上下文管理器协议
    # ================================================================
    
    def __enter__(self) -> 'VideoFrameNode':
        """
        进入上下文管理器。
        
        Returns:
            self，支持 with 语句使用
        """
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """
        退出上下文管理器，确保资源释放。
        
        Args:
            exc_type: 异常类型（如果有）
            exc_val: 异常值
            exc_tb: 异常 traceback
        
        注意：
            无论是否发生异常，都会调用 cleanup() 清理资源
        """
        self.cleanup()

    # ── Serialization ──

    def to_dict(self) -> dict:
        """Serialize node to dict for save/load."""
        d = super().to_dict()
        meta = d.get("_meta") if isinstance(d.get("_meta"), dict) else {}
        meta.update({
            "fps": self.video_fps,
            "total_frames": self.total_frames,
            "video_width": self._width,
            "video_height": self._height,
            "bit_depth": self._get_bit_depth(),
            "is_hdr": self.parameters.get("is_hdr", False),
            "is_fallback": self._is_fallback,
        })
        d["_meta"] = meta
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "VideoFrameNode":
        """Deserialize node from dict."""
        node = super().from_dict(data)
        meta = data.get("_meta", {})
        # Read-only parameters will be refreshed when video opens
        node.parameters["fps"] = meta.get("fps", 0.0)
        node.parameters["total_frames"] = meta.get("total_frames", 0)
        node.parameters["video_width"] = meta.get("video_width", 0)
        node.parameters["video_height"] = meta.get("video_height", 0)
        node.parameters["bit_depth"] = meta.get("bit_depth", 8)
        node.parameters["is_hdr"] = meta.get("is_hdr", False)
        node._is_fallback = meta.get("is_fallback", False)
        return node
    
    # ================================================================
    # GC 后备释放（AUD-7605：finalizer 注册已移除）
    # ================================================================

    @staticmethod
    def _cleanup_on_gc(gc_resources: List[Any]) -> None:
        """Release resources passed in ``gc_resources``.

        AUD-7605: the weakref.finalize registration that (never) fed this
        helper was removed — the list was permanently empty, so the
        "GC 时确保 GPU 纹理和 MediaSource 释放" guarantee documented in
        __init__ never actually held. GL objects must not be released from
        the GC thread regardless (no context is current there); node
        resources are torn down via the explicit cleanup() path on the
        render thread. The helper itself is retained as a small utility
        (it correctly releases/close() whatever it is given) because the
        cleanup-on-exception contract tests reference it.
        """
        for res in gc_resources:
            try:
                if res is not None:
                    if hasattr(res, 'release'):
                        res.release()
                    elif hasattr(res, 'close'):
                        res.close()
            except Exception:
                logger.debug("GC cleanup of %r failed", res, exc_info=True)
