"""
Hardware Acceleration Module
硬件解码加速支持。

## 职责

- HardwareDecoder: 硬件解码器类型枚举
- HardwareCapability: 硬件能力探测
- HardwareDecodeConfig: 硬解配置
- FFmpegHwaccelSource: 硬件加速解码源

## 设计原则

- 平台自适应探测（macOS/Windows/Linux）
- 优雅降级到 CPU 解码
- 完整类型注解
- 不依赖 gl_backend.py 和 UI 模块

## 硬解适用场景

- H.264/H.265 长 GOP 编码（高效硬解）
- ProRes/DNxHR Intra 编码（CPU 多线程足够）
- 4K/8K 高分辨率视频（GPU 解码明显更快）
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.shared.ffmpeg_locator import find_ffmpeg

from .ffmpeg_source import (
    FFmpegMediaSource,
    _is_intra_codec,
)
from .frame import DecodedFrame
from .source import MediaOpenError, MediaDecodeError

logger = logging.getLogger(__name__)

# `ffmpeg -hwaccels` 输出缓存（None = 未探测；"" = 不可用）
_cached_hwaccels_output: Optional[str] = None


def _ffmpeg_hwaccels_output() -> str:
    """运行 ``ffmpeg -hwaccels`` 并缓存 stdout（二进制经 ffmpeg_locator 定位）。"""
    global _cached_hwaccels_output
    if _cached_hwaccels_output is not None:
        return _cached_hwaccels_output
    output = ""
    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin is not None:
        try:
            result = subprocess.run(
                [ffmpeg_bin, '-hwaccels'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                output = result.stdout or ""
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            output = ""
    _cached_hwaccels_output = output
    return output


# =============================================================================
# 硬件解码器类型
# =============================================================================

class HardwareDecoder(Enum):
    """
    硬件解码器类型枚举。

    Values 对应 FFmpeg hwaccel 名称。
    """
    VIDEOTOOLBOX = 'videotoolbox'   # macOS VideoToolbox
    NVDEC = 'nvdec'                 # NVIDIA NVDEC (through CUVID)
    VAAPI = 'vaapi'                 # Linux VAAPI (Intel/AMD)
    QSV = 'qsv'                     # Intel Quick Sync Video
    D3D11VA = 'd3d11va'             # Windows DirectX 11
    CUDA = 'cuda'                   # NVIDIA CUDA (legacy, use NVDEC)


# =============================================================================
# 编解码器支持矩阵
# =============================================================================

# 各解码器支持的 codec
HWACCEL_CODEC_SUPPORT: dict[HardwareDecoder, frozenset[str]] = {
    HardwareDecoder.VIDEOTOOLBOX: frozenset({
        'h264', 'hevc', 'h265', 'prores', 'prores_ks', 'vp9'
    }),
    HardwareDecoder.NVDEC: frozenset({
        'h264', 'hevc', 'h265', 'vp9', 'av1', 'mpeg2video', 'mpeg4', 'vc1'
    }),
    HardwareDecoder.VAAPI: frozenset({
        'h264', 'hevc', 'h265', 'vp9', 'mpeg2video', 'mpeg4', 'vc1', 'jpeg'
    }),
    HardwareDecoder.QSV: frozenset({
        'h264', 'hevc', 'h265', 'vp9', 'mpeg2video', 'vc1', 'jpeg'
    }),
    HardwareDecoder.D3D11VA: frozenset({
        'h264', 'hevc', 'h265', 'vp9', 'av1', 'mpeg2video', 'mpeg4', 'vc1'
        # W2-01：av1 进矩阵（FFmpeg 7 有 av1 d3d11va hwaccel；实测是否生效
        # 因驱动而异——AMD RX 6800/驱动 26.8.1 上 PyAV 静默软解，见
        # tests/media/results/windows_capability_matrix.*。硬解未生效时
        # hw_planar_source.open() 首帧验证显式拒绝并回退 CPU，不会误路由。
    }),
    HardwareDecoder.CUDA: frozenset({
        'h264', 'hevc', 'h265', 'mpeg2video', 'mpeg4', 'vc1'
    }),
}

# 硬解效率更高的 codec（推荐使用硬解）
HWACCEL_RECOMMENDED_CODECS: frozenset[str] = frozenset({
    'h264', 'hevc', 'h265', 'vp9', 'av1', 'mpeg2video', 'mpeg4'
})

# Intra codec（CPU 多线程效率好，不推荐硬解）
HWACCEL_NOT_RECOMMENDED_CODECS: frozenset[str] = frozenset({
    'prores', 'prores_ks', 'prores_aw', 'dnxhr', 'dnxhd', 'ffv1', 'rawvideo'
})


# =============================================================================
# 硬件能力探测
# =============================================================================

class HardwareCapability:
    """
    硬件解码能力探测。

    静态方法探测系统可用的硬件解码器。

    Examples:
        >>> decoders = HardwareCapability.detect_available_decoders()
        >>> default = HardwareCapability.get_platform_default()
        >>> supported = HardwareCapability.check_codec_support(
        ...     HardwareDecoder.VIDEOTOOLBOX, 'h264'
        ... )
    """

    # 缓存探测结果
    _detected_decoders: Optional[list[HardwareDecoder]] = None
    _nvdec_available: Optional[bool] = None
    _vaapi_available: Optional[bool] = None
    _qsv_available: Optional[bool] = None

    # AUD-34909：探测缓存的填充加锁双检——探测会运行 nvidia-smi /
    # ffmpeg -hwaccels 子进程（各最坏 5s timeout），多线程首查并发时
    # 避免重复探测；命中路径保持无锁快读
    _DETECT_LOCK: threading.RLock = threading.RLock()

    @staticmethod
    def detect_available_decoders() -> list[HardwareDecoder]:
        """
        探测可用的硬件解码器。

        Returns:
            可用解码器列表（按优先级排序）
        """
        # AUD-34909：命中路径无锁快读
        if HardwareCapability._detected_decoders is not None:
            return HardwareCapability._detected_decoders

        with HardwareCapability._DETECT_LOCK:
            # 双检：等待期间其他线程可能已完成探测
            if HardwareCapability._detected_decoders is not None:
                return HardwareCapability._detected_decoders
            available = HardwareCapability._detect_available_decoders_uncached()
            HardwareCapability._detected_decoders = available
            logger.info(f"Detected hardware decoders: {[d.value for d in available]}")
            return available

    @staticmethod
    def _detect_available_decoders_uncached() -> list[HardwareDecoder]:
        """执行实际探测（调用方须持有 _DETECT_LOCK）。"""
        available: list[HardwareDecoder] = []
        system = platform.system()

        # macOS: VideoToolbox
        if system == 'Darwin':
            if HardwareCapability._check_videotoolbox():
                available.append(HardwareDecoder.VIDEOTOOLBOX)

        # Windows: D3D11VA, QSV, NVDEC
        elif system == 'Windows':
            # D3D11VA is universally available on Windows 8+
            available.append(HardwareDecoder.D3D11VA)

            # Check NVIDIA GPU
            if HardwareCapability._check_nvdec():
                available.append(HardwareDecoder.NVDEC)

            # Check Intel Quick Sync
            if HardwareCapability._check_qsv():
                available.append(HardwareDecoder.QSV)

        # Linux: VAAPI, NVDEC
        elif system == 'Linux':
            # Check VAAPI (Intel/AMD)
            if HardwareCapability._check_vaapi():
                available.append(HardwareDecoder.VAAPI)

            # Check NVIDIA GPU
            if HardwareCapability._check_nvdec():
                available.append(HardwareDecoder.NVDEC)

        return available

    @staticmethod
    def get_platform_default() -> Optional[HardwareDecoder]:
        """
        获取平台默认解码器。

        优先级：
        - macOS → VideoToolbox
        - Windows + NVIDIA → NVDEC > D3D11VA
        - Linux + NVIDIA → NVDEC > VAAPI
        - Linux + Intel → VAAPI

        Returns:
            默认解码器或 None（无硬件解码器可用）
        """
        available = HardwareCapability.detect_available_decoders()

        if not available:
            return None

        # macOS: VideoToolbox 是唯一选择
        if HardwareDecoder.VIDEOTOOLBOX in available:
            return HardwareDecoder.VIDEOTOOLBOX

        # NVIDIA NVDEC 优先（性能最好）
        if HardwareDecoder.NVDEC in available:
            return HardwareDecoder.NVDEC

        # Windows D3D11VA 优先（普遍可用）
        if HardwareDecoder.D3D11VA in available:
            return HardwareDecoder.D3D11VA

        # Linux VAAPI
        if HardwareDecoder.VAAPI in available:
            return HardwareDecoder.VAAPI

        # Intel QSV
        if HardwareDecoder.QSV in available:
            return HardwareDecoder.QSV

        # 返回第一个可用的
        return available[0]

    @staticmethod
    def check_codec_support(decoder: HardwareDecoder, codec: str) -> bool:
        """
        检查解码器是否支持特定 codec。

        Args:
            decoder: 硬件解码器
            codec: 编解码器名称（如 'h264', 'hevc'）

        Returns:
            支持返回 True
        """
        supported_codecs = HWACCEL_CODEC_SUPPORT.get(decoder, frozenset())
        codec_lower = codec.lower()

        # 精确匹配
        if codec_lower in supported_codecs:
            return True

        # 别名匹配
        aliases = {
            'h265': 'hevc',
            'libx264': 'h264',
            'libx265': 'hevc',
        }
        alias_codec = aliases.get(codec_lower, codec_lower)
        return alias_codec in supported_codecs

    @staticmethod
    def is_hwaccel_recommended(codec: str) -> bool:
        """
        判断是否推荐使用硬解。

        H.264/H.265/VP9/AV1 等 GOP codec 硬解效率高。
        ProRes/DNxHR 等 Intra codec CPU 多线程效率好。

        Args:
            codec: 编解码器名称

        Returns:
            推荐硬解返回 True
        """
        codec_lower = codec.lower()

        # 别名映射
        aliases = {
            'libx264': 'h264',
            'libx265': 'hevc',
            'x264': 'h264',
            'x265': 'hevc',
        }
        codec_lower = aliases.get(codec_lower, codec_lower)

        # 明确不推荐的
        if codec_lower in HWACCEL_NOT_RECOMMENDED_CODECS:
            return False
        if any(c in codec_lower for c in ('prores', 'dnx', 'ffv1', 'raw')):
            return False

        # 推荐硬解的
        if codec_lower in HWACCEL_RECOMMENDED_CODECS:
            return True
        if any(c in codec_lower for c in ('h264', 'h265', 'hevc', 'vp9', 'av1')):
            return True

        # 其他 codec 不推荐硬解
        return False

    @staticmethod
    def get_best_decoder_for_codec(codec: str) -> Optional[HardwareDecoder]:
        """
        获取指定 codec 的最佳解码器。

        Args:
            codec: 编解码器名称

        Returns:
            最佳解码器或 None（不推荐硬解或无支持）
        """
        # 不推荐硬解的 codec
        if not HardwareCapability.is_hwaccel_recommended(codec):
            return None

        available = HardwareCapability.detect_available_decoders()

        # 平台默认解码器编码了性能优先级（Windows: NVDEC > D3D11VA），
        # 优先于 detect_available_decoders 的探测顺序（D3D11VA 恒排第一）
        platform_default = HardwareCapability.get_platform_default()
        if (
            platform_default is not None
            and HardwareCapability.check_codec_support(platform_default, codec)
        ):
            return platform_default

        # 找到支持该 codec 的解码器
        for decoder in available:
            if HardwareCapability.check_codec_support(decoder, codec):
                return decoder

        return None

    # -------------------------------------------------------------------------
    # 内部检测方法
    # -------------------------------------------------------------------------

    @staticmethod
    def _check_videotoolbox() -> bool:
        """
        检查 VideoToolbox 是否可用。

        macOS 10.8+ 都支持 VideoToolbox。

        Returns:
            可用返回 True
        """
        if platform.system() != 'Darwin':
            return False

        # macOS 基本都支持，检查 FFmpeg 是否编译了 videotoolbox 支持
        if 'videotoolbox' in _ffmpeg_hwaccels_output():
            return True

        # 备用检查：通过 PyAV
        try:
            import av
            # 检查编译选项
            if hasattr(av, 'codec') and hasattr(av.codec, 'Codec'):
                # 尝试创建一个测试 codec context
                try:
                    codec = av.codec.Codec('h264', 'r')
                    if hasattr(codec, 'capabilities'):
                        # 检查是否有硬件解码能力
                        return True
                except Exception:
                    pass
        except ImportError:
            pass

        # macOS 默认假设可用
        return True

    @staticmethod
    def _check_nvdec() -> bool:
        """
        检查 NVIDIA NVDEC 是否可用。

        W2-02（vendor 放开的前置）：Windows 上 NVDEC 判定收敛到
        ``has_nvidia_gpu()``（nvcuda.dll 只随 NVIDIA 驱动安装）。旧实现
        在找不到 N 卡时会退问 ffmpeg CLI 是否编译了 cuda/nvdec —— 常见
        发行版构建（gyan/BtbN）几乎都带，会把 AMD/Intel GPU 机器误判为
        NVDEC 可用 → ``get_platform_default`` 选中 NVDEC → PyAV 静默软解
        → open() 拒绝，硬解永远到不了实际可用的 D3D11VA。

        Returns:
            可用返回 True
        """
        if HardwareCapability._nvdec_available is not None:
            return HardwareCapability._nvdec_available

        system = platform.system()

        # Windows: 只信真实 NVIDIA GPU（nvcuda.dll），不信 ffmpeg 编译项
        if system == 'Windows':
            HardwareCapability._nvdec_available = (
                HardwareCapability.has_nvidia_gpu()
            )
            return HardwareCapability._nvdec_available

        # Linux: 检查 NVIDIA 驱动
        elif system == 'Linux':
            try:
                result = subprocess.run(
                    ['nvidia-smi'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    HardwareCapability._nvdec_available = True
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        # 注意：不再向 ffmpeg CLI 询问 cuda/nvdec 编译项——「编译进 FFmpeg」
        # 不等于「本机有可用硬件」（W2-02，同 has_nvidia_gpu 的判定理由）。
        HardwareCapability._nvdec_available = False
        return False

    @staticmethod
    def has_nvidia_gpu() -> bool:
        """
        检查本机是否存在 NVIDIA GPU（真实硬件存在性，无 CLI 回退）。

        W2-02 起 ``_check_nvdec``（Windows/Linux）已收敛到同一判定语义：
        「FFmpeg 编译进 cuda/nvdec」不等于「本机有 N 卡」。Windows 解码
        路由（硬解优先还是 CPU 优先）需要的是真实存在 NVIDIA GPU。

        Returns:
            存在 NVIDIA GPU 返回 True
        """
        system = platform.system()
        if system == 'Windows':
            # nvcuda.dll 只随 NVIDIA 驱动安装；wmic 在 Win11 24H2+ 已移除，
            # 文件存在性检查更快且不受影响
            system_root = os.environ.get('SystemRoot', r'C:\Windows')
            return os.path.exists(
                os.path.join(system_root, 'System32', 'nvcuda.dll')
            )
        if system == 'Linux':
            try:
                result = subprocess.run(
                    ['nvidia-smi'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                return False
        return False

    @staticmethod
    def _check_vaapi() -> bool:
        """
        检查 VAAPI 是否可用。

        Returns:
            可用返回 True
        """
        if HardwareCapability._vaapi_available is not None:
            return HardwareCapability._vaapi_available

        if platform.system() != 'Linux':
            HardwareCapability._vaapi_available = False
            return False

        # 检查 /dev/dri/renderD* 设备
        # AUD-34903：无 DRM 设备（/dev/dri 不存在）或无权限（容器、
        # 受限环境）时 os.listdir 直接抛 FileNotFoundError/PermissionError，
        # 击穿整个探测路径——按"无设备"处理，走后续回退。
        try:
            dri_entries = os.listdir('/dev/dri')
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            dri_entries = []
        dri_devices = [
            f for f in dri_entries
            if f.startswith('renderD')
        ]
        if dri_devices:
            HardwareCapability._vaapi_available = True
            return True

        # 通过 FFmpeg 检查
        if 'vaapi' in _ffmpeg_hwaccels_output():
            HardwareCapability._vaapi_available = True
            return True

        HardwareCapability._vaapi_available = False
        return False

    @staticmethod
    def _check_qsv() -> bool:
        """
        检查 Intel Quick Sync Video 是否可用。

        Returns:
            可用返回 True
        """
        if HardwareCapability._qsv_available is not None:
            return HardwareCapability._qsv_available

        system = platform.system()

        # Windows: 检查 Intel GPU
        if system == 'Windows':
            # WMIC was removed from recent Windows 11 releases, and the
            # presence of ``qsv`` in FFmpeg only proves a build-time codec
            # option.  Query the real DXGI adapter list instead.
            try:
                from src.shared.media.windows_hw.gpu_capability import (
                    probe_gpu_capability,
                )
                cap = probe_gpu_capability()
                HardwareCapability._qsv_available = any(
                    adapter.vendor_name == 'intel'
                    for adapter in cap.adapters
                )
                return HardwareCapability._qsv_available
            except (ImportError, AttributeError, OSError):
                HardwareCapability._qsv_available = False
                return False

        # Linux: 检查 Intel GPU
        elif system == 'Linux':
            # AUD-34904：QSV 是 Intel 专属。VAAPI 可用不代表 QSV 可用
            # （AMD/NVIDIA 平台也有 VAAPI），必须确认 Intel DRM 设备存在；
            # 且绝不回退到 'qsv' in -hwaccels 的构建项检查——那只证明
            # ffmpeg 编译期选项，非 Intel 平台会误报 QSV 可用（与 W2-02
            # 已修复的 NVDEC 误判同类）。
            HardwareCapability._qsv_available = (
                HardwareCapability._check_vaapi()
                and HardwareCapability._has_intel_drm_device()
            )
            return HardwareCapability._qsv_available

        # AUD-34904：其余平台（macOS 等）QSV 不可用，同样不落 ffmpeg
        # 构建项检查
        HardwareCapability._qsv_available = False
        return False

    @staticmethod
    def _has_intel_drm_device() -> bool:
        """
        AUD-34904：检查 Linux 上是否存在 Intel DRM 设备。

        读取 /sys/class/drm/renderD*/device/vendor（PCI vendor ID，
        0x8086 = Intel）。纯文件系统检查，无子进程开销；sysfs 缺失/
        不可读（容器等）按无 Intel 设备处理（QSV 探测诚实失败，
        软件解码兜底）。

        Returns:
            存在 Intel render 设备返回 True
        """
        from pathlib import Path as _Path

        try:
            render_nodes = [
                name for name in os.listdir('/sys/class/drm')
                if name.startswith('renderD')
            ]
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return False

        for node in render_nodes:
            vendor_file = _Path('/sys/class/drm') / node / 'device' / 'vendor'
            try:
                vendor = vendor_file.read_text(encoding='ascii').strip()
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                continue
            if vendor.lower() == '0x8086':
                return True
        return False

    @staticmethod
    def clear_cache() -> None:
        """
        清除探测结果缓存。

        用于重新探测硬件能力。
        """
        HardwareCapability._detected_decoders = None
        HardwareCapability._nvdec_available = None
        HardwareCapability._vaapi_available = None
        HardwareCapability._qsv_available = None


# =============================================================================
# 硬解配置
# =============================================================================

@dataclass
class HardwareDecodeConfig:
    """
    硬件解码配置。

    Attributes:
        decoder: 指定解码器（None = Auto，自动选择最佳）
        fallback_to_cpu: 硬解失败时是否回退到 CPU
        copy_to_cpu: 是否复制帧数据到 CPU（False = 保持 GPU 状态）
        prefer_hwaccel_for_gop: 是否优先对 GOP codec 使用硬解
    """

    decoder: Optional[HardwareDecoder] = None  # None = Auto
    fallback_to_cpu: bool = True
    copy_to_cpu: bool = True      # 大多数场景需要 CPU 访问
    prefer_hwaccel_for_gop: bool = True  # H.264/H.265 优先硬解

    def __post_init__(self) -> None:
        """验证配置。

        AUD-34909：构造期不再触发可能耗时数秒的子进程探测
        （nvidia-smi / ffmpeg -hwaccels，各最坏 5s timeout）——构造可能
        发生在 UI 线程；可用性告警惰性推迟到首次真正选择解码器时。
        """
        self._availability_checked: bool = False

    def _warn_if_decoder_unavailable(self) -> None:
        """惰性可用性告警（AUD-34909）：首次选择解码器时才探测一次。"""
        if self._availability_checked:
            return
        self._availability_checked = True
        if self.decoder is None:
            return
        available = HardwareCapability.detect_available_decoders()
        if self.decoder not in available:
            logger.warning(
                f"Requested decoder {self.decoder.value} not available, "
                f"will use CPU fallback"
            )

    def get_decoder_for_codec(self, codec: str) -> Optional[HardwareDecoder]:
        """
        获取指定 codec 的解码器。

        Args:
            codec: 编解码器名称

        Returns:
            解码器或 None（使用 CPU）
        """
        # 明确指定的解码器
        if self.decoder is not None:
            # AUD-34909：可用性探测/告警在此惰性触发（非构造期）
            self._warn_if_decoder_unavailable()
            if HardwareCapability.check_codec_support(self.decoder, codec):
                return self.decoder
            # 指定的解码器不支持该 codec
            if self.fallback_to_cpu:
                logger.info(
                    f"Decoder {self.decoder.value} doesn't support {codec}, "
                    f"falling back to CPU"
                )
                return None
            return self.decoder  # 强制使用（会失败）

        # Auto: 根据 codec 选择最佳
        if self.prefer_hwaccel_for_gop:
            return HardwareCapability.get_best_decoder_for_codec(codec)

        return None


# =============================================================================
# 硬件加速解码源
# =============================================================================

class FFmpegHwaccelSource(FFmpegMediaSource):
    """
    硬件加速解码源。

    继承 FFmpegMediaSource，使用 hwaccel 进行硬件解码。

    特性：
    - 自动探测硬件解码器
    - 优雅降级到 CPU
    - 支持 H.264/H.265/VP9 等硬解
    - ProRes/DNxHR 使用 CPU 多线程

    Examples:
        >>> config = HardwareDecodeConfig(decoder=HardwareDecoder.VIDEOTOOLBOX)
        >>> source = FFmpegHwaccelSource('/path/to/video.mp4', config)
        >>> with source:
        ...     frame = source.read_frame(100)
    """

    def __init__(
        self,
        path: str,
        config: Optional[HardwareDecodeConfig] = None
    ) -> None:
        """
        初始化硬件加速解码源。

        Args:
            path: 媒体文件路径
            config: 硬解配置（None = 默认配置）
        """
        super().__init__(path)

        self._hwaccel_config: HardwareDecodeConfig = config or HardwareDecodeConfig()
        # AUD-34910: 只写不读的 hwaccel codec_context 死状态已移除
        # （全仓零读取点；PyAV codec_context 仍经 self._video_stream 可达）。
        self._active_decoder: Optional[HardwareDecoder] = None
        self._using_hwaccel: bool = False
        # GPU 路径，不禁用 hwaccel（父类 FFmpegMediaSource 默认禁用 hwaccel）
        self._disable_hwaccel: bool = False

    def open(self) -> None:
        """
        打开媒体文件（带硬件加速）。

        会尝试使用硬件解码，失败时自动回退到 CPU。

        Raises:
            MediaOpenError: 打开失败
        """
        # 先用父类打开（CPU 解码）
        super().open()

        # 尝试启用硬件加速
        if self._metadata is not None:
            self._try_enable_hwaccel()

    def _try_enable_hwaccel(self) -> None:
        """
        尝试启用硬件加速。

        根据 codec 和配置选择合适的解码器。
        """
        if self._metadata is None:
            return

        codec = self._metadata.codec_name

        # 获取推荐的解码器
        decoder = self._hwaccel_config.get_decoder_for_codec(codec)

        if decoder is None:
            logger.info(f"Using CPU decode for {codec}")
            self._using_hwaccel = False
            return

        # 检查是否可用
        available = HardwareCapability.detect_available_decoders()
        if decoder not in available:
            logger.warning(
                f"Decoder {decoder.value} not available, using CPU fallback"
            )
            self._using_hwaccel = False
            return

        # 尝试配置 hwaccel
        try:
            self._configure_hwaccel(decoder)
            self._active_decoder = decoder
            self._using_hwaccel = True
            logger.info(
                f"Enabled hardware acceleration: {decoder.value} for {codec}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to configure hwaccel {decoder.value}: {e}, "
                f"falling back to CPU"
            )
            if self._hwaccel_config.fallback_to_cpu:
                self._using_hwaccel = False
            else:
                raise MediaOpenError(
                    f"Failed to configure hwaccel: {e}",
                    source_path=self._path
                ) from e

    def _configure_hwaccel(self, decoder: HardwareDecoder) -> None:
        """
        配置硬件加速。

        PyAV 17.x 改变了 hwaccel API：
        - 旧版: ctx.hwaccel = 'videotoolbox' (属性可写)
        - 新版: ctx.hwaccel 是只读的，必须通过 av.open(hwaccel=...) 配置

        本方法关闭旧容器，使用 hwaccel 参数重新打开。

        Args:
            decoder: 硬件解码器

        Raises:
            RuntimeError: 配置失败
        """
        try:
            import av
        except ImportError:
            raise RuntimeError("PyAV not installed")

        if self._video_stream is None:
            raise RuntimeError("Video stream not initialized")

        hwaccel_type = decoder.value

        # PyAV 17.x+: use HWAccel class passed to av.open()
        try:
            from av.codec.hwaccel import HWAccel, HWDeviceType

            device_type_map = {
                'videotoolbox': HWDeviceType.videotoolbox,
                'nvdec': HWDeviceType.cuda,
                'vaapi': HWDeviceType.vaapi,
                'qsv': HWDeviceType.qsv,
                'd3d11va': HWDeviceType.d3d11va,
                'cuda': HWDeviceType.cuda,
            }

            device_type = device_type_map.get(hwaccel_type)
            if device_type is None:
                raise RuntimeError(
                    f"Unsupported hwaccel device type: {hwaccel_type}"
                )

            accel = HWAccel(device_type)

            # Close the existing CPU container and reopen with hwaccel
            self._frame_iterator = None
            self._decoder_buffer = []
            self._pending_av_frame = None
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    pass
                self._container = None
                self._video_stream = None

            # Reopen with hwaccel.
            # AUD-34901：旧容器已在上方关闭——重开/校验失败时必须恢复
            # CPU 容器再抛出。否则 _is_open 仍为 True 而 container/stream
            # 为 None，_try_enable_hwaccel 宣称"回退 CPU"后，后续每次
            # 读取必抛 RuntimeError("Container or video stream not
            # initialized")，CPU 回退完全失效。
            try:
                new_container = av.open(self._path, hwaccel=accel)
                video_streams = new_container.streams.video
                if not video_streams:
                    try:
                        new_container.close()
                    except Exception:  # noqa: BLE001 — 清理失败不掩盖主错误
                        pass
                    raise RuntimeError(
                        "No video stream found on hwaccel reopen"
                    )
                self._container = new_container
                self._video_stream = video_streams[0]
            except Exception:
                self._reopen_cpu_container()
                raise
            # Frame threading is useful for CPU GOP decode, but with PyAV
            # HWAccel on Windows it can silently drop the hardware surface
            # path. WindowsHwPlanarSource follows the same rule.
            if sys.platform != 'win32':
                self._video_stream.thread_type = 'AUTO'
            self._is_open = True
            self._frame_iterator = None

            logger.info(
                f"Configured hwaccel via PyAV 17.x HWAccel API: {hwaccel_type}, "
                f"is_hwaccel={self._video_stream.codec_context.is_hwaccel}"
            )
            return

        except (ImportError, AttributeError):
            # Fallback for older PyAV versions
            pass

        # Legacy PyAV (< 17.x): try setting codec context attributes
        ctx = self._video_stream.codec_context
        if hasattr(ctx, 'hwaccel'):
            try:
                ctx.hwaccel = hwaccel_type
                logger.info(f"Configured hwaccel via legacy ctx.hwaccel: {hwaccel_type}")
                return
            except (AttributeError, TypeError):
                pass

        if hasattr(ctx, 'hw_device_type'):
            try:
                ctx.hw_device_type = hwaccel_type
                logger.info(f"Configured hwaccel via ctx.hw_device_type: {hwaccel_type}")
                return
            except (AttributeError, TypeError):
                pass

        raise RuntimeError(
            f"Cannot configure hwaccel for {hwaccel_type}: "
            f"PyAV doesn't support hwaccel configuration on this version"
        )

    def _reopen_cpu_container(self, resync_frame: Optional[int] = None) -> None:
        """
        AUD-34901/34902：hwaccel 容器失效后按 CPU 重建解码容器。

        关闭 hwaccel 容器、丢弃全部迭代状态（含残留 _pending_av_frame /
        seek 会话，防止吐出陈旧帧），再以纯软件方式重开（线程配置与父类
        open() 一致，并显式禁用自动 hw 路由），可选 seek 回指定帧号。

        Args:
            resync_frame: 非 None 时重开后 seek 到该帧号（AUD-34902 中途
                回退场景，保证内容/帧号不错位）；None 表示从当前位置服务
                （AUD-34901 打开期场景，容器从流头开始）。

        Raises:
            RuntimeError: CPU 容器重建失败（此时 _is_open 置 False，
                显式不可用而非假开状态）
        """
        try:
            import av
        except ImportError:
            self._is_open = False
            raise RuntimeError("PyAV not installed")

        # 丢弃全部迭代状态：hwaccel 容器的迭代器/缓冲/落地帧对 CPU
        # 容器全部无效，保留只会丢帧/帧号漂移/吐陈旧帧
        self._frame_iterator = None
        self._decoder_buffer = []
        self._pending_av_frame = None
        self._gop_seek_iterator = None
        self._main_resync_pts = None

        if self._container is not None:
            try:
                self._container.close()
            except Exception:  # noqa: BLE001 — 关闭失败也要继续重建
                pass
        self._container = None
        self._video_stream = None

        try:
            self._container = av.open(self._path)
            video_streams = self._container.streams.video
            if not video_streams:
                raise RuntimeError("No video stream found on CPU reopen")
            self._video_stream = video_streams[0]
            # 线程配置与父类 open() 一致：intra 用 SLICE，GOP 用 AUTO
            try:
                stream_codec = str(
                    getattr(self._video_stream.codec_context, 'name', '') or ''
                ).lower()
                if _is_intra_codec(stream_codec):
                    self._video_stream.thread_type = 'SLICE'
                else:
                    self._video_stream.thread_type = 'AUTO'
            except (AttributeError, ValueError):
                self._video_stream.thread_type = 'AUTO'
            if (hasattr(self._video_stream, 'codec_context')
                    and self._video_stream.codec_context):
                self._video_stream.codec_context.thread_count = 0
                # CPU 回退必须显式禁用自动 hw 路由：FFmpeg 8.1+ 在 macOS
                # 可能把 ProRes 等 codec 自动路由回 VideoToolbox——那不是
                # CPU 回退，同样的错误会再次发生
                if hasattr(self._video_stream.codec_context, 'options'):
                    self._video_stream.codec_context.options = {
                        **(self._video_stream.codec_context.options or {}),
                        'hwaccel': 'none',
                    }
        except Exception:
            # CPU 重建也失败：显式置为不可用。_is_open=True + container=None
            # 的假开状态会让后续每次读取抛 RuntimeError
            self._is_open = False
            raise

        if resync_frame is not None:
            # 重开容器位于流头。seek_frame 有 `_current_frame == 目标 即
            # 返回` 的短路——必须先把帧号归零到与容器位置一致，否则
            # resync 会被短路成空操作，后续读取从帧 0 开始却标着旧帧号。
            self._current_frame = 0
            self.seek_frame(resync_frame)

    def read_next_frame(self) -> DecodedFrame:
        """
        读取下一帧（硬件解码）。

        如果启用了硬件加速，使用 GPU 解码。
        否则使用 CPU 解码。

        Returns:
            DecodedFrame 实例

        Raises:
            MediaDecodeError: 解码失败
            StopIteration: 到达视频末尾
        """
        if not self._is_open:
            raise RuntimeError("Media source not open. Call open() first.")

        if self._container is None or self._video_stream is None:
            raise RuntimeError("Container or video stream not initialized")

        # 如果没有启用硬件加速，使用父类方法
        if not self._using_hwaccel:
            return super().read_next_frame()

        # 硬件解码
        try:
            # AUD-34900：与父类 read_next_frame 保持同一 seek 落地契约。
            # seek_frame（普通 GOP/Intra 与 SLICE 会话）都会把目标帧缓存在
            # _pending_av_frame、把主会话回同步点记在 _main_resync_pts；
            # 顺序读必须先消费落地帧、再续读 seek 会话、建主迭代器前先
            # 回同步，否则 seek 后返回错位帧且落地帧永久滞留。
            if self._pending_av_frame is not None:
                frame = self._pending_av_frame
                self._pending_av_frame = None
                decoded = self._convert_hwaccel_frame(frame)
                self._current_frame = max(
                    self._current_frame, decoded.frame_index + 1
                )
                return decoded

            session_frame = self._pull_next_frame_from_seek_session()
            if session_frame is not None:
                decoded = self._convert_hwaccel_frame(session_frame)
                self._current_frame = max(
                    self._current_frame, decoded.frame_index + 1
                )
                return decoded

            # 创建帧迭代器（如果不存在）
            if self._frame_iterator is None:
                self._resync_main_session_if_needed()
                self._frame_iterator = self._container.decode(self._video_stream)

            # 解码下一帧
            for frame in self._frame_iterator:
                decoded = self._convert_hwaccel_frame(frame)
                self._current_frame += 1
                return decoded

            # 视频结束
            raise StopIteration("End of video stream")

        except StopIteration:
            raise
        except Exception as e:
            logger.error(f"HWaccel decode error at frame {self._current_frame}: {e}")

            # 尝试回退到 CPU
            if self._hwaccel_config.fallback_to_cpu and self._using_hwaccel:
                logger.warning("Falling back to CPU decode")
                # AUD-34902：旧实现只翻转标志位并置空迭代器——容器仍是
                # hwaccel 打开的（继续硬解、同样的错误会复现）、迭代器
                # 内已缓冲帧被丢弃（丢帧+帧号漂移）、残留 _pending_av_frame
                # 会吐出陈旧帧。真回退：关 hwaccel 容器 → CPU 重开 →
                # seek 回当前帧号 → 重新解码当前帧。
                fallback_frame = self._current_frame
                self._using_hwaccel = False
                try:
                    self._reopen_cpu_container(resync_frame=fallback_frame)
                except Exception as fallback_error:
                    raise MediaDecodeError(
                        f"CPU fallback failed at frame {fallback_frame}: "
                        f"{fallback_error}",
                        source_path=self._path
                    ) from e
                return super().read_next_frame()

            raise MediaDecodeError(
                f"Decode error at frame {self._current_frame}: {e}",
                source_path=self._path
            ) from e

    def _convert_hwaccel_frame(self, frame: Any) -> DecodedFrame:
        """
        转换硬件解码帧为 DecodedFrame。

        硬解输出的帧可能是 GPU surface，需要复制到 CPU。

        Args:
            frame: PyAV VideoFrame

        Returns:
            DecodedFrame 实例
        """
        # Keep the output contract in one place.  The old implementation called
        # ``frame.to_ndarray()`` directly, which exposed the decoder's NV12/P010
        # layout even when the source was configured for packed RGB output.  That
        # made ``decode_path=hwaccel_download_packed_rgb`` misleading and could
        # send a two-dimensional, stacked-plane array into RGB consumers.
        #
        # The base converters already handle hardware-surface transfer, target
        # sizing, pixel-format conversion, and plane metadata consistently with
        # the CPU source.  WindowsHwPlanarSource remains the specialized fast
        # path for callers that explicitly request preserved YUV planes.
        if self._preserve_planes or self._output_mode == "planar_yuv":
            return super()._convert_frame_planar(frame)
        return super()._convert_frame(frame)

    def close(self) -> None:
        """
        关闭并释放资源。
        """
        # 清理 hwaccel context（AUD-34910: 死状态已移除）
        self._active_decoder = None
        self._using_hwaccel = False

        # 父类关闭
        super().close()

    @property
    def using_hwaccel(self) -> bool:
        """
        是否正在使用硬件加速。

        Returns:
            True = 硬解，False = CPU 解码
        """
        return self._using_hwaccel

    @property
    def active_decoder(self) -> Optional[HardwareDecoder]:
        """
        当前使用的硬件解码器。

        Returns:
            解码器或 None
        """
        return self._active_decoder

    @property
    def hwaccel_config(self) -> HardwareDecodeConfig:
        """
        硬解配置。

        Returns:
            HardwareDecodeConfig 实例
        """
        return self._hwaccel_config


# =============================================================================
# 便捷函数
# =============================================================================

def create_hwaccel_source(
    path: str,
    decoder: Optional[HardwareDecoder] = None,
    fallback_to_cpu: bool = True
) -> FFmpegHwaccelSource:
    """
    创建硬件加速解码源的便捷函数。

    Args:
        path: 媒体文件路径
        decoder: 指定解码器（None = Auto）
        fallback_to_cpu: 硬解失败时回退到 CPU

    Returns:
        FFmpegHwaccelSource 实例
    """
    config = HardwareDecodeConfig(
        decoder=decoder,
        fallback_to_cpu=fallback_to_cpu,
    )
    return FFmpegHwaccelSource(path, config)


def get_hwaccel_status() -> dict[str, Any]:
    """
    获取硬件加速状态摘要。

    Returns:
        状态字典，包含可用解码器、默认解码器等信息
    """
    available = HardwareCapability.detect_available_decoders()
    default = HardwareCapability.get_platform_default()

    return {
        'available_decoders': [d.value for d in available],
        'default_decoder': default.value if default else None,
        'platform': platform.system(),
        'hwaccel_enabled': len(available) > 0,
    }


# N16/Q01：本类覆写了父类的 open/read_next_frame，而 av_locked_operation
# 只装饰 FFmpegMediaSource.__dict__ 里的版本——子类属性查找拿到未装饰
# 实现，硬解路径既不进进程级 in-flight 计数也不取实例锁，ffsrc-closer
# 的 wait_for_idle() 会在硬解进行中关容器（libav↔GIL 互锁）。照
# macos_vt/source.py 的写法在模块尾统一补锁。
from src.shared.media.av_concurrency import av_locked_operation as _av_op_dec  # noqa: E402

for _name in ("open", "read_next_frame"):
    if _name in FFmpegHwaccelSource.__dict__:
        setattr(FFmpegHwaccelSource, _name,
                _av_op_dec(FFmpegHwaccelSource.__dict__[_name]))
