"""
Media Source Factory Module
媒体源工厂，根据配置和可用性选择最优后端。

## 职责

- MediaBackend: 后端类型枚举
- MediaSourceFactory: 工厂类，创建 MediaSource 实例
- 硬件加速策略集成（P9.A）

## 后端优先级（auto 模式）

0. Topos 自研解码（libtopos_codec）——仅当内容探测（is_topos_movie，
   结构化 magic 'TPIC'）确认是 Topos MOV 时优先（阶段 6，R-17）
1. FFmpeg (PyAV) + 硬件加速（如可用且策略允许）
2. FFmpeg (PyAV) CPU 解码

Topos MOV 与通用 QuickTime 共用 '.mov' 扩展名，auto 路由按文件内容而非
扩展名分派；Topos 误探测在逐后端回退中自愈（topos open 失败 → ffmpeg）。

## 硬件加速策略（P9.A）

| 平台 | 首选 | fallback |
| macOS | FFmpeg VideoToolbox | PyAV CPU |
| Windows NVIDIA | NVDEC/D3D11VA 硬解优先 | PyAV CPU |
| Windows AMD/Intel GPU | 默认 CPU（PyAV 每帧同步下载使 D3D11VA/QSV 劣于 CPU 多线程解码，实测见 scripts/bench_hw_decode_windows.py） | TOPOS_FORCE_WIN_HW_DECODE=1 开启硬解 |
| Linux | VAAPI/NVDEC 可选 | PyAV CPU |

硬件加速只作为 DecodePolicy 的一个选项，不改变 MediaSource 外部接口。
如果硬解输出无法安全转 numpy/planes，先 CPU download，不做零拷贝。
记录 hardware decode 是否启用、fallback 原因。

## 设计原则

- 用户可配置首选后端
- 自动检测后端可用性
- 优雅降级，不崩溃
- 硬件失败不会导致加载失败，只降级

## 使用示例

```python
from src.shared.media import MediaSourceFactory, MediaBackend

# 自动选择后端
source = MediaSourceFactory.create('/path/to/video.mp4')

# 强制使用 FFmpeg
source = MediaSourceFactory.create('/path/to/video.mp4', backend='ffmpeg')

# 使用硬件加速（P9.A）
from src.shared.media.decode_policy import DecodeTarget
source, status = MediaSourceFactory.create_with_hwaccel(
    '/path/to/video.mp4',
    decode_target=DecodeTarget.PREVIEW
)
print(status)  # HardwareDecodeStatus(hwaccel=videotoolbox)

# 检查可用后端
backends = MediaSourceFactory.get_available_backends()
```
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, TYPE_CHECKING, Tuple

from .source import MediaSource, MediaOpenError
from .metadata import VideoMetadata

if TYPE_CHECKING:
    from .decode_policy import DecodePolicy, DecodeTarget, HardwareDecodeStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# T-P0-10：媒体后端探测与 probe 结果单一化
# ---------------------------------------------------------------------------

#: 文件签名（size_bytes, mtime_ns）——探测/嗅探缓存的失效键。
FileSignature = Tuple[int, int]


def _file_signature(path: str) -> Optional[FileSignature]:
    """文件签名；文件不可读时返回 None（调用方不缓存）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


@dataclass(frozen=True)
class MediaBackendDecision:
    """detect_media_backend() 的结果：唯一的内容路由判定。

    ``backend`` 取值：``'toos_sequence'``/``'toos_image'``/``'topos'``/
    ``'ffmpeg'``。``reason_code`` 说明判定依据（TPIC/TPIM 内容命中、
    兜底等），供日志与 AI 能力层消费。
    """

    backend: str
    reason_code: str
    detail: str = ""


@dataclass(frozen=True)
class MediaProbeResult:
    """probe_video_metadata_cached() 的结果（可共享、可审计）。

    - ``metadata``：探测得到的元数据；失败为 None。
    - ``backend``：产生（或试图产生）metadata 的后端：
      ``'topos_native'`` / ``'ffmpeg'`` / ``'none'``。
    - ``reason_code``：TOPOS native 不可用时携带库解析三态原因
      （``TOPOS_CODEC_LIB_NOT_FOUND`` / ``TOPOS_CODEC_INCOMPATIBLE``），
      FFmpeg 失败时携带 ``FFMPEG_PROBE_FAILED``；成功为 None。
    """

    path: str
    signature: Optional[FileSignature]
    metadata: Optional[VideoMetadata]
    backend: str
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    probed_at: float = field(default_factory=time.time)


class MediaBackend(Enum):
    """
    媒体后端类型。

    Values:
        AUTO: 自动选择（按内容探测路由 Topos/FFmpeg）
        FFMPEG: 强制使用 FFmpeg (PyAV)
        TOPOS: 强制使用 Topos 自研解码（libtopos_codec）
    """
    AUTO = 'auto'
    FFMPEG = 'ffmpeg'
    TOPOS = 'topos'


class MediaSourceFactory:
    """
    媒体源工厂。

    根据配置和后端可用性创建合适的 MediaSource 实例。
    FFmpeg (PyAV) 为唯一解码后端（GStreamer 后端已移除）。
    """

    # 后端注册表（延迟加载）
    _BACKEND_REGISTRY: dict[str, Callable[[str, Optional[tuple[int, int]]], MediaSource]] = {}

    # 后端可用性缓存
    _BACKEND_AVAILABLE: dict[str, bool] = {}

    # AUD-34612：注册表/可用性缓存的填充与注册加锁（RLock——注册链内
    # check_* 回调会经 _is_backend_available 重入 _ensure_backends_registered）。
    # 命中路径（已注册）保持无锁快读。
    _REGISTRY_LOCK: threading.RLock = threading.RLock()
    _REGISTRY_INITIALIZED: bool = False

    # 用户偏好设置
    _USER_PREFERRED_BACKEND: Optional[str] = None

    # T-P0-10：内容嗅探与 probe 结果缓存（键 = 文件路径，值带文件签名；
    # 签名变化自动失效）。
    _BACKEND_DECISION_CACHE: dict[str, tuple[FileSignature, MediaBackendDecision]] = {}
    _PROBE_RESULT_CACHE: dict[str, tuple[FileSignature, MediaProbeResult]] = {}
    # native 失败只记录一次（键 = path + signature + reason_code）。
    # AUD-34602：集合无上限且签名失效只覆盖旧值不删条目——改为有序
    # dict 充当有界有序集合，经 _cache_put 统一淘汰。
    _NATIVE_FAILURE_LOGGED: dict[tuple[str, FileSignature, str], None] = {}
    # AUD-34602：上述类级缓存的容量上限（插入序近似 LRU，命中经
    # _cache_put 重插刷新）；0 表示不限制。长会话浏览/导入上千素材或
    # AI 批量扫描目录时防内存单调增长。
    _CACHE_MAX_ENTRIES: int = 4096

    @classmethod
    def _cache_put(cls, cache: dict, key, value) -> None:
        """向类级缓存写入并按容量淘汰最旧条目（AUD-34602）。

        命中路径的重插（先删后插）会刷新插入序，实现近似 LRU。
        """
        if key in cache:
            del cache[key]
        cache[key] = value
        max_entries = cls._CACHE_MAX_ENTRIES
        if max_entries > 0:
            excess = len(cache) - max_entries
            if excess > 0:
                for oldest in list(cache)[:excess]:
                    del cache[oldest]

    @classmethod
    def detect_media_backend(cls, path: str) -> MediaBackendDecision:
        """唯一的内容路由判定入口（T-P0-10 步骤 1）。

        先按容器/codec 标识（TPIC/TPIM magic）判断 TOPOS 家族，再兜底
        FFmpeg；禁止只按扩展名猜测。结果带文件签名缓存。
        """
        sig = _file_signature(path)
        if sig is not None:
            cached = cls._BACKEND_DECISION_CACHE.get(path)
            if cached is not None and cached[0] == sig:
                # AUD-34602：命中重插刷新插入序（近似 LRU）
                cls._cache_put(cls._BACKEND_DECISION_CACHE, path, cached)
                return cached[1]
        decision = cls._detect_media_backend_uncached(path)
        if sig is not None:
            cls._cache_put(cls._BACKEND_DECISION_CACHE, path, (sig, decision))
        return decision

    @classmethod
    def _detect_media_backend_uncached(cls, path: str) -> MediaBackendDecision:
        # 虚拟素材没有磁盘文件，直接短路（与 create() 的拒绝一致）。
        if isinstance(path, str) and path.startswith("topos://"):
            return MediaBackendDecision(
                backend="ffmpeg", reason_code="VIRTUAL_ASSET_NO_FILE",
                detail=path,
            )
        # Topos Image 路由：TPIM magic 内容探测（.toos 扩展名仅快速候选）。
        try:
            from .format_registry import is_toos_image
            if is_toos_image(path):
                from .topos_image_sequence import parse_frame_pattern, scan_sequence
                parsed = parse_frame_pattern(path)
                if parsed is not None:
                    prefix, padding, suffix = parsed
                    if suffix.lower() == ".toos":
                        slots = scan_sequence(
                            os.path.dirname(path), prefix, padding, suffix)
                        if len(slots) > 1:
                            return MediaBackendDecision(
                                backend="toos_sequence",
                                reason_code="TOOS_SEQUENCE_CONTENT_MATCH",
                            )
                return MediaBackendDecision(
                    backend="toos_image", reason_code="TOOS_IMAGE_CONTENT_MATCH")
        except Exception as exc:  # noqa: BLE001 — 嗅探失败不阻断后续路由
            logger.debug("toos image sniff failed for %s: %s", path, exc)
        # Topos MOV 路由：TPIC 结构化内容探测（'.mov' 与 QuickTime 冲突，
        # 不能按扩展名；误探测由逐后端回退自愈）。
        try:
            from .topos_source import is_topos_movie
            if is_topos_movie(str(path)):
                return MediaBackendDecision(
                    backend="topos", reason_code="TOPOS_TPIC_CONTENT_MATCH")
        except Exception as exc:  # noqa: BLE001
            logger.debug("topos movie sniff failed for %s: %s", path, exc)
        return MediaBackendDecision(
            backend="ffmpeg", reason_code="FFMPEG_GENERIC_FALLBACK")

    @classmethod
    def register_backend(
        cls,
        name: str,
        factory_func: Callable[[str, Optional[tuple[int, int]]], MediaSource],
        check_available: Optional[Callable[[], bool]] = None
    ) -> None:
        """
        注册一个媒体后端。

        Args:
            name: 后端名称（如 'ffmpeg'）
            factory_func: 创建 MediaSource 的工厂函数
            check_available: 检查后端是否可用的函数（可选）
        """
        # AUD-34612：注册写入持锁，避免并发 check-then-set 竞态
        with cls._REGISTRY_LOCK:
            cls._BACKEND_REGISTRY[name] = factory_func
            if check_available:
                cls._BACKEND_AVAILABLE[name] = check_available()

    @classmethod
    def set_user_preferred_backend(cls, backend: str) -> None:
        """
        设置用户偏好后端。

        Args:
            backend: 后端名称 ('auto', 'ffmpeg')
        """
        cls._USER_PREFERRED_BACKEND = backend

    # 解释 no_deblock（预览跳过输出去块）的后端：仅 Topos native 平面路径。
    _NO_DEBLOCK_BACKENDS = frozenset({'topos'})

    @classmethod
    def create(
        cls,
        path: str,
        backend: str = 'auto',
        target_size: Optional[tuple[int, int]] = None,
        preserve_planes: bool = False,
        auto_open: bool = False,
        no_deblock: bool = False
    ) -> MediaSource:
        """
        创建媒体源（不自动打开）。

        Args:
            path: 媒体文件路径
            backend: 后端选择 ('auto', 'ffmpeg', 'topos')
            target_size: 可选的目标尺寸 (width, height)，用于代理缩放
            preserve_planes: 是否保留 planar 格式（避免 CPU RGB 转换）。
                             当 True 且源像素格式是 YUV 时，输出 planar DecodedFrame。
            auto_open: 是否自动打开（默认 False，调用方应显式调用 open_source()）

        Returns:
            MediaSource 实例（未打开状态）

        Raises:
            MediaOpenError: 所有后端都无法创建文件
            ValueError: 无效的后端名称

        Note:
            P3.A: create() 现在只创建不 open，避免重复 open 问题。
            调用方应显式调用 open_source() 或 source.open()。
            P3.C: preserve_planes 参数用于 v3 proxy 播放，让 YUV 素材直接输出 planar。

        backend='auto' 逻辑：
        1. 检查用户配置的偏好后端
        2. 检查文件格式支持
        3. 检查后端可用性
        4. 选择最优后端
        5. 失败时 fallback 到下一个可用后端
        """
        # 验证路径
        if not path or not os.path.exists(path):
            raise MediaOpenError(f"文件不存在: {path}", source_path=path)

        # 虚拟素材（topos:// scheme）没有磁盘文件，无法用任何后端解码 —— 提前
        # 拒绝，给出明确错误而非让 PyAV 抛 ProtocolNotFoundError。这些素材
        # （adjustment/solid/comp_clip/nested_sequence）的内容由渲染层程序化生成。
        if isinstance(path, str) and path.startswith("topos://"):
            raise MediaOpenError(
                f"虚拟素材不可解码（无磁盘文件）: {path}", source_path=path
            )

        # 确定使用的后端
        selected_backend = cls._select_backend(path, backend)

        # 尝试创建媒体源
        errors: list[str] = []

        for backend_name in cls._get_backend_order(selected_backend, path):
            source: Optional[MediaSource] = None
            try:
                source = cls._create_with_backend(backend_name, path, target_size,
                                                  preserve_planes, no_deblock)
                logger.info(f"MediaSourceFactory: 使用后端 {backend_name} 创建 {path}, preserve_planes={preserve_planes}")
                # P3.A: 不自动 open，由调用方决定何时打开
                if auto_open:
                    source.open()
                return source
            except Exception as e:
                # AUD-34601：open() 失败时已创建（可能部分打开）的 source
                # 做 best-effort close 再切换下一后端——与 open_source()
                # 的清理行为对齐，避免多后端回退 + 批量加载时累积泄漏。
                if source is not None:
                    try:
                        source.close()
                    except Exception:  # noqa: BLE001 — best-effort 清理
                        pass
                errors.append(f"{backend_name}: {str(e)}")
                logger.warning(f"后端 {backend_name} 失败: {e}")
                continue

        # 所有后端都失败
        error_msg = f"无法创建媒体源 {path}。尝试的后端: {', '.join(errors)}"
        raise MediaOpenError(error_msg, source_path=path)

    @classmethod
    def _select_backend(cls, path: str, backend: str) -> str:
        """
        选择后端。

        Args:
            path: 文件路径
            backend: 请求的后端

        Returns:
            最终选择的后端名称
        """
        # 如果指定了具体后端，直接使用
        if backend == 'ffmpeg':
            return backend
        if backend == 'topos':
            return backend
        if backend == 'gstreamer':
            # GStreamer 后端已移除：显式请求时回退 auto 并告警
            logger.warning("GStreamer media backend has been removed; using 'auto' (FFmpeg)")
            return 'auto'

        if backend != 'auto':
            # AUD-34603：未识别的 backend 名称（如拼写错误 'ffmgeg'）按
            # create() docstring 契约抛 ValueError，不再静默按 auto 处理
            raise ValueError(
                f"无效的后端名称: {backend!r}（可选: 'auto', 'ffmpeg', 'topos'）"
            )

        # auto 模式：检查用户偏好
        if cls._USER_PREFERRED_BACKEND and cls._USER_PREFERRED_BACKEND != 'auto':
            if cls._is_backend_available(cls._USER_PREFERRED_BACKEND):
                return cls._USER_PREFERRED_BACKEND

        # auto 模式：根据文件格式和后端可用性选择
        return 'auto'

    @classmethod
    def _get_backend_order(
        cls,
        selected_backend: str,
        path: Optional[str] = None,
    ) -> list[str]:
        """
        获取后端尝试顺序。

        Args:
            selected_backend: 选择的后端
            path: 媒体文件路径（auto 模式下做 Topos 内容探测路由，R-17）

        Returns:
            后端尝试顺序列表
        """
        if selected_backend == 'auto':
            order: list[str] = []
            # T-P0-10：唯一内容路由判定（TPIC/TPIM magic，带签名缓存）。
            if path is not None:
                decision = cls.detect_media_backend(path)
                if decision.backend == 'toos_sequence':
                    if cls._is_backend_available('toos_sequence'):
                        order.append('toos_sequence')
                    if cls._is_backend_available('toos_image'):
                        order.append('toos_image')
                elif decision.backend == 'toos_image':
                    if cls._is_backend_available('toos_image'):
                        order.append('toos_image')
                elif decision.backend == 'topos' and cls._is_backend_available('topos'):
                    order.append('topos')
            if cls._is_backend_available('ffmpeg'):
                order.append('ffmpeg')
            return order if order else ['ffmpeg']  # 即使不可用也尝试
        else:
            # 指定模式：只尝试指定后端
            return [selected_backend]

    @classmethod
    def _create_with_backend(
        cls,
        backend_name: str,
        path: str,
        target_size: Optional[tuple[int, int]],
        preserve_planes: bool = False,
        no_deblock: bool = False
    ) -> MediaSource:
        """
        使用指定后端创建媒体源（不打开）。

        Args:
            backend_name: 后端名称
            path: 文件路径
            target_size: 目标尺寸
            preserve_planes: 是否保留 planar 格式

        Returns:
            MediaSource 实例（未打开状态）

        Raises:
            MediaOpenError: 创建失败

        Note:
            P3.A: 只创建不打开，由调用方或 create() 的 auto_open 参数决定。
            P3.C: preserve_planes 参数传递给 FFmpegMediaSource。
        """
        # 确保后端已注册
        cls._ensure_backends_registered()

        if backend_name not in cls._BACKEND_REGISTRY:
            raise ValueError(f"未知的后端: {backend_name}")

        factory_func = cls._BACKEND_REGISTRY[backend_name]
        # 后端专属选项：no_deblock = 预览跳过输出去块（仅 Topos native 平面
        # 路径解释；其它后端闭合签名不含该参数）。显式白名单而非签名反射。
        extra: dict = {}
        if no_deblock and backend_name in cls._NO_DEBLOCK_BACKENDS:
            extra['no_deblock'] = True
        source = factory_func(path, target_size, preserve_planes, **extra)

        # P3.A: 不再自动 open，直接返回创建的 source
        return source

    @classmethod
    def open_source(cls, source: MediaSource) -> MediaSource:
        """
        打开媒体源。

        Args:
            source: 未打开的 MediaSource 实例

        Returns:
            打开后的 MediaSource 实例

        Raises:
            MediaOpenError: 打开失败

        Note:
            P3.A: 配合 create() 使用，实现创建和打开分离。
            如果打开失败，会尝试清理资源。
        """
        try:
            source.open()
            return source
        except Exception as e:
            # 创建成功但打开失败，清理资源
            if hasattr(source, 'close'):
                try:
                    source.close()
                except Exception:
                    pass
            raise MediaOpenError(
                f"打开媒体源失败: {e}",
                source_path=source.path if hasattr(source, 'path') else None
            )

    @classmethod
    def _ensure_backends_registered(cls) -> None:
        """
        确保所有后端已注册。

        延迟注册，避免导入循环依赖。
        """
        # AUD-34612：命中路径无锁快读；首次调用持锁完成一次性注册，
        # 避免并发 check-then-set 重复注册/重复探测。RLock 允许注册链内
        # check_* 回调经 _is_backend_available 重入。
        if 'ffmpeg' in cls._BACKEND_REGISTRY:
            return
        with cls._REGISTRY_LOCK:
            if cls._REGISTRY_INITIALIZED:
                return
            if 'ffmpeg' not in cls._BACKEND_REGISTRY:
                # 注册 FFmpeg 后端（使用已实现的 FFmpegMediaSource）
                # P3.B: FFmpegMediaSource 现已支持 target_size 参数
                # P3.C: FFmpegMediaSource 现已支持 preserve_planes 参数
                def create_ffmpeg(
                    path: str,
                    target_size: Optional[tuple[int, int]],
                    preserve_planes: bool = False
                ) -> MediaSource:
                    from .ffmpeg_source import FFmpegMediaSource
                    return FFmpegMediaSource(
                        path,
                        target_size=target_size,
                        preserve_planes=preserve_planes
                    )

                def check_ffmpeg_available() -> bool:
                    from .probe import check_ffmpeg_available as _check
                    return _check()

                cls.register_backend('ffmpeg', create_ffmpeg, check_ffmpeg_available)

            if 'topos' not in cls._BACKEND_REGISTRY:
                # 注册 Topos 自研解码后端（阶段 6；libtopos_codec 未构建时不可用，
                # auto 路由自动跳过，显式指定则由 create() 报出明确错误）
                def create_topos(
                    path: str,
                    target_size: Optional[tuple[int, int]],
                    preserve_planes: bool = False,
                    no_deblock: bool = False
                ) -> MediaSource:
                    from .topos_source import ToposMediaSource
                    return ToposMediaSource(
                        path,
                        target_size=target_size,
                        preserve_planes=preserve_planes,
                        no_deblock=no_deblock,
                    )

                def check_topos_available() -> bool:
                    from .topos_source import check_topos_codec_available
                    return check_topos_codec_available()

                cls.register_backend('topos', create_topos, check_topos_available)

            if 'toos_image' not in cls._BACKEND_REGISTRY:
                # Topos Image 静态图后端（Topos Image 阶段 5；libtopos_codec 未
                # 构建时不可用）。仅路由 TPIM magic 内容，'.topos' 工程包不受影响。
                def create_toos_image(
                    path: str,
                    target_size: Optional[tuple[int, int]],
                    preserve_planes: bool = False
                ) -> MediaSource:
                    from .topos_image_source import ToposImageSource
                    return ToposImageSource(path)

                def check_toos_image_available() -> bool:
                    try:
                        from ..codec.topos_image_binding import (
                            check_image_codec_available)
                        return check_image_codec_available()
                    except Exception:  # noqa: BLE001 —— 依赖缺失按不可用处理
                        return False

                cls.register_backend('toos_image', create_toos_image,
                                     check_toos_image_available)

            if 'toos_sequence' not in cls._BACKEND_REGISTRY:
                # Numeric .toos sequences reuse the same image codec and expose the
                # regular MediaSource contract, so DecodeQueue/UI need no special
                # playback branch.
                def create_toos_sequence(
                    path: str,
                    target_size: Optional[tuple[int, int]],
                    preserve_planes: bool = False
                ) -> MediaSource:
                    from .topos_image_sequence import SequenceOptions, open_sequence
                    return open_sequence(
                        path, SequenceOptions(target_size=target_size))

                def check_toos_sequence_available() -> bool:
                    return cls._is_backend_available('toos_image')

                cls.register_backend('toos_sequence', create_toos_sequence,
                                     check_toos_sequence_available)

            cls._REGISTRY_INITIALIZED = True
    @classmethod
    def _is_backend_available(cls, backend_name: str) -> bool:
        """
        检查后端是否可用。

        Args:
            backend_name: 后端名称

        Returns:
            是否可用
        """
        cls._ensure_backends_registered()
        return cls._BACKEND_AVAILABLE.get(backend_name, False)

    @classmethod
    def get_available_backends(cls) -> list[str]:
        """
        返回可用后端列表。

        Returns:
            可用后端名称列表
        """
        cls._ensure_backends_registered()
        return [
            name for name, available in cls._BACKEND_AVAILABLE.items()
            if available
        ]

    @classmethod
    def get_preferred_backend(cls) -> str:
        """
        根据配置返回首选后端。

        Returns:
            首选后端名称
        """
        # 优先用户设置
        if cls._USER_PREFERRED_BACKEND:
            return cls._USER_PREFERRED_BACKEND

        # 根据可用性自动选择
        available = cls.get_available_backends()
        if 'ffmpeg' in available:
            return 'ffmpeg'

        # 默认 auto
        return 'auto'

    @classmethod
    def _probe_video_metadata_ffmpeg(cls, path: str) -> Optional[VideoMetadata]:
        """
        使用 FFmpeg/PyAV 快速探测视频元数据（优先获取准确的 codec 信息）。

        直接使用 PyAV 探测，能获取准确的 codec_name 和 pixel_format。
        仅在需要 codec 信息（如 zero-copy gate）时使用。

        Args:
            path: 视频文件路径

        Returns:
            VideoMetadata 或 None（失败）
        """
        try:
            from .probe import check_ffmpeg_available
            if not check_ffmpeg_available():
                return None

            import av
            from .metadata import Rational

            container = av.open(path)
            try:
                for stream in container.streams:
                    if stream.type == 'video':
                        width = stream.width
                        height = stream.height

                        # 编解码器（PyAV 提供准确的 codec name）
                        codec_name = 'unknown'
                        if hasattr(stream, 'codec') and stream.codec:
                            codec_name = stream.codec.name
                        elif hasattr(stream, 'codec_context') and stream.codec_context:
                            codec_name = stream.codec_context.name

                        # 像素格式。禁止直接读 stream.pix_fmt：PyAV 17 的
                        # getter 对 AV_PIX_FMT_NONE（文件畸形/无任何帧解析，
                        # 如按扩展名被 image2 认领的占位文件）不做守卫——
                        # av_pix_fmt_desc_get(-1) 返回 NULL 后直接解引用，
                        # C 层段崩溃，try/except 与 hasattr 均无法拦截。
                        # stream.format 是库自身的安全通道（NONE → None，
                        # VideoStream.__repr__ 同款守卫）；真实文件上
                        # format.name ≡ pix_fmt（实测 rgb24 一致）。
                        fmt = getattr(stream, "format", None)
                        pix_fmt = fmt.name if fmt is not None else 'unknown'

                        # 位深度
                        bit_depth = 8
                        if pix_fmt:
                            if '10le' in pix_fmt or 'p010' in pix_fmt.lower():
                                bit_depth = 10
                            elif '12le' in pix_fmt or '12be' in pix_fmt:
                                bit_depth = 12
                            elif '16le' in pix_fmt or '16be' in pix_fmt:
                                bit_depth = 16

                        # 帧率
                        fps_value = 24.0
                        if hasattr(stream, 'average_rate') and stream.average_rate:
                            fps_value = float(stream.average_rate)
                        elif hasattr(stream, 'base_rate') and stream.base_rate:
                            fps_value = float(stream.base_rate)
                        fps_rational = Rational.from_float(fps_value, max_den=1001)

                        # 总帧数
                        total_frames = 0
                        if hasattr(stream, 'frames') and stream.frames:
                            total_frames = stream.frames

                        # Codec profile
                        codec_profile = ''
                        if hasattr(stream, 'codec_context') and stream.codec_context:
                            ctx = stream.codec_context
                            if hasattr(ctx, 'profile') and ctx.profile:
                                codec_profile = str(ctx.profile)

                        return VideoMetadata(
                            width=width,
                            height=height,
                            fps=fps_rational,
                            total_frames=total_frames,
                            codec_name=codec_name,
                            codec_profile=codec_profile or None,
                            pixel_format=pix_fmt,
                            bit_depth=bit_depth,
                        )
            finally:
                container.close()
        except Exception as e:
            logger.debug(f"FFmpeg 快速探测失败: {e}")

        return None

    @classmethod
    def probe_video_metadata(cls, path: str) -> Optional[VideoMetadata]:
        """快速探测视频元数据（不打开解码器）。

        T-P0-10 probe 单一化：
        1. TOPOS (TPIC) 文件 → metadata **只**来自 ``ToposMediaSource.probe()``，
           不再先走必定失败的 FFmpeg/PyAV probe（消除
           ``'VideoStream' object has no attribute 'width'`` 无效探测）。
           native 不可用时记录一次结构化失败（库解析三态原因），再按
           fallback 策略尝试 FFmpeg。
        2. 非 TOPOS 文件 → 优先 FFmpeg/PyAV，失败时回退 MediaSource 完整探测。
        3. 结果带文件签名缓存（:meth:`probe_video_metadata_cached`），由
           import、缩略图、导出、Composite、AI 分析共享。
        """
        return cls.probe_video_metadata_cached(path).metadata

    @classmethod
    def probe_video_metadata_cached(cls, path: str) -> MediaProbeResult:
        """带共享缓存的探测（T-P0-10 步骤 2/3/4）。

        缓存键为路径，文件签名（size, mtime_ns）变化自动失效；同一签名的
        重复探测直接命中缓存，保证"每个调用只产生一次 native probe"。
        """
        if isinstance(path, str) and path.startswith("topos://"):
            return MediaProbeResult(
                path=path, signature=None, metadata=None, backend="none",
                reason_code="VIRTUAL_ASSET_NO_FILE",
            )
        sig = _file_signature(path)
        if sig is not None:
            cached = cls._PROBE_RESULT_CACHE.get(path)
            if cached is not None and cached[0] == sig:
                # AUD-34602：命中重插刷新插入序（近似 LRU）
                cls._cache_put(cls._PROBE_RESULT_CACHE, path, cached)
                return cached[1]

        result = cls._probe_video_metadata_uncached(path, sig)
        if sig is not None:
            cls._cache_put(cls._PROBE_RESULT_CACHE, path, (sig, result))
        return result

    @classmethod
    def _probe_video_metadata_uncached(
        cls, path: str, sig: Optional[FileSignature],
    ) -> MediaProbeResult:
        decision = cls.detect_media_backend(path)
        if decision.backend == "topos":
            return cls._probe_topos_native(path, sig)
        # 非 TOPOS：FFmpeg/PyAV 优先（提供准确 codec_name/pixel_format）。
        metadata = cls._probe_video_metadata_ffmpeg(path)
        if metadata is not None:
            return MediaProbeResult(
                path=path, signature=sig, metadata=metadata, backend="ffmpeg",
            )
        # FFmpeg 不可用：使用 MediaSource 完整探测（作为 fallback）。
        source: Optional[MediaSource] = None
        try:
            source = cls.create(path)
            source.open()
            metadata = source.probe()
        except Exception as e:  # noqa: BLE001 — 完整探测失败按原语义返回 None
            logger.warning(f"完整探测失败: {e}")
            return MediaProbeResult(
                path=path, signature=sig, metadata=None, backend="none",
                reason_code="FULL_PROBE_FAILED", detail=str(e),
            )
        finally:
            # AUD-34600：open() 成功后 probe() 抛异常时 close() 被跳过，
            # 已打开的 PyAV 容器滞留至 GC——对齐 _probe_topos_native 的
            # finally close 模式（批量探测损坏文件时不再累积泄漏）。
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001 — best-effort 清理
                    pass
        backend = "topos_native" if decision.backend in (
            "topos", "toos_image", "toos_sequence") else "ffmpeg"
        return MediaProbeResult(
            path=path, signature=sig, metadata=metadata, backend=backend,
        )

    @classmethod
    def _probe_topos_native(
        cls, path: str, sig: Optional[FileSignature],
    ) -> MediaProbeResult:
        """TOPOS 文件的 native-only 探测（metadata 只来自 ToposMediaSource）。"""
        try:
            from .topos_source import ToposMediaSource
        except Exception as exc:  # noqa: BLE001
            return cls._topos_native_failure(
                path, sig, "TOPOS_SOURCE_IMPORT_FAILED", str(exc))
        source = ToposMediaSource(str(path))
        try:
            source.open()
            metadata = source.probe()
        except Exception as exc:  # noqa: BLE001 — 库缺失/不兼容/文件损坏分流
            reason, detail = cls._classify_topos_open_failure(exc)
            return cls._topos_native_failure(path, sig, reason, detail)
        finally:
            try:
                source.close()
            except Exception:  # noqa: BLE001 — close 失败不影响探测结果
                pass
        return MediaProbeResult(
            path=path, signature=sig, metadata=metadata, backend="topos_native",
        )

    @staticmethod
    def _classify_topos_open_failure(exc: Exception) -> tuple[str, str]:
        """把 ToposMediaSource 打开失败归类为稳定 reason_code（T-P0-10 步骤 6）。"""
        from src.shared.codec.topos_binding import (
            LIB_RESOLUTION_INCOMPATIBLE,
            LIB_RESOLUTION_NOT_FOUND,
            resolve_topos_codec_library,
        )
        text = str(exc)
        resolution = resolve_topos_codec_library()
        if resolution.status == LIB_RESOLUTION_NOT_FOUND:
            return "TOPOS_CODEC_LIB_NOT_FOUND", (
                resolution.detail or text)
        if resolution.status == LIB_RESOLUTION_INCOMPATIBLE:
            return "TOPOS_CODEC_INCOMPATIBLE", (
                resolution.detail or text)
        # 库可用但该文件打不开：真实文件级失败
        return "TOPOS_FILE_OPEN_FAILED", text

    @classmethod
    def _topos_native_failure(
        cls, path: str, sig: Optional[FileSignature],
        reason_code: str, detail: str,
    ) -> MediaProbeResult:
        """native 失败：结构化原因只记录一次（同一文件签名+原因），不刷屏。"""
        key = (path, sig, reason_code)
        if key not in cls._NATIVE_FAILURE_LOGGED:
            cls._cache_put(cls._NATIVE_FAILURE_LOGGED, key, None)
            logger.warning(
                "TOPOS native probe unavailable for %s [%s]: %s",
                path, reason_code, detail,
            )
        # 按 fallback 策略尝试 FFmpeg（旧 dylib 场景：PyAV 可开容器但无
        # 解码器 → metadata=None + 结构化原因，绝不伪装成功）。
        metadata = cls._probe_video_metadata_ffmpeg(path)
        backend = "ffmpeg" if metadata is not None else "none"
        return MediaProbeResult(
            path=path, signature=sig, metadata=metadata, backend=backend,
            reason_code=reason_code, detail=detail,
        )

    @classmethod
    def clear_media_probe_cache(cls) -> None:
        """清空探测/嗅探缓存与一次性失败记录（测试与环境变化用）。"""
        cls._PROBE_RESULT_CACHE.clear()
        cls._BACKEND_DECISION_CACHE.clear()
        cls._NATIVE_FAILURE_LOGGED.clear()

    @classmethod
    def is_video_file(cls, path: str) -> bool:
        """
        判断文件是否为支持的视频格式。

        Args:
            path: 文件路径

        Returns:
            是否为视频文件
        """
        from src.shared.media.format_registry import is_video
        return is_video(path)

    # ================================================================
    # P9.A: 硬件加速支持
    # ================================================================

    @classmethod
    def create_with_hwaccel(
        cls,
        path: str,
        decode_target: 'DecodeTarget',
        backend: str = 'auto',
        target_size: Optional[Tuple[int, int]] = None,
        preserve_planes: bool = False,
        auto_open: bool = False
    ) -> Tuple[MediaSource, 'HardwareDecodeStatus']:
        """
        创建媒体源（带硬件加速状态跟踪）。

        根据 DecodeTarget 选择合适的解码策略：
        - PREVIEW: 尝试使用硬件加速
        - MASTER/EXPORT: 使用 CPU 解码保证质量

        Args:
            path: 媒体文件路径
            decode_target: 解码目标类型
            backend: 后端选择 ('auto', 'ffmpeg')
            target_size: 可选的目标尺寸（用于 proxy）
            preserve_planes: 是否保留 planar 格式（避免 CPU RGB 转换）。
                             当 True 且源像素格式是 YUV 时，输出 planar DecodedFrame；
                             Windows 的 NVDEC/D3D11VA 预览会使用经过首帧验证的
                             NV12/P010 硬件下载路径。
            auto_open: 是否自动打开

        Returns:
            (MediaSource 实例, HardwareDecodeStatus 状态)

        Raises:
            MediaOpenError: 所有后端都无法创建文件

        Note:
            P9.A: 硬件加速作为 DecodePolicy 的选项，不改变 MediaSource 外部接口。
            硬件失败不会导致加载失败，只降级到 CPU。
            P3.C: preserve_planes 参数用于 v3 proxy 播放，让 YUV 素材直接输出 planar。
        """
        from .decode_policy import DecodePolicyManager, HardwareDecodeStatus

        # 获取解码策略
        policy = DecodePolicyManager.get_policy(decode_target)

        # 验证路径
        if not path or not os.path.exists(path):
            raise MediaOpenError(f"文件不存在: {path}", source_path=path)

        # 虚拟素材（topos:// scheme）不可硬件解码 —— 与 create() 一致提前拒绝。
        if isinstance(path, str) and path.startswith("topos://"):
            raise MediaOpenError(
                f"虚拟素材不可解码（无磁盘文件）: {path}", source_path=path
            )

        # 初始化状态
        status = HardwareDecodeStatus()

        # 尝试创建带硬件加速的源
        if policy.enable_hwaccel:
            source, status = cls._try_create_hwaccel_source(
                path, policy, target_size, backend, preserve_planes
            )
            if source is not None:
                logger.info(
                    f"MediaSourceFactory: 使用硬件解码 {status.decoder_type} "
                    f"创建 {path}"
                )
                if auto_open:
                    try:
                        source.open()
                    except Exception as exc:  # noqa: BLE001 - hardware must fail soft
                        # A hardware session may fail only when the first
                        # decoder frame is requested (driver/profile/format).
                        # Preview loading must continue with CPU decode.
                        logger.warning(
                            "MediaSourceFactory: hardware open failed for %s; "
                            "falling back to CPU: %s",
                            path,
                            exc,
                        )
                        try:
                            source.close()
                        except Exception:  # noqa: BLE001 - best-effort cleanup
                            pass
                        status.hwaccel_enabled = False
                        status.decoder_type = 'cpu'
                        status.fallback_occurred = True
                        status.fallback_reason = (
                            f"hwaccel_open_failed:{type(exc).__name__}:{exc}"
                        )
                    else:
                        return source, status
                else:
                    return source, status

        # 硬件加速不可用或策略不允许，使用 CPU 解码
        source = cls.create(
            path,
            backend=backend,
            target_size=target_size,
            preserve_planes=preserve_planes,
            auto_open=False,
        )
        status.hwaccel_enabled = False
        status.decoder_type = 'cpu'
        status.fallback_occurred = policy.enable_hwaccel  # 如果策略启用了硬解但不可用
        if policy.enable_hwaccel and not status.fallback_reason:
            status.fallback_reason = 'hwaccel_not_available_or_codec_not_supported'

        if auto_open:
            source.open()

        logger.info(f"MediaSourceFactory: 使用 CPU 解码创建 {path}")
        return source, status

    @classmethod
    def _try_create_hwaccel_source(
        cls,
        path: str,
        policy: 'DecodePolicy',
        target_size: Optional[Tuple[int, int]],
        backend: str,
        preserve_planes: bool = False
    ) -> Tuple[Optional[MediaSource], 'HardwareDecodeStatus']:
        """
        尝试创建带硬件加速的媒体源。

        根据 P9.A 策略表选择解码器：
        | 平台 | 首选 | fallback |
        | macOS | FFmpeg VideoToolbox | PyAV CPU |
        | Windows NVIDIA | NVDEC/D3D11VA 硬解优先 | PyAV CPU |
        | Windows AMD/Intel GPU | 默认 CPU（TOPOS_FORCE_WIN_HW_DECODE=1 开启硬解） | PyAV CPU |
        | Linux | VAAPI/NVDEC 可选 | PyAV CPU |

        Args:
            path: 媒体文件路径
            policy: 解码策略
            target_size: 目标尺寸
            backend: 后端选择
            preserve_planes: 是否保留 planar 格式

        Returns:
            (MediaSource 或 None, HardwareDecodeStatus)

        Note:
            如果硬解输出无法安全转 numpy/planes，先 CPU download，不做零拷贝。
            Windows planar 预览由专用源负责首帧格式验证；其余硬解源使用
            packed RGB 合约。
        """
        from .decode_policy import HardwareDecodeStatus
        from .hwaccel import (
            HardwareCapability,
            HardwareDecoder,
            HardwareDecodeConfig,
            FFmpegHwaccelSource,
            HWACCEL_RECOMMENDED_CODECS,
        )

        status = HardwareDecodeStatus()

        # Phase D: 优先尝试 macOS VT 零拷贝路径
        import sys
        if sys.platform == 'darwin':
            try:
                from .macos_vt.source import MacOSVtZeroCopySource
                from .macos_vt.capability import is_zero_copy_available

                metadata = cls.probe_video_metadata(path) if path else None
                codec = metadata.codec_name.lower() if metadata else ''

                if is_zero_copy_available(codec=codec):
                    try:
                        vt_source = MacOSVtZeroCopySource()
                        if vt_source.open(path):
                            status.hwaccel_enabled = True
                            status.decoder_type = 'macos_vt_zero_copy'
                            status.codec_supported = True
                            status.decode_path = 'macos_vt_iosurface_zero_copy'
                            logger.info(
                                f"MediaSourceFactory: macOS VT zero-copy source "
                                f"created for {path}, codec={codec}"
                            )
                            return vt_source, status
                        else:
                            reason = vt_source.fallback_reason or "unknown"
                            logger.info(
                                f"MediaSourceFactory: macOS VT zero-copy not available "
                                f"for {path}: {reason}, falling back"
                            )
                            vt_source.close()
                    except Exception as e:
                        logger.debug(f"MediaSourceFactory: macOS VT zero-copy attempt failed: {e}")
            except Exception as e:
                logger.debug(f"MediaSourceFactory: macos_vt module not available or failed: {e}")

        # 确保使用 FFmpeg 后端（硬件加速需要 PyAV）
        # 先探测 codec 类型
        try:
            metadata = cls.probe_video_metadata(path)
            if metadata is None:
                status.fallback_reason = 'metadata_probe_failed'
                return None, status

            codec = metadata.codec_name.lower()

            # 检查 codec 是否推荐硬件解码
            # H.264/H.265/VP9 等 GOP codec 推荐硬解
            # ProRes/DNxHR 等 Intra codec CPU 多线程效率好，不推荐硬解
            is_recommended = codec in HWACCEL_RECOMMENDED_CODECS or \
                            any(c in codec for c in ('h264', 'h265', 'hevc', 'vp9', 'av1'))

            if not is_recommended:
                logger.debug(f"Codec {codec} 不推荐硬件解码，使用 CPU")
                status.fallback_reason = 'codec_not_recommended_for_hwaccel'
                return None, status

            # 探测可用的硬件解码器
            available_decoders = HardwareCapability.detect_available_decoders()

            if not available_decoders:
                status.fallback_reason = 'no_hwaccel_decoder_available'
                return None, status

            # 选择解码器
            decoder: Optional[HardwareDecoder] = None

            if policy.hwaccel_preferred_decoder:
                # 用户指定了首选解码器
                preferred_str = policy.hwaccel_preferred_decoder.lower()
                for hwdec in available_decoders:
                    if hwdec.value == preferred_str:
                        decoder = hwdec
                        break

                if decoder is None:
                    logger.warning(
                        f"用户指定的解码器 {policy.hwaccel_preferred_decoder} 不可用"
                    )
                    # 继续尝试自动选择

            if decoder is None:
                # 自动选择最佳解码器
                decoder = HardwareCapability.get_best_decoder_for_codec(codec)

            if decoder is None:
                # 该 codec 不被任何硬件解码器支持
                status.codec_supported = False
                status.fallback_reason = f'codec_{codec}_not_supported_by_hwaccel'
                return None, status

            # 检查解码器是否支持该 codec
            if not HardwareCapability.check_codec_support(decoder, codec):
                status.codec_supported = False
                status.fallback_reason = f'decoder_{decoder.value}_does_not_support_{codec}'
                return None, status

            # Windows 非 NVIDIA GPU 默认回 CPU 解码（保持 FFmpegHwaccelSource
            # 仅供 NVIDIA / 显式开启者）。基准依据见 scripts/bench_hw_decode_windows.py：
            # PyAV HWAccel 每帧同步 GPU→CPU 下载使 D3D11VA 硬解 4K H.264 仅
            # ~47fps，而 CPU frame-threaded 解码 ~486fps（AMD RX 6800 + 24 线程
            # CPU 实测）。用户显式指定解码器（hwaccel_preferred_decoder）时尊重
            # 其选择；TOPOS_FORCE_WIN_HW_DECODE=1 可强制启用自动硬解。
            if (
                sys.platform == 'win32'
                and not policy.hwaccel_preferred_decoder
                and decoder.value in ('d3d11va', 'qsv')
                and not os.environ.get('TOPOS_FORCE_WIN_HW_DECODE')
            ):
                status.fallback_reason = 'win_non_nvidia_cpu_decode_preferred'
                logger.debug(
                    "MediaSourceFactory: Windows %s 硬解默认让位 CPU 解码 "
                    "(TOPOS_FORCE_WIN_HW_DECODE=1 可覆盖)",
                    decoder.value,
                )
                return None, status

            # Windows + planar preview: keep the decoder's NV12/P010 output
            # instead of routing through FFmpegHwaccelSource's packed RGB
            # conversion.  The latter is a correctness-safe fallback, but it
            # moves a full RGB frame through CPU memory and defeats the GPU YUV
            # upload path used by the preview renderer.  The Windows source
            # verifies the first output frame, so PyAV's silent software
            # fallback is rejected and the caller can use CPU planar decode.
            if (
                sys.platform == 'win32'
                and preserve_planes
                and target_size is None
                and decoder in (HardwareDecoder.NVDEC, HardwareDecoder.D3D11VA)
                and not os.environ.get('TOPOS_DISABLE_WIN_HW_DECODE')
            ):
                try:
                    from .windows_hw.hw_planar_source import WindowsHwPlanarSource

                    source = WindowsHwPlanarSource(path, decoder=decoder)
                    status.hwaccel_enabled = True
                    status.decoder_type = decoder.value
                    status.codec_supported = True
                    status.decode_path = 'hwaccel_download_planar_yuv'
                    logger.info(
                        "MediaSourceFactory: Windows planar hardware source "
                        "selected for %s (decoder=%s)",
                        path,
                        decoder.value,
                    )
                    return source, status
                except Exception as e:  # noqa: BLE001 - CPU fallback below
                    logger.warning(
                        "Windows planar hardware source creation failed: %s", e
                    )
                    status.fallback_occurred = True
                    status.fallback_reason = (
                        f'hwaccel_planar_source_creation_failed:{type(e).__name__}:{e}'
                    )

            # 创建硬件加速配置
            hwaccel_config = HardwareDecodeConfig(
                decoder=decoder,
                fallback_to_cpu=policy.hwaccel_fallback_to_cpu,
                copy_to_cpu=policy.hwaccel_copy_to_cpu,
                prefer_hwaccel_for_gop=True,
            )

            # 创建硬件加速源
            try:
                source = FFmpegHwaccelSource(path, hwaccel_config)

                # 如果有 target_size，需要设置（FFmpegHwaccelSource 继承自 FFmpegMediaSource）
                if target_size is not None:
                    source._target_size = target_size

                status.hwaccel_enabled = True
                status.decoder_type = decoder.value
                status.codec_supported = True

                # Generic hardware source uses the packed RGB contract.  Windows
                # preserve_planes is handled by the verified planar source above.
                # 记录 decode_path 以便调用方了解实际解码路径
                status.decode_path = 'hwaccel_download_packed_rgb'

                # P2-4: Warn when hwaccel forces packed RGB at 4K resolution,
                # because this moves ~33 MB/frame through CPU memory.
                src_w = metadata.width if metadata else 0
                src_h = metadata.height if metadata else 0
                if src_w >= 3840 or src_h >= 2160:
                    logger.warning(
                        f"4K source ({src_w}x{src_h}) using hwaccel_download_packed_rgb "
                        f"path — planar decode with GPU YUV upload would be more efficient. "
                        f"codec={codec}, decoder={decoder.value}"
                    )

                logger.info(
                    f"成功创建硬件加速源: decoder={decoder.value}, "
                    f"codec={codec}, fallback={hwaccel_config.fallback_to_cpu}, "
                    f"decode_path={status.decode_path}"
                )
                return source, status

            except Exception as e:
                logger.warning(f"创建硬件加速源失败: {e}")
                status.fallback_occurred = True
                status.fallback_reason = f'hwaccel_source_creation_failed: {str(e)}'
                return None, status

        except Exception as e:
            logger.warning(f"硬件加速探测失败: {e}")
            status.fallback_reason = f'hwaccel_detection_failed: {str(e)}'
            return None, status

    @classmethod
    def get_hwaccel_status_summary(cls) -> dict:
        """
        获取硬件加速状态摘要。

        Returns:
            包含可用解码器、平台默认等信息
        """
        from .hwaccel import get_hwaccel_status

        # 获取底层 hwaccel 状态
        hwaccel_status = get_hwaccel_status()

        # 添加 factory 层信息
        hwaccel_status['factory_backend_order'] = cls._get_backend_order('auto')
        hwaccel_status['user_preferred_backend'] = cls._USER_PREFERRED_BACKEND

        return hwaccel_status

    @classmethod
    def should_use_hwaccel_for_codec(cls, codec: str) -> bool:
        """
        判断是否应该使用硬件解码指定 codec。

        Args:
            codec: 编解码器名称

        Returns:
            True 表示推荐使用硬件解码
        """
        from .hwaccel import HardwareCapability

        # 检查是否推荐硬解
        if not HardwareCapability.is_hwaccel_recommended(codec):
            return False

        # 检查是否有支持该 codec 的硬件解码器
        decoder = HardwareCapability.get_best_decoder_for_codec(codec)
        return decoder is not None


# ================================================================
# 配置接口（供 ConfigManager 使用）
# ================================================================

def configure_media_backend(backend: str) -> None:
    """
    配置媒体后端偏好。

    Args:
        backend: 后端名称 ('auto', 'ffmpeg', 'topos')
    """
    MediaSourceFactory.set_user_preferred_backend(backend)


def get_current_backend_config() -> str:
    """
    获取当前后端配置。

    Returns:
        当前配置的后端名称
    """
    return MediaSourceFactory.get_preferred_backend()
