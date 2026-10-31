"""
FFmpeg/PyAV 媒体源实现。

提供基于 PyAV 的视频文件探测和解码功能。

## 职责

- FFmpegMediaSource: 实现 MediaSource Protocol
- 视频元数据探测（probe）
- 顺序帧解码（read_next_frame）
- 精确帧定位（seek_frame）
- GOP/Intra codec 差异处理

## 设计原则

- 实现 MediaSource Protocol 接口
- 不依赖 GStreamer/OpenCV
- 完整类型注解
- 资源安全管理（close 后释放）
- 高位深支持（10-bit/12-bit/16-bit）
"""

from __future__ import annotations

import logging
import threading
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Any, Generator, Tuple, Literal

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import queue

from .metadata import Rational, VideoMetadata
from .source import MediaSourceError, MediaOpenError, MediaDecodeError
from .frame import DecodedFrame, PlaneInfo
from .errors import SeekError
from .gop_index import GopIndex, get_or_build_gop_index, load_gop_index
from .probe import check_ffmpeg_available
from .color_metadata import (
    COLOR_PRIMARIES_MAP,  # noqa: F401 — 转口 re-export（src.shared.media.__init__ 经本模块聚合导出）
    COLOR_TRANSFER_MAP,  # noqa: F401 — 转口 re-export（同上）
    COLOR_SPACE_MAP,  # noqa: F401 — 转口 re-export（同上）
    COLOR_RANGE_MAP,  # noqa: F401 — 转口 re-export（同上）
    map_color_primaries,
    map_color_transfer,
    map_color_space,
    map_color_range,
    normalize_color_transfer,
    normalize_color_primaries,
    normalize_color_space,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 编解码器类型分类
# =============================================================================

# GOP-based codecs that require keyframe seek + decode forward
GOP_CODECS: frozenset[str] = frozenset({
    'h264', 'h265', 'hevc', 'mpeg4', 'mpeg2video', 'mpeg1video',
    'vp8', 'vp9', 'av1', 'libx264', 'libx265', 'libvpx', 'libvpx-vp9'
})

# Intra-frame codecs that support precise direct seek
INTRA_CODECS: frozenset[str] = frozenset({
    'prores', 'prores_ks', 'prores_aw', 'dnxhr', 'dnxhd',
    'dvvideo', 'dv', 'ffv1', 'utvideo', 'huffyuv', 'lagarith',
    'magicyuv', 'rawvideo', 'yuv4'
})

# P2-05: GOP codec 中 seek 路由到独立 SLICE 会话的子集。
# 实测（4K，平均 walk ~11 帧）：HEVC 上 SLICE 并行有效（单帧 80ms→13ms），
# seek 端到端 ~2.6x；H264 上 frame 线程同时赢得 walk（51ms vs 165ms）与
# 顺序吞吐（441fps vs 77fps），必须留在主会话。该表按实测数据维护，
# 新 codec 需先复测再追加。
GOP_SLICE_SEEK_CODECS: frozenset[str] = frozenset({'hevc', 'h265'})


# =============================================================================
# 像素格式位深度映射表
# =============================================================================

# 像素格式到位深度的映射
PIXEL_FORMAT_DEPTH_MAP: dict[str, int] = {
    # 8-bit formats
    'yuv420p': 8,
    'yuv422p': 8,
    'yuv444p': 8,
    'yuva420p': 8,
    'yuva422p': 8,
    'yuva444p': 8,
    'yuvj420p': 8,
    'yuvj422p': 8,
    'yuvj444p': 8,
    'rgb24': 8,
    'bgr24': 8,
    'rgba': 8,
    'bgra': 8,
    'argb': 8,
    'abgr': 8,
    'gbrp': 8,
    'gbrap': 8,
    'gray': 8,

    # 10-bit formats
    'yuv420p10le': 10,
    'yuv420p10be': 10,
    'yuv422p10le': 10,
    'yuv422p10be': 10,
    'yuv444p10le': 10,
    'yuv444p10be': 10,
    'yuv420p10': 10,
    'yuv422p10': 10,
    'yuv444p10': 10,
    'gbrp10le': 10,
    'gbrp10be': 10,
    'gbrap10le': 10,
    'gbrap10be': 10,
    'gray10le': 10,
    'gray10be': 10,

    # 10-bit YUVA formats (ProRes 4444)
    'yuva420p10le': 10,
    'yuva420p10be': 10,
    'yuva422p10le': 10,
    'yuva422p10be': 10,
    'yuva444p10le': 10,
    'yuva444p10be': 10,

    # 12-bit formats
    'yuv420p12le': 12,
    'yuv420p12be': 12,
    'yuv422p12le': 12,
    'yuv422p12be': 12,
    'yuv444p12le': 12,
    'yuv444p12be': 12,
    'yuv420p12': 12,
    'yuv422p12': 12,
    'yuv444p12': 12,
    'gbrp12le': 12,
    'gbrp12be': 12,
    'gbrap12le': 12,
    'gbrap12be': 12,
    'gray12le': 12,
    'gray12be': 12,

    # 12-bit YUVA formats (ProRes 4444 XQ)
    'yuva420p12le': 12,
    'yuva420p12be': 12,
    'yuva422p12le': 12,
    'yuva422p12be': 12,
    'yuva444p12le': 12,
    'yuva444p12be': 12,

    # 14-bit formats
    'yuv420p14le': 14,
    'yuv420p14be': 14,
    'yuv422p14le': 14,
    'yuv422p14be': 14,
    'yuv444p14le': 14,
    'yuv444p14be': 14,
    'gbrp14le': 14,
    'gbrp14be': 14,
    'gray14le': 14,
    'gray14be': 14,

    # 16-bit formats
    'yuv420p16le': 16,
    'yuv420p16be': 16,
    'yuv422p16le': 16,
    'yuv422p16be': 16,
    'yuv444p16le': 16,
    'yuv444p16be': 16,
    'gbrp16le': 16,
    'gbrp16be': 16,
    'gbrap16le': 16,
    'gbrap16be': 16,
    'gray16le': 16,
    'gray16be': 16,
    'rgb48le': 16,
    'rgb48be': 16,
    'rgba64le': 16,
    'rgba64be': 16,
    'bgr48le': 16,
    'bgr48be': 16,
    'bgra64le': 16,
    'bgra64be': 16,

    # 16-bit YUVA formats
    'yuva420p16le': 16,
    'yuva420p16be': 16,
    'yuva422p16le': 16,
    'yuva422p16be': 16,
    'yuva444p16le': 16,
    'yuva444p16be': 16,

    # 32-bit float formats
    'gbrpf32le': 32,
    'gbrpf32be': 32,
    'gbrapf32le': 32,
    'gbrapf32be': 32,
    'grayf32le': 32,
    'grayf32be': 32,

    # ProRes formats
    'prores_422': 10,
    'prores_422hq': 10,
    'prores_422proxy': 10,
    'prores_422lt': 10,
    'prores_4444': 10,      # ProRes 4444 (10-bit with alpha)
    'prores_4444xq': 12,    # ProRes 4444 XQ (12-bit with alpha)
    'prores_4444hq': 10,

    # DNxHR formats
    'dnxhr_lb': 8,
    'dnxhr_sq': 8,
    'dnxhr_hq': 8,
    'dnxhr_hqx': 10,
    'dnxhr_444': 10,
}


# =============================================================================
# 色彩映射函数别名（向后兼容）
# =============================================================================

# 使用 color_metadata 模块的函数，通过别名保持兼容
_map_color_primaries = map_color_primaries
_map_color_transfer = map_color_transfer
_map_color_space = map_color_space
_map_color_range = map_color_range


def _safe_stream_pix_fmt(stream: Any) -> str:
    """读取视频流像素格式名的安全通道。

    禁止直接读 ``stream.pix_fmt`` / ``codec_context.pix_fmt``：PyAV 17
    的 getter 对 ``AV_PIX_FMT_NONE``（文件畸形或仅按扩展名被 image2
    认领、未解析出任何帧的流）不做守卫——``av_pix_fmt_desc_get(-1)``
    返回 NULL 后直接解引用，C 层段崩溃，``hasattr``/``try/except``
    均无法拦截。``stream.format`` 是库自身的安全通道（NONE → None，
    ``VideoStream.__repr__`` 同款守卫）；真实文件上
    ``format.name`` ≡ ``pix_fmt``。
    """
    try:
        fmt = getattr(stream, "format", None)
        if fmt is not None and getattr(fmt, "name", None):
            return str(fmt.name)
    except Exception:  # noqa: BLE001 — 探测层绝不因个别属性失败中断
        pass
    return 'unknown'


def _infer_bit_depth(pix_fmt: str) -> int:
    """
    从像素格式推断位深度。

    Args:
        pix_fmt: 像素格式名称

    Returns:
        位深度（默认 8）
    """
    # 直接匹配
    if pix_fmt in PIXEL_FORMAT_DEPTH_MAP:
        return PIXEL_FORMAT_DEPTH_MAP[pix_fmt]

    # 模式匹配
    lower_fmt = pix_fmt.lower()

    # 检查后缀
    if 'p10' in lower_fmt or '10le' in lower_fmt or '10be' in lower_fmt:
        return 10
    if 'p12' in lower_fmt or '12le' in lower_fmt or '12be' in lower_fmt:
        return 12
    if 'p14' in lower_fmt or '14le' in lower_fmt or '14be' in lower_fmt:
        return 14
    if 'p16' in lower_fmt or '16le' in lower_fmt or '16be' in lower_fmt:
        return 16
    if 'f32' in lower_fmt:
        return 32
    if '48le' in lower_fmt or '48be' in lower_fmt or '64le' in lower_fmt or '64be' in lower_fmt:
        return 16

    # 默认 8-bit
    return 8


def _normalize_chroma_siting(value: Any) -> int | None:
    """Map FFmpeg chroma-location labels to Topos' 0/1/2 enum.

    FFmpeg's AVChromaLocation integers are 1=left, 2=center and 3=topleft;
    zero is unspecified. String-valued container metadata is accepted as well.
    Locations that the Topos enum cannot represent fail closed.
    """
    if value is None:
        return None
    if isinstance(value, str):
        name = value.strip().lower().replace("avchroma_loc_", "")
        name = name.replace("_", "").replace("-", "")
        return {
            "left": 0,
            "center": 1,
            "topleft": 2,
            "bottomleft": 2,
            "top": 1,
            "bottom": 1,
        }.get(name)
    try:
        return {1: 0, 2: 1, 3: 2, 4: 1, 5: 2, 6: 1}.get(int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def extract_pts_index_seconds(path: str) -> "list[float] | None":
    """F5.4/M04（S04）· 抽取视频流逐显示帧 PTS 秒表（VFR 索引构建）。

    一次顺序 demux（不解码）收集视频包 **``pts × time_base``**（显示
    时间戳），跳过缺戳包，按 pts 升序排列得到**显示帧序**（B 帧重排下
    demux 顺序 ≠ 显示顺序——此前读 ``dts`` 且按 demux 序收集，重排样例
    中 timeline 帧 0 被映射到源帧 2），再减去首帧 pts 归一（stream
    start_time / 非零首戳不污染 clip 相对映射——``map_vfr_frame`` 的
    target 从 0 秒起）。

    契约：``index[i]`` = 显示帧 i 的 PTS 相对首个显示帧的秒差。
    CFR 文件返回等差表（调用方可据此判定非 VFR 并跳过绑定）。失败返
    回 None（调用方回退 CFR 数学，不阻断导入）。
    """
    try:
        import av

        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            time_base = stream.time_base
            if time_base is None:
                return None
            pts_values: "list[float]" = []
            for packet in container.demux(stream):
                if packet.pts is None:
                    continue  # 无显示戳的包无法定位显示序
                pts_values.append(float(packet.pts * time_base))
            if not pts_values:
                return None
            pts_values.sort()
            first = pts_values[0]
            if first:
                pts_values = [t - first for t in pts_values]
            return pts_values
    except Exception:  # noqa: BLE001 — 抽取失败回退 CFR
        return None


def looks_vfr(pts_seconds: "list[float] | None") -> bool:
    """PTS 秒表是否为变帧率（相邻差不齐 > 半帧容差）。"""
    if not pts_seconds or len(pts_seconds) < 3:
        return False
    deltas = [b - a for a, b in zip(pts_seconds, pts_seconds[1:])]
    if any(d <= 0 for d in deltas):
        return True  # 时间戳异常按 VFR 处理（走索引不走 CFR 数学）
    mean = sum(deltas) / len(deltas)
    return any(abs(d - mean) > mean * 0.25 for d in deltas)


def _infer_field_order(field_order: str | None) -> str | None:
    """
    推断场序。

    Args:
        field_order: FFmpeg field_order 字符串

    Returns:
        'progressive', 'top', 'bottom' 或 None
    """
    if field_order is None:
        return None

    lower = field_order.lower()
    if lower in ('progressive', 'prog', 'unknown'):
        return 'progressive'
    if lower in ('tt', 'top', 'top-first', 'tff'):
        return 'top'
    if lower in ('bb', 'bottom', 'bottom-first', 'bff'):
        return 'bottom'

    return None


def _is_gop_codec(codec_name: str) -> bool:
    """
    判断是否为 GOP 编解码器（需要 keyframe seek + decode forward）。

    Args:
        codec_name: 编解码器名称

    Returns:
        GOP 编解码器返回 True
    """
    name_lower = codec_name.lower()
    return name_lower in GOP_CODECS or any(
        c in name_lower for c in ('h264', 'h265', 'hevc', 'vp9', 'av1')
    )


def _is_intra_codec(codec_name: str) -> bool:
    """
    判断是否为 Intra-frame 编解码器（可直接 seek）。

    Args:
        codec_name: 编解码器名称

    Returns:
        Intra 编解码器返回 True
    """
    name_lower = codec_name.lower()
    return name_lower in INTRA_CODECS or any(
        c in name_lower for c in ('prores', 'dnx', 'raw', 'ffv1')
    )


def _is_yuv_pixel_format(pix_fmt: str) -> bool:
    """
    判断像素格式是否为 YUV 类型（包括 YUV/YUVA/NV12/P010 等）。

    用于判断是否应该走 planar 输出路径。

    Args:
        pix_fmt: 像素格式名称（如 'yuv420p', 'yuv422p10le', 'nv12', 'p010le'）

    Returns:
        YUV 格式返回 True，RGB 格式返回 False
    """
    pix_fmt_lower = pix_fmt.lower()

    # YUV planar formats
    if pix_fmt_lower.startswith('yuv') or pix_fmt_lower.startswith('yuva'):
        return True

    # NV12/P010 semiplanar formats (Y plane + interleaved UV)
    if pix_fmt_lower in ('nv12', 'nv21', 'p010le', 'p010be', 'p016le', 'p016be'):
        return True

    # YUV 422 semiplanar formats
    if pix_fmt_lower in ('nv16', 'nv24', 'p210le', 'p210be', 'p216le', 'p216be'):
        return True

    return False


# AUD-34704：半平面（semiplanar）格式布局表。
# 这些格式在内存中只有 2 个缓冲平面：Y 平面 + 交织的 chroma 平面。
# 通用 planar 提取路径会把交织 chroma 当作单一分量切片（色度错乱）
# 且永远缺少 V 平面，因此必须走显式去交织提取。
# 值为 (chroma_width_divisor, chroma_height_divisor, vu_order)：
# - nv12/nv21/p010/p016 为 4:2:0（chroma = w/2 × h/2）
# - nv16/p210/p216 为 4:2:2（chroma = w/2 × h）
# - nv24/nv42 为 4:4:4（chroma = w × h）
# - vu_order=True 表示交织顺序为 VU（NV21/NV42），否则为 UV
_SEMIPLANAR_LAYOUTS: dict[str, tuple[int, int, bool]] = {
    'nv12': (2, 2, False),
    'nv21': (2, 2, True),
    'p010le': (2, 2, False),
    'p010be': (2, 2, False),
    'p016le': (2, 2, False),
    'p016be': (2, 2, False),
    'nv16': (2, 1, False),
    'p210le': (2, 1, False),
    'p210be': (2, 1, False),
    'p216le': (2, 1, False),
    'p216be': (2, 1, False),
    'nv24': (1, 1, False),
    'nv42': (1, 1, True),
}


# AUD-34703：packed RGB 像素格式名前缀（rgb24/rgba/rgb48le/bgr48le 等）。
# planar 输出路径用其做布局一致性防御：这些格式只有一个交错平面，
# 按平面切片只会得到每行前 1/N 样本的错乱数据。
_PACKED_RGB_PREFIXES = ('rgb', 'bgr', 'argb', 'abgr')


# Output mode type for FFmpegMediaSource
OutputMode = Literal["packed_rgb", "planar_yuv"]


# Q01/F1.1：GC/并发上下文中跳过关闭的容器停靠处。faulthandler 两次实证：
# PyAV container.close() 与并发 libav 使用互锁期间不释放 GIL，任何 join
# 等待都会把主线程一并冻死。__del__ 上下文因此完全不做容器关闭——引用
# 停靠在此防止 PyAV dealloc 再次触发 close；由进程退出统一回收。容量上
# 限防止病态场景无限累积（超过即丢弃最老引用，接受其 dealloc 风险）。
_DEFERRED_CONTAINERS: "list[Any]" = []
_DEFERRED_CONTAINERS_CAP = 32

# Q01/F1.1：单例常驻关闭线程 + 队列。三次转储实证 av 容器 close 与并发
# 解码在 libav↔GIL 间锁序反转（close 持 GIL 等 libav；解码持 libav 等
# GIL），期间主线程无论 join 还是 Thread.start() 都会被连带冻死。主线程
# 只做 queue.put（纯 C、不释放 GIL），关闭全部由本常驻守护线程串行执
# 行；该线程若被楔死，后续容器停留在队列中随进程退出回收，主线程永不
# 阻塞。
_CLOSE_QUEUE: "queue.Queue" = None  # type: ignore[assignment]
_CLOSER_STARTED = False
_CLOSER_START_LOCK = threading.Lock()


def _ensure_closer_thread() -> "queue.Queue":
    global _CLOSE_QUEUE, _CLOSER_STARTED
    with _CLOSER_START_LOCK:
        if not _CLOSER_STARTED:
            import queue as _queue

            _CLOSE_QUEUE = _queue.Queue()

            def _closer_loop() -> None:
                while True:
                    container = _CLOSE_QUEUE.get()
                    # Q01/F1.2 根治：只在全局解码空闲窗口关容器——打断
                    # libav↔GIL 锁序反转（close 持 GIL 等 libav 锁、解码持
                    # libav 锁等 GIL）。持续播放时容器停留队列等待。
                    from src.shared.media.av_concurrency import wait_for_idle
                    wait_for_idle()
                    try:
                        container.close()
                    except Exception:  # noqa: BLE001 - 关闭失败仅丢弃
                        pass

            threading.Thread(
                target=_closer_loop, daemon=True, name="ffsrc-closer").start()
            _CLOSER_STARTED = True
        return _CLOSE_QUEUE


def _close_container_bounded(container, timeout: float, *, wait: bool = True) -> bool:
    """Q01/F1.1：容器关闭入队常驻守护线程，调用方永不阻塞、永不 spawn。

    返回 False 表示已交由常驻关闭线程/进程退出收尾（恒异步）。Windows
    注意：close() 返回后文件句柄可能仍短暂打开（POSIX 无碍；Windows 文件
    锁行为由 B 线真机验证，见 B0.2/C0.1）。
    """
    _ = timeout, wait
    _ensure_closer_thread().put(container)
    return False


def _defer_container(container) -> None:
    _DEFERRED_CONTAINERS.append(container)
    while len(_DEFERRED_CONTAINERS) > _DEFERRED_CONTAINERS_CAP:
        _DEFERRED_CONTAINERS.pop(0)


class FFmpegMediaSource:
    """
    FFmpeg/PyAV 媒体源实现。

    实现 MediaSource Protocol，提供视频文件探测和解码功能。

    支持精确帧定位，自动处理 GOP 和 Intra 编解码器的差异。
    支持高位深像素格式（10-bit/12-bit/16-bit）。
    支持 target_size 降分辨率（用于 proxy 播放）。
    支持 planar YUV 输出模式（避免 CPU 端 RGB 转换）。

    Attributes:
        path: 媒体文件路径
        is_open: 是否已打开
        current_frame: 当前帧号
        metadata: 视频元数据
        target_size: 目标分辨率（用于 proxy）
        preserve_planes: 是否保留 planar 格式（避免 RGB 转换）
        output_mode: 输出模式（"packed_rgb" 或 "planar_yuv"）

    Examples:
        >>> source = FFmpegMediaSource('/path/to/video.mp4')
        >>> metadata = source.probe()
        >>> with source:
        ...     frame = source.read_frame(100)
        ...     source.seek_frame(50)
        ...     frame = source.read_next_frame()

        >>> # Proxy 模式（降分辨率）
        >>> proxy_source = FFmpegMediaSource('/path/to/4k.mp4', target_size=(1920, 1080))
        >>> with proxy_source:
        ...     proxy_frame = proxy_source.read_next_frame()
        ...     print(proxy_frame.width, proxy_frame.height)  # 1920, 1080
        ...     print(proxy_frame.is_proxy)  # True

        >>> # Planar YUV 模式（避免 CPU RGB 转换）
        >>> planar_source = FFmpegMediaSource('/path/to/yuv420p.mp4', preserve_planes=True)
        >>> with planar_source:
        ...     frame = planar_source.read_next_frame()
        ...     print(frame.is_planar)  # True
        ...     print(frame.pixel_format)  # yuv420p
    """

    def __init__(
        self,
        path: str,
        target_size: Optional[Tuple[int, int]] = None,
        preserve_planes: bool = False,
        output_mode: OutputMode = "packed_rgb",
        output_format: str | None = None,
    ) -> None:
        """
        初始化媒体源。

        Args:
            path: 媒体文件路径
            target_size: 目标分辨率 (width, height)，用于 proxy 降分辨率。
                         None 表示保持原始分辨率（master path）。
            preserve_planes: 是否保留 planar 格式。当 True 且源像素格式是 YUV 时，
                             read_next_frame() 输出 planar DecodedFrame，不进行 CPU RGB 转换。
            output_mode: 输出模式。"packed_rgb" 表示输出打包 RGB 格式，
                         "planar_yuv" 表示输出 planar YUV 格式（当源是 YUV 时）。
            output_format: 可选的强制打包输出格式（当前只允许 ``rgb24`` 或
                           ``rgba``）。用于 CPU-only 消费者要求可靠 8-bit RGB
                           转换时；默认 ``None`` 保持原有高位深输出策略。
        """
        self._path: str = path
        self._target_size: Optional[Tuple[int, int]] = target_size
        self._preserve_planes: bool = preserve_planes
        self._output_mode: OutputMode = output_mode
        if output_format not in (None, "rgb24", "rgba"):
            raise ValueError(f"Unsupported forced output format: {output_format!r}")
        self._output_format: str | None = output_format
        self._original_size: Optional[Tuple[int, int]] = None  # 原始分辨率（从 probe 获取）
        self._container: Optional[Any] = None  # av.container.InputContainer
        self._video_stream: Optional[Any] = None  # av.video.stream.VideoStream
        self._metadata: Optional[VideoMetadata] = None
        self._is_open: bool = False
        self._current_frame: int = 0
        self._frame_iterator: Optional[Generator[Any, None, None]] = None
        self._decoder_buffer: list[Any] = []  # 解码缓冲区（用于精确 seek）
        self._pending_av_frame: Optional[Any] = None  # seek 后缓存的 PyAV 目标帧

        # CPU 解码路径应禁用 hwaccel，防止 FFmpeg 8.1+ 在 macOS 上
        # 自动将 ProRes 等路由到 VideoToolbox 产出 GPU surface 帧。
        # FFmpegHwaccelSource 子类会将此设为 False 以启用硬件解码。
        self._disable_hwaccel: bool = True

        # Planar 模式状态跟踪
        self._is_planar_mode: bool = False  # 是否当前处于 planar 输出模式

        # P2-05: GOP 持久化索引（懒加载；未就绪时 seek 走原路径）
        self._gop_index: Optional[GopIndex] = None
        self._gop_index_attempted: bool = False
        self._gop_index_thread: Optional[threading.Thread] = None
        # Q01/F1.2 run6：同实例 av 操作串行化（libav 上下文非线程安全，
        # 并发访问会楔死且不释放 GIL；见 av_concurrency.av_locked_operation）。
        self._av_lock = threading.RLock()

        # P2-05: HEVC 类 GOP codec 的 seek 专用轻量会话（SLICE 线程）。
        # 实测（4K HEVC 10-bit，复用 session，平均 walk ~11 帧）：
        #   SLICE+auto: container.seek 0ms + walk p50 146ms
        #   AUTO(frame 线程): container.seek 排空 ~122ms + walk p50 251ms
        # frame 线程的管线排空税与短 walk 的填充延迟让主会话 seek 慢 ~2.6x；
        # 但顺序播放 frame 线程仍最优（4K H264 441fps vs 77fps），故主会话
        # 线程策略不变，仅 HEVC 类 seek 路由到独立 SLICE 会话。
        self._gop_seek_container: Optional[Any] = None
        self._gop_seek_stream: Optional[Any] = None
        self._gop_seek_iterator: Optional[Generator[Any, None, None]] = None
        # seek 会话接管期间，主会话需要回同步到的 pts（最后已服务帧）
        self._main_resync_pts: Optional[int] = None

    @property
    def path(self) -> str:
        """媒体源路径"""
        return self._path

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._is_open

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        return self._current_frame

    @property
    def target_size(self) -> Optional[Tuple[int, int]]:
        """目标分辨率（用于 proxy）"""
        return self._target_size

    @property
    def original_size(self) -> Optional[Tuple[int, int]]:
        """原始分辨率"""
        return self._original_size

    @property
    def preserve_planes(self) -> bool:
        """是否保留 planar 格式"""
        return self._preserve_planes

    @property
    def output_mode(self) -> OutputMode:
        """输出模式"""
        return self._output_mode

    @property
    def is_planar_mode(self) -> bool:
        """是否当前处于 planar 输出模式（打开后确定）"""
        return self._is_planar_mode

    def set_preserve_planes(self, value: bool) -> None:
        """
        设置是否保留 planar 格式。

        Args:
            value: True 表示保留 planar 格式
        """
        self._preserve_planes = value
        # 更新 planar 模式状态（如果已打开）
        if self._is_open and self._metadata is not None:
            self._is_planar_mode = value and _is_yuv_pixel_format(self._metadata.pixel_format)

    def set_output_mode(self, mode: OutputMode) -> None:
        """
        设置输出模式。

        Args:
            mode: "packed_rgb" 或 "planar_yuv"
        """
        self._output_mode = mode
        # preserve_planes=True 且 output_mode="planar_yuv" 且源是 YUV 时启用 planar
        if self._is_open and self._metadata is not None:
            self._is_planar_mode = (
                (self._preserve_planes or mode == "planar_yuv")
                and _is_yuv_pixel_format(self._metadata.pixel_format)
            )

    def probe(self) -> VideoMetadata:
        """
        探测视频文件元数据。

        使用 PyAV 打开文件并读取所有视频流元数据。

        Returns:
            VideoMetadata 实例

        Raises:
            MediaSourceError: 探测失败
        """
        if self._metadata is not None:
            return self._metadata

        if not check_ffmpeg_available():
            raise MediaSourceError(
                "PyAV/FFmpeg is not available",
                source_path=self._path
            )

        try:
            import av
        except ImportError:
            raise MediaSourceError(
                "PyAV is not installed",
                source_path=self._path
            )

        try:
            # 打开容器（不保持打开状态，仅探测）
            container = av.open(self._path)

            try:
                # 查找最佳视频流
                video_stream = None
                for stream in container.streams:
                    if stream.type == 'video':
                        video_stream = stream
                        break

                if video_stream is None:
                    raise MediaSourceError(
                        "No video stream found",
                        source_path=self._path
                    )

                # 提取基础元数据
                width = video_stream.width
                height = video_stream.height

                # 帧率
                fps = self._extract_fps(video_stream, container)

                # 总帧数
                total_frames = self._extract_total_frames(video_stream, container)

                # 编解码器
                codec_name = video_stream.codec.name if video_stream.codec else 'unknown'
                codec_profile = self._extract_profile(video_stream)

                # 像素格式（_safe_stream_pix_fmt：禁止直读 pix_fmt，
                # PyAV 17 对 AV_PIX_FMT_NONE 的 getter 是段崩溃，见其定义处）
                pix_fmt = _safe_stream_pix_fmt(video_stream)

                # 位深度（从像素格式推断）
                bit_depth = _infer_bit_depth(pix_fmt)

                # 色彩元数据（使用 normalize 函数兼容多种输入类型）
                color_primaries = normalize_color_primaries(
                    self._get_stream_property(video_stream, 'color_primaries')
                )
                color_transfer = normalize_color_transfer(
                    self._get_stream_property(video_stream, 'color_trc')
                )
                color_space = normalize_color_space(
                    self._get_stream_property(video_stream, 'colorspace')
                )
                color_range = _map_color_range(
                    self._get_stream_property(video_stream, 'color_range')
                )

                # SAR（像素宽高比）
                sar = self._extract_sar(video_stream)

                # 场序
                field_order = _infer_field_order(
                    self._get_stream_property(video_stream, 'field_order', as_str=True)
                )

                # 时间码
                timecode = self._extract_timecode(video_stream, container)

                # 是否有 Alpha
                has_alpha = self._check_alpha(pix_fmt, video_stream)

                # 旋转（从 side data 或元数据）
                rotation = self._extract_rotation(video_stream, container)

                # 创建元数据对象
                self._metadata = VideoMetadata(
                    width=width,
                    height=height,
                    fps=fps,
                    total_frames=total_frames,
                    codec_name=codec_name,
                    codec_profile=codec_profile,
                    pixel_format=pix_fmt,
                    bit_depth=bit_depth,
                    color_primaries=color_primaries,
                    color_transfer=color_transfer,
                    color_matrix=color_space,
                    color_range=color_range,
                    sample_aspect_ratio=sar,
                    timecode=timecode,
                    has_alpha=has_alpha,
                    field_order=field_order,
                    rotation=rotation,
                )

                # P3.B: 保存原始分辨率（用于 proxy metadata）
                self._original_size = (width, height)

                logger.debug(
                    f"Probed video: {width}x{height}, {fps}, "
                    f"codec={codec_name}, profile={codec_profile}, "
                    f"pix_fmt={pix_fmt}, bit_depth={bit_depth}, "
                    f"color=({color_primaries}, {color_transfer}, {color_space})"
                )

                return self._metadata

            finally:
                # 确保容器关闭
                container.close()

        except av.error.FileNotFoundError:
            raise MediaSourceError(
                f"File not found: {self._path}",
                source_path=self._path
            )
        except av.error.InvalidDataError as e:
            raise MediaSourceError(
                f"Invalid or corrupted file: {e}",
                source_path=self._path
            )
        except Exception as e:
            logger.exception(f"Probe failed for {self._path}")
            raise MediaSourceError(
                f"Failed to probe video: {e}",
                source_path=self._path
            ) from e

    def _extract_fps(self, stream: Any, container: Any) -> Rational:
        """
        提取帧率。

        优先使用 avg_frame_rate，其次使用 r_frame_rate。

        Args:
            stream: 视频流对象
            container: 容器对象

        Returns:
            Rational 帧率
        """

        # 尝试 avg_frame_rate
        if hasattr(stream, 'average_rate') and stream.average_rate:
            rate = stream.average_rate
            if rate and rate.denominator > 0:
                return Rational(rate.numerator, rate.denominator)

        # 尝试 r_frame_rate
        if hasattr(stream, 'base_rate') and stream.base_rate:
            rate = stream.base_rate
            if rate and rate.denominator > 0:
                return Rational(rate.numerator, rate.denominator)

        # 从 time_base 和 duration 估算
        if stream.duration and stream.time_base:
            duration_sec = float(stream.duration * stream.time_base)
            if stream.frames and stream.frames > 0:
                fps_float = stream.frames / duration_sec
                return Rational.from_float(fps_float)

        # 默认 24fps
        logger.warning("Could not determine frame rate, using default 24fps")
        return Rational(24, 1)

    def _extract_total_frames(self, stream: Any, container: Any) -> int | None:
        """
        提取总帧数。

        Args:
            stream: 视频流对象
            container: 容器对象

        Returns:
            总帧数或 None
        """
        # 直接从 stream.frames 获取
        if hasattr(stream, 'frames') and stream.frames:
            return stream.frames

        # 从 duration 计算
        if stream.duration and stream.time_base:
            # AUD-34700：stream.duration 是 time_base 单位的 PTS 时长，
            # 秒数 = duration_pts * time_base。旧公式把 time_base 当除数
            # （÷ time_base），帧数虚高约 1/tb² 倍（如 tb=1/1000 虚高 1e6 倍）。
            duration_sec = float(stream.duration) * float(stream.time_base)
            fps = self._extract_fps(stream, container)
            total_frames = int(duration_sec * fps.num / fps.den)
            return total_frames if total_frames > 0 else None

        return None

    def _extract_profile(self, stream: Any) -> str | None:
        """
        提取编码器 profile。

        Args:
            stream: 视频流对象

        Returns:
            profile 名称或 None
        """
        profile = self._get_stream_property(stream, 'profile', as_str=True)
        if profile:
            return profile

        # 从 codec 获取
        if hasattr(stream, 'codec') and stream.codec:
            if hasattr(stream.codec, 'profile'):
                profile_val = stream.codec.profile
                if profile_val:
                    return str(profile_val)

        return None

    def _extract_sar(self, stream: Any) -> Rational | None:
        """
        提取像素宽高比（SAR）。

        Args:
            stream: 视频流对象

        Returns:
            SAR 或 None
        """
        # sample_aspect_ratio
        sar = self._get_stream_property(stream, 'sample_aspect_ratio')
        if sar:
            if hasattr(sar, 'numerator') and hasattr(sar, 'denominator'):
                if sar.denominator > 0:
                    return Rational(sar.numerator, sar.denominator)

        # display_aspect_ratio 反推
        dar = self._get_stream_property(stream, 'display_aspect_ratio')
        if dar and hasattr(dar, 'numerator') and hasattr(dar, 'denominator'):
            if dar.denominator > 0 and stream.width and stream.height:
                # DAR = width * SAR / height
                # SAR = DAR * height / width
                sar_num = dar.numerator * stream.height
                sar_den = dar.denominator * stream.width
                from math import gcd
                g = gcd(sar_num, sar_den)
                return Rational(sar_num // g, sar_den // g)

        return None

    def _extract_timecode(self, stream: Any, container: Any) -> str | None:
        """
        提取起始时间码。

        Args:
            stream: 视频流对象
            container: 容器对象

        Returns:
            时间码字符串或 None
        """
        # 从 stream metadata 获取
        if hasattr(stream, 'metadata') and stream.metadata:
            for key in ('timecode', 'TIMECODE', 'time_code'):
                if key in stream.metadata:
                    return stream.metadata[key]

        # 从 container metadata 获取
        if hasattr(container, 'metadata') and container.metadata:
            for key in ('timecode', 'TIMECODE', 'time_code'):
                if key in container.metadata:
                    return container.metadata[key]

        return None

    def _check_alpha(self, pix_fmt: str, stream: Any) -> bool:
        """
        检查是否有 Alpha 通道。

        Args:
            pix_fmt: 像素格式
            stream: 视频流对象

        Returns:
            是否有 Alpha
        """
        # 从像素格式推断
        lower_fmt = pix_fmt.lower()
        if 'a' in lower_fmt and ('yuva' in lower_fmt or 'gbrap' in lower_fmt or
                                 'rgba' in lower_fmt or 'bgra' in lower_fmt or
                                 'argb' in lower_fmt or 'abgr' in lower_fmt):
            return True

        # 从 codec 检查
        if hasattr(stream, 'codec') and stream.codec:
            codec_name = getattr(stream.codec, 'name', '').lower()
            if '4444' in codec_name:
                return True  # ProRes 4444

        return False

    def _extract_rotation(self, stream: Any, container: Any) -> int:
        """
        提取旋转角度。

        Args:
            stream: 视频流对象
            container: 容器对象

        Returns:
            旋转角度（0, 90, 180, 270）
        """
        # 从 stream metadata 获取
        if hasattr(stream, 'metadata') and stream.metadata:
            rotation = stream.metadata.get('rotation')
            if rotation:
                try:
                    angle = int(float(rotation))
                    # 规范化为标准角度
                    return {0: 0, 90: 90, 180: 180, 270: 270, -90: 270,
                            -180: 180, -270: 90}.get(angle, 0)
                except (ValueError, TypeError):
                    pass

        # 从 container metadata 获取
        if hasattr(container, 'metadata') and container.metadata:
            rotation = container.metadata.get('rotation')
            if rotation:
                try:
                    angle = int(float(rotation))
                    return {0: 0, 90: 90, 180: 180, 270: 270, -90: 270,
                            -180: 180, -270: 90}.get(angle, 0)
                except (ValueError, TypeError):
                    pass

        return 0

    def _get_stream_property(
        self,
        stream: Any,
        name: str,
        as_str: bool = False
    ) -> int | str | None:
        """
        安全获取流属性。

        Args:
            stream: 视频流对象
            name: 属性名称
            as_str: 是否返回字符串

        Returns:
            属性值或 None
        """
        # 直接属性
        if hasattr(stream, name):
            value = getattr(stream, name)
            if value is not None:
                if as_str:
                    return str(value)
                return value

        # 从 codec 获取
        if hasattr(stream, 'codec') and stream.codec:
            if hasattr(stream.codec, name):
                value = getattr(stream.codec, name)
                if value is not None:
                    if as_str:
                        return str(value)
                    return value

        # 从 context 获取
        if hasattr(stream, 'codec_context') and stream.codec_context:
            if hasattr(stream.codec_context, name):
                value = getattr(stream.codec_context, name)
                if value is not None:
                    if as_str:
                        return str(value)
                    return value

        return None

    def open(self) -> None:
        """
        打开媒体文件。

        初始化解码器和相关资源。

        Raises:
            MediaOpenError: 打开失败
        """
        if self._is_open:
            return

        if not check_ffmpeg_available():
            raise MediaOpenError(
                "PyAV/FFmpeg is not available",
                source_path=self._path
            )

        try:
            import av
        except ImportError:
            raise MediaOpenError(
                "PyAV is not installed",
                source_path=self._path
            )

        try:
            # 打开容器
            self._container = av.open(self._path)

            # 查找最佳视频流
            video_streams = self._container.streams.video
            if not video_streams:
                raise MediaOpenError(
                    "No video stream found",
                    source_path=self._path
                )

            self._video_stream = video_streams[0]
            # 线程模型按编码类型选择（实测 4K DNxHR，i9-9900K）：
            # - intra 编码（DNxHR/ProRes/FFV1）：纯 SLICE 线程。AUTO 的帧级
            #   线程会让 container.seek 等待管线排空（~22ms）、flush 后首帧
            #   重建管线（~54ms），随机定位周期 75ms；SLICE 无此惩罚，seek 后
            #   首帧仅 ~5.4ms（184fps）。intra 帧级线程无 GOP 可流水，本就
            #   无吞吐收益。
            # - GOP 编码（H.264/H.265 等）：保持 AUTO，帧线程对顺序播放
            #   的长 GOP walk 仍有吞吐价值。
            try:
                stream_codec = str(
                    getattr(self._video_stream.codec_context, 'name', '') or ''
                ).lower()
                if _is_intra_codec(stream_codec):
                    self._video_stream.thread_type = 'SLICE'
                else:
                    self._video_stream.thread_type = 'AUTO'
            except Exception:
                self._video_stream.thread_type = 'AUTO'
            # P1-3: 设置线程数为 0（自动选择），配合 thread_type = AUTO
            # 让 FFmpeg/PyAV 根据 codec 和系统自动决定最优线程数
            # 对于 ProRes/DNxHR 等 Intra codec，会使用多线程解码
            # 对于 H.264/H.265 等 GOP codec，硬解时此设置被忽略
            if hasattr(self._video_stream, 'codec_context') and self._video_stream.codec_context:
                self._video_stream.codec_context.thread_count = 0  # 自动选择
                # CPU 路径应显式禁用 hwaccel，防止 FFmpeg 8.1+ 在 macOS 上
                # 自动将 ProRes 等路由到 VideoToolbox 产出 GPU surface 帧。
                # FFmpegHwaccelSource 子类设 _disable_hwaccel=False，不走此分支。
                if self._disable_hwaccel:
                    if hasattr(self._video_stream.codec_context, 'options'):
                        self._video_stream.codec_context.options = {
                            **(self._video_stream.codec_context.options or {}),
                            'hwaccel': 'none',
                        }
                    elif hasattr(self._video_stream.codec_context, 'hwaccel'):
                        self._video_stream.codec_context.hwaccel = 'none'
                    elif hasattr(self._video_stream.codec_context, 'hw_device_type'):
                        self._video_stream.codec_context.hw_device_type = 'none'

            # 探测元数据（如果尚未探测）
            if self._metadata is None:
                self._metadata = self._extract_metadata_from_stream()

            self._is_open = True
            self._current_frame = 0
            self._frame_iterator = None
            self._decoder_buffer = []
            self._pending_av_frame = None

            # P3.B: 保存原始分辨率（如果尚未探测）
            if self._original_size is None:
                self._original_size = (self._metadata.width, self._metadata.height)

            # Planar 模式初始化：根据配置和源像素格式决定是否走 planar 路径
            source_pix_fmt = self._metadata.pixel_format
            self._is_planar_mode = (
                (self._preserve_planes or self._output_mode == "planar_yuv")
                and _is_yuv_pixel_format(source_pix_fmt)
            )

            logger.debug(
                f"Opened media source: {self._path}, "
                f"{self._metadata.width}x{self._metadata.height}, "
                f"{self._metadata.fps.to_fps():.3f}fps, codec={self._metadata.codec_name}, "
                f"target_size={self._target_size}, "
                f"pix_fmt={source_pix_fmt}, planar_mode={self._is_planar_mode}"
            )

        except av.error.FileNotFoundError:
            raise MediaOpenError(
                f"File not found: {self._path}",
                source_path=self._path
            )
        except Exception as e:
            suffix = Path(self._path).suffix.lower()
            missing_raw_decoder = (
                (suffix == '.braw' and isinstance(e, AttributeError)
                 and 'width' in str(e))
                or (suffix == '.r3d' and isinstance(e, av.error.InvalidDataError))
            )
            # 容器可能已打开，元数据探测却失败（例如 BRAW 无解码器）；
            # 失败路径也必须释放句柄，不能等对象析构。
            self.close()
            if missing_raw_decoder:
                raise MediaOpenError(
                    f"当前 TOPOS 的 FFmpeg/PyAV 后端无法解码 {suffix}；"
                    "需先用支持该格式的工具转换，原生 RAW 导入仍待专用解码后端",
                    source_path=self._path,
                ) from e
            logger.exception(f"Failed to open media: {self._path}")
            raise MediaOpenError(
                f"Failed to open media: {e}",
                source_path=self._path
            ) from e

    def close(self, close_timeout: float = 3.0, *, from_gc: bool = False) -> None:
        """
        关闭并释放资源。

        Args:
            close_timeout: 容器关闭的限时（秒）。Q01/F1.1：容器 close 可能与
                并发 ffmpeg 使用在 C 层互锁（期间不释放 GIL），超时后放弃等
                待、由后台守护线程/进程退出回收，调用方有界返回。
            from_gc: ``__del__`` 上下文必须传 True——GC 可能发生在任意线程
                的任意字节码边界，此处**完全不做容器关闭**，容器停靠到模块
                级注册表防止 PyAV dealloc 再触发互锁 close，由进程退出回收。
        """
        # Q01/F1.2：尽量取实例锁再清理（同实例解码串行化）；楔死持有者
        # 场景下超时让行——引用清理容忍并发，容器关闭已在空闲窗口执行。
        av_lock = getattr(self, "_av_lock", None)
        lock_acquired = av_lock is not None and av_lock.acquire(timeout=2.0)
        # AUD-34709：等待后台 GOP 索引线程退出（短超时兜底——线程内
        # 构建完成前会检查 _is_open，超时后写回路径自动跳过），避免
        # 对象关闭后线程仍写 _gop_index / 落盘缓存的生命周期边界模糊。
        # getattr 防御：__init__ 可能在 _gop_index_thread 初始化（见
        # output_format 校验的提前 raise）之前失败，部分构造对象经
        # __del__ 走到本方法时属性尚不存在（TestAud36201 回归暴露）。
        gop_thread = getattr(self, "_gop_index_thread", None)
        if gop_thread is not None and gop_thread.is_alive():
            gop_thread.join(timeout=2.0)
            self._gop_index_thread = None

        # 清理迭代器和缓冲区
        self._frame_iterator = None
        self._decoder_buffer = []
        self._pending_av_frame = None
        self._gop_seek_iterator = None
        self._main_resync_pts = None

        if self._container is not None:
            container, self._container = self._container, None
            self._video_stream = None
            if from_gc:
                _defer_container(container)
                logger.debug(
                    "FFmpegSource.close(from_gc): container deferred to process exit "
                    "(Q01 GIL-freeze guard)")
            elif not _close_container_bounded(container, close_timeout):
                logger.debug(
                    "FFmpegSource.close: container close dispatched to daemon thread "
                    "(Q01 async-close; concurrent ffmpeg use safe)")

        self._close_gop_seek_session(from_gc=from_gc)
        self._is_open = False
        self._current_frame = 0
        logger.debug(f"Closed media source: {self._path}")
        if lock_acquired:
            av_lock.release()

    def _close_gop_seek_session(self, *, from_gc: bool = False) -> None:
        """关闭 P2-05 seek 专用会话容器。

        ``__del__`` can run after a partially failed construction (and unit
        doubles may intentionally use ``__new__``), so cleanup must tolerate
        fields that were never initialized. GC 路径（from_gc）同样只停靠、
        不做容器关闭（Q01 GIL-freeze guard）。
        """
        gop_seek_container = getattr(self, "_gop_seek_container", None)
        if gop_seek_container is not None:
            self._gop_seek_container = None
            self._gop_seek_stream = None
            if from_gc:
                _defer_container(gop_seek_container)
            elif not _close_container_bounded(gop_seek_container, 3.0):
                logger.debug(
                    "FFmpegSource: gop seek session close dispatched to daemon thread "
                    "(Q01 async-close)")

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """
        读取指定帧。

        如果当前帧不是目标帧，会自动 seek 到目标帧位置。

        Args:
            frame_index: 帧号（从 0 开始）

        Returns:
            DecodedFrame 实例

        Raises:
            MediaDecodeError: 解码失败
            ValueError: 帧号超出范围
            RuntimeError: 媒体源未打开

        P2-1: 此方法是同步解码。播放 tick 不应直接调用。
        播放应通过 DecodeWorker 异步解码，从 ReadyFrameQueue 取帧。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        try:
            from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
            _assert_no_sync_decode_in_playback(
                operation="FFmpegMediaSource.read_frame",
                frame_index=frame_index,
                source_type="ffmpeg",
            )
        except ImportError:
            pass  # 允许独立使用 ffmpeg_source 模块

        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        # 验证帧号范围
        if self._metadata is not None and self._metadata.total_frames is not None:
            if frame_index < 0:
                raise ValueError(f"Frame index cannot be negative: {frame_index}")
            if frame_index >= self._metadata.total_frames:
                raise ValueError(
                    f"Frame index {frame_index} out of range [0, {self._metadata.total_frames})"
                )

        # 如果当前帧不是目标帧，先 seek
        if self._current_frame != frame_index:
            self.seek_frame(frame_index)

        # 读取下一帧
        return self.read_next_frame()

    def read_next_frame(self) -> DecodedFrame:
        """
        读取下一帧（顺序解码）。

        从当前位置解码下一个视频帧，返回 DecodedFrame。
        解码后 current_frame 会自动递增。

        如果 is_planar_mode=True 且源是 YUV 格式，返回 planar DecodedFrame，
        避免 CPU 端 RGB 转换。

        Returns:
            DecodedFrame 实例（packed 或 planar，取决于配置）

        Raises:
            MediaDecodeError: 解码失败
            StopIteration: 到达视频末尾
            RuntimeError: 媒体源未打开

        P2-1: 此方法是同步解码。播放 tick 不应直接调用。
        播放应通过 DecodeWorker 异步解码，从 ReadyFrameQueue 取帧。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        try:
            from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
            _assert_no_sync_decode_in_playback(
                operation="FFmpegMediaSource.read_next_frame",
                frame_index=self._current_frame,
                source_type="ffmpeg",
            )
        except ImportError:
            pass  # 允许独立使用 ffmpeg_source 模块

        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        if self._container is None or self._video_stream is None:
            raise RuntimeError("Container or video stream not initialized")

        try:
            # 检查是否有缓存的 seek 目标帧
            if self._pending_av_frame is not None:
                frame = self._pending_av_frame
                self._pending_av_frame = None
                # Planar 模式：调用 _convert_frame_planar
                if self._is_planar_mode:
                    decoded = self._convert_frame_planar(frame)
                else:
                    decoded = self._convert_frame(frame)
                self._current_frame = max(self._current_frame, decoded.frame_index + 1)
                return decoded

            # P2-05: seek 会话（SLICE）顺序续读 —— HEVC 类 GOP seek 走独立
            # 轻量会话后，续读帧继续从该会话取（SLICE 解码便宜），避免为
            # 单帧顺序读回同步主会话（frame 线程排空 + 重定位 ~370ms 一次）。
            session_frame = self._pull_next_frame_from_seek_session()
            if session_frame is not None:
                if self._is_planar_mode:
                    decoded = self._convert_frame_planar(session_frame)
                else:
                    decoded = self._convert_frame(session_frame)
                self._current_frame = max(self._current_frame, decoded.frame_index + 1)
                return decoded

            # 创建帧迭代器（如果不存在）
            if self._frame_iterator is None:
                self._resync_main_session_if_needed()
                self._frame_iterator = self._container.decode(self._video_stream)

            # 解码下一帧
            for frame in self._frame_iterator:
                # Planar 模式：调用 _convert_frame_planar
                if self._is_planar_mode:
                    decoded = self._convert_frame_planar(frame)
                else:
                    decoded = self._convert_frame(frame)
                self._current_frame += 1
                return decoded

            # 视频结束
            raise StopIteration("End of video stream")

        except StopIteration:
            raise
        except Exception as e:
            logger.error(f"Decode error at frame {self._current_frame}: {e}")
            raise MediaDecodeError(
                f"Decode error at frame {self._current_frame}: {e}",
                source_path=self._path
            ) from e

    def seek_frame(self, frame_index: int) -> None:
        """
        定位到指定帧。

        对于 GOP codec (H.264/H.265/VP9/AV1)，会先 seek 到最近的 keyframe，
        然后 decode forward 到目标帧。

        对于 Intra codec (ProRes/DNxHR/FFV1)，可以直接 seek 到目标帧。

        Args:
            frame_index: 目标帧号（0-based）

        Raises:
            SeekError: 定位失败（无法到达目标帧）
            ValueError: 帧号超出范围
            RuntimeError: 媒体源未打开

        P2-1: seek 操作是同步解码的一部分。播放 tick 不应调用 seek。
        播放时的 seek 应通过 DecodeWorker 请求，不阻塞 UI。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        try:
            from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
            _assert_no_sync_decode_in_playback(
                operation="FFmpegMediaSource.seek_frame",
                frame_index=frame_index,
                source_type="ffmpeg_seek",
            )
        except ImportError:
            pass  # 允许独立使用 ffmpeg_source 模块

        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        if self._container is None or self._video_stream is None:
            raise RuntimeError("Container or video stream not initialized")

        # 获取元数据
        if self._metadata is None:
            self._metadata = self.probe()

        # 验证帧号范围（不允许负数，超出范围也抛异常）
        if frame_index < 0:
            raise ValueError(
                f"Invalid frame index: {frame_index}. "
                f"Frame index must be >= 0."
            )
        if self._metadata.total_frames is not None:
            if frame_index >= self._metadata.total_frames:
                raise ValueError(
                    f"Invalid frame index: {frame_index}. "
                    f"Total frames: {self._metadata.total_frames}. "
                    f"Frame index must be < total_frames."
                )

        # 如果已经在目标帧，无需操作
        if self._current_frame == frame_index:
            return

        logger.debug(
            f"Seeking to frame {frame_index} (current: {self._current_frame}, codec: {self._metadata.codec_name})"
        )

        # 清理旧状态
        self._frame_iterator = None
        self._decoder_buffer = []
        self._pending_av_frame = None
        self._gop_seek_iterator = None
        self._main_resync_pts = None

        # 根据编解码器类型选择 seek 策略
        codec_name = self._metadata.codec_name

        try:
            if _is_intra_codec(codec_name):
                # Intra codec: 直接 seek 到 PTS
                self._seek_intra(frame_index)
            else:
                # GOP codec: keyframe seek + decode forward
                self._seek_gop(frame_index)

            logger.debug(f"Seek complete, current frame: {self._current_frame}")

        except Exception as e:
            logger.error(f"Seek failed: target={frame_index}, actual={self._current_frame}, error={e}")
            raise SeekError(
                target_frame=frame_index,
                actual_frame=self._current_frame
            ) from e

    def _seek_intra(self, frame_index: int) -> None:
        """
        Intra codec 的精确 seek。

        ProRes/DNxHR 等每帧都是关键帧，可以直接 seek 到 PTS。

        Args:
            frame_index: 目标帧号
        """
        if self._container is None or self._video_stream is None:
            return

        # 计算 PTS
        pts = self._frame_to_pts(frame_index)

        # Seek 到 PTS
        try:
            self._container.seek(pts, stream=self._video_stream)
        except Exception as e:
            logger.warning(f"Intra seek failed, fallback to GOP seek: {e}")
            self._seek_gop(frame_index)
            return

        # 创建新迭代器
        self._frame_iterator = self._container.decode(self._video_stream)

        # 读取一帧以验证位置
        try:
            for frame in self._frame_iterator:
                actual_pts = frame.pts if frame.pts is not None else 0
                self._current_frame = self._pts_to_frame(actual_pts)

                # 如果 seek 后的位置正好是目标帧，缓存该帧
                if self._current_frame == frame_index:
                    self._pending_av_frame = frame
                    return

                # 如果 seek 后的位置不是目标帧，微调
                if self._current_frame < frame_index:
                    # 需要向前解码（会缓存目标帧）
                    self._decode_to_target(frame_index, max_frames=100)
                elif self._current_frame > frame_index:
                    # overshoot，需要重新 seek
                    logger.warning(
                        f"Intra seek overshoot: target={frame_index}, got={self._current_frame}"
                    )
                    # 重新 seek 到更早的位置
                    self._seek_gop(frame_index)
                return
        except StopIteration:
            pass

        # Seek 后没有帧
        self._current_frame = frame_index

    def _seek_gop(self, frame_index: int) -> None:
        """
        GOP codec 的精确 seek。

        HEVC 类 codec 路由到独立 SLICE seek 会话（P2-05，实测 seek 快
        ~2.6x 且不影响主会话顺序吞吐）；其余（H264 等）在主会话上
        seek 到最近的 keyframe，然后 decode forward 到目标帧。

        Args:
            frame_index: 目标帧号
        """
        if self._container is None or self._video_stream is None:
            return

        index = self._ensure_gop_index()
        codec_name = self._metadata.codec_name if self._metadata is not None else ""
        if str(codec_name).lower() in GOP_SLICE_SEEK_CODECS:
            try:
                self._seek_gop_session(frame_index, index)
                return
            except Exception as e:  # noqa: BLE001 - 会话失败回退主会话路径
                logger.warning(
                    f"GOP seek session failed (target={frame_index}), "
                    f"falling back to main session: {e}"
                )
                # 回退前复位会话状态，主会话状态由下方 legacy 路径重建
                self._gop_seek_iterator = None
                self._main_resync_pts = None

        # 计算 PTS
        pts = self._frame_to_pts(frame_index)

        # Seek 到目标 PTS（FFmpeg 会定位到之前的 keyframe）
        try:
            # 使用 any_frame=False 确保定位到之前的 keyframe
            self._container.seek(pts, stream=self._video_stream, any_frame=False)
        except Exception as e:
            logger.error(f"GOP seek failed: {e}")
            raise

        # 创建新迭代器
        self._frame_iterator = self._container.decode(self._video_stream)

        # Decode forward 到目标帧；索引就绪时用真实 keyframe 距离收紧
        # walk 上界（防坏流/坏索引下的病态长 walk）
        max_frames = 500
        if index is not None:
            distance = index.decode_distance(frame_index)
            if distance is not None:
                max_frames = min(500, max(16, distance + 4))
        self._decode_to_target(frame_index, max_frames=max_frames)

    # =========================================================================
    # P2-05: GOP 持久化索引 + SLICE seek 会话
    # =========================================================================

    def _ensure_gop_index(self) -> Optional[GopIndex]:
        """
        获取 GOP 索引（P2-05）。

        首次调用同步查磁盘缓存（<1ms 量级）；未命中时后台线程构建并落盘，
        构建完成后的 seek 自动受益。任何失败返回 None，seek 走无索引路径。
        """
        if self._gop_index is not None:
            return self._gop_index
        if self._gop_index_attempted:
            return None
        self._gop_index_attempted = True
        try:
            cached = load_gop_index(self._path)
        except Exception:  # noqa: BLE001 - 索引绝不影响解码主流程
            cached = None
        if cached is not None:
            self._gop_index = cached
            return cached
        # 未命中：后台构建（demux 全量扫描有 IO 成本，不阻塞当前 seek）
        path = self._path

        def _build() -> None:
            try:
                from src.shared.media.av_concurrency import av_operation
                with av_operation():
                    built = get_or_build_gop_index(path, force=False)
            except Exception:  # noqa: BLE001 - 索引构建失败不影响解码主流程
                logger.debug("gop index background build failed", exc_info=True)
                return
            # AUD-34709：close() 后不再写回 _gop_index/落盘缓存
            #（后台线程可能晚于 close 存活）
            if built is not None and self._is_open:
                self._gop_index = built

        self._gop_index_thread = threading.Thread(
            target=_build,
            name=f"gop-index-{Path(self._path).name}",
            daemon=True,
        )
        self._gop_index_thread.start()
        return None

    def warm_gop_index(self, *, force: bool = False) -> Optional[GopIndex]:
        """
        同步构建/加载 GOP 索引（供测试与预热调用；勿在 UI 线程调用）。

        Returns:
            就绪的索引；失败返回 None（AUD-34711：与 docstring 契约
            一致，IO/解析异常记 warning 后不再上抛）。
        """
        try:
            built = get_or_build_gop_index(self._path, force=force)
        except Exception as e:  # noqa: BLE001 - 预热失败不阻断调用方
            logger.warning(f"warm_gop_index failed: {e}")
            return None
        if built is not None:
            self._gop_index = built
            self._gop_index_attempted = True
        return built

    @property
    def keyframe_interval_hint(self) -> Optional[int]:
        """
        真实 keyframe 间隔（索引就绪时）；供 seek 成本决策使用（P2-05）。

        未就绪时 None，决策层回退到 codec 猜测表。
        """
        index = self._gop_index
        if index is not None and len(index.keyframe_frames) >= 2:
            return index.keyframe_interval
        return None

    def _seek_gop_session(self, frame_index: int, index: Optional[GopIndex]) -> None:
        """
        HEVC 类 GOP codec 的 SLICE seek 会话（P2-05）。

        独立容器 + SLICE 线程：seek 无 frame 线程管线排空税，短 walk 有
        slice 并行。目标帧缓存进 _pending_av_frame（与主会话 seek 同一
        语义）；顺序续读帧继续从该会话取（_pull_next_frame_from_seek_session），
        直到显式回同步主会话（_resync_main_session_if_needed）。

        Args:
            frame_index: 目标帧号
            index: GOP 索引（可为 None；用于收紧 walk 上界）
        """
        import av

        if self._gop_seek_container is None:
            container = av.open(self._path)
            video_streams = container.streams.video
            if not video_streams:
                container.close()
                raise MediaOpenError(
                    "No video stream found (gop seek session)",
                    source_path=self._path
                )
            stream = video_streams[0]
            try:
                stream.thread_type = 'SLICE'
            except Exception:  # noqa: BLE001
                pass
            if hasattr(stream, 'codec_context') and stream.codec_context:
                stream.codec_context.thread_count = 0
                if self._disable_hwaccel:
                    # 与主会话一致：CPU 路径禁用 hwaccel
                    if hasattr(stream.codec_context, 'options'):
                        stream.codec_context.options = {
                            **(stream.codec_context.options or {}),
                            'hwaccel': 'none',
                        }
            self._gop_seek_container = container
            self._gop_seek_stream = stream

        stream = self._gop_seek_stream
        target_pts = self._frame_to_pts(frame_index)
        # 旧迭代器直接丢弃：SLICE 会话上重新 seek 无排空代价（实测 ~0ms）
        self._gop_seek_iterator = None
        self._gop_seek_container.seek(target_pts, stream=stream, any_frame=False)
        iterator = self._gop_seek_container.decode(stream)

        max_frames = 500
        if index is not None:
            distance = index.decode_distance(frame_index)
            if distance is not None:
                max_frames = min(500, max(16, distance + 4))

        landed: Any = None
        last_pts: int = -1
        frames_walked = 0
        while frames_walked < max_frames and landed is None:
            exhausted = True
            for frame in iterator:
                exhausted = False
                pts = frame.pts if frame.pts is not None else 0
                frames_walked += 1
                if pts >= target_pts:
                    landed = frame
                    last_pts = pts
                    break
                last_pts = pts
            if exhausted:
                break
            if landed is None:
                # for 正常耗尽（生成器结束）→ 无更多帧
                break

        if landed is None:
            logger.error(
                f"GOP seek session cannot reach target: target={frame_index}, "
                f"last_pts={last_pts}, walked={frames_walked}"
            )
            self._current_frame = (
                self._pts_to_frame(last_pts) if last_pts >= 0 else 0
            )
            raise SeekError(target_frame=frame_index, actual_frame=self._current_frame)

        landed_pts = landed.pts if landed.pts is not None else 0
        if landed_pts != target_pts:
            logger.warning(
                f"Seek overshoot (session): target={frame_index}, "
                f"got={self._pts_to_frame(landed_pts)}"
            )
            self._current_frame = self._pts_to_frame(landed_pts)
            raise SeekError(target_frame=frame_index, actual_frame=self._current_frame)

        # 与主会话 seek 相同的落地语义：缓存目标帧，标记主会话待回同步
        self._pending_av_frame = landed
        self._current_frame = frame_index
        self._frame_iterator = None
        self._gop_seek_iterator = iterator
        self._main_resync_pts = target_pts

    def _pull_next_frame_from_seek_session(self) -> Optional[Any]:
        """
        顺序续读：从 seek 会话取下一帧。

        Returns:
            av frame；会话不存在/已耗尽返回 None（调用方回退主会话）。
        """
        if self._gop_seek_iterator is None:
            return None
        try:
            for frame in self._gop_seek_iterator:
                # AUD-34702：续读推进主会话回同步点——_main_resync_pts
                # 语义是"最近已（即将）服务帧的 PTS"。若一直停留在 seek
                # 目标，会话耗尽/出错时 _resync_main_session_if_needed 会
                # 把主会话回卷到 seek 落地点，重放已服务过的帧。
                pts = frame.pts if frame.pts is not None else 0
                self._main_resync_pts = pts
                return frame
        except StopIteration:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"Seek session decode error: {e}")
        # 会话耗尽/出错：释放迭代器；主会话回同步到已服务位置
        self._gop_seek_iterator = None
        return None

    def _resync_main_session_if_needed(self) -> None:
        """
        恢复主会话迭代器前，回同步到 seek 会话已服务的位置。

        主会话 frame 线程需排空 + 重定位（HEVC 4K 实测 ~370ms 一次），
        仅在真正需要主会话迭代器时付出（例如 seek 会话耗尽或显式回主）。
        """
        if self._main_resync_pts is None:
            return
        resync_pts = self._main_resync_pts
        self._main_resync_pts = None
        self._gop_seek_iterator = None
        container = self._container
        stream = self._video_stream
        if container is None or stream is None:
            return
        try:
            container.seek(resync_pts, stream=stream, any_frame=False)
            iterator = container.decode(stream)
            for frame in iterator:
                pts = frame.pts if frame.pts is not None else 0
                if pts >= resync_pts:
                    # 已服务的帧再次解出后丢弃，迭代器定位于下一帧
                    break
            self._frame_iterator = iterator
        except StopIteration:
            # 源在 seek 会话读取期间已到尾：主迭代器置空即可，
            # 调用方随后的迭代触发正常的 End of stream 语义
            self._frame_iterator = None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Main session resync failed: {e}")
            self._frame_iterator = None

    def _decode_to_target(
        self,
        target_frame: int,
        max_frames: int = 500
    ) -> None:
        """
        从当前位置解码到目标帧。

        Args:
            target_frame: 目标帧号
            max_frames: 最大解码帧数（防止无限循环）

        Raises:
            SeekError: 无法到达目标帧
        """
        frames_decoded = 0

        while frames_decoded < max_frames:
            try:
                for frame in self._frame_iterator or []:
                    actual_pts = frame.pts if frame.pts is not None else 0
                    self._current_frame = self._pts_to_frame(actual_pts)

                    if self._current_frame >= target_frame:
                        # 到达或超过目标帧，缓存目标帧
                        if self._current_frame == target_frame:
                            self._pending_av_frame = frame
                        else:
                            # AUD-34701：overshoot 不能静默返回——
                            # _pending_av_frame 为空而迭代器已越过目标帧，
                            # read_next_frame 会交付下一帧的内容且帧号错标。
                            # 与 _seek_gop_session 的落地语义一致：报 SeekError。
                            logger.warning(
                                f"Seek overshoot: target={target_frame}, "
                                f"got={self._current_frame}"
                            )
                            raise SeekError(
                                target_frame=target_frame,
                                actual_frame=self._current_frame
                            )
                        return

                    frames_decoded += 1

                # 迭代器结束
                break

            except StopIteration:
                break
            except SeekError:
                # AUD-34701：overshoot 的 SeekError 直接向上传递，
                # 不落入通用解码错误分支（避免误日志 + 语义混淆）
                raise
            except Exception as e:
                logger.error(f"Decode error during seek: {e}")
                break

        # 无法到达目标帧
        if self._current_frame != target_frame:
            logger.error(
                f"Cannot reach target frame: target={target_frame}, "
                f"actual={self._current_frame}, decoded={frames_decoded}"
            )
            raise SeekError(target_frame=target_frame, actual_frame=self._current_frame)

    def _frame_to_pts(self, frame_index: int) -> int:
        """
        将帧号转换为 PTS。

        PTS = frame_index * (time_base.den / fps.num)

        Args:
            frame_index: 帧号

        Returns:
            PTS 值（整数）
        """
        if self._video_stream is None or self._metadata is None:
            return frame_index

        time_base = self._video_stream.time_base
        fps = self._metadata.fps

        if time_base is None:
            # 使用帧率倒数作为时间基
            return frame_index * fps.den

        # PTS = frame_index / fps / time_base
        # = frame_index * fps.den / fps.num * time_base.den / time_base.num
        # 简化：假设 time_base = 1/fps，则 PTS = frame_index
        # 但实际情况更复杂，需要考虑 time_base 的实际值

        # 使用 Rational 计算
        # time_base = num/den 秒，PTS 单位是 time_base 的倒数
        # 每帧 PTS 间隔 = 1/fps / time_base = fps.den/fps.num * time_base.den/time_base.num
        # 注意：PyAV 的 time_base 是 Fraction 类型，属性是 numerator/denominator
        tb_num = time_base.numerator if hasattr(time_base, 'numerator') else time_base.num
        tb_den = time_base.denominator if hasattr(time_base, 'denominator') else time_base.den
        pts_value = Fraction(
            frame_index * fps.den * tb_den,
            fps.num * tb_num,
        )

        return int(pts_value + Fraction(1, 2))

    def _pts_to_frame(self, pts: int) -> int:
        """
        将 PTS 转换为帧号。

        Args:
            pts: PTS 值

        Returns:
            帧号（整数，向下取整）
        """
        if self._video_stream is None or self._metadata is None:
            return 0

        time_base = self._video_stream.time_base
        fps = self._metadata.fps

        if time_base is None:
            return pts // fps.den

        # Frame = PTS * time_base * fps
        # = PTS * time_base.num / time_base.den * fps.num / fps.den
        # 简化：Frame = PTS / pts_per_frame
        # 注意：PyAV 的 time_base 是 Fraction 类型，属性是 numerator/denominator
        tb_num = time_base.numerator if hasattr(time_base, 'numerator') else time_base.num
        tb_den = time_base.denominator if hasattr(time_base, 'denominator') else time_base.den
        frame_value = Fraction(
            int(pts) * tb_num * fps.num,
            tb_den * fps.den,
        )
        frame = int(frame_value + Fraction(1, 2))

        return max(0, frame)

    def _convert_frame(self, frame: Any) -> DecodedFrame:
        """
        将 PyAV VideoFrame 转换为 DecodedFrame。

        保持高位深像素格式，不降到 uint8。
        对于 10-bit/12-bit 视频，输出为 uint16 格式。

        P3.B: 如果 target_size 存在，使用 VideoReformatter 进行缩放。

        Args:
            frame: PyAV VideoFrame

        Returns:
            DecodedFrame 实例
        """
        # 获取元数据
        if self._metadata is None:
            self._metadata = self.probe()

        # PTS 和时间
        pts = frame.pts
        time_seconds = 0.0
        if pts is not None and self._video_stream is not None:
            if self._video_stream.time_base is not None:
                time_seconds = float(pts * self._video_stream.time_base)

        # 如果帧是硬件加速帧（CVPixelBuffer 等），先传输到 CPU 内存
        # 这是 FFmpegMediaSource（CPU 路径）的防御性措施：
        # FFmpeg 8.1+ 在 macOS 上可能自动将 ProRes 等 codec 路由到 VideoToolbox，
        # 产出 GPU surface 帧导致 swscale 无法做 reformat。
        # FFmpegHwaccelSource（GPU 路径）有自己的 hw 帧处理逻辑，不经过此代码。
        frame = self._transfer_hw_frame_if_needed(frame)

        # 确定输出像素格式
        target_format = self._determine_output_format()

        # P3.B: 确定 output 尺寸
        # 如果 target_size 存在，使用缩放后的尺寸
        # 否则使用原始尺寸
        if self._target_size is not None:
            output_width, output_height = self._target_size
            is_proxy = True
        else:
            output_width = self._metadata.width
            output_height = self._metadata.height
            is_proxy = False

        data = None
        src_fmt = frame.format.name if hasattr(frame, 'format') and frame.format else ''

        try:
            # P3.B: MVP 方案 - 直接使用 frame.reformat() 进行格式转换和缩放
            # PyAV VideoFrame.reformat() 可以同时进行格式转换和尺寸缩放
            if self._target_size is not None:
                # 缩放 + 格式转换（一步完成）
                converted = frame.reformat(
                    width=output_width,
                    height=output_height,
                    format=target_format
                )
            else:
                # 无缩放：只进行格式转换
                converted = frame.reformat(format=target_format)

            data = converted.to_ndarray()

        except Exception:
            # 主路径失败，尝试多级 fallback
            # 原因可能是：exotic pixel format、hw frame 未 transfer、
            # swscale 不支持该转换组合等
            logger.warning(f"Direct reformat failed ({src_fmt} -> {target_format}), trying fallback chain")

            # 获取源格式的 fallback 中间格式链
            fallback_chain = self._get_reformat_fallback_chain(src_fmt, target_format)

            for inter_fmt, final_fmt in fallback_chain:
                try:
                    intermediate = frame.reformat(format=inter_fmt)
                    if self._target_size is not None:
                        converted = intermediate.reformat(
                            width=output_width, height=output_height,
                            format=final_fmt
                        )
                    else:
                        converted = intermediate.reformat(format=final_fmt)
                    data = converted.to_ndarray()
                    target_format = final_fmt
                    logger.info(
                        f"Fallback reformat succeeded: {src_fmt} -> {inter_fmt} -> {final_fmt}"
                    )
                    break
                except Exception:
                    continue

            if data is None:
                # 所有中间格式 fallback 都失败，最后尝试 to_ndarray
                logger.warning(f"All intermediate reformats failed for {src_fmt}, trying to_ndarray")
                try:
                    data = frame.to_ndarray()
                    target_format = src_fmt
                    output_width = data.shape[1] if data.ndim >= 2 else self._metadata.width
                    output_height = data.shape[0] if data.ndim >= 1 else self._metadata.height
                except Exception as e3:
                    raise MediaDecodeError(
                        f"Failed to convert frame: {e3}",
                        source_path=self._path
                    ) from e3

        # 确定数据类型和尺寸
        dtype = data.dtype
        height, width = data.shape[:2]

        return DecodedFrame(
            frame_index=self._current_frame,
            pts=pts,
            time_seconds=time_seconds,
            data=data,
            planes=tuple(),
            pixel_format=target_format,
            dtype=dtype,
            width=width,
            height=height,
            metadata=self._metadata,
            is_proxy=is_proxy,  # P3.B: 标记 proxy 帧
        )

    @staticmethod
    def _transfer_hw_frame_if_needed(frame: Any) -> Any:
        """
        如果帧是硬件加速帧（CVPixelBuffer-backed 等），将其传输到 CPU 内存。

        FFmpeg 8.1+ 在 macOS 上可能自动将 ProRes 等 codec 路由到 VideoToolbox
        解码，产出的帧是 GPU surface 而非 CPU 内存帧。swscale 无法对 GPU 帧
        进行 reformat() 操作（返回 [Errno 45] Operation not supported）。

        此方法检测 hw 帧并调用 frame.transfer() 下载到 CPU，确保后续
        reformat/to_ndarray 操作可以正常进行。

        注意：此方法仅在 FFmpegMediaSource（CPU 路径）中使用。
        FFmpegHwaccelSource（GPU 路径）有自己的 hw 帧处理逻辑，
        不经过此方法，因此不会破坏全程 GPU 处理链路。

        Args:
            frame: PyAV VideoFrame

        Returns:
            CPU 内存中的 VideoFrame（如果是 hw 帧则传输后的副本，否则原帧）
        """
        is_hw = (
            (hasattr(frame, 'is_hw') and frame.is_hw)
            or (hasattr(frame, 'format')
                and hasattr(frame.format, 'name')
                and frame.format.name.startswith('hw_'))
        )
        if is_hw:
            try:
                if hasattr(frame, 'transfer'):
                    cpu_frame = frame.transfer()
                    logger.debug(
                        f"Transferred hw frame (format: {frame.format.name}) to CPU "
                        f"(format: {cpu_frame.format.name})"
                    )
                    return cpu_frame
            except Exception as e:
                logger.warning(f"Failed to transfer hw frame to CPU: {e}, trying direct reformat")
        return frame

    @staticmethod
    def _get_reformat_fallback_chain(source_pix_fmt: str, target_format: str) -> list[tuple[str, ...]]:
        """
        根据源像素格式生成 reformat 中间格式降级链。

        对于高位深 YUV 格式（如 yuv444p12le），swscale 可能无法直接转换到 rgb48le。
        此方法返回一系列中间格式组合，按优先级排序：

        1. 同族降深：yuv444p12le → yuv444p → rgb24（保留 4:4:4 色度）
        2. 同族 16-bit：yuv444p12le → yuv444p16le → rgb48le（保留位深度范围）
        3. 异族降深：yuv444p12le → yuv420p → rgb24（损失色度，但兼容性最好）

        对于不在此映射表中的格式，返回通用的 yuv420p 中间格式。

        Args:
            source_pix_fmt: 源像素格式名称
            target_format: 目标像素格式名称

        Returns:
            中间格式链列表，每项是 (intermediate_format, final_format) 元组。
            空列表表示无法提供中间格式（应直接报错）。
        """
        fmt = source_pix_fmt.lower()

        SAME_FAMILY_8BIT = {
            'yuv444p10le': 'yuv444p',
            'yuv444p10be': 'yuv444p',
            'yuv444p12le': 'yuv444p',
            'yuv444p12be': 'yuv444p',
            'yuv444p16le': 'yuv444p',
            'yuv444p16be': 'yuv444p',
            'yuv422p10le': 'yuv422p',
            'yuv422p10be': 'yuv422p',
            'yuv422p12le': 'yuv422p',
            'yuv422p12be': 'yuv422p',
            'yuv422p16le': 'yuv422p',
            'yuv422p16be': 'yuv422p',
            'yuv420p10le': 'yuv420p',
            'yuv420p10be': 'yuv420p',
            'yuv420p12le': 'yuv420p',
            'yuv420p12be': 'yuv420p',
            'yuv420p16le': 'yuv420p',
            'yuv420p16be': 'yuv420p',
            'yuva444p10le': 'yuva444p',
            'yuva444p10be': 'yuva444p',
            'yuva444p12le': 'yuva444p',
            'yuva444p12be': 'yuva444p',
            'yuva422p10le': 'yuva422p',
            'yuva422p10be': 'yuva422p',
            'yuva420p10le': 'yuva420p',
            'yuva420p10be': 'yuva420p',
            'p010le': 'yuv420p',
            'p010be': 'yuv420p',
            'p210le': 'yuv422p',
            'p210be': 'yuv422p',
        }

        SAME_FAMILY_16BIT = {
            'yuv444p10le': 'yuv444p16le',
            'yuv444p10be': 'yuv444p16le',
            'yuv444p12le': 'yuv444p16le',
            'yuv444p12be': 'yuv444p16le',
            'yuv422p10le': 'yuv422p16le',
            'yuv422p10be': 'yuv422p16le',
            'yuv422p12le': 'yuv422p16le',
            'yuv422p12be': 'yuv422p16le',
            'yuv420p10le': 'yuv420p16le',
            'yuv420p10be': 'yuv420p16le',
            'yuv420p12le': 'yuv420p16le',
            'yuv420p12be': 'yuv420p16le',
        }

        is_16bit_target = target_format in ('rgb48le', 'rgba64le')

        fallback_chain: list[tuple[str, ...]] = []

        # 优先尝试同族降深到 8-bit（保留色度采样，损失位深精度）
        if fmt in SAME_FAMILY_8BIT:
            inter = SAME_FAMILY_8BIT[fmt]
            final = 'rgb24' if not is_16bit_target else target_format
            fallback_chain.append((inter, final))

        # 其次尝试同族升深到 16-bit（保留位数精度，swscale 对 le 格式互转支持好）
        if fmt in SAME_FAMILY_16BIT and is_16bit_target:
            inter = SAME_FAMILY_16BIT[fmt]
            fallback_chain.append((inter, target_format))

        # 最后尝试异族降深到 yuv420p（兼容性最好，但损失色度）
        fallback_chain.append(('yuv420p', 'rgb24'))

        return fallback_chain

    def _determine_output_format(self) -> str:
        """
        确定输出像素格式。

        根据源视频的位深度选择合适的输出格式：
        - 8-bit: rgb24/rgba
        - 10-bit+: rgb48le/rgba64le（小端格式，与 PyAV/FFmpeg 兼容）

        Returns:
            输出像素格式名称
        """
        if self._metadata is None:
            return 'rgba'

        # 某些 PyAV/FFmpeg 组合在 10-bit YUV → rgb48le 的转换中会产生错误
        # 的色度值。CPU 导出可显式请求 8-bit RGB，交由 swscale 完成正确的
        # range/colorspace 转换；默认仍保留高位深，以免影响 HDR/GPU 路径。
        if self._output_format is not None:
            return self._output_format

        bit_depth = self._metadata.bit_depth
        has_alpha = self._metadata.has_alpha

        # 高位深视频 -> 16-bit 输出（使用 le 后缀，小端格式）
        if bit_depth > 8:
            if has_alpha:
                return 'rgba64le'
            else:
                return 'rgb48le'

        # 8-bit 视频
        if has_alpha:
            return 'rgba'
        else:
            return 'rgb24'

    # =========================================================================
    # Planar 输出和高位深支持 (P3.3)
    # =========================================================================

    def read_frame_planar(self, frame_index: int) -> DecodedFrame:
        """
        读取帧（保留 planar 格式）。

        不进行 CPU 端的 RGB 转换，保留原始 YUV/YUVA planes。
        用于后续 GPU shader 处理，避免 CPU 端不必要的转换。

        对于 yuv420p10le 等 format：
        - 不调用 frame.reformat(format='rgb48le')
        - 直接提取 Y/U/V planes
        - planes = (Y, U, V) 或 (Y, U, V, A)

        Args:
            frame_index: 帧号（从 0 开始）

        Returns:
            DecodedFrame 实例（planes 包含原始平面数据）

        Raises:
            MediaDecodeError: 解码失败
            ValueError: 帧号超出范围
            RuntimeError: 媒体源未打开
        """
        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        # 验证帧号范围
        if self._metadata is not None and self._metadata.total_frames is not None:
            if frame_index < 0:
                raise ValueError(f"Frame index cannot be negative: {frame_index}")
            if frame_index >= self._metadata.total_frames:
                raise ValueError(
                    f"Frame index {frame_index} out of range [0, {self._metadata.total_frames})"
                )

        # 如果当前帧不是目标帧，先 seek
        if self._current_frame != frame_index:
            self.seek_frame(frame_index)

        # 读取下一帧（planar 模式）
        return self._read_next_frame_planar()

    def _read_next_frame_planar(self) -> DecodedFrame:
        """
        读取下一帧（保留 planar 格式）。

        Returns:
            DecodedFrame 实例（包含原始平面数据）

        Raises:
            MediaDecodeError: 解码失败
            StopIteration: 到达视频末尾
        """
        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        if self._container is None or self._video_stream is None:
            raise RuntimeError("Container or video stream not initialized")

        try:
            # 检查是否有缓存的 seek 目标帧
            if self._pending_av_frame is not None:
                frame = self._pending_av_frame
                self._pending_av_frame = None
                decoded = self._convert_frame_planar(frame)
                self._current_frame = max(self._current_frame, decoded.frame_index + 1)
                return decoded

            # P2-05: seek 会话（SLICE）顺序续读（与 read_next_frame 同语义）
            session_frame = self._pull_next_frame_from_seek_session()
            if session_frame is not None:
                decoded = self._convert_frame_planar(session_frame)
                self._current_frame = max(self._current_frame, decoded.frame_index + 1)
                return decoded

            # 创建帧迭代器（如果不存在）
            if self._frame_iterator is None:
                self._resync_main_session_if_needed()
                self._frame_iterator = self._container.decode(self._video_stream)

            # 解码下一帧
            for frame in self._frame_iterator:
                decoded = self._convert_frame_planar(frame)
                self._current_frame += 1
                return decoded

            # 视频结束
            raise StopIteration("End of video stream")

        except StopIteration:
            raise
        except Exception as e:
            logger.error(f"Planar decode error at frame {self._current_frame}: {e}")
            raise MediaDecodeError(
                f"Planar decode error at frame {self._current_frame}: {e}",
                    source_path=self._path
                ) from e

    def _convert_frame_planar(self, frame: Any) -> DecodedFrame:
        """
        将 PyAV VideoFrame 转换为 DecodedFrame（planar 格式）。

        保留原始 YUV/YUVA planes，不进行 RGB 转换。
        用于高位深视频的直接 GPU 处理。

        当 target_size 存在时：
        - 先用 PyAV reformat 到目标尺寸和同族 YUV planar 格式
        - 再从缩放后的 frame 提取 planes

        Args:
            frame: PyAV VideoFrame

        Returns:
            DecodedFrame 实例（planes 包含原始平面数据）
        """
        # 获取元数据
        if self._metadata is None:
            self._metadata = self.probe()

        # PTS 和时间
        pts = frame.pts
        time_seconds = 0.0
        if pts is not None and self._video_stream is not None:
            if self._video_stream.time_base is not None:
                time_seconds = float(pts * self._video_stream.time_base)

        # 获取原始像素格式
        original_pix_fmt = frame.format.name if hasattr(frame, 'format') and frame.format else 'unknown'

        # 如果帧是硬件加速帧，先传输到 CPU（同 _convert_frame 的防御性措施）
        frame = self._transfer_hw_frame_if_needed(frame)
        # 传输后格式可能已变化，重新获取
        original_pix_fmt = frame.format.name if hasattr(frame, 'format') and frame.format else original_pix_fmt

        # 确定输出尺寸和 proxy 标志
        if self._target_size is not None:
            output_width, output_height = self._target_size
            is_proxy = True
        else:
            output_width = self._metadata.width
            output_height = self._metadata.height
            is_proxy = False

        # 用于记录 reformat 状态
        reformat_success = False
        reformat_fallback = False
        pix_fmt = original_pix_fmt

        # P3.B: 如果有 target_size，先进行缩放
        if self._target_size is not None:
            # 获取目标 planar 格式
            target_planar_fmt = self._get_planar_format_for_scaling(original_pix_fmt)

            if target_planar_fmt:
                # 尝试 reformat 到目标尺寸和 planar 格式
                try:
                    converted = frame.reformat(
                        width=output_width,
                        height=output_height,
                        format=target_planar_fmt
                    )
                    frame = converted
                    pix_fmt = target_planar_fmt
                    reformat_success = True
                    logger.debug(
                        f"Planar reformat: {original_pix_fmt} -> {target_planar_fmt} "
                        f"at {output_width}x{output_height}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Planar reformat failed for {original_pix_fmt} -> {target_planar_fmt}: {e}, "
                        f"falling back via planar conversion chain"
                    )
                    # AUD-34703：回退只允许落在 planar 格式上——fallback
                    # chain 的中间格式全部是同族 planar YUV，以其为终点
                    # 逐一尝试；绝不继续转到 packed rgb48le/rgba64le 后再
                    # 按平面布局切片（那样只会取到每行前 1/3 样本）。
                    fallback_frame, fallback_fmt = self._reformat_to_planar_fallback(
                        frame, original_pix_fmt, output_width, output_height
                    )
                    if fallback_fmt != original_pix_fmt:
                        frame = fallback_frame
                        pix_fmt = fallback_fmt
                        reformat_fallback = True
            else:
                # 不支持的格式：同 AUD-34703，走 planar 中间格式回退链
                logger.warning(
                    f"No planar format mapping for {original_pix_fmt}, "
                    f"falling back via planar conversion chain"
                )
                fallback_frame, fallback_fmt = self._reformat_to_planar_fallback(
                    frame, original_pix_fmt, output_width, output_height
                )
                if fallback_fmt != original_pix_fmt:
                    frame = fallback_frame
                    pix_fmt = fallback_fmt
                    reformat_fallback = True

        # AUD-34703 布局一致性防御：planar 输出合约下绝不能对 packed RGB
        # 帧做平面切片（错乱数据）。到这一步 pix_fmt 仍是 packed 说明回退
        # 未能守住 planar 合约——显式失败优于静默交付错乱平面。
        if pix_fmt.lower().startswith(_PACKED_RGB_PREFIXES):
            raise MediaDecodeError(
                f"Planar output not available: {original_pix_fmt} only converts "
                f"to packed RGB ({pix_fmt})",
                source_path=self._path
            )

        # 提取 planes
        planes, plane_infos = self._extract_planes_from_frame(frame, pix_fmt)

        # 确定 dtype（从第一个平面）
        dtype = planes[0].dtype if planes else np.uint8

        # 获取尺寸（从第一个平面）
        height, width = planes[0].shape if planes else (0, 0)

        # 构建额外元数据（记录 reformat 状态）
        extra_metadata = {}
        frame_chroma_siting = _normalize_chroma_siting(
            getattr(frame, 'chroma_location', None))
        if frame_chroma_siting is not None:
            extra_metadata['chroma_siting'] = frame_chroma_siting
        if is_proxy:
            extra_metadata['proxy_reformat_success'] = reformat_success
            extra_metadata['proxy_reformat_fallback'] = reformat_fallback
            extra_metadata['original_pixel_format'] = original_pix_fmt

        return DecodedFrame(
            frame_index=self._current_frame,
            pts=pts,
            time_seconds=time_seconds,
            data=None,  # planar 格式，无打包数据
            planes=planes,
            pixel_format=pix_fmt,
            dtype=dtype,
            width=width,
            height=height,
            metadata=self._metadata,
            plane_infos=plane_infos,
            is_proxy=is_proxy,
            extra=extra_metadata if extra_metadata else None,
        )

    def _get_planar_format_for_scaling(self, source_pix_fmt: str) -> Optional[str]:
        """
        根据源像素格式获取用于缩放的目标 planar YUV 格式。

        格式映射：
        - yuv420p / yuvj420p -> yuv420p
        - yuv420p10le / p010le -> yuv420p10le（不支持时 fallback packed 16-bit RGB）
        - yuv422p -> yuv422p
        - yuv444p -> yuv444p

        Args:
            source_pix_fmt: 源像素格式名称

        Returns:
            目标 planar 格式名称，或 None（不支持的格式）
        """
        fmt_lower = source_pix_fmt.lower()

        # 8-bit YUV 4:2:0
        if fmt_lower in ('yuv420p', 'yuvj420p'):
            return 'yuv420p'

        # 10-bit YUV 4:2:0 (P010 semiplanar -> yuv420p10le planar)
        if fmt_lower in ('yuv420p10le', 'yuv420p10be', 'p010le', 'p010be'):
            return 'yuv420p10le'

        # 12-bit YUV 4:2:0
        if fmt_lower in ('yuv420p12le', 'yuv420p12be'):
            return 'yuv420p12le'

        # 8-bit YUV 4:2:2
        if fmt_lower in ('yuv422p', 'yuvj422p'):
            return 'yuv422p'

        # 10-bit YUV 4:2:2
        if fmt_lower in ('yuv422p10le', 'yuv422p10be', 'p210le', 'p210be'):
            return 'yuv422p10le'

        # 12-bit YUV 4:2:2 (DNxHR HQX 12-bit 等)
        if fmt_lower in ('yuv422p12le', 'yuv422p12be'):
            return 'yuv422p12le'

        # 8-bit YUV 4:4:4
        if fmt_lower in ('yuv444p', 'yuvj444p'):
            return 'yuv444p'

        # 10-bit YUV 4:4:4
        if fmt_lower in ('yuv444p10le', 'yuv444p10be'):
            return 'yuv444p10le'

        # 12-bit YUV 4:4:4
        if fmt_lower in ('yuv444p12le', 'yuv444p12be'):
            return 'yuv444p12le'

        # YUVA formats (with alpha)
        if fmt_lower in ('yuva420p',):
            return 'yuva420p'

        if fmt_lower in ('yuva420p10le', 'yuva420p10be'):
            return 'yuva420p10le'

        if fmt_lower in ('yuva422p',):
            return 'yuva422p'

        if fmt_lower in ('yuva422p10le', 'yuva422p10be'):
            return 'yuva422p10le'

        if fmt_lower in ('yuva444p',):
            return 'yuva444p'

        if fmt_lower in ('yuva444p10le', 'yuva444p10be'):
            return 'yuva444p10le'

        # NV12 / NV21 semiplanar -> yuv420p planar
        if fmt_lower in ('nv12', 'nv21'):
            return 'yuv420p'

        # 不支持的格式，返回 None（将 fallback 到 packed RGB）
        logger.debug(f"No planar format mapping for {source_pix_fmt}")
        return None

    def _reformat_to_planar_fallback(
        self,
        frame: Any,
        source_pix_fmt: str,
        output_width: int,
        output_height: int,
    ) -> tuple[Any, str]:
        """
        AUD-34703：planar 路径的缩放回退——只允许落在 planar 格式上。

        `_get_reformat_fallback_chain` 为 packed RGB 目标构造的中间格式
        全部是同族 planar YUV（yuv420p/yuv444p/yuv*p16le/yuva*p 等）。
        planar 输出合约要求布局标志与实际切片一致：回退到 packed
        rgb48le/rgba64le 后继续按平面切片会得到每行前 1/3 样本的错乱
        数据，因此这里以中间 planar 格式为终点逐一尝试（同时完成缩放）。

        Args:
            frame: PyAV VideoFrame（未被修改的原帧）
            source_pix_fmt: 源像素格式名称
            output_width: 目标宽度（proxy 缩放）
            output_height: 目标高度（proxy 缩放）

        Returns:
            (frame, pix_fmt)：成功时为缩放后的 planar 帧及其格式；
            全部失败时原样返回 (frame, source_pix_fmt)（保持源 planar
            布局、放弃缩放），由调用方的 packed 防御兜底。
        """
        candidates: list[str] = []
        for inter_fmt, _final_fmt in self._get_reformat_fallback_chain(
            source_pix_fmt, 'rgb48le'
        ):
            if inter_fmt not in candidates:
                candidates.append(inter_fmt)

        for planar_fmt in candidates:
            try:
                converted = frame.reformat(
                    width=output_width,
                    height=output_height,
                    format=planar_fmt
                )
                logger.info(
                    f"Planar fallback: {source_pix_fmt} -> {planar_fmt} "
                    f"at {output_width}x{output_height}"
                )
                return converted, planar_fmt
            except Exception:
                continue

        logger.warning(
            f"Planar fallback chain exhausted for {source_pix_fmt}, "
            f"delivering unscaled source-layout frame"
        )
        return frame, source_pix_fmt

    def _extract_planes_from_frame(
        self,
        frame: Any,
        pix_fmt: str
    ) -> Tuple[tuple[np.ndarray, ...], tuple[PlaneInfo, ...]]:
        """
        从 PyAV VideoFrame 提取所有平面。

        支持各种 planar YUV/YUVA 格式：
        - yuv420p, yuv422p, yuv444p (Y, U, V)
        - yuva420p, yuva444p (Y, U, V, A)
        - yuv420p10le, yuv444p10le (Y, U, V, 10-bit)
        - yuva444p10le (Y, U, V, A, 10-bit)

        半平面格式（nv12/nv21/p010 等）由 _extract_semiplanar_planes
        显式去交织为 (Y, U, V) 后返回（AUD-34704）。

        Args:
            frame: PyAV VideoFrame
            pix_fmt: 像素格式名称

        Returns:
            (planes tuple, plane_infos tuple)
        """
        import numpy as np

        planes: list[np.ndarray] = []
        plane_infos: list[PlaneInfo] = []

        # 获取 frame 的 planes（PyAV 内部结构）
        # PyAV VideoFrame 有 .planes 属性
        if hasattr(frame, 'planes') and frame.planes:
            av_planes = frame.planes

            # AUD-34704：半平面格式（nv12/p010 等）不能按纯 planar 提取——
            # 交织 chroma 会被当作单一分量切片且缺少 V 平面。
            semiplanar_layout = _SEMIPLANAR_LAYOUTS.get(pix_fmt.lower())
            if semiplanar_layout is not None:
                return self._extract_semiplanar_planes(
                    frame, pix_fmt, semiplanar_layout
                )

            # 根据像素格式确定分量名称
            is_yuv = pix_fmt.startswith('yuv') or pix_fmt.startswith('yuva')
            is_gbr = pix_fmt.startswith('gbr') or pix_fmt.startswith('gbrp')
            has_alpha = 'a' in pix_fmt.lower() or 'yuva' in pix_fmt.lower()

            # 推断位深度
            bit_depth = _infer_bit_depth(pix_fmt)

            # 确定 dtype
            if bit_depth <= 8:
                dtype = np.uint8
            elif bit_depth <= 16:
                dtype = np.uint16
            else:
                dtype = np.float32

            # 主尺寸
            width = frame.width
            height = frame.height

            # 色度子采样因子
            chroma_width_div = self._get_chroma_width_divisor(pix_fmt)
            chroma_height_div = self._get_chroma_height_divisor(pix_fmt)

            for i, av_plane in enumerate(av_planes):
                # 确定分量名称
                if is_yuv:
                    if i == 0:
                        component = 'Y'
                        plane_width = width
                        plane_height = height
                    elif i == 1:
                        component = 'U'
                        plane_width = width // chroma_width_div
                        plane_height = height // chroma_height_div
                    elif i == 2:
                        component = 'V'
                        plane_width = width // chroma_width_div
                        plane_height = height // chroma_height_div
                    elif i == 3 and has_alpha:
                        component = 'A'
                        plane_width = width
                        plane_height = height
                    else:
                        component = f'P{i}'
                        plane_width = av_plane.width if hasattr(av_plane, 'width') else width
                        plane_height = av_plane.height if hasattr(av_plane, 'height') else height
                elif is_gbr:
                    if i == 0:
                        component = 'G'
                    elif i == 1:
                        component = 'B'
                    elif i == 2:
                        component = 'R'
                    elif i == 3 and has_alpha:
                        component = 'A'
                    else:
                        component = f'P{i}'
                    plane_width = width
                    plane_height = height
                else:
                    component = f'P{i}'
                    plane_width = av_plane.width if hasattr(av_plane, 'width') else width
                    plane_height = av_plane.height if hasattr(av_plane, 'height') else height

                plane_data = self._extract_visible_plane_data(
                    av_plane=av_plane,
                    plane_width=plane_width,
                    plane_height=plane_height,
                    dtype=np.dtype(dtype),
                    pix_fmt=pix_fmt,
                )
                actual_height, actual_width = plane_data.shape[:2]

                planes.append(plane_data)

                # 创建 PlaneInfo
                plane_info = PlaneInfo.create(
                    index=i,
                    width=actual_width,
                    height=actual_height,
                    component=component,
                    dtype=dtype,
                    bit_depth=bit_depth,
                )
                plane_infos.append(plane_info)

        else:
            # 没有 planes 属性，可能是 packed 格式
            # 尝试直接转换为 ndarray 并分割
            logger.warning(f"Frame has no planes attribute, pix_fmt={pix_fmt}")
            data = frame.to_ndarray()
            # 对于 packed 格式，暂时作为单平面处理
            planes.append(data)
            plane_infos.append(PlaneInfo.create(
                index=0,
                width=data.shape[1] if data.ndim >= 2 else 1,
                height=data.shape[0] if data.ndim >= 1 else 1,
                component='RGB',
                dtype=data.dtype,
                bit_depth=_infer_bit_depth(pix_fmt),
            ))

        return tuple(planes), tuple(plane_infos)

    def _extract_semiplanar_planes(
        self,
        frame: Any,
        pix_fmt: str,
        layout: tuple[int, int, bool],
    ) -> Tuple[tuple[np.ndarray, ...], tuple[PlaneInfo, ...]]:
        """
        AUD-34704：半平面格式（NV12/NV21/P010/NV16/NV24 等）的显式平面提取。

        半平面格式在内存中只有 2 个缓冲平面：Y 平面 + 交织 chroma 平面
        （UV 或 VU 交替存储）。输出合约与其余 planar 路径一致：返回按
        分量拆分的 (Y, U, V) 三个独立平面，尺寸按色度子采样计算。

        Args:
            frame: PyAV VideoFrame（CPU 内存帧，2 个平面）
            pix_fmt: 像素格式名称
            layout: (chroma_width_divisor, chroma_height_divisor, vu_order)

        Returns:
            (planes tuple, plane_infos tuple)：Y/U/V 三平面及信息

        Raises:
            MediaDecodeError: 帧平面数不符合半平面布局（数据/格式契约不符）
        """
        chroma_w_div, chroma_h_div, vu_order = layout
        av_planes = frame.planes
        if len(av_planes) < 2:
            raise MediaDecodeError(
                f"Semiplanar frame must have >=2 planes, got {len(av_planes)} "
                f"(pix_fmt={pix_fmt})",
                source_path=self._path
            )

        width = frame.width
        height = frame.height
        # FFmpeg 子采样维度向上取整（奇数分辨率下 chroma 也要覆盖全帧）
        chroma_width = (width + chroma_w_div - 1) // chroma_w_div
        chroma_height = (height + chroma_h_div - 1) // chroma_h_div

        bit_depth = _infer_bit_depth(pix_fmt)
        dtype = np.dtype(np.uint8) if bit_depth <= 8 else np.dtype(np.uint16)

        y_plane = self._extract_visible_plane_data(
            av_plane=av_planes[0],
            plane_width=width,
            plane_height=height,
            dtype=dtype,
            pix_fmt=pix_fmt,
        )
        # 交织 chroma 平面：每行 2*chroma_width 个样本（U/V 交替），
        # 以完整交织行宽提取后按奇偶列去交织
        uv_interleaved = self._extract_visible_plane_data(
            av_plane=av_planes[1],
            plane_width=chroma_width * 2,
            plane_height=chroma_height,
            dtype=dtype,
            pix_fmt=pix_fmt,
        )
        even_samples = uv_interleaved[:, 0::2]
        odd_samples = uv_interleaved[:, 1::2]
        if vu_order:
            # NV21/NV42：交织顺序为 VU
            u_plane, v_plane = odd_samples, even_samples
        else:
            u_plane, v_plane = even_samples, odd_samples

        planes_out: tuple[np.ndarray, ...] = (
            np.ascontiguousarray(y_plane),
            np.ascontiguousarray(u_plane),
            np.ascontiguousarray(v_plane),
        )
        plane_infos: tuple[PlaneInfo, ...] = (
            PlaneInfo.create(
                index=0, width=width, height=height,
                component='Y', dtype=dtype, bit_depth=bit_depth,
            ),
            PlaneInfo.create(
                index=1, width=chroma_width, height=chroma_height,
                component='U', dtype=dtype, bit_depth=bit_depth,
            ),
            PlaneInfo.create(
                index=2, width=chroma_width, height=chroma_height,
                component='V', dtype=dtype, bit_depth=bit_depth,
            ),
        )
        return planes_out, plane_infos

    @staticmethod
    def _extract_visible_plane_data(
        av_plane: Any,
        plane_width: int,
        plane_height: int,
        dtype: np.dtype,
        pix_fmt: str,
    ) -> np.ndarray:
        """
        Extract a PyAV plane as visible pixel samples, cropping stride padding.

        PyAV exposes VideoPlane as a byte buffer. For high-bit-depth formats such
        as yuv422p10le, np.array(av_plane) returns uint8 bytes with width equal
        to line_size, not uint16 pixel samples. Interpreting those bytes directly
        doubles the visible width and breaks YUV color reconstruction.
        """
        itemsize = dtype.itemsize
        line_size = int(getattr(av_plane, 'line_size', 0) or plane_width * itemsize)
        buffer_height = int(getattr(av_plane, 'height', 0) or plane_height)

        if line_size <= 0 or buffer_height <= 0:
            raise ValueError(
                f"Invalid plane layout: line_size={line_size}, height={buffer_height}"
            )
        if line_size % itemsize != 0:
            raise ValueError(
                f"Plane line_size {line_size} is not aligned to dtype {dtype}"
            )

        raw = np.frombuffer(av_plane, dtype=np.uint8)
        required_bytes = line_size * buffer_height
        if raw.size < required_bytes:
            raise ValueError(
                f"Plane buffer too small: got {raw.size} bytes, need {required_bytes}"
            )

        raw = raw[:required_bytes]

        if itemsize == 1:
            rows = raw.reshape(buffer_height, line_size)
            visible = rows[:plane_height, :plane_width].astype(dtype, copy=False)
            return np.ascontiguousarray(visible)

        fmt_lower = pix_fmt.lower()
        if dtype == np.dtype(np.uint16):
            sample_dtype = np.dtype('>u2') if fmt_lower.endswith('be') else np.dtype('<u2')
        else:
            sample_dtype = dtype

        stride_samples = line_size // itemsize
        samples = raw.view(sample_dtype).reshape(buffer_height, stride_samples)
        visible = samples[:plane_height, :plane_width]
        if visible.dtype != dtype:
            visible = visible.astype(dtype, copy=False)
        return np.ascontiguousarray(visible)

    def _get_chroma_width_divisor(self, pix_fmt: str) -> int:
        """
        获取色度平面宽度除数。

        Args:
            pix_fmt: 像素格式名称

        Returns:
            除数值（1, 2, 4）
        """
        # 4:2:0 和 4:2:2 -> 宽度除 2
        # 4:1:1 -> 宽度除 4
        # 4:4:4 -> 宽度除 1
        lower_fmt = pix_fmt.lower()

        if '420' in lower_fmt or '422' in lower_fmt:
            return 2
        elif '411' in lower_fmt or '410' in lower_fmt:
            return 4
        elif '444' in lower_fmt:
            return 1
        # 默认
        return 2 if 'yuv' in lower_fmt else 1

    def _get_chroma_height_divisor(self, pix_fmt: str) -> int:
        """
        获取色度平面高度除数。

        Args:
            pix_fmt: 像素格式名称

        Returns:
            除数值（1, 2）
        """
        # 4:2:0 -> 高度除 2
        # 4:2:2 和 4:4:4 -> 高度除 1
        lower_fmt = pix_fmt.lower()

        if '420' in lower_fmt or '410' in lower_fmt:
            return 2
        elif '422' in lower_fmt or '444' in lower_fmt:
            return 1
        # 默认
        return 2 if 'yuv' in lower_fmt and '420' in lower_fmt else 1

    # =========================================================================
    # Alpha 保留和 ProRes 4444 支持 (P3.4)
    # =========================================================================

    def _extract_alpha_from_yuva(self, frame: Any) -> Optional[np.ndarray]:
        """
        从 YUVA 格式提取 alpha 平面。

        对于 yuva444p10le 等 YUVA 格式，alpha 是第 4 个平面。

        Args:
            frame: PyAV VideoFrame

        Returns:
            alpha 数组或 None（无 alpha）
        """
        import numpy as np

        if not hasattr(frame, 'planes') or not frame.planes:
            return None

        # YUVA 格式有 4 个平面：Y, U, V, A
        if len(frame.planes) >= 4:
            alpha_plane = frame.planes[3]
            alpha_data = np.array(alpha_plane, copy=True)
            return alpha_data

        return None

    def _handle_prores_4444_alpha(self, frame: Any) -> Tuple[Optional[np.ndarray], dict[str, Any]]:
        """
        ProRes 4444 alpha 特殊处理。

        ProRes 4444 编码的 alpha 可能是独立的，需要检查：
        1. frame.planes（直接 alpha 平面）
        2. side_data（某些编码器可能将 alpha 存放在 side data）
        3. metadata（alpha channel 信息）

        Args:
            frame: PyAV VideoFrame

        Returns:
            (alpha 数组或 None, alpha 元数据字典)
        """
        import numpy as np

        alpha_metadata: dict[str, Any] = {
            'has_alpha': False,
            'bit_depth': 0,
            'source': 'none',
        }

        # 方法 1: 直接从 planes 提取（最常见）
        if hasattr(frame, 'planes') and len(frame.planes) >= 4:
            alpha_data = self._extract_alpha_from_yuva(frame)
            if alpha_data is not None:
                alpha_metadata['has_alpha'] = True
                alpha_metadata['source'] = 'plane'
                alpha_metadata['bit_depth'] = _infer_bit_depth(
                    frame.format.name if hasattr(frame, 'format') and frame.format else 'yuva444p10le'
                )
                return alpha_data, alpha_metadata

        # 方法 2: 检查 side_data（某些编码器）
        if hasattr(frame, 'side_data'):
            for side_data in frame.side_data:
                if hasattr(side_data, 'type') and 'alpha' in str(side_data.type).lower():
                    # 尝试提取 side data
                    try:
                        alpha_data = np.array(side_data, copy=True)
                        alpha_metadata['has_alpha'] = True
                        alpha_metadata['source'] = 'side_data'
                        alpha_metadata['bit_depth'] = 8  # side data alpha 通常 8-bit
                        return alpha_data, alpha_metadata
                    except Exception as e:
                        logger.warning(f"Failed to extract alpha from side_data: {e}")

        # 方法 3: 检查 frame metadata
        if hasattr(frame, 'metadata') and frame.metadata:
            alpha_info = frame.metadata.get('alpha', None)
            if alpha_info:
                alpha_metadata['has_alpha'] = True
                alpha_metadata['source'] = 'metadata'
                # 但无法直接提取数据，只能标记有 alpha

        # 检查 codec 是否为 ProRes 4444
        if self._metadata and '4444' in self._metadata.codec_name.lower():
            alpha_metadata['has_alpha'] = True
            alpha_metadata['source'] = 'codec_hint'

        return None, alpha_metadata

    def _get_alpha_plane(
        self,
        frame: Any,
        pix_fmt: str,
        prefer_planar: bool = True
    ) -> Optional[np.ndarray]:
        """
        获取帧的 alpha 平面。

        支持：
        - YUVA planar 格式
        - RGBA packed 格式
        - ProRes 4444 特殊处理

        Args:
            frame: PyAV VideoFrame
            pix_fmt: 像素格式名称
            prefer_planar: 是否优先提取 planar alpha

        Returns:
            alpha 数组或 None
        """
        import numpy as np

        # YUVA planar 格式
        if 'yuva' in pix_fmt.lower():
            if prefer_planar:
                return self._extract_alpha_from_yuva(frame)
            else:
                # 转换为 RGBA 后提取 alpha
                try:
                    rgba = frame.reformat(format='rgba64le' if _infer_bit_depth(pix_fmt) > 8 else 'rgba')
                    rgba_data = rgba.to_ndarray()
                    return rgba_data[..., 3]  # Alpha 通道
                except Exception as e:
                    logger.warning(f"Failed to convert YUVA to RGBA: {e}")
                    return self._extract_alpha_from_yuva(frame)

        # GBRAP planar 格式
        if 'gbrap' in pix_fmt.lower():
            if hasattr(frame, 'planes') and len(frame.planes) >= 4:
                alpha_data = np.array(frame.planes[3], copy=True)
                return alpha_data

        # RGBA/GBRA packed 格式
        if 'rgba' in pix_fmt.lower() or 'bgra' in pix_fmt.lower():
            try:
                data = frame.to_ndarray()
                if data.ndim == 3 and data.shape[2] >= 4:
                    return data[..., 3]
            except Exception as e:
                logger.warning(f"Failed to extract alpha from packed format: {e}")

        # ProRes 4444 特殊处理
        if self._metadata and 'prores' in self._metadata.codec_name.lower() and '4444' in self._metadata.codec_name.lower():
            alpha_data, _ = self._handle_prores_4444_alpha(frame)
            return alpha_data

        return None

    def _extract_metadata_from_stream(self) -> VideoMetadata:
        """
        从已打开的视频流提取元数据。

        Returns:
            VideoMetadata 实例
        """
        if self._container is None or self._video_stream is None:
            raise RuntimeError("Container or stream not initialized")

        return self._extract_metadata_from_container(
            self._container, self._video_stream
        )

    def _extract_metadata_from_container(
        self,
        container: Any,
        stream: Any
    ) -> VideoMetadata:
        """
        从 PyAV container 和 stream 提取元数据。

        Args:
            container: PyAV container
            stream: 视频流

        Returns:
            VideoMetadata 实例
        """
        # 基本信息
        width = stream.width
        height = stream.height

        codec_name = 'unknown'
        if hasattr(stream, 'codec') and stream.codec:
            codec_name = stream.codec.name
        elif hasattr(stream, 'codec_context') and stream.codec_context:
            codec_name = stream.codec_context.name

        codec_profile = self._extract_profile(stream)

        # 像素格式（_safe_stream_pix_fmt：禁止直读 pix_fmt——PyAV 17 的
        # getter 对 AV_PIX_FMT_NONE 无守卫，PixelFormat(-1) 取 NULL 描述符
        # 直接段崩溃，hasattr/try/except 均无法拦截）
        pix_fmt = _safe_stream_pix_fmt(stream)

        # 位深度
        bit_depth = _infer_bit_depth(pix_fmt)

        # 帧率
        fps = self._extract_fps(stream, container)

        # 总帧数
        total_frames = self._extract_total_frames(stream, container)

        # 色彩信息（使用 normalize 函数兼容多种输入类型）
        color_primaries = normalize_color_primaries(
            self._get_stream_property(stream, 'color_primaries')
        )
        color_transfer = normalize_color_transfer(
            self._get_stream_property(stream, 'color_trc')
        )
        color_space = normalize_color_space(
            self._get_stream_property(stream, 'colorspace')
        )
        color_range = _map_color_range(
            self._get_stream_property(stream, 'color_range')
        )

        raw_chroma_siting = self._get_stream_property(
            stream, 'chroma_location')
        if raw_chroma_siting is None:
            stream_metadata = getattr(stream, 'metadata', None) or {}
            raw_chroma_siting = (
                stream_metadata.get('chroma_location')
                or stream_metadata.get('chroma-location')
            )
        chroma_siting = _normalize_chroma_siting(raw_chroma_siting)

        # SAR
        sar = self._extract_sar(stream)

        # 时间码
        timecode = self._extract_timecode(stream, container)

        # 是否有 Alpha
        has_alpha = self._check_alpha(pix_fmt, stream)

        # 场序
        field_order = _infer_field_order(
            self._get_stream_property(stream, 'field_order', as_str=True)
        )

        # 旋转
        rotation = self._extract_rotation(stream, container)

        return VideoMetadata(
            width=width,
            height=height,
            fps=fps,
            total_frames=total_frames,
            codec_name=codec_name,
            codec_profile=codec_profile,
            pixel_format=pix_fmt,
            bit_depth=bit_depth,
            color_primaries=color_primaries,
            color_transfer=color_transfer,
            color_matrix=color_space,
            color_range=color_range,
            sample_aspect_ratio=sar,
            timecode=timecode,
            has_alpha=has_alpha,
            field_order=field_order,
            rotation=rotation,
            chroma_siting=chroma_siting,
        )

    def read_audio_samples(
        self,
        start_pts: int,
        count: int
    ) -> Optional[Any]:
        """
        读取音频采样（预留接口）。

        Args:
            start_pts: 起始时间戳
            count: 采样数量

        Returns:
            音频采样数组，无音频返回 None
        """
        # 预留接口，后续实现
        return None

    def __enter__(self) -> 'FFmpegMediaSource':
        """上下文管理器入口"""
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """上下文管理器退出"""
        self.close()

    def __del__(self) -> None:
        """析构时确保资源释放。

        Q01/F1.1：GC 可发生在任意线程/字节码边界，并发 ffmpeg 使用期间
        av 容器 close 会互锁且不释放 GIL——GC 路径不做容器关闭，停靠到
        模块级注册表由进程退出回收（两次 faulthandler 转储实证）。
        """
        try:
            self.close(from_gc=True)
        except Exception:  # noqa: BLE001 - __del__ 绝不向外抛
            pass


# Q01/F1.2 根治：解码公共入口加进程级 in-flight 计数 + 每实例 RLock
# 串行化（同实例并发访问会楔死 libav 上下文且不释放 GIL；详见
# src/shared/media/av_concurrency.py）。
from src.shared.media.av_concurrency import av_locked_operation as _av_op_dec  # noqa: E402

for _av_entry in (
    "open", "probe", "read_frame", "read_next_frame", "seek_frame",
    "warm_gop_index",
):
    setattr(FFmpegMediaSource, _av_entry,
            _av_op_dec(FFmpegMediaSource.__dict__[_av_entry]))
