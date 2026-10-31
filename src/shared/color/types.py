"""
Fundamental color types and FFmpeg mapping utilities.

Contains the basic enums (ColorPrimaries, ColorTransfer, ColorMatrix,
ColorRange) and their FFmpeg mapping tables. These types are used across
shared/ and features/ layers, so they live here to avoid circular
dependencies.

The color_manager.py in features/color imports these enums and adds
higher-level logic (ICC profiles, OCIO, singleton ColorManager).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Dict


# =============================================================================
# Color Metadata Enums (FFmpeg -> Internal Mapping)
# =============================================================================

class ColorPrimaries(Enum):
    """
    Color primaries (色域) 枚举。

    定义视频的色域范围，影响 RGB 三原色的坐标位置。

    Values:
        BT709: Rec. 709 / sRGB (HD 视频、计算机显示器)
        BT2020: Rec. 2020 (UHD/HDR 视频)
        SMPTE170M: SMPTE 170M / BT. 601 NTSC (SD 视频)
        SMPTE240M: SMPTE 240M (历史格式)
        P3_DCI: DCI-P3 (数字影院)
        P3_DISPLAY: Display P3 (Apple/现代显示器)
        BT470M: BT.470 System M (历史 NTSC)
        BT470BG: BT.470 System B,G (历史 PAL)
        FILM: Generic film (电影胶片)
        XYZ: SMPTE ST 428-1 (CIE XYZ)
        EBU3213: EBU 3213-E
    """

    BT709 = 'bt709'
    BT2020 = 'bt2020'
    SMPTE170M = 'smpte170m'      # BT.601 NTSC
    SMPTE240M = 'smpte240m'
    P3_DCI = 'smpte431'          # DCI-P3
    P3_DISPLAY = 'smpte432'      # Display P3
    BT470M = 'bt470m'
    BT470BG = 'bt470bg'
    FILM = 'film'
    XYZ = 'smpte428'
    EBU3213 = 'ebu3213'
    # ACES AP1 primaries (ACEScg working space). Not an FFmpeg-recognised
    # value — internal tag for the colour pipeline (IDT/ODT/provider).
    AP1 = 'ap1'
    UNKNOWN = 'unknown'


class ColorTransfer(Enum):
    """
    Color transfer function (传输特性/伽马曲线) 枚举。

    定义光信号到电信号的转换曲线。

    Values:
        BT709: BT.709 gamma (~2.2 with toe)
        LINEAR: Linear light (场景线性)
        SRGB: sRGB / IEC 61966-2-1 (计算机显示器)
        PQ: SMPTE ST 2084 (HDR10, 10,000 nits peak)
        HLG: ARIB STD-B67 (Hybrid Log-Gamma, HDR broadcast)
        SMPTE170M: SMPTE 170M (BT.601)
        SMPTE240M: SMPTE 240M
        GAMMA22: Pure gamma 2.2
        GAMMA28: Pure gamma 2.8
        BT2020_10: BT.2020 10-bit
        BT2020_12: BT.2020 12-bit
        ST2084: SMPTE ST 2084 (alias for PQ)
    """

    BT709 = 'bt709'
    LINEAR = 'linear'
    SRGB = 'iec61966_2_1'        # sRGB
    PQ = 'smpte2084'             # HDR10
    HLG = 'arib-std-b67'         # Hybrid Log-Gamma
    SMPTE170M = 'smpte170m'
    SMPTE240M = 'smpte240m'
    GAMMA22 = 'gamma22'
    GAMMA28 = 'gamma28'
    BT2020_10 = 'bt2020_10'
    BT2020_12 = 'bt2020_12'
    ST2084 = 'st2084'            # Alias for PQ
    TRAW_LOG0 = 'topos-traw-log0'  # TRAW PWL-log12（codec transfer id 20，v1.6）
    UNKNOWN = 'unknown'


class ColorMatrix(Enum):
    """
    Color matrix (色彩矩阵/YUV->RGB 转换矩阵) 枚举。

    定义 YUV/YCbCr 到 RGB 的转换矩阵系数。

    Values:
        BT709: Rec. 709 (HD 视频)
        BT2020_NC: Rec. 2020 non-constant luminance (UHD)
        BT2020_C: Rec. 2020 constant luminance (UHD)
        BT601: BT.601 (SD 视频)
        FCC: FCC Title 47
        SMPTE240M: SMPTE 240M
        YCGCO: YCgCo
        ICTCP: ICtCp (HDR)
        GBR: Identity (RGB planar)
    """

    BT709 = 'bt709'
    BT2020_NC = 'bt2020nc'
    BT2020_C = 'bt2020c'
    BT601 = 'smpte170m'          # BT.601 NTSC
    BT601_PAL = 'bt470bg'        # BT.601 PAL
    FCC = 'fcc'
    SMPTE240M = 'smpte240m'
    YCGCO = 'ycgco'
    ICTCP = 'ictcp'
    GBR = 'gbr'                  # Identity matrix
    UNKNOWN = 'unknown'


class ColorRange(Enum):
    """
    Color range (色彩范围) 枚举。

    定义像素值的范围限制。

    Values:
        LIMITED: TV/legal range (Y: 16-235, C: 16-240)
        FULL: PC/data range (0-255)
    """

    LIMITED = 'limited'          # TV range: 16-235
    FULL = 'full'                # PC range: 0-255
    UNKNOWN = 'unknown'


class ColorMetadataAuthority(Enum):
    """The authority used after TPIC samples have been reconstructed as RGB."""

    ICCP = 'icc'
    CICP = 'cicp'


class AlphaAssociation(Enum):
    """File-level alpha association; conversion is explicit at display boundaries."""

    ABSENT = 'absent'
    STRAIGHT = 'straight'
    PREMULTIPLIED = 'premultiplied'


# =============================================================================
# FFmpeg -> Internal Mapping Tables
# =============================================================================

# FFmpeg color_primaries values (ISO/IEC 23091-1 / ITU-T H.273)
FFMPEG_COLOR_PRIMARIES_MAP: Dict[int, ColorPrimaries] = {
    1: ColorPrimaries.BT709,
    4: ColorPrimaries.BT470M,
    5: ColorPrimaries.BT470BG,
    6: ColorPrimaries.SMPTE170M,
    7: ColorPrimaries.SMPTE240M,
    8: ColorPrimaries.FILM,
    9: ColorPrimaries.BT2020,
    10: ColorPrimaries.XYZ,
    11: ColorPrimaries.P3_DCI,
    12: ColorPrimaries.P3_DISPLAY,
    22: ColorPrimaries.EBU3213,
}

# FFmpeg color_transfer values (ISO/IEC 23091-1 / ITU-T H.273)
FFMPEG_COLOR_TRANSFER_MAP: Dict[int, ColorTransfer] = {
    1: ColorTransfer.BT709,
    4: ColorTransfer.GAMMA22,
    5: ColorTransfer.GAMMA28,
    6: ColorTransfer.SMPTE170M,
    7: ColorTransfer.SMPTE240M,
    8: ColorTransfer.LINEAR,
    13: ColorTransfer.SRGB,
    14: ColorTransfer.BT2020_10,
    15: ColorTransfer.BT2020_12,
    16: ColorTransfer.PQ,
    18: ColorTransfer.HLG,
}

# FFmpeg colorspace (color_matrix) values
FFMPEG_COLOR_MATRIX_MAP: Dict[int, ColorMatrix] = {
    0: ColorMatrix.GBR,
    1: ColorMatrix.BT709,
    4: ColorMatrix.FCC,
    5: ColorMatrix.BT601_PAL,
    6: ColorMatrix.BT601,
    7: ColorMatrix.SMPTE240M,
    8: ColorMatrix.YCGCO,
    9: ColorMatrix.BT2020_NC,
    10: ColorMatrix.BT2020_C,
    14: ColorMatrix.ICTCP,
}

# FFmpeg color_range values
FFMPEG_COLOR_RANGE_MAP: Dict[int, ColorRange] = {
    1: ColorRange.LIMITED,
    2: ColorRange.FULL,
}


# =============================================================================
# FFmpeg -> Enum Mapping Functions
# =============================================================================

def map_ffmpeg_color_primaries(value: Optional[int]) -> ColorPrimaries:
    """Map FFmpeg color_primaries value to internal enum."""
    if value is None:
        return ColorPrimaries.UNKNOWN
    return FFMPEG_COLOR_PRIMARIES_MAP.get(value, ColorPrimaries.UNKNOWN)


def map_ffmpeg_color_transfer(value: Optional[int]) -> ColorTransfer:
    """Map FFmpeg color_transfer value to internal enum."""
    if value is None:
        return ColorTransfer.UNKNOWN
    return FFMPEG_COLOR_TRANSFER_MAP.get(value, ColorTransfer.UNKNOWN)


def map_ffmpeg_color_matrix(value: Optional[int]) -> ColorMatrix:
    """Map FFmpeg colorspace value to internal enum."""
    if value is None:
        return ColorMatrix.UNKNOWN
    return FFMPEG_COLOR_MATRIX_MAP.get(value, ColorMatrix.UNKNOWN)


def map_ffmpeg_color_range(value: Optional[int]) -> ColorRange:
    """Map FFmpeg color_range value to internal enum."""
    if value is None:
        return ColorRange.UNKNOWN
    return FFMPEG_COLOR_RANGE_MAP.get(value, ColorRange.UNKNOWN)


# =============================================================================
# Display Name Helpers
# =============================================================================

def get_color_primaries_name(primaries: ColorPrimaries) -> str:
    """Get display name for color primaries."""
    names: Dict[ColorPrimaries, str] = {
        ColorPrimaries.BT709: 'Rec. 709 / sRGB',
        ColorPrimaries.BT2020: 'Rec. 2020',
        ColorPrimaries.SMPTE170M: 'BT.601 (NTSC)',
        ColorPrimaries.SMPTE240M: 'SMPTE 240M',
        ColorPrimaries.P3_DCI: 'DCI-P3',
        ColorPrimaries.P3_DISPLAY: 'Display P3',
        ColorPrimaries.BT470M: 'BT.470 System M',
        ColorPrimaries.BT470BG: 'BT.470 System B,G',
        ColorPrimaries.FILM: 'Generic Film',
        ColorPrimaries.XYZ: 'CIE XYZ',
        ColorPrimaries.EBU3213: 'EBU 3213-E',
        ColorPrimaries.UNKNOWN: 'Unknown',
    }
    return names.get(primaries, 'Unknown')


def get_color_transfer_name(transfer: ColorTransfer) -> str:
    """Get display name for color transfer."""
    names: Dict[ColorTransfer, str] = {
        ColorTransfer.BT709: 'BT.709 Gamma',
        ColorTransfer.LINEAR: 'Linear',
        ColorTransfer.SRGB: 'sRGB',
        ColorTransfer.PQ: 'PQ (HDR10)',
        ColorTransfer.HLG: 'HLG',
        ColorTransfer.SMPTE170M: 'BT.601',
        ColorTransfer.SMPTE240M: 'SMPTE 240M',
        ColorTransfer.GAMMA22: 'Gamma 2.2',
        ColorTransfer.GAMMA28: 'Gamma 2.8',
        ColorTransfer.BT2020_10: 'BT.2020 10-bit',
        ColorTransfer.BT2020_12: 'BT.2020 12-bit',
        ColorTransfer.ST2084: 'ST.2084 (PQ)',
        ColorTransfer.UNKNOWN: 'Unknown',
    }
    return names.get(transfer, 'Unknown')


def get_color_matrix_name(matrix: ColorMatrix) -> str:
    """Get display name for color matrix."""
    names: Dict[ColorMatrix, str] = {
        ColorMatrix.BT709: 'Rec. 709',
        ColorMatrix.BT2020_NC: 'Rec. 2020 NCL',
        ColorMatrix.BT2020_C: 'Rec. 2020 CL',
        ColorMatrix.BT601: 'BT.601 (NTSC)',
        ColorMatrix.BT601_PAL: 'BT.601 (PAL)',
        ColorMatrix.FCC: 'FCC',
        ColorMatrix.SMPTE240M: 'SMPTE 240M',
        ColorMatrix.YCGCO: 'YCgCo',
        ColorMatrix.ICTCP: 'ICtCp',
        ColorMatrix.GBR: 'RGB (Identity)',
        ColorMatrix.UNKNOWN: 'Unknown',
    }
    return names.get(matrix, 'Unknown')


def is_hdr_transfer(transfer: ColorTransfer) -> bool:
    """Check if the transfer function is HDR (PQ or HLG)."""
    return transfer in (ColorTransfer.PQ, ColorTransfer.HLG, ColorTransfer.ST2084)
