"""
Shared color types and utilities.

Fundamental color enums and helper functions used across the application.
These types are independent of the color processing engine and can be
imported by any layer (shared, features, app).
"""

from .types import (
    ColorPrimaries,
    ColorTransfer,
    ColorMatrix,
    ColorRange,
    get_color_primaries_name,
    get_color_transfer_name,
    get_color_matrix_name,
    is_hdr_transfer,
    map_ffmpeg_color_primaries,
    map_ffmpeg_color_transfer,
    map_ffmpeg_color_matrix,
    map_ffmpeg_color_range,
)

from .color_conversion import linear_to_srgb, srgb_to_linear

# 统一颜色选择器（CompactColorStudio）及其公共 API。
# AUD-32400：此前此处是顶层立即导入，而注释却宣称"延迟导入"——任何
# `from src.shared.color.types import ...` 都会把 QtWidgets/QtSvg 全套
# UI 栈拖进渲染侧进程。现按 PEP 562 用模块级 __getattr__ 按需加载，
# 真正实现解耦目标。
_PICKER_EXPORTS = ("CompactColorStudio", "EyeDropperOverlay", "get_color")


def __getattr__(name: str):
    """按需加载颜色选择器 UI 符号（PEP 562 lazy attribute）。"""
    if name in _PICKER_EXPORTS:
        from .color_picker import CompactColorStudio, EyeDropperOverlay, get_color

        return {"CompactColorStudio": CompactColorStudio,
                "EyeDropperOverlay": EyeDropperOverlay,
                "get_color": get_color}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_PICKER_EXPORTS))

__all__ = [
    'ColorPrimaries',
    'ColorTransfer',
    'ColorMatrix',
    'ColorRange',
    'get_color_primaries_name',
    'get_color_transfer_name',
    'get_color_matrix_name',
    'is_hdr_transfer',
    'map_ffmpeg_color_primaries',
    'map_ffmpeg_color_transfer',
    'map_ffmpeg_color_matrix',
    'map_ffmpeg_color_range',
    'linear_to_srgb',
    'srgb_to_linear',
    # 颜色选择器
    'CompactColorStudio',
    'EyeDropperOverlay',
    'get_color',
]
