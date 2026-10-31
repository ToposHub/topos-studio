#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Topos Color - UI Style Preview Tool
====================================

Preview all centralized UI styles defined in style_sheet.py:

基础控件 (Basic Controls):
  1. Color Constants Palette — 颜色常量色板 (class C 全部颜色)
  2. Button Styles — 按钮样式 (普通/悬停/按下/应用/导出)
  3. Slider Styles — 滑动条样式 (水平/垂直/可点击)
  4. Input Controls — 输入控件 (SpinBox/DoubleSpinBox/ComboBox)
  5. ToggleSwitch — 开关控件 (ON/OFF 状态)
  6. DragValue — 拖拽数值控件
  7. Curve Toolbar Buttons — 曲线工具栏按钮 (M/R/G/B/吸管/重置)
  8. CheckBox — 复选框
  9. TabWidget & ListWidget — 标签页 & 列表
 10. Mixer Track Buttons — 混音器轨道按钮 (M/S/Mon)

面板/对话框模板 (Panel/Dialog Templates):
 11. 通用面板组件模板 — 标题/参数标签/分隔线/空状态/SpinBox/透明容器
 12. Composite 节点编辑器 — 节点外框/标题/工具栏/按钮/分组框
 13. 对话框模板 — 通用对话框/预设对话框/右键菜单
 14. Script Panel — 工具栏/Run/Stop/编辑器/控制台/补全列表
 15. Library Panel — 面板整体/列表视图/模式按钮/缩放标签
 16. Plugin Panel — 标题/错误组/AI效果组/处理按钮
 17. HSL/LUT/Shortcut/Mixer — 曲线下拉/LUT列表/快捷键/混音SpinBox

Usage:
    python tools/preview_styles.py
"""

import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QSpinBox, QDoubleSpinBox,
    QComboBox, QCheckBox, QGroupBox, QScrollArea, QFrame,
    QTabWidget, QListWidget, QListWidgetItem, QGridLayout,
    QSplitter, QScrollArea, QToolButton, QLineEdit, QPlainTextEdit,
    QKeySequenceEdit,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QPainter, QBrush, QFont

from src.shared.ui.style_sheet import (
    C, DARK_THEME, TAB_STYLE, apply_combo_dropdown_style,
    # DragValue / Curve
    CURVE_BTN_CONTAINER_STYLE, CURVE_PICKER_BTN_STYLE, CURVE_RESET_BTN_STYLE,
    curve_channel_btn_style, DRAG_VALUE_NORMAL_STYLE, DRAG_VALUE_ACTIVE_STYLE,
    DRAG_VALUE_LABEL_STYLE,
    # 通用面板组件
    PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE, ENABLE_LABEL_STYLE,
    TRANSPARENT_STYLE, SEPARATOR_LINE_STYLE, SCROLL_AREA_TRANSPARENT_STYLE,
    SPINBOX_COMPACT_STYLE, EMPTY_STATE_LABEL_STYLE,
    # Composite 节点编辑器
    NODE_SECTION_STYLE, NODE_COLLAPSE_BTN_STYLE, NODE_TITLE_LABEL_STYLE,
    NODE_CONTENT_STYLE, PROPS_HEADER_STYLE, PROPS_TITLE_LABEL_STYLE,
    TOOLBAR_FLAT_BTN_STYLE, COMPOSITE_TOOLBAR_STYLE, COMPOSITE_TOOLBAR_TITLE_STYLE,
    COMPOSITE_ADD_NODE_BTN_STYLE, COMPOSITE_EVAL_BTN_STYLE, COMPOSITE_MODE_BTN_STYLE,
    ZOOM_LABEL_STYLE, NODE_GROUP_BOX_STYLE,
    # 对话框
    DIALOG_BASE_STYLE, PRESET_DIALOG_STYLE, MANAGE_PRESET_DIALOG_STYLE, CONTEXT_MENU_STYLE,
    # Script Panel
    SCRIPT_TOOLBAR_BTN_STYLE, SCRIPT_RUN_BTN_STYLE, SCRIPT_STOP_BTN_STYLE,
    SCRIPT_EDITOR_STYLE, SCRIPT_CONSOLE_STYLE, SCRIPT_STATUS_STYLE,
    CONSOLE_LABEL_STYLE, CONSOLE_CLEAR_BTN_STYLE, COMPLETER_POPUP_STYLE,
    # Library Panel
    LIBRARY_PANEL_STYLE, LIBRARY_LIST_VIEW_STYLE, LIBRARY_MODE_BTN_STYLE,
    LIBRARY_ZOOM_LABEL_STYLE,
    # Plugin Panel
    PLUGIN_TITLE_STYLE, AI_EFFECT_GROUP_STYLE, AI_PROCESS_BTN_STYLE,
    ERROR_GROUP_STYLE, ERROR_LABEL_STYLE, SCALE_LABEL_STYLE,
    # HSL / LUT / Shortcut / Mixer
    HSL_MODE_COMBO_STYLE, HSL_INFO_LABEL_STYLE, LUT_TOOLBAR_LABEL_STYLE,
    LUT_LIST_STYLE, SHORTCUT_EDITOR_STYLE, MIXER_SPIN_STYLE, MIXER_TRACK_BTN_STYLE,
)
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.topos_controls import ToposSlider, ToposClickableSlider


# ============================================================================
# Helpers
# ============================================================================

def make_section(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    """Create a titled group box section."""
    group = QGroupBox(title)
    layout = QVBoxLayout(group)
    layout.setSpacing(8)
    return group, layout


def make_color_swatch(color_value: str, name: str) -> QWidget:
    """Create a color swatch + label."""
    widget = QWidget()
    widget.setFixedWidth(130)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(2)

    swatch = QFrame()
    swatch.setFixedSize(120, 40)
    swatch.setStyleSheet(f"""
        QFrame {{
            background-color: {color_value};
            border: 1px solid #555;
            border-radius: 4px;
        }}
    """)

    label = QLabel(f"{name}\n{color_value}")
    label.setStyleSheet("color: #aaa; font-size: 9px; background: transparent; border: none;")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)

    layout.addWidget(swatch)
    layout.addWidget(label)
    return widget


def _info_label(text: str) -> QLabel:
    """Create a small info label."""
    lbl = QLabel(text)
    lbl.setStyleSheet("color: #888; font-size: 10px; background: transparent; border: none;")
    return lbl


# ============================================================================
# Main Preview Window
# ============================================================================

class StylePreviewWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Topos Color — UI Style Preview")
        self.setMinimumSize(1200, 900)

        # Apply global dark theme
        self.setStyleSheet(DARK_THEME)

        # Main scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #1e1e1e; }")

        container = QWidget()
        container.setStyleSheet("background: #1e1e1e;")
        self.main_layout = QVBoxLayout(container)
        self.main_layout.setSpacing(16)
        self.main_layout.setContentsMargins(20, 20, 20, 20)

        # Build sections
        self._build_color_palette()
        self._build_buttons()
        self._build_sliders()
        self._build_input_controls()
        self._build_toggle_switches()
        self._build_drag_values()
        self._build_curve_buttons()
        self._build_checkboxes()
        self._build_tabs_and_lists()
        self._build_mixer_buttons()
        # --- 新增板块：收拢后的面板/对话框模板 ---
        self._build_generic_panel_components()
        self._build_composite_node_editor()
        self._build_dialog_templates()
        self._build_script_panel()
        self._build_library_panel()
        self._build_plugin_panel()
        self._build_hsl_lut_shortcut()

        self.main_layout.addStretch()
        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    def _labeled(self, widget: QWidget, label_text: str) -> QWidget:
        """Wrap a widget with a small label underneath."""
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        v.addWidget(widget)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #666; font-size: 9px; background: transparent; border: none;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(lbl)
        return container

    # ------------------------------------------------------------------
    # 1. Color Constants Palette
    # ------------------------------------------------------------------
    def _build_color_palette(self):
        group, layout = make_section("1. Color Constants Palette (颜色常量色板 — class C)")
        layout.setSpacing(4)

        categories = {
            "Theme Orange (主题橙)": [
                ("ORANGE", C.ORANGE),
                ("ORANGE_HOVER", C.ORANGE_HOVER),
                ("ORANGE_PRESS", C.ORANGE_PRESS),
                ("ORANGE_180", C.ORANGE_180),
            ],
            "Background Levels (背景层级)": [
                ("BG_DEEPEST", C.BG_DEEPEST),
                ("BG_DARKEST", C.BG_DARKEST),
                ("BG_DARK", C.BG_DARK),
                ("BG_PANEL", C.BG_PANEL),
                ("BG_INPUT", C.BG_INPUT),
                ("BG_INPUT_DARK", C.BG_INPUT_DARK),
                ("BG_HOVER", C.BG_HOVER),
                ("BG_ACTIVE", C.BG_ACTIVE),
                ("BG_BUTTON", C.BG_BUTTON),
                ("BG_BUTTON_HOVER", C.BG_BUTTON_HOVER),
                ("BG_BUTTON_PRESS", C.BG_BUTTON_PRESS),
            ],
            "Border (边框)": [
                ("BORDER", C.BORDER),
                ("BORDER_HOVER", C.BORDER_HOVER),
                ("BORDER_DARK", C.BORDER_DARK),
                ("BORDER_MID", C.BORDER_MID),
            ],
            "Text (文字)": [
                ("TEXT_PRIMARY", C.TEXT_PRIMARY),
                ("TEXT_SECONDARY", C.TEXT_SECONDARY),
                ("TEXT_HINT", C.TEXT_HINT),
                ("TEXT_DIM", C.TEXT_DIM),
                ("TEXT_LABEL", C.TEXT_LABEL),
                ("TEXT_BRIGHT", C.TEXT_BRIGHT),
            ],
            "ToggleSwitch (开关)": [
                ("TOGGLE_PILL_BG", C.TOGGLE_PILL_BG),
                ("TOGGLE_CIRCLE_ON", C.TOGGLE_CIRCLE_ON),
                ("TOGGLE_CIRCLE_OFF", C.TOGGLE_CIRCLE_OFF),
            ],
            "Curve / Scope (曲线/示波器)": [
                ("SCOPE_BG", C.SCOPE_BG),
                ("CURVE_BTN_BG", C.CURVE_BTN_BG),
                ("CURVE_BTN_BORDER", C.CURVE_BTN_BORDER),
                ("CHANNEL_M", C.CHANNEL_M),
                ("CHANNEL_R", C.CHANNEL_R),
                ("CHANNEL_G", C.CHANNEL_G),
                ("CHANNEL_B", C.CHANNEL_B),
                ("PICKER_CHECKED_BG", C.PICKER_CHECKED_BG),
                ("PICKER_CHECKED_BORDER", C.PICKER_CHECKED_BORDER),
            ],
            "Mixer (混音器)": [
                ("MIXER_MUTE_CHECKED", C.MIXER_MUTE_CHECKED),
                ("MIXER_MUTE_BORDER", C.MIXER_MUTE_BORDER),
                ("MIXER_SOLO_CHECKED", C.MIXER_SOLO_CHECKED),
                ("MIXER_SOLO_BORDER", C.MIXER_SOLO_BORDER),
                ("MIXER_MONITOR_CHECKED", C.MIXER_MONITOR_CHECKED),
                ("MIXER_MONITOR_BORDER", C.MIXER_MONITOR_BORDER),
            ],
        }

        for cat_name, colors in categories.items():
            cat_label = QLabel(f"  {cat_name}")
            cat_label.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; background: transparent; border: none;")
            layout.addWidget(cat_label)

            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setSpacing(4)
            row_layout.setContentsMargins(0, 0, 0, 0)
            for name, value in colors:
                row_layout.addWidget(make_color_swatch(value, name))
            row_layout.addStretch()
            layout.addWidget(row)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 2. Button Styles (按钮样式)
    # ------------------------------------------------------------------
    def _build_buttons(self):
        group, layout = make_section("2. Button Styles (按钮样式)")

        row1 = QHBoxLayout()
        row1.setSpacing(12)

        # Normal button
        btn_normal = QPushButton("Normal")
        row1.addWidget(self._labeled(btn_normal, "QPushButton\n(普通按钮)"))

        # Hover state
        btn_hover = QPushButton("Hover")
        btn_hover.setStyleSheet(f"""
            QPushButton {{
                background-color: {C.BG_BUTTON_HOVER};
                border: 1px solid {C.BORDER_MID};
                padding: 5px 15px;
                border-radius: 3px;
                color: {C.TEXT_BRIGHT};
            }}
        """)
        row1.addWidget(self._labeled(btn_hover, "hover state\n(悬停效果)"))

        # Pressed state
        btn_press = QPushButton("Pressed")
        btn_press.setStyleSheet(f"""
            QPushButton {{
                background-color: {C.BG_BUTTON_PRESS};
                border: 1px solid {C.BORDER_DARK};
                padding: 5px 15px;
                border-radius: 3px;
                color: {C.TEXT_BRIGHT};
            }}
        """)
        row1.addWidget(self._labeled(btn_press, "pressed state\n(按下效果)"))

        # Apply button (primary action)
        btn_apply = QPushButton("Apply")
        btn_apply.setObjectName("gradingApplyBtn")
        row1.addWidget(self._labeled(btn_apply, "#gradingApplyBtn\n(应用按钮)"))

        # Reset button (round)
        btn_reset = QPushButton("R")
        btn_reset.setObjectName("reset_btn")
        row1.addWidget(self._labeled(btn_reset, "#reset_btn\n(重置按钮)"))

        row1.addStretch()
        layout.addLayout(row1)

        # Second row: toolbar & export
        row2 = QHBoxLayout()
        row2.setSpacing(12)

        tb_btn = QToolButton()
        tb_btn.setText("Tool")
        tb_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 4px 8px;
                color: {C.TEXT_SECONDARY};
            }}
            QToolButton:hover {{
                background: {C.BG_BUTTON};
            }}
        """)
        row2.addWidget(self._labeled(tb_btn, "QToolButton\n(工具栏按钮)"))

        export_btn = QToolButton()
        export_btn.setText("Export")
        export_btn.setObjectName("ExportButton")
        export_btn.setStyleSheet(f"""
            QToolButton {{
                background-color: {C.ORANGE_PRESS};
                color: white;
                font-weight: bold;
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }}
        """)
        row2.addWidget(self._labeled(export_btn, "#ExportButton\n(导出按钮)"))

        row2.addStretch()
        layout.addLayout(row2)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 3. Slider Styles (滑动条样式)
    # ------------------------------------------------------------------
    def _build_sliders(self):
        group, layout = make_section("3. Slider Styles (滑动条样式 — QSlider)")

        # Horizontal slider
        layout.addWidget(_info_label("Horizontal Slider — ToposSlider (水平滑动条)"))
        slider_h = ToposSlider(Qt.Orientation.Horizontal, default_value=50)
        slider_h.setRange(0, 100)
        slider_h.setValue(50)
        slider_h.setFixedHeight(30)
        layout.addWidget(slider_h)

        # Clickable slider
        layout.addWidget(_info_label("Clickable Slider — ToposClickableSlider (可点击滑动条, double-click to reset)"))
        slider_click = ToposClickableSlider(Qt.Orientation.Horizontal, default_value=30)
        slider_click.setRange(0, 100)
        slider_click.setValue(30)
        slider_click.setFixedHeight(30)
        layout.addWidget(slider_click)

        # Vertical sliders
        layout.addWidget(_info_label("Vertical Slider — QSlider::vertical (垂直滑动条)"))
        v_row = QHBoxLayout()
        v_row.setSpacing(16)
        for val in [20, 50, 80]:
            sv = QSlider(Qt.Orientation.Vertical)
            sv.setRange(0, 100)
            sv.setValue(val)
            sv.setFixedHeight(80)
            sv.setFixedWidth(30)
            v_row.addWidget(sv)
        v_row.addStretch()
        layout.addLayout(v_row)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 4. Input Controls (输入控件)
    # ------------------------------------------------------------------
    def _build_input_controls(self):
        group, layout = make_section("4. Input Controls (输入控件)")

        row = QHBoxLayout()
        row.setSpacing(16)

        # SpinBox
        spin_col = QVBoxLayout()
        spin_col.addWidget(_info_label("QSpinBox\n(整数输入框)"))
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(42)
        spin_col.addWidget(spin)
        row.addLayout(spin_col)

        # DoubleSpinBox
        dspin_col = QVBoxLayout()
        dspin_col.addWidget(_info_label("QDoubleSpinBox\n(浮点输入框)"))
        dspin = QDoubleSpinBox()
        dspin.setRange(0.0, 1.0)
        dspin.setDecimals(2)
        dspin.setSingleStep(0.01)
        dspin.setValue(0.75)
        dspin_col.addWidget(dspin)
        row.addLayout(dspin_col)

        # ComboBox
        combo_col = QVBoxLayout()
        combo_col.addWidget(_info_label("QComboBox\n(下拉选择框)"))
        combo = QComboBox()
        combo.addItems(["Option A", "Option B", "Option C", "Option D"])
        apply_combo_dropdown_style(combo)
        combo_col.addWidget(combo)
        row.addLayout(combo_col)

        row.addStretch()
        layout.addLayout(row)
        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 5. ToggleSwitch (开关控件)
    # ------------------------------------------------------------------
    def _build_toggle_switches(self):
        group, layout = make_section("5. ToggleSwitch (开关控件)")

        row = QHBoxLayout()
        row.setSpacing(20)

        # OFF state
        off_widget = QWidget()
        off_widget.setStyleSheet("background: transparent;")
        off_v = QVBoxLayout(off_widget)
        off_v.setContentsMargins(0, 0, 0, 0)
        off_v.setSpacing(4)
        ts_off = ToggleSwitch(checked=False)
        off_v.addWidget(ts_off)
        off_lbl = QLabel(
            f"OFF (关闭)\n"
            f"Pill: {C.TOGGLE_PILL_BG}\n"
            f"Circle: {C.TOGGLE_CIRCLE_OFF}"
        )
        off_lbl.setStyleSheet("color: #888; font-size: 9px; background: transparent; border: none;")
        off_v.addWidget(off_lbl)
        row.addWidget(off_widget)

        # ON state
        on_widget = QWidget()
        on_widget.setStyleSheet("background: transparent;")
        on_v = QVBoxLayout(on_widget)
        on_v.setContentsMargins(0, 0, 0, 0)
        on_v.setSpacing(4)
        ts_on = ToggleSwitch(checked=True)
        on_v.addWidget(ts_on)
        on_lbl = QLabel(
            f"ON (开启)\n"
            f"Pill: {C.TOGGLE_PILL_BG}\n"
            f"Circle: {C.TOGGLE_CIRCLE_ON}"
        )
        on_lbl.setStyleSheet("color: #888; font-size: 9px; background: transparent; border: none;")
        on_v.addWidget(on_lbl)
        row.addWidget(on_widget)

        row.addStretch()
        layout.addLayout(row)
        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 6. DragValue (拖拽数值控件)
    # ------------------------------------------------------------------
    def _build_drag_values(self):
        group, layout = make_section("6. DragValue (拖拽数值控件)")

        info = QLabel("Drag to adjust | Shift+Drag = fine | Double-click = input | Middle-click = reset\n"
                       "(拖拽调整 | Shift+拖拽=精细 | 双击=输入 | 中键=重置)")
        info.setStyleSheet("color: #666; font-size: 10px; background: transparent; border: none;")
        info.setWordWrap(True)
        layout.addWidget(info)

        row = QHBoxLayout()
        row.setSpacing(16)

        dv1_container = QWidget()
        dv1_container.setStyleSheet("background: transparent;")
        dv1_v = QVBoxLayout(dv1_container)
        dv1_v.setContentsMargins(0, 0, 0, 0)
        dv1_v.setSpacing(2)
        dv1 = DragValue("Brightness", -100, 100, 0, decimals=0, suffix="")
        dv1_v.addWidget(dv1)
        dv1_v.addWidget(_info_label("DragValue: Brightness\n(亮度)"))
        row.addWidget(dv1_container)

        dv2_container = QWidget()
        dv2_container.setStyleSheet("background: transparent;")
        dv2_v = QVBoxLayout(dv2_container)
        dv2_v.setContentsMargins(0, 0, 0, 0)
        dv2_v.setSpacing(2)
        dv2 = DragValue("Saturation", 0.0, 2.0, 1.0, decimals=2, suffix="x")
        dv2_v.addWidget(dv2)
        dv2_v.addWidget(_info_label("DragValue: Saturation\n(饱和度)"))
        row.addWidget(dv2_container)

        dv3_container = QWidget()
        dv3_container.setStyleSheet("background: transparent;")
        dv3_v = QVBoxLayout(dv3_container)
        dv3_v.setContentsMargins(0, 0, 0, 0)
        dv3_v.setSpacing(2)
        dv3 = DragValue("Hue", 0, 360, 180, decimals=0, suffix="°")
        dv3_v.addWidget(dv3)
        dv3_v.addWidget(_info_label("DragValue: Hue\n(色相)"))
        row.addWidget(dv3_container)

        row.addStretch()
        layout.addLayout(row)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 7. Curve Toolbar Buttons (曲线工具栏按钮)
    # ------------------------------------------------------------------
    def _build_curve_buttons(self):
        group, layout = make_section("7. Curve Toolbar Buttons (曲线工具栏按钮)")

        # Curve button container
        container = QWidget()
        container.setStyleSheet(CURVE_BTN_CONTAINER_STYLE)
        container.setFixedHeight(34)
        btn_layout = QHBoxLayout(container)
        btn_layout.setContentsMargins(5, 2, 5, 2)
        btn_layout.setSpacing(4)

        # Channel buttons M R G B
        channels = [
            ("M", C.CHANNEL_M),
            ("R", C.CHANNEL_R),
            ("G", C.CHANNEL_G),
            ("B", C.CHANNEL_B),
        ]
        for label, color in channels:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedSize(28, 24)
            if label == "M":
                btn.setChecked(True)
            btn.setStyleSheet(curve_channel_btn_style(color))
            btn_layout.addWidget(btn)

        # Eyedropper / Color picker button
        picker = QPushButton("⊕")
        picker.setFixedSize(28, 24)
        picker.setCheckable(True)
        picker.setStyleSheet(CURVE_PICKER_BTN_STYLE)
        btn_layout.addWidget(picker)

        # Reset button
        reset = QPushButton("↺")
        reset.setFixedSize(28, 24)
        reset.setStyleSheet(CURVE_RESET_BTN_STYLE)
        btn_layout.addWidget(reset)

        btn_layout.addStretch()
        layout.addWidget(container)

        # Legend
        legend = QLabel(
            "M = Master (总控) | R = Red (红) | G = Green (绿) | B = Blue (蓝) | "
            "⊕ = ColorPicker (吸管) | ↺ = Reset (重置)\n"
            f"Container: CURVE_BTN_CONTAINER_STYLE"
        )
        legend.setStyleSheet("color: #666; font-size: 9px; background: transparent; border: none;")
        legend.setWordWrap(True)
        layout.addWidget(legend)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 8. CheckBox (复选框)
    # ------------------------------------------------------------------
    def _build_checkboxes(self):
        group, layout = make_section("8. CheckBox (复选框 — QCheckBox)")

        row = QHBoxLayout()
        row.setSpacing(20)

        cb1_container = QWidget()
        cb1_container.setStyleSheet("background: transparent;")
        cb1_v = QVBoxLayout(cb1_container)
        cb1_v.setContentsMargins(0, 0, 0, 0)
        cb1_v.setSpacing(2)
        cb1 = QCheckBox("Unchecked")
        cb1_v.addWidget(cb1)
        cb1_v.addWidget(_info_label("(未选中)"))
        row.addWidget(cb1_container)

        cb2_container = QWidget()
        cb2_container.setStyleSheet("background: transparent;")
        cb2_v = QVBoxLayout(cb2_container)
        cb2_v.setContentsMargins(0, 0, 0, 0)
        cb2_v.setSpacing(2)
        cb2 = QCheckBox("Checked")
        cb2.setChecked(True)
        cb2_v.addWidget(cb2)
        cb2_v.addWidget(_info_label("(已选中)"))
        row.addWidget(cb2_container)

        row.addStretch()
        layout.addLayout(row)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 9. TabWidget & ListWidget (标签页 & 列表)
    # ------------------------------------------------------------------
    def _build_tabs_and_lists(self):
        group, layout = make_section("9. TabWidget & ListWidget (标签页 & 列表)")

        # TabWidget
        layout.addWidget(_info_label("QTabWidget (标签页)"))
        tabs = QTabWidget()
        tabs.setStyleSheet(DARK_THEME)
        tab1 = QWidget()
        tab1.setStyleSheet(f"background: {C.BG_PANEL};")
        tab1_layout = QVBoxLayout(tab1)
        tab1_layout.addWidget(QLabel("Tab 1 content (标签页 1 内容)"))
        tab2 = QWidget()
        tab2.setStyleSheet(f"background: {C.BG_PANEL};")
        tab2_layout = QVBoxLayout(tab2)
        tab2_layout.addWidget(QLabel("Tab 2 content (标签页 2 内容)"))
        tabs.addTab(tab1, "Tab 1")
        tabs.addTab(tab2, "Tab 2")
        layout.addWidget(tabs)

        # ListWidget
        layout.addWidget(_info_label("QListWidget (列表)"))
        list_widget = QListWidget()
        list_widget.addItems(["Item 1", "Item 2", "Item 3 (selected)", "Item 4"])
        list_widget.item(2).setSelected(True)
        list_widget.setFixedHeight(120)
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.BG_PANEL};
                border: none;
                color: {C.TEXT_SECONDARY};
            }}
            QListWidget::item {{
                padding: 4px 8px;
                border-bottom: 1px solid #2a2a2a;
            }}
            QListWidget::item:selected {{
                background-color: {C.BG_BUTTON};
            }}
            QListWidget::item:hover {{
                background-color: {C.BORDER_DARK};
            }}
        """)
        layout.addWidget(list_widget)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 10. Mixer Track Buttons (混音器轨道按钮)
    # ------------------------------------------------------------------
    def _build_mixer_buttons(self):
        group, layout = make_section("10. Mixer Track Buttons (混音器轨道按钮)")

        row = QHBoxLayout()
        row.setSpacing(8)

        # Mute checked
        mute = QToolButton()
        mute.setText("M")
        mute.setObjectName("mute_btn")
        mute.setCheckable(True)
        mute.setChecked(True)
        mute.setFixedSize(36, 24)
        mute.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        row.addWidget(self._labeled(mute, "MUTE checked\n(静音-激活)"))

        # Mute unchecked
        mute_off = QToolButton()
        mute_off.setText("M")
        mute_off.setObjectName("mute_btn")
        mute_off.setCheckable(True)
        mute_off.setFixedSize(36, 24)
        mute_off.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        row.addWidget(self._labeled(mute_off, "MUTE unchecked\n(静音-关闭)"))

        # Solo checked
        solo = QToolButton()
        solo.setText("S")
        solo.setObjectName("solo_btn")
        solo.setCheckable(True)
        solo.setChecked(True)
        solo.setFixedSize(36, 24)
        solo.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        row.addWidget(self._labeled(solo, "SOLO checked\n(独奏-激活)"))

        # Solo unchecked
        solo_off = QToolButton()
        solo_off.setText("S")
        solo_off.setObjectName("solo_btn")
        solo_off.setCheckable(True)
        solo_off.setFixedSize(36, 24)
        solo_off.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        row.addWidget(self._labeled(solo_off, "SOLO unchecked\n(独奏-关闭)"))

        # Monitor checked
        mon = QToolButton()
        mon.setText("Mon")
        mon.setObjectName("monitor_btn")
        mon.setCheckable(True)
        mon.setChecked(True)
        mon.setFixedSize(36, 24)
        mon.setStyleSheet(MIXER_TRACK_BTN_STYLE)
        row.addWidget(self._labeled(mon, "MONITOR checked\n(监听-激活)"))

        row.addStretch()
        layout.addLayout(row)
        self.main_layout.addWidget(group)

    # ========================================================================
    # 新增板块：收拢后的面板/对话框模板预览
    # ========================================================================

    # ------------------------------------------------------------------
    # 11. 通用面板组件模板 (Generic Panel Components)
    #     供各功能面板复用的基础 UI 组件样式
    # ------------------------------------------------------------------
    def _build_generic_panel_components(self):
        group, layout = make_section("11. 通用面板组件模板 (Generic Panel Components — 各面板复用)")

        # --- 11.1 面板标题标签 ---
        layout.addWidget(_info_label("PANEL_TITLE_LABEL_STYLE — 面板标题标签 (如 'EFFECT CONTROLS')"))
        title_lbl = QLabel("EFFECT CONTROLS")
        title_lbl.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        layout.addWidget(title_lbl)

        # --- 11.2 参数标签 ---
        layout.addWidget(_info_label("PARAM_LABEL_STYLE — 参数标签 (滑动条左侧灰色文字)"))
        param_lbl = QLabel("Brightness")
        param_lbl.setStyleSheet(PARAM_LABEL_STYLE)
        layout.addWidget(param_lbl)

        # --- 11.3 Enable 标签 ---
        layout.addWidget(_info_label("ENABLE_LABEL_STYLE — 启用标签 (Enabled / Processing)"))
        enable_lbl = QLabel("Enabled")
        enable_lbl.setStyleSheet(ENABLE_LABEL_STYLE)
        layout.addWidget(enable_lbl)

        # --- 11.4 分隔线 ---
        layout.addWidget(_info_label("SEPARATOR_LINE_STYLE — 分隔线"))
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(SEPARATOR_LINE_STYLE)
        layout.addWidget(sep)

        # --- 11.5 空状态提示标签 ---
        layout.addWidget(_info_label("EMPTY_STATE_LABEL_STYLE — 空状态提示 (如 '暂无插件')"))
        empty_lbl = QLabel("No plugins available (暂无插件)")
        empty_lbl.setStyleSheet(EMPTY_STATE_LABEL_STYLE)
        layout.addWidget(empty_lbl)

        # --- 11.6 紧凑 SpinBox ---
        layout.addWidget(_info_label("SPINBOX_COMPACT_STYLE — 紧凑浮点输入框"))
        spin_row = QHBoxLayout()
        spin_row.setSpacing(8)
        dspin = QDoubleSpinBox()
        dspin.setRange(0.0, 1.0)
        dspin.setDecimals(2)
        dspin.setValue(0.50)
        dspin.setStyleSheet(SPINBOX_COMPACT_STYLE)
        spin_row.addWidget(dspin)
        spin_row.addStretch()
        layout.addLayout(spin_row)

        # --- 11.7 透明容器 + 滚动区域 ---
        layout.addWidget(_info_label("TRANSPARENT_STYLE / SCROLL_AREA_TRANSPARENT_STYLE — 透明背景"))
        trans_demo = QWidget()
        trans_demo.setFixedHeight(40)
        trans_demo.setStyleSheet(TRANSPARENT_STYLE)
        demo_layout = QHBoxLayout(trans_demo)
        demo_layout.setContentsMargins(8, 4, 8, 4)
        demo_lbl = QLabel("透明容器示例 (Transparent Container)")
        demo_lbl.setStyleSheet(f"color: {C.TEXT_SECONDARY}; background: transparent;")
        demo_layout.addWidget(demo_lbl)
        # 加个有色背景父容器来对比
        trans_parent = QWidget()
        trans_parent.setStyleSheet(f"background: {C.BG_DARK}; border-radius: 4px;")
        tp_layout = QVBoxLayout(trans_parent)
        tp_layout.addWidget(trans_demo)
        layout.addWidget(trans_parent)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 12. Composite 节点编辑器模板 (Node Editor Templates)
    #     用于 Composite 页面的节点/工具栏/按钮样式
    # ------------------------------------------------------------------
    def _build_composite_node_editor(self):
        group, layout = make_section("12. Composite 节点编辑器模板 (Node Editor — 节点合成页面)")

        # --- 12.1 节点 Section 外框 ---
        layout.addWidget(_info_label("NODE_SECTION_STYLE — 节点区域外框 (带圆角边框)"))
        node_section = QWidget()
        node_section.setFixedHeight(50)
        node_section.setStyleSheet(NODE_SECTION_STYLE)
        ns_layout = QVBoxLayout(node_section)
        ns_layout.setContentsMargins(8, 8, 8, 8)
        ns_lbl = QLabel("Node Section (节点区域)")
        ns_lbl.setStyleSheet(f"color: {C.TEXT_SECONDARY}; background: transparent; border: none;")
        ns_layout.addWidget(ns_lbl)
        layout.addWidget(node_section)

        # --- 12.2 节点标题文字 ---
        layout.addWidget(_info_label("NODE_TITLE_LABEL_STYLE — 节点标题"))
        node_title = QLabel("Color Correction")
        node_title.setStyleSheet(NODE_TITLE_LABEL_STYLE)
        layout.addWidget(node_title)

        # --- 12.3 节点内容区 ---
        layout.addWidget(_info_label("NODE_CONTENT_STYLE — 节点内容区背景"))
        node_content = QWidget()
        node_content.setFixedHeight(30)
        node_content.setStyleSheet(NODE_CONTENT_STYLE)
        nc_layout = QVBoxLayout(node_content)
        nc_layout.setContentsMargins(8, 4, 8, 4)
        nc_lbl = QLabel("内容区 (Content Area)")
        nc_lbl.setStyleSheet(f"color: {C.TEXT_HINT}; background: transparent; border: none;")
        nc_layout.addWidget(nc_lbl)
        layout.addWidget(node_content)

        # --- 12.4 Properties 头部栏 ---
        layout.addWidget(_info_label("PROPS_HEADER_STYLE — Properties 头部栏"))
        props_header = QWidget()
        props_header.setFixedHeight(30)
        props_header.setStyleSheet(PROPS_HEADER_STYLE)
        ph_layout = QHBoxLayout(props_header)
        ph_layout.setContentsMargins(8, 4, 8, 4)
        props_title = QLabel("Properties")
        props_title.setStyleSheet(PROPS_TITLE_LABEL_STYLE)
        ph_layout.addWidget(props_title)
        ph_layout.addStretch()
        layout.addWidget(props_header)

        # --- 12.5 折叠按钮 ---
        layout.addWidget(_info_label("NODE_COLLAPSE_BTN_STYLE — 节点折叠/展开按钮"))
        collapse_row = QHBoxLayout()
        collapse_btn = QToolButton()
        collapse_btn.setText("▼")
        collapse_btn.setStyleSheet(NODE_COLLAPSE_BTN_STYLE)
        collapse_row.addWidget(collapse_btn)
        collapse_row.addStretch()
        layout.addLayout(collapse_row)

        # --- 12.6 Composite 工具栏 ---
        layout.addWidget(_info_label("COMPOSITE_TOOLBAR_STYLE — 工具栏背景"))
        toolbar = QWidget()
        toolbar.setFixedHeight(36)
        toolbar.setStyleSheet(COMPOSITE_TOOLBAR_STYLE)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)
        tb_title = QLabel("Node Graph")
        tb_title.setStyleSheet(COMPOSITE_TOOLBAR_TITLE_STYLE)
        tb_layout.addWidget(tb_title)
        tb_layout.addStretch()
        layout.addWidget(toolbar)

        # --- 12.7 工具栏扁平按钮 ---
        layout.addWidget(_info_label("TOOLBAR_FLAT_BTN_STYLE — 工具栏图标按钮 (悬停可见效果)"))
        flat_btn_row = QHBoxLayout()
        flat_btn_row.setSpacing(4)
        for icon_text in ["🔍", "⊞", "⟳"]:
            fb = QToolButton()
            fb.setText(icon_text)
            fb.setFixedSize(28, 28)
            fb.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
            flat_btn_row.addWidget(fb)
        flat_btn_row.addStretch()
        layout.addLayout(flat_btn_row)

        # --- 12.8 添加节点按钮 ---
        layout.addWidget(_info_label("COMPOSITE_ADD_NODE_BTN_STYLE — 添加节点按钮"))
        add_btn = QPushButton("+ Add Node")
        add_btn.setStyleSheet(COMPOSITE_ADD_NODE_BTN_STYLE)
        layout.addWidget(add_btn)

        # --- 12.9 Evaluate 按钮 (橙色主操作) ---
        layout.addWidget(_info_label("COMPOSITE_EVAL_BTN_STYLE — Evaluate 按钮 (橙色)"))
        eval_btn = QPushButton("Evaluate")
        eval_btn.setStyleSheet(COMPOSITE_EVAL_BTN_STYLE)
        layout.addWidget(eval_btn)

        # --- 12.10 Preview/Full 切换按钮 ---
        layout.addWidget(_info_label("COMPOSITE_MODE_BTN_STYLE — Preview/Full 切换 (可选中)"))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        preview_btn = QPushButton("Preview")
        preview_btn.setCheckable(True)
        preview_btn.setChecked(True)
        preview_btn.setStyleSheet(COMPOSITE_MODE_BTN_STYLE)
        mode_row.addWidget(preview_btn)
        full_btn = QPushButton("Full")
        full_btn.setCheckable(True)
        full_btn.setStyleSheet(COMPOSITE_MODE_BTN_STYLE)
        mode_row.addWidget(full_btn)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # --- 12.11 Zoom 标签 ---
        layout.addWidget(_info_label("ZOOM_LABEL_STYLE — 缩放比例标签"))
        zoom_lbl = QLabel("100%")
        zoom_lbl.setStyleSheet(ZOOM_LABEL_STYLE)
        layout.addWidget(zoom_lbl)

        # --- 12.12 节点分组框 ---
        layout.addWidget(_info_label("NODE_GROUP_BOX_STYLE — 节点参数分组框"))
        node_group = QGroupBox("Parameters")
        node_group.setStyleSheet(NODE_GROUP_BOX_STYLE)
        ng_layout = QVBoxLayout(node_group)
        ng_lbl = QLabel("Group content (分组内容)")
        ng_lbl.setStyleSheet(f"color: {C.TEXT_SECONDARY}; background: transparent; border: none;")
        ng_layout.addWidget(ng_lbl)
        layout.addWidget(node_group)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 13. 对话框模板 (Dialog Templates)
    #     通用对话框、预设对话框、右键菜单等
    # ------------------------------------------------------------------
    def _build_dialog_templates(self):
        group, layout = make_section("13. 对话框模板 (Dialog Templates — 弹窗/预设/菜单)")

        # --- 13.1 通用暗色对话框 ---
        layout.addWidget(_info_label("DIALOG_BASE_STYLE — 通用对话框 (含 QLabel/QLineEdit/QPushButton/QSlider)"))
        dialog_demo = QWidget()
        dialog_demo.setStyleSheet(DIALOG_BASE_STYLE)
        dd_layout = QVBoxLayout(dialog_demo)
        dd_layout.setContentsMargins(12, 12, 12, 12)
        dd_layout.setSpacing(8)
        dd_layout.addWidget(QLabel("Dialog Label (对话框文字)"))
        dd_line = QLineEdit()
        dd_line.setPlaceholderText("Input text...")
        dd_line.setText("示例输入")
        dd_layout.addWidget(dd_line)
        dd_btn_row = QHBoxLayout()
        dd_ok = QPushButton("OK")
        dd_cancel = QPushButton("Cancel")
        dd_btn_row.addWidget(dd_ok)
        dd_btn_row.addWidget(dd_cancel)
        dd_btn_row.addStretch()
        dd_layout.addLayout(dd_btn_row)
        dialog_demo.setFixedHeight(140)
        layout.addWidget(dialog_demo)

        # --- 13.2 保存预设对话框 ---
        layout.addWidget(_info_label("PRESET_DIALOG_STYLE — 保存预设对话框 (橙色确认按钮)"))
        preset_demo = QWidget()
        preset_demo.setStyleSheet(PRESET_DIALOG_STYLE)
        pd_layout = QVBoxLayout(preset_demo)
        pd_layout.setContentsMargins(12, 12, 12, 12)
        pd_layout.setSpacing(8)
        pd_layout.addWidget(QLabel("Preset Name (预设名称):"))
        pd_line = QLineEdit()
        pd_line.setPlaceholderText("My Preset...")
        pd_layout.addWidget(pd_line)
        pd_save = QPushButton("Save (保存)")
        pd_layout.addWidget(pd_save)
        preset_demo.setFixedHeight(120)
        layout.addWidget(preset_demo)

        # --- 13.3 管理预设对话框 ---
        layout.addWidget(_info_label("MANAGE_PRESET_DIALOG_STYLE — 管理预设对话框 (含列表)"))
        manage_demo = QWidget()
        manage_demo.setStyleSheet(MANAGE_PRESET_DIALOG_STYLE)
        md_layout = QVBoxLayout(manage_demo)
        md_layout.setContentsMargins(12, 12, 12, 12)
        md_layout.setSpacing(8)
        md_list = QListWidget()
        md_list.addItems(["Preset A", "Preset B (selected)", "Preset C"])
        md_list.item(1).setSelected(True)
        md_list.setFixedHeight(70)
        md_layout.addWidget(md_list)
        md_btn_row = QHBoxLayout()
        md_delete = QPushButton("Delete (删除)")
        md_close = QPushButton("Close (关闭)")
        md_btn_row.addWidget(md_delete)
        md_btn_row.addWidget(md_close)
        md_btn_row.addStretch()
        md_layout.addLayout(md_btn_row)
        manage_demo.setFixedHeight(150)
        layout.addWidget(manage_demo)

        # --- 13.4 上下文菜单样式 ---
        layout.addWidget(_info_label("CONTEXT_MENU_STYLE — 右键菜单覆盖样式 (演示用 QLabel 模拟)"))
        ctx_demo = QWidget()
        ctx_demo.setStyleSheet(f"background: {C.BG_PANEL};")
        ctx_layout = QVBoxLayout(ctx_demo)
        ctx_layout.setContentsMargins(8, 8, 8, 8)
        # 模拟菜单项
        for text in ["Copy (复制)", "Paste (粘贴)", "Delete (删除)"]:
            item = QLabel(f"  {text}")
            item.setStyleSheet(CONTEXT_MENU_STYLE + f"padding: 4px 8px;")
            ctx_layout.addWidget(item)
        ctx_demo.setFixedHeight(100)
        layout.addWidget(ctx_demo)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 14. Script Panel 模板 (脚本编辑器面板)
    #     工具栏按钮、Run/Stop、编辑器、控制台等
    # ------------------------------------------------------------------
    def _build_script_panel(self):
        group, layout = make_section("14. Script Panel 模板 (脚本编辑器面板)")

        # --- 14.1 工具栏按钮 ---
        layout.addWidget(_info_label("SCRIPT_TOOLBAR_BTN_STYLE — 工具栏按钮 (悬停可见边框)"))
        tb_row = QHBoxLayout()
        tb_row.setSpacing(4)
        for text in ["New", "Open", "Save"]:
            btn = QPushButton(text)
            btn.setStyleSheet(SCRIPT_TOOLBAR_BTN_STYLE)
            tb_row.addWidget(btn)
        tb_row.addStretch()
        layout.addLayout(tb_row)

        # --- 14.2 Run 按钮 ---
        layout.addWidget(_info_label("SCRIPT_RUN_BTN_STYLE — Run 运行按钮 (绿色)"))
        run_btn = QPushButton("▶ Run")
        run_btn.setStyleSheet(SCRIPT_RUN_BTN_STYLE)
        run_btn.setFixedHeight(28)
        layout.addWidget(run_btn)

        # --- 14.3 Stop 按钮 ---
        layout.addWidget(_info_label("SCRIPT_STOP_BTN_STYLE — Stop 停止按钮 (红色)"))
        stop_btn = QPushButton("■ Stop")
        stop_btn.setStyleSheet(SCRIPT_STOP_BTN_STYLE)
        stop_btn.setFixedHeight(28)
        layout.addWidget(stop_btn)

        # --- 14.4 状态标签 ---
        layout.addWidget(_info_label("SCRIPT_STATUS_STYLE — 状态标签"))
        status_lbl = QLabel("Ready (就绪)")
        status_lbl.setStyleSheet(SCRIPT_STATUS_STYLE)
        layout.addWidget(status_lbl)

        # --- 14.5 编辑器区域 ---
        layout.addWidget(_info_label("SCRIPT_EDITOR_STYLE — 代码编辑器区域"))
        editor = QPlainTextEdit()
        editor.setPlainText("# Python script example\nprint('Hello Topos!')")
        editor.setStyleSheet(SCRIPT_EDITOR_STYLE)
        editor.setFixedHeight(60)
        editor.setReadOnly(True)
        layout.addWidget(editor)

        # --- 14.6 Console 标签 ---
        layout.addWidget(_info_label("CONSOLE_LABEL_STYLE — 控制台标题标签"))
        console_lbl = QLabel("Console (控制台)")
        console_lbl.setStyleSheet(CONSOLE_LABEL_STYLE)
        layout.addWidget(console_lbl)

        # --- 14.7 控制台区域 ---
        layout.addWidget(_info_label("SCRIPT_CONSOLE_STYLE — 控制台输出区域"))
        console = QPlainTextEdit()
        console.setPlainText(">>> Script executed successfully.\n>>> Output: Hello Topos!")
        console.setStyleSheet(SCRIPT_CONSOLE_STYLE)
        console.setFixedHeight(50)
        console.setReadOnly(True)
        layout.addWidget(console)

        # --- 14.8 清除按钮 ---
        layout.addWidget(_info_label("CONSOLE_CLEAR_BTN_STYLE — 控制台清除按钮"))
        clear_btn = QPushButton("Clear (清除)")
        clear_btn.setStyleSheet(CONSOLE_CLEAR_BTN_STYLE)
        layout.addWidget(clear_btn)

        # --- 14.9 补全弹出列表 ---
        layout.addWidget(_info_label("COMPLETER_POPUP_STYLE — 代码补全弹出列表"))
        completer_demo = QListWidget()
        completer_demo.addItems(["print()", "property", "import"])
        completer_demo.item(0).setSelected(True)
        completer_demo.setStyleSheet(COMPLETER_POPUP_STYLE)
        completer_demo.setFixedHeight(70)
        completer_demo.setFixedWidth(200)
        layout.addWidget(completer_demo)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 15. Library Panel 模板 (媒体库面板)
    #     面板整体、列表视图、模式按钮、缩放标签
    # ------------------------------------------------------------------
    def _build_library_panel(self):
        group, layout = make_section("15. Library Panel 模板 (媒体库面板)")

        # --- 15.1 面板整体 ---
        layout.addWidget(_info_label("LIBRARY_PANEL_STYLE — 面板整体 (含搜索框/ToolButton/ComboBox)"))
        lib_demo = QWidget()
        lib_demo.setStyleSheet(LIBRARY_PANEL_STYLE)
        lib_demo.setFixedHeight(100)
        ld_layout = QVBoxLayout(lib_demo)
        ld_layout.setContentsMargins(8, 8, 8, 8)
        ld_layout.setSpacing(6)
        # 搜索框
        search = QLineEdit()
        search.setPlaceholderText("Search media... (搜索媒体)")
        ld_layout.addWidget(search)
        # 工具栏行
        tb_row = QHBoxLayout()
        for icon in ["📁", "🔍", "⚙"]:
            tb = QToolButton()
            tb.setText(icon)
            tb_row.addWidget(tb)
        tb_row.addStretch()
        # 下拉框
        combo = QComboBox()
        combo.addItems(["All", "Images", "Videos"])
        combo.setFixedWidth(100)
        tb_row.addWidget(combo)
        ld_layout.addLayout(tb_row)
        layout.addWidget(lib_demo)

        # --- 15.2 列表视图 ---
        layout.addWidget(_info_label("LIBRARY_LIST_VIEW_STYLE — 媒体列表视图"))
        lib_list = QListWidget()
        lib_list.addItems(["clip_001.mp4", "clip_002.mov", "image_003.png", "image_004.jpg"])
        lib_list.setStyleSheet(LIBRARY_LIST_VIEW_STYLE)
        lib_list.setFixedHeight(80)
        layout.addWidget(lib_list)

        # --- 15.3 模式切换按钮 ---
        layout.addWidget(_info_label("LIBRARY_MODE_BTN_STYLE — 列表/图标/缩略图模式按钮 (可选中)"))
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        for i, text in enumerate(["☰", "▦", "▣"]):
            btn = QToolButton()
            btn.setText(text)
            btn.setCheckable(True)
            if i == 0:
                btn.setChecked(True)
            btn.setStyleSheet(LIBRARY_MODE_BTN_STYLE)
            mode_row.addWidget(btn)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # --- 15.4 缩放标签 ---
        layout.addWidget(_info_label("LIBRARY_ZOOM_LABEL_STYLE — 缩放级别标签"))
        zoom_lbl = QLabel("Zoom: 100%")
        zoom_lbl.setStyleSheet(LIBRARY_ZOOM_LABEL_STYLE)
        layout.addWidget(zoom_lbl)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 16. Plugin Panel 模板 (插件效果面板)
    #     标题、AI效果组、错误组、处理按钮等
    # ------------------------------------------------------------------
    def _build_plugin_panel(self):
        group, layout = make_section("16. Plugin Panel 模板 (插件效果面板)")

        # --- 16.1 插件面板标题 ---
        layout.addWidget(_info_label("PLUGIN_TITLE_STYLE — 插件面板标题"))
        plugin_title = QLabel("EFFECTS")
        plugin_title.setStyleSheet(PLUGIN_TITLE_STYLE)
        layout.addWidget(plugin_title)

        # --- 16.2 分隔线 ---
        layout.addWidget(_info_label("SEPARATOR_LINE_STYLE — 分隔线 (复用)"))
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(SEPARATOR_LINE_STYLE)
        layout.addWidget(sep)

        # --- 16.3 空状态 ---
        layout.addWidget(_info_label("EMPTY_STATE_LABEL_STYLE — 无插件时的空状态提示"))
        empty_lbl = QLabel("No plugins loaded (未加载插件)")
        empty_lbl.setStyleSheet(EMPTY_STATE_LABEL_STYLE)
        layout.addWidget(empty_lbl)

        # --- 16.4 错误组 ---
        layout.addWidget(_info_label("ERROR_GROUP_STYLE + ERROR_LABEL_STYLE — 错误提示组"))
        error_group = QGroupBox("Error (错误)")
        error_group.setStyleSheet(ERROR_GROUP_STYLE)
        eg_layout = QVBoxLayout(error_group)
        error_lbl = QLabel("Plugin load failed: missing dependency (插件加载失败)")
        error_lbl.setStyleSheet(ERROR_LABEL_STYLE)
        eg_layout.addWidget(error_lbl)
        layout.addWidget(error_group)

        # --- 16.5 AI 效果组 ---
        layout.addWidget(_info_label("AI_EFFECT_GROUP_STYLE — AI 效果分组框 (蓝色边框)"))
        ai_group = QGroupBox("AI Enhancement (AI 增强)")
        ai_group.setStyleSheet(AI_EFFECT_GROUP_STYLE)
        ag_layout = QVBoxLayout(ai_group)
        ag_layout.setContentsMargins(8, 16, 8, 8)
        # Enable 标签
        enable_lbl = QLabel("Processing")
        enable_lbl.setStyleSheet(ENABLE_LABEL_STYLE)
        ag_layout.addWidget(enable_lbl)
        # Scale 标签
        scale_lbl = QLabel("Scale: 4x")
        scale_lbl.setStyleSheet(SCALE_LABEL_STYLE)
        ag_layout.addWidget(scale_lbl)
        layout.addWidget(ai_group)

        # --- 16.6 AI 处理按钮 ---
        layout.addWidget(_info_label("AI_PROCESS_BTN_STYLE — AI 处理按钮 (蓝色)"))
        process_btn = QPushButton("Process (处理)")
        process_btn.setObjectName("ai_process_btn")
        process_btn.setStyleSheet(AI_PROCESS_BTN_STYLE)
        process_btn.setFixedHeight(30)
        layout.addWidget(process_btn)

        self.main_layout.addWidget(group)

    # ------------------------------------------------------------------
    # 17. HSL / LUT / Shortcut / Mixer 其他模板
    #     HSL 曲线下拉、LUT 列表、快捷键编辑器、混音 SpinBox
    # ------------------------------------------------------------------
    def _build_hsl_lut_shortcut(self):
        group, layout = make_section("17. HSL / LUT / Shortcut / Mixer 其他模板")

        # --- 17.1 HSL 模式下拉框 ---
        layout.addWidget(_info_label("HSL_MODE_COMBO_STYLE — HSL 曲线模式下拉框"))
        hsl_combo = QComboBox()
        hsl_combo.addItems(["Hue", "Saturation", "Lightness"])
        hsl_combo.setStyleSheet(HSL_MODE_COMBO_STYLE)
        layout.addWidget(hsl_combo)

        # --- 17.2 HSL 信息标签 ---
        layout.addWidget(_info_label("HSL_INFO_LABEL_STYLE — HSL 曲线信息标签 (绿色)"))
        hsl_info = QLabel("H: 180°")
        hsl_info.setStyleSheet(HSL_INFO_LABEL_STYLE)
        layout.addWidget(hsl_info)

        # --- 17.3 LUT 工具栏标签 ---
        layout.addWidget(_info_label("LUT_TOOLBAR_LABEL_STYLE — LUT 画廊工具栏标签"))
        lut_lbl = QLabel("LUT Gallery (LUT 画廊)")
        lut_lbl.setStyleSheet(LUT_TOOLBAR_LABEL_STYLE)
        layout.addWidget(lut_lbl)

        # --- 17.4 LUT 列表 ---
        layout.addWidget(_info_label("LUT_LIST_STYLE — LUT 列表控件"))
        lut_list = QListWidget()
        lut_list.addItems(["cinematic.cube", "test03.cube", "07.cube", "08.cube"])
        lut_list.item(0).setSelected(True)
        lut_list.setStyleSheet(LUT_LIST_STYLE)
        lut_list.setFixedHeight(90)
        layout.addWidget(lut_list)

        # --- 17.5 快捷键编辑器 ---
        layout.addWidget(_info_label("SHORTCUT_EDITOR_STYLE — 快捷键编辑框 (橙色边框)"))
        key_edit = QKeySequenceEdit()
        key_edit.setStyleSheet(SHORTCUT_EDITOR_STYLE)
        layout.addWidget(key_edit)

        # --- 17.6 混音器 SpinBox ---
        layout.addWidget(_info_label("MIXER_SPIN_STYLE — 混音器整数输入框"))
        mixer_spin = QSpinBox()
        mixer_spin.setRange(-60, 12)
        mixer_spin.setValue(0)
        mixer_spin.setStyleSheet(MIXER_SPIN_STYLE)
        layout.addWidget(mixer_spin)

        # --- 17.7 DragValue 标签 ---
        layout.addWidget(_info_label("DRAG_VALUE_LABEL_STYLE — DragValue 描述标签"))
        dv_lbl = QLabel("Exposure (曝光)")
        dv_lbl.setStyleSheet(DRAG_VALUE_LABEL_STYLE)
        layout.addWidget(dv_lbl)

        self.main_layout.addWidget(group)


# ============================================================================
# Entry point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = StylePreviewWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
