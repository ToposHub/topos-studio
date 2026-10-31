"""
Pixel Format Registry Module
像素格式注册表，提供完整的像素格式描述和查询功能。

## 职责

- Subsampling: YUV 色度子采样枚举
- ColorModel: 颜色模型枚举（YUV/RGB/GRAY）
- PixelFormatInfo: 扩展的像素格式信息
- PixelFormatRegistry: 像素格式注册表

## 设计原则

- 与 frame.py 中的 PixelFormatInfo 兼容
- 支持从 subsampling 元组推断 Subsampling 枚举
- 提供 20+ 种常见格式的完整注册
- 为 shader 处理提供色度除数计算

## 使用示例

```python
from src.shared.media.pixel_format import PixelFormatRegistry, Subsampling, ColorModel

# 查询单个格式
fmt = PixelFormatRegistry.get('yuv420p10le')
print(fmt.bit_depth)  # 10
print(fmt.subsampling)  # Subsampling.YUV_420

# 按属性查询
formats = PixelFormatRegistry.get_by_properties(
    color_model=ColorModel.YUV,
    bit_depth=10,
    has_alpha=False
)

# 检查格式类型
if PixelFormatRegistry.is_yuv('yuv422p10le'):
    # 处理 YUV 格式
    div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv422p10le')
```
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


class Subsampling(Enum):
    """
    YUV 色度子采样模式。

    表示方式为 J:a:b 格式：
    - J: 每 4 个水平像素的亮度样本数（通常为 4）
    - a: 第一行的色度样本数
    - b: 第二行的色度样本数

    色度分辨率：
    - YUV_444: 无子采样，色度全分辨率
    - YUV_422: 水平 1/2，垂直全分辨率
    - YUV_420: 水平 1/2，垂直 1/2
    - YUV_411: 水平 1/4，垂直全分辨率
    - YUV_410: 水平 1/4，垂直 1/2
      （AUD-35502：4:1:0 垂直子采样为 1/2，与 chroma_height_divisor
      对 b==0 返回 2 的实现一致，原文档"垂直 1/4"有误）
    """

    YUV_444 = (4, 4, 4)  # 4:4:4 无子采样
    YUV_422 = (4, 2, 2)  # 4:2:2 水平子采样
    YUV_420 = (4, 2, 0)  # 4:2:0 水平+垂直子采样
    YUV_411 = (4, 1, 1)  # 4:1:1 水平 1/4 子采样
    YUV_410 = (4, 1, 0)  # 4:1:0 水平 1/4 + 垂直 1/2 子采样
    # AUD-35500：NONE 不能与 YUV_444 同值——Python Enum 对重复值会生成
    # 别名（NONE is YUV_444），导致所有以 Subsampling.NONE 注册的
    # RGB/GRAY 格式在 == 比较下等同于 YUV_4:4:4，get_by_properties(
    # subsampling=YUV_444) 会混入 rgb/gray 等非 YUV 格式。(0, 0, 0)
    # 表示"无色度平面/无 J:a:b 子采样语义"，与 J:a:b 值域天然不冲突。
    NONE = (0, 0, 0)  # 无子采样（RGB/GRAY，无色度平面）

    @property
    def chroma_width_divisor(self) -> int:
        """
        色度宽度除数。

        Returns:
            除数值（用于计算色度平面宽度）；NONE（RGB/GRAY）无色度
            平面，返回 1（等价于不除）
        """
        if self is Subsampling.NONE:
            return 1
        j, a, _ = self.value
        return j // a

    @property
    def chroma_height_divisor(self) -> int:
        """
        色度高度除数。

        Returns:
            除数值（用于计算色度平面高度）；NONE（RGB/GRAY）无色度
            平面，返回 1（等价于不除）
        """
        if self is Subsampling.NONE:
            return 1
        j, _, b = self.value
        if b == 0:
            return 2  # 4:2:0 or 4:1:0
        return j // b if b > 0 else 1

    @classmethod
    def from_tuple(cls, t: tuple[int, int, int]) -> Subsampling:
        """
        从元组推断子采样模式。

        Args:
            t: (J, a, b) 元组

        Returns:
            对应的 Subsampling 枚举值

        Raises:
            ValueError: 无法识别的子采样模式
        """
        for member in cls:
            if member.value == t:
                return member
        raise ValueError(f"Unknown subsampling: {t}")


class ColorModel(Enum):
    """
    颜色模型。

    - YUV: 亮度/色度模型（电视、视频）
    - RGB: 红绿蓝模型（计算机图形）
    - GRAY: 灰度模型
    """

    YUV = 'yuv'
    RGB = 'rgb'
    GRAY = 'gray'


@dataclass(frozen=True)
class PixelFormatInfo:
    """
    扩展的像素格式信息。

    描述视频帧的完整像素格式信息，支持各种 YUV 和 RGB 格式。

    Attributes:
        name: 格式名称（如 'yuv420p', 'yuv422p10le', 'rgb48le'）
        planes: 平面数量（1=打包，3=YUV 平面，4=YUVA 平面）
        subsampling: 色度子采样模式
        bit_depth: 每分量位深度（8/10/12/14/16/32）
        has_alpha: 是否包含 Alpha 通道
        color_model: 颜色模型（YUV/RGB/GRAY）
        chroma_bits_per_pixel: 色度每像素位数（用于存储计算）
    """

    name: str
    planes: int
    subsampling: Subsampling
    bit_depth: int
    has_alpha: bool
    color_model: ColorModel
    chroma_bits_per_pixel: int

    @property
    def is_yuv(self) -> bool:
        """是否为 YUV 格式"""
        return self.color_model == ColorModel.YUV

    @property
    def is_rgb(self) -> bool:
        """是否为 RGB 格式"""
        return self.color_model == ColorModel.RGB

    @property
    def is_gray(self) -> bool:
        """是否为灰度格式"""
        return self.color_model == ColorModel.GRAY

    @property
    def is_planar(self) -> bool:
        """是否为平面格式"""
        return self.planes > 1

    @property
    def is_packed(self) -> bool:
        """是否为打包格式"""
        return self.planes == 1

    @property
    def chroma_width_divisor(self) -> int:
        """色度宽度除数"""
        return self.subsampling.chroma_width_divisor

    @property
    def chroma_height_divisor(self) -> int:
        """色度高度除数"""
        return self.subsampling.chroma_height_divisor

    @property
    def bits_per_pixel(self) -> float:
        """
        每像素平均位数。

        计算方式：
        - RGB: bit_depth * channels
        - YUV: bit_depth * (1 + chroma_factor)
          其中 chroma_factor 取决于子采样
        """
        if self.color_model == ColorModel.RGB:
            channels = 4 if self.has_alpha else 3
            return float(self.bit_depth * channels)

        if self.color_model == ColorModel.GRAY:
            return float(self.bit_depth)

        # YUV: Y + U * (1/div_w * 1/div_h) + V * (1/div_w * 1/div_h)
        div_w = self.chroma_width_divisor
        div_h = self.chroma_height_divisor
        chroma_factor = 2.0 / (div_w * div_h)
        result = self.bit_depth * (1.0 + chroma_factor)
        if self.has_alpha:
            result += self.bit_depth
        return result


class PixelFormatRegistry:
    """
    像素格式注册表。

    管理所有已注册的像素格式，提供查询和属性访问功能。

    用法：
        # 查询单个格式
        fmt = PixelFormatRegistry.get('yuv420p')

        # 按属性查询
        formats = PixelFormatRegistry.get_by_properties(
            color_model=ColorModel.YUV,
            bit_depth=10
        )

        # 类型检查
        if PixelFormatRegistry.is_yuv('yuv422p'):
            ...
    """

    _formats: ClassVar[dict[str, PixelFormatInfo]] = {}
    _initialized: ClassVar[bool] = False

    @classmethod
    def _ensure_initialized(cls) -> None:
        """确保注册表已初始化"""
        if not cls._initialized:
            cls._register_common_formats()
            cls._initialized = True

    @classmethod
    def _register_common_formats(cls) -> None:
        """注册常见像素格式"""
        # ========================================
        # YUV 8-bit 系列
        # ========================================

        # YUV 4:2:0 8-bit
        cls._register(PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12  # 8-bit 4:2:0 = 1.5×8：Y 8 + U 2 + V 2 = 12 bits/pixel
        ))

        # YUV 4:2:2 8-bit
        cls._register(PixelFormatInfo(
            name='yuv422p',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=16  # 4 + 2 + 2 = 8 bits/2 pixels = 16 bits/pixel
        ))

        # YUV 4:4:4 8-bit
        cls._register(PixelFormatInfo(
            name='yuv444p',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24  # 8 * 3 = 24 bits/pixel
        ))

        # YUVA 8-bit (带 Alpha)
        cls._register(PixelFormatInfo(
            name='yuva420p',
            planes=4,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        ))

        cls._register(PixelFormatInfo(
            name='yuva422p',
            planes=4,
            subsampling=Subsampling.YUV_422,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=16
        ))

        cls._register(PixelFormatInfo(
            name='yuva444p',
            planes=4,
            subsampling=Subsampling.YUV_444,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24
        ))

        # ========================================
        # YUV 10-bit 系列 (little-endian)
        # ========================================

        cls._register(PixelFormatInfo(
            name='yuv420p10le',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=15
        ))

        cls._register(PixelFormatInfo(
            name='yuv422p10le',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=20
        ))

        cls._register(PixelFormatInfo(
            name='yuv444p10le',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=30
        ))

        # 10-bit big-endian variants
        cls._register(PixelFormatInfo(
            name='yuv420p10be',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=15
        ))

        cls._register(PixelFormatInfo(
            name='yuv422p10be',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=20
        ))

        cls._register(PixelFormatInfo(
            name='yuv444p10be',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=30
        ))

        # ========================================
        # YUV 12-bit 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='yuv420p12le',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=12,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=18
        ))

        cls._register(PixelFormatInfo(
            name='yuv422p12le',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=12,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24
        ))

        cls._register(PixelFormatInfo(
            name='yuv444p12le',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=12,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=36
        ))

        # ========================================
        # YUV 16-bit 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='yuv420p16le',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24
        ))

        cls._register(PixelFormatInfo(
            name='yuv422p16le',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=32
        ))

        cls._register(PixelFormatInfo(
            name='yuv444p16le',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=48
        ))

        # ========================================
        # RGB 8-bit 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='rgb24',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=24
        ))

        cls._register(PixelFormatInfo(
            name='rgba',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        ))

        cls._register(PixelFormatInfo(
            name='bgr24',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=24
        ))

        cls._register(PixelFormatInfo(
            name='bgra',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        ))

        # ========================================
        # RGB 16-bit 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='rgb48le',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=48
        ))

        cls._register(PixelFormatInfo(
            name='rgba64le',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=64
        ))

        cls._register(PixelFormatInfo(
            name='rgb48be',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=48
        ))

        cls._register(PixelFormatInfo(
            name='rgba64be',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=64
        ))

        # ========================================
        # GBR (Planar RGB) 系列
        # ========================================

        # 8-bit
        cls._register(PixelFormatInfo(
            name='gbrp',
            planes=3,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=24
        ))

        cls._register(PixelFormatInfo(
            name='gbrap',
            planes=4,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        ))

        # 10-bit
        cls._register(PixelFormatInfo(
            name='gbrp10le',
            planes=3,
            subsampling=Subsampling.NONE,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=30
        ))

        cls._register(PixelFormatInfo(
            name='gbrap10le',
            planes=4,
            subsampling=Subsampling.NONE,
            bit_depth=10,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=40
        ))

        # 12-bit
        cls._register(PixelFormatInfo(
            name='gbrp12le',
            planes=3,
            subsampling=Subsampling.NONE,
            bit_depth=12,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=36
        ))

        cls._register(PixelFormatInfo(
            name='gbrap12le',
            planes=4,
            subsampling=Subsampling.NONE,
            bit_depth=12,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=48
        ))

        # 16-bit
        cls._register(PixelFormatInfo(
            name='gbrp16le',
            planes=3,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=48
        ))

        cls._register(PixelFormatInfo(
            name='gbrap16le',
            planes=4,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=64
        ))

        # ========================================
        # GRAY (灰度) 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='gray',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=8
        ))

        cls._register(PixelFormatInfo(
            name='gray10le',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=10
        ))

        cls._register(PixelFormatInfo(
            name='gray12le',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=12,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=12
        ))

        cls._register(PixelFormatInfo(
            name='gray16le',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=16,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=16
        ))

        # ========================================
        # ProRes 专用格式
        # ProRes 422: yuv422p10 (10-bit 4:2:2)
        # ProRes 4444: 解码器实际输出 yuva444p10le（10-bit 4:4:4 带 alpha，
        # 见 AUD-35501）；本表注册的 yuv444p10 为无 alpha 的 4:4:4 10-bit
        # 基础格式（has_alpha=False），不承载 ProRes 4444 的 alpha 通道。
        # ========================================

        cls._register(PixelFormatInfo(
            name='yuv422p10',  # ProRes 422 使用
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=20
        ))

        cls._register(PixelFormatInfo(
            name='yuv444p10',  # 4:4:4 10-bit 无 alpha（ProRes 4444 实际解码为 yuva444p10le）
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=10,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=30
        ))

        # ========================================
        # YUV JPEG (Full Range) 系列
        # ========================================

        cls._register(PixelFormatInfo(
            name='yuvj420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        ))

        cls._register(PixelFormatInfo(
            name='yuvj422p',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=16
        ))

        cls._register(PixelFormatInfo(
            name='yuvj444p',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24
        ))

        # ========================================
        # 32-bit Float RGB
        # ========================================

        cls._register(PixelFormatInfo(
            name='gbrpf32le',
            planes=3,
            subsampling=Subsampling.NONE,
            bit_depth=32,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=96
        ))

        cls._register(PixelFormatInfo(
            name='gbrapf32le',
            planes=4,
            subsampling=Subsampling.NONE,
            bit_depth=32,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=128
        ))

        # ========================================
        # 其他常用格式别名
        # ========================================

        # ARGB/ABGR (常见于 macOS/iOS)
        cls._register(PixelFormatInfo(
            name='argb',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        ))

        cls._register(PixelFormatInfo(
            name='abgr',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        ))

    @classmethod
    def _register(cls, fmt: PixelFormatInfo) -> None:
        """
        注册像素格式。

        Args:
            fmt: 要注册的格式信息
        """
        cls._formats[fmt.name] = fmt

    @classmethod
    def get(cls, name: str) -> PixelFormatInfo | None:
        """
        根据名称获取像素格式信息。

        Args:
            name: 格式名称

        Returns:
            PixelFormatInfo 或 None（未找到）
        """
        cls._ensure_initialized()
        return cls._formats.get(name)

    @classmethod
    def get_by_properties(
        cls,
        color_model: ColorModel | None = None,
        bit_depth: int | None = None,
        has_alpha: bool | None = None,
        subsampling: Subsampling | None = None,
        is_planar: bool | None = None
    ) -> list[PixelFormatInfo]:
        """
        根据属性查询像素格式。

        Args:
            color_model: 颜色模型过滤
            bit_depth: 位深度过滤
            has_alpha: 是否包含 Alpha 过滤
            subsampling: 子采样模式过滤
            is_planar: 是否为平面格式过滤

        Returns:
            匹配的像素格式列表
        """
        cls._ensure_initialized()
        results: list[PixelFormatInfo] = []

        for fmt in cls._formats.values():
            if color_model is not None and fmt.color_model != color_model:
                continue
            if bit_depth is not None and fmt.bit_depth != bit_depth:
                continue
            if has_alpha is not None and fmt.has_alpha != has_alpha:
                continue
            if subsampling is not None and fmt.subsampling != subsampling:
                continue
            if is_planar is not None and fmt.is_planar != is_planar:
                continue
            results.append(fmt)

        return results

    @classmethod
    def is_yuv(cls, name: str) -> bool:
        """
        检查格式是否为 YUV。

        Args:
            name: 格式名称

        Returns:
            是 YUV 格式返回 True
        """
        fmt = cls.get(name)
        return fmt.is_yuv if fmt else False

    @classmethod
    def is_rgb(cls, name: str) -> bool:
        """
        检查格式是否为 RGB。

        Args:
            name: 格式名称

        Returns:
            是 RGB 格式返回 True
        """
        fmt = cls.get(name)
        return fmt.is_rgb if fmt else False

    @classmethod
    def is_planar(cls, name: str) -> bool:
        """
        检查格式是否为平面格式。

        Args:
            name: 格式名称

        Returns:
            是平面格式返回 True
        """
        fmt = cls.get(name)
        return fmt.is_planar if fmt else False

    @classmethod
    def get_chroma_divisors(cls, name: str) -> tuple[int, int]:
        """
        获取色度子采样除数。

        用于计算色度平面尺寸：
        chroma_width = luma_width / width_divisor
        chroma_height = luma_height / height_divisor

        Args:
            name: 格式名称

        Returns:
            (width_divisor, height_divisor) 元组
            例如 yuv420p 返回 (2, 2)，yuv422p 返回 (2, 1)
        """
        fmt = cls.get(name)
        if fmt is None:
            return (1, 1)
        return (fmt.chroma_width_divisor, fmt.chroma_height_divisor)

    @classmethod
    def get_all_formats(cls) -> list[str]:
        """
        获取所有已注册格式名称。

        Returns:
            格式名称列表
        """
        cls._ensure_initialized()
        return list(cls._formats.keys())

    @classmethod
    def count(cls) -> int:
        """
        获取已注册格式数量。

        Returns:
            格式数量
        """
        cls._ensure_initialized()
        return len(cls._formats)