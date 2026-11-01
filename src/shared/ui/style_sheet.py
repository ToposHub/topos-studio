# -*- coding: utf-8 -*-
"""
Topos Color - 全局暗色主题样式表
===============================

包含全局暗色主题样式表和下拉组合框样式工具函数。
"""

from PyQt6.QtWidgets import QComboBox, QListView
from PyQt6.QtGui import QPalette, QColor
from pathlib import Path



_CHECKMARK_ICON_FILE = (
    Path(__file__).resolve().parents[1] / "assets" / "icons" / "checkmark.svg"
)
# QSS 的 url() 不接受 Windows 反斜杠路径（\U、\D 等会被当作转义序列，
# 导致整张样式表 "Could not parse stylesheet"），必须注入正斜杠形式。
# 注意：不能换成 Path.as_uri()——Qt6 QSS 解析器把带引号的 url("file:...")
# 当作相对路径（实测解析为 cwd + "file:/..."），图标永远加载不出来。
_CHECKMARK_ICON_PATH = _CHECKMARK_ICON_FILE.resolve().as_posix()


# ============================================================================
# 统一颜色常量 — 修改此处的颜色值即可全局生效
# ============================================================================

class C:
    """
    全局 UI 颜色常量集中定义。
    所有面板、控件的颜色均引用此类属性，修改此处即可统一调整配色。
    """

    # --- 主题橙色（高亮 / 激活 / 强调） ---
    ORANGE          = "#ff8800"
    ORANGE_HOVER    = "#ffaa44"
    ORANGE_PRESS    = "#cc6600"
    ORANGE_180      = "rgba(255, 136, 0, 180)"   # 带透明度版本
    # 选中标签的背景色；开关开启圆点和滑块手柄也统一使用此颜色。
    ACCENT_SURFACE  = ORANGE_PRESS

    # --- 深色背景层级（从最深到最浅） ---
    BG_DEEPEST      = "#121212"   # 画布 / 最深区域
    BG_DARKEST      = "#1a1a1a"   # 主窗口 / 分隔条
    BG_DARK         = "#1e1e1e"   # 面板标题 / 停靠窗口
    KEYFRAMES_BG    = "#1c1d20"   # Keyframes 左右工作区基础底色
    BG_PANEL        = "#27272b"   # 面板背景（标准）
    BG_INPUT        = "#252529"   # 输入框 / 下拉框背景
    BG_INPUT_DARK   = "#252525"   # 复选框背景
    BG_HOVER        = "#2a2a2a"   # 悬停背景 / 滑槽背景
    BG_ACTIVE       = "#2e2e33"   # 激活 / 悬停输入框
    BG_BUTTON       = "#3d3d3d"   # 按钮背景
    BG_BUTTON_HOVER = "#4d4d4d"   # 按钮悬停
    BG_BUTTON_PRESS = "#2d2d2d"   # 按钮按下

    # --- 边框 ---
    BORDER          = "#3a3a3a"   # 默认边框
    BORDER_HOVER    = "#4a4a4a"   # 悬停边框
    BORDER_DARK     = "#333333"   # 深色边框
    BORDER_SUBTLE   = "#24262a"   # 时间线分割线 / 细分隔线
    BORDER_MID      = "#555555"   # 中灰边框
    # Dock 面板之间的固定分割线。该颜色由项目统一控制，避免使用
    # Windows/macOS 原生 QMainWindow separator 的平台差异。
    PANEL_DIVIDER   = "#0d0d0f"

    # --- 文字颜色 ---
    TEXT_PRIMARY     = "#E0E0E0"  # 主文字
    TEXT_SECONDARY   = "#cccccc"  # 次要文字
    TEXT_HINT        = "#888888"  # 提示文字
    TEXT_DIM         = "#999999"  # 更暗的提示文字
    TEXT_LABEL       = "#aaaaaa"  # 标签文字
    TEXT_BRIGHT      = "#dddddd"  # 按钮文字

    # --- ToggleSwitch 专用 ---
    TOGGLE_PILL_BG  = "#3C3C42"  # 药丸背景
    TOGGLE_CIRCLE_ON  = ACCENT_SURFACE  # 开启圆点（与选中标签背景一致）
    TOGGLE_CIRCLE_OFF = "#666666"  # 关闭圆点（灰色）

    # --- 曲线 / 示波器专用 ---
    SCOPE_BG        = "#050505"   # 示波器背景
    CURVE_BTN_BG    = "rgba(30, 30, 30, 100)"       # 曲线按钮容器
    CURVE_BTN_BORDER = "rgba(255, 255, 255, 20)"     # 曲线按钮容器边框
    CHANNEL_M       = "#ffffff"
    CHANNEL_R       = "#ff4444"
    CHANNEL_G       = "#44ff44"
    CHANNEL_B       = "#4444ff"
    PICKER_CHECKED_BG    = "rgba(68, 255, 68, 60)"
    PICKER_CHECKED_BORDER = "#44ff44"

    # --- Composite Keyframes / Spline 面板专用 ---
    KEYFRAME_PLAYHEAD    = "#e53935"   # 播放头红色（不复用橙色）
    KEYFRAME_SELECTED    = "#ff8800"   # 选中关键帧高亮（= ORANGE）
    # 参数曲线通道色（多参数行区分）
    CURVE_HUE            = "#7ec8e3"   # 蓝
    CURVE_SAT            = "#8bc34a"   # 绿
    CURVE_ANGLE          = "#ffb74d"   # 黄
    CURVE_STRENGTH       = "#e57373"   # 红
    CURVE_DEFAULT        = "#bdbdbd"   # 默认灰

    # --- 音频混音器专用 ---
    MIXER_BG             = "#18181b"
    MIXER_STRIP_BG       = "#202024"
    MIXER_WELL_BG        = "#121214"
    MIXER_CYAN_ACCENT    = "#00d2c1"
    MIXER_BTN_BG         = "#26262a"
    MIXER_BTN_BORDER     = "#34343a"
    MIXER_MUTE_CHECKED   = "#c62828"
    MIXER_MUTE_BORDER    = "#e53935"
    MIXER_SOLO_CHECKED   = "#2e7d32"
    MIXER_SOLO_BORDER    = "#43a047"
    MIXER_MONITOR_CHECKED = "#1565c0"
    MIXER_MONITOR_BORDER  = "#1e88e5"


# ============================================================================
# 可复用 QSS 模板 — 供各控件直接引用，避免硬编码
# ============================================================================

# --- 参数标签（DragValue 左侧的灰色文字） ---
DRAG_VALUE_LABEL_STYLE = f"""
    QLabel {{
        color: {C.TEXT_DIM}; font-size: 11px;
    }}
"""

DRAG_VALUE_NORMAL_STYLE = f"""
    QLabel {{
        color: {C.TEXT_BRIGHT}; font-size: 11px; font-weight: bold;
        background: #232325; border: 1px solid #151517;
        border-radius: 3px; padding: 0px 3px;
    }}
    QLabel:hover {{ border: 1px solid {C.BORDER_HOVER}; background: {C.BG_ACTIVE}; }}
"""

DRAG_VALUE_ACTIVE_STYLE = f"""
    QLabel {{
        color: #ffffff; font-size: 11px; font-weight: bold;
        background: #3a3a40; border: 1px solid {C.ORANGE};
        border-radius: 3px; padding: 0px 3px;
    }}
"""

# --- 参数标签 / 数值格（禁用态，达芬奇式整列变灰） ---
PARAM_LABEL_DISABLED_STYLE = "color: #555555; font-size: 11px;"

DRAG_VALUE_DISABLED_STYLE = f"""
    QLabel {{
        color: {C.TEXT_DIM}; font-size: 11px; font-weight: bold;
        background: #1c1c1e; border: 1px solid #1a1a1c;
        border-radius: 3px; padding: 0px 3px;
    }}
"""

# --- 曲线工具栏按钮容器 ---
CURVE_BTN_CONTAINER_STYLE = f"""
    QWidget {{
        background-color: {C.CURVE_BTN_BG};
        border-radius: 6px;
        border: 1px solid {C.CURVE_BTN_BORDER};
    }}
"""

# --- 曲线通道按钮基础样式（channel_color 需调用时 f-string 替换） ---
def curve_channel_btn_style(channel_color: str) -> str:
    return f"""
        QPushButton {{
            background-color: transparent;
            color: {channel_color};
            border: 1px solid transparent;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            text-align: center;
            padding: 0px;
        }}
        QPushButton:checked {{
            background-color: rgba(255, 255, 255, 30);
            border: 1px solid {channel_color};
        }}
        QPushButton:hover {{
            background-color: rgba(255, 255, 255, 20);
            border: 1px solid {C.BORDER_MID};
        }}
    """

# --- 吸管/拾色器按钮样式 ---
CURVE_PICKER_BTN_STYLE = f"""
    QPushButton {{
        background-color: transparent;
        color: #ffffff;
        border: 1px solid transparent;
        border-radius: 4px;
        font-size: 14px;
        text-align: center;
        padding: 0px;
    }}
    QPushButton:hover {{
        background-color: rgba(255, 255, 255, 20);
        border: 1px solid {C.BORDER_MID};
    }}
    QPushButton:pressed {{
        background-color: rgba(255, 255, 255, 40);
    }}
    QPushButton:checked {{
        background-color: {C.PICKER_CHECKED_BG};
        border: 1px solid {C.PICKER_CHECKED_BORDER};
        color: {C.CHANNEL_G};
    }}
    QPushButton:checked:hover {{
        background-color: rgba(68, 255, 68, 80);
    }}
"""

# --- 曲线重置按钮样式 ---
CURVE_RESET_BTN_STYLE = f"""
    QPushButton {{
        background-color: transparent;
        color: #ffffff;
        border: 1px solid transparent;
        border-radius: 4px;
        font-size: 16px;
        font-weight: bold;
        text-align: center;
        padding: 0px;
    }}
    QPushButton:hover {{
        background-color: rgba(255, 255, 255, 20);
        border: 1px solid {C.BORDER_MID};
        color: {C.CHANNEL_G};
    }}
    QPushButton:pressed {{
        background-color: rgba(255, 255, 255, 40);
    }}
"""

# --- HSL 曲线模式下拉框样式 ---
HSL_MODE_COMBO_STYLE = f"""
    QComboBox {{
        background-color: {C.BG_INPUT};
        color: #ffffff;
        border: 1px solid {C.BORDER_MID};
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 11px;
        min-width: 90px;
    }}
    QComboBox:hover {{
        border: 1px solid {C.BORDER_MID};
        background-color: rgba(55, 55, 58, 200);
    }}
    QComboBox::drop-down {{
        border: none;
        width: 16px;
        background: transparent;
        subcontrol-origin: padding;
        subcontrol-position: right;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {C.TEXT_LABEL};
        margin-right: 4px;
    }}
"""

# --- HSL 曲线信息标签样式 ---
HSL_INFO_LABEL_STYLE = f"""
    QLabel {{
        background-color: {C.CURVE_BTN_BG};
        color: {C.CHANNEL_G};
        border-radius: 4px;
        padding: 2px 6px;
        font-size: 11px;
        font-weight: bold;
    }}
"""

# --- LUT 画廊工具栏标签样式 ---
LUT_TOOLBAR_LABEL_STYLE = f"font-size: 11px; color: {C.TEXT_LABEL};"

# --- LUT 画廊列表控件样式 ---
LUT_LIST_STYLE = f"""
    QListWidget {{
        background-color: {C.BG_PANEL};
        border: none;
    }}
    QListWidget::item {{
        color: {C.TEXT_LABEL};
        background-color: #222;
        border-radius: 4px;
        padding: 5px;
    }}
    QListWidget::item:selected {{
        background-color: {C.BORDER_DARK};
        border: 1px solid {C.ORANGE_PRESS};
    }}
"""

# --- 音频混音器轨道按钮样式 ---
MIXER_TRACK_BTN_STYLE = f"""
    QToolButton {{
        background: {C.MIXER_BTN_BG};
        border: 1px solid {C.MIXER_BTN_BORDER};
        border-radius: 2px;
        color: #9999a0;
        font-size: 9px;
        font-weight: bold;
    }}
    QToolButton:hover {{
        background: #32323a;
        border: 1px solid #484852;
        color: #ffffff;
    }}
    QToolButton:checked {{
        background: {C.BG_BUTTON_HOVER};
        border: 1px solid {C.BORDER_MID};
        color: #ffffff;
    }}
    #mute_btn:checked {{
        background: {C.MIXER_MUTE_CHECKED};
        border: 1px solid {C.MIXER_MUTE_BORDER};
        color: white;
    }}
    #solo_btn:checked {{
        background: {C.MIXER_SOLO_CHECKED};
        border: 1px solid {C.MIXER_SOLO_BORDER};
        color: white;
    }}
    #monitor_btn:checked {{
        background: {C.MIXER_MONITOR_CHECKED};
        border: 1px solid {C.MIXER_MONITOR_BORDER};
        color: white;
    }}
"""

# --- 音频混音器 SpinBox 样式（极简无边框） ---
MIXER_SPIN_STYLE = """
    QDoubleSpinBox, QSpinBox {
        background-color: transparent;
        border: none;
        color: #d0d0d8;
        font-size: 10px;
        font-weight: bold;
    }
    QDoubleSpinBox:hover, QSpinBox:hover {
        color: #ffffff;
    }
    QDoubleSpinBox:focus, QSpinBox:focus {
        background-color: #121216;
        border: 1px solid #33333d;
        border-radius: 2px;
        color: #ffffff;
    }
"""

# ============================================================================
# 通用面板组件模板 — 供各功能面板直接引用
# ============================================================================

# --- 面板标题标签（如 "EFFECT CONTROLS", "软裁切" 等） ---
PANEL_TITLE_LABEL_STYLE = f"color: {C.TEXT_HINT}; font-weight: bold; font-size: 10px; letter-spacing: 1px;"

# --- 参数标签（滑动条左侧的灰色文字） ---
PARAM_LABEL_STYLE = "color: #bbbbbb; font-size: 11px;"

# --- Enable 标签（Enabled / Processing 等） ---
ENABLE_LABEL_STYLE = f"color: {C.TEXT_BRIGHT}; font-size: 10px; font-weight: bold;"

# --- 透明背景容器 ---
TRANSPARENT_STYLE = "background: transparent;"

# --- 分隔线 ---
SEPARATOR_LINE_STYLE = f"background-color: {C.BORDER_DARK};"

# --- 透明滚动区域 ---
SCROLL_AREA_TRANSPARENT_STYLE = "QScrollArea { background: transparent; border: none; }"

# --- 紧凑 SpinBox（浮点输入框） ---
SPINBOX_COMPACT_STYLE = f"""
    QDoubleSpinBox {{
        background-color: {C.BG_PANEL};
        border: 1px solid {C.BORDER};
        border-radius: 4px;
        padding: 2px 6px;
        color: #eee;
        font-size: 10px;
    }}
    QDoubleSpinBox:hover {{
        border: 1px solid {C.BORDER_HOVER};
        background-color: {C.BG_ACTIVE};
    }}
"""

# --- 空状态提示标签 ---
EMPTY_STATE_LABEL_STYLE = f"""
    QLabel {{
        color: {C.BORDER_MID};
        font-size: 11px;
        padding: 20px;
    }}
"""

# ============================================================================
# Composite 节点编辑器模板
# ============================================================================

# --- NodeSectionWidget 外框 ---
NODE_SECTION_STYLE = f"""
    NodeSectionWidget {{
        border: 1px solid {C.BORDER_DARK};
        border-radius: 4px;
        background-color: {C.BG_PANEL};
    }}
"""

# --- 节点折叠按钮 ---
NODE_COLLAPSE_BTN_STYLE = f"""
    QToolButton {{ background: transparent; border: none; color: #8E929A; font-size: 9px; padding: 0; }}
    QToolButton:hover {{ color: {C.TEXT_PRIMARY}; }}
"""

# --- 节点标题文字 ---
NODE_TITLE_LABEL_STYLE = f"color: {C.TEXT_PRIMARY}; font-weight: bold; font-size: 11px; border: none;"

# --- 节点内容区 ---
NODE_CONTENT_STYLE = f"background-color: {C.BG_PANEL}; border: none;"

# --- Settings section（Project / 3D Scene Settings）---
# 这两个 section 不是节点（无 node_id / node_color），与 NodeSectionWidget 共存于
# 同一面板。为了让它们的标题栏与节点 header 视觉统一，复用相同的渐变质感和
# 底部分隔线，但用中性灰（不带节点色）以资区分。文字固定白色：中性灰背景恒为
# 暗色，无需像节点那样按 luminance 自适应黑/白。
SETTINGS_SECTION_FRAME_STYLE = (
    f"border: 1px solid {C.BORDER_DARK}; border-radius: 4px;"
    f" background-color: {C.BG_PANEL};"
)
SETTINGS_SECTION_HEADER_STYLE = (
    f"background-color: qlineargradient("
    f"x1:0, y1:0, x2:0, y2:1, stop:0 {C.BG_ACTIVE}, stop:1 {C.BG_PANEL});"
    f" border-bottom: 1px solid {C.BORDER_DARK};"
)
SETTINGS_SECTION_TITLE_STYLE = (
    f"color: {C.TEXT_PRIMARY}; font-weight: bold; font-size: 11px; border: none;"
)
SETTINGS_SECTION_COLLAPSE_BTN_STYLE = (
    f"QToolButton {{ background: transparent; border: none;"
    f" color: {C.TEXT_SECONDARY}; font-size: 9px; padding: 0; }}"
    f"QToolButton:hover {{ color: {C.TEXT_PRIMARY}; }}"
)

# --- Properties 头部栏 ---
PROPS_HEADER_STYLE = f"background-color: {C.BG_PANEL}; border-bottom: 1px solid {C.BG_HOVER};"

# --- Properties 标题文字 ---
PROPS_TITLE_LABEL_STYLE = "color: #8E929A; font-weight: bold; font-size: 11px; border: none;"

# --- 工具栏按钮（通用扁平风格） ---
TOOLBAR_FLAT_BTN_STYLE = """
    QToolButton {
        background: transparent;
        border: none;
        border-radius: 4px;
        padding: 3px;
        margin: 1px;
    }
    QToolButton:hover {
        background-color: rgba(255, 255, 255, 18);
    }
    QToolButton:pressed {
        background-color: rgba(255, 255, 255, 10);
    }
    QToolButton:checked {
        background: transparent;
    }
    QToolButton:checked:hover {
        background-color: rgba(255, 255, 255, 18);
    }
"""

# ============================================================================
# Edit Properties 面板模板
# ============================================================================
#
# Edit 的 inspector 和 Composite 节点属性面板有不同的信息密度需求：前者通常
# 同时展示多个剪辑分组，因此使用无卡片、无底色的平整标题行，仅以细分隔线组织
# 内容；这与达芬奇的 Inspector 信息层级一致。

EDIT_PROPERTIES_CONTENT_STYLE = f"""
    QWidget#editPropertiesContent {{
        background-color: {C.BG_PANEL};
    }}
"""

EDIT_PROPERTIES_SECTION_STYLE = """
    QWidget#editPropertiesSection {
        background: transparent;
        border: none;
    }
"""

EDIT_PROPERTIES_SECTION_HEADER_STYLE = """
    QWidget#editPropertiesSectionHeader {
        background: transparent;
        border: none;
    }
"""

EDIT_PROPERTIES_SECTION_DIVIDER_STYLE = (
    f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
    f"stop:0 {C.BG_DARKEST}, stop:0.5 {C.BG_DARKEST}, "
    "stop:0.51 rgba(26, 26, 26, 128), stop:1 rgba(26, 26, 26, 128)); "
    "border: none;"
)

EDIT_PROPERTIES_SECTION_CONTENT_STYLE = """
    QWidget#editPropertiesSectionContent {
        background: transparent;
        border: none;
    }
    /* The global QWidget rule supplies a panel background.  Clear it again
       for inspector layout primitives so labels and slider shells do not
       turn into disconnected grey rectangles.  Actual input cells retain
       their own component styles. */
    QWidget#editPropertiesSectionContent QLabel,
    QWidget#editPropertiesSectionContent QCheckBox,
    QWidget#editPropertiesSectionContent DragValue {
        background: transparent;
    }
"""

EDIT_PROPERTIES_ROW_STYLE = """
    QWidget#editPropertiesRow {
        background: transparent;
        border: none;
    }
"""

EDIT_PROPERTIES_SECTION_TITLE_STYLE = (
    f"color: {C.TEXT_PRIMARY}; font-size: 11px; font-weight: bold; border: none;"
)

EDIT_PROPERTIES_TOOL_BTN_STYLE = f"""
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 3px;
        padding: 2px;
    }}
    QToolButton:hover {{
        background-color: rgba(255, 255, 255, 18);
        border-color: {C.BORDER_HOVER};
    }}
    QToolButton:pressed {{
        background-color: rgba(0, 0, 0, 32);
    }}
    QToolButton:checked {{
        background-color: rgba(255, 136, 0, 22);
        border-color: rgba(255, 136, 0, 110);
    }}
"""

# --- Composite 工具栏背景 ---
# 与剪辑页面时间线顶部工具栏背景保持一致（#27272b）
COMPOSITE_TOOLBAR_STYLE = f"background-color: {C.BG_PANEL}; border-bottom: 1px solid {C.BORDER};"

# --- Composite 工具栏标题 ---
COMPOSITE_TOOLBAR_TITLE_STYLE = f"color: {C.TEXT_LABEL}; font-weight: bold; font-size: 11px;"

# --- 添加节点按钮 ---
# 无背景框，仅保留名称与加号，悬停时给一点淡色提示
COMPOSITE_ADD_NODE_BTN_STYLE = f"""
    QPushButton {{
        background: transparent; color: {C.TEXT_SECONDARY}; border: none;
        padding: 2px 6px; font-size: 11px;
    }}
    QPushButton:hover {{ color: {C.TEXT_BRIGHT}; }}
    QPushButton:pressed {{ color: {C.TEXT_HINT}; }}
"""

# --- 设置按钮（⋯ 弹出菜单） ---
# 与节点快捷按钮同风格：无背景框，悬停时文字变橙
COMPOSITE_SETTINGS_BTN_STYLE = f"""
    QToolButton {{
        border: none; background: transparent;
        color: {C.TEXT_SECONDARY}; padding: 0px 4px; font-size: 13px;
    }}
    QToolButton:hover {{ color: {C.ORANGE}; }}
    QToolButton:pressed {{ color: {C.ORANGE_PRESS}; }}
"""

# --- Zoom 标签 ---
ZOOM_LABEL_STYLE = f"color: {C.TEXT_HINT}; font-size: 10px; margin-left: 8px;"

# --- 节点分组框 ---
NODE_GROUP_BOX_STYLE = f"""
    QGroupBox {{ border: 1px solid {C.BORDER_MID}; border-radius: 3px; margin-top: 6px;
     padding-top: 10px; font-weight: bold; font-size: 10px; color: {C.TEXT_BRIGHT}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 6px; padding: 0 3px; }}
"""

# ============================================================================
# 对话框通用模板
# ============================================================================

# --- 通用暗色对话框 ---
DIALOG_BASE_STYLE = f"""
    QDialog {{ background-color: {C.BG_PANEL}; }}
    QLabel {{ color: {C.TEXT_SECONDARY}; }}
    QLineEdit {{
        background-color: {C.BG_HOVER}; color: #eee;
        border: 1px solid {C.BORDER_MID}; padding: 4px;
    }}
    QTextEdit {{
        background-color: {C.BG_HOVER}; color: #eee;
        border: 1px solid {C.BORDER_MID}; font-size: 12px;
    }}
    QPushButton {{
        background-color: {C.BG_BUTTON_HOVER}; color: {C.TEXT_SECONDARY};
        border: 1px solid {C.BORDER_MID}; padding: 6px 12px;
        min-width: 80px;
    }}
    QPushButton:hover {{ background-color: #5a5a5a; }}
    QSlider::groove:horizontal {{
        background: {C.BG_HOVER}; height: 6px; border: 1px solid {C.BORDER_MID};
    }}
    QSlider::handle:horizontal {{
        background: #ddd; width: 14px; margin: -5px 0; border-radius: 7px;
    }}
    QSlider::sub-page:horizontal {{ background: #6a6a6a; }}
"""

# --- 保存预设对话框 ---
PRESET_DIALOG_STYLE = f"""
    QDialog {{ background-color: {C.BG_PANEL}; color: {C.TEXT_SECONDARY}; }}
    QLabel {{ color: {C.TEXT_SECONDARY}; }}
    QLineEdit {{ background-color: {C.BG_HOVER}; color: {C.TEXT_SECONDARY}; border: 1px solid {C.BORDER_MID}; padding: 4px; }}
    QTextEdit {{ background-color: {C.BG_HOVER}; color: {C.TEXT_SECONDARY}; border: 1px solid {C.BORDER_MID}; padding: 4px; }}
    QPushButton {{ background-color: {C.ORANGE_PRESS}; color: white; padding: 6px 12px; border-radius: 3px; }}
    QPushButton:hover {{ background-color: {C.ORANGE_HOVER}; }}
"""

# --- 管理预设对话框 ---
MANAGE_PRESET_DIALOG_STYLE = f"""
    QDialog {{ background-color: {C.BG_PANEL}; color: {C.TEXT_SECONDARY}; }}
    QLabel {{ color: {C.TEXT_SECONDARY}; }}
    QListWidget {{ background-color: {C.BG_HOVER}; color: {C.TEXT_SECONDARY}; border: 1px solid {C.BORDER_MID}; }}
    QPushButton {{ background-color: {C.ORANGE_PRESS}; color: white; padding: 6px 12px; border-radius: 3px; }}
    QPushButton:hover {{ background-color: {C.ORANGE_HOVER}; }}
    QPushButton:disabled {{ background-color: {C.BORDER_MID}; color: {C.TEXT_HINT}; }}
"""

# --- 上下文菜单覆盖样式 ---
CONTEXT_MENU_STYLE = f"""
    QMenu {{
        background-color: {C.BORDER}; color: {C.TEXT_SECONDARY}; border: 1px solid {C.BORDER_MID};
        padding: 4px; font-size: 12px;
    }}
    QMenu::item:selected {{ background-color: {C.ORANGE_PRESS}; }}
"""

# ============================================================================
# Script Panel 模板
# ============================================================================

# --- 脚本工具栏按钮 ---
SCRIPT_TOOLBAR_BTN_STYLE = (
    f"QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 2px; }}"
    f"QPushButton:hover {{ background: {C.BG_BUTTON}; border-color: {C.BORDER_MID}; }}"
    f"QPushButton:pressed {{ background: {C.BG_BUTTON_HOVER}; }}"
)

# --- Run 按钮 ---
SCRIPT_RUN_BTN_STYLE = (
    f"QPushButton {{ background-color: #2d5a2d; color: #4CAF50; font-weight: bold; border-radius: 3px; }}"
    f"QPushButton:hover {{ background-color: #3a7a3a; }}"
    f"QPushButton:disabled {{ background-color: {C.BORDER_DARK}; color: {C.TEXT_HINT}; }}"
)

# --- Stop 按钮 ---
SCRIPT_STOP_BTN_STYLE = (
    f"QPushButton {{ background-color: #5a2d2d; color: #f44336; font-weight: bold; border-radius: 3px; }}"
    f"QPushButton:hover {{ background-color: #7a3a3a; }}"
    f"QPushButton:disabled {{ background-color: {C.BORDER_DARK}; color: {C.TEXT_HINT}; }}"
)

# --- 编辑器样式 ---
SCRIPT_EDITOR_STYLE = f"QPlainTextEdit {{ background-color: {C.BG_DARK}; color: #d4d4d4; border: none; }}"

# --- 控制台样式 ---
SCRIPT_CONSOLE_STYLE = f"QPlainTextEdit {{ background-color: {C.BG_DARKEST}; color: {C.TEXT_SECONDARY}; border: none; }}"

# --- 状态标签 ---
SCRIPT_STATUS_STYLE = f"QLabel {{ color: {C.TEXT_HINT}; font-size: 11px; padding: 0 4px; }}"

# --- Console 标签 ---
CONSOLE_LABEL_STYLE = f"color: {C.TEXT_HINT}; font-size: 10px;"

# --- Console 清除按钮 ---
CONSOLE_CLEAR_BTN_STYLE = (
    f"QPushButton {{ border: 1px solid transparent; border-radius: 2px; background: transparent; }}"
    f"QPushButton:hover {{ background: {C.BG_BUTTON}; border-color: {C.BORDER_MID}; }}"
)

# --- 补全弹出列表 ---
COMPLETER_POPUP_STYLE = (
    f"QListView {{ background-color: #2b2b2b; color: #ddd; border: 1px solid {C.BORDER_MID}; }}"
    f"QListView::item {{ padding: 2px 6px; }}"
    f"QListView::item:selected {{ background-color: #3c6f3c; }}"
)

# ============================================================================
# Library Panel 模板
# ============================================================================

LIBRARY_PANEL_STYLE = f"""
    QWidget {{ background: {C.BG_PANEL}; color: {C.TEXT_SECONDARY}; font-size: 11px; }}
    QFrame#Header {{ background: #2f2f33; border-bottom: 1px solid #222; }}
    QFrame#Footer {{ background: #2f2f33; border-top: 1px solid #222; }}
    
    QLineEdit {{ 
        background: {C.BG_HOVER}; border-radius: 4px; padding: 4px 10px; color: #eee; border: 1px solid {C.BORDER_MID};
        font-size: 11px;
    }}
    QLineEdit:focus {{ border: 1px solid {C.ORANGE}; }}
    
    QToolButton {{ 
        background: transparent; border: None; border-radius: 4px; padding: 4px; color: {C.TEXT_HINT};
    }}
    QToolButton:hover {{ background: {C.BORDER_MID}; color: #eee; }}
    
    QComboBox {{
        background: {C.BG_INPUT}; border: 1px solid {C.BORDER_MID}; border-radius: 4px; padding: 2px 8px; font-size: 11px;
        color: {C.TEXT_LABEL};
    }}
    QComboBox:hover {{ color: #eee; border-color: {C.ORANGE}; }}
    QComboBox::drop-down {{ border: none; background: transparent; subcontrol-origin: padding; subcontrol-position: right; }}

    QLabel {{ background: transparent; }}
    
    QSlider {{ background: transparent; }}
    QSlider::groove:horizontal {{
        border: none; height: 4px; background: #222; margin: 2px 0; border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {C.ORANGE_PRESS}; border: none; width: 10px; height: 10px; margin: -4px 0; border-radius: 5px;
    }}
    QSlider::handle:horizontal:hover {{ background: {C.ORANGE_HOVER}; }}
"""

LIBRARY_LIST_VIEW_STYLE = f"QListView {{ background: {C.BG_PANEL}; border: none; padding: 10px; }}"

LIBRARY_MODE_BTN_STYLE = f"""
    QToolButton {{ padding: 4px; border-radius: 4px; background: {C.BG_HOVER}; border: 1px solid {C.BORDER_MID}; }}
    QToolButton:hover {{ background: {C.BORDER_DARK}; }}
    QToolButton:checked {{ background: {C.ORANGE}; border-color: {C.ORANGE}; color: white; }}
"""

LIBRARY_ZOOM_LABEL_STYLE = f"color: {C.TEXT_HINT}; font-size: 10px; font-weight: bold;"

# ============================================================================
# Plugin Panel 模板
# ============================================================================

PLUGIN_TITLE_STYLE = f"""
    QLabel {{
        font-weight: bold;
        font-size: 10px;
        color: {C.TEXT_HINT};
        letter-spacing: 1px;
    }}
"""

AI_EFFECT_GROUP_STYLE = """
    QGroupBox {
        border: 1px solid #224488;
        border-radius: 6px;
        margin-top: 10px;
        font-weight: bold;
        font-size: 9px;
        color: #66aaff;
        padding-top: 10px;
        background-color: #1a1a2e;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 5px;
    }
"""

AI_PROCESS_BTN_STYLE = """
    QPushButton#ai_process_btn {
        background-color: #2255aa;
        border: 1px solid #3366cc;
        border-radius: 4px;
        padding: 4px 12px;
        color: #ffffff;
        font-size: 11px;
        font-weight: bold;
    }
    QPushButton#ai_process_btn:hover {
        background-color: #2b66bb;
        border: 1px solid #4488dd;
    }
    QPushButton#ai_process_btn:pressed {
        background-color: #1a4488;
    }
"""

ERROR_GROUP_STYLE = """
    QGroupBox {
        border: 1px solid #662222;
        background-color: #331111;
        color: #ff6666;
    }
"""

ERROR_LABEL_STYLE = "color: #ff6666; font-size: 11px;"

SCALE_LABEL_STYLE = "color: #5588bb; font-size: 9px;"

# ============================================================================
# Shortcut Dialog 模板
# ============================================================================

SHORTCUT_EDITOR_STYLE = f"""
    QKeySequenceEdit {{
        background-color: {C.BG_INPUT_DARK};
        border: 2px solid {C.ORANGE};
        border-radius: 3px;
        padding: 4px;
        color: {C.TEXT_PRIMARY};
        font-size: 13px;
    }}
"""


# --- 渐变滑块手柄样式（供 ToposSlider._apply_gradient_style 使用） ---
def gradient_slider_groove_style(gradient_qss: str) -> str:
    """为渐变滑槽生成完整 QSS（保留手柄样式统一）。"""
    return f"""
        QSlider::groove:horizontal {{
            border: 1px solid #444;
            height: 4px;
            background: {gradient_qss};
            margin: 2px 0;
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {C.ACCENT_SURFACE};
            border: 2px solid {C.BG_DARK};
            width: 10px;
            height: 10px;
            margin: -4px 0;
            border-radius: 5px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {C.ORANGE_HOVER};
            border: 2px solid {C.BORDER_DARK};
        }}
    """


# ============================================================================
# 节点属性面板 选项下拉框样式
# ============================================================================

# 用于 Composite 节点属性面板的 option 类型参数下拉框。
# 设计参考：深色背景、无边框、圆角、左侧白色文字 + 右侧 chevron 箭头。
NODE_OPTION_COMBO_STYLE = """
    QComboBox {
        background-color: #2d2d30;
        border: none;
        border-radius: 8px;
        padding: 6px 14px;
        padding-right: 34px;
        color: #ffffff;
        font-size: 12px;
        min-height: 26px;
    }
    QComboBox:hover {
        background-color: #38383c;
    }
    QComboBox:on {
        background-color: #3a3a3e;
    }
    QComboBox::drop-down {
        border: none;
        width: 28px;
    }
    QComboBox::down-arrow {
        image: none;
        border-left: 8px solid transparent;
        border-right: 8px solid transparent;
        border-top: 8px solid #b0b0b0;
        margin-right: 6px;
    }
    QComboBox QAbstractItemView {
        background-color: rgba(39, 39, 43, 245);
        border: 1px solid rgba(80, 80, 80, 200);
        border-radius: 8px;
        selection-background-color: rgba(255, 136, 0, 180);
        selection-color: #ffffff;
        outline: none;
        padding: 4px 0px;
    }
"""


# ============================================================================
# 下拉组合框弹出列表样式
# ============================================================================

# 用于 QComboBox 弹出列表的 QListView stylesheet
# 注意：Fusion 模式下 CSS 的 vertical padding 不会撑大 item 绘制区域，
# 只偏移文字位置，导致 QPalette 绘制的高亮矩形与文字区域不匹配。
# 所以这里只用水平 padding，垂直间距由 QListView.setSpacing() 控制。
COMBO_DROPDOWN_VIEW_STYLE = """
QListView {
    background-color: rgba(39, 39, 43, 240);
    border: 1px solid rgba(80, 80, 80, 200);
    border-radius: 4px;
    outline: none;
    font-size: 12px;
}

QListView::item {
    padding: 0px 12px;
    color: #cccccc;
}
"""


def apply_combo_dropdown_style(combo: QComboBox) -> None:
    """
    为 QComboBox 应用与 QMenu 下拉菜单一致的弹出列表样式。

    在 macOS 上，QComboBox 默认使用 NSPopUpButton 原生控件渲染弹出列表，
    完全无视 Qt stylesheet。此函数通过以下步骤强制使用 Qt 自绘引擎：

    1. combo.setView(QListView()) — 显式创建 QListView（避免懒加载返回 None）
    2. view.setPalette(...) — 通过 QPalette 设置文字颜色和选中颜色
       （QPalette 是 item delegate 渲染时真正读取的颜色源，stylesheet 不可靠）
    3. view.setSpacing(...) — 通过 QListView API 控制 item 之间的间距

    用法：
        combo = QComboBox()
        apply_combo_dropdown_style(combo)
    """
    # Do not install a per-widget QStyle here.  Qt/PyQt style ownership is not
    # safe across a deferred widget teardown on macOS and was the source of
    # QComboBox native crashes.  ChevronComboBox draws its indicator itself;
    # the application stylesheet remains the single visual authority.

    # 步骤 1：显式创建 QListView，避免懒加载返回 None
    view = QListView()
    combo.setView(view)
    # ``setView`` transfers the native ownership to QComboBox, while retaining
    # the wrapper prevents premature Python-side disposal during polish.
    combo._topos_combo_view = view  # type: ignore[attr-defined]

    # 步骤 2：通过 QPalette 设置颜色（最可靠的方式）
    palette = view.palette()
    # 正常文字颜色
    palette.setColor(QPalette.ColorRole.Text, QColor("#cccccc"))
    # 选中项文字颜色
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    # 选中项背景颜色（橙色）
    palette.setColor(QPalette.ColorRole.Highlight, QColor(255, 136, 0, 180))
    view.setPalette(palette)

    # 步骤 3：设置样式和间距
    view.setStyleSheet(COMBO_DROPDOWN_VIEW_STYLE)
    view.setSpacing(2)  # item 之间的垂直间距


# ============================================================================
# 主主题样式表
# ============================================================================

"""
Topos Color - 全局暗色主题样式表
===============================

本文件定义了应用程序的全局 UI 样式。通过修改此文件，您可以统一调整：
- 字体、颜色、大小
- 面板背景色
- 滑动条样式（颜色、大小）
- 按钮样式
- 输入框样式
- 复选框样式
- 分组框样式

=== 颜色参考 ===
主题橙色（高亮/激活）：#ff8800
主题橙色悬停：#ffaa44
主题橙色按下：#cc6600

深色背景层级：
- 最深（主窗口/分隔条）：#1a1a1a
- 次深（面板/停靠窗口标题）：#1e1e1e
- 面板背景：#27272b
- 输入框背景：#252525
- 滑槽背景：#2a2a2a
- 按钮背景：#3d3d3d
- 边框颜色：#3a3a3a ~ #555555

文字颜色：
- 主文字：#E0E0E0
- 次要文字：#cccccc
- 提示文字：#888888
"""

# ============================================================================
# 主主题样式表
# ============================================================================
DARK_THEME = """
/* ============================================
   Topos Color - 全局暗色主题样式表
   ============================================ */

/* === 全局重置与基础样式 === */
* {
    /* 字体设置：系统默认无衬线字体 */
    font-family: "PingFang SC", "Helvetica Neue", "Segoe UI", Arial, Helvetica, sans-serif;
    /* 全局默认字号 */
    font-size: 13px;
}

/* === 主窗口背景 === */
QMainWindow {
    background-color: #1a1a1a;  /* 最深的背景色 */
}

/* === 基础组件 - 统一面板背景 === */
QWidget {
    background-color: #27272b;  /* 面板背景色（比主窗口稍浅） */
    color: #E0E0E0;             /* 主文字颜色 */
}

/* === 面板专属背景强化 === */
/* 确保所有面板使用统一的背景色 */
QDockWidget, QDockWidget > QWidget,
AdjustmentPanel, GradingPanel, RawPanel, MixerPanel,
QualifierPanel, BlurSharpenPanel, CurvesPanel,
FilmGrainPanel, LutPanel, DenoisePanel, PluginPanel {
    background-color: #27272b;  /* 面板背景色 */
}

/* === 停靠窗口 === */
QDockWidget {
    titlebar-close-icon: none;   /* 隐藏默认关闭图标 */
    titlebar-normal-icon: none;  /* 隐藏默认浮动图标 */
    /* 禁用平台原生边框；边界由 PanelDockWidget 的自定义覆盖层绘制。 */
    border: none;
}

QDockWidget::title {
    background-color: transparent;  /* 透明背景 */
    padding: 0px;                   /* 无内边距 */
    margin: 0px;                    /* 无外边距 */
    color: transparent;             /* 透明文字 */
    height: 0px;                    /* 高度为0，通过setTitleBarWidget控制显示 */
    min-height: 0px;
    max-height: 0px;
}

/* === 菜单栏 === */
QMenuBar {
    background-color: #27272b;   /* 菜单栏背景 - 与工具栏统一 */
    color: #cccccc;              /* 菜单文字颜色 */
    border: none;
    padding: 0px;
    margin: 0px;
}

QMenuBar::item:selected {
    background-color: #333333;   /* 选中菜单项背景 */
}

/* === 下拉菜单 === */
QMenu {
    background-color: rgba(39, 39, 43, 240);
    border: 1px solid rgba(80, 80, 80, 200);
    border-radius: 4px;
    padding: 4px 0px;
    color: #ccc;
    font-size: 12px;
}

QMenu::item {
    padding: 5px 50px 5px 20px;
}

QMenu::item:selected {
    background-color: rgba(255, 136, 0, 180);
    color: white;
}

QMenu::separator {
    height: 1px;
    background: rgba(80, 80, 80, 150);
    margin: 3px 8px;
}

QMenu::right-arrow {
    width: 10px;
    height: 10px;
}

QMenu::indicator {
    width: 16px;
    height: 16px;
    margin-left: 4px;
}

/* === 工具栏 === */
QToolBar {
    background-color: #1c1c1f;   /* 工具栏背景 */
    border: none;
    border-bottom: 1px solid #0a0a0a;  /* 底部深色分隔线 */
    spacing: 10px;               /* 工具按钮间距 */
    padding: 5px;
    margin: 0px;
}

/* 工具栏内的容器继承工具栏背景 */
QToolBar QWidget {
    background-color: transparent;
}

/* === 状态栏 === */
QStatusBar {
    background-color: #1e1e1e;   /* 状态栏背景 */
    color: #888888;              /* 状态栏文字颜色 */
}

/* === 普通按钮 === */
QPushButton {
    background-color: #3d3d3d;   /* 按钮背景色 */
    border: 1px solid #555555;   /* 边框颜色和宽度 */
    padding: 5px 15px;           /* 内边距：上下5px，左右15px */
    border-radius: 3px;          /* 圆角半径 */
    color: #dddddd;              /* 按钮文字颜色 */
}

QPushButton:hover {
    background-color: #4d4d4d;   /* 悬停时背景变亮 */
}

QPushButton:pressed {
    background-color: #2d2d2d;   /* 按下时背景变暗 */
}

/* === 应用按钮（主要操作按钮）=== */
QPushButton#gradingApplyBtn {
    background-color: #cc6600;   /* 主题橙色背景 */
    color: white;                /* 白色文字 */
    font-weight: bold;           /* 粗体 */
    border: none;
}

QPushButton#gradingApplyBtn:hover {
    background-color: #e67300;   /* 悬停时更亮 */
}

/* === 工具栏按钮 === */
QToolBar QToolButton {
    background: transparent;      /* 透明背景 */
    border: none;
    border-radius: 4px;          /* 圆角 */
    padding: 4px 8px;
    margin-left: 2px;
}

QToolBar QToolButton:hover {
    background: #3d3d3d;         /* 悬停背景 */
}

QToolBar QToolButton:pressed {
    background: #1a1a1a;         /* 按下背景 */
}

/* 导出按钮特殊样式 */
QToolBar QToolButton[objectName="ExportButton"] {
    background-color: #cc6600;   /* 主题橙色 */
    color: white;
    font-weight: bold;
}

/* === 分组框 === */
QGroupBox {
    border: none;                /* 无边框 */
    margin-top: 10px;
    font-weight: bold;
    color: #888;                 /* 标题文字颜色 */
}

/* AdjustmentPanel 内的分组框透明 */
AdjustmentPanel QGroupBox {
    background: transparent;
}

AdjustmentPanel QLabel {
    background: transparent;
}

/* ============================================
   === 滑动条样式 ===
   ============================================
   
   滑动条由三部分组成：
   1. groove - 滑槽（背景轨道）
   2. sub-page - 已滑过部分（左侧/下方）
   3. add-page - 未滑过部分（右侧/上方）
   4. handle - 滑块手柄
   
   当前设计：无进度条，只有滑槽和手柄
*/

/* --- 水平滑动条 --- */

/* 滑槽（轨道） */
QSlider::groove:horizontal {
    border: 1px solid #3d3d3d;   /* 边框颜色 */
    height: 4px;                 /* 滑槽高度 */
    background: #1a1a1c;         /* 滑槽背景色 */
    margin: 2px 0;               /* 上下边距 */
    border-radius: 2px;          /* 圆角 */
}

/* 已滑过部分（设为透明，不显示进度） */
QSlider::sub-page:horizontal {
    background: transparent;      /* 透明：不显示进度条 */
    border: none;
    height: 4px;
    border-radius: 2px;
}

/* 未滑过部分 */
QSlider::add-page:horizontal {
    background: transparent;      /* 透明 */
    border: none;
    height: 4px;
    border-radius: 2px;
}

/* 滑块手柄 */
QSlider::handle:horizontal {
    background: #cc6600;         /* 与选中标签背景一致 */
    border: 1px solid #1e1e1e;   /* 边框：宽度 颜色 */
    width: 10px;                 /* 手柄宽度 */
    height: 10px;                /* 手柄高度 */
    margin: -4px 0;              /* 负边距使手柄居中于滑槽 */
    border-radius: 5px;          /* 圆角（设为宽高一半则为正圆） */
}

QSlider::handle:horizontal:hover {
    background: #ffaa44;         /* 悬停时更亮的橙色 */
    border: 1px solid #333333;
}

QSlider::handle:horizontal:pressed {
    background: #cc6600;         /* 按下时更深的橙色 */
}

/* 滑杆禁用态：DenoisePanel 达芬奇式"整列变灰"联动专用（限定作用域，
   不影响其他面板既有观感；几何与常规态一致，仅颜色转暗，避免布局跳动） */
DenoisePanel QSlider::groove:horizontal:disabled {
    border: 1px solid #262628;
    height: 4px;
    background: #202023;
    margin: 2px 0;
    border-radius: 2px;
}

DenoisePanel QSlider::handle:horizontal:disabled {
    background: #4a4a4e;
    border: 1px solid #1e1e1e;
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 5px;
}

/* --- 垂直滑动条 --- */

/* 滑槽 */
QSlider::groove:vertical {
    border: 1px solid #3d3d3d;
    width: 4px;                  /* 滑槽宽度 */
    background: #2a2a2a;
    margin: 0 2px;
    border-radius: 2px;
}

/* 已滑过部分 */
QSlider::sub-page:vertical {
    background: transparent;
    border: none;
    width: 4px;
    border-radius: 2px;
}

/* 未滑过部分 */
QSlider::add-page:vertical {
    background: transparent;
    border: none;
    width: 4px;
    border-radius: 2px;
}

/* 手柄 */
QSlider::handle:vertical {
    background: #cc6600;         /* 与选中标签背景一致 */
    border: 1px solid #1e1e1e;   /* 边框 */
    width: 10px;                 /* 手柄宽度 */
    height: 10px;                /* 手柄高度 */
    margin: 0 -4px;              /* 负边距使手柄居中于滑槽 */
    border-radius: 5px;          /* 圆角（设为宽高一半则为正圆） */
}

QSlider::handle:vertical:hover {
    background: #ffaa44;         /* 悬停时更亮的橙色 */
    border: 1px solid #333333;
}

QSlider::handle:vertical:pressed {
    background: #cc6600;         /* 按下时更深的橙色 */
}

/* ============================================
   === 数值输入框 ===
   ============================================
*/

/* SpinBox 和 DoubleSpinBox */
QSpinBox, QDoubleSpinBox {
    background-color: #27272b;   /* 输入框背景 */
    border: 1px solid #3a3a3a;   /* 边框 */
    border-radius: 4px;          /* 圆角 */
    padding: 2px 6px;            /* 内边距 */
    min-height: 20px;            /* 固定高度（含边框共 22px） */
    color: #E0E0E0;              /* 文字颜色 */
    font-size: 11px;             /* 字号 */
    selection-background-color: #ff8800;  /* 选中文字背景 */
}

QSpinBox:hover, QDoubleSpinBox:hover {
    border: 1px solid #4a4a4a;   /* 悬停边框变亮 */
    background-color: #2e2e33;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #ff8800;   /* 聚焦时显示主题橙色边框 */
}

/* 隐藏上下调节按钮 */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0px;                  /* 设为0隐藏按钮 */
}

/* ============================================
   === 文本输入框 ===
   ============================================
*/

QLineEdit {
    background-color: #27272b;   /* 输入框背景（与 SpinBox 一致） */
    border: 1px solid #3a3a3a;   /* 边框 */
    border-radius: 4px;          /* 圆角 */
    padding: 4px 8px;            /* 内边距 */
    color: #E0E0E0;              /* 文字颜色 */
    font-size: 11px;             /* 字号 */
    selection-background-color: #ff8800;  /* 选中文字背景 */
}

QLineEdit:hover {
    border: 1px solid #4a4a4a;   /* 悬停边框变亮 */
}

QLineEdit:focus {
    border: 1px solid #ff8800;   /* 聚焦时橙色边框 */
}

/* ============================================
   === 下拉组合框 ===
   ============================================
*/

QComboBox {
    background-color: #252529;   /* 背景色 */
    border: 1px solid #3a3a3a;   /* 边框 */
    border-radius: 4px;          /* 圆角 */
    padding: 4px 8px;            /* 内边距 */
    color: #E0E0E0;              /* 文字颜色 */
    font-size: 12px;
    min-height: 22px;            /* 最小高度 */
}

QComboBox:hover {
    border: 1px solid #4a4a4a;
    background-color: #2a2a2a;
}

QComboBox:focus, QComboBox:on {
    border: 1px solid #ff8800;   /* 聚焦/展开时橙色边框 */
}

/* 下拉按钮区域 */
QComboBox::drop-down {
    border: none;
    width: 20px;                 /* 下拉按钮宽度 */
    background: transparent;     /* 透明背景，去除分隔线 */
    subcontrol-origin: padding;  /* 从内边距区域开始 */
    subcontrol-position: right;  /* 定位在右侧 */
}

/* 下拉箭头（使用CSS绘制三角形） */
QComboBox::down-arrow {
    image: none;                 /* 不使用图片 */
    /* 用边框模拟三角形箭头 */
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #888888;  /* 箭头颜色 */
    margin-right: 5px;
}

/* 下拉列表 */
QComboBox QAbstractItemView {
    background-color: rgba(39, 39, 43, 240);
    border: 1px solid rgba(80, 80, 80, 200);
    border-radius: 4px;
    selection-background-color: rgba(255, 136, 0, 180);
    selection-color: #ffffff;
    outline: none;
    padding: 4px 0px;
}

/* ============================================
   === 复选框 ===
   ============================================
*/

QCheckBox {
    color: #aaaaaa;              /* 文字颜色 */
    spacing: 8px;                /* 文字与勾选框间距 */
    font-size: 11px;
}

/* 勾选框指示器 */
QCheckBox::indicator {
    width: 14px;                 /* 全局复选框尺寸 */
    height: 14px;
    border: 1px solid #3a3a3a;   /* 边框 */
    border-radius: 3px;          /* 圆角 */
    background-color: #252525;   /* 背景 */
}

QCheckBox::indicator:hover {
    border: 1px solid #4a4a4a;
    background-color: #2a2a2a;
}

/* 选中状态 */
QCheckBox::indicator:checked {
    background-color: #cc6600;   /* 与选中标签背景一致 */
    border: 1px solid #cc6600;
    image: url("__CHECKMARK_ICON_URL__");
}

QCheckBox::indicator:checked:hover {
    background-color: #ffaa44;
    border: 1px solid #ffaa44;
}

QCheckBox:checked {
    color: #cc6600;              /* 与选中标签背景一致 */
}

/* ============================================
   === 重置按钮 ===
   ============================================
   面板标题栏右侧的小圆形重置按钮
*/

QPushButton#reset_btn {
    padding: 0px;
    border-radius: 8px;          /* 圆形（设为宽高一半） */
    background-color: transparent;
    border: 1px solid #333333;
    min-width: 16px;
    max-width: 16px;
    min-height: 16px;
    max-height: 16px;
}

QPushButton#reset_btn:hover {
    background-color: #333333;
    border: 1px solid #ff8800;   /* 悬停时橙色边框 */
}

QPushButton#reset_btn:pressed {
    background-color: #ff8800;   /* 按下时橙色背景 */
}

/* === 色轮重置按钮 === */
QPushButton#color_wheel_reset_btn {
    padding: 0px;
    border-radius: 9px;
    background-color: transparent;
    border: 1px solid #444444;
    min-width: 18px;
    max-width: 18px;
    min-height: 18px;
    max-height: 18px;
}

QPushButton#color_wheel_reset_btn:hover {
    background-color: #333333;
    border: 1px solid #ff8800;
}

QPushButton#color_wheel_reset_btn:pressed {
    background-color: #ff8800;
}

/* ============================================
   === 分组框（带边框） ===
   ============================================
*/

QGroupBox {
    border: 1px solid #333333;   /* 边框颜色 */
    border-radius: 6px;          /* 圆角 */
    margin-top: 12px;            /* 顶部外边距（为标题留空间） */
    font-weight: bold;
    font-size: 10px;
    color: #888888;              /* 标题颜色 */
    padding-top: 8px;            /* 内部顶部内边距 */
}

QGroupBox::title {
    subcontrol-origin: margin;   /* 标题定位基准 */
    left: 10px;                  /* 标题左边距 */
    padding: 0 5px;              /* 标题内边距 */
}

/* ============================================
   === 图形视图 ===
   ============================================
*/

QGraphicsView {
    border: none;
    background-color: #121212;   /* 画布背景（最深） */
}

/* ============================================
   === 分隔条 ===
   ============================================
*/

QSplitter::handle {
    /* 与 Dock 分割器使用同一条项目自定义分割线 */
    background-color: __PANEL_DIVIDER__;
    border: none;
}

QSplitter::handle:horizontal {
    width: 1px;                  /* 水平分隔条宽度 */
}

QSplitter::handle:vertical {
    height: 1px;                 /* 垂直分隔条高度 */
}

/* ============================================
   === 滚动条 ===
   ============================================
*/

/* 垂直滚动条 */
QScrollBar:vertical {
    border: none;
    background: #1e1e1e;         /* 滚动条区域背景 */
    width: 10px;                 /* 滚动条宽度 */
    margin: 0px;
}

QScrollBar::handle:vertical {
    background: #3d3d3d;         /* 滑块颜色 */
    min-height: 20px;            /* 滑块最小高度 */
    border-radius: 5px;          /* 圆角 */
    margin: 2px;
}

QScrollBar::handle:vertical:hover {
    background: #555555;         /* 悬停时滑块变亮 */
}

/* 隐藏滚动条两端按钮 */
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

/* 水平滚动条 */
QScrollBar:horizontal {
    border: none;
    background: #1e1e1e;
    height: 10px;
    margin: 0px;
}

QScrollBar::handle:horizontal {
    background: #3d3d3d;
    min-width: 20px;
    border-radius: 5px;
    margin: 2px;
}

QScrollBar::handle:horizontal:hover {
    background: #555555;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* ============================================
   === 主窗口分隔符 ===
   ============================================
*/

QMainWindow::separator {
    /* Keep the wide resize hit area transparent; MainWindow paints one center line. */
    background-color: transparent;
    border: none;
    width: 8px;
    height: 8px;
}

/* === Dock 标签栏 === */
QTabWidget::pane {
    border: none;
}

QTabBar::tab {
    background: rgb(70, 70, 77);
    color: #bbb;
    padding: 3px 14px;
    border: none;
}

QTabBar::tab:hover {
    color: #bbb;
    background: rgba(255, 255, 255, 15);
}

QTabBar::tab:selected {
    color: #fff;
    background: rgb(204, 102, 0);
}

QTabBar::tab:first {
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}

QTabBar::tab:last {
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}

QTabBar::tab:only-one {
    border-radius: 8px;
}
"""

# ``QCheckBox::indicator`` 使用自定义背景后，Qt 不会再绘制平台原生勾选标记。
# 以运行时绝对 URL 注入资源，开发环境和 PyInstaller 包内均能可靠加载。
DARK_THEME = DARK_THEME.replace("__CHECKMARK_ICON_URL__", _CHECKMARK_ICON_PATH)
DARK_THEME = DARK_THEME.replace("__PANEL_DIVIDER__", C.PANEL_DIVIDER)

# ============================================================================
# 标签页样式
# ============================================================================
TAB_STYLE = """
    /* 标签页面板 */
    QTabWidget::pane { 
        border-top: 2px solid #333;   /* 顶部边框 */
        background: #181818;           /* 面板背景 */
    }
    
    /* 标签按钮 */
    QTabBar::tab { 
        background: #222;              /* 未选中背景 */
        color: #999;                   /* 未选中文字颜色 */
        padding: 8px 15px;             /* 内边距 */
        margin-right: 2px;             /* 标签间距 */
        min-width: 80px;               /* 最小宽度 */
        font-size: 11px;
        font-weight: bold;
        border-top-left-radius: 4px;   /* 左上圆角 */
        border-top-right-radius: 4px;  /* 右上圆角 */
    }
    
    QTabBar::tab:hover {
        background: #2a2a2a;
        color: #eee;
    }
    
    /* 选中状态 */
    QTabBar::tab:selected { 
        background: #333;              /* 选中背景 */
        color: #ff8800;               /* 选中文字颜色（主题橙色） */
        border-bottom: 2px solid #ff8800;  /* 底部橙色线条 */
    }
"""

# ============================================================================
# 图层列表样式
# ============================================================================
LAYER_LIST_STYLE = """
    /* 图层列表 */
    QListWidget { 
        background: #27272b;              /* 列表背景 */
        border: none; 
        outline: none; 
    }
    
    QListWidget::item { 
        border-bottom: 1px solid #2a2a2a;  /* 项目分隔线 */
    }
    
    QListWidget::item:selected { 
        background: #444;              /* 选中背景 */
    }
    
    QListWidget::item:hover { 
        background: #333;              /* 悬停背景 */
    }
"""

# ============================================================================
# Composite Keyframes 面板样式（Fusion 风格底部 Dock）
# 按计划表 4 节映射到全局 token，不硬编码 DaVinci 私有色值。
# ============================================================================

COMPOSITE_KEYFRAMES_PANEL_STYLE = f"""
    QWidget#CompositeKeyframesPanel, QWidget#CompositeSplinePanel {{
        background-color: {C.BG_DARKEST};
        border: none;
    }}
"""

COMPOSITE_KEYFRAMES_TOOLBAR_STYLE = f"""
    QToolBar {{
        background-color: {C.BG_DARK};
        border: none;
        border-bottom: 1px solid {C.BORDER_DARK};
        spacing: 2px;
        padding: 3px 6px;
        min-height: 32px;
    }}
    QToolBar::separator {{
        width: 1px;
        height: 18px;
        margin: 4px 5px;
        background-color: {C.BORDER_MID};
    }}
    QToolBar QLabel {{
        color: {C.TEXT_SECONDARY};
        font-weight: 600;
        font-size: 12px;
    }}
"""

COMPOSITE_KEYFRAMES_ICON_BTN_STYLE = f"""
    QToolButton {{
        background-color: {C.BG_INPUT};
        border: 1px solid {C.BORDER_DARK};
        border-radius: 3px;
        padding: 0px;
        color: {C.TEXT_HINT};
    }}
    QToolButton:hover {{
        background-color: {C.BG_HOVER};
        color: {C.TEXT_SECONDARY};
        border-color: {C.BORDER_MID};
    }}
    QToolButton:pressed, QToolButton:checked {{
        background-color: {C.BG_ACTIVE};
        color: {C.ORANGE};
        border-color: {C.ORANGE_PRESS};
    }}
    QToolButton:disabled {{
        color: {C.TEXT_HINT};
    }}
    QLineEdit#KeyframesPropertySearch {{
        background-color: {C.BG_INPUT};
        color: {C.TEXT_SECONDARY};
        border: 1px solid {C.BORDER_DARK};
        border-radius: 3px;
        padding: 2px 6px;
        min-height: 20px;
        selection-background-color: {C.ORANGE_PRESS};
    }}
    QLineEdit#KeyframesPropertySearch:focus {{
        border-color: {C.BORDER_HOVER};
    }}
    QComboBox#KeyframesPropertyFilter {{
        min-height: 20px;
    }}
"""

COMPOSITE_KEYFRAMES_TREE_STYLE = f"""
    QWidget#KeyframesTreeSide {{
        background-color: {C.KEYFRAMES_BG};
        border-right: 1px solid {C.BORDER_SUBTLE};
    }}
"""

COMPOSITE_KEYFRAMES_COMBO_STYLE = f"""
    QComboBox {{
        background-color: {C.BG_INPUT};
        border: 1px solid {C.BORDER_DARK};
        border-radius: 3px;
        padding: 1px 6px;
        color: {C.TEXT_SECONDARY};
        font-size: 11px;
        min-width: 60px;
    }}
    QComboBox:hover {{
        border: 1px solid {C.BORDER_HOVER};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 16px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {C.BG_PANEL};
        border: 1px solid {C.BORDER};
        selection-background-color: {C.ORANGE_PRESS};
        color: {C.TEXT_SECONDARY};
    }}
"""
