"""
Proxy Strategy Module
代理解码策略，定义实时播放时的分辨率降级规则。

## 职责

- ProxyStrategy: 代理策略枚举
- ProxyConfig: 代理配置数据类
- calculate_proxy_size: 计算代理分辨率

## 设计原则

- 与 DecodePolicy 配合使用
- 支持多种降级策略
- 考虑视口大小和性能自动选择
- 完整类型注解

## 使用示例

```python
from src.shared.media.proxy import ProxyStrategy, ProxyConfig, calculate_proxy_size

# 计算代理分辨率
proxy_size = calculate_proxy_size(
    original_size=(3840, 2160),  # 4K
    viewport_size=(1920, 1080),
    strategy=ProxyStrategy.AUTO
)
print(proxy_size)  # (1920, 1080)

# 创建代理配置
config = ProxyConfig(
    strategy=ProxyStrategy.HALF,
    force_8bit=True
)
```

## 与 DecodePolicy 的关系

- DecodePolicy 定义解码格式和质量要求
- ProxyStrategy 定义分辨率降级策略
- 两者共同决定最终解码参数
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class ProxyStrategy(Enum):
    """
    代理策略类型。

    定义实时播放时如何降级分辨率。

    Attributes:
        ORIGINAL: 不降级，保持原始分辨率
        HALF: 50% 分辨率
        QUARTER: 25% 分辨率
        CUSTOM: 用户指定最大分辨率
        AUTO: 根据视口和性能自动选择
    """

    ORIGINAL = 'original'     # 不降级
    HALF = 'half'             # 50% 分辨率
    QUARTER = 'quarter'       # 25% 分辨率
    CUSTOM = 'custom'         # 用户指定
    AUTO = 'auto'             # 自动选择


@dataclass(frozen=True)
class ProxyConfig:
    """
    代理配置。

    定义代理解码的具体参数。

    Attributes:
        strategy: 代理策略
        max_resolution: 自定义最大分辨率（仅 CUSTOM 策略有效）
        force_8bit: 是否强制 8-bit 输出（用于性能优化）
        min_resolution: 最小分辨率限制（防止过小）
    """

    strategy: ProxyStrategy
    max_resolution: Optional[Tuple[int, int]] = None  # (width, height)
    force_8bit: bool = True
    min_resolution: Tuple[int, int] = (320, 180)  # 最小 320x180

    def __post_init__(self) -> None:
        """验证配置"""
        if self.strategy == ProxyStrategy.CUSTOM and self.max_resolution is None:
            raise ValueError("CUSTOM strategy requires max_resolution")

        if self.max_resolution is not None:
            w, h = self.max_resolution
            if w <= 0 or h <= 0:
                raise ValueError(f"Invalid max_resolution: {self.max_resolution}")

    @property
    def scale_factor(self) -> float:
        """
        获取缩放因子。

        Returns:
            缩放因子（1.0 表示不缩放）
        """
        if self.strategy == ProxyStrategy.ORIGINAL:
            return 1.0
        elif self.strategy == ProxyStrategy.HALF:
            return 0.5
        elif self.strategy == ProxyStrategy.QUARTER:
            return 0.25
        elif self.strategy == ProxyStrategy.CUSTOM:
            # CUSTOM 策略的缩放因子在 calculate_proxy_size 中计算
            return 1.0  # 占位，实际计算需要原始分辨率
        else:
            return 1.0  # AUTO 策略需要动态计算


# 默认代理配置
DEFAULT_PROXY_CONFIG = ProxyConfig(
    strategy=ProxyStrategy.AUTO,
    force_8bit=True
)

# 性能阈值配置
# 视口大小阈值，超过此值时启用代理
VIEWPORT_THRESHOLD = (1920, 1080)  # 1080p

# 性能阈值，帧率低于此值时启用更低分辨率代理
FPS_THRESHOLD_LOW = 20.0  # FPS

# 最大长边限制（用于 AUTO 策略）
MAX_LONG_EDGE_AUTO = 1920  # 1080p 长边


def _scale_with_min_clamp(
    original_size: Tuple[int, int],
    scale: float,
    min_size: Tuple[int, int],
) -> Tuple[int, int]:
    """按统一缩放系数计算尺寸并施加最小分辨率钳制（AUD-35505）。

    最小分辨率以"钳制缩放系数"的方式整体生效，而不是按轴独立
    ``max(min, dim)``：按轴独立钳制会破坏宽高比（两轴各自顶到不同
    下限），并且会把小于下限的小尺寸源上采样。钳制后的系数上限为
    1.0——代理永不放大源分辨率；源本身小于下限时保持原始尺寸。

    Args:
        original_size: 原始分辨率 (width, height)
        scale: 策略给出的基础缩放系数（HALF=0.5 等）
        min_size: 最小分辨率限制 (width, height)

    Returns:
        缩放后的分辨率 (width, height)（保持宽高比，不超过原始）
    """
    orig_w, orig_h = original_size
    min_w, min_h = min_size
    eff_scale = scale
    if orig_w > 0:
        eff_scale = max(eff_scale, min_w / orig_w)
    if orig_h > 0:
        eff_scale = max(eff_scale, min_h / orig_h)
    eff_scale = min(eff_scale, 1.0)
    return (int(orig_w * eff_scale), int(orig_h * eff_scale))


def calculate_proxy_size(
    original_size: Tuple[int, int],
    viewport_size: Tuple[int, int],
    strategy: ProxyStrategy,
    custom_max: Optional[Tuple[int, int]] = None,
    min_size: Tuple[int, int] = (320, 180)
) -> Tuple[int, int]:
    """
    计算代理分辨率。

    根据策略和视口大小，计算合适的代理分辨率。
    保持原始宽高比；最小分辨率以统一缩放系数方式钳制（不放大源、
    不破坏宽高比，见 _scale_with_min_clamp）。

    Args:
        original_size: 原始分辨率 (width, height)
        viewport_size: 视口分辨率 (width, height)
        strategy: 代理策略
        custom_max: 自定义最大分辨率（仅 CUSTOM 策略使用）
        min_size: 最小分辨率限制

    Returns:
        代理分辨率 (width, height)

    Examples:
        >>> calculate_proxy_size((3840, 2160), (1920, 1080), ProxyStrategy.HALF)
        (1920, 1080)

        >>> calculate_proxy_size((3840, 2160), (1920, 1080), ProxyStrategy.QUARTER)
        (960, 540)

        >>> calculate_proxy_size((3840, 2160), (1920, 1080), ProxyStrategy.AUTO)
        (1920, 1080)  # 自动选择，不超过视口

        >>> calculate_proxy_size((1280, 720), (1920, 1080), ProxyStrategy.AUTO)
        (1280, 720)  # 视频小于视口，保持原始
    """
    orig_w, orig_h = original_size
    view_w, view_h = viewport_size

    # ORIGINAL 策略：不降级
    if strategy == ProxyStrategy.ORIGINAL:
        return (orig_w, orig_h)

    # HALF 策略：50% 分辨率
    if strategy == ProxyStrategy.HALF:
        return _scale_with_min_clamp(original_size, 0.5, min_size)

    # QUARTER 策略：25% 分辨率
    if strategy == ProxyStrategy.QUARTER:
        return _scale_with_min_clamp(original_size, 0.25, min_size)

    # CUSTOM 策略：用户指定最大分辨率
    if strategy == ProxyStrategy.CUSTOM:
        if custom_max is None:
            # 没有指定，使用视口大小
            custom_max = viewport_size

        max_w, max_h = custom_max

        # 计算缩放比例，保持宽高比
        scale_w = max_w / orig_w if orig_w > max_w else 1.0
        scale_h = max_h / orig_h if orig_h > max_h else 1.0
        scale = min(scale_w, scale_h)

        return _scale_with_min_clamp(original_size, scale, min_size)

    # AUTO 策略：根据视口和性能自动选择
    if strategy == ProxyStrategy.AUTO:
        # 计算长边
        orig_long_edge = max(orig_w, orig_h)
        view_long_edge = max(view_w, view_h)

        # 如果视频小于视口，保持原始分辨率
        if orig_long_edge <= view_long_edge:
            return (orig_w, orig_h)

        # 如果视频大于视口，缩放到不超过视口
        # 同时限制最大长边
        target_long_edge = min(view_long_edge, MAX_LONG_EDGE_AUTO)

        # 计算缩放比例
        scale = target_long_edge / orig_long_edge

        return _scale_with_min_clamp(original_size, scale, min_size)

    # 默认返回原始分辨率
    return (orig_w, orig_h)


def should_use_proxy(
    original_size: Tuple[int, int],
    viewport_size: Tuple[int, int],
    current_fps: float = 60.0
) -> bool:
    """
    判断是否需要启用代理。

    根据视频尺寸、视口大小和当前性能判断。

    Args:
        original_size: 原始分辨率
        viewport_size: 视口分辨率
        current_fps: 当前播放帧率

    Returns:
        True 表示应该使用代理

    Examples:
        >>> should_use_proxy((3840, 2160), (1920, 1080), 60.0)
        True  # 4K 视频，1080p 视口

        >>> should_use_proxy((1920, 1080), (1920, 1080), 60.0)
        False  # 视频和视口相同大小

        >>> should_use_proxy((3840, 2160), (1920, 1080), 15.0)
        True  # 性能低，需要代理
    """
    orig_w, orig_h = original_size
    view_w, view_h = viewport_size

    # 视频大于视口，需要代理
    if max(orig_w, orig_h) > max(view_w, view_h):
        return True

    # 性能低于阈值，考虑使用代理
    if current_fps < FPS_THRESHOLD_LOW:
        # 即使视频不大，也可能需要代理
        return orig_w * orig_h > view_w * view_h

    return False


def get_recommended_strategy(
    original_size: Tuple[int, int],
    viewport_size: Tuple[int, int],
    current_fps: float = 60.0,
    codec_name: Optional[str] = None
) -> ProxyStrategy:
    """
    获取推荐的代理策略。

    综合考虑视频尺寸、视口、性能和编解码器类型。

    Args:
        original_size: 原始分辨率
        viewport_size: 视口分辨率
        current_fps: 当前播放帧率
        codec_name: 编解码器名称（用于判断解码复杂度）

    Returns:
        推荐的 ProxyStrategy

    Examples:
        >>> get_recommended_strategy((3840, 2160), (1920, 1080), 60.0, 'h264')
        ProxyStrategy.HALF  # 4K H.264，推荐 50%

        >>> get_recommended_strategy((3840, 2160), (1920, 1080), 60.0, 'prores')
        ProxyStrategy.AUTO  # ProRes 解码快，AUTO 即可

        >>> get_recommended_strategy((7680, 4320), (1920, 1080), 60.0, 'h265')
        ProxyStrategy.QUARTER  # 8K H.265，推荐 25%
    """
    orig_w, orig_h = original_size
    view_w, view_h = viewport_size

    # 计算像素比
    pixel_ratio = (orig_w * orig_h) / (view_w * view_h)

    # 编解码器复杂度权重
    # H.265/HEVC 比 H.264 解码更慢
    # VP9/AV1 解码复杂度也高
    codec_weights: dict[str, float] = {
        'h265': 1.5,
        'hevc': 1.5,
        'vp9': 1.3,
        'av1': 2.0,  # AV1 解码最慢
        'h264': 1.0,
        'prores': 0.3,  # ProRes 解码快
        'dnxhr': 0.3,
        'dnxhd': 0.3,
    }

    weight = 1.0
    if codec_name:
        codec_lower = codec_name.lower()
        for key, w in codec_weights.items():
            if key in codec_lower:
                weight = w
                break

    # 综合判断
    effective_ratio = pixel_ratio * weight

    # 8K+ 视频，复杂编解码器，性能低
    if effective_ratio > 16.0 or (pixel_ratio > 8.0 and current_fps < FPS_THRESHOLD_LOW):
        return ProxyStrategy.QUARTER

    # 4K+ 视频，复杂编解码器
    if effective_ratio > 4.0 or (pixel_ratio > 2.0 and weight > 1.0):
        return ProxyStrategy.HALF

    # 默认 AUTO
    return ProxyStrategy.AUTO


def is_proxy_frame(
    frame_size: Tuple[int, int],
    original_size: Tuple[int, int]
) -> bool:
    """
    判断帧是否为代理帧。

    Args:
        frame_size: 帧分辨率
        original_size: 原始分辨率

    Returns:
        True 表示代理帧

    Examples:
        >>> is_proxy_frame((1920, 1080), (3840, 2160))
        True

        >>> is_proxy_frame((1920, 1080), (1920, 1080))
        False
    """
    frame_w, frame_h = frame_size
    orig_w, orig_h = original_size

    return frame_w < orig_w or frame_h < orig_h


def calculate_scale_ratio(
    proxy_size: Tuple[int, int],
    original_size: Tuple[int, int]
) -> float:
    """
    计算代理帧相对于原始帧的缩放比例。

    Args:
        proxy_size: 代理分辨率
        original_size: 原始分辨率

    Returns:
        缩放比例（0.25, 0.5, 1.0 等）

    Examples:
        >>> calculate_scale_ratio((1920, 1080), (3840, 2160))
        0.5

        >>> calculate_scale_ratio((1920, 1080), (1920, 1080))
        1.0
    """
    proxy_w, proxy_h = proxy_size
    orig_w, orig_h = original_size

    # 使用宽度的缩放比例（高度应该相同比例）
    return proxy_w / orig_w if orig_w > 0 else 1.0


# =============================================================================
# 磁盘代理（Disk Proxy）- P10.3
# =============================================================================

from enum import Enum, auto
import hashlib
import os


class ProxyFormat(Enum):
    """
    磁盘代理文件格式枚举。

    Attributes:
        TOPOS_PROXY: 自研 Topos 422 Proxy（TPIC MOV，帧内 4:2:2 10-bit；
            纯视频容器 ADR-C008。topos 源恒走本格式；普通源经
            PyAV 解码 + native 编码生成，播放侧内容嗅探自动路由）
        PRORES_PROXY: ProRes Proxy（跨平台优先，帧内编码）
        H264: H.264 Proxy（通用，体积小）
        IMAGE_SEQUENCE: 图像序列代理（每帧独立文件）
    """
    TOPOS_PROXY = auto()
    PRORES_PROXY = auto()
    H264 = auto()
    IMAGE_SEQUENCE = auto()

    @property
    def extension(self) -> str:
        """获取文件扩展名"""
        mapping = {
            ProxyFormat.TOPOS_PROXY: '.mov',
            ProxyFormat.PRORES_PROXY: '.mov',
            ProxyFormat.H264: '.mp4',
            ProxyFormat.IMAGE_SEQUENCE: '',  # 目录形式
        }
        return mapping[self]

    @property
    def codec_name(self) -> str:
        """获取 FFmpeg codec 名称"""
        mapping = {
            ProxyFormat.TOPOS_PROXY: 'topos',  # native 编码，非 FFmpeg 域
            ProxyFormat.PRORES_PROXY: 'prores_ks',
            ProxyFormat.H264: 'libx264',
            ProxyFormat.IMAGE_SEQUENCE: 'png',  # 图像序列格式
        }
        return mapping[self]

    @property
    def is_intra_only(self) -> bool:
        """是否为帧内编码（适合随机访问）"""
        return self in (
            ProxyFormat.TOPOS_PROXY,
            ProxyFormat.PRORES_PROXY,
            ProxyFormat.IMAGE_SEQUENCE,
        )


class ProxyResolution(Enum):
    """
    代理分辨率比例枚举。

    Attributes:
        FULL: 原始分辨率（1:1）
        HALF: 1/2 分辨率
        QUARTER: 1/4 分辨率
        EIGHTH: 1/8 分辨率
    """
    FULL = 1.0
    HALF = 0.5
    QUARTER = 0.25
    EIGHTH = 0.125

    @property
    def scale_factor(self) -> float:
        """获取缩放因子"""
        return self.value

    def calculate_size(
        self,
        original_size: Tuple[int, int]
    ) -> Tuple[int, int]:
        """
        计算代理分辨率。

        Args:
            original_size: 原始分辨率 (width, height)

        Returns:
            代理分辨率（保持宽高比，向下取整到偶数；最小分辨率以
            统一缩放系数钳制——不放大源、不破坏宽高比，AUD-35505）
        """
        scaled_w, scaled_h = _scale_with_min_clamp(
            original_size, self.value, (320, 180))
        proxy_w = max(2, scaled_w // 2 * 2)  # 保证偶数
        proxy_h = max(2, scaled_h // 2 * 2)
        return (proxy_w, proxy_h)


# 默认代理分辨率映射
DEFAULT_PROXY_RESOLUTIONS: dict[ProxyResolution, str] = {
    ProxyResolution.FULL: "Original",
    ProxyResolution.HALF: "1/2",
    ProxyResolution.QUARTER: "1/4",
    ProxyResolution.EIGHTH: "1/8",
}


class DiskProxyConfig:
    """
    磁盘代理配置。

    定义如何生成和管理磁盘上的代理文件。

    Attributes:
        format: 代理文件格式
        resolution: 代理分辨率比例
        prores_profile: ProRes profile（仅 PRORES_PROXY 有效）
            'proxy': ProRes Proxy（最低码率）
            '422LT': ProRes 422 LT
            '422': ProRes 422（默认）
        h264_crf: H.264 CRF 值（仅 H264 有效，默认 23）
        image_format: 图像序列格式（仅 IMAGE_SEQUENCE 有效）
            'png': PNG（无损，默认）
            'jpg': JPEG（有损，体积小）
        cache_dir: 代理缓存目录（相对于项目目录）
        target_height: 可选的固定代理高度（编辑页使用 1080p）
    """

    def __init__(
        self,
        format: ProxyFormat = ProxyFormat.PRORES_PROXY,
        resolution: ProxyResolution = ProxyResolution.HALF,
        prores_profile: str = '422',
        h264_crf: int = 23,
        image_format: str = 'png',
        cache_dir: str = '.cache/video_proxy',
        target_height: Optional[int] = None,
        topos_bit_depth: int = 10,
        topos_keep_444: bool = False,
        topos_qp: int = 34,
    ) -> None:
        """
        初始化磁盘代理配置。

        Args:
            format: 代理文件格式
            resolution: 代理分辨率比例
            prores_profile: ProRes profile
            h264_crf: H.264 CRF 值
            image_format: 图像序列格式
            cache_dir: 代理缓存目录
            target_height: 可选固定代理高度
            topos_bit_depth: Topos 代理位深（10/12；12-bit 源可保留
                12-bit 免右移量化；实际 = min(请求, 源位深)。G12）
            topos_keep_444: Topos 代理保留 4:4:4（默认 False = 4:2:2
                水平平均降采样）。G12
            topos_qp: Topos 代理固定 QP（默认 34，既有口径）。G12
        """
        self.format = format
        self.resolution = resolution
        self.prores_profile = prores_profile
        self.h264_crf = h264_crf
        self.image_format = image_format
        self.cache_dir = cache_dir
        self.target_height = (
            max(2, int(target_height) // 2 * 2)
            if target_height is not None else None
        )
        self.topos_bit_depth = (
            int(topos_bit_depth) if int(topos_bit_depth) in (10, 12, 16)
            else 10)
        self.topos_keep_444 = bool(topos_keep_444)
        self.topos_qp = max(0, min(95, int(topos_qp)))

    def calculate_target_size(self, original_size: Tuple[int, int]) -> Tuple[int, int]:
        """Calculate a proxy size while retaining the source aspect ratio."""
        width, height = original_size
        if self.target_height is None:
            return self.resolution.calculate_size(original_size)

        # AUD-35506：高度与宽度同样向下取偶——H264/JPEG（yuv420p 系）
        # 要求两维均为偶数，奇数高度会让 ffmpeg 报
        # "height not divisible by 2" 导致代理生成失败。
        target_height = min(max(2, int(height)), self.target_height) // 2 * 2
        scale = target_height / max(1, int(height))
        target_width = max(2, int(round(int(width) * scale)) // 2 * 2)
        return target_width, target_height

    def get_ffmpeg_args(self, target_size: Tuple[int, int]) -> list[str]:
        """
        生成 FFmpeg 编码参数。

        Args:
            target_size: 目标分辨率 (width, height)

        Returns:
            FFmpeg 参数列表
        """
        w, h = target_size
        args: list[str] = []

        if self.format == ProxyFormat.PRORES_PROXY:
            profile_map = {
                'proxy': 'proxy',       # ProRes Proxy
                '422LT': 'lt',          # ProRes 422 LT
                '422': 'standard',      # ProRes 422
                '422HQ': 'hq',          # ProRes 422 HQ
            }
            profile_code = profile_map.get(self.prores_profile, 'standard')
            # Use format filter to convert pixel format before scale,
            # fixing swscaler rejection for exotic input formats (e.g. yuv444p12le)
            args.extend([
                '-vf', f'format=yuv422p10le,scale={w}:{h}:force_original_aspect_ratio=decrease',
                '-c:v', 'prores_ks',
                '-profile:v', profile_code,
                '-pix_fmt', 'yuv422p10le',
            ])

        elif self.format == ProxyFormat.H264:
            args.extend([
                '-vf', f'format=yuv420p,scale={w}:{h}:force_original_aspect_ratio=decrease',
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', str(self.h264_crf),
                '-pix_fmt', 'yuv420p',
            ])

        elif self.format == ProxyFormat.IMAGE_SEQUENCE:
            if self.image_format == 'jpg':
                args.extend([
                    '-vf', f'format=yuvj420p,scale={w}:{h}:force_original_aspect_ratio=decrease',
                    '-c:v', 'mjpeg',
                    '-q:v', '5',
                    '-pix_fmt', 'yuvj420p',
                ])
            else:  # png
                args.extend([
                    '-vf', f'format=rgb24,scale={w}:{h}:force_original_aspect_ratio=decrease',
                    '-c:v', 'png',
                    '-pix_fmt', 'rgb24',
                ])

        return args

    def get_output_filename(
        self,
        source_path: str,
        proxy_key: str
    ) -> str:
        """
        生成代理文件输出路径。

        Args:
            source_path: 源文件路径
            proxy_key: 代理缓存键

        Returns:
            代理文件完整路径
        """
        if self.format == ProxyFormat.IMAGE_SEQUENCE:
            # 图像序列：目录形式
            base_name = os.path.splitext(os.path.basename(source_path))[0]
            return os.path.join(
                self.cache_dir,
                f"{base_name}_{proxy_key}",
                f"%06d.{self.image_format}"
            )
        else:
            # 视频文件：单文件
            ext = self.format.extension
            base_name = os.path.splitext(os.path.basename(source_path))[0]
            return os.path.join(
                self.cache_dir,
                f"{base_name}_{proxy_key}{ext}"
            )


def generate_proxy_key(
    source_path: str,
    source_mtime: float,
    source_size: int,
    proxy_config: DiskProxyConfig,
    color_space: str = 'Rec.709',
    fps: float = 24.0,
) -> str:
    """
    生成代理缓存键。

    代理键包含源文件特征和代理配置，用于判断代理是否有效。
    源文件修改后（mtime/size 变化）或配置变化时，代理自动失效。

    Args:
        source_path: 源文件路径
        source_mtime: 源文件修改时间
        source_size: 源文件大小（字节）
        proxy_config: 代理配置
        color_space: 色彩空间
        fps: 帧率

    Returns:
        代理缓存键（MD5 哈希）

    Examples:
        >>> config = DiskProxyConfig()
        >>> key = generate_proxy_key('/path/to/video.mp4', 1234567890.0, 1000000, config)
        >>> len(key)
        32
    """
    key_parts = [
        source_path,
        str(source_mtime),
        str(source_size),
        proxy_config.format.name,
        proxy_config.resolution.name,
        str(proxy_config.prores_profile),
        str(proxy_config.h264_crf),
        proxy_config.image_format,
        str(proxy_config.target_height),
        color_space,
        f"{fps:.2f}",
        # AUD-35504：Topos 代理编码三参数必须进缓存键——它们直接决定
        # 代理码流（位深/色度保留/固定 QP），变更后旧缓存需失效。
        str(proxy_config.topos_bit_depth),
        str(proxy_config.topos_keep_444),
        str(proxy_config.topos_qp),
    ]
    key_string = '|'.join(key_parts)
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()


def get_source_file_info(source_path: str) -> tuple[float, int]:
    """
    获取源文件信息（mtime 和 size）。

    Args:
        source_path: 源文件路径

    Returns:
        (mtime, size) 元组

    Raises:
        FileNotFoundError: 文件不存在
        OSError: 无法读取文件信息
    """
    stat = os.stat(source_path)
    return (stat.st_mtime, stat.st_size)


# 默认磁盘代理配置
DEFAULT_DISK_PROXY_CONFIG = DiskProxyConfig(
    format=ProxyFormat.PRORES_PROXY,
    resolution=ProxyResolution.HALF,
    prores_profile='proxy',
    target_height=1080,
)


# =============================================================================
# 代理状态（ProxyStatus）- 专业 NLE proxy 缓存体系
# =============================================================================

class ProxyStatus(Enum):
    """
    磁盘代理状态枚举。

    定义代理文件的生命周期状态，用于管理代理生成和播放优先级。

    Attributes:
        MISSING: 代理不存在（需要生成）
        GENERATING: 正在生成中（后台任务进行中）
        READY: 代理已就绪（可以优先使用）
        STALE: 代理过期（源文件已修改，需要重新生成）
        FAILED: 生成失败（不可用）

    状态转换：
        MISSING -> GENERATING -> READY（正常流程）
        MISSING -> GENERATING -> FAILED（失败流程）
        READY -> STALE（源文件修改）
        STALE -> GENERATING -> READY（重新生成）
        ANY -> MISSING（代理被删除）
    """

    MISSING = 'missing'
    GENERATING = 'generating'
    READY = 'ready'
    STALE = 'stale'
    FAILED = 'failed'

    @property
    def can_play_with_proxy(self) -> bool:
        """是否可以用代理播放"""
        return self == ProxyStatus.READY

    @property
    def needs_generation(self) -> bool:
        """是否需要生成代理"""
        return self in (ProxyStatus.MISSING, ProxyStatus.STALE, ProxyStatus.FAILED)

    @property
    def is_generating(self) -> bool:
        """是否正在生成"""
        return self == ProxyStatus.GENERATING

    @staticmethod
    def from_string(status_str: str) -> ProxyStatus:
        """
        从字符串转换。

        Args:
            status_str: 状态字符串

        Returns:
            ProxyStatus 枚举值
        """
        mapping = {
            'missing': ProxyStatus.MISSING,
            'generating': ProxyStatus.GENERATING,
            'ready': ProxyStatus.READY,
            'stale': ProxyStatus.STALE,
            'failed': ProxyStatus.FAILED,
            'none': ProxyStatus.MISSING,  # 兼容旧状态
            'pending': ProxyStatus.GENERATING,  # 兼容旧状态
        }
        return mapping.get(status_str.lower(), ProxyStatus.MISSING)

    def to_string(self) -> str:
        """转换为字符串"""
        return self.value


@dataclass(frozen=True)
class DiskProxyInfo:
    """
    磁盘代理信息。

    包含代理状态、路径、分辨率等完整信息。

    Attributes:
        status: 代理状态
        proxy_path: 代理文件路径（ready 时有效）
        proxy_resolution: 代理分辨率（width, height）
        proxy_format: 代理格式
        source_mtime: 源文件修改时间（用于验证）
        source_size: 源文件大小（用于验证）
        generation_progress: 生成进度（generating 时有效）
        error_message: 错误消息（failed 时有效）
    """

    status: ProxyStatus = ProxyStatus.MISSING
    proxy_path: Optional[str] = None
    proxy_resolution: Optional[Tuple[int, int]] = None
    proxy_format: Optional[ProxyFormat] = None
    source_mtime: float = 0.0
    source_size: int = 0
    generation_progress: float = 0.0  # 0.0-100.0
    error_message: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        """代理是否就绪"""
        return self.status == ProxyStatus.READY and self.proxy_path is not None

    @property
    def needs_generation(self) -> bool:
        """是否需要生成"""
        return self.status.needs_generation

    def with_status(self, status: ProxyStatus) -> DiskProxyInfo:
        """返回新状态的信息"""
        return DiskProxyInfo(
            status=status,
            proxy_path=self.proxy_path,
            proxy_resolution=self.proxy_resolution,
            proxy_format=self.proxy_format,
            source_mtime=self.source_mtime,
            source_size=self.source_size,
            generation_progress=self.generation_progress,
            error_message=self.error_message,
        )

    def with_progress(self, progress: float) -> DiskProxyInfo:
        """返回带进度更新的信息"""
        return DiskProxyInfo(
            status=ProxyStatus.GENERATING,
            proxy_path=self.proxy_path,
            proxy_resolution=self.proxy_resolution,
            proxy_format=self.proxy_format,
            source_mtime=self.source_mtime,
            source_size=self.source_size,
            generation_progress=progress,
            error_message=self.error_message,
        )

    def with_proxy_path(self, path: str, resolution: Tuple[int, int]) -> DiskProxyInfo:
        """返回带代理路径的信息"""
        return DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path=path,
            proxy_resolution=resolution,
            proxy_format=self.proxy_format,
            source_mtime=self.source_mtime,
            source_size=self.source_size,
            generation_progress=100.0,
            error_message=None,
        )


def validate_disk_proxy(
    proxy_info: DiskProxyInfo,
    source_path: str,
) -> DiskProxyInfo:
    """
    验证磁盘代理状态。

    检查代理文件是否存在、源文件是否修改，返回更新后的状态。

    Args:
        proxy_info: 代理信息
        source_path: 源文件路径

    Returns:
        更新后的 DiskProxyInfo
    """
    if proxy_info.status == ProxyStatus.GENERATING:
        # 正在生成，不验证
        return proxy_info

    if proxy_info.proxy_path is None:
        # 无代理路径
        return DiskProxyInfo(
            status=ProxyStatus.MISSING,
            source_mtime=proxy_info.source_mtime,
            source_size=proxy_info.source_size,
        )

    # 检查代理文件是否存在
    if not os.path.isfile(proxy_info.proxy_path):
        return DiskProxyInfo(
            status=ProxyStatus.MISSING,
            source_mtime=proxy_info.source_mtime,
            source_size=proxy_info.source_size,
        )

    # 检查源文件是否修改
    try:
        current_mtime, current_size = get_source_file_info(source_path)
        if current_mtime != proxy_info.source_mtime or current_size != proxy_info.source_size:
            # 源文件已修改，代理过期
            return DiskProxyInfo(
                status=ProxyStatus.STALE,
                proxy_path=proxy_info.proxy_path,
                proxy_resolution=proxy_info.proxy_resolution,
                proxy_format=proxy_info.proxy_format,
                source_mtime=current_mtime,
                source_size=current_size,
            )
    except (FileNotFoundError, OSError):
        # 源文件不存在，代理无效
        return DiskProxyInfo(
            status=ProxyStatus.MISSING,
            source_mtime=proxy_info.source_mtime,
            source_size=proxy_info.source_size,
        )

    # 代理有效
    return DiskProxyInfo(
        status=ProxyStatus.READY,
        proxy_path=proxy_info.proxy_path,
        proxy_resolution=proxy_info.proxy_resolution,
        proxy_format=proxy_info.proxy_format,
        source_mtime=proxy_info.source_mtime,
        source_size=proxy_info.source_size,
    )


def get_disk_proxy_rejection_reason(
    source_metadata: object,
    proxy_metadata: object,
    preview_size: Optional[Tuple[int, int]] = None,
) -> Optional[str]:
    """Return why an opened disk proxy is unsafe for realtime playback.

    Filesystem timestamps cannot detect a partially encoded proxy or a proxy
    accidentally written at source resolution. Those cases otherwise fall
    through to expensive master decoding once playback reaches EOF.
    """
    def _positive_int(value: object) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    source_frames = _positive_int(getattr(source_metadata, "total_frames", 0))
    proxy_frames = _positive_int(getattr(proxy_metadata, "total_frames", 0))
    if source_frames and proxy_frames and proxy_frames < source_frames:
        return f"truncated frame count {proxy_frames} < {source_frames}"

    if preview_size is None:
        return None

    source_width = _positive_int(getattr(source_metadata, "width", 0))
    source_height = _positive_int(getattr(source_metadata, "height", 0))
    proxy_width = _positive_int(getattr(proxy_metadata, "width", 0))
    proxy_height = _positive_int(getattr(proxy_metadata, "height", 0))
    preview_width, preview_height = preview_size
    source_is_larger_than_preview = (
        source_width > preview_width or source_height > preview_height
    )
    proxy_is_full_resolution = (
        proxy_width >= source_width and proxy_height >= source_height
    )
    if source_is_larger_than_preview and proxy_is_full_resolution:
        return (
            f"full-resolution proxy {proxy_width}x{proxy_height} for "
            f"{source_width}x{source_height} source"
        )

    return None


def get_proxy_playback_priority(
    proxy_info: DiskProxyInfo,
    playback_mode: str = 'preview',
) -> str:
    """
    根据代理状态和播放模式确定帧源优先级。

    Args:
        proxy_info: 代理信息
        playback_mode: 播放模式 ('preview', 'paused', 'export')

    Returns:
        'proxy' - 使用代理文件
        'master' - 使用原始文件
        'master_downscaled' - 使用原始文件降级解码

    Note:
        专业 NLE 优先级规则：
        - preview 模式：优先 proxy，fallback 到 master_downscaled
        - paused 模式：使用 master（高质量）
        - export 模式：使用 master（最高质量）
    """
    if playback_mode == 'export':
        # 导出始终使用 master
        return 'master'

    if playback_mode == 'paused':
        # 暂停使用 master（高质量）
        return 'master'

    # preview 模式
    if proxy_info.is_ready:
        # 有就绪的磁盘代理，优先使用
        return 'proxy'

    if proxy_info.status == ProxyStatus.GENERATING:
        # 正在生成，使用 master_downscaled 等待
        return 'master_downscaled'

    # 其他情况（missing, stale, failed），使用 master_downscaled
    return 'master_downscaled'


# 默认代理信息
DEFAULT_DISK_PROXY_INFO = DiskProxyInfo(
    status=ProxyStatus.MISSING,
)


# =============================================================================
# 播放代理状态（PlaybackProxyState）- P1-1
# =============================================================================

class PlaybackProxyState(Enum):
    """
    播放代理状态枚举。

    用于 UI 显示当前播放使用的帧源状态。
    专业 NLE 优先级：disk_proxy > memory_proxy > master_downscaled > master

    Attributes:
        USING_ORIGINAL: 使用原始素材（未降级）
        USING_MEMORY_PROXY: 使用内存降级代理（实时解码降级）
        USING_DISK_PROXY: 使用磁盘代理文件（专业 NLE 优化）
        GENERATING_PROXY: 正在生成代理（后台任务）

    Note:
        - USING_DISK_PROXY: 最高优先级，4K ProRes 播放首选
        - USING_MEMORY_PROXY: proxy 未就绪时的 fallback
        - USING_ORIGINAL: 小视频无需代理，或导出/暂停模式
        - GENERATING_PROXY: 代理生成中，使用 memory proxy 等待
    """

    USING_ORIGINAL = 'using_original'
    USING_MEMORY_PROXY = 'using_memory_proxy'
    USING_DISK_PROXY = 'using_disk_proxy'
    GENERATING_PROXY = 'generating_proxy'

    @property
    def display_name(self) -> str:
        """
        获取 UI 显示名称。

        Returns:
            显示名称字符串
        """
        mapping = {
            PlaybackProxyState.USING_ORIGINAL: 'Using original',
            PlaybackProxyState.USING_MEMORY_PROXY: 'Using memory proxy',
            PlaybackProxyState.USING_DISK_PROXY: 'Using disk proxy',
            PlaybackProxyState.GENERATING_PROXY: 'Generating proxy',
        }
        return mapping[self]

    @property
    def is_proxy(self) -> bool:
        """
        是否使用代理（磁盘或内存）。

        Returns:
            True 表示使用代理
        """
        return self in (
            PlaybackProxyState.USING_DISK_PROXY,
            PlaybackProxyState.USING_MEMORY_PROXY,
        )

    @property
    def is_disk_proxy(self) -> bool:
        """
        是否使用磁盘代理。

        Returns:
            True 表示使用磁盘代理
        """
        return self == PlaybackProxyState.USING_DISK_PROXY

    @property
    def is_generating(self) -> bool:
        """
        是否正在生成代理。

        Returns:
            True 表示正在生成
        """
        return self == PlaybackProxyState.GENERATING_PROXY

    @staticmethod
    def from_disk_proxy_info(
        disk_proxy_info: Optional[DiskProxyInfo],
        has_disk_proxy_source: bool,
        has_memory_proxy_source: bool,
        playback_mode: str = 'preview',
    ) -> PlaybackProxyState:
        """
        从磁盘代理信息推断播放状态。

        Args:
            disk_proxy_info: 磁盘代理信息
            has_disk_proxy_source: 是否已打开磁盘代理源
            has_memory_proxy_source: 是否有内存代理源
            playback_mode: 播放模式 ('preview', 'paused', 'export')

        Returns:
            PlaybackProxyState 枚举值

        Examples:
            >>> info = DiskProxyInfo(status=ProxyStatus.READY, proxy_path='/path/to/proxy.mov')
            >>> PlaybackProxyState.from_disk_proxy_info(info, True, False, 'preview')
            PlaybackProxyState.USING_DISK_PROXY

            >>> info = DiskProxyInfo(status=ProxyStatus.GENERATING)
            >>> PlaybackProxyState.from_disk_proxy_info(info, False, True, 'preview')
            PlaybackProxyState.GENERATING_PROXY
        """
        # 导出/暂停模式，总是使用原始
        if playback_mode in ('paused', 'export'):
            return PlaybackProxyState.USING_ORIGINAL

        # disk_proxy_info 为 None，检查内存代理
        if disk_proxy_info is None:
            if has_memory_proxy_source:
                return PlaybackProxyState.USING_MEMORY_PROXY
            return PlaybackProxyState.USING_ORIGINAL

        # 磁盘代理已打开
        if has_disk_proxy_source and disk_proxy_info.is_ready:
            return PlaybackProxyState.USING_DISK_PROXY

        # 磁盘代理正在生成
        if disk_proxy_info.status == ProxyStatus.GENERATING:
            if has_memory_proxy_source:
                return PlaybackProxyState.GENERATING_PROXY
            # 没有 fallback，显示为原始
            return PlaybackProxyState.USING_ORIGINAL

        # 磁盘代理未就绪（missing, stale, failed）
        if has_memory_proxy_source:
            return PlaybackProxyState.USING_MEMORY_PROXY

        # 没有 proxy，使用原始
        return PlaybackProxyState.USING_ORIGINAL


def get_proxy_state_display(
    state: PlaybackProxyState,
    progress: float = 0.0,
) -> str:
    """
    获取代理状态显示字符串（带进度）。

    Args:
        state: 播放代理状态
        progress: 生成进度（0.0-100.0）

    Returns:
        显示字符串

    Examples:
        >>> get_proxy_state_display(PlaybackProxyState.USING_DISK_PROXY)
        'Using disk proxy'

        >>> get_proxy_state_display(PlaybackProxyState.GENERATING_PROXY, 45.0)
        'Generating proxy (45%)'
    """
    if state == PlaybackProxyState.GENERATING_PROXY:
        if progress > 0:
            return f"Generating proxy ({int(progress)}%)"
        return "Generating proxy"

    return state.display_name
