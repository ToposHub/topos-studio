"""
PixelFormatRegistry 测试
测试像素格式注册表模块的核心功能。

测试覆盖：
- Subsampling 枚举（色度子采样）
- ColorModel 枚举（颜色模型）
- PixelFormatInfo dataclass
- PixelFormatRegistry 查询功能
- 格式识别
- 位深度查询
- 平面数查询
- 色度子采样查询
- Alpha 支持
"""

import pytest
from typing import Tuple

from src.shared.media.pixel_format import (
    Subsampling,
    ColorModel,
    PixelFormatInfo,
    PixelFormatRegistry
)


class TestSubsampling:
    """测试 Subsampling 枚举"""

    def test_subsampling_values(self):
        """测试子采样值"""
        assert Subsampling.YUV_444.value == (4, 4, 4)
        assert Subsampling.YUV_422.value == (4, 2, 2)
        assert Subsampling.YUV_420.value == (4, 2, 0)
        assert Subsampling.YUV_411.value == (4, 1, 1)
        assert Subsampling.YUV_410.value == (4, 1, 0)
        # AUD-35500：NONE 必须是独立成员——同值会变成 YUV_444 的枚举
        # 别名，令 RGB/GRAY 格式与 4:4:4 混淆。
        assert Subsampling.NONE.value == (0, 0, 0)
        assert Subsampling.NONE is not Subsampling.YUV_444
        assert Subsampling.NONE in list(Subsampling)

    def test_subsampling_chroma_width_divisor(self):
        """测试色度宽度除数"""
        # 4:4:4 无除数
        assert Subsampling.YUV_444.chroma_width_divisor == 1

        # 4:2:2 水平 1/2
        assert Subsampling.YUV_422.chroma_width_divisor == 2

        # 4:2:0 水平 1/2
        assert Subsampling.YUV_420.chroma_width_divisor == 2

        # 4:1:1 水平 1/4
        assert Subsampling.YUV_411.chroma_width_divisor == 4

        # 4:1:0 水平 1/4
        assert Subsampling.YUV_410.chroma_width_divisor == 4

    def test_subsampling_chroma_height_divisor(self):
        """测试色度高度除数"""
        # 4:4:4 无除数
        assert Subsampling.YUV_444.chroma_height_divisor == 1

        # 4:2:2 垂直除数（根据代码实现：j // b = 4 // 2 = 2）
        assert Subsampling.YUV_422.chroma_height_divisor == 2

        # 4:2:0 垂直 1/2
        assert Subsampling.YUV_420.chroma_height_divisor == 2

        # 4:1:1 垂直除数（j // b = 4 // 1 = 4）
        assert Subsampling.YUV_411.chroma_height_divisor == 4

        # 4:1:0 垂直 1/2
        assert Subsampling.YUV_410.chroma_height_divisor == 2

    def test_subsampling_from_tuple(self):
        """测试从元组推断子采样"""
        assert Subsampling.from_tuple((4, 4, 4)) == Subsampling.YUV_444
        assert Subsampling.from_tuple((4, 2, 2)) == Subsampling.YUV_422
        assert Subsampling.from_tuple((4, 2, 0)) == Subsampling.YUV_420
        assert Subsampling.from_tuple((4, 1, 1)) == Subsampling.YUV_411
        assert Subsampling.from_tuple((4, 1, 0)) == Subsampling.YUV_410

    def test_subsampling_from_tuple_invalid(self):
        """测试无效元组抛出异常"""
        with pytest.raises(ValueError):
            Subsampling.from_tuple((3, 3, 3))


class TestColorModel:
    """测试 ColorModel 枚举"""

    def test_color_model_values(self):
        """测试颜色模型值"""
        assert ColorModel.YUV.value == 'yuv'
        assert ColorModel.RGB.value == 'rgb'
        assert ColorModel.GRAY.value == 'gray'

    def test_color_model_members(self):
        """测试颜色模型成员"""
        members = list(ColorModel)
        assert len(members) == 3
        assert ColorModel.YUV in members
        assert ColorModel.RGB in members
        assert ColorModel.GRAY in members


class TestPixelFormatInfo:
    """测试 PixelFormatInfo dataclass"""

    def test_pixel_format_info_creation(self):
        """测试创建 PixelFormatInfo"""
        fmt = PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        )

        assert fmt.name == 'yuv420p'
        assert fmt.planes == 3
        assert fmt.subsampling == Subsampling.YUV_420
        assert fmt.bit_depth == 8
        assert fmt.has_alpha is False
        assert fmt.color_model == ColorModel.YUV
        assert fmt.chroma_bits_per_pixel == 12

    def test_pixel_format_info_properties_yuv(self):
        """测试 YUV 格式属性"""
        fmt = PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        )

        assert fmt.is_yuv is True
        assert fmt.is_rgb is False
        assert fmt.is_gray is False
        assert fmt.is_planar is True
        assert fmt.is_packed is False

    def test_pixel_format_info_properties_rgb(self):
        """测试 RGB 格式属性"""
        fmt = PixelFormatInfo(
            name='rgba',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        )

        assert fmt.is_yuv is False
        assert fmt.is_rgb is True
        assert fmt.is_gray is False
        assert fmt.is_planar is False
        assert fmt.is_packed is True

    def test_pixel_format_info_properties_gray(self):
        """测试灰度格式属性"""
        fmt = PixelFormatInfo(
            name='gray',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=8
        )

        assert fmt.is_yuv is False
        assert fmt.is_rgb is False
        assert fmt.is_gray is True
        assert fmt.is_planar is False
        assert fmt.is_packed is True

    def test_pixel_format_info_chroma_divisors(self):
        """测试色度除数属性"""
        # 4:2:0
        fmt_420 = PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        )

        assert fmt_420.chroma_width_divisor == 2
        assert fmt_420.chroma_height_divisor == 2

        # 4:2:2
        fmt_422 = PixelFormatInfo(
            name='yuv422p',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=16
        )

        assert fmt_422.chroma_width_divisor == 2
        # chroma_height_divisor = j // b = 4 // 2 = 2
        assert fmt_422.chroma_height_divisor == 2

    def test_pixel_format_info_bits_per_pixel_yuv_420(self):
        """测试 YUV 4:2:0 每像素位数"""
        fmt = PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        )

        # YUV 4:2:0: Y=8, U=8/4=2, V=8/4=2 -> 12 bits/pixel
        bpp = fmt.bits_per_pixel
        assert bpp == 12.0

    def test_pixel_format_info_bits_per_pixel_yuv_422(self):
        """测试 YUV 4:2:2 每像素位数"""
        fmt = PixelFormatInfo(
            name='yuv422p',
            planes=3,
            subsampling=Subsampling.YUV_422,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=16
        )

        # YUV 4:2:2: 实际计算
        # div_w = 2, div_h = 2 (根据实现)
        # chroma_factor = 2 / (2*2) = 0.5
        # bpp = 8 * (1 + 0.5) = 12 bits/pixel
        bpp = fmt.bits_per_pixel
        assert bpp == 12.0

    def test_pixel_format_info_bits_per_pixel_yuv_444(self):
        """测试 YUV 4:4:4 每像素位数"""
        fmt = PixelFormatInfo(
            name='yuv444p',
            planes=3,
            subsampling=Subsampling.YUV_444,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=24
        )

        # YUV 4:4:4: Y=8, U=8, V=8 -> 24 bits/pixel
        bpp = fmt.bits_per_pixel
        assert bpp == 24.0

    def test_pixel_format_info_bits_per_pixel_rgb(self):
        """测试 RGB 每像素位数"""
        # RGB 无 Alpha
        fmt_rgb = PixelFormatInfo(
            name='rgb24',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=24
        )

        # RGB 24-bit: 8 * 3 = 24 bits/pixel
        bpp = fmt_rgb.bits_per_pixel
        assert bpp == 24.0

        # RGBA
        fmt_rgba = PixelFormatInfo(
            name='rgba',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=True,
            color_model=ColorModel.RGB,
            chroma_bits_per_pixel=32
        )

        # RGBA 32-bit: 8 * 4 = 32 bits/pixel
        bpp = fmt_rgba.bits_per_pixel
        assert bpp == 32.0

    def test_pixel_format_info_bits_per_pixel_gray(self):
        """测试灰度每像素位数"""
        fmt = PixelFormatInfo(
            name='gray',
            planes=1,
            subsampling=Subsampling.NONE,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.GRAY,
            chroma_bits_per_pixel=8
        )

        # Gray: 8 bits/pixel
        bpp = fmt.bits_per_pixel
        assert bpp == 8.0


class TestPixelFormatRegistryGet:
    """测试 PixelFormatRegistry.get"""

    def test_get_yuv420p(self):
        """测试获取 yuv420p"""
        fmt = PixelFormatRegistry.get('yuv420p')

        assert fmt is not None
        assert fmt.name == 'yuv420p'
        assert fmt.planes == 3
        assert fmt.subsampling == Subsampling.YUV_420
        assert fmt.bit_depth == 8
        assert fmt.has_alpha is False
        assert fmt.color_model == ColorModel.YUV

    def test_get_yuv420p10le(self):
        """测试获取 yuv420p10le"""
        fmt = PixelFormatRegistry.get('yuv420p10le')

        assert fmt is not None
        assert fmt.name == 'yuv420p10le'
        assert fmt.bit_depth == 10
        assert fmt.subsampling == Subsampling.YUV_420

    def test_get_yuv422p(self):
        """测试获取 yuv422p"""
        fmt = PixelFormatRegistry.get('yuv422p')

        assert fmt is not None
        assert fmt.name == 'yuv422p'
        assert fmt.subsampling == Subsampling.YUV_422

    def test_get_yuv444p(self):
        """测试获取 yuv444p"""
        fmt = PixelFormatRegistry.get('yuv444p')

        assert fmt is not None
        assert fmt.name == 'yuv444p'
        assert fmt.subsampling == Subsampling.YUV_444

    def test_get_rgba(self):
        """测试获取 rgba"""
        fmt = PixelFormatRegistry.get('rgba')

        assert fmt is not None
        assert fmt.name == 'rgba'
        assert fmt.color_model == ColorModel.RGB
        assert fmt.has_alpha is True

    def test_get_rgb48le(self):
        """测试获取 rgb48le"""
        fmt = PixelFormatRegistry.get('rgb48le')

        assert fmt is not None
        assert fmt.name == 'rgb48le'
        assert fmt.bit_depth == 16

    def test_get_gbrp(self):
        """测试获取 gbrp (planar RGB)"""
        fmt = PixelFormatRegistry.get('gbrp')

        assert fmt is not None
        assert fmt.name == 'gbrp'
        assert fmt.color_model == ColorModel.RGB
        assert fmt.is_planar is True
        assert fmt.planes == 3

    def test_get_gbrap(self):
        """测试获取 gbrap (planar RGBA)"""
        fmt = PixelFormatRegistry.get('gbrap')

        assert fmt is not None
        assert fmt.name == 'gbrap'
        assert fmt.has_alpha is True
        assert fmt.planes == 4

    def test_get_gray(self):
        """测试获取 gray"""
        fmt = PixelFormatRegistry.get('gray')

        assert fmt is not None
        assert fmt.name == 'gray'
        assert fmt.color_model == ColorModel.GRAY
        assert fmt.bit_depth == 8

    def test_get_gray10le(self):
        """测试获取 gray10le"""
        fmt = PixelFormatRegistry.get('gray10le')

        assert fmt is not None
        assert fmt.name == 'gray10le'
        assert fmt.bit_depth == 10

    def test_get_yuva420p(self):
        """测试获取 yuva420p"""
        fmt = PixelFormatRegistry.get('yuva420p')

        assert fmt is not None
        assert fmt.name == 'yuva420p'
        assert fmt.has_alpha is True
        assert fmt.planes == 4

    def test_get_gbrpf32le(self):
        """测试获取 gbrpf32le (32-bit float)"""
        fmt = PixelFormatRegistry.get('gbrpf32le')

        assert fmt is not None
        assert fmt.name == 'gbrpf32le'
        assert fmt.bit_depth == 32

    def test_get_unknown(self):
        """测试获取未知格式"""
        fmt = PixelFormatRegistry.get('unknown_format')
        assert fmt is None


class TestPixelFormatRegistryGetByProperties:
    """测试 PixelFormatRegistry.get_by_properties"""

    def test_get_by_color_model_yuv(self):
        """测试按颜色模型查询 YUV"""
        formats = PixelFormatRegistry.get_by_properties(color_model=ColorModel.YUV)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.color_model == ColorModel.YUV

    def test_get_by_color_model_rgb(self):
        """测试按颜色模型查询 RGB"""
        formats = PixelFormatRegistry.get_by_properties(color_model=ColorModel.RGB)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.color_model == ColorModel.RGB

    def test_get_by_color_model_gray(self):
        """测试按颜色模型查询灰度"""
        formats = PixelFormatRegistry.get_by_properties(color_model=ColorModel.GRAY)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.color_model == ColorModel.GRAY

    def test_get_by_bit_depth_8(self):
        """测试按位深度查询 8-bit"""
        formats = PixelFormatRegistry.get_by_properties(bit_depth=8)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.bit_depth == 8

    def test_get_by_bit_depth_10(self):
        """测试按位深度查询 10-bit"""
        formats = PixelFormatRegistry.get_by_properties(bit_depth=10)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.bit_depth == 10

    def test_get_by_bit_depth_12(self):
        """测试按位深度查询 12-bit"""
        formats = PixelFormatRegistry.get_by_properties(bit_depth=12)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.bit_depth == 12

    def test_get_by_bit_depth_16(self):
        """测试按位深度查询 16-bit"""
        formats = PixelFormatRegistry.get_by_properties(bit_depth=16)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.bit_depth == 16

    def test_get_by_has_alpha_true(self):
        """测试按 Alpha 查询有 Alpha"""
        formats = PixelFormatRegistry.get_by_properties(has_alpha=True)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.has_alpha is True

    def test_get_by_has_alpha_false(self):
        """测试按 Alpha 查询无 Alpha"""
        formats = PixelFormatRegistry.get_by_properties(has_alpha=False)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.has_alpha is False

    def test_get_by_subsampling_420(self):
        """测试按子采样查询 4:2:0"""
        formats = PixelFormatRegistry.get_by_properties(
            subsampling=Subsampling.YUV_420
        )

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.subsampling == Subsampling.YUV_420

    def test_get_by_subsampling_422(self):
        """测试按子采样查询 4:2:2"""
        formats = PixelFormatRegistry.get_by_properties(
            subsampling=Subsampling.YUV_422
        )

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.subsampling == Subsampling.YUV_422

    def test_get_by_subsampling_444(self):
        """测试按子采样查询 4:4:4"""
        formats = PixelFormatRegistry.get_by_properties(
            subsampling=Subsampling.YUV_444
        )

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.subsampling == Subsampling.YUV_444

    def test_get_by_is_planar_true(self):
        """测试按平面格式查询"""
        formats = PixelFormatRegistry.get_by_properties(is_planar=True)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.is_planar is True
            assert fmt.planes > 1

    def test_get_by_is_planar_false(self):
        """测试按打包格式查询"""
        formats = PixelFormatRegistry.get_by_properties(is_planar=False)

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.is_planar is False
            assert fmt.planes == 1

    def test_get_by_multiple_properties(self):
        """测试多属性组合查询"""
        # YUV + 10-bit + 4:2:0
        formats = PixelFormatRegistry.get_by_properties(
            color_model=ColorModel.YUV,
            bit_depth=10,
            subsampling=Subsampling.YUV_420,
            has_alpha=False
        )

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.color_model == ColorModel.YUV
            assert fmt.bit_depth == 10
            assert fmt.subsampling == Subsampling.YUV_420
            assert fmt.has_alpha is False

    def test_get_by_rgb_with_alpha(self):
        """测试 RGB + Alpha 组合查询"""
        formats = PixelFormatRegistry.get_by_properties(
            color_model=ColorModel.RGB,
            has_alpha=True
        )

        assert len(formats) > 0
        for fmt in formats:
            assert fmt.color_model == ColorModel.RGB
            assert fmt.has_alpha is True


class TestPixelFormatRegistryTypeCheck:
    """测试 PixelFormatRegistry 类型检查"""

    def test_is_yuv_yuv_format(self):
        """测试 YUV 格式类型检查"""
        assert PixelFormatRegistry.is_yuv('yuv420p') is True
        assert PixelFormatRegistry.is_yuv('yuv422p10le') is True
        assert PixelFormatRegistry.is_yuv('yuva420p') is True

    def test_is_yuv_rgb_format(self):
        """测试 RGB 格式不是 YUV"""
        assert PixelFormatRegistry.is_yuv('rgba') is False
        assert PixelFormatRegistry.is_yuv('rgb48le') is False
        assert PixelFormatRegistry.is_yuv('gbrp') is False

    def test_is_yuv_gray_format(self):
        """测试灰度格式不是 YUV"""
        assert PixelFormatRegistry.is_yuv('gray') is False
        assert PixelFormatRegistry.is_yuv('gray10le') is False

    def test_is_rgb_rgb_format(self):
        """测试 RGB 格式类型检查"""
        assert PixelFormatRegistry.is_rgb('rgba') is True
        assert PixelFormatRegistry.is_rgb('rgb48le') is True
        assert PixelFormatRegistry.is_rgb('gbrp') is True
        assert PixelFormatRegistry.is_rgb('gbrap10le') is True

    def test_is_rgb_yuv_format(self):
        """测试 YUV 格式不是 RGB"""
        assert PixelFormatRegistry.is_rgb('yuv420p') is False
        assert PixelFormatRegistry.is_rgb('yuv422p10le') is False

    def test_is_planar_planar_format(self):
        """测试平面格式检查"""
        assert PixelFormatRegistry.is_planar('yuv420p') is True
        assert PixelFormatRegistry.is_planar('yuv422p') is True
        assert PixelFormatRegistry.is_planar('gbrp') is True
        assert PixelFormatRegistry.is_planar('gbrap') is True

    def test_is_planar_packed_format(self):
        """测试打包格式不是平面"""
        assert PixelFormatRegistry.is_planar('rgba') is False
        assert PixelFormatRegistry.is_planar('rgb48le') is False
        assert PixelFormatRegistry.is_planar('gray') is False

    def test_is_yuv_unknown_format(self):
        """测试未知格式类型检查"""
        assert PixelFormatRegistry.is_yuv('unknown') is False
        assert PixelFormatRegistry.is_rgb('unknown') is False
        assert PixelFormatRegistry.is_planar('unknown') is False


class TestPixelFormatRegistryChromaDivisors:
    """测试 PixelFormatRegistry.get_chroma_divisors"""

    def test_get_chroma_divisors_420(self):
        """测试 4:2:0 色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv420p')
        assert div_w == 2
        assert div_h == 2

    def test_get_chroma_divisors_422(self):
        """测试 4:2:2 色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv422p')
        assert div_w == 2
        # div_h = j // b = 4 // 2 = 2
        assert div_h == 2

    def test_get_chroma_divisors_444(self):
        """测试 4:4:4 色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv444p')
        assert div_w == 1
        assert div_h == 1

    def test_get_chroma_divisors_411(self):
        """测试 4:1:1 色度除数"""
        # 4:1:1 格式未注册，返回默认值
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv411p')
        # 未知格式返回 (1, 1)
        assert div_w == 1
        assert div_h == 1

    def test_get_chroma_divisors_10bit_420(self):
        """测试 10-bit 4:2:0 色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('yuv420p10le')
        assert div_w == 2
        assert div_h == 2

    def test_get_chroma_divisors_rgb(self):
        """测试 RGB 色度除数（无子采样）"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('rgba')
        assert div_w == 1
        assert div_h == 1

    def test_get_chroma_divisors_gray(self):
        """测试灰度色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('gray')
        assert div_w == 1
        assert div_h == 1

    def test_get_chroma_divisors_unknown(self):
        """测试未知格式色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('unknown')
        assert div_w == 1
        assert div_h == 1


class TestPixelFormatRegistryList:
    """测试 PixelFormatRegistry 列表功能"""

    def test_get_all_formats(self):
        """测试获取所有格式"""
        formats = PixelFormatRegistry.get_all_formats()

        assert len(formats) > 20  # 应有超过 20 种格式
        assert 'yuv420p' in formats
        assert 'yuv422p' in formats
        assert 'yuv444p' in formats
        assert 'rgba' in formats
        assert 'gbrp' in formats
        assert 'gray' in formats

    def test_count(self):
        """测试格式计数"""
        count = PixelFormatRegistry.count()

        assert count > 20
        assert count == len(PixelFormatRegistry.get_all_formats())


class TestPixelFormatRegistryRegisteredFormats:
    """测试已注册的格式"""

    def test_yuv_8bit_formats(self):
        """测试 YUV 8-bit 格式"""
        for name in ['yuv420p', 'yuv422p', 'yuv444p']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 8
            assert fmt.color_model == ColorModel.YUV

    def test_yuv_10bit_formats(self):
        """测试 YUV 10-bit 格式"""
        for name in ['yuv420p10le', 'yuv422p10le', 'yuv444p10le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 10

        # Big-endian
        for name in ['yuv420p10be', 'yuv422p10be', 'yuv444p10be']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 10

    def test_yuv_12bit_formats(self):
        """测试 YUV 12-bit 格式"""
        for name in ['yuv420p12le', 'yuv422p12le', 'yuv444p12le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 12

    def test_yuv_16bit_formats(self):
        """测试 YUV 16-bit 格式"""
        for name in ['yuv420p16le', 'yuv422p16le', 'yuv444p16le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 16

    def test_yuva_formats(self):
        """测试 YUVA 格式"""
        for name in ['yuva420p', 'yuva422p', 'yuva444p']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.has_alpha is True
            assert fmt.planes == 4

    def test_rgb_8bit_formats(self):
        """测试 RGB 8-bit 格式"""
        for name in ['rgb24', 'rgba', 'bgr24', 'bgra', 'argb', 'abgr']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 8
            assert fmt.color_model == ColorModel.RGB

    def test_rgb_16bit_formats(self):
        """测试 RGB 16-bit 格式"""
        for name in ['rgb48le', 'rgba64le', 'rgb48be', 'rgba64be']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.bit_depth == 16

    def test_gbr_formats(self):
        """测试 GBR (planar RGB) 格式"""
        # 8-bit
        assert PixelFormatRegistry.get('gbrp').bit_depth == 8
        assert PixelFormatRegistry.get('gbrap').has_alpha is True

        # 10-bit
        assert PixelFormatRegistry.get('gbrp10le').bit_depth == 10
        assert PixelFormatRegistry.get('gbrap10le').has_alpha is True

        # 12-bit
        assert PixelFormatRegistry.get('gbrp12le').bit_depth == 12
        assert PixelFormatRegistry.get('gbrap12le').has_alpha is True

        # 16-bit
        assert PixelFormatRegistry.get('gbrp16le').bit_depth == 16
        assert PixelFormatRegistry.get('gbrap16le').has_alpha is True

    def test_gray_formats(self):
        """测试灰度格式"""
        assert PixelFormatRegistry.get('gray').bit_depth == 8
        assert PixelFormatRegistry.get('gray10le').bit_depth == 10
        assert PixelFormatRegistry.get('gray12le').bit_depth == 12
        assert PixelFormatRegistry.get('gray16le').bit_depth == 16

    def test_yuvjpeg_formats(self):
        """测试 YUV JPEG (full range) 格式"""
        for name in ['yuvj420p', 'yuvj422p', 'yuvj444p']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.color_model == ColorModel.YUV

    def test_float_formats(self):
        """测试浮点格式"""
        fmt = PixelFormatRegistry.get('gbrpf32le')
        assert fmt is not None
        assert fmt.bit_depth == 32

        fmt = PixelFormatRegistry.get('gbrapf32le')
        assert fmt is not None
        assert fmt.bit_depth == 32
        assert fmt.has_alpha is True

    def test_prores_formats(self):
        """测试 ProRes 格式"""
        fmt = PixelFormatRegistry.get('yuv422p10')
        assert fmt is not None
        assert fmt.bit_depth == 10
        assert fmt.subsampling == Subsampling.YUV_422

        fmt = PixelFormatRegistry.get('yuv444p10')
        assert fmt is not None
        assert fmt.bit_depth == 10
        assert fmt.subsampling == Subsampling.YUV_444


class TestPixelFormatInfoFrozen:
    """测试 PixelFormatInfo frozen 特性"""

    def test_pixel_format_info_frozen(self):
        """测试 PixelFormatInfo 是 frozen dataclass"""
        fmt = PixelFormatInfo(
            name='yuv420p',
            planes=3,
            subsampling=Subsampling.YUV_420,
            bit_depth=8,
            has_alpha=False,
            color_model=ColorModel.YUV,
            chroma_bits_per_pixel=12
        )

        # frozen dataclass 不允许修改
        with pytest.raises(Exception):  # FrozenInstanceError
            fmt.bit_depth = 10


class TestPixelFormatRegistryInit:
    """测试 PixelFormatRegistry 初始化"""

    def test_lazy_initialization(self):
        """测试延迟初始化"""
        # Registry 使用延迟初始化
        # 第一次调用时会初始化
        formats = PixelFormatRegistry.get_all_formats()
        assert len(formats) > 0

        # 第二次调用不会重新初始化
        formats2 = PixelFormatRegistry.get_all_formats()
        assert formats == formats2

    def test_double_initialization_safe(self):
        """测试双重初始化安全"""
        # 多次调用 get_all_formats 应返回相同结果
        count1 = PixelFormatRegistry.count()
        count2 = PixelFormatRegistry.count()

        assert count1 == count2


class TestEdgeCases:
    """测试边界情况"""

    def test_get_empty_string(self):
        """测试空字符串格式名"""
        fmt = PixelFormatRegistry.get('')
        assert fmt is None

    def test_get_chroma_divisors_empty_string(self):
        """测试空字符串色度除数"""
        div_w, div_h = PixelFormatRegistry.get_chroma_divisors('')
        assert div_w == 1
        assert div_h == 1

    def test_get_by_properties_empty_result(self):
        """测试无结果的组合查询"""
        # 不存在的组合：GRAY + has_alpha=True（灰度带 alpha）
        formats = PixelFormatRegistry.get_by_properties(
            color_model=ColorModel.GRAY,
            has_alpha=True
        )

        # 灰度格式没有 alpha
        assert len(formats) == 0

    def test_bits_per_pixel_high_bit_depth(self):
        """测试高位深每像素位数"""
        fmt = PixelFormatRegistry.get('yuv420p16le')

        # 16-bit YUV 4:2:0
        # Y=16, U=16/4=4, V=16/4=4 -> 24 bits/pixel
        bpp = fmt.bits_per_pixel
        assert bpp == 24.0


class TestRegistryConsistency:
    """测试注册表一致性"""

    def test_all_formats_have_valid_info(self):
        """测试所有格式有有效信息"""
        formats = PixelFormatRegistry.get_all_formats()

        for name in formats:
            fmt = PixelFormatRegistry.get(name)
            assert fmt is not None
            assert fmt.name == name
            assert fmt.bit_depth in [8, 10, 12, 14, 16, 32]
            assert fmt.color_model in [ColorModel.YUV, ColorModel.RGB, ColorModel.GRAY]
            assert fmt.planes >= 1
            assert fmt.chroma_bits_per_pixel > 0

    def test_planar_formats_have_correct_planes(self):
        """测试平面格式平面数正确"""
        # YUV planar: 3 planes
        for name in ['yuv420p', 'yuv422p', 'yuv444p']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt.planes == 3

        # YUVA planar: 4 planes
        for name in ['yuva420p', 'yuva422p', 'yuva444p']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt.planes == 4

        # GBR planar: 3 planes
        for name in ['gbrp', 'gbrp10le', 'gbrp16le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt.planes == 3

        # GBRAP planar: 4 planes
        for name in ['gbrap', 'gbrap10le', 'gbrap16le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt.planes == 4

    def test_packed_formats_have_one_plane(self):
        """测试打包格式只有一个平面"""
        for name in ['rgba', 'rgb48le', 'rgba64le', 'gray', 'gray10le']:
            fmt = PixelFormatRegistry.get(name)
            assert fmt.planes == 1