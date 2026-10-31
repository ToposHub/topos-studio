"""
色彩元数据映射模块。

提供 FFmpeg 色彩元数据值与内部枚举之间的双向映射。

## 功能

- 色彩原色（Color Primaries）映射
- 传输特性（Transfer Characteristics）映射
- 色彩矩阵（Color Matrix/Space）映射
- 色彩范围（Color Range）映射

## 设计原则

- 复用 color_manager.py 中的枚举定义，避免重复
- 补充 FFmpeg 特有的映射值
- 提供类型安全的转换函数
- 支持双向映射（FFmpeg 值 ↔ 枚举）

## 使用示例

```python
from src.shared.media.color_metadata import (
    ffmpeg_to_color_primaries,
    ffmpeg_to_color_transfer,
    ColorPrimaries,
    ColorTransfer,
)

# FFmpeg 值 → 枚举
primaries = ffmpeg_to_color_primaries(9)  # ColorPrimaries.BT2020

# 枚举 → FFmpeg 值
ffmpeg_value = color_primaries_to_ffmpeg(ColorPrimaries.BT709)  # 1

# 枚举 → 标准名称
name = get_standard_name(ColorTransfer.PQ)  # 'smpte2084'
```
"""

from __future__ import annotations

from typing import Optional, Dict, Any

# Import fundamental color types from shared
from src.shared.color.types import (
    ColorPrimaries,
    ColorTransfer,
    ColorMatrix,
    ColorRange,
)


# =============================================================================
# FFmpeg → 枚举 映射表（扩展版）
# =============================================================================

# FFmpeg color_primaries 值（ISO/IEC 23091-1 / ITU-T H.273）
# 复用 color_manager.py 的 FFMPEG_COLOR_PRIMARIES_MAP，补充缺失值
_FFMPEG_COLOR_PRIMARIES_MAP: Dict[int, ColorPrimaries] = {
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

# FFmpeg color_transfer 值（ISO/IEC 23091-1 / ITU-T H.273）
# 扩展 color_manager.py 中的映射，包含更多传输特性
_FFMPEG_COLOR_TRANSFER_MAP: Dict[int, ColorTransfer] = {
    1: ColorTransfer.BT709,
    4: ColorTransfer.GAMMA22,
    5: ColorTransfer.GAMMA28,
    6: ColorTransfer.SMPTE170M,
    7: ColorTransfer.SMPTE240M,
    8: ColorTransfer.LINEAR,
    # 以下为 ffmpeg_source.py 中特有的映射
    9: ColorTransfer.UNKNOWN,       # Logarithmic (100:1 range) - 暂无对应枚举
    10: ColorTransfer.UNKNOWN,      # Logarithmic (sqrt(10):1 range) - 暂无对应枚举
    11: ColorTransfer.UNKNOWN,      # IEC 61966-2-4 - 暂无对应枚举
    12: ColorTransfer.UNKNOWN,      # BT.1361 - 暂无对应枚举
    13: ColorTransfer.SRGB,
    14: ColorTransfer.BT2020_10,
    15: ColorTransfer.BT2020_12,
    16: ColorTransfer.PQ,
    17: ColorTransfer.UNKNOWN,      # SMPTE ST 428-1 - 暂无对应枚举
    18: ColorTransfer.HLG,
    # AUD-34421：TRAW PWL-log12（RC7；codec transfer id 20）。此前缺失使
    # ffmpeg_to_color_transfer(20) 静默降级 UNKNOWN——与 normalize_color_transfer
    # 兼容表（20 → 'traw-log0'）不同步，TRAW 源经枚举路径读取时 transfer 丢失。
    20: ColorTransfer.TRAW_LOG0,
}

# FFmpeg colorspace (color_matrix) 值
# 扩展 color_manager.py 中的映射
_FFMPEG_COLOR_MATRIX_MAP: Dict[int, ColorMatrix] = {
    0: ColorMatrix.GBR,
    1: ColorMatrix.BT709,
    # 2: unspecified
    # 3: reserved
    4: ColorMatrix.FCC,
    5: ColorMatrix.BT601_PAL,
    6: ColorMatrix.BT601,
    7: ColorMatrix.SMPTE240M,
    8: ColorMatrix.YCGCO,
    9: ColorMatrix.BT2020_NC,
    10: ColorMatrix.BT2020_C,
    # 11: SMPTE ST 2085 YDzDxDz - 暂无对应枚举
    # 12: Chromaticity-derived non-constant
    # 13: Chromaticity-derived constant
    14: ColorMatrix.ICTCP,
}

# FFmpeg color_range 值
_FFMPEG_COLOR_RANGE_MAP: Dict[int, ColorRange] = {
    0: ColorRange.UNKNOWN,
    1: ColorRange.LIMITED,
    2: ColorRange.FULL,
}


# =============================================================================
# 枚举 → FFmpeg 值 反向映射
# =============================================================================

# ColorPrimaries → FFmpeg 值
_ENUM_TO_FFMPEG_PRIMARIES: Dict[ColorPrimaries, int] = {
    ColorPrimaries.BT709: 1,
    ColorPrimaries.BT470M: 4,
    ColorPrimaries.BT470BG: 5,
    ColorPrimaries.SMPTE170M: 6,
    ColorPrimaries.SMPTE240M: 7,
    ColorPrimaries.FILM: 8,
    ColorPrimaries.BT2020: 9,
    ColorPrimaries.XYZ: 10,
    ColorPrimaries.P3_DCI: 11,
    ColorPrimaries.P3_DISPLAY: 12,
    ColorPrimaries.EBU3213: 22,
    # AUD-34423: AP1 无对应 FFmpeg 值（非 FFmpeg 识别的内部标签），
    # 反向映射刻意不收录（get 返回 None），调用方按内部标签处理。
}

# ColorTransfer → FFmpeg 值
_ENUM_TO_FFMPEG_TRANSFER: Dict[ColorTransfer, int] = {
    ColorTransfer.BT709: 1,
    ColorTransfer.GAMMA22: 4,
    ColorTransfer.GAMMA28: 5,
    ColorTransfer.SMPTE170M: 6,
    ColorTransfer.SMPTE240M: 7,
    ColorTransfer.LINEAR: 8,
    ColorTransfer.SRGB: 13,
    ColorTransfer.BT2020_10: 14,
    ColorTransfer.BT2020_12: 15,
    ColorTransfer.PQ: 16,
    ColorTransfer.HLG: 18,
    # AUD-34421：ST2084 是 PQ 的别名（H.273 同一值 16）；TRAW_LOG0 为
    # transfer id 20。此前两者均缺失，enum→ffmpeg 往返时标签丢失。
    ColorTransfer.ST2084: 16,
    ColorTransfer.TRAW_LOG0: 20,
}

# ColorMatrix → FFmpeg 值
_ENUM_TO_FFMPEG_MATRIX: Dict[ColorMatrix, int] = {
    ColorMatrix.GBR: 0,
    ColorMatrix.BT709: 1,
    ColorMatrix.FCC: 4,
    ColorMatrix.BT601_PAL: 5,
    ColorMatrix.BT601: 6,
    ColorMatrix.SMPTE240M: 7,
    ColorMatrix.YCGCO: 8,
    ColorMatrix.BT2020_NC: 9,
    ColorMatrix.BT2020_C: 10,
    ColorMatrix.ICTCP: 14,
}

# ColorRange → FFmpeg 值
_ENUM_TO_FFMPEG_RANGE: Dict[ColorRange, int] = {
    ColorRange.UNKNOWN: 0,
    ColorRange.LIMITED: 1,
    ColorRange.FULL: 2,
}


# =============================================================================
# 枚举 → 标准名称 映射
# =============================================================================

# ColorPrimaries → 标准名称（FFmpeg 标准名称）
_PRIMARIES_TO_STANDARD_NAME: Dict[ColorPrimaries, str] = {
    ColorPrimaries.BT709: 'bt709',
    ColorPrimaries.BT2020: 'bt2020',
    ColorPrimaries.SMPTE170M: 'smpte170m',
    ColorPrimaries.SMPTE240M: 'smpte240m',
    ColorPrimaries.P3_DCI: 'smpte431',
    ColorPrimaries.P3_DISPLAY: 'smpte432',
    ColorPrimaries.BT470M: 'bt470m',
    ColorPrimaries.BT470BG: 'bt470bg',
    ColorPrimaries.FILM: 'film',
    ColorPrimaries.XYZ: 'smpte428',
    ColorPrimaries.EBU3213: 'ebu3213',
    # AUD-34423: AP1 是内部 ACES AP1 标签（非 FFmpeg 值），标准名取枚举值。
    ColorPrimaries.AP1: 'ap1',
    ColorPrimaries.UNKNOWN: 'unknown',
}

# ColorTransfer → 标准名称（FFmpeg 标准名称）
_TRANSFER_TO_STANDARD_NAME: Dict[ColorTransfer, str] = {
    ColorTransfer.BT709: 'bt709',
    ColorTransfer.LINEAR: 'linear',
    ColorTransfer.SRGB: 'iec61966_2_1',
    ColorTransfer.PQ: 'smpte2084',
    ColorTransfer.HLG: 'arib-std-b67',
    ColorTransfer.SMPTE170M: 'smpte170m',
    ColorTransfer.SMPTE240M: 'smpte240m',
    ColorTransfer.GAMMA22: 'gamma22',
    ColorTransfer.GAMMA28: 'gamma28',
    ColorTransfer.BT2020_10: 'bt2020_10',
    ColorTransfer.BT2020_12: 'bt2020_12',
    ColorTransfer.ST2084: 'smpte2084',
    # AUD-34421：TRAW PWL-log12 的标准名（与 normalize_color_transfer
    # 兼容表 20 → 'traw-log0' 一致）
    ColorTransfer.TRAW_LOG0: 'traw-log0',
    ColorTransfer.UNKNOWN: 'unknown',
}

# ColorMatrix → 标准名称（FFmpeg 标准名称）
_MATRIX_TO_STANDARD_NAME: Dict[ColorMatrix, str] = {
    ColorMatrix.BT709: 'bt709',
    ColorMatrix.BT2020_NC: 'bt2020nc',
    ColorMatrix.BT2020_C: 'bt2020c',
    ColorMatrix.BT601: 'smpte170m',
    ColorMatrix.BT601_PAL: 'bt470bg',
    ColorMatrix.FCC: 'fcc',
    ColorMatrix.SMPTE240M: 'smpte240m',
    ColorMatrix.YCGCO: 'ycgco',
    ColorMatrix.ICTCP: 'ictcp',
    ColorMatrix.GBR: 'gbr',
    ColorMatrix.UNKNOWN: 'unknown',
}

# ColorRange → 标准名称
_RANGE_TO_STANDARD_NAME: Dict[ColorRange, str] = {
    ColorRange.LIMITED: 'limited',
    ColorRange.FULL: 'full',
    ColorRange.UNKNOWN: 'unknown',
}


# =============================================================================
# 转换函数：FFmpeg 值 → 枚举
# =============================================================================

def ffmpeg_to_color_primaries(value: Optional[int]) -> ColorPrimaries:
    """
    将 FFmpeg color_primaries 值映射为内部枚举。

    Args:
        value: FFmpeg color_primaries 值（ISO/IEC 23091-1 / ITU-T H.273）

    Returns:
        ColorPrimaries 枚举值，未知值返回 UNKNOWN

    Examples:
        >>> ffmpeg_to_color_primaries(1)
        <ColorPrimaries.BT709: 'bt709'>
        >>> ffmpeg_to_color_primaries(9)
        <ColorPrimaries.BT2020: 'bt2020'>
        >>> ffmpeg_to_color_primaries(None)
        <ColorPrimaries.UNKNOWN: 'unknown'>
    """
    if value is None:
        return ColorPrimaries.UNKNOWN
    return _FFMPEG_COLOR_PRIMARIES_MAP.get(value, ColorPrimaries.UNKNOWN)


def ffmpeg_to_color_transfer(value: Optional[int]) -> ColorTransfer:
    """
    将 FFmpeg color_transfer 值映射为内部枚举。

    Args:
        value: FFmpeg color_transfer 值（ISO/IEC 23091-1 / ITU-T H.273）

    Returns:
        ColorTransfer 枚举值，未知值返回 UNKNOWN

    Examples:
        >>> ffmpeg_to_color_transfer(16)
        <ColorTransfer.PQ: 'smpte2084'>
        >>> ffmpeg_to_color_transfer(18)
        <ColorTransfer.HLG: 'arib-std-b67'>
        >>> ffmpeg_to_color_transfer(None)
        <ColorTransfer.UNKNOWN: 'unknown'>
    """
    if value is None:
        return ColorTransfer.UNKNOWN
    return _FFMPEG_COLOR_TRANSFER_MAP.get(value, ColorTransfer.UNKNOWN)


def ffmpeg_to_color_matrix(value: Optional[int]) -> ColorMatrix:
    """
    将 FFmpeg colorspace 值映射为内部枚举。

    Args:
        value: FFmpeg colorspace 值（ISO/IEC 23091-1 / ITU-T H.273）

    Returns:
        ColorMatrix 枚举值，未知值返回 UNKNOWN

    Examples:
        >>> ffmpeg_to_color_matrix(1)
        <ColorMatrix.BT709: 'bt709'>
        >>> ffmpeg_to_color_matrix(9)
        <ColorMatrix.BT2020_NC: 'bt2020nc'>
        >>> ffmpeg_to_color_matrix(None)
        <ColorMatrix.UNKNOWN: 'unknown'>
    """
    if value is None:
        return ColorMatrix.UNKNOWN
    return _FFMPEG_COLOR_MATRIX_MAP.get(value, ColorMatrix.UNKNOWN)


def ffmpeg_to_color_range(value: Optional[int]) -> ColorRange:
    """
    将 FFmpeg color_range 值映射为内部枚举。

    Args:
        value: FFmpeg color_range 值

    Returns:
        ColorRange 枚举值，未知值返回 UNKNOWN

    Examples:
        >>> ffmpeg_to_color_range(1)
        <ColorRange.LIMITED: 'limited'>
        >>> ffmpeg_to_color_range(2)
        <ColorRange.FULL: 'full'>
        >>> ffmpeg_to_color_range(None)
        <ColorRange.UNKNOWN: 'unknown'>
    """
    if value is None:
        return ColorRange.UNKNOWN
    return _FFMPEG_COLOR_RANGE_MAP.get(value, ColorRange.UNKNOWN)


# =============================================================================
# 转换函数：枚举 → FFmpeg 值
# =============================================================================

def color_primaries_to_ffmpeg(primaries: ColorPrimaries) -> Optional[int]:
    """
    将 ColorPrimaries 枚举转换为 FFmpeg 值。

    Args:
        primaries: ColorPrimaries 枚举值

    Returns:
        FFmpeg color_primaries 值，UNKNOWN 返回 None

    Examples:
        >>> color_primaries_to_ffmpeg(ColorPrimaries.BT709)
        1
        >>> color_primaries_to_ffmpeg(ColorPrimaries.BT2020)
        9
        >>> color_primaries_to_ffmpeg(ColorPrimaries.UNKNOWN)
        None
    """
    if primaries == ColorPrimaries.UNKNOWN:
        return None
    return _ENUM_TO_FFMPEG_PRIMARIES.get(primaries)


def color_transfer_to_ffmpeg(transfer: ColorTransfer) -> Optional[int]:
    """
    将 ColorTransfer 枚举转换为 FFmpeg 值。

    Args:
        transfer: ColorTransfer 枚举值

    Returns:
        FFmpeg color_transfer 值，UNKNOWN 返回 None

    Examples:
        >>> color_transfer_to_ffmpeg(ColorTransfer.PQ)
        16
        >>> color_transfer_to_ffmpeg(ColorTransfer.HLG)
        18
        >>> color_transfer_to_ffmpeg(ColorTransfer.UNKNOWN)
        None
    """
    if transfer == ColorTransfer.UNKNOWN:
        return None
    return _ENUM_TO_FFMPEG_TRANSFER.get(transfer)


def color_matrix_to_ffmpeg(matrix: ColorMatrix) -> Optional[int]:
    """
    将 ColorMatrix 枚举转换为 FFmpeg 值。

    Args:
        matrix: ColorMatrix 枚举值

    Returns:
        FFmpeg colorspace 值，UNKNOWN 返回 None

    Examples:
        >>> color_matrix_to_ffmpeg(ColorMatrix.BT709)
        1
        >>> color_matrix_to_ffmpeg(ColorMatrix.BT2020_NC)
        9
        >>> color_matrix_to_ffmpeg(ColorMatrix.UNKNOWN)
        None
    """
    if matrix == ColorMatrix.UNKNOWN:
        return None
    return _ENUM_TO_FFMPEG_MATRIX.get(matrix)


def color_range_to_ffmpeg(color_range: ColorRange) -> Optional[int]:
    """
    将 ColorRange 枚举转换为 FFmpeg 值。

    Args:
        color_range: ColorRange 枚举值

    Returns:
        FFmpeg color_range 值，UNKNOWN 返回 None

    Examples:
        >>> color_range_to_ffmpeg(ColorRange.LIMITED)
        1
        >>> color_range_to_ffmpeg(ColorRange.FULL)
        2
        >>> color_range_to_ffmpeg(ColorRange.UNKNOWN)
        None
    """
    if color_range == ColorRange.UNKNOWN:
        return None
    return _ENUM_TO_FFMPEG_RANGE.get(color_range)


# =============================================================================
# 转换函数：枚举 → 标准名称
# =============================================================================

def get_standard_name(
    color_enum: ColorPrimaries | ColorTransfer | ColorMatrix | ColorRange,
) -> str:
    """
    将色彩枚举转换为 FFmpeg 标准名称。

    支持所有四种色彩枚举类型。

    Args:
        color_enum: ColorPrimaries | ColorTransfer | ColorMatrix | ColorRange

    Returns:
        FFmpeg 标准名称字符串

    Examples:
        >>> get_standard_name(ColorPrimaries.BT709)
        'bt709'
        >>> get_standard_name(ColorTransfer.PQ)
        'smpte2084'
        >>> get_standard_name(ColorMatrix.BT2020_NC)
        'bt2020nc'
        >>> get_standard_name(ColorRange.LIMITED)
        'limited'
    """
    if isinstance(color_enum, ColorPrimaries):
        return _PRIMARIES_TO_STANDARD_NAME.get(color_enum, 'unknown')
    elif isinstance(color_enum, ColorTransfer):
        return _TRANSFER_TO_STANDARD_NAME.get(color_enum, 'unknown')
    elif isinstance(color_enum, ColorMatrix):
        return _MATRIX_TO_STANDARD_NAME.get(color_enum, 'unknown')
    elif isinstance(color_enum, ColorRange):
        return _RANGE_TO_STANDARD_NAME.get(color_enum, 'unknown')
    else:
        return 'unknown'


# =============================================================================
# 兼容性别名（向后兼容 ffmpeg_source.py）
# =============================================================================

# 这些映射表与 ffmpeg_source.py 中的定义保持兼容
# 用于过渡期的代码迁移
COLOR_PRIMARIES_MAP: Dict[int, str] = {
    1: 'bt709',
    4: 'bt470m',
    5: 'bt470bg',
    6: 'smpte170m',
    7: 'smpte240m',
    8: 'film',
    9: 'bt2020',
    10: 'smpte428',
    11: 'smpte431',
    12: 'smpte432',
    22: 'ebu3213',
}

COLOR_TRANSFER_MAP: Dict[int, str] = {
    1: 'bt709',
    4: 'gamma22',
    5: 'gamma28',
    6: 'smpte170m',
    7: 'smpte240m',
    8: 'linear',
    9: 'log',
    10: 'log_sqrt',
    11: 'iec61966_2_4',
    12: 'bt1361',
    13: 'iec61966_2_1',
    14: 'bt2020_10',
    15: 'bt2020_12',
    16: 'smpte2084',
    17: 'smpte428',
    18: 'arib-std-b67',
    20: 'traw-log0',   # RC7：TRAW PWL-log12（.toos/MOV RAW 冻结对）
}

COLOR_SPACE_MAP: Dict[int, str] = {
    0: 'gbr',
    1: 'bt709',
    2: 'unknown',
    3: 'reserved',
    4: 'fcc',
    5: 'bt470bg',
    6: 'smpte170m',
    7: 'smpte240m',
    8: 'ycgco',
    9: 'bt2020nc',
    10: 'bt2020c',
    11: 'smpte2085',
    12: 'chroma_nc',
    13: 'chroma_c',
    14: 'ictcp',
}

COLOR_RANGE_MAP: Dict[int, str] = {
    0: 'unknown',
    1: 'limited',
    2: 'full',
}


# =============================================================================
# 兼容性函数（向后兼容 ffmpeg_source.py）
# =============================================================================

def map_color_primaries(value: Optional[int]) -> Optional[str]:
    """
    将 FFmpeg color_primaries 值映射为标准名称（兼容性别名）。

    Args:
        value: FFmpeg color_primaries 值

    Returns:
        标准名称或 None

    Note:
        此函数用于向后兼容，新代码应使用 ffmpeg_to_color_primaries()。
    """
    if value is None:
        return None
    return COLOR_PRIMARIES_MAP.get(value, 'unknown')


def map_color_transfer(value: Optional[int]) -> Optional[str]:
    """
    将 FFmpeg color_transfer 值映射为标准名称（兼容性别名）。

    Args:
        value: FFmpeg color_transfer 值

    Returns:
        标准名称或 None

    Note:
        此函数用于向后兼容，新代码应使用 ffmpeg_to_color_transfer()。
    """
    if value is None:
        return None
    return COLOR_TRANSFER_MAP.get(value, 'unknown')


def normalize_color_transfer(value: Any) -> Optional[str]:
    """
    将任意类型的 color_transfer 值规范化为标准名称。

    PyAV 可能返回多种类型的 color_trc 值：
    - int (标准 FFmpeg 值如 16 = PQ)
    - str ('smpte2084', 'pq', 'bt709' 等)
    - enum 对象 (有 .name 属性)
    - None

    Args:
        value: 任意类型的 color_transfer 值

    Returns:
        标准名称（如 'smpte2084', 'arib-std-b67', 'bt709'）
        或 'unknown'（无法识别）
        或 None（输入为 None）

    Examples:
        >>> normalize_color_transfer(16)
        'smpte2084'
        >>> normalize_color_transfer('smpte2084')
        'smpte2084'
        >>> normalize_color_transfer('pq')
        'smpte2084'
        >>> normalize_color_transfer(None)
        None
    """
    if value is None:
        return None

    # 1. 尝试直接映射 int 值
    if isinstance(value, int):
        return COLOR_TRANSFER_MAP.get(value, 'unknown')

    # 2. 尝试从 enum 对象获取值
    if hasattr(value, 'name'):
        # PyAV enum 对象有 .name 属性
        enum_name = str(value.name).lower()
        # 直接返回已知名称
        if enum_name in ('smpte2084', 'pq', 'arib-std-b67', 'hlg',
                         'bt709', 'bt2020_10', 'bt2020_12',
                         'linear', 'iec61966_2_1', 'srgb'):
            # 规范化别名
            return _normalize_transfer_alias(enum_name)
        # 尝试转换 enum 的 int 值
        try:
            int_val = int(value)
            return COLOR_TRANSFER_MAP.get(int_val, 'unknown')
        except (ValueError, TypeError):
            pass

    # 3. 处理字符串值
    if isinstance(value, str):
        return _normalize_transfer_alias(value.lower())

    # 4. 尝试强制转换为 int
    try:
        int_val = int(value)
        return COLOR_TRANSFER_MAP.get(int_val, 'unknown')
    except (ValueError, TypeError):
        pass

    # 无法识别，返回 unknown
    return 'unknown'


def _normalize_transfer_alias(name: str) -> str:
    """
    将传输特性别名规范化为标准名称。

    Args:
        name: 传输特性名称或别名

    Returns:
        标准名称
    """
    # PQ 别名映射
    PQ_ALIASES = {'pq', 'smpte2084', 'st2084', 'smpte_st2084'}
    # HLG 别名映射
    HLG_ALIASES = {'hlg', 'arib-std-b67', 'arib_std_b67', 'aribb67'}
    # sRGB 别名映射
    SRGB_ALIASES = {'srgb', 'iec61966_2_1', 'iec61966-2-1'}
    # 其他标准名称
    OTHER_STANDARD = {
        'bt709', 'bt2020_10', 'bt2020_12', 'linear',
        'smpte170m', 'smpte240m', 'gamma22', 'gamma28',
        'unknown'
    }

    name_lower = name.lower().replace('-', '_').replace(' ', '_')

    if name_lower in PQ_ALIASES:
        return 'smpte2084'
    if name_lower in HLG_ALIASES:
        return 'arib-std-b67'
    if name_lower in SRGB_ALIASES:
        return 'iec61966_2_1'  # sRGB 的标准 FFmpeg 名称

    # 直接匹配已知标准名称
    for std_name in OTHER_STANDARD:
        if name_lower == std_name.lower().replace('-', '_'):
            return std_name

    # AUD-34422：透传仅限映射表内全部标准值（覆盖 OTHER_STANDARD 之外的
    # 合法 FFmpeg 名称，如 smpte428/log_sqrt）；其余未识别字符串按
    # docstring 契约返回 'unknown'，不再透传原值
    for std_name in COLOR_TRANSFER_MAP.values():
        if name_lower == std_name.lower().replace('-', '_').replace(' ', '_'):
            return std_name

    return 'unknown'


def normalize_color_primaries(value: Any) -> Optional[str]:
    """
    将任意类型的 color_primaries 值规范化为标准名称。

    Args:
        value: 任意类型的 color_primaries 值

    Returns:
        标准名称或 'unknown' 或 None
    """
    if value is None:
        return None

    if isinstance(value, int):
        return COLOR_PRIMARIES_MAP.get(value, 'unknown')

    if hasattr(value, 'name'):
        enum_name = str(value.name).lower()
        if enum_name in ('bt709', 'bt2020', 'smpte170m', 'smpte431', 'smpte432'):
            return enum_name
        try:
            int_val = int(value)
            return COLOR_PRIMARIES_MAP.get(int_val, 'unknown')
        except (ValueError, TypeError):
            pass

    if isinstance(value, str):
        return _normalize_primaries_alias(value.lower())

    try:
        int_val = int(value)
        return COLOR_PRIMARIES_MAP.get(int_val, 'unknown')
    except (ValueError, TypeError):
        pass

    return 'unknown'


def _normalize_primaries_alias(name: str) -> str:
    """
    将色域别名规范化为标准名称。
    """
    name_lower = name.lower().replace('-', '_').replace(' ', '_')

    # P3 别名映射
    P3_DCI_ALIASES = {'p3_dci', 'smpte431', 'dci_p3', 'dcip3'}
    P3_DISPLAY_ALIASES = {'p3_display', 'smpte432', 'display_p3', 'displayp3'}

    if name_lower in P3_DCI_ALIASES:
        return 'smpte431'
    if name_lower in P3_DISPLAY_ALIASES:
        return 'smpte432'

    # 直接匹配
    for std_name in COLOR_PRIMARIES_MAP.values():
        if name_lower == std_name.lower().replace('-', '_'):
            return std_name

    # AUD-34422：未识别字符串按 docstring 契约返回 'unknown'，不透传原值
    return 'unknown'


def normalize_color_space(value: Any) -> Optional[str]:
    """
    将任意类型的 color_space/color_matrix 值规范化为标准名称。
    """
    if value is None:
        return None

    if isinstance(value, int):
        return COLOR_SPACE_MAP.get(value, 'unknown')

    if hasattr(value, 'name'):
        enum_name = str(value.name).lower()
        if enum_name in ('bt709', 'bt2020nc', 'bt2020c', 'smpte170m'):
            return enum_name
        try:
            int_val = int(value)
            return COLOR_SPACE_MAP.get(int_val, 'unknown')
        except (ValueError, TypeError):
            pass

    if isinstance(value, str):
        return _normalize_space_alias(value.lower())

    try:
        int_val = int(value)
        return COLOR_SPACE_MAP.get(int_val, 'unknown')
    except (ValueError, TypeError):
        pass

    return 'unknown'


def _normalize_space_alias(name: str) -> str:
    """
    将色彩矩阵别名规范化为标准名称。
    """
    name_lower = name.lower().replace('-', '_').replace(' ', '_')

    # BT.2020 别名
    BT2020_NC_ALIASES = {'bt2020nc', 'bt2020_nc', 'bt2020nonconstant'}

    if name_lower in BT2020_NC_ALIASES:
        return 'bt2020nc'

    for std_name in COLOR_SPACE_MAP.values():
        if name_lower == std_name.lower().replace('-', '_'):
            return std_name

    # AUD-34422：未识别字符串按 docstring 契约返回 'unknown'，不透传原值
    return 'unknown'


def map_color_space(value: Optional[int]) -> Optional[str]:
    """
    将 FFmpeg colorspace 值映射为标准名称（兼容性别名）。

    Args:
        value: FFmpeg colorspace 值

    Returns:
        标准名称或 None

    Note:
        此函数用于向后兼容，新代码应使用 ffmpeg_to_color_matrix()。
    """
    if value is None:
        return None
    return COLOR_SPACE_MAP.get(value, 'unknown')


def map_color_range(value: Optional[int]) -> Optional[str]:
    """
    将 FFmpeg color_range 值映射为标准名称（兼容性别名）。

    Args:
        value: FFmpeg color_range 值

    Returns:
        标准名称或 None

    Note:
        此函数用于向后兼容，新代码应使用 ffmpeg_to_color_range()。
    """
    if value is None:
        return None
    return COLOR_RANGE_MAP.get(value, 'unknown')


# =============================================================================
# 重新导出 color_manager 中的函数（统一接口）
# =============================================================================

# Import display name functions from shared color types
from src.shared.color.types import (
    get_color_primaries_name,
    get_color_transfer_name,
    get_color_matrix_name,
    is_hdr_transfer,
)

# 重新导出所有公共接口
__all__ = [
    # 枚举类型
    'ColorPrimaries',
    'ColorTransfer',
    'ColorMatrix',
    'ColorRange',

    # FFmpeg → 枚举
    'ffmpeg_to_color_primaries',
    'ffmpeg_to_color_transfer',
    'ffmpeg_to_color_matrix',
    'ffmpeg_to_color_range',

    # 枚举 → FFmpeg
    'color_primaries_to_ffmpeg',
    'color_transfer_to_ffmpeg',
    'color_matrix_to_ffmpeg',
    'color_range_to_ffmpeg',

    # 枚举 → 标准名称
    'get_standard_name',

    # 显示名称
    'get_color_primaries_name',
    'get_color_transfer_name',
    'get_color_matrix_name',

    # HDR 检测
    'is_hdr_transfer',

    # 兼容性别名（向后兼容）
    'COLOR_PRIMARIES_MAP',
    'COLOR_TRANSFER_MAP',
    'COLOR_SPACE_MAP',
    'COLOR_RANGE_MAP',
    'map_color_primaries',
    'map_color_transfer',
    'map_color_space',
    'map_color_range',

    # 新增：类型兼容规范化函数
    'normalize_color_transfer',
    'normalize_color_primaries',
    'normalize_color_space',
]