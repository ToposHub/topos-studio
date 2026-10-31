# -*- coding: utf-8 -*-
"""
Topos Color - Edit 模块专属设计 Token
======================================

为 Edit 页面（时间线、媒体池、属性面板等）提供统一的设计规范。
橙色仅用于 active/accent，主体保持中性深灰色调。
"""

from __future__ import annotations

# ============================================================================
# 设计 Token - 颜色
# ============================================================================
EDIT_COLORS = {
    # 背景层级 — DaVinci Resolve 深色冷灰色阶
    "bg_app": "#1B1C21",        # 应用主背景
    "bg_panel": "#1B1C21",      # 面板背景
    "bg_panel_alt": "#202126",  # 面板交替背景
    "bg_toolbar": "#191A1F",    # 工具栏背景
    "bg_input": "#15161A",      # 输入框背景

    # 边框 — 半透明线条
    "border": "#34363E",        # 标准边框
    "border_subtle": "#282A31", # 细微边框

    # 文字
    "text": "#D6D8DE",          # 主文字颜色
    "text_muted": "#8E929A",    # 次要文字颜色

    # 强调色（DaVinci 橙色）
    "accent": "#E29227",        # 主题橘黄色（激活/选中）
    "accent_hover": "#f09a3e",  # 橘黄色悬停

    # 状态色
    "danger": "#B71C1C",        # 深红（警示色）
    "success": "#73b66b",       # 成功

    # 时间线专用色
    # Keep the timeline near the Color page's neutral panel hierarchy rather
    # than treating the entire editing canvas as the application's darkest
    # surface. Semantic clip colors remain intentionally distinct.
    "timeline_canvas_bg": "#242429",
    "video_clip": "#285C78",    # 视频片段（低饱和专业蓝）
    "video_clip_label_bar": "#1B4259",  # 视频片段标签栏底色
    "video_clip_selected": "#E29227",   # 视频片段选中边框
    "audio_clip": "#266C43",    # 音频片段（低饱和专业绿）
    "audio_clip_label_bar": "#1A4D30",  # 音频片段标签栏底色
    "audio_clip_selected": "#E29227",   # 音频片段选中边框
    "transition": "#ba8b45",    # 转场
    "playhead": "#FF3333",      # 播放头（DaVinci 红色）
    "clip_separator": "#0A0A0C",    # 片段间隔暗缝色
    "clip_gap_width": 2,            # 片段间隔宽度 (px)

    # 材质效果色
    "highlight_edge": "#FFFFFF",
    "highlight_edge_alpha": 40,
    "inner_shadow": "#000000",
    "inner_shadow_alpha": 100,

    # 时间刻度尺专用 (DaVinci-like 层级)
    "ruler_bg": "#202025",              # 刻度尺背景
    "ruler_bottom_line": "#34343A",     # ruler 底部分隔线
    "ruler_major_tick": "#5C606A",      # 主刻度线
    "ruler_medium_tick": "#3C4049",     # 中刻度线
    "ruler_minor_tick": "#2D3037",      # 小刻度线
    "ruler_major_grid": "rgba(90, 95, 105, 0.22)",  # 主网格线
    "ruler_minor_grid": "rgba(70, 74, 82, 0.12)",   # 副网格线
    "ruler_text": "#9DA2AC",            # 时间码文字
    "ruler_active_text": "#E29227",     # 播放头位置高亮

    # 轨道标头专用 (DaVinci-like)
    "track_header_bg": "#242429",           # 轨道头背景
    "track_header_bg_alt": "#202025",       # 轨道头交替背景
    "track_header_bg_selected": "#2A2A30",
    "track_header_divider": "#17171B",      # 轨道头分隔线
    "track_header_target_bar": "#D8872B",   # 目标轨指示条
    "track_header_badge_video": "#743841",  # 视频轨 badge (红褐色)
    "track_header_badge_audio": "#743841",  # 音频轨 badge (红褐色)

    # 左上 timecode 面板
    "timecode_panel_bg": "#202025",     # timecode 面板背景
    "timecode_panel_text": "#D8D9DE",   # timecode 文字色
    "timecode_panel_bottom_line": "#34343A",  # 面板底部分隔线

    # 波形专用
    "waveform_bg_dark": "#152D24",
    "waveform_line": "#B7DECC",         # bright enough for low-level detail

    # Phase 2: 片段徽章和标记专用色
    "badge_bg": "#000000",
    "badge_text": "#ffffff",
    "speed_fast": "#ff9500",
    "speed_slow": "#5ac8fa",
    "opacity_indicator": "#a0a0a0",
    "effects_badge": "#ffcc00",
}

# ============================================================================
# 设计 Token - 尺寸
# ============================================================================
EDIT_SIZES = {
    "toolbar_h": 30,            # 工具栏高度
    "icon_btn": 26,             # 图标按钮尺寸
    "track_header_w": 220,      # 轨道头宽度 (DaVinci-like 加宽)
    "track_h_video": 82,        # 视频轨道高度，接近 DaVinci 的可读缩略图轨道
    "track_h_audio": 66,        # 音频轨道高度，为波形和轨道头信息留空间
    "track_h_min": 24,          # BUG-09: 轨道最小高度 (统一常量)
    "track_h_max": 200,         # BUG-09: 轨道最大高度 (统一常量)
    "radius": 4,                # 标准圆角半径

    # Timeline layout constants (DaVinci-like 优化)
    "ruler_h": 38,              # 时间码标尺高度
    "track_area_top": 38,       # 轨道内容区起始 y 坐标 (= ruler_h)

    # DaVinci-like 轨道头信息布局尺寸
    "track_badge_w": 32,        # 轨道编号 badge 宽度
    "track_badge_h": 20,        # 轨道编号 badge 高度
    "track_badge_radius": 3,    # badge 圆角半径
    "track_button_size": 18,    # 轨道控制点击尺寸
    "track_button_margin": 3,   # 按钮间距

    # DaVinci-like 时间刻度条参数
    "ruler_major_tick_h": 12,   # 主刻度线高度
    "ruler_medium_tick_h": 7,   # 中刻度线高度
    "ruler_minor_tick_h": 4,    # 小刻度线高度
    "ruler_target_major_min_px": 90,   # 主刻度最小像素间距
    "ruler_target_major_max_px": 150,  # 主刻度最大像素间距
    "ruler_target_minor_min_px": 10,   # 副刻度最小像素间距
    "ruler_target_minor_max_px": 24,   # 副刻度最大像素间距

    # DaVinci-like 播放头吸附参数
    "snap_threshold_px": 10,    # 吸附像素阈值

    # Phase 2: 片段徽章和标记专用尺寸
    "badge_h": 16,              # 徽章高度
    "badge_radius": 3,          # 徽章圆角半径
    "badge_font_size": 8,       # 徽章字体大小
    "clip_icon_size": 14,       # 片段类型图标尺寸
    "indicator_h": 14,          # 指示器高度
    "indicator_radius": 2,      # 指示器圆角半径
    "indicator_font_size": 7,   # 指示器字体大小
    "min_clip_width_for_badge": 80,   # 显示徽章的最小 clip 宽度
    "min_clip_width_for_icon": 60,    # 显示类型图标的最小 clip 宽度
    "min_clip_width_for_indicator": 50,  # 显示指示器的最小 clip 宽度
}


# ============================================================================
# Convenience 函数 - 样式表生成
# ============================================================================

def _color(name: str) -> str:
    """获取颜色值，用于样式表字符串拼接。"""
    return EDIT_COLORS[name]


def _size(name: str) -> int:
    """获取尺寸值。"""
    return EDIT_SIZES[name]


def tool_button_style() -> str:
    """
    工具栏按钮样式。

    适用于：Edit 页面工具栏中的图标按钮。

    Returns:
        QString 样式表
    """
    return f"""
        QToolButton {{
            background: transparent;
            border: none;
            border-radius: 4px;
            padding: 4px;
            margin: 1px;
        }}

        QToolButton:hover {{
            background-color: {_color("bg_panel_alt")};
            border: 1px solid {_color("border_subtle")};
        }}

        QToolButton:pressed {{
            background-color: {_color("bg_input")};
        }}

        QToolButton:checked {{
            background-color: {_color("accent")};
            border: 1px solid {_color("accent_hover")};
        }}

        QToolButton:checked:hover {{
            background-color: {_color("accent_hover")};
        }}

        QToolButton:disabled {{
            background: transparent;
            opacity: 0.5;
        }}
    """


def panel_style() -> str:
    """
    面板容器样式。

    适用于：Edit 页面的各种面板容器（媒体池、属性面板等）。

    Returns:
        QString 样式表
    """
    return f"""
        QWidget {{
            background-color: {_color("bg_panel")};
            color: {_color("text")};
            border: none;
        }}

        QLabel {{
            background: transparent;
            color: {_color("text")};
            font-size: 12px;
        }}

        QLabel:disabled {{
            color: {_color("text_muted")};
        }}
    """


def tree_style() -> str:
    """
    树形列表样式 — DaVinci Resolve 紧凑风格。

    适用于：媒体池的树形视图、项目结构浏览等。

    Returns:
        QString 样式表
    """
    return f"""
        QTreeWidget, QTreeView {{
            background-color: {_color("bg_panel")};
            alternate-background-color: {_color("bg_panel_alt")};
            border: none;
            border-right: 1px solid {_color("border_subtle")};
            outline: none;
            font-size: 12px;
        }}

        QTreeWidget::item, QTreeView::item {{
            height: 22px;
            padding: 1px 4px;
            border: none;
        }}

        QTreeWidget::item:hover, QTreeView::item:hover {{
            background-color: rgba(255, 255, 255, 0.05);
        }}

        QTreeWidget::item:selected, QTreeView::item:selected {{
            background-color: rgba(226, 146, 39, 0.25);
            color: {_color("text")};
        }}

        QTreeWidget::item:selected:hover, QTreeView::item:selected:hover {{
            background-color: rgba(226, 146, 39, 0.35);
        }}

        QTreeWidget::branch, QTreeView::branch {{
            background: transparent;
        }}

        QTreeWidget::branch:has-children:!has-siblings:closed,
        QTreeWidget::branch:closed:has-children:has-siblings {{
            image: none;
            border-image: none;
        }}

        QTreeWidget::branch:open:has-children:!has-siblings,
        QTreeWidget::branch:open:has-children:has-siblings {{
            image: none;
            border-image: none;
        }}

        QHeaderView::section {{
            background-color: {_color("bg_toolbar")};
            color: {_color("text_muted")};
            padding: 4px 6px;
            border: none;
            border-bottom: 1px solid {_color("border_subtle")};
            font-size: 11px;
            font-weight: bold;
        }}

        QHeaderView::section:hover {{
            background-color: {_color("bg_panel_alt")};
        }}
    """


def input_style() -> str:
    """
    输入控件样式。

    适用于：文本输入框、数值输入框等。

    Returns:
        QString 样式表
    """
    return f"""
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
            background-color: {_color("bg_input")};
            border: 1px solid {_color("border_subtle")};
            border-radius: {_size("radius")}px;
            padding: 4px 8px;
            color: {_color("text")};
            font-size: 12px;
            min-height: 20px;
        }}

        QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
            border: 1px solid {_color("border")};
            background-color: {_color("bg_panel")};
        }}

        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
            border: 1px solid {_color("accent")};
            background-color: {_color("bg_panel")};
        }}

        QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
            background-color: {_color("bg_toolbar")};
            color: {_color("text_muted")};
            border: 1px solid transparent;
        }}

        /* 下拉组合框特有样式 */
        QComboBox::drop-down {{
            border: none;
            width: 20px;
            background: transparent;
        }}

        QComboBox::down-arrow {{
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid {_color("text_muted")};
            margin-right: 6px;
        }}

        QComboBox QAbstractItemView {{
            background-color: {_color("bg_panel")};
            border: 1px solid {_color("border")};
            border-radius: {_size("radius")}px;
            selection-background-color: {_color("accent")};
            selection-color: #ffffff;
            outline: none;
        }}

        /* 数值输入框隐藏上下按钮 */
        QSpinBox::up-button, QSpinBox::down-button,
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            width: 0px;
            height: 0px;
            border: none;
        }}
    """


def timeline_style() -> str:
    """
    时间线容器样式。

    适用于：时间线整体容器、时间轴标尺区域。

    Returns:
        QString 样式表
    """
    return f"""
        QWidget {{
            background-color: {_color("bg_app")};
            color: {_color("text")};
            border: none;
        }}

        /* 时间轴标尺 */
        QScrollBar:horizontal {{
            background-color: {_color("bg_toolbar")};
            height: 8px;
            border: none;
            margin: 0px;
        }}

        QScrollBar::handle:horizontal {{
            background-color: {_color("border")};
            min-width: 40px;
            border-radius: 4px;
            margin: 1px;
        }}

        QScrollBar::handle:horizontal:hover {{
            background-color: {_color("accent")};
        }}

        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}

        QScrollBar:vertical {{
            background-color: {_color("bg_toolbar")};
            width: 8px;
            border: none;
            margin: 0px;
        }}

        QScrollBar::handle:vertical {{
            background-color: {_color("border")};
            min-height: 40px;
            border-radius: 4px;
            margin: 1px;
        }}

        QScrollBar::handle:vertical:hover {{
            background-color: {_color("accent")};
        }}

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
    """


def track_header_style() -> str:
    """
    轨道头样式。

    适用于：时间线左侧轨道头区域（轨道名称、控制按钮）。

    Returns:
        QString 样式表
    """
    return f"""
        QWidget {{
            background-color: {_color("bg_toolbar")};
            border-right: 1px solid {_color("border_subtle")};
            border-bottom: 1px solid {_color("border_subtle")};
        }}

        QLabel {{
            background: transparent;
            color: {_color("text")};
            font-size: 11px;
            font-weight: bold;
            padding: 2px 4px;
        }}

        QToolButton {{
            background: transparent;
            border: none;
            border-radius: 3px;
            padding: 2px;
        }}

        QToolButton:hover {{
            background-color: {_color("bg_panel_alt")};
        }}

        QToolButton:pressed {{
            background-color: {_color("bg_input")};
        }}

        QToolButton:checked {{
            background-color: {_color("accent")};
        }}
    """


def clip_item_style(video: bool = True) -> str:
    """
    片段项样式。

    适用于：时间线上的视频/音频片段。

    Args:
        video: True 为视频片段样式，False 为音频片段样式。

    Returns:
        QString 样式表
    """
    clip_color = _color("video_clip") if video else _color("audio_clip")

    return f"""
        QWidget {{
            background-color: {clip_color};
            border: 1px solid {_color("border")};
            border-radius: {_size("radius")}px;
        }}

        QWidget:hover {{
            background-color: {_color("accent")};
            border: 1px solid {_color("accent_hover")};
        }}

        QWidget:selected {{
            background-color: {_color("accent")};
            border: 2px solid {_color("accent_hover")};
        }}

        QLabel {{
            background: transparent;
            color: {_color("text")};
            font-size: 10px;
            padding: 2px 4px;
        }}

        QLabel:disabled {{
            color: {_color("text_muted")};
        }}
    """


def button_style() -> str:
    """
    通用按钮样式。

    适用于：Edit 页面的普通按钮。

    Returns:
        QString 样式表
    """
    return f"""
        QPushButton {{
            background-color: {_color("bg_panel_alt")};
            border: 1px solid {_color("border")};
            border-radius: {_size("radius")}px;
            padding: 6px 16px;
            color: {_color("text")};
            font-size: 12px;
        }}

        QPushButton:hover {{
            background-color: {_color("border")};
            border: 1px solid {_color("text_muted")};
        }}

        QPushButton:pressed {{
            background-color: {_color("bg_input")};
        }}

        QPushButton:disabled {{
            background-color: {_color("bg_toolbar")};
            color: {_color("text_muted")};
            border: 1px solid {_color("border_subtle")};
        }}
    """


def accent_button_style() -> str:
    """
    强调按钮样式。

    适用于：主要操作按钮（使用橙色 accent）。

    Returns:
        QString 样式表
    """
    return f"""
        QPushButton {{
            background-color: {_color("accent")};
            border: none;
            border-radius: {_size("radius")}px;
            padding: 6px 16px;
            color: #ffffff;
            font-size: 12px;
            font-weight: bold;
        }}

        QPushButton:hover {{
            background-color: {_color("accent_hover")};
        }}

        QPushButton:pressed {{
            background-color: #b87024;
        }}

        QPushButton:disabled {{
            background-color: {_color("bg_panel_alt")};
            color: {_color("text_muted")};
        }}
    """


def danger_button_style() -> str:
    """
    危险按钮样式。

    适用于：删除、警告等危险操作按钮。

    Returns:
        QString 样式表
    """
    return f"""
        QPushButton {{
            background-color: transparent;
            border: 1px solid {_color("danger")};
            border-radius: {_size("radius")}px;
            padding: 6px 16px;
            color: {_color("danger")};
            font-size: 12px;
        }}

        QPushButton:hover {{
            background-color: {_color("danger")};
            color: #ffffff;
        }}

        QPushButton:pressed {{
            background-color: #b84d4d;
            color: #ffffff;
        }}

        QPushButton:disabled {{
            border: 1px solid {_color("border_subtle")};
            color: {_color("text_muted")};
        }}
    """


def toolbar_container_style() -> str:
    """
    工具栏容器样式。

    适用于：包含工具按钮的工具栏区域。

    Returns:
        QString 样式表
    """
    return f"""
        QWidget {{
            background-color: {_color("bg_toolbar")};
            border: none;
            border-bottom: 1px solid {_color("border_subtle")};
            min-height: {_size("toolbar_h")}px;
            max-height: {_size("toolbar_h")}px;
        }}

        QToolBar {{
            background-color: {_color("bg_toolbar")};
            border: none;
            spacing: 4px;
            padding: 2px 8px;
        }}
    """


def scroll_area_style() -> str:
    """
    滚动区域样式。

    适用于：需要滚动的面板区域。

    Returns:
        QString 样式表
    """
    return f"""
        QScrollArea {{
            background-color: {_color("bg_panel")};
            border: none;
        }}

        QScrollArea > QWidget > QWidget {{
            background-color: {_color("bg_panel")};
        }}

        QScrollBar:vertical {{
            background-color: {_color("bg_toolbar")};
            width: 10px;
            border: none;
            margin: 0px;
        }}

        QScrollBar::handle:vertical {{
            background-color: {_color("border")};
            min-height: 30px;
            border-radius: 5px;
            margin: 1px;
        }}

        QScrollBar::handle:vertical:hover {{
            background-color: {_color("accent")};
        }}

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}

        QScrollBar:horizontal {{
            background-color: {_color("bg_toolbar")};
            height: 10px;
            border: none;
            margin: 0px;
        }}

        QScrollBar::handle:horizontal {{
            background-color: {_color("border")};
            min-width: 30px;
            border-radius: 5px;
            margin: 1px;
        }}

        QScrollBar::handle:horizontal:hover {{
            background-color: {_color("accent")};
        }}

        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """


def checkbox_style() -> str:
    """
    复选框样式。

    Returns:
        QString 样式表
    """
    return f"""
        QCheckBox {{
            color: {_color("text")};
            spacing: 6px;
            font-size: 12px;
        }}

        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid {_color("border")};
            border-radius: 3px;
            background-color: {_color("bg_input")};
        }}

        QCheckBox::indicator:hover {{
            border: 1px solid {_color("text_muted")};
            background-color: {_color("bg_panel")};
        }}

        QCheckBox::indicator:checked {{
            background-color: {_color("accent")};
            border: 1px solid {_color("accent")};
        }}

        QCheckBox::indicator:checked:hover {{
            background-color: {_color("accent_hover")};
            border: 1px solid {_color("accent_hover")};
        }}

        QCheckBox:disabled {{
            color: {_color("text_muted")};
        }}

        QCheckBox::indicator:disabled {{
            background-color: {_color("bg_toolbar")};
            border: 1px solid {_color("border_subtle")};
        }}
    """


# ============================================================================
# 颜色值直接访问
# ============================================================================

def get_color(key: str) -> str:
    """
    获取颜色值。

    Args:
        key: 颜色键名（如 "bg_panel", "accent" 等）

    Returns:
        十六进制颜色字符串

    Raises:
        KeyError: 如果键名不存在
    """
    return EDIT_COLORS[key]


def get_size(key: str) -> int:
    """
    获取尺寸值。

    Args:
        key: 尺寸键名（如 "toolbar_h", "radius" 等）

    Returns:
        尺寸值（像素）

    Raises:
        KeyError: 如果键名不存在
    """
    return EDIT_SIZES[key]
