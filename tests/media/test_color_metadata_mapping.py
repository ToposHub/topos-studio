"""
色彩元数据映射测试。

测试覆盖：
- COLOR_PRIMARIES_MAP 双向映射
- COLOR_TRANSFER_MAP 双向映射
- COLOR_SPACE_MAP 双向映射
- COLOR_RANGE_MAP 双向映射
- ffmpeg_to_color_* 函数
- color_*_to_ffmpeg 函数
- map_color_* 兼容性函数
- get_standard_name 函数
- None 输入处理
- 边界值测试
- 未知值 fallback
"""

import pytest
from typing import Optional

from src.shared.media.color_metadata import (
    # 映射表
    COLOR_PRIMARIES_MAP,
    COLOR_TRANSFER_MAP,
    COLOR_SPACE_MAP,
    COLOR_RANGE_MAP,
    # FFmpeg → 枚举
    ffmpeg_to_color_primaries,
    ffmpeg_to_color_transfer,
    ffmpeg_to_color_matrix,
    ffmpeg_to_color_range,
    # 枚举 → FFmpeg
    color_primaries_to_ffmpeg,
    color_transfer_to_ffmpeg,
    color_matrix_to_ffmpeg,
    color_range_to_ffmpeg,
    # 枚举 → 标准名称
    get_standard_name,
    # 兼容性函数
    map_color_primaries,
    map_color_transfer,
    map_color_space,
    map_color_range,
)
from src.features.color.core.color_manager import (
    ColorPrimaries,
    ColorTransfer,
    ColorMatrix,
    ColorRange,
)


# =============================================================================
# COLOR_PRIMARIES_MAP 测试
# =============================================================================

class TestColorPrimariesMap:
    """测试 COLOR_PRIMARIES_MAP 映射表"""

    def test_bt709_mapping(self) -> None:
        """测试 BT.709 (1) 映射"""
        assert COLOR_PRIMARIES_MAP[1] == 'bt709'

    def test_bt2020_mapping(self) -> None:
        """测试 BT.2020 (9) 映射"""
        assert COLOR_PRIMARIES_MAP[9] == 'bt2020'

    def test_smpte431_mapping(self) -> None:
        """测试 SMPTE 431 (DCI-P3) (11) 映射"""
        assert COLOR_PRIMARIES_MAP[11] == 'smpte431'

    def test_smpte432_mapping(self) -> None:
        """测试 SMPTE 432 (Display P3) (12) 映射"""
        assert COLOR_PRIMARIES_MAP[12] == 'smpte432'

    def test_smpte170m_mapping(self) -> None:
        """测试 SMPTE 170M (BT.601 NTSC) (6) 映射"""
        assert COLOR_PRIMARIES_MAP[6] == 'smpte170m'

    def test_bt470bg_mapping(self) -> None:
        """测试 BT.470BG (BT.601 PAL) (5) 映射"""
        assert COLOR_PRIMARIES_MAP[5] == 'bt470bg'

    def test_film_mapping(self) -> None:
        """测试 Generic Film (8) 映射"""
        assert COLOR_PRIMARIES_MAP[8] == 'film'

    def test_smpte428_mapping(self) -> None:
        """测试 SMPTE 428 (XYZ) (10) 映射"""
        assert COLOR_PRIMARIES_MAP[10] == 'smpte428'

    def test_ebu3213_mapping(self) -> None:
        """测试 EBU 3213 (22) 映射"""
        assert COLOR_PRIMARIES_MAP[22] == 'ebu3213'

    def test_map_contains_key_types(self) -> None:
        """测试映射表键类型为 int"""
        for key in COLOR_PRIMARIES_MAP.keys():
            assert isinstance(key, int)

    def test_map_value_types(self) -> None:
        """测试映射表值类型为 str"""
        for value in COLOR_PRIMARIES_MAP.values():
            assert isinstance(value, str)

    def test_map_length(self) -> None:
        """测试映射表长度"""
        # 应包含至少 11 个标准 primaries
        assert len(COLOR_PRIMARIES_MAP) >= 11


# =============================================================================
# COLOR_TRANSFER_MAP 测试
# =============================================================================

class TestColorTransferMap:
    """测试 COLOR_TRANSFER_MAP 映射表"""

    def test_bt709_mapping(self) -> None:
        """测试 BT.709 / sRGB transfer (1) 映射"""
        assert COLOR_TRANSFER_MAP[1] == 'bt709'

    def test_smpte2084_pq_mapping(self) -> None:
        """测试 SMPTE ST 2084 (PQ) (16) 映射"""
        assert COLOR_TRANSFER_MAP[16] == 'smpte2084'

    def test_arib_std_b67_hlg_mapping(self) -> None:
        """测试 ARIB STD-B67 (HLG) (18) 映射"""
        assert COLOR_TRANSFER_MAP[18] == 'arib-std-b67'

    def test_linear_mapping(self) -> None:
        """测试 Linear (8) 映射"""
        assert COLOR_TRANSFER_MAP[8] == 'linear'

    def test_iec61966_2_1_srgb_mapping(self) -> None:
        """测试 IEC 61966-2-1 (sRGB) (13) 映射"""
        assert COLOR_TRANSFER_MAP[13] == 'iec61966_2_1'

    def test_gamma22_mapping(self) -> None:
        """测试 Gamma 2.2 (4) 映射"""
        assert COLOR_TRANSFER_MAP[4] == 'gamma22'

    def test_gamma28_mapping(self) -> None:
        """测试 Gamma 2.8 (5) 映射"""
        assert COLOR_TRANSFER_MAP[5] == 'gamma28'

    def test_bt2020_10_mapping(self) -> None:
        """测试 BT.2020 10-bit (14) 映射"""
        assert COLOR_TRANSFER_MAP[14] == 'bt2020_10'

    def test_bt2020_12_mapping(self) -> None:
        """测试 BT.2020 12-bit (15) 映射"""
        assert COLOR_TRANSFER_MAP[15] == 'bt2020_12'

    def test_smpte170m_mapping(self) -> None:
        """测试 SMPTE 170M (6) 映射"""
        assert COLOR_TRANSFER_MAP[6] == 'smpte170m'

    def test_log_mapping(self) -> None:
        """测试 Logarithmic (9) 映射"""
        assert COLOR_TRANSFER_MAP[9] == 'log'

    def test_log_sqrt_mapping(self) -> None:
        """测试 Log Sqrt (10) 映射"""
        assert COLOR_TRANSFER_MAP[10] == 'log_sqrt'

    def test_map_contains_key_types(self) -> None:
        """测试映射表键类型为 int"""
        for key in COLOR_TRANSFER_MAP.keys():
            assert isinstance(key, int)

    def test_map_value_types(self) -> None:
        """测试映射表值类型为 str"""
        for value in COLOR_TRANSFER_MAP.values():
            assert isinstance(value, str)

    def test_map_length(self) -> None:
        """测试映射表长度"""
        # 应包含至少 15 个标准 transfer
        assert len(COLOR_TRANSFER_MAP) >= 15


# =============================================================================
# COLOR_SPACE_MAP 测试
# =============================================================================

class TestColorSpaceMap:
    """测试 COLOR_SPACE_MAP 映射表"""

    def test_bt709_mapping(self) -> None:
        """测试 BT.709 (1) 映射"""
        assert COLOR_SPACE_MAP[1] == 'bt709'

    def test_bt2020nc_mapping(self) -> None:
        """测试 BT.2020 NCL (9) 映射"""
        assert COLOR_SPACE_MAP[9] == 'bt2020nc'

    def test_bt2020c_mapping(self) -> None:
        """测试 BT.2020 CL (10) 映射"""
        assert COLOR_SPACE_MAP[10] == 'bt2020c'

    def test_gbr_mapping(self) -> None:
        """测试 GBR (0) 映射"""
        assert COLOR_SPACE_MAP[0] == 'gbr'

    def test_smpte170m_mapping(self) -> None:
        """测试 SMPTE 170M (BT.601) (6) 映射"""
        assert COLOR_SPACE_MAP[6] == 'smpte170m'

    def test_bt470bg_mapping(self) -> None:
        """测试 BT.470BG (BT.601 PAL) (5) 映射"""
        assert COLOR_SPACE_MAP[5] == 'bt470bg'

    def test_fcc_mapping(self) -> None:
        """测试 FCC (4) 映射"""
        assert COLOR_SPACE_MAP[4] == 'fcc'

    def test_ycgco_mapping(self) -> None:
        """测试 YCgCo (8) 映射"""
        assert COLOR_SPACE_MAP[8] == 'ycgco'

    def test_ictcp_mapping(self) -> None:
        """测试 ICtCp (14) 映射"""
        assert COLOR_SPACE_MAP[14] == 'ictcp'

    def test_unknown_mapping(self) -> None:
        """测试 Unknown (2) 映射"""
        assert COLOR_SPACE_MAP[2] == 'unknown'

    def test_reserved_mapping(self) -> None:
        """测试 Reserved (3) 映射"""
        assert COLOR_SPACE_MAP[3] == 'reserved'

    def test_map_contains_key_types(self) -> None:
        """测试映射表键类型为 int"""
        for key in COLOR_SPACE_MAP.keys():
            assert isinstance(key, int)

    def test_map_value_types(self) -> None:
        """测试映射表值类型为 str"""
        for value in COLOR_SPACE_MAP.values():
            assert isinstance(value, str)

    def test_map_length(self) -> None:
        """测试映射表长度"""
        # 应包含至少 12 个标准 space
        assert len(COLOR_SPACE_MAP) >= 12


# =============================================================================
# COLOR_RANGE_MAP 测试
# =============================================================================

class TestColorRangeMap:
    """测试 COLOR_RANGE_MAP 映射表"""

    def test_unknown_mapping(self) -> None:
        """测试 Unknown (0) 映射"""
        assert COLOR_RANGE_MAP[0] == 'unknown'

    def test_limited_mapping(self) -> None:
        """测试 Limited/TV range (1) 映射"""
        assert COLOR_RANGE_MAP[1] == 'limited'

    def test_full_mapping(self) -> None:
        """测试 Full/PC range (2) 映射"""
        assert COLOR_RANGE_MAP[2] == 'full'

    def test_map_contains_only_valid_keys(self) -> None:
        """测试映射表仅包含有效键"""
        assert set(COLOR_RANGE_MAP.keys()) == {0, 1, 2}

    def test_map_contains_only_valid_values(self) -> None:
        """测试映射表仅包含有效值"""
        assert set(COLOR_RANGE_MAP.values()) == {'unknown', 'limited', 'full'}

    def test_map_key_types(self) -> None:
        """测试映射表键类型为 int"""
        for key in COLOR_RANGE_MAP.keys():
            assert isinstance(key, int)

    def test_map_value_types(self) -> None:
        """测试映射表值类型为 str"""
        for value in COLOR_RANGE_MAP.values():
            assert isinstance(value, str)

    def test_map_length(self) -> None:
        """测试映射表长度"""
        assert len(COLOR_RANGE_MAP) == 3


# =============================================================================
# ffmpeg_to_color_primaries 测试
# =============================================================================

class TestFfmpegToColorPrimaries:
    """测试 ffmpeg_to_color_primaries 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 映射"""
        result = ffmpeg_to_color_primaries(1)
        assert result == ColorPrimaries.BT709

    def test_bt2020(self) -> None:
        """测试 BT.2020 映射"""
        result = ffmpeg_to_color_primaries(9)
        assert result == ColorPrimaries.BT2020

    def test_smpte431_dci_p3(self) -> None:
        """测试 DCI-P3 映射"""
        result = ffmpeg_to_color_primaries(11)
        assert result == ColorPrimaries.P3_DCI

    def test_smpte432_display_p3(self) -> None:
        """测试 Display P3 映射"""
        result = ffmpeg_to_color_primaries(12)
        assert result == ColorPrimaries.P3_DISPLAY

    def test_none_returns_unknown(self) -> None:
        """测试 None 输入返回 UNKNOWN"""
        result = ffmpeg_to_color_primaries(None)
        assert result == ColorPrimaries.UNKNOWN

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 UNKNOWN"""
        result = ffmpeg_to_color_primaries(999)
        assert result == ColorPrimaries.UNKNOWN

    def test_zero_returns_unknown(self) -> None:
        """测试 0 返回 UNKNOWN"""
        result = ffmpeg_to_color_primaries(0)
        assert result == ColorPrimaries.UNKNOWN

    def test_negative_returns_unknown(self) -> None:
        """测试负数返回 UNKNOWN"""
        result = ffmpeg_to_color_primaries(-1)
        assert result == ColorPrimaries.UNKNOWN


# =============================================================================
# ffmpeg_to_color_transfer 测试
# =============================================================================

class TestFfmpegToColorTransfer:
    """测试 ffmpeg_to_color_transfer 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 映射"""
        result = ffmpeg_to_color_transfer(1)
        assert result == ColorTransfer.BT709

    def test_smpte2084_pq(self) -> None:
        """测试 PQ (HDR10) 映射"""
        result = ffmpeg_to_color_transfer(16)
        assert result == ColorTransfer.PQ

    def test_arib_std_b67_hlg(self) -> None:
        """测试 HLG 映射"""
        result = ffmpeg_to_color_transfer(18)
        assert result == ColorTransfer.HLG

    def test_linear(self) -> None:
        """测试 Linear 映射"""
        result = ffmpeg_to_color_transfer(8)
        assert result == ColorTransfer.LINEAR

    def test_srgb(self) -> None:
        """测试 sRGB 映射"""
        result = ffmpeg_to_color_transfer(13)
        assert result == ColorTransfer.SRGB

    def test_none_returns_unknown(self) -> None:
        """测试 None 输入返回 UNKNOWN"""
        result = ffmpeg_to_color_transfer(None)
        assert result == ColorTransfer.UNKNOWN

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 UNKNOWN"""
        result = ffmpeg_to_color_transfer(999)
        assert result == ColorTransfer.UNKNOWN

    def test_logarithmic_returns_unknown(self) -> None:
        """测试 Logarithmic (暂无枚举) 返回 UNKNOWN"""
        # 值 9 对应 Logarithmic，暂时无对应枚举
        result = ffmpeg_to_color_transfer(9)
        assert result == ColorTransfer.UNKNOWN


# =============================================================================
# ffmpeg_to_color_matrix 测试
# =============================================================================

class TestFfmpegToColorMatrix:
    """测试 ffmpeg_to_color_matrix 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 映射"""
        result = ffmpeg_to_color_matrix(1)
        assert result == ColorMatrix.BT709

    def test_bt2020nc(self) -> None:
        """测试 BT.2020 NCL 映射"""
        result = ffmpeg_to_color_matrix(9)
        assert result == ColorMatrix.BT2020_NC

    def test_bt2020c(self) -> None:
        """测试 BT.2020 CL 映射"""
        result = ffmpeg_to_color_matrix(10)
        assert result == ColorMatrix.BT2020_C

    def test_gbr(self) -> None:
        """测试 GBR 映射"""
        result = ffmpeg_to_color_matrix(0)
        assert result == ColorMatrix.GBR

    def test_ictcp(self) -> None:
        """测试 ICtCp 映射"""
        result = ffmpeg_to_color_matrix(14)
        assert result == ColorMatrix.ICTCP

    def test_none_returns_unknown(self) -> None:
        """测试 None 输入返回 UNKNOWN"""
        result = ffmpeg_to_color_matrix(None)
        assert result == ColorMatrix.UNKNOWN

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 UNKNOWN"""
        result = ffmpeg_to_color_matrix(999)
        assert result == ColorMatrix.UNKNOWN

    def test_reserved_returns_unknown(self) -> None:
        """测试 Reserved 值 (3) 返回 UNKNOWN"""
        result = ffmpeg_to_color_matrix(3)
        assert result == ColorMatrix.UNKNOWN


# =============================================================================
# ffmpeg_to_color_range 测试
# =============================================================================

class TestFfmpegToColorRange:
    """测试 ffmpeg_to_color_range 函数"""

    def test_limited(self) -> None:
        """测试 Limited/TV range 映射"""
        result = ffmpeg_to_color_range(1)
        assert result == ColorRange.LIMITED

    def test_full(self) -> None:
        """测试 Full/PC range 映射"""
        result = ffmpeg_to_color_range(2)
        assert result == ColorRange.FULL

    def test_zero_returns_unknown(self) -> None:
        """测试 0 返回 UNKNOWN"""
        result = ffmpeg_to_color_range(0)
        assert result == ColorRange.UNKNOWN

    def test_none_returns_unknown(self) -> None:
        """测试 None 输入返回 UNKNOWN"""
        result = ffmpeg_to_color_range(None)
        assert result == ColorRange.UNKNOWN

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 UNKNOWN"""
        result = ffmpeg_to_color_range(999)
        assert result == ColorRange.UNKNOWN


# =============================================================================
# color_primaries_to_ffmpeg 测试
# =============================================================================

class TestColorPrimariesToFfmpeg:
    """测试 color_primaries_to_ffmpeg 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 → 1"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.BT709)
        assert result == 1

    def test_bt2020(self) -> None:
        """测试 BT.2020 → 9"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.BT2020)
        assert result == 9

    def test_smpte431(self) -> None:
        """测试 P3_DCI → 11"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.P3_DCI)
        assert result == 11

    def test_smpte432(self) -> None:
        """测试 P3_DISPLAY → 12"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.P3_DISPLAY)
        assert result == 12

    def test_unknown_returns_none(self) -> None:
        """测试 UNKNOWN 返回 None"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.UNKNOWN)
        assert result is None


# =============================================================================
# color_transfer_to_ffmpeg 测试
# =============================================================================

class TestColorTransferToFfmpeg:
    """测试 color_transfer_to_ffmpeg 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 → 1"""
        result = color_transfer_to_ffmpeg(ColorTransfer.BT709)
        assert result == 1

    def test_pq(self) -> None:
        """测试 PQ → 16"""
        result = color_transfer_to_ffmpeg(ColorTransfer.PQ)
        assert result == 16

    def test_hlg(self) -> None:
        """测试 HLG → 18"""
        result = color_transfer_to_ffmpeg(ColorTransfer.HLG)
        assert result == 18

    def test_linear(self) -> None:
        """测试 LINEAR → 8"""
        result = color_transfer_to_ffmpeg(ColorTransfer.LINEAR)
        assert result == 8

    def test_srgb(self) -> None:
        """测试 SRGB → 13"""
        result = color_transfer_to_ffmpeg(ColorTransfer.SRGB)
        assert result == 13

    def test_unknown_returns_none(self) -> None:
        """测试 UNKNOWN 返回 None"""
        result = color_transfer_to_ffmpeg(ColorTransfer.UNKNOWN)
        assert result is None


# =============================================================================
# color_matrix_to_ffmpeg 测试
# =============================================================================

class TestColorMatrixToFfmpeg:
    """测试 color_matrix_to_ffmpeg 函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 → 1"""
        result = color_matrix_to_ffmpeg(ColorMatrix.BT709)
        assert result == 1

    def test_bt2020nc(self) -> None:
        """测试 BT2020_NC → 9"""
        result = color_matrix_to_ffmpeg(ColorMatrix.BT2020_NC)
        assert result == 9

    def test_bt2020c(self) -> None:
        """测试 BT2020_C → 10"""
        result = color_matrix_to_ffmpeg(ColorMatrix.BT2020_C)
        assert result == 10

    def test_gbr(self) -> None:
        """测试 GBR → 0"""
        result = color_matrix_to_ffmpeg(ColorMatrix.GBR)
        assert result == 0

    def test_ictcp(self) -> None:
        """测试 ICTCP → 14"""
        result = color_matrix_to_ffmpeg(ColorMatrix.ICTCP)
        assert result == 14

    def test_unknown_returns_none(self) -> None:
        """测试 UNKNOWN 返回 None"""
        result = color_matrix_to_ffmpeg(ColorMatrix.UNKNOWN)
        assert result is None


# =============================================================================
# color_range_to_ffmpeg 测试
# =============================================================================

class TestColorRangeToFfmpeg:
    """测试 color_range_to_ffmpeg 函数"""

    def test_limited(self) -> None:
        """测试 LIMITED → 1"""
        result = color_range_to_ffmpeg(ColorRange.LIMITED)
        assert result == 1

    def test_full(self) -> None:
        """测试 FULL → 2"""
        result = color_range_to_ffmpeg(ColorRange.FULL)
        assert result == 2

    def test_unknown_returns_none(self) -> None:
        """测试 UNKNOWN 返回 None"""
        result = color_range_to_ffmpeg(ColorRange.UNKNOWN)
        assert result is None


# =============================================================================
# get_standard_name 测试
# =============================================================================

class TestGetStandardName:
    """测试 get_standard_name 函数"""

    def test_color_primaries_bt709(self) -> None:
        """测试 ColorPrimaries.BT709 → 'bt709'"""
        result = get_standard_name(ColorPrimaries.BT709)
        assert result == 'bt709'

    def test_color_primaries_bt2020(self) -> None:
        """测试 ColorPrimaries.BT2020 → 'bt2020'"""
        result = get_standard_name(ColorPrimaries.BT2020)
        assert result == 'bt2020'

    def test_color_primaries_unknown(self) -> None:
        """测试 ColorPrimaries.UNKNOWN → 'unknown'"""
        result = get_standard_name(ColorPrimaries.UNKNOWN)
        assert result == 'unknown'

    def test_color_transfer_pq(self) -> None:
        """测试 ColorTransfer.PQ → 'smpte2084'"""
        result = get_standard_name(ColorTransfer.PQ)
        assert result == 'smpte2084'

    def test_color_transfer_hlg(self) -> None:
        """测试 ColorTransfer.HLG → 'arib-std-b67'"""
        result = get_standard_name(ColorTransfer.HLG)
        assert result == 'arib-std-b67'

    def test_color_transfer_unknown(self) -> None:
        """测试 ColorTransfer.UNKNOWN → 'unknown'"""
        result = get_standard_name(ColorTransfer.UNKNOWN)
        assert result == 'unknown'

    def test_color_matrix_bt709(self) -> None:
        """测试 ColorMatrix.BT709 → 'bt709'"""
        result = get_standard_name(ColorMatrix.BT709)
        assert result == 'bt709'

    def test_color_matrix_bt2020nc(self) -> None:
        """测试 ColorMatrix.BT2020_NC → 'bt2020nc'"""
        result = get_standard_name(ColorMatrix.BT2020_NC)
        assert result == 'bt2020nc'

    def test_color_matrix_unknown(self) -> None:
        """测试 ColorMatrix.UNKNOWN → 'unknown'"""
        result = get_standard_name(ColorMatrix.UNKNOWN)
        assert result == 'unknown'

    def test_color_range_limited(self) -> None:
        """测试 ColorRange.LIMITED → 'limited'"""
        result = get_standard_name(ColorRange.LIMITED)
        assert result == 'limited'

    def test_color_range_full(self) -> None:
        """测试 ColorRange.FULL → 'full'"""
        result = get_standard_name(ColorRange.FULL)
        assert result == 'full'

    def test_color_range_unknown(self) -> None:
        """测试 ColorRange.UNKNOWN → 'unknown'"""
        result = get_standard_name(ColorRange.UNKNOWN)
        assert result == 'unknown'

    def test_invalid_type_returns_unknown(self) -> None:
        """测试无效类型返回 'unknown'"""
        result = get_standard_name("not_an_enum")  # type: ignore
        assert result == 'unknown'

    def test_invalid_type_int_returns_unknown(self) -> None:
        """测试整数类型返回 'unknown'"""
        result = get_standard_name(123)  # type: ignore
        assert result == 'unknown'


# =============================================================================
# map_color_primaries 兼容性函数测试
# =============================================================================

class TestMapColorPrimaries:
    """测试 map_color_primaries 兼容性函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 (1) → 'bt709'"""
        result = map_color_primaries(1)
        assert result == 'bt709'

    def test_bt2020(self) -> None:
        """测试 BT.2020 (9) → 'bt2020'"""
        result = map_color_primaries(9)
        assert result == 'bt2020'

    def test_smpte431(self) -> None:
        """测试 SMPTE 431 (11) → 'smpte431'"""
        result = map_color_primaries(11)
        assert result == 'smpte431'

    def test_none_returns_none(self) -> None:
        """测试 None 输入返回 None"""
        result = map_color_primaries(None)
        assert result is None

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 'unknown'"""
        result = map_color_primaries(999)
        assert result == 'unknown'

    def test_negative_returns_unknown(self) -> None:
        """测试负数返回 'unknown'"""
        result = map_color_primaries(-1)
        assert result == 'unknown'


# =============================================================================
# map_color_transfer 兼容性函数测试
# =============================================================================

class TestMapColorTransfer:
    """测试 map_color_transfer 兼容性函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 (1) → 'bt709'"""
        result = map_color_transfer(1)
        assert result == 'bt709'

    def test_smpte2084(self) -> None:
        """测试 PQ (16) → 'smpte2084'"""
        result = map_color_transfer(16)
        assert result == 'smpte2084'

    def test_arib_std_b67(self) -> None:
        """测试 HLG (18) → 'arib-std-b67'"""
        result = map_color_transfer(18)
        assert result == 'arib-std-b67'

    def test_none_returns_none(self) -> None:
        """测试 None 输入返回 None"""
        result = map_color_transfer(None)
        assert result is None

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 'unknown'"""
        result = map_color_transfer(999)
        assert result == 'unknown'


# =============================================================================
# map_color_space 兼容性函数测试
# =============================================================================

class TestMapColorSpace:
    """测试 map_color_space 兼容性函数"""

    def test_bt709(self) -> None:
        """测试 BT.709 (1) → 'bt709'"""
        result = map_color_space(1)
        assert result == 'bt709'

    def test_bt2020nc(self) -> None:
        """测试 BT.2020 NCL (9) → 'bt2020nc'"""
        result = map_color_space(9)
        assert result == 'bt2020nc'

    def test_gbr(self) -> None:
        """测试 GBR (0) → 'gbr'"""
        result = map_color_space(0)
        assert result == 'gbr'

    def test_none_returns_none(self) -> None:
        """测试 None 输入返回 None"""
        result = map_color_space(None)
        assert result is None

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 'unknown'"""
        result = map_color_space(999)
        assert result == 'unknown'


# =============================================================================
# map_color_range 兼容性函数测试
# =============================================================================

class TestMapColorRange:
    """测试 map_color_range 兼容性函数"""

    def test_limited(self) -> None:
        """测试 Limited (1) → 'limited'"""
        result = map_color_range(1)
        assert result == 'limited'

    def test_full(self) -> None:
        """测试 Full (2) → 'full'"""
        result = map_color_range(2)
        assert result == 'full'

    def test_unknown(self) -> None:
        """测试 Unknown (0) → 'unknown'"""
        result = map_color_range(0)
        assert result == 'unknown'

    def test_none_returns_none(self) -> None:
        """测试 None 输入返回 None"""
        result = map_color_range(None)
        assert result is None

    def test_unknown_value_returns_unknown(self) -> None:
        """测试未知值返回 'unknown'"""
        result = map_color_range(999)
        assert result == 'unknown'


# =============================================================================
# 双向映射一致性测试
# =============================================================================

class TestBidirectionalMapping:
    """测试枚举与 FFmpeg 值的双向映射一致性"""

    def test_primaries_roundtrip(self) -> None:
        """测试 ColorPrimaries 双向映射一致性"""
        for primaries in [ColorPrimaries.BT709, ColorPrimaries.BT2020,
                          ColorPrimaries.P3_DCI, ColorPrimaries.P3_DISPLAY]:
            ffmpeg_val = color_primaries_to_ffmpeg(primaries)
            if ffmpeg_val is not None:
                result = ffmpeg_to_color_primaries(ffmpeg_val)
                assert result == primaries, f"Roundtrip failed for {primaries}"

    def test_transfer_roundtrip(self) -> None:
        """测试 ColorTransfer 双向映射一致性"""
        for transfer in [ColorTransfer.BT709, ColorTransfer.PQ,
                         ColorTransfer.HLG, ColorTransfer.LINEAR, ColorTransfer.SRGB]:
            ffmpeg_val = color_transfer_to_ffmpeg(transfer)
            if ffmpeg_val is not None:
                result = ffmpeg_to_color_transfer(ffmpeg_val)
                assert result == transfer, f"Roundtrip failed for {transfer}"

    def test_matrix_roundtrip(self) -> None:
        """测试 ColorMatrix 双向映射一致性"""
        for matrix in [ColorMatrix.BT709, ColorMatrix.BT2020_NC,
                       ColorMatrix.BT2020_C, ColorMatrix.GBR, ColorMatrix.ICTCP]:
            ffmpeg_val = color_matrix_to_ffmpeg(matrix)
            if ffmpeg_val is not None:
                result = ffmpeg_to_color_matrix(ffmpeg_val)
                assert result == matrix, f"Roundtrip failed for {matrix}"

    def test_range_roundtrip(self) -> None:
        """测试 ColorRange 双向映射一致性"""
        for color_range in [ColorRange.LIMITED, ColorRange.FULL]:
            ffmpeg_val = color_range_to_ffmpeg(color_range)
            if ffmpeg_val is not None:
                result = ffmpeg_to_color_range(ffmpeg_val)
                assert result == color_range, f"Roundtrip failed for {color_range}"


# =============================================================================
# 边界值测试
# =============================================================================

class TestBoundaryValues:
    """测试边界值和特殊情况"""

    def test_primaries_zero(self) -> None:
        """测试 primaries 值 0（reserved）"""
        result = ffmpeg_to_color_primaries(0)
        assert result == ColorPrimaries.UNKNOWN

    def test_primaries_max_valid(self) -> None:
        """测试 primaries 最大有效值 22"""
        result = ffmpeg_to_color_primaries(22)
        assert result == ColorPrimaries.EBU3213

    def test_primaries_out_of_range(self) -> None:
        """测试 primaries 超出范围值"""
        result = ffmpeg_to_color_primaries(255)
        assert result == ColorPrimaries.UNKNOWN

    def test_transfer_zero(self) -> None:
        """测试 transfer 值 0（reserved）"""
        result = ffmpeg_to_color_transfer(0)
        assert result == ColorTransfer.UNKNOWN

    def test_transfer_max_valid(self) -> None:
        """测试 transfer 最大有效值 18"""
        result = ffmpeg_to_color_transfer(18)
        assert result == ColorTransfer.HLG

    def test_transfer_out_of_range(self) -> None:
        """测试 transfer 超出范围值"""
        result = ffmpeg_to_color_transfer(255)
        assert result == ColorTransfer.UNKNOWN

    def test_matrix_zero_gbr(self) -> None:
        """测试 matrix 值 0（GBR，有效）"""
        result = ffmpeg_to_color_matrix(0)
        assert result == ColorMatrix.GBR

    def test_matrix_max_valid(self) -> None:
        """测试 matrix 最大有效值 14"""
        result = ffmpeg_to_color_matrix(14)
        assert result == ColorMatrix.ICTCP

    def test_matrix_out_of_range(self) -> None:
        """测试 matrix 超出范围值"""
        result = ffmpeg_to_color_matrix(255)
        assert result == ColorMatrix.UNKNOWN

    def test_range_all_valid_values(self) -> None:
        """测试 range 所有有效值"""
        assert ffmpeg_to_color_range(0) == ColorRange.UNKNOWN
        assert ffmpeg_to_color_range(1) == ColorRange.LIMITED
        assert ffmpeg_to_color_range(2) == ColorRange.FULL

    def test_range_out_of_range(self) -> None:
        """测试 range 超出范围值"""
        result = ffmpeg_to_color_range(3)
        assert result == ColorRange.UNKNOWN

    def test_negative_primaries(self) -> None:
        """测试负数 primaries 值"""
        result = ffmpeg_to_color_primaries(-100)
        assert result == ColorPrimaries.UNKNOWN

    def test_negative_transfer(self) -> None:
        """测试负数 transfer 值"""
        result = ffmpeg_to_color_transfer(-100)
        assert result == ColorTransfer.UNKNOWN

    def test_negative_matrix(self) -> None:
        """测试负数 matrix 值"""
        result = ffmpeg_to_color_matrix(-100)
        assert result == ColorMatrix.UNKNOWN

    def test_negative_range(self) -> None:
        """测试负数 range 值"""
        result = ffmpeg_to_color_range(-100)
        assert result == ColorRange.UNKNOWN

    def test_large_positive_primaries(self) -> None:
        """测试大正数 primaries 值"""
        result = ffmpeg_to_color_primaries(10000)
        assert result == ColorPrimaries.UNKNOWN

    def test_large_positive_transfer(self) -> None:
        """测试大正数 transfer 值"""
        result = ffmpeg_to_color_transfer(10000)
        assert result == ColorTransfer.UNKNOWN

    def test_large_positive_matrix(self) -> None:
        """测试大正数 matrix 值"""
        result = ffmpeg_to_color_matrix(10000)
        assert result == ColorMatrix.UNKNOWN

    def test_large_positive_range(self) -> None:
        """测试大正数 range 值"""
        result = ffmpeg_to_color_range(10000)
        assert result == ColorRange.UNKNOWN


# =============================================================================
# 类型注解验证测试
# =============================================================================

class TestTypeAnnotations:
    """测试函数返回类型符合预期"""

    def test_ffmpeg_to_color_primaries_returns_enum(self) -> None:
        """测试 ffmpeg_to_color_primaries 返回 ColorPrimaries"""
        result = ffmpeg_to_color_primaries(1)
        assert isinstance(result, ColorPrimaries)

    def test_ffmpeg_to_color_transfer_returns_enum(self) -> None:
        """测试 ffmpeg_to_color_transfer 返回 ColorTransfer"""
        result = ffmpeg_to_color_transfer(16)
        assert isinstance(result, ColorTransfer)

    def test_ffmpeg_to_color_matrix_returns_enum(self) -> None:
        """测试 ffmpeg_to_color_matrix 返回 ColorMatrix"""
        result = ffmpeg_to_color_matrix(9)
        assert isinstance(result, ColorMatrix)

    def test_ffmpeg_to_color_range_returns_enum(self) -> None:
        """测试 ffmpeg_to_color_range 返回 ColorRange"""
        result = ffmpeg_to_color_range(1)
        assert isinstance(result, ColorRange)

    def test_color_primaries_to_ffmpeg_returns_int_or_none(self) -> None:
        """测试 color_primaries_to_ffmpeg 返回 int 或 None"""
        result = color_primaries_to_ffmpeg(ColorPrimaries.BT709)
        assert isinstance(result, int)
        result_none = color_primaries_to_ffmpeg(ColorPrimaries.UNKNOWN)
        assert result_none is None

    def test_color_transfer_to_ffmpeg_returns_int_or_none(self) -> None:
        """测试 color_transfer_to_ffmpeg 返回 int 或 None"""
        result = color_transfer_to_ffmpeg(ColorTransfer.PQ)
        assert isinstance(result, int)
        result_none = color_transfer_to_ffmpeg(ColorTransfer.UNKNOWN)
        assert result_none is None

    def test_color_matrix_to_ffmpeg_returns_int_or_none(self) -> None:
        """测试 color_matrix_to_ffmpeg 返回 int 或 None"""
        result = color_matrix_to_ffmpeg(ColorMatrix.BT709)
        assert isinstance(result, int)
        result_none = color_matrix_to_ffmpeg(ColorMatrix.UNKNOWN)
        assert result_none is None

    def test_color_range_to_ffmpeg_returns_int_or_none(self) -> None:
        """测试 color_range_to_ffmpeg 返回 int 或 None"""
        result = color_range_to_ffmpeg(ColorRange.FULL)
        assert isinstance(result, int)
        result_none = color_range_to_ffmpeg(ColorRange.UNKNOWN)
        assert result_none is None

    def test_get_standard_name_returns_str(self) -> None:
        """测试 get_standard_name 返回 str"""
        result = get_standard_name(ColorPrimaries.BT709)
        assert isinstance(result, str)

    def test_map_color_primaries_returns_str_or_none(self) -> None:
        """测试 map_color_primaries 返回 str 或 None"""
        result = map_color_primaries(1)
        assert isinstance(result, str)
        result_none = map_color_primaries(None)
        assert result_none is None

    def test_map_color_transfer_returns_str_or_none(self) -> None:
        """测试 map_color_transfer 返回 str 或 None"""
        result = map_color_transfer(16)
        assert isinstance(result, str)
        result_none = map_color_transfer(None)
        assert result_none is None

    def test_map_color_space_returns_str_or_none(self) -> None:
        """测试 map_color_space 返回 str 或 None"""
        result = map_color_space(9)
        assert isinstance(result, str)
        result_none = map_color_space(None)
        assert result_none is None

    def test_map_color_range_returns_str_or_none(self) -> None:
        """测试 map_color_range 返回 str 或 None"""
        result = map_color_range(1)
        assert isinstance(result, str)
        result_none = map_color_range(None)
        assert result_none is None


# =============================================================================
# HDR 相关映射测试
# =============================================================================

class TestHDRMappings:
    """测试 HDR 相关色彩元数据映射"""

    def test_bt2020_pq_mapping(self) -> None:
        """测试 BT.2020 + PQ (HDR10) 映射"""
        primaries = ffmpeg_to_color_primaries(9)
        transfer = ffmpeg_to_color_transfer(16)
        assert primaries == ColorPrimaries.BT2020
        assert transfer == ColorTransfer.PQ

    def test_bt2020_hlg_mapping(self) -> None:
        """测试 BT.2020 + HLG (HDR broadcast) 映射"""
        primaries = ffmpeg_to_color_primaries(9)
        transfer = ffmpeg_to_color_transfer(18)
        assert primaries == ColorPrimaries.BT2020
        assert transfer == ColorTransfer.HLG

    def test_display_p3_pq_mapping(self) -> None:
        """测试 Display P3 + PQ (HDR) 映射"""
        primaries = ffmpeg_to_color_primaries(12)
        transfer = ffmpeg_to_color_transfer(16)
        assert primaries == ColorPrimaries.P3_DISPLAY
        assert transfer == ColorTransfer.PQ

    def test_bt2020_nc_matrix_mapping(self) -> None:
        """测试 BT.2020 NCL matrix 映射"""
        matrix = ffmpeg_to_color_matrix(9)
        assert matrix == ColorMatrix.BT2020_NC

    def test_ictcp_matrix_mapping(self) -> None:
        """测试 ICtCp matrix 映射（HDR 推荐）"""
        matrix = ffmpeg_to_color_matrix(14)
        assert matrix == ColorMatrix.ICTCP

    def test_full_range_for_hdr(self) -> None:
        """测试 Full range 用于 HDR"""
        color_range = ffmpeg_to_color_range(2)
        assert color_range == ColorRange.FULL