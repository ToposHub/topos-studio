"""
Decoded Frame Module
定义解码后帧的数据结构。

## 职责

- DecodedFrame: 解码后的帧数据容器
- PlaneInfo: 平面信息（用于 YUV 等多平面格式）
- PixelFormatInfo: 像素格式信息

## 设计原则

- 支持打包格式（RGB/RGBA）和平面格式（YUV/YUVA）
- 完整类型注解
- 不依赖任何外部库（GStreamer/OpenCV/PyAV）
- 与 NumPy 兼容，但不强制依赖
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .metadata import VideoMetadata


@dataclass(frozen=True)
class PixelFormatInfo:
    """
    像素格式信息。

    描述视频帧的像素布局，支持各种 YUV 和 RGB 格式。

    Attributes:
        name: 格式名称（如 'yuv420p', 'yuv422p10le', 'rgb48le', 'rgba'）
        planes: 平面数量（1=打包，2-4=平面）
        subsampling: 色度子采样 (J, A, B) 格式（如 (4, 2, 0) 表示 4:2:0）
        bit_depth: 每分量位深度
        has_alpha: 是否包含 Alpha 通道
    """

    name: str
    planes: int
    subsampling: tuple[int, int, int]
    bit_depth: int
    has_alpha: bool = False

    @property
    def is_rgb(self) -> bool:
        """是否为 RGB 格式"""
        return self.name.startswith('rgb') or self.name.startswith('gbr')

    @property
    def is_yuv(self) -> bool:
        """是否为 YUV 格式"""
        return self.name.startswith('yuv')

    @property
    def is_planar(self) -> bool:
        """是否为平面格式"""
        return self.planes > 1

    @property
    def chroma_width_divisor(self) -> int:
        """
        色度平面宽度除数。

        Returns:
            除数值（如 4:2:0 为 2，4:4:4 为 1）
        """
        j, a, b = self.subsampling
        if j == 4 and a == 4:
            return 1  # 4:4:4
        elif j == 4 and a == 2:
            return 2  # 4:2:0 or 4:2:2
        elif j == 4 and a == 1:
            return 4  # 4:1:1 or 4:1:0
        return 1

    @property
    def chroma_height_divisor(self) -> int:
        """
        色度平面高度除数。

        Returns:
            除数值（如 4:2:0 为 2，4:2:2 为 1）
        """
        j, a, b = self.subsampling
        if j == 4 and a == 4:
            return 1  # 4:4:4
        elif j == 4 and b == 0:
            return 2  # 4:2:0
        elif j == 4 and b == 2:
            return 1  # 4:2:2
        return 1


# 预定义的常见像素格式
PIXEL_FORMATS: dict[str, PixelFormatInfo] = {
    # 8-bit RGB
    'rgb24': PixelFormatInfo('rgb24', 1, (4, 4, 4), 8, False),
    'rgba': PixelFormatInfo('rgba', 1, (4, 4, 4), 8, True),
    'bgr24': PixelFormatInfo('bgr24', 1, (4, 4, 4), 8, False),
    'bgra': PixelFormatInfo('bgra', 1, (4, 4, 4), 8, True),

    # 高位深 RGB (PyAV 使用 le 后缀的小端格式)
    'rgb48': PixelFormatInfo('rgb48', 1, (4, 4, 4), 16, False),
    'rgba64': PixelFormatInfo('rgba64', 1, (4, 4, 4), 16, True),
    'rgb48le': PixelFormatInfo('rgb48le', 1, (4, 4, 4), 16, False),
    'rgba64le': PixelFormatInfo('rgba64le', 1, (4, 4, 4), 16, True),

    # 8-bit YUV
    'yuv420p': PixelFormatInfo('yuv420p', 3, (4, 2, 0), 8, False),
    'yuv422p': PixelFormatInfo('yuv422p', 3, (4, 2, 2), 8, False),
    'yuv444p': PixelFormatInfo('yuv444p', 3, (4, 4, 4), 8, False),
    'yuva420p': PixelFormatInfo('yuva420p', 4, (4, 2, 0), 8, True),
    'yuva422p': PixelFormatInfo('yuva422p', 4, (4, 2, 2), 8, True),
    'yuva444p': PixelFormatInfo('yuva444p', 4, (4, 4, 4), 8, True),

    # Semi-planar 4:2:0 YUV. Hardware decoders commonly expose these as a
    # single contiguous ndarray, but they still require the two-plane GPU path.
    'nv12': PixelFormatInfo('nv12', 2, (4, 2, 0), 8, False),
    'nv21': PixelFormatInfo('nv21', 2, (4, 2, 0), 8, False),

    # 10-bit YUV (little-endian)
    'yuv420p10le': PixelFormatInfo('yuv420p10le', 3, (4, 2, 0), 10, False),
    'yuv422p10le': PixelFormatInfo('yuv422p10le', 3, (4, 2, 2), 10, False),
    'yuv444p10le': PixelFormatInfo('yuv444p10le', 3, (4, 4, 4), 10, False),
    'p010': PixelFormatInfo('p010', 2, (4, 2, 0), 10, False),
    'p010le': PixelFormatInfo('p010le', 2, (4, 2, 0), 10, False),

    # 10-bit YUVA (ProRes 4444 等)
    'yuva420p10le': PixelFormatInfo('yuva420p10le', 4, (4, 2, 0), 10, True),
    'yuva422p10le': PixelFormatInfo('yuva422p10le', 4, (4, 2, 2), 10, True),
    'yuva444p10le': PixelFormatInfo('yuva444p10le', 4, (4, 4, 4), 10, True),

    # 12-bit YUV (little-endian)
    'yuv420p12le': PixelFormatInfo('yuv420p12le', 3, (4, 2, 0), 12, False),
    'yuv422p12le': PixelFormatInfo('yuv422p12le', 3, (4, 2, 2), 12, False),
    'yuv444p12le': PixelFormatInfo('yuv444p12le', 3, (4, 4, 4), 12, False),

    # 12-bit YUVA (ProRes 4444 XQ)
    'yuva420p12le': PixelFormatInfo('yuva420p12le', 4, (4, 2, 0), 12, True),
    'yuva422p12le': PixelFormatInfo('yuva422p12le', 4, (4, 2, 2), 12, True),
    'yuva444p12le': PixelFormatInfo('yuva444p12le', 4, (4, 4, 4), 12, True),

    # 16-bit YUV
    'yuv420p16le': PixelFormatInfo('yuv420p16le', 3, (4, 2, 0), 16, False),
    'yuv422p16le': PixelFormatInfo('yuv422p16le', 3, (4, 2, 2), 16, False),
    'yuv444p16le': PixelFormatInfo('yuv444p16le', 3, (4, 4, 4), 16, False),
    'p016': PixelFormatInfo('p016', 2, (4, 2, 0), 16, False),
    'p016le': PixelFormatInfo('p016le', 2, (4, 2, 0), 16, False),

    # 16-bit YUVA
    'yuva420p16le': PixelFormatInfo('yuva420p16le', 4, (4, 2, 0), 16, True),
    'yuva422p16le': PixelFormatInfo('yuva422p16le', 4, (4, 2, 2), 16, True),
    'yuva444p16le': PixelFormatInfo('yuva444p16le', 4, (4, 4, 4), 16, True),

    # GBRP (planar RGB) - 用于平面 RGB 格式
    'gbrp': PixelFormatInfo('gbrp', 3, (4, 4, 4), 8, False),
    'gbrp10le': PixelFormatInfo('gbrp10le', 3, (4, 4, 4), 10, False),
    'gbrp12le': PixelFormatInfo('gbrp12le', 3, (4, 4, 4), 12, False),
    'gbrp16le': PixelFormatInfo('gbrp16le', 3, (4, 4, 4), 16, False),

    # GBRP HALF（Topos Image 浮点样本域，spec §15 / ADR-C057 / M1-F2）：
    # G,B,R 平面序 float16 线性值平面——dtype 语义为浮点，bit_depth=16 仅
    # 描述存储位宽，**无满刻度契约**（线性 HDR 值域可 <0 / >1；归一化/
    # 数值语义由消费方按 float 域处理，不做整数满刻度缩放）。仅
    # sample_kind=HALF 的 .toos 文件产出该名。
    'gbrph16le': PixelFormatInfo('gbrph16le', 3, (4, 4, 4), 16, False),

    # GBRAP (planar RGBA) - 用于平面 RGBA 格式
    'gbrap': PixelFormatInfo('gbrap', 4, (4, 4, 4), 8, True),
    'gbrap10le': PixelFormatInfo('gbrap10le', 4, (4, 4, 4), 10, True),
    'gbrap12le': PixelFormatInfo('gbrap12le', 4, (4, 4, 4), 12, True),
    'gbrap16le': PixelFormatInfo('gbrap16le', 4, (4, 4, 4), 16, True),

    # Topos Video Codec 2.0（自研，阶段 6）：YUV 4:2:2 全高色度 + 全分辨率
    # Alpha（不子采样），uint16 满刻度码值（0..2^N−1，不左移）。
    # 无 Alpha 的输出与 'yuv422p10le' 语义逐位一致，直接复用该名（零新增
    # 下游接线）；带 Alpha 时色彩面 10-bit、Alpha 面位深独立为 8/10/12
    # （mode2 近似精度）或 16（mode1 无损），故单独命名 a{N}，逐面位深以
    # PlaneInfo.bit_depth 为权威。
    'topos_yuva422p10a8': PixelFormatInfo('topos_yuva422p10a8', 4, (4, 2, 2), 10, True),
    'topos_yuva422p10a10': PixelFormatInfo('topos_yuva422p10a10', 4, (4, 2, 2), 10, True),
    'topos_yuva422p10a12': PixelFormatInfo('topos_yuva422p10a12', 4, (4, 2, 2), 10, True),
    'topos_yuva422p10a16': PixelFormatInfo('topos_yuva422p10a16', 4, (4, 2, 2), 10, True),
    # R4.1：12-bit 4:2:2 变体（v1.2 枚举；Alpha 面位深语义同上）
    'topos_yuva422p12a8': PixelFormatInfo('topos_yuva422p12a8', 4, (4, 2, 2), 12, True),
    'topos_yuva422p12a10': PixelFormatInfo('topos_yuva422p12a10', 4, (4, 2, 2), 12, True),
    'topos_yuva422p12a12': PixelFormatInfo('topos_yuva422p12a12', 4, (4, 2, 2), 12, True),
    'topos_yuva422p12a16': PixelFormatInfo('topos_yuva422p12a16', 4, (4, 2, 2), 12, True),
    # R4.2：4:4:4 变体（v1.3 枚举，色彩面无子采样；Alpha 面位深语义同上）
    'topos_yuva444p10a8': PixelFormatInfo('topos_yuva444p10a8', 4, (4, 4, 4), 10, True),
    'topos_yuva444p10a10': PixelFormatInfo('topos_yuva444p10a10', 4, (4, 4, 4), 10, True),
    'topos_yuva444p10a12': PixelFormatInfo('topos_yuva444p10a12', 4, (4, 4, 4), 10, True),
    'topos_yuva444p10a16': PixelFormatInfo('topos_yuva444p10a16', 4, (4, 4, 4), 10, True),
    'topos_yuva444p12a8': PixelFormatInfo('topos_yuva444p12a8', 4, (4, 4, 4), 12, True),
    'topos_yuva444p12a10': PixelFormatInfo('topos_yuva444p12a10', 4, (4, 4, 4), 12, True),
    'topos_yuva444p12a12': PixelFormatInfo('topos_yuva444p12a12', 4, (4, 4, 4), 12, True),
    'topos_yuva444p12a16': PixelFormatInfo('topos_yuva444p12a16', 4, (4, 4, 4), 12, True),
    # R4.3：GBR 变体（v1.4 枚举，平面序 G,B,R；Alpha 面位深语义同上）
    'topos_gbrap10a8': PixelFormatInfo('topos_gbrap10a8', 4, (4, 4, 4), 10, True),
    'topos_gbrap10a10': PixelFormatInfo('topos_gbrap10a10', 4, (4, 4, 4), 10, True),
    'topos_gbrap10a12': PixelFormatInfo('topos_gbrap10a12', 4, (4, 4, 4), 10, True),
    'topos_gbrap10a16': PixelFormatInfo('topos_gbrap10a16', 4, (4, 4, 4), 10, True),
    'topos_gbrap12a8': PixelFormatInfo('topos_gbrap12a8', 4, (4, 4, 4), 12, True),
    'topos_gbrap12a10': PixelFormatInfo('topos_gbrap12a10', 4, (4, 4, 4), 12, True),
    'topos_gbrap12a12': PixelFormatInfo('topos_gbrap12a12', 4, (4, 4, 4), 12, True),
    'topos_gbrap12a16': PixelFormatInfo('topos_gbrap12a16', 4, (4, 4, 4), 12, True),

    # TRAW（Topos RAW，v1.6 批 1）：Bayer CFA 4 相位平面 R/Gr/Gb/B——
    # 每平面均为 W/2×H/2（(4,2,0) 的双维减半语义，非色度下采样）。
    # uint16 满刻度 12-bit 码值；无 Alpha（TRAW 与 alpha 互斥）。
    # debayer 由 GPU 侧 CfaGpuDebayer 承担（cfa_gpu_debayer.py）。
    'topos_cfa12le': PixelFormatInfo('topos_cfa12le', 4, (4, 2, 0), 12, False),
    # 批 4：16-bit linear 归档档（raw16-*；transfer=linear）
    'topos_cfa16le': PixelFormatInfo('topos_cfa16le', 4, (4, 2, 0), 16, False),
}


def get_pixel_format(name: str) -> Optional[PixelFormatInfo]:
    """
    根据名称获取像素格式信息。

    Args:
        name: 格式名称

    Returns:
        PixelFormatInfo 或 None（未找到）
    """
    return PIXEL_FORMATS.get(name)


@dataclass(frozen=True)
class PlaneInfo:
    """
    单个平面的信息（用于平面格式如 YUV）。

    Attributes:
        index: 平面索引（0, 1, 2, 3 对应 Y, U, V, A 或 R, G, B, A）
        width: 平面宽度（像素）
        height: 平面高度（像素）
        stride: 行字节数（可能包含 padding）
        component: 分量类型（'Y', 'U', 'V', 'A', 'R', 'G', 'B'）
        dtype: 数据类型（np.uint8, np.uint16, np.float32 等）
        bit_depth: 该平面位深度（8, 10, 12, 16, 32）
    """

    index: int
    width: int
    height: int
    stride: int
    component: str
    dtype: np.dtype
    bit_depth: int

    @classmethod
    def create(
        cls,
        index: int,
        width: int,
        height: int,
        component: str,
        dtype: np.dtype,
        bit_depth: int,
        stride: Optional[int] = None,
    ) -> 'PlaneInfo':
        """
        创建 PlaneInfo 实例的便捷方法。

        Args:
            index: 平面索引
            width: 平面宽度
            height: 平面高度
            component: 分量类型
            dtype: 数据类型（可以是 np.dtype 或类型如 np.uint16）
            bit_depth: 位深度
            stride: 行字节数（默认为 width * dtype.itemsize）

        Returns:
            PlaneInfo 实例
        """
        import numpy as np
        # 确保 dtype 是 np.dtype 对象
        actual_dtype = np.dtype(dtype) if not isinstance(dtype, np.dtype) else dtype
        if stride is None:
            stride = width * actual_dtype.itemsize
        return cls(
            index=index,
            width=width,
            height=height,
            stride=stride,
            component=component,
            dtype=actual_dtype,
            bit_depth=bit_depth,
        )


@dataclass
class DecodedFrame:
    """
    解码后的帧数据容器。

    支持打包格式（RGB/RGBA，单个 ndarray）和平面格式（YUV/YUVA，多个平面）。
    设计为与解码器无关，可由 GStreamer、OpenCV、PyAV 等填充。

    Attributes:
        frame_index: 帧号（从 0 开始）
        pts: Presentation Time Stamp（时间戳），可能为 None
        time_seconds: 该帧对应的时间点（秒）
        data: 打包格式的帧数据 (H, W, C)，RGB/RGBA/BGR/BGRA
        planes: 平面格式的平面数据元组 (Y, U, V, [A])
        pixel_format: 像素格式名称（如 'bgra', 'yuv420p'）
        dtype: 数据类型（如 uint8, uint16, float32）
        width: 帧宽度（像素）
        height: 帧高度（像素）
        metadata: 视频元数据引用
        is_proxy: 是否为代理帧（低分辨率版本）
        plane_infos: 各平面的详细信息（可选）
        extra: 额外元数据（如 reformat 状态等）
    """

    frame_index: int
    pts: Optional[int]
    time_seconds: float
    data: Optional[np.ndarray] = None  # 打包格式
    planes: tuple[np.ndarray, ...] = field(default_factory=tuple)  # 平面格式
    pixel_format: str = 'bgra'
    dtype: Optional[np.dtype] = None
    width: int = 0
    height: int = 0
    metadata: Optional[VideoMetadata] = None
    is_proxy: bool = False
    plane_infos: Optional[tuple[PlaneInfo, ...]] = None
    extra: Optional[dict] = None  # 额外元数据（如 proxy reformat 状态）

    def __post_init__(self) -> None:
        """初始化派生属性"""
        if self.dtype is None:
            if self.data is not None:
                object.__setattr__(self, 'dtype', self.data.dtype)
            elif self.planes:
                object.__setattr__(self, 'dtype', self.planes[0].dtype)

        if self.width == 0 or self.height == 0:
            if self.data is not None:
                h, w = self.data.shape[:2]
                object.__setattr__(self, 'width', w)
                object.__setattr__(self, 'height', h)
            elif self.planes:
                h, w = self.planes[0].shape[:2]
                object.__setattr__(self, 'width', w)
                object.__setattr__(self, 'height', h)

    @property
    def is_packed(self) -> bool:
        """
        是否为打包格式（RGB/RGBA）。

        Returns:
            打包格式返回 True，平面格式返回 False
        """
        return self.data is not None

    @property
    def is_planar(self) -> bool:
        """
        是否为平面格式（YUV/YUVA）。

        Returns:
            平面格式返回 True，打包格式返回 False
        """
        return len(self.planes) > 0

    @property
    def format_info(self) -> Optional[PixelFormatInfo]:
        """
        获取像素格式信息。

        Returns:
            PixelFormatInfo 或 None
        """
        return PIXEL_FORMATS.get(self.pixel_format)

    @property
    def has_alpha(self) -> bool:
        """
        是否包含 Alpha 通道。

        Returns:
            有 Alpha 通道返回 True
        """
        fmt = self.format_info
        if fmt:
            return fmt.has_alpha
        # 根据格式名称推断
        return 'a' in self.pixel_format.lower() or self.pixel_format in ('rgba', 'bgra', 'rgba64', 'rgba64le')

    @property
    def bit_depth(self) -> int:
        """
        获取帧位深度。

        Returns:
            位深度（8, 10, 12, 16 等）
        """
        fmt = self.format_info
        if fmt:
            return fmt.bit_depth
        # 从 plane_infos 推断
        if self.plane_infos and len(self.plane_infos) > 0:
            return self.plane_infos[0].bit_depth
        # 从 dtype 推断
        if self.dtype is not None:
            if self.dtype == np.uint8:
                return 8
            elif self.dtype == np.uint16:
                return 16  # 可能是 10/12/16-bit
            elif self.dtype == np.float32:
                return 32
        # 从 pixel_format 名称推断
        import re
        match = re.search(r'(\d+)(?:le|be)?$', self.pixel_format)
        if match:
            return int(match.group(1))
        # 默认 8-bit
        return 8

    @property
    def shape(self) -> tuple[int, int, int] | tuple[int, int]:
        """
        获取帧形状。

        Returns:
            (height, width, channels) 或 (height, width)
        """
        if self.is_packed:
            return self.data.shape  # type: ignore
        elif self.is_planar:
            return (self.height, self.width)
        return (self.height, self.width)

    def to_rgb(self) -> Optional[np.ndarray]:
        """
        转换为 RGB 格式（如果可能）。

        Returns:
            RGB 数组或 None
        """
        if self.is_packed and self.data is not None:
            import numpy as np
            if self.pixel_format in ('rgb24', 'rgb48', 'rgb48le'):
                return self.data
            elif self.pixel_format in ('rgba', 'rgba64', 'rgba64le'):
                return self.data[..., :3]
            elif self.pixel_format in ('bgr24', 'bgra', 'bgra64'):
                # BGR -> RGB
                # AUD-34803：np.flip 产生负 stride 视图，下游按连续内存
                # 消费的写入器（编码器/GL 上传/缓冲协议）会踩坑——统一
                # 交出 C 连续数组。
                return np.ascontiguousarray(np.flip(self.data[..., :3], axis=-1))
        return None

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"DecodedFrame(frame={self.frame_index}, "
            f"size={self.width}x{self.height}, "
            f"fmt={self.pixel_format}, "
            f"pts={self.pts}, "
            f"proxy={self.is_proxy})"
        )


# 为了支持类型注解，延迟导入 numpy
if TYPE_CHECKING:
    pass
