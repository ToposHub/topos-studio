"""
Edit Properties Panel — 完全镜像 Composite 页面 Properties 面板架构。

架构（与 src/features/composite/ui/node_properties_panel.py 一致）：
- 每个参数分组渲染成一个可折叠的 EditSectionWidget（头部工具栏 + 内容区）。
- 数值行用 DragValue（固定 64px）+ ToposClickableSlider（stretch=1 自适应宽度）。
- 下拉行用 ChevronComboBox（自绘 chevron + 全局弹出样式）。
- 面板不限最大宽度，仅 setMinimumWidth(280)，可自由拖动 dock 分隔条。

对外接口契约（必须保留，供 edit_controller.py 调用）：
  方法: set_clip / set_clips / set_transition / set_track / set_asset /
        set_timeline / clear / update_color_status / update_composite_status
  信号: clip_param_changed / reset_transform_requested /
        effect_param_changed / effect_enabled_changed / effect_remove_requested /
        transition_param_changed / track_param_changed / keyframe_toggled
  属性: _selected_effect_id（edit_controller.py:975 直接写入）
"""

import logging
from dataclasses import replace
from typing import Optional, List, Any, Dict, Callable

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QLineEdit,
    QToolButton, QCheckBox,
    QPlainTextEdit, QPushButton, QColorDialog, QComboBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QPointF, QPoint, QMimeData, QEvent, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPolygonF, QFont, QFontMetrics,
    QFontDatabase, QPixmap, QDrag,
)

from src.shared.i18n.i18n import I18N
from src.shared.media.metadata import format_codec_label
from src.shared.assets.icon_manager import (
    get_icon, IconColors, make_link_button,
)
from src.shared.ui.widgets import (
    ChevronComboBox, DragValue, ToggleSwitch,
)
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.ui.style_sheet import (
    C,
    NODE_COLLAPSE_BTN_STYLE,
    TOOLBAR_FLAT_BTN_STYLE,
    EMPTY_STATE_LABEL_STYLE,
    EDIT_PROPERTIES_CONTENT_STYLE,
    EDIT_PROPERTIES_SECTION_CONTENT_STYLE,
    EDIT_PROPERTIES_SECTION_DIVIDER_STYLE,
    EDIT_PROPERTIES_SECTION_HEADER_STYLE,
    EDIT_PROPERTIES_ROW_STYLE,
    EDIT_PROPERTIES_SECTION_STYLE,
    EDIT_PROPERTIES_SECTION_TITLE_STYLE,
    EDIT_PROPERTIES_TOOL_BTN_STYLE,
    apply_combo_dropdown_style,
)
from src.features.composite.nodes.base_node import ParamDef
from src.features.edit.core.clip_params import (
    ClipBlendMode,
    RawParams,
    SpeedParams,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance, TransitionInstance, EditTrack, TimelineSequence,
    effective_subtitle_font_size,
)
from src.features.edit.core.project import MediaAsset
from src.features.edit.core.effect_instance import ParamSchema
from src.features.edit.core.effect_registry import get_effect_registry
from src.features.edit.core.transition_catalog import (
    TRANSITION_TYPE_OPTIONS as CATALOG_TRANSITION_TYPE_OPTIONS,
    TRANSITION_TYPE_LABELS as CATALOG_TRANSITION_TYPE_LABELS,
)
from src.features.edit.core.audio_transition_catalog import (
    AUDIO_TRANSITION_TYPE_OPTIONS as CATALOG_AUDIO_TRANSITION_TYPE_OPTIONS,
    AUDIO_TRANSITION_LABEL_MAP as CATALOG_AUDIO_TRANSITION_LABEL_MAP,
)

logger = logging.getLogger(__name__)


# ================================================================
# 常量
# ================================================================

# 数值格固定宽度（与 composite 面板一致）— 保持各数值行左对齐。
_VALUE_FIXED_WIDTH = 64

# 宽于此值时单行 slider 显示（与 composite 一致，保证窄面板不裁切）。
_SLIDER_SHOW_WIDTH = 380

# 注：X/Y 链锁按钮原用 Unicode ⛓ + _CHAIN_STYLE，已统一为
# icon_manager.make_link_button 小锁图标（锁定橙/解锁灰），常量已移除。


# ================================================================
# ParamDef 适配层 — 把 edit 的 dataclass 参数描述成 composite 三元组
# ================================================================

# Transform 组（像素空间模型，与合成页 TransformNode / 字幕一致）。
# 以 UHD 画布为上限，并为画面外定位保留约一倍安全边界。此前统一的
# ±8192 会让 DragValue 的每个鼠标像素对应过大的位移（1080p 下尤其明显）。
_TRANSFORM_X_LIMIT = 4096.0
_TRANSFORM_Y_LIMIT = 2160.0

TRANSFORM_PARAM_DEFS: Dict[str, ParamDef] = {
    "translate_x": ParamDef(name="translate_x", ptype="float",
                            min_val=-_TRANSFORM_X_LIMIT, max_val=_TRANSFORM_X_LIMIT,
                            step=1.0, decimals=1, default=0.0),
    "translate_y": ParamDef(name="translate_y", ptype="float",
                            min_val=-_TRANSFORM_Y_LIMIT, max_val=_TRANSFORM_Y_LIMIT,
                            step=1.0, decimals=1, default=0.0),
    "scale_x": ParamDef(name="scale_x", ptype="float",
                        min_val=0.01, max_val=5.0, step=0.01, decimals=2, default=1.0),
    "scale_y": ParamDef(name="scale_y", ptype="float",
                        min_val=0.01, max_val=5.0, step=0.01, decimals=2, default=1.0),
    "rotation": ParamDef(name="rotation", ptype="float",
                         min_val=-180.0, max_val=180.0, step=0.1, decimals=1, default=0.0),
    "pivot_mode": ParamDef(name="pivot_mode", ptype="option", default="center"),
    "pivot_x": ParamDef(name="pivot_x", ptype="float",
                        min_val=-_TRANSFORM_X_LIMIT, max_val=_TRANSFORM_X_LIMIT,
                        step=1.0, decimals=1, default=0.0),
    "pivot_y": ParamDef(name="pivot_y", ptype="float",
                        min_val=-_TRANSFORM_Y_LIMIT, max_val=_TRANSFORM_Y_LIMIT,
                        step=1.0, decimals=1, default=0.0),
}

# Transform 锚点模式选项（center=画面中心 / custom=自定义坐标）。
# 与 composite TransformNode 及字幕的 _SUB_PIVOT_MODES 保持一致。
_TRANSFORM_PIVOT_MODES: List[str] = ["center", "custom"]
_TRANSFORM_PIVOT_LABELS: List[str] = ["Center", "Custom"]

# Crop 组：left/right/top/bottom 用百分比 0-50 展示（内部存 0-0.5 归一化），feather 0-100px
CROP_PARAM_DEFS: Dict[str, ParamDef] = {
    "left": ParamDef(name="left", ptype="float", min_val=0.0, max_val=50.0, step=0.1, decimals=1, default=0.0),
    "right": ParamDef(name="right", ptype="float", min_val=0.0, max_val=50.0, step=0.1, decimals=1, default=0.0),
    "top": ParamDef(name="top", ptype="float", min_val=0.0, max_val=50.0, step=0.1, decimals=1, default=0.0),
    "bottom": ParamDef(name="bottom", ptype="float", min_val=0.0, max_val=50.0, step=0.1, decimals=1, default=0.0),
    "feather": ParamDef(name="feather", ptype="float", min_val=0.0, max_val=100.0, step=0.1, decimals=1, default=0.0),
}

# Opacity 组：百分比 0-100（内部存 0-1.0 归一化）
OPACITY_PARAM_DEFS: Dict[str, ParamDef] = {
    "value": ParamDef(name="value", ptype="float", min_val=0.0, max_val=100.0, step=0.1, decimals=1, default=100.0),
}

# TRAW（批 2）：RAW 参数组（WB 增益 / EI 曝光 / RC3 色彩管理）。
# black_level 为码值域：12-bit 源上限 4095、16-bit 源上限 65535——
# 面板统一放开到 65535（debayer 侧减除后 clamp，越限仅画面全黑可
# 自行回退，不做位深分支判定）。
RAW_PARAM_DEFS: Dict[str, ParamDef] = {
    "wb_gain_r": ParamDef(name="wb_gain_r", ptype="float", min_val=0.25, max_val=4.0, step=0.01, decimals=2, default=1.0),
    "wb_gain_g": ParamDef(name="wb_gain_g", ptype="float", min_val=0.25, max_val=4.0, step=0.01, decimals=2, default=1.0),
    "wb_gain_b": ParamDef(name="wb_gain_b", ptype="float", min_val=0.25, max_val=4.0, step=0.01, decimals=2, default=1.0),
    "exposure_stops": ParamDef(name="exposure_stops", ptype="float", min_val=-4.0, max_val=4.0, step=0.05, decimals=2, default=0.0),
    "display_ootf": ParamDef(name="display_ootf", ptype="bool", default=True),
    "black_level": ParamDef(name="black_level", ptype="float", min_val=0.0, max_val=65535.0, step=1.0, decimals=1, default=0.0),
    "color_space": ParamDef(name="color_space", ptype="option", default="native"),
    "gamma": ParamDef(name="gamma", ptype="option", default="preview-2.2"),
}

# RC3：IDT 目标色空间选项（"native" + camera_gamut_matrices 8 预设）。
# 选项 id 与 CAMERA_GAMUTS gamut_id 严格一致；标签走 i18n。
_RAW_COLOR_SPACES: List[str] = [
    "native",
    "arri-awg3", "sony-sgamut3", "panasonic-vgamut", "davinci-wg",
    "red-rwg", "canon-cg", "fuji-fgamut", "bmd-wg",
]

# RC3：查看伽马选项（debayer 线性结果的显示编码；导出与预览同语义）。
_RAW_GAMMAS: List[str] = ["preview-2.2", "srgb", "linear"]

# Speed 组：speed 0.1-4.0
SPEED_PARAM_DEFS: Dict[str, ParamDef] = {
    "speed": ParamDef(name="speed", ptype="float", min_val=0.1, max_val=8.0, step=0.01, decimals=2, default=1.0),
    "reverse": ParamDef(name="reverse", ptype="bool", default=False),
    "preserve_pitch": ParamDef(name="preserve_pitch", ptype="bool", default=False),
}

# ================================================================
# P10.3 字幕片段 ParamDef（与 TextNode.PARAM_DEFS 同步）
# 字段名严格对齐 SubtitleClip，参数范围与 composite 文本节点一致。
# ================================================================

# 字重/对齐选项（与 text_node.py 同步）
_SUB_FONT_WEIGHTS: List[str] = ["light", "normal", "demibold", "bold", "black"]
_SUB_HALIGNS: List[str] = ["left", "center", "right"]
_SUB_VALIGNS: List[str] = ["top", "middle", "bottom"]
_SUB_PIVOT_MODES: List[str] = ["center", "custom"]

# 字幕字体参数
SUBTITLE_FONT_PARAM_DEFS: Dict[str, ParamDef] = {
    "font_family": ParamDef(name="font_family", ptype="str", default="Arial"),
    "font_size": ParamDef(name="font_size", ptype="int",
                          min_val=1, max_val=2048, step=1,
                          default=effective_subtitle_font_size(0)),
    "font_weight": ParamDef(name="font_weight", ptype="option", default="normal"),
    "italic": ParamDef(name="italic", ptype="bool", default=False),
}

# 字幕排版参数
SUBTITLE_LAYOUT_PARAM_DEFS: Dict[str, ParamDef] = {
    "halign": ParamDef(name="halign", ptype="option", default="center"),
    "valign": ParamDef(name="valign", ptype="option", default="bottom"),
    "letter_spacing": ParamDef(name="letter_spacing", ptype="float",
                               min_val=-50.0, max_val=200.0, step=0.5, decimals=1, default=0.0),
    "line_spacing": ParamDef(name="line_spacing", ptype="float",
                             min_val=0.5, max_val=3.0, step=0.05, decimals=2, default=1.0),
}

# 字幕变换参数（translate 像素空间，scale 0.01~20，rotation 度）
SUBTITLE_TRANSFORM_PARAM_DEFS: Dict[str, ParamDef] = {
    "translate_x": ParamDef(name="translate_x", ptype="float",
                            min_val=-8192.0, max_val=8192.0, step=1.0, decimals=1, default=0.0),
    "translate_y": ParamDef(name="translate_y", ptype="float",
                            min_val=-8192.0, max_val=8192.0, step=1.0, decimals=1, default=0.0),
    "scale_x": ParamDef(name="scale_x", ptype="float",
                        min_val=0.01, max_val=20.0, step=0.01, decimals=2, default=1.0),
    "scale_y": ParamDef(name="scale_y", ptype="float",
                        min_val=0.01, max_val=20.0, step=0.01, decimals=2, default=1.0),
    "rotation": ParamDef(name="rotation", ptype="float",
                         min_val=-360.0, max_val=360.0, step=0.1, decimals=1, default=0.0),
    "pivot_mode": ParamDef(name="pivot_mode", ptype="option", default="center"),
    "pivot_x": ParamDef(name="pivot_x", ptype="float",
                        min_val=-8192.0, max_val=8192.0, step=1.0, decimals=1, default=0.0),
    "pivot_y": ParamDef(name="pivot_y", ptype="float",
                        min_val=-8192.0, max_val=8192.0, step=1.0, decimals=1, default=0.0),
}

# 字幕填充色（HDR float）
SUBTITLE_FILL_PARAM_DEFS: Dict[str, ParamDef] = {
    "fill_r": ParamDef(name="fill_r", ptype="float",
                       min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
    "fill_g": ParamDef(name="fill_g", ptype="float",
                       min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
    "fill_b": ParamDef(name="fill_b", ptype="float",
                       min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
    "fill_a": ParamDef(name="fill_a", ptype="float",
                       min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
}

# 字幕描边
SUBTITLE_OUTLINE_PARAM_DEFS: Dict[str, ParamDef] = {
    "outline_enable": ParamDef(name="outline_enable", ptype="bool", default=False),
    "outline_width": ParamDef(name="outline_width", ptype="float",
                              min_val=0.0, max_val=64.0, step=0.5, decimals=1, default=4.0),
    "outline_r": ParamDef(name="outline_r", ptype="float",
                          min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "outline_g": ParamDef(name="outline_g", ptype="float",
                          min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "outline_b": ParamDef(name="outline_b", ptype="float",
                          min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "outline_a": ParamDef(name="outline_a", ptype="float",
                          min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
}

# 字幕阴影
SUBTITLE_SHADOW_PARAM_DEFS: Dict[str, ParamDef] = {
    "shadow_enable": ParamDef(name="shadow_enable", ptype="bool", default=False),
    "shadow_offset_x": ParamDef(name="shadow_offset_x", ptype="float",
                                min_val=-512.0, max_val=512.0, step=0.5, decimals=1, default=8.0),
    "shadow_offset_y": ParamDef(name="shadow_offset_y", ptype="float",
                                min_val=-512.0, max_val=512.0, step=0.5, decimals=1, default=8.0),
    "shadow_blur": ParamDef(name="shadow_blur", ptype="float",
                            min_val=0.0, max_val=128.0, step=0.5, decimals=1, default=8.0),
    "shadow_r": ParamDef(name="shadow_r", ptype="float",
                         min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "shadow_g": ParamDef(name="shadow_g", ptype="float",
                         min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "shadow_b": ParamDef(name="shadow_b", ptype="float",
                         min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=0.0),
    "shadow_a": ParamDef(name="shadow_a", ptype="float",
                         min_val=0.0, max_val=65535.0, step=0.01, decimals=2, default=1.0),
}

# Transition 组选项
TRANSITION_TYPE_OPTIONS = list(CATALOG_TRANSITION_TYPE_OPTIONS)
TRANSITION_TYPE_LABELS = list(CATALOG_TRANSITION_TYPE_LABELS)
# Audio transition options (crossfades) — Type 下拉在选中音频转场时切换到此。
AUDIO_TRANSITION_TYPE_OPTIONS = list(CATALOG_AUDIO_TRANSITION_TYPE_OPTIONS)
AUDIO_TRANSITION_TYPE_LABELS = [
    CATALOG_AUDIO_TRANSITION_LABEL_MAP[t] for t in AUDIO_TRANSITION_TYPE_OPTIONS
]
TRANSITION_DIRECTION_OPTIONS = ["left", "right", "up", "down"]
TRANSITION_DIRECTION_LABELS = ["From Left", "From Right", "From Top", "From Bottom"]
TRANSITION_ALIGN_OPTIONS = ["centered", "start_on_cut", "end_on_cut"]
TRANSITION_ALIGN_LABELS = ["Center on Cut", "Start on Cut", "End on Cut"]
TRANSITION_CURVE_OPTIONS = ["linear", "ease_in", "ease_out", "ease_in_out"]
TRANSITION_CURVE_LABELS = ["Linear", "Ease In", "Ease Out", "Ease In-Out"]

# AUD-20008: Transition section Reset 的默认值表（与 _build_transition_section
# 各 ParamDef 的 default 同源；transition_type 是用户选定的转场类型，不参与
# 重置）。音频转场无空间参数（curve/direction/softness）。
_TRANSITION_RESET_DEFAULTS: Dict[str, Any] = {
    "duration": 12,
    "alignment": TRANSITION_ALIGN_OPTIONS[0],
    "curve": TRANSITION_CURVE_OPTIONS[0],
    "direction": TRANSITION_DIRECTION_OPTIONS[0],
    "softness": 0.02,
}
_TRANSITION_SPATIAL_KEYS = ("curve", "direction", "softness")


# ================================================================
# KeyframeIndicatorButton — edit 特有菱形关键帧按钮（自绘，保留）
# ================================================================

class KeyframeIndicatorButton(QToolButton):
    """菱形关键帧切换按钮（自绘，edit 特有）。

    视觉状态：
    - 空心灰边：无关键帧
    - 空心黄边：已动画化但当前帧无关键帧
    - 实心橙：当前帧有关键帧
    """

    keyframe_toggled = pyqtSignal(str, int, float)  # param_name, frame, value
    DIAMOND_SIZE = 10

    def __init__(self, param_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._param_name = param_name
        self._has_keyframe_at_frame = False
        self._is_animated = False
        self._current_frame = 0
        self._current_value = 0.0
        self.setFixedSize(18, 18)
        self.setToolTip(I18N.tr("edit.keyframe.toggle", "Toggle keyframe at current frame"))
        self.clicked.connect(self._on_clicked)

    def set_state(self, has_keyframe_at_frame: bool, is_animated: bool,
                  current_frame: int, current_value: float) -> None:
        self._has_keyframe_at_frame = has_keyframe_at_frame
        self._is_animated = is_animated
        self._current_frame = current_frame
        self._current_value = current_value
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        diamond = QPolygonF([
            QPointF(cx, cy - self.DIAMOND_SIZE / 2),
            QPointF(cx + self.DIAMOND_SIZE / 2, cy),
            QPointF(cx, cy + self.DIAMOND_SIZE / 2),
            QPointF(cx - self.DIAMOND_SIZE / 2, cy),
        ])
        if self._has_keyframe_at_frame:
            fill, border = QColor(C.ORANGE), QColor(C.ORANGE_HOVER)
        elif self._is_animated:
            fill, border = QColor("#252525"), QColor("#ffcc00")
        else:
            fill, border = QColor("#252525"), QColor(C.TEXT_HINT)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(border, 1.5))
        painter.drawPolygon(diamond)

    def _on_clicked(self) -> None:
        self.keyframe_toggled.emit(self._param_name, self._current_frame, self._current_value)

    def param_name(self) -> str:
        return self._param_name


# ================================================================
# EditSectionWidget — 精简版 NodeSectionWidget（edit 专用）
# ================================================================

class EditSectionWidget(QWidget):
    """可折叠参数 section（镜像 composite NodeSectionWidget 的视觉外壳）。

    与 composite 的差异：
    - 去掉 save preset / color indicator / enable toggle（edit 分组无这些概念），
      保留 collapse / title / pin / lock / reset。
    - 参数行构建在面板层完成（通过 parent_panel 回调），section 只负责外壳 + 内容容器。
    """

    # 信号：section 内任意参数变化。section_id 形如 "clip:<id>:transform"。
    param_changed = pyqtSignal(str, str, object)  # section_id, param_name, value
    reset_requested = pyqtSignal(str)             # section_id
    # Effect-section-only signals (emitted by the header enable/delete buttons
    # added when effect_mode=True).
    effect_enable_toggled = pyqtSignal(str, bool)  # section_id, enabled
    effect_delete_requested = pyqtSignal(str)      # section_id
    # P-clip-FX: 「打开面板」按钮信号 — 打开该 audio 效果的独立参数对话框。
    # payload: (section_id, effect_type)
    effect_open_dialog_requested = pyqtSignal(str, str)

    def __init__(self, section_id: str, title: str,
                 parameters: Optional[Dict[str, Any]] = None,
                 param_defs: Optional[Dict[str, ParamDef]] = None,
                 param_options: Optional[Dict[str, list]] = None,
                 parent_panel: Optional["EditPropertiesPanel"] = None,
                 node_color: str = "#666666",
                 collapsed: bool = False,
                 effect_mode: bool = False,
                 parent=None):
        super().__init__(parent)
        self.section_id = section_id
        self.title = title
        self.parameters: Dict[str, Any] = dict(parameters) if parameters else {}
        self.param_defs: Dict[str, ParamDef] = param_defs or {}
        self.param_options: Dict[str, list] = param_options or {}
        self._parent_panel = parent_panel
        self.node_color = node_color
        self._is_collapsed = collapsed
        self._section_enabled = True
        self._effect_mode = effect_mode
        self._effect_enabled = True
        self._drag_start_pos: Optional[QPoint] = None  # FX-H04-UI drag reorder
        # P-clip-FX: 该 section 对应的 effect_type（仅 effect_mode 有效）。
        # 由面板在创建 section 后通过 set_effect_type 设置，用于决定是否
        # 显示「打开面板」按钮以及打开哪种 dialog。
        self.effect_type: str = ""
        self._param_widgets: Dict[str, Any] = {}
        # Numeric-row sliders live between label and DragValue and stretch to
        # fill the available width (mirrors composite).  Unlike composite's
        # XY-pair sliders they are ALWAYS visible: hiding them (as an earlier
        # attempt did) collapses each row to ``[label][DragValue]+whitespace``
        # and produces exactly the "everything left-aligned" look we are fixing.
        self._slidable_sliders: list = []
        # Shared minimum label width so every row's label right-edge aligns
        # (mirrors composite's _label_min_width).  Computed by the panel before
        # the section builder runs, from the widest param display name.
        self._label_min_width: int = 60

        # Inspector sections use their own compact, neutral global template.
        # NodeSectionWidget's full-width colour gradient is retained for the
        # Composite page, where a section represents a single graph node.
        self.setObjectName("editPropertiesSection")
        self.setStyleSheet(EDIT_PROPERTIES_SECTION_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 标题上下的全宽细分隔线 + 头部工具栏 ──
        # 使用独立控件而非 header border，避免线条被标题内的控件视觉切碎。
        self._header_top_divider = QWidget()
        self._header_top_divider.setFixedHeight(2)
        self._header_top_divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        layout.addWidget(self._header_top_divider)

        self._header = QWidget()
        self._header.setObjectName("editPropertiesSectionHeader")
        self._header.setFixedHeight(24)
        self._build_header()
        layout.addWidget(self._header)

        self._header_bottom_divider = QWidget()
        self._header_bottom_divider.setFixedHeight(2)
        self._header_bottom_divider.setStyleSheet(EDIT_PROPERTIES_SECTION_DIVIDER_STYLE)
        layout.addWidget(self._header_bottom_divider)

        # ── 内容区 ──
        self._content_widget = QWidget()
        self._content_widget.setObjectName("editPropertiesSectionContent")
        self._content_widget.setStyleSheet(EDIT_PROPERTIES_SECTION_CONTENT_STYLE)
        self._content_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(8, 5, 8, 5)
        self._content_layout.setSpacing(3)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._content_widget)

        self._update_header_style()

        # Apply initial collapsed state (must run after _content_widget and
        # _collapse_btn exist). When collapsed=True the section starts folded
        # — used for native param sections when effects are present, and for
        # non-selected effect sections.
        if self._is_collapsed:
            self._content_widget.setVisible(False)
            self._collapse_btn.setText("▶")

    def _build_header(self) -> None:
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(7, 0, 3, 0)
        header_layout.setSpacing(2)

        # FX-H04-UI: Drag handle（仅 effect section 显示）— 拖拽排序入口。
        if self._effect_mode:
            self._drag_handle = QToolButton()
            self._drag_handle.setIcon(get_icon('fa5s.grip-vertical', IconColors.WHITE))
            self._drag_handle.setIconSize(QSize(12, 12))
            self._drag_handle.setFixedSize(18, 18)
            self._drag_handle.setStyleSheet(EDIT_PROPERTIES_TOOL_BTN_STYLE)
            self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
            # QToolButton consumes mouse events, so the parent section's
            # mousePressEvent/mouseMoveEvent never sees a real grip drag.
            # Observe the handle directly and start QDrag from its events.
            self._drag_handle.installEventFilter(self)
            self._drag_handle.setToolTip(
                I18N.tr("edit.properties.drag_reorder", "Drag to reorder effect")
            )
            header_layout.addWidget(self._drag_handle)
        else:
            self._drag_handle = None

        # Collapse 按钮
        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("▼")
        self._collapse_btn.setFixedSize(18, 18)
        self._collapse_btn.setStyleSheet(NODE_COLLAPSE_BTN_STYLE)
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        header_layout.addWidget(self._collapse_btn)

        # All Edit groups use the same compact on/off affordance.  Effects
        # route this to their real render-bypass state; native groups use it
        # to enable or disable editing of that parameter group.
        self._section_enable_toggle = ToggleSwitch(checked=True)
        self._section_enable_toggle.setFixedSize(24, 14)
        self._section_enable_toggle.setToolTip(
            I18N.tr("edit.properties.enable_section", "Enable section controls")
        )
        header_layout.addWidget(self._section_enable_toggle)

        # 标题
        self._title_label = QLabel(self.title)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        # Effect-only controls.  The common leading toggle above is the
        # effect's real enable control, avoiding a duplicate eye icon.
        if self._effect_mode:
            # P-clip-FX: 「打开面板」按钮（仅 mixer audio 效果显示）。
            # 默认隐藏，set_effect_type 后按 effect_type 是否在 mixer 集合内
            # 决定可见性。
            self._effect_open_dialog_btn = QToolButton()
            self._effect_open_dialog_btn.setIcon(get_icon('fa5s.sliders-h', IconColors.WHITE))
            self._effect_open_dialog_btn.setIconSize(QSize(14, 14))
            self._effect_open_dialog_btn.setFixedSize(20, 20)
            self._effect_open_dialog_btn.setStyleSheet(EDIT_PROPERTIES_TOOL_BTN_STYLE)
            self._effect_open_dialog_btn.setToolTip(
                I18N.tr("edit.properties.open_effect_panel", "Open effect panel")
            )
            self._effect_open_dialog_btn.clicked.connect(self._on_effect_open_dialog_clicked)
            self._effect_open_dialog_btn.setVisible(False)
            header_layout.addWidget(self._effect_open_dialog_btn)

            self._effect_enable_btn = self._section_enable_toggle
            self._effect_enable_btn.setToolTip(
                I18N.tr("edit.properties.toggle_effect", "Toggle effect on/off")
            )
            self._effect_enable_btn.toggled.connect(
                lambda _enabled: self._on_effect_enable_clicked()
            )

            self._effect_delete_btn = QToolButton()
            self._effect_delete_btn.setIcon(get_icon('fa5s.trash-alt', IconColors.WHITE))
            self._effect_delete_btn.setIconSize(QSize(14, 14))
            self._effect_delete_btn.setFixedSize(20, 20)
            self._effect_delete_btn.setStyleSheet(EDIT_PROPERTIES_TOOL_BTN_STYLE)
            self._effect_delete_btn.setToolTip(
                I18N.tr("edit.properties.remove_effect", "Remove effect")
            )
            self._effect_delete_btn.clicked.connect(self._on_effect_delete_clicked)
            header_layout.addWidget(self._effect_delete_btn)
        else:
            self._effect_enable_btn = None
            self._effect_delete_btn = None
            self._effect_open_dialog_btn = None
            self._section_enable_toggle.toggled.connect(self._on_section_enable_toggled)

        # Reset 按钮（仅当有可重置参数时显示）
        self._reset_btn = QToolButton()
        self._reset_btn.setIcon(get_icon('fa5s.undo', IconColors.WHITE))
        self._reset_btn.setIconSize(QSize(14, 14))
        self._reset_btn.setFixedSize(20, 20)
        self._reset_btn.setStyleSheet(EDIT_PROPERTIES_TOOL_BTN_STYLE)
        self._reset_btn.setToolTip(I18N.tr("edit.properties.reset", "Reset"))
        self._reset_btn.clicked.connect(lambda: self.reset_requested.emit(self.section_id))
        header_layout.addWidget(self._reset_btn)

    def _update_header_style(self) -> None:
        """Apply the compact neutral inspector header template."""
        self._header.setStyleSheet(EDIT_PROPERTIES_SECTION_HEADER_STYLE)
        self._title_label.setStyleSheet(EDIT_PROPERTIES_SECTION_TITLE_STYLE)
        self._collapse_btn.setStyleSheet(NODE_COLLAPSE_BTN_STYLE)

    # ── 公共 API ──

    def clear_content(self) -> None:
        """清空内容区（断开信号 + 移除控件，避免悬空 paint 崩溃）。"""
        while self._content_layout.count() > 0:
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.disconnect()
                except (TypeError, RuntimeError):
                    pass
                w.setParent(None)
                w.deleteLater()
        self._param_widgets.clear()

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def update_parameters(self, parameters: Dict[str, Any]) -> None:
        """更新参数值（keys 不变时增量刷新控件，否则由面板重建）。"""
        self.parameters = dict(parameters) if parameters else {}
        if self._parent_panel is not None:
            self._parent_panel._refresh_section_values(self)

    # ── Slots ──

    def toggle_collapse(self) -> None:
        self._is_collapsed = not self._is_collapsed
        self._content_widget.setVisible(not self._is_collapsed)
        self._collapse_btn.setText("▶" if self._is_collapsed else "▼")
        # 把展开/折叠状态记忆到面板（按 section 类型 key，跨片段保留），
        # 否则下次 set_clip 销毁重建 section 时状态会丢失。
        if self._parent_panel is not None:
            self._parent_panel._remember_collapse_state(
                self.section_id, self.effect_type, self._is_collapsed)

    def _on_section_enable_toggled(self, enabled: bool) -> None:
        """Enable or disable editing for native parameter groups."""
        self._section_enabled = enabled
        self._content_widget.setEnabled(enabled)

    # ── Effect-section slots ────────────────────────────────────────

    def set_effect_enabled(self, enabled: bool) -> None:
        """Programmatically set the enable-toggle state.

        Used by the panel when building an effect section to reflect the
        instance's ``is_enabled`` without emitting the toggled signal
        (avoids spurious commands during construction).
        """
        self._effect_enabled = enabled
        if self._effect_enable_btn is not None:
            self._effect_enable_btn.blockSignals(True)
            self._effect_enable_btn.setChecked(enabled)
            self._effect_enable_btn.blockSignals(False)
            # Dim the content when disabled so the visual state matches the
            # data — same affordance as locking, but driven by enable state.
            self._content_widget.setEnabled(enabled)

    def set_reorder_enabled(self, enabled: bool, reason: str = "") -> None:
        """Enable the grip or show why this section cannot be reordered."""
        if self._drag_handle is None:
            return
        self._drag_handle.setEnabled(enabled)
        if enabled:
            self._drag_handle.setToolTip(
                I18N.tr("edit.properties.drag_reorder", "Drag to reorder effect")
            )
        elif reason:
            self._drag_handle.setToolTip(reason)

    def _on_effect_enable_clicked(self) -> None:
        if self._effect_enable_btn is None:
            return
        enabled = self._effect_enable_btn.isChecked()
        self._effect_enabled = enabled
        self._content_widget.setEnabled(enabled)
        self.effect_enable_toggled.emit(self.section_id, enabled)

    def _on_effect_delete_clicked(self) -> None:
        self.effect_delete_requested.emit(self.section_id)

    def set_effect_type(self, effect_type: str) -> None:
        """P-clip-FX: 记录该 section 的 effect_type，并按需显示「打开面板」按钮。

        仅当 effect_type 属于 mixer audio 效果集合时显示按钮（这些效果有
        独立的完整参数对话框）。视频/未知类型保持隐藏。

        FX-M03: also badge the section title with the effect's render status
        (Preview-only / Not in export / Unsupported) so a silently-dropped
        effect is visible instead of looking normal.
        """
        self.effect_type = effect_type or ""
        if self._effect_open_dialog_btn is None:
            return
        from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
        self._effect_open_dialog_btn.setVisible(self.effect_type in AUDIO_MIXER_EFFECT_TYPES)

        # FX-M03: badge the title with render status. Audio mixer effects have
        # their own DSP path; only badge catalog video filters.
        self._refresh_effect_status_badge()

    def _refresh_effect_status_badge(self) -> None:
        """FX-M03: append a short render-status tag to the section title.

        Only video filters in the EffectCatalog get a badge (they are the ones
        that can silently drop on CPU/export). Unknown / audio / clip-property
        types are left unbadged.
        """
        if self._title_label is None or not self.effect_type:
            return
        try:
            from src.features.shared.effects import (
                get_effect_catalog,
                compute_effect_status,
                resolve_capability,
            )
            catalog = get_effect_catalog()
            if not catalog.is_known(self.effect_type):
                return  # not a catalog video filter — no badge
            cap = resolve_capability(
                self.effect_type, catalog_known=True,
            )
            diag = compute_effect_status(self.effect_type, cap)
            tag = {
                "active": "",
                "preview_degraded": "  [GPU-only]",
                "export_blocked": "  [Not in export]",
                "unsupported": "  [Unsupported]",
            }.get(diag.state.value, "")
            self._title_label.setText(f"{self.title}{tag}")
        except Exception:  # noqa: BLE001 — badge must never break the panel
            pass

    def set_plugin_status(self, report: object) -> None:
        """Display the stable-identity status for a plugin instance."""
        status = getattr(getattr(report, "status", None), "value", "")
        if not status or status == "active":
            return
        tag = {
            "legacy": "Legacy",
            "missing": "Missing",
            "version_mismatch": "Version changed",
            "schema_mismatch": "Parameters changed",
            "content_changed": "Content changed",
            "compile_error": "Compile error",
            "unsupported_backend": "Unsupported",
        }.get(status, status.replace("_", " ").title())
        self._title_label.setText(f"{self.title}  [{tag}]")
        detail = str(getattr(report, "detail", "") or "")
        if detail:
            self._title_label.setToolTip(detail)

    def _on_effect_open_dialog_clicked(self) -> None:
        """P-clip-FX: 「打开面板」按钮 → emit effect_open_dialog_requested。"""
        if not self.effect_type:
            return
        self.effect_open_dialog_requested.emit(self.section_id, self.effect_type)

    # ── FX-H04-UI: Drag-reorder initiation ──────────────────────────
    def eventFilter(self, obj: object, event: QEvent) -> bool:
        """Route drag-handle mouse events into the section reorder drag."""
        if obj is not self._drag_handle:
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_start_pos = event.position().toPoint()
                self._drag_handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            return False

        if event.type() == QEvent.Type.MouseButtonRelease:
            self._drag_start_pos = None
            self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
            return False

        if event.type() != QEvent.Type.MouseMove or self._drag_start_pos is None:
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        delta = event.position().toPoint() - self._drag_start_pos
        if delta.manhattanLength() < 6:
            return False

        self._drag_start_pos = None
        self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(
            "application/x-topos-effect-reorder",
            self.section_id.encode("utf-8"),
        )
        drag.setMimeData(mime)
        # Small pixmap so the cursor shows a grip indicator
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        drag.setPixmap(pm)
        drag.exec(Qt.DropAction.MoveAction)
        return True


# ================================================================
# EditPropertiesPanel — 主面板
# ================================================================

class EditPropertiesPanel(QWidget):
    """Edit 页面属性面板（镜像 composite Properties 面板架构）。

    每个参数分组渲染为独立的 EditSectionWidget，堆叠在滚动区里。
    支持五类选择：clip / transition / track / asset / timeline。
    """

    # ── 信号（保持原签名，供 edit_controller 连接）──
    clip_param_changed = pyqtSignal(str, str, str, object)  # clip_id, param_type, param_path, value
    # RI-05/R16：多选批量参数（每片按自身原值派生），一次宏提交
    clips_param_batch_changed = pyqtSignal(list)  # [(clip_id, param_type, param_path, value)]
    reset_transform_requested = pyqtSignal(list)            # list of clip_ids

    effect_param_changed = pyqtSignal(str, str, str, object)  # clip_id, effect_id, param_name, value
    # 批量参数变更（如 radius_x/radius_y 链锁联动）：控制器把它合并成一个
    # MacroCommand，一次 Ctrl+Z 全撤销。changes = [(param_name, value), ...]
    effect_params_changed = pyqtSignal(str, str, list)        # clip_id, effect_id, changes
    effect_enabled_changed = pyqtSignal(str, str, bool)       # clip_id, effect_id, enabled
    effect_remove_requested = pyqtSignal(str, str)            # clip_id, effect_id
    effect_selected = pyqtSignal(str, str)                    # clip_id, effect_id
    # P-clip-FX: 「打开面板」按钮 → 打开该 audio 效果的独立参数对话框。
    effect_open_dialog_requested = pyqtSignal(str, str, str)  # clip_id, effect_id, effect_type
    # FX-H04-UI: 拖拽排序释放后提交一次 reorder command。
    effect_reorder_requested = pyqtSignal(str, str, int)      # clip_id, effect_id, new_order

    transition_param_changed = pyqtSignal(str, str, object)   # transition_id, param_name, value
    track_param_changed = pyqtSignal(str, str, object)        # track_id, param_name, value
    # 拖动期间的纯视觉预览（不进命令栈）：track_id, param_name, value
    # 例如滑块拖动中持续发出，controller 只更新轨道头视觉，松手时再发
    # track_param_changed 提交一次命令。
    track_param_preview = pyqtSignal(str, str, object)
    keyframe_toggled = pyqtSignal(str, str, int, float)       # clip_id, param_name, frame, value
    # Phase 3 / round 2: per-effect keyframe toggle.
    # (clip_id, effect_id, param_name, frame, value)
    effect_keyframe_toggled = pyqtSignal(str, str, str, int, float)
    # R10（第七轮审计）：已动画参数的数值编辑 → 在当前帧更新/插入关键帧。
    # (clip_id, [(param_full_name, model_domain_value), ...], frame)——
    # 复数形式承载 scale 锁联动的双轴打点（单次 Undo 的 MacroCommand）。
    animated_params_edited = pyqtSignal(str, list, int)

    # P10.3 字幕片段参数变化：(clip_id, track_id, param_name, value)
    # 参数名直接对应 SubtitleClip 字段（text/font_family/font_size/translate_x/
    # fill_r/outline_enable/...）。controller 收到后构造 UpdateSubtitleClipCommand。
    subtitle_param_changed = pyqtSignal(str, str, str, object)
    # SUB-P1-01: 多字段合并提交（text debounce commit / RGBA 一次 patch）。
    # (clip_id, track_id, updates_dict) → controller 构造一条 UpdateSubtitleClipCommand。
    subtitle_params_changed = pyqtSignal(str, str, dict)
    subtitle_scope_changed = pyqtSignal(str, str, str)  # clip_id, track_id, global/custom

    # 生成器片段参数变化：Solid Color 的颜色（clip_id, asset_id, rgb tuple 0..1）。
    # controller 收到后通过 EditProject 更新 asset.metadata["color"]。
    generator_color_changed = pyqtSignal(str, str, object)
    # 生成器片段完整参数变化（clip_id, asset_id, params_dict）。
    # 携带 mode/colors/checker/gradient/radial 全部参数，controller 持久化到
    # asset.metadata["generator_params"]。
    generator_params_changed = pyqtSignal(str, str, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._updating = False  # 防递归锁
        self._current_clips: List[ClipInstance] = []
        self._fps: float = 24.0
        self._current_frame: int = 0
        # AUD-20000: 播放头帧来源。控制器可经 set_frame_provider 注入回调；
        # 未注入时面板在需要帧号（◆ 点击 / 按钮状态刷新）时自动从所在
        # 主窗口的 timeline_panel 拉取，并尝试订阅其 frame_changed 信号。
        self._frame_provider: Optional[Callable[[], int]] = None
        self._frame_source_connected: bool = False

        self._registry = get_effect_registry()
        self._selected_effect_id: Optional[str] = None  # edit_controller.py:975 直接写入

        # P5.1 选择对象
        self._current_transition: Optional[TransitionInstance] = None
        self._current_track: Optional[EditTrack] = None
        self._current_asset: Optional[MediaAsset] = None
        self._current_timeline: Optional[TimelineSequence] = None
        # 生成器片段的 media_type（adjustment / solid），由 set_clip 传入，
        # 用于跳过无关 section 与渲染 Solid Color 编辑控件。
        self._current_clip_media_type: Optional[str] = None
        # Solid 片段当前颜色（RGB 0..1），由 controller 在 set_clip 时注入。
        self._current_solid_color: Optional[tuple] = None
        # Solid 生成器完整参数集（mode/colors/checker/gradient/radial）。
        self._current_generator_params: Optional[dict] = None
        # P10.3 字幕片段选择对象
        self._current_subtitle_clip = None
        self._subtitle_scope: str = "global"
        self._subtitle_canvas_height: int = 1080

        # 颜色/合成状态（暂 stash，与旧版一致）
        self._current_color_stack_id: Optional[str] = None
        self._current_composite_id: Optional[str] = None

        # scale 锁定状态（X↔Y 联动）
        self._scale_locked = True

        # sections 容器
        self._sections: Dict[str, EditSectionWidget] = {}

        # 各 section「类型」的折叠状态记忆（跨片段保留）：type_key -> 是否折叠。
        # type_key 用 "transform"/"crop"/"speed"/"opacity"/"solid_color"/
        # "audio"/"effect:<effect_type>" 等类型标识，不含 clip_id，
        # 这样切换到不同片段时，同类 section 仍保持你上次的展开/折叠习惯。
        # 这样做是为了解决「选中/拖拽片段时 section 被销毁重建、_is_collapsed
        # 状态丢失」的问题（Edit 面板采用销毁重建架构，与合成页的 section 复用不同）。
        self._section_collapse_state: Dict[str, bool] = {}

        # Audio section 的滑块/DragValue 引用，按 track_id 索引：
        #   { track_id: {"volume": (slider, drag), "pan": (slider, drag)} }
        # 用于 reset 时主动把控件归位，以及 set_clip 重建后刷新显示。
        self._audio_widget_refs: Dict[str, Dict[str, tuple]] = {}

        # 关键帧按钮缓存
        self._keyframe_buttons: Dict[str, KeyframeIndicatorButton] = {}
        # R10：可动画参数 → 行控件取值/回显闭包（◆ 取值用控件当前值；
        # 播放头移动按帧求值回显，blockSignals 防反写）。
        self._animated_value_widgets: Dict[str, Dict[str, Any]] = {}

        # 不限最大宽度：仅 setMinimumWidth，与 composite 一致，可自由拖动 dock。
        # 水平 sizePolicy 用 Preferred（不主动争取宽度，但允许被 splitter 拉伸），
        # 这是修复"面板自动变宽挤小其它面板"的关键。
        self.setMinimumWidth(280)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )

        self._init_ui()
        logger.debug("EditPropertiesPanel initialized (composite-mirror architecture)")

    # ============================================================
    # UI 构建
    # ============================================================

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # The Edit inspector intentionally has no top title strip: the
        # selected clip's parameter groups are the first visible hierarchy.
        # Keep this label as an internal status sink because selection methods
        # update it, but do not place it in the layout.
        self._title_label = QLabel(I18N.tr("edit.properties.header", "Properties"))
        self._title_label.setVisible(False)

        # 滚动区
        # 与 composite 一致：setWidgetResizable(True) 让内部 _content 跟随 viewport
        # 宽度拉伸，section/数值行才能填满整个面板宽度（DragValue 贴右边缘）。
        # 之前给 _scroll/_content 加的水平 sizePolicy 限制（Preferred/Maximum）会
        # 把内容钉死在 sizeHint 宽度，导致 dock 拖宽后右侧出现大面积空白。
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {C.BG_PANEL}; border: none; }}"
        )
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setObjectName("editPropertiesContent")
        self._content.setStyleSheet(EDIT_PROPERTIES_CONTENT_STYLE)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(5, 5, 5, 5)
        self._content_layout.setSpacing(3)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll)

        # 无选择提示
        self._no_selection_label = QLabel(
            I18N.tr("edit.properties.no_selection", "No clip selected")
        )
        self._no_selection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_selection_label.setStyleSheet(EMPTY_STATE_LABEL_STYLE)
        self._content_layout.addWidget(self._no_selection_label)

        # FX-H04-UI: Enable effect section drag-reorder via drop on content area.
        self._content.setAcceptDrops(True)
        self._content.installEventFilter(self)
        # Thin insertion indicator shown during drag (2px accent line).
        self._drop_indicator = QWidget(self._content)
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet(f"background-color: {C.ORANGE};")
        self._drop_indicator.setVisible(False)
        self._drop_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # sizeHint / minimumSizeHint 故意不 override —— 与 composite 面板一致，
    # 让 Qt 用基于内容的默认 sizeHint。  之前为对抗"播放时 dock 撑宽"而硬编码的
    # QSize(100,116) 会让 dock 在布局协商中处于劣势，是"固定宽度感"的来源之一。

    # ============================================================
    # Section 容器管理
    # ============================================================

    def _clear_sections(self) -> None:
        """Remove all sections without leaving native combo popups behind.

        On macOS a QComboBox owns its ``QListView`` popup in a separate native
        window. Detaching a section while that popup still has queued paint or
        scrollbar events can leave Qt holding a dangling QWidget pointer and
        crash in ``QScrollBar::initStyleOption``. Hide every popup before
        detaching its section so Qt processes the close against live parents.
        """
        # The debounce timer captures the selected cue in its timeout closure.
        # Stop and release it before replacing sections so a queued timeout
        # cannot commit text into a cue that is no longer selected.
        subtitle_timer = getattr(self, "_subtitle_text_timer", None)
        if subtitle_timer is not None:
            subtitle_timer.stop()
            subtitle_timer.deleteLater()
            self._subtitle_text_timer = None
        self._subtitle_text_edit = None

        for section in list(self._sections.values()):
            for combo in section.findChildren(QComboBox):
                combo.hidePopup()
            self._content_layout.removeWidget(section)
            # Do not detach the QWidget from its native parent before Qt has
            # processed pending QListView/scrollbar events. Keeping the parent
            # until deleteLater() avoids a macOS use-after-free in the combo
            # popup's private window while still removing it from the layout.
            section.hide()
            section.deleteLater()
        self._sections.clear()
        self._keyframe_buttons.clear()
        self._audio_widget_refs.clear()

    # ============================================================
    # FX-H04-UI: Effect drag-reorder (event filter on _content)
    # ============================================================

    _REORDER_MIME = "application/x-topos-effect-reorder"

    def _effect_sections_in_order(self) -> List[EditSectionWidget]:
        """Return effect-mode sections in their current layout order."""
        result = []
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, EditSectionWidget) and w._effect_mode:
                result.append(w)
        return result

    def _drop_insertion_index(self, pos: QPoint) -> int:
        """Compute insertion index from a position in _content coordinates.

        Returns 0..N where N = number of effect sections. The index means
        "insert before the section currently at that index".
        """
        sections = self._effect_sections_in_order()
        for idx, sec in enumerate(sections):
            geo = sec.geometry()
            mid_y = geo.top() + geo.height() // 2
            if pos.y() < mid_y:
                return idx
        return len(sections)

    def _show_drop_indicator(self, index: int) -> None:
        """Position the 2px indicator line at the given insertion index."""
        sections = self._effect_sections_in_order()
        if not sections:
            self._drop_indicator.setVisible(False)
            return
        if index < len(sections):
            geo = sections[index].geometry()
            y = geo.top() - 2
        else:
            geo = sections[-1].geometry()
            y = geo.bottom() + 1
        self._drop_indicator.setGeometry(4, y, self._content.width() - 8, 2)
        self._drop_indicator.setVisible(True)
        self._drop_indicator.raise_()

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        """Handle drag events on _content for effect reorder."""
        if obj is not self._content:
            return super().eventFilter(obj, event)

        etype = event.type()
        from PyQt6.QtCore import QEvent
        if etype == QEvent.Type.DragEnter:
            if event.mimeData().hasFormat(self._REORDER_MIME):
                event.acceptProposedAction()
                return True
        elif etype == QEvent.Type.DragMove:
            if event.mimeData().hasFormat(self._REORDER_MIME):
                idx = self._drop_insertion_index(event.position().toPoint())
                self._show_drop_indicator(idx)
                event.acceptProposedAction()
                return True
        elif etype == QEvent.Type.DragLeave:
            self._drop_indicator.setVisible(False)
        elif etype == QEvent.Type.Drop:
            self._drop_indicator.setVisible(False)
            if event.mimeData().hasFormat(self._REORDER_MIME):
                self._handle_effect_drop(event)
                event.acceptProposedAction()
                return True
        return super().eventFilter(obj, event)

    def _handle_effect_drop(self, event) -> None:
        """Parse the dropped MIME data, compute new_order, emit signal once."""
        raw = bytes(event.mimeData().data(self._REORDER_MIME)).decode("utf-8")
        # section_id format: "clip:<clip_id>:effect:<effect_id>"
        parts = raw.split(":")
        if len(parts) < 4 or parts[0] != "clip" or parts[2] != "effect":
            return
        clip_id = parts[1]
        effect_id = ":".join(parts[3:])  # effect_id may contain colons (uuid)

        sections = self._effect_sections_in_order()
        # Find current index of the dragged effect
        current_idx = -1
        for i, sec in enumerate(sections):
            if sec.section_id == raw:
                current_idx = i
                break
        if current_idx == -1:
            return

        new_idx = self._drop_insertion_index(event.position().toPoint())
        # Adjust: if dropping below the source, the removal shifts indices
        if new_idx > current_idx:
            new_idx -= 1
        if new_idx == current_idx:
            return  # no-op drop (same position)

        self.effect_reorder_requested.emit(clip_id, effect_id, new_idx)

    def _add_section(self, section_id: str, title: str,
                     parameters: Dict[str, Any],
                     param_defs: Dict[str, ParamDef],
                     param_options: Optional[Dict[str, list]] = None,
                     node_color: str = "#666666",
                     builder: Optional[Any] = None,
                     collapsed: bool = False,
                     effect_mode: bool = False) -> EditSectionWidget:
        """创建并添加一个 section，用 builder 回调构建内容区。

        builder(section) — 面板提供的回调，负责往 section.content_layout() 填参数行。

        Args:
            collapsed: 是否默认折叠（True = 内容隐藏，仅显示 header）。
                用于原生参数 section 在有特效时折叠，以及非选中的特效 section。
            effect_mode: 是否启用 header 上的 enable/delete 按钮（特效 section 用）。
        """
        section = EditSectionWidget(
            section_id=section_id,
            title=title,
            parameters=parameters,
            param_defs=param_defs,
            param_options=param_options or {},
            parent_panel=self,
            node_color=node_color,
            collapsed=collapsed,
            effect_mode=effect_mode,
        )
        section.param_changed.connect(self._on_section_param_changed)
        section.reset_requested.connect(self._on_section_reset)
        if effect_mode:
            # Effect-section header buttons → re-emit as the existing
            # effect_enabled_changed / effect_remove_requested signals so
            # the controller needs no changes (it already handles those).
            section.effect_enable_toggled.connect(self._on_effect_enable_toggled)
            section.effect_delete_requested.connect(self._on_effect_delete_requested)
            # P-clip-FX: 「打开面板」按钮 → 转发到 controller 打开独立对话框
            section.effect_open_dialog_requested.connect(self._on_effect_open_dialog_requested)
        # Compute a shared label width from the widest param display name so
        # every row's value/slider left-edge lines up (mirrors composite's
        # _label_min_width).  Done before the builder runs because labels are
        # created inside the builder and read section._label_min_width then.
        section._label_min_width = self._compute_label_min_width(param_defs or {})
        if builder is not None:
            builder(section)
        # 插到 no_selection_label 之前
        idx = self._content_layout.indexOf(self._no_selection_label)
        self._content_layout.insertWidget(max(idx, 0), section)
        self._sections[section_id] = section
        return section

    @staticmethod
    def _compute_label_min_width(param_defs: Dict[str, ParamDef]) -> int:
        """Widest param-name pixel width + padding, floored at 60.

        Uses the same font metrics as composite (QFont("", 10)) so label
        alignment matches across both panels.  Param keys are title-cased
        the same way ``_make_param_label`` displays them.
        """
        fm = QFontMetrics(QFont("", 10))
        widest = 36
        for key in param_defs.keys():
            display = key.replace("_", " ").title()
            widest = max(widest, fm.horizontalAdvance(display) + 12)
        return max(widest, 60)

    # ============================================================
    # 数值行构建（镜像 composite _create_numeric_row）
    # ============================================================

    def _make_param_label(self, text: str, section: EditSectionWidget) -> QLabel:
        """参数标签：统一最小宽度（按 section 内最长参数名计算），永不横向增长。

        与 composite _make_param_label 一致：所有 label 共享同一个
        ``_label_min_width``，保证不同长度的参数名右边缘对齐 —— 这样 DragValue /
        slider 的左起点在同一垂直线上，行与行整齐。  Maximum sizePolicy 确保
        label 永远不抢宽度，多余空间全部分给中间的 slider。
        """
        label = QLabel(text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(section._label_min_width)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        return label

    @staticmethod
    def _make_properties_row() -> QWidget:
        """Create a transparent row container inside the dark inspector."""
        row = QWidget()
        row.setObjectName("editPropertiesRow")
        row.setStyleSheet(EDIT_PROPERTIES_ROW_STYLE)
        return row

    def _make_numeric_slider(self, is_int: bool, mn: float, mx: float,
                             step: float, value: float) -> ToposClickableSlider:
        """数值滑块（ToposClickableSlider，stretch=1 自适应宽度）。"""
        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.enable_velocity_drag = True
        if is_int:
            slider.setRange(int(mn), int(mx))
            slider.setValue(int(value))
        else:
            steps = max(int((mx - mn) / (step or 1.0)), 1)
            slider.setRange(0, steps)
            slider.setValue(int((value - mn) / (step or 1.0)))
        # Sliders are always visible — they stretch to fill the row's middle so
        # the value box stays pinned to the right edge (composite parity).  An
        # earlier version hid them on narrow docks, which left-aligned every row.
        slider.setVisible(True)
        return slider

    def _build_numeric_row(self, section: EditSectionWidget, key: str,
                           value: float, pdef: ParamDef,
                           enable_slider: bool = True,
                           enable_keyframe: bool = False,
                           keyframe_param: Optional[str] = None,
                           to_model=None, from_model=None) -> QWidget:
        """构建数值行：[label] [slider?] [DragValue] [◆?]。

        与 composite _create_numeric_row 一致：DragValue 固定 64px，slider stretch=1。
        G16：``keyframe_param`` 允许 ◆ 按钮携带与 section 键不同的完整参数名
        （如 rotation 行的键是 "rotation"，动画参数是 "transform.rotation"）。
        """
        is_int = pdef.ptype == "int"
        row = self._make_properties_row()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        # 标签
        display_name = key.replace("_", " ").title()
        label = self._make_param_label(display_name, section)
        h.addWidget(label)

        # DragValue（主控件，固定宽度）
        drag = DragValue("", pdef.min_val, pdef.max_val, float(value),
                         decimals=pdef.decimals)
        drag.setSingleStep(pdef.step)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

        info: Dict[str, Any] = {
            "type": "int" if is_int else "float",
            "widget": drag,
            "min_val": pdef.min_val, "max_val": pdef.max_val,
            "step": pdef.step, "decimals": pdef.decimals,
        }

        # 可选 slider（stretch=1 吃掉多余宽度）
        slider = None
        if enable_slider:
            slider = self._make_numeric_slider(
                is_int, pdef.min_val, pdef.max_val, pdef.step, float(value)
            )
            h.addWidget(slider, 1)
            # Register with the section so it can be hidden when the dock is
            # dragged narrow (see EditSectionWidget._apply_slider_visibility).
            section._slidable_sliders.append(slider)

            def drag_changed(v, k=key, sl=slider, inf=info) -> None:
                if self._updating:
                    return
                sl.blockSignals(True)
                sl.setValue(self._value_to_slider(v, inf))
                sl.blockSignals(False)
                self._emit_section_param(section, k, v)

            def slider_changed(v, k=key, d=drag, inf=info) -> None:
                if self._updating:
                    return
                fv = v if inf["type"] == "int" else v * inf["step"] + inf["min_val"]
                d.blockSignals(True)
                d.setValue(fv)
                d.blockSignals(False)
                self._emit_section_param(section, k, fv)

            drag.valueChanged.connect(drag_changed)
            slider.valueChanged.connect(slider_changed)
            info["slider"] = slider
        else:
            drag.valueChanged.connect(
                lambda v, k=key: self._emit_section_param(section, k, v)
            )

        h.addWidget(drag)

        # 可选关键帧按钮
        if enable_keyframe:
            full_param = keyframe_param or key
            kf_btn = KeyframeIndicatorButton(full_param)
            kf_btn.keyframe_toggled.connect(self._on_keyframe_button_toggled)
            self._keyframe_buttons[full_param] = kf_btn
            h.addWidget(kf_btn)
            self._register_animated_value_widget(
                full_param, drag, slider, info, to_model, from_model)

        section._param_widgets[key] = info
        return row

    def _register_animated_value_widget(
        self, full_param: str, drag, slider, info: Dict[str, Any],
        to_model=None, from_model=None) -> None:
        """R10：登记可动画参数行控件的取值/回显闭包。

        ``to_model``/``from_model`` 处理 UI 域与模型域换算（如 opacity
        行显示 0-100%、模型存 0-1）。set 内部 blockSignals——程序按帧
        求值回显不得反写命令栈。
        """
        def _get() -> float:
            v = float(drag.value())
            return float(to_model(v)) if to_model is not None else v

        def _set(model_value: float) -> None:
            v = float(from_model(model_value)) if from_model is not None \
                else float(model_value)
            drag.blockSignals(True)
            drag.setValue(v)
            drag.blockSignals(False)
            if slider is not None:
                slider.blockSignals(True)
                slider.setValue(self._value_to_slider(v, info))
                slider.blockSignals(False)

        self._animated_value_widgets[full_param] = {"get": _get, "set": _set}

    @staticmethod
    def _value_to_slider(value: float, info: Dict) -> int:
        if info.get("type") == "int":
            return int(value)
        mn = info["min_val"]
        step = info["step"] or 1.0
        return int((value - mn) / step)

    def _build_combo_row(self, section: EditSectionWidget, key: str,
                         value: Any, options: List[str],
                         labels: Optional[List[str]] = None,
                         pdef: Optional[ParamDef] = None) -> QWidget:
        """构建下拉行：[label] [ChevronComboBox(stretch)]。"""
        row = self._make_properties_row()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        display_name = key.replace("_", " ").title()
        label = self._make_param_label(display_name, section)
        h.addWidget(label)

        combo = ChevronComboBox()
        display_items = labels if labels else [str(o) for o in options]
        combo.addItems(display_items)
        if value in options:
            combo.setCurrentIndex(options.index(value))
        apply_combo_dropdown_style(combo)
        combo.currentIndexChanged.connect(
            lambda idx, k=key, opts=list(options):
                self._emit_section_param(section, k, opts[idx]) if 0 <= idx < len(opts) else None
        )
        h.addWidget(combo, 1)

        section._param_widgets[key] = {"type": "combo", "widget": combo, "options": list(options)}
        return row

    def _build_xy_param_row(self, section: EditSectionWidget, base: str,
                            x_key: str, y_key: str,
                            x_val: float, y_val: float,
                            pdef_x: ParamDef, pdef_y: ParamDef,
                            display_name: Optional[str] = None,
                            enable_link: bool = False,
                            link_default_on: bool = False,
                            enable_sliders: bool = True,
                            enable_keyframe: bool = False,
                            keyframe_prefix: str = "") -> QWidget:
        """构建 X/Y 配对数值行，对齐合成页 TransformNode 的 _create_xy_param_row。

        布局：[label] [⛓?] [x-slider?] x[v][◆?]  [y-slider?] y[v][◆?]
        - base 为 'scale' 时显示等比锁（默认锁住，1:1），与合成页一致。
        - 每轴前有一个 10px 灰字轴标记 x/y。
        - 两个 DragValue 固定 64px，slider 仅在 enable_sliders 时显示。
        - scale 联动：锁住时改一轴，另一轴 1:1 跟随（编辑页 _emit_transform_param
          已根据 self._scale_locked 处理 scale_x/scale_y 联动）。
        - G16：``enable_keyframe`` 时每轴附 ◆（动画参数名 = keyframe_prefix+轴键，
          如 "transform.translate_x"）。
        """
        row = self._make_properties_row()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        name = display_name or base.replace("_", " ").title()
        label = self._make_param_label(name, section)
        h.addWidget(label)

        # 等比锁（仅 scale）：合成页 scale/radius 默认锁住、1:1。
        chain = None
        if enable_link:
            chain = make_link_button(
                initial_linked=link_default_on,
                tooltip="Link X and Y",
            )
            chain.toggled.connect(self._on_scale_lock_toggled)
            h.addWidget(chain)

        def axis_marker(letter: str) -> QLabel:
            m = QLabel(letter)
            m.setStyleSheet("color: #888; font-size: 10px;")
            m.setFixedWidth(10)
            m.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return m

        def build_axis(key, val, pdef, letter):
            is_int = pdef.ptype == "int"
            mn, mx = pdef.min_val, pdef.max_val
            step = pdef.step or 1.0
            dec = pdef.decimals
            info: Dict[str, Any] = {
                "type": "int" if is_int else "float", "widget": None,
                "min_val": mn, "max_val": mx, "step": step, "decimals": dec,
            }
            slider = None
            if enable_sliders:
                slider = self._make_numeric_slider(is_int, mn, mx, step, float(val))
                slider.setVisible(enable_sliders)
                h.addWidget(slider, 1)
                section._slidable_sliders.append(slider)
                info["slider"] = slider

            h.addWidget(axis_marker(letter))
            drag = DragValue("", mn, mx, float(val), decimals=dec)
            drag.setSingleStep(step)
            drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
            h.addWidget(drag)
            info["widget"] = drag
            if enable_keyframe:
                # G16：每轴独立 ◆（动画参数名带 keyframe_prefix）
                full_param = f"{keyframe_prefix}{key}"
                kf_btn = KeyframeIndicatorButton(full_param)
                kf_btn.keyframe_toggled.connect(self._on_keyframe_button_toggled)
                self._keyframe_buttons[full_param] = kf_btn
                h.addWidget(kf_btn)
                # R10：登记行控件取值/回显闭包（transform 域无换算）。
                self._register_animated_value_widget(
                    full_param, drag, slider, info)

            def on_drag(v, k=key, sl=slider, inf=info) -> None:
                if self._updating:
                    return
                if sl is not None:
                    sl.blockSignals(True)
                    sl.setValue(self._value_to_slider(v, inf))
                    sl.blockSignals(False)
                self._emit_section_param(section, k, v)

            def on_slider(v, k=key, d=drag, inf=info) -> None:
                if self._updating:
                    return
                fv = v if inf["type"] == "int" else v * inf["step"] + inf["min_val"]
                d.blockSignals(True)
                d.setValue(fv)
                d.blockSignals(False)
                self._emit_section_param(section, k, fv)

            drag.valueChanged.connect(on_drag)
            if slider is not None:
                slider.valueChanged.connect(on_slider)
            return drag, slider, info

        drag_x, slider_x, info_x = build_axis(x_key, x_val, pdef_x, "x")
        drag_y, slider_y, info_y = build_axis(y_key, y_val, pdef_y, "y")
        h.addStretch()

        # scale 联动：锁住时改一轴，另一轴 1:1 跟随显示（_emit_transform_param
        # 会在数据层同步 scale_y/scale_x，这里仅同步显示）。
        if enable_link:
            def sync_peer(src_drag, peer_drag, peer_key):
                def _sync(v) -> None:
                    if not self._scale_locked or self._updating:
                        return
                    peer_drag.blockSignals(True)
                    peer_drag.setValue(v)
                    peer_drag.blockSignals(False)
                    # 同步 peer 的 slider 显示
                    peer_info = section._param_widgets.get(peer_key)
                    peer_sl = peer_info.get("slider") if peer_info else None
                    if peer_sl is not None:
                        peer_sl.blockSignals(True)
                        peer_sl.setValue(self._value_to_slider(v, peer_info))
                        peer_sl.blockSignals(False)
                return _sync
            drag_x.valueChanged.connect(sync_peer(drag_x, drag_y, y_key))
            drag_y.valueChanged.connect(sync_peer(drag_y, drag_x, x_key))

        section._param_widgets[x_key] = info_x
        section._param_widgets[y_key] = info_y
        return row

    def _build_bool_row(self, section: EditSectionWidget, key: str,
                        value: bool, label_text: Optional[str] = None) -> QWidget:
        """构建布尔行：[label] [CheckBox]。"""
        row = self._make_properties_row()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        display_name = label_text or key.replace("_", " ").title()
        label = self._make_param_label(display_name, section)
        h.addWidget(label)

        checkbox = QCheckBox()
        checkbox.setChecked(bool(value))
        checkbox.stateChanged.connect(
            lambda state, k=key: self._emit_section_param(
                section, k, state == Qt.CheckState.Checked.value)
            if state != Qt.CheckState.PartiallyChecked.value else None
        )
        h.addWidget(checkbox)
        h.addStretch()

        section._param_widgets[key] = {"type": "bool", "widget": checkbox}
        return row

    def _build_info_row(self, label_text: str, value_text: str,
                        bright: bool = False) -> QWidget:
        """构建只读信息行：[label] [value QLabel]。"""
        row = self._make_properties_row()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)

        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        h.addWidget(label)

        value_label = QLabel(value_text)
        color = C.TEXT_BRIGHT if bright else C.TEXT_HINT
        value_label.setStyleSheet(f"color: {color}; font-size: 10px;")
        value_label.setWordWrap(not bright)
        h.addWidget(value_label, 1)
        return row

    # ============================================================
    # 信号路由
    # ============================================================

    def _emit_section_param(self, section: EditSectionWidget,
                            param_name: str, value: Any) -> None:
        """从控件变化路由到 section 信号（带防递归）。"""
        if self._updating:
            return
        section.param_changed.emit(section.section_id, param_name, value)

    def _on_section_param_changed(self, section_id: str, param_name: str, value: Any) -> None:
        """section 参数变化 → 重建 dataclass → 发对应信号。

        section_id 格式：
          "clip:<clip_id>:transform" / "clip:<clip_id>:crop" /
          "clip:<clip_id>:opacity" / "clip:<clip_id>:speed" /
          "clip:<clip_id>:effect:<effect_id>" /
          "transition:<id>" / "track:<id>" /
          "subtitle:<clip_id>:<track_id>:<group>"  ← P10.3
        """
        if self._updating:
            return

        parts = section_id.split(":")

        kind = parts[0] if parts else ""

        if kind == "clip":
            if len(parts) < 3:
                return
            clip_id = parts[1]
            group = parts[2]
            # AUD-20001: effect section 的 id 自带被编辑的 effect
            # （clip:<clip_id>:effect:<effect_id>）。把 id 解析出来传给路由，
            # 不再依赖"全局选中特效"——展开非选中 section 点 Reset/改参数
            # 曾把全局选中特效的参数改掉。
            effect_id = (
                parts[3]
                if group == "effect" and len(parts) >= 4
                else None
            )
            self._route_clip_param(clip_id, group, param_name, value,
                                   effect_id=effect_id)
        elif kind == "subtitle":
            # subtitle:<clip_id>:<track_id>:<group>
            if len(parts) < 4:
                return
            clip_id = parts[1]
            track_id = parts[2]
            # group = parts[3] —— 字幕参数名直接对应 SubtitleClip 字段，
            # 无需按 group 重建 dataclass（controller 直接发 updates 字典）。
            # Keep the legacy cue signal as the public UI contract. The
            # controller routes style fields to the track when scope=global.
            self.subtitle_param_changed.emit(clip_id, track_id, param_name, value)
        elif kind == "transition":
            self._route_transition_param(parts[1] if len(parts) > 1 else "", param_name, value)
        elif kind == "track":
            self._route_track_param(parts[1] if len(parts) > 1 else "", param_name, value)

    def _route_clip_param(self, clip_id: str, group: str,
                          param_name: str, value: Any,
                          effect_id: Optional[str] = None) -> None:
        """clip 参数路由 → clip_param_changed 信号。

        注意契约：transform/crop/speed 发完整 dataclass；opacity 发 float。
        effect 分组用 section_id 携带的 effect_id（AUD-20001），而非面板
        全局选中态。
        """
        clip = self._current_clips[0] if self._current_clips else None
        if clip is None or clip.clip_id != clip_id:
            # 多选或 clip 不匹配，发标量值由 controller 处理
            self.clip_param_changed.emit(clip_id, group, param_name, value)
            return

        # RI-05/R16：多选时共享参数按每片自身原值派生，批量单宏提交
        #（此前只写 primary，X/Opacity 输入只改首片）
        if len(self._current_clips) > 1 and group in (
            "transform", "crop", "speed", "opacity", "raw"
        ):
            batch = []
            for member in self._current_clips:
                derived = self._derive_param_value(
                    member, group, param_name, value
                )
                if derived is not None:
                    batch.append((member.clip_id, group, param_name, derived))
            if batch:
                self.clips_param_batch_changed.emit(batch)
            return

        # R10（第七轮审计）：已动画参数的数值编辑走关键帧路径——在当前
        # 播放头帧更新/插入关键帧。旧逻辑只写静态 base（transform/
        # opacity 数据类），关键帧求值覆盖静态值，输入的值渲染不可见。
        _full = self._animatable_full_name(group, param_name)
        if _full is not None:
            is_animated = getattr(
                clip, "is_param_animated", lambda _: False)
            if is_animated(_full):
                frame = self._resolve_current_frame()
                model_value = float(value)
                if group == "opacity":
                    model_value = float(value) / 100.0
                edits = [(_full, model_value)]
                # R10 复查补齐：scale 锁联动——编辑一轴时另一轴也已动画
                # 的，同帧同步打点（与静态路径 with_scale(x, y) 同语义，
                # 合并为单次 Undo 的 MacroCommand）。peer 未动画时保持
                # 静态值（其 ◆ 未激活，不自动创建轨道）。
                if (
                    group == "transform"
                    and param_name in ("scale_x", "scale_y")
                    and self._scale_locked
                ):
                    peer = "scale_y" if param_name == "scale_x" else "scale_x"
                    peer_full = f"transform.{peer}"
                    if is_animated(peer_full):
                        edits.append((peer_full, model_value))
                self.animated_params_edited.emit(
                    clip.clip_id, edits, frame)
                return

        if group == "transform":
            self._emit_transform_param(clip, param_name, value)
        elif group == "crop":
            self._emit_crop_param(clip, param_name, value)
        elif group == "raw":
            # TRAW（批 2）：发完整 RawParams（与 transform/crop 契约同构）。
            # 2026-09-21 复查修复：从 clip 模型重建（section.parameters 是
            # 构建期快照且不随控件更新——此前连续编辑第二个参数会把
            # 第一个参数回退成快照旧值，面板显示与实际生效值分叉）。
            from src.features.edit.core.clip_params import RawParams
            base = getattr(clip, "raw", None) or RawParams()
            values = {
                "wb_gain_r": base.wb_gain_r,
                "wb_gain_g": base.wb_gain_g,
                "wb_gain_b": base.wb_gain_b,
                "exposure_stops": base.exposure_stops,
                "display_ootf": base.display_ootf,
                "black_level": base.black_level,
                "color_space": base.color_space,
                "gamma": base.gamma,
            }
            if param_name in values:
                values[param_name] = value
            try:
                raw = RawParams(
                    wb_gain_r=float(values["wb_gain_r"]),
                    wb_gain_g=float(values["wb_gain_g"]),
                    wb_gain_b=float(values["wb_gain_b"]),
                    exposure_stops=float(values["exposure_stops"]),
                    display_ootf=bool(values["display_ootf"]),
                    black_level=float(values["black_level"]),
                    color_space=str(values["color_space"]),
                    gamma=str(values["gamma"]),
                )
            except ValueError:
                return
            self.clip_param_changed.emit(clip_id, "raw", "value", raw)
        elif group == "opacity":
            # opacity 内部存 0-1.0，UI 显示 0-100%
            normalized = float(value) / 100.0 if isinstance(value, (int, float)) else value
            self.clip_param_changed.emit(clip_id, "opacity", "value", normalized)
        elif group == "speed":
            self._emit_speed_param(clip, param_name, value)
        elif group == "effect":
            # section_id = clip:<clip_id>:effect:<effect_id>
            if effect_id:
                self.effect_param_changed.emit(clip_id, effect_id, param_name, value)

    @staticmethod
    def _animatable_full_name(group: str, param_name: str) -> Optional[str]:
        """R10：section 行键 → 完整动画参数名（仅 G16 六个可动画参数）。"""
        if group == "transform" and param_name in (
            "translate_x", "translate_y", "scale_x", "scale_y", "rotation",
        ):
            return f"transform.{param_name}"
        if group == "opacity" and param_name in ("value", "opacity"):
            return "opacity"
        return None

    def _derive_param_value(self, clip: ClipInstance, group: str,
                            param_name: str, value: Any,
                            base: Any = None) -> Any:
        """RI-05/R16：按 clip 自身当前值派生新参数值（批量多选用）。

        ``base`` 显式给出时从该组值（而非 clip 上的当前值）派生——
        N13 的整组默认链式派生用它逐字段累积。transform 的 scale lock
        联动只在单选路径生效（避免批量写时 _sync_paired_widget 与多片
        值互相干扰）。
        """
        if group == "transform":
            t = base if base is not None else clip.transform
            if param_name == "translate_x":
                return t.with_translate(float(value), t.translate_y)
            if param_name == "translate_y":
                return t.with_translate(t.translate_x, float(value))
            if param_name in ("scale_x", "scale_y"):
                if self._scale_locked:
                    return t.with_scale(float(value), float(value))
                if param_name == "scale_x":
                    return t.with_scale(float(value), t.scale_y)
                return t.with_scale(t.scale_x, float(value))
            if param_name == "rotation":
                return t.with_rotation(float(value))
            if param_name == "pivot_mode":
                return t.with_pivot_mode(str(value))
            if param_name == "pivot_x":
                return t.with_pivot(t.pivot_mode, float(value), t.pivot_y)
            if param_name == "pivot_y":
                return t.with_pivot(t.pivot_mode, t.pivot_x, float(value))
            return None
        if group == "crop":
            c = base if base is not None else clip.crop
            if param_name == "feather":
                return c.with_feather(float(value))
            return c.with_crop(**{param_name: float(value) / 100.0})
        if group == "speed":
            speed_obj = base if base is not None else clip.speed
            current = (
                speed_obj if isinstance(speed_obj, SpeedParams)
                else SpeedParams(speed=float(speed_obj))
            )
            if param_name == "speed":
                return current.with_speed(float(value))
            if param_name == "reverse":
                return current.with_reverse(bool(value))
            if param_name == "preserve_pitch":
                return current.with_preserve_pitch(bool(value))
            return None
        if group == "opacity":
            if isinstance(value, (int, float)):
                return float(value) / 100.0
            return value
        if group == "raw":
            current = base if base is not None else (clip.raw or RawParams())
            if param_name not in current.__dataclass_fields__:
                return None
            return replace(current, **{param_name: value})
        return None

    def _derive_group_default(self, clip: ClipInstance, group: str,
                              defaults: Dict[str, Any]) -> Any:
        """N13/T3-05：链式派生"整组默认"参数值。

        逐个把 section ParamDef 默认值经 _derive_param_value 的组内语
        义套到累积的组值上（base 链），非 UI 字段（速度点、pivot 联动
        等）原样保留；opacity 归一化为 0-1。不依赖对 clip 做
        dataclasses.replace（mock/替身宿主同样可用）。
        """
        current = None
        for key, default in defaults.items():
            derived = self._derive_param_value(
                clip, group, key, default, base=current)
            if derived is None:
                continue
            current = derived
        return current

    def _emit_transform_param(self, clip: ClipInstance, param_name: str, value: Any) -> None:
        """重建 TransformParams 并发信号。处理 scale lock 联动。"""
        t = clip.transform
        if param_name == "translate_x":
            new_t = t.with_translate(float(value), t.translate_y)
        elif param_name == "translate_y":
            new_t = t.with_translate(t.translate_x, float(value))
        elif param_name == "scale_x":
            new_y = float(value) if self._scale_locked else t.scale_y
            new_t = t.with_scale(float(value), new_y)
            if self._scale_locked:
                self._sync_paired_widget("scale_y", float(value))
        elif param_name == "scale_y":
            new_x = float(value) if self._scale_locked else t.scale_x
            new_t = t.with_scale(new_x, float(value))
            if self._scale_locked:
                self._sync_paired_widget("scale_x", float(value))
        elif param_name == "rotation":
            new_t = t.with_rotation(float(value))
        elif param_name == "pivot_mode":
            new_t = t.with_pivot_mode(str(value))
        elif param_name == "pivot_x":
            new_t = t.with_pivot(t.pivot_mode, float(value), t.pivot_y)
        elif param_name == "pivot_y":
            new_t = t.with_pivot(t.pivot_mode, t.pivot_x, float(value))
        else:
            return
        self.clip_param_changed.emit(clip.clip_id, "transform", param_name, new_t)

    def _emit_crop_param(self, clip: ClipInstance, param_name: str, value: Any) -> None:
        """重建 CropParams（UI 显示百分比 0-50，内部存 0-0.5）。"""
        c = clip.crop
        if param_name == "feather":
            new_c = c.with_feather(float(value))
        else:
            normalized = float(value) / 100.0
            new_c = c.with_crop(**{param_name: normalized})
        self.clip_param_changed.emit(clip.clip_id, "crop", param_name, new_c)

    def _emit_speed_param(self, clip: ClipInstance, param_name: str, value: Any) -> None:
        """重建 SpeedParams。"""
        if isinstance(clip.speed, SpeedParams):
            current = clip.speed
        else:
            current = SpeedParams(speed=float(clip.speed))
        if param_name == "speed":
            new_s = current.with_speed(float(value))
        elif param_name == "reverse":
            new_s = current.with_reverse(bool(value))
        elif param_name == "preserve_pitch":
            # P1-08：音频保调变速开关（离线导出 WSOLA；实时预览暂仍变调）
            new_s = current.with_preserve_pitch(bool(value))
        else:
            return
        self.clip_param_changed.emit(clip.clip_id, "speed", param_name, new_s)

    def _route_transition_param(self, transition_id: str, param_name: str, value: Any) -> None:
        self.transition_param_changed.emit(transition_id, param_name, value)

    def _route_track_param(self, track_id: str, param_name: str, value: Any) -> None:
        self.track_param_changed.emit(track_id, param_name, value)

    def _sync_paired_widget(self, key: str, value: float) -> None:
        """scale lock 联动：同步另一轴的 DragValue **与 slider** 显示。

        AUD-20002: 只 setValue DragValue 时，经滑块路径发起的联动会让
        对侧滑块 thumb 停在旧位置（该路径不会触发 drag.valueChanged 上的
        sync_peer），数值框与滑块显示分叉。两处都在 _updating 锁内归位
        （setValue 触发的 valueChanged 处理器检查同一把锁，天然防递归）。
        """
        # 在 transform section 里找对应 key 的 DragValue
        for section in self._sections.values():
            if ":transform" in section.section_id:
                info = section._param_widgets.get(key)
                if info and "widget" in info:
                    self._updating = True
                    try:
                        info["widget"].setValue(value)
                        slider = info.get("slider")
                        if slider is not None:
                            slider.setValue(
                                self._value_to_slider(float(value), info))
                    finally:
                        self._updating = False
                break

    def _on_section_reset(self, section_id: str) -> None:
        """section reset 请求。"""
        parts = section_id.split(":")
        kind = parts[0] if parts else ""
        if kind == "clip" and len(parts) >= 3:
            group = parts[2]
            if group == "transform":
                # 发 reset_transform_requested 信号（controller 会批量重置）
                clip_ids = [c.clip_id for c in self._current_clips]
                self.reset_transform_requested.emit(clip_ids)
            elif group == "solid_color":
                # AUD-20007: Solid 参数不经 _route_clip_param（直发
                # generator_params_changed 写 asset metadata），通用分支
                # 会把参数信号静默丢弃却把控件显示归位——UI 与数据脱节。
                self._reset_solid_color_section(section_id)
            else:
                section = self._sections.get(section_id)
                if not section:
                    return
                # N13/T3-05：Reset 是一次动作，必须一个宏提交。逐参数
                # 重放控件事件会发 N 个宏（Crop 五项 Reset 只剩最后一
                # 项默认值）、逐次与旧快照派生互相覆盖；这里按每片快照
                # 一次性构造"整组默认"参数（保留非 UI 字段如速度点），
                # 单批一次提交，已默认的片不进批（零历史零 revision）。
                defaults = {
                    key: pdef.default for key, pdef in
                    section.param_defs.items() if pdef.default is not None
                }
                if defaults and group in ("crop", "opacity", "speed", "raw"):
                    batch = []
                    for member in self._current_clips:
                        derived = self._derive_group_default(
                            member, group, defaults)
                        if derived is None:
                            continue
                        current_value = getattr(member, group, None)
                        if group == "opacity" and isinstance(
                                current_value, (int, float)):
                            current_value = float(current_value)
                        if derived == current_value:
                            continue  # 已默认：no-op，不产生历史
                        batch.append(
                            (member.clip_id, group, "reset", derived))
                    if batch:
                        self.clips_param_batch_changed.emit(batch)
                else:
                    for key, default_value in defaults.items():
                        self._emit_section_param(section, key, default_value)
                self._reset_section_widgets(section)
        # Track 级分组（如 Audio：volume/pan 是轨道级数据）
        elif kind == "track" and len(parts) >= 3:
            group = parts[2]
            track_id = parts[1]
            if group == "audio":
                # 重置为默认：volume=1.0（100%）、pan=0.0（中）
                self.track_param_changed.emit(track_id, "volume", 1.0)
                self.track_param_changed.emit(track_id, "pan", 0.0)
                # 同步本 section 的 slider/drag 控件显示
                self._reset_audio_section_widgets(track_id)
        elif kind == "transition":
            # AUD-20008: 转场 section 的 Reset 此前完全静默——
            # _on_section_reset 没有该 kind 的分支，按钮却无条件创建。
            transition = self._current_transition
            if transition is not None and len(parts) >= 2 \
                    and transition.transition_id == parts[1]:
                self._reset_transition_section(transition)
        elif kind == "subtitle":
            # AUD-20008: subtitle:<clip_id>:<track_id>:<group> 的 Reset
            # 此前同样静默。字幕分组用通用参数行构建，按 ParamDef 默认值
            # 逐项发 subtitle_param_changed，并把行控件显示归位。
            section = self._sections.get(section_id)
            if section:
                for key, pdef in section.param_defs.items():
                    if pdef.default is not None:
                        self._emit_section_param(section, key, pdef.default)
                self._reset_section_widgets(section)

    def _reset_section_widgets(self, section: EditSectionWidget) -> None:
        """把通用参数 section 的控件同步到各自默认值。"""
        for key, pdef in section.param_defs.items():
            default = pdef.default
            if default is None:
                continue
            info = section._param_widgets.get(key, {})
            widget = info.get("widget")
            if widget is not None:
                widget.blockSignals(True)
                try:
                    if isinstance(widget, QCheckBox):
                        widget.setChecked(bool(default))
                    elif isinstance(widget, QComboBox):
                        options = info.get("options", [])
                        if default in options:
                            widget.setCurrentIndex(options.index(default))
                    elif isinstance(widget, QPlainTextEdit):
                        # AUD-20008: 字幕 Text 行（多行编辑框）也能被 Reset
                        # 归位；blockSignals 防止触发 textChanged 的 debounce。
                        widget.setPlainText(str(default))
                    elif hasattr(widget, "setValue"):
                        widget.setValue(default)
                finally:
                    widget.blockSignals(False)

            slider = info.get("slider")
            if slider is not None:
                slider.blockSignals(True)
                try:
                    slider.setValue(self._value_to_slider(float(default), info))
                finally:
                    slider.blockSignals(False)

    def _reset_audio_section_widgets(self, track_id: str) -> None:
        """把 Audio section 的 volume/pan 控件显示归位到默认值。

        在 _on_section_reset 发出 track_param_changed 后调用。命令链会通过
        增量更新改轨道数据，但本 section 的 slider/drag 是独立控件，需要
        主动 setValue 才会刷新显示。
        """
        refs = self._audio_widget_refs.get(track_id)
        if not refs:
            return
        vol_widgets = refs.get("volume")
        if vol_widgets:
            slider, drag = vol_widgets
            slider.blockSignals(True)
            drag.blockSignals(True)
            slider.setValue(100)   # volume 默认 100%
            drag.setValue(100.0)
            slider.blockSignals(False)
            drag.blockSignals(False)
        pan_widgets = refs.get("pan")
        if pan_widgets:
            slider, drag = pan_widgets
            slider.blockSignals(True)
            drag.blockSignals(True)
            slider.setValue(0)     # pan 默认 0（中）
            drag.setValue(0.0)
            slider.blockSignals(False)
            drag.blockSignals(False)

    def _on_keyframe_button_toggled(self, param_name: str, frame: int, value: float) -> None:
        """关键帧按钮 → keyframe_toggled 信号。

        AUD-20000: 按钮携带的 frame/value 是其上次 set_state 的缓存
        （从未喂过数据时是构造默认值 0/0.0），发射前重新解析播放头帧
        与该帧上的当前参数值。
        """
        clip = self._current_clips[0] if self._current_clips else None
        if clip is None:
            return
        frame = self._resolve_current_frame()
        # R10：取值优先行控件当前值——用户刚在数值框输入的值（如 100）
        # 就是打点意图；模型求值会被已有动画覆盖（0→100 动画中点输入
        # 100 再点 ◆，旧逻辑写入的是求值 0 而非输入 100）。
        record = self._animated_value_widgets.get(param_name)
        if record is not None:
            try:
                value = float(record["get"]())
            except (TypeError, ValueError, RuntimeError):
                record = None
        if record is None:
            get_value = getattr(clip, "get_param_at_frame", None)
            if get_value is not None:
                try:
                    value = float(get_value(param_name, frame))
                except (TypeError, ValueError):
                    pass  # 参数不可数值化时保留按钮缓存值
        self.keyframe_toggled.emit(clip.clip_id, param_name, frame, value)

    def _on_effect_keyframe_button_toggled(
        self, effect_id: str, param_name: str, clip_id: str,
        frame: int, value: float,
    ) -> None:
        """效果参数 ◆ 点击 → effect_keyframe_toggled 信号。

        AUD-20000: 同 _on_keyframe_button_toggled，发射前重新解析
        播放头帧与该帧上的参数值（值取自 EffectInstance 的动画求值）。
        """
        clip = next(
            (c for c in self._current_clips if c.clip_id == clip_id), None)
        if clip is None:
            clip = self._current_clips[0] if self._current_clips else None
        if clip is None:
            return
        frame = self._resolve_current_frame()
        effect = clip.get_effect_by_id(effect_id) if hasattr(
            clip, "get_effect_by_id") else None
        if effect is not None:
            try:
                value = float(effect.get_param_at_frame(param_name, frame))
            except (TypeError, ValueError):
                pass
        self.effect_keyframe_toggled.emit(
            clip.clip_id, effect_id, param_name, frame, value)

    # ============================================================
    # 数值刷新（外部 set_clip 等调用后，把数据刷到控件）
    # ============================================================

    def _refresh_section_values(self, section: EditSectionWidget) -> None:
        """把 section.parameters 的当前值刷到控件显示（增量，不重建）。"""
        if self._updating:
            return
        self._updating = True
        try:
            for key, info in section._param_widgets.items():
                value = section.parameters.get(key)
                if value is None:
                    continue
                w = info.get("widget")
                if w is None:
                    continue
                if info["type"] in ("float", "int"):
                    w.setValue(float(value))
                elif info["type"] == "combo":
                    options = info.get("options", [])
                    if value in options:
                        w.setCurrentIndex(options.index(value))
                elif info["type"] == "bool":
                    w.setChecked(bool(value))
        finally:
            self._updating = False

    # ============================================================
    # 折叠状态记忆（跨片段保留）
    # ============================================================

    def _section_type_key(self, section_id: str, effect_type: str = "") -> Optional[str]:
        """从 section_id 提取「类型 key」（不含 clip_id/track_id），用于跨片段记忆。

        section_id 形如：
          "clip:<id>:transform"     -> "transform"
          "clip:<id>:crop"          -> "crop"
          "clip:<id>:speed"         -> "speed"
          "clip:<id>:opacity"       -> "opacity"
          "clip:<id>:solid_color"   -> "solid_color"
          "clip:<id>:effect:<eid>"  -> "effect:<effect_type>"（同类效果共享记忆）
          "track:<id>:audio"        -> "audio"
        无法解析的返回 None（不记忆，用各 builder 默认值）。
        """
        if not section_id:
            return None
        parts = section_id.split(":")
        # clip:<id>:<kind>[:effect:<eid>]
        if len(parts) >= 3 and parts[0] == "clip":
            kind = parts[2]
            if kind == "effect":
                # effect section：按 effect_type 记忆，同类效果共享展开状态。
                return f"effect:{effect_type}" if effect_type else "effect:unknown"
            return kind  # transform/crop/speed/opacity/solid_color
        # track:<id>:audio
        if len(parts) >= 3 and parts[0] == "track":
            return parts[2]  # audio
        return None

    def _remember_collapse_state(self, section_id: str, effect_type: str,
                                 collapsed: bool) -> None:
        """EditSectionWidget.toggle_collapse 回调：按 section 类型记忆折叠状态。"""
        type_key = self._section_type_key(section_id, effect_type)
        if type_key is not None:
            self._section_collapse_state[type_key] = collapsed

    def _collapsed_for(self, type_key: str, default_collapsed: bool) -> bool:
        """查记忆；无记忆则用 default_collapsed。"""
        return self._section_collapse_state.get(type_key, default_collapsed)

    # ============================================================
    # Public API — 选择类型切换（保留原接口契约）
    # ============================================================

    def clear(self) -> None:
        """清空面板（无选择）。"""
        self._updating = True
        self._current_clips = []
        self._current_transition = None
        self._current_track = None
        self._current_asset = None
        self._current_timeline = None
        self._current_subtitle_clip = None
        self._subtitle_scope = "global"
        self._clear_sections()
        self._no_selection_label.setVisible(True)
        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug("EditPropertiesPanel.clear() called")

    def set_multi_asset_selection(self, count: int) -> None:
        """E19: 多选素材时显示明确的批量状态，而不是清空面板。

        批量素材属性暂不支持（产品边界），但用户应看到"已选中 N 个、
        为什么面板不显示属性"，而非一片空白造成"面板坏了"的印象。
        """
        count = max(0, int(count))
        self.clear()
        self._no_selection_label.setVisible(True)
        self._no_selection_label.setText(I18N.tr(
            "edit.properties.multi_asset_selected",
            f"{count} assets selected\nBatch editing is not supported yet — select a single asset to edit its properties.",
        ))
        self._title_label.setText(I18N.tr(
            "edit.properties.multi_asset_header",
            f"Properties ({count} assets)",
        ))
        logger.debug("EditPropertiesPanel.set_multi_asset_selection(%d)", count)

    def set_clip(self, clip: ClipInstance, fps: float = 24.0, track: Optional[EditTrack] = None, media_type: Optional[str] = None, solid_color: Optional[tuple] = None, generator_params: Optional[dict] = None) -> None:
        """显示单个 clip 的属性。

        Args:
            clip: 选中的片段
            fps: 帧率
            track: 片段所在的轨道（可选）。音频片段选中时传入，用于在面板
                显示并编辑所在轨道的音量/声像（音频参数是轨道级数据）。
            media_type: 片段引用素材的 media_type（可选）。生成器类片段
            （adjustment / solid）据此跳过无关 section：adjustment 保留
                Transform/Opacity，用于作用于下方合成结果；Crop/Speed 仍不适用。
                solid 额外显示 Color 编辑 section。
            solid_color: Solid 片段当前 RGB（0..1），用于色块预览与初始色。
            generator_params: Solid 生成器完整参数集（mode/colors/checker/gradient/radial）。
        """
        self._current_clips = [clip]
        self._fps = fps
        self._current_transition = None
        self._current_track = track
        self._current_asset = None
        self._current_timeline = None
        self._current_clip_media_type = media_type
        self._current_solid_color = solid_color
        self._current_generator_params = generator_params
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)

        # 原生参数 section（Transform/Crop/Speed/Opacity）
        # 默认展开 —— 调节片段的 Transform/Opacity 会作用于其下方的
        # 合成结果，因此它们对 Adjustment Clip 也保持可编辑。
        # 折叠状态由 _section_collapse_state 跨片段记忆：用户手动折叠过的
        # 类型会保持折叠，其余展开。这样选中/拖拽片段重建 section 后，Transform
        # 不会被重置成折叠（修复「调手柄时参数组消失」的问题）。
        # Effect sections 由 _build_effect_sections 内部单独控制（记忆优先，
        # 无记忆时刚添加/选中的特效展开，其余折叠），不跟随 collapse_native。
        collapse_native = False
        is_adjustment = media_type == "adjustment"
        is_solid = media_type == "solid"

        # 1. Effect sections（置顶！每个特效一个独立可折叠 section）
        #    调节片段的核心就是效果链，正常显示；solid 通常不挂效果。
        self._build_effect_sections(clip)
        # 2. 原生参数 sections（默认展开，见上方 collapse_native 说明）
        #    Adjustment 的 Transform/Opacity 作用于下方已合成画面；Crop/Speed
        #    仍是源片段属性，因此只给普通媒体构建 Crop/Speed。
        if not is_solid:
            self._build_transform_section(clip, collapsed=self._collapsed_for("transform", collapse_native))
            if not is_adjustment:
                self._build_crop_section(clip, collapsed=self._collapsed_for("crop", collapse_native))
                self._build_speed_section(clip, collapsed=self._collapsed_for("speed", collapse_native))
        # Solid Color 颜色编辑 section（置于 Opacity 上方，solid 的核心参数）
        if is_solid:
            self._build_solid_color_section(clip, collapsed=self._collapsed_for("solid_color", False))
        # TRAW RAW section（批 2）：视频/图片片段均显示——参数仅对 RAW
        # 素材生效（非 RAW 片段 debayer 分支不消费，无副作用）
        media_type_for_raw = getattr(self, "_current_clip_media_type", None)
        if media_type_for_raw in ("video", "image", None):
            self._build_raw_section(clip, collapsed=self._collapsed_for("raw", True))
        self._build_opacity_section(clip, collapsed=self._collapsed_for("opacity", collapse_native))
        # Audio section（仅音频片段：显示所在轨道的音量/声像）
        if track is not None and track.track_type == "audio":
            self._build_audio_clip_section(clip, track, collapsed=self._collapsed_for("audio", True))

        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        # AUD-20000: 选中片段后立即按当前播放头帧刷新 ◆ 按钮状态。
        self._update_keyframe_buttons()
        logger.debug(f"EditPropertiesPanel.set_clip({clip.clip_id})")

    def set_clips(self, clips: List[ClipInstance], fps: float = 24.0,
                  tracks: Optional[List[Optional[EditTrack]]] = None,
                  media_type: Optional[str] = None,
                  solid_color: Optional[tuple] = None,
                  generator_params: Optional[dict] = None) -> None:
        """多选 clip（简化：单 clip 走 set_clip，多选取第一个显示 + mixed 标记）。

        Args:
            clips: 选中的片段列表
            fps: 帧率
            tracks: 与 clips 等长的轨道列表（可选），用于音频片段的音量/声像显示。
            media_type: 首个片段的素材类型（可选）。
            solid_color: Solid 片段当前 RGB（可选）。
            generator_params: Solid 生成器完整参数集（可选）。
        """
        self._current_clips = clips
        self._fps = fps
        if not clips:
            self.clear()
            return
        if len(clips) == 1:
            track = tracks[0] if tracks else None
            self.set_clip(clips[0], fps, track=track,
                          media_type=media_type, solid_color=solid_color,
                          generator_params=generator_params)
            return
        # INT-13：多选不再被 set_clip 截断为单片段。SelectionContext
        #（_current_clips）保持完整列表，重置/批量命令作用于全部选中；
        # 面板以 primary（第一个）构建控件，多选时标题提示计数。
        self.set_clip(clips[0], fps, media_type=media_type,
                      solid_color=solid_color, generator_params=generator_params)
        self._current_clips = list(clips)  # set_clip 内部置 [primary]，此处恢复完整上下文
        self._title_label.setText(I18N.tr(
            "edit.properties.header_multi",
            "Properties ({n} clips selected)",
        ).format(n=len(clips)))
        self.refresh_clip_widgets_from_snapshots()
        logger.debug(f"EditPropertiesPanel.set_clips({len(clips)} clips)")

    def set_transition(self, transition: TransitionInstance) -> None:
        """显示转场属性。"""
        self._current_transition = transition
        self._current_clips = []
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)
        self._build_transition_section(transition)
        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug(f"EditPropertiesPanel.set_transition({transition.transition_id})")

    def set_subtitle_clip(self, clip, fps: float = 24.0,
                          canvas_height: int = 1080,
                          resolved_style: Optional[dict] = None) -> None:
        """显示字幕片段属性（P10.3）。

        与 set_clip 同构：stash 选择对象、清空 sections、按分组重建字幕参数 UI。
        clip 是 SubtitleClip（frozen dataclass）。
        """
        self._current_subtitle_clip = clip
        self._subtitle_scope = "custom" if clip.style_override is not None else "global"
        display_clip = clip
        if resolved_style:
            display_values = {
                key: value for key, value in resolved_style.items()
                if hasattr(clip, key)
            }
            if display_values:
                display_clip = replace(clip, **display_values)
        self._current_clips = []
        self._current_transition = None
        self._current_track = None
        self._current_asset = None
        self._current_timeline = None
        self._fps = fps
        self._subtitle_canvas_height = max(1, int(canvas_height or 1080))
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)

        # 分组构建顺序：info → text → font → layout → transform → fill → outline → shadow
        self._build_subtitle_info_section(display_clip)
        self._build_subtitle_text_section(display_clip)
        self._build_subtitle_font_section(display_clip)
        self._build_subtitle_layout_section(display_clip)
        self._build_subtitle_transform_section(display_clip)
        self._build_subtitle_fill_section(display_clip)
        self._build_subtitle_outline_section(display_clip)
        self._build_subtitle_shadow_section(display_clip)

        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug(f"EditPropertiesPanel.set_subtitle_clip({clip.clip_id})")

    def update_subtitle_transform_values(self, clip: object) -> None:
        """Synchronize only the selected subtitle's transform controls.

        Handle dragging updates an immutable timeline many times per second.
        Rebuilding the entire properties panel here is expensive and destroys
        focus, so this method updates existing widgets under the panel's
        anti-recursion lock instead.
        """
        if clip is None or self._current_subtitle_clip is None:
            return
        clip_id = getattr(clip, "clip_id", None)
        if clip_id != getattr(self._current_subtitle_clip, "clip_id", None):
            return
        track_id = getattr(clip, "track_id", "")
        section_id = f"subtitle:{clip_id}:{track_id}:transform"
        section = self._sections.get(section_id)
        if section is None:
            return
        values: Dict[str, Any] = {
            "translate_x": getattr(clip, "translate_x", 0.0),
            "translate_y": getattr(clip, "translate_y", 0.0),
            "scale_x": getattr(clip, "scale_x", 1.0),
            "scale_y": getattr(clip, "scale_y", 1.0),
            "rotation": getattr(clip, "rotation", 0.0),
            "pivot_mode": getattr(clip, "pivot_mode", "center"),
            "pivot_x": getattr(clip, "pivot_x", 0.0),
            "pivot_y": getattr(clip, "pivot_y", 0.0),
        }
        previous_updating = self._updating
        self._updating = True
        try:
            for key, value in values.items():
                info = section._param_widgets.get(key)
                if info is None:
                    continue
                widget = info.get("widget")
                if widget is not None:
                    widget.blockSignals(True)
                    if info.get("type") == "combo":
                        options = info.get("options", [])
                        if value in options:
                            widget.setCurrentIndex(options.index(value))
                    else:
                        widget.setValue(float(value))
                    widget.blockSignals(False)
                slider = info.get("slider")
                if slider is not None:
                    slider.blockSignals(True)
                    slider.setValue(self._value_to_slider(float(value), info))
                    slider.blockSignals(False)
                section.parameters[key] = value
            self._current_subtitle_clip = clip
        finally:
            self._updating = previous_updating

    def set_track(self, track: EditTrack) -> None:
        """显示轨道属性。"""
        self._current_track = track
        self._current_clips = []
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)
        self._build_track_section(track)
        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug(f"EditPropertiesPanel.set_track({track.track_id})")

    def set_asset(self, asset: MediaAsset) -> None:
        """显示媒体资产属性（只读）。"""
        self._current_asset = asset
        self._current_clips = []
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)
        self._build_asset_section(asset)
        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug(f"EditPropertiesPanel.set_asset({asset.asset_id})")

    def set_timeline(self, timeline: TimelineSequence) -> None:
        """显示时间线属性（只读）。"""
        self._current_timeline = timeline
        self._current_clips = []
        self._updating = True
        self._clear_sections()
        self._no_selection_label.setVisible(False)
        self._build_timeline_section(timeline)
        self._title_label.setText(I18N.tr("edit.properties.header", "Properties"))
        self._updating = False
        logger.debug(f"EditPropertiesPanel.set_timeline({timeline.timeline_id})")

    # ============================================================
    # 状态更新（P9.1/P9.2 — stash，与旧版一致）
    # ============================================================

    def update_color_status(self, color_stack_id: str) -> None:
        if not color_stack_id:
            return
        self._current_color_stack_id = color_stack_id
        logger.debug(f"EditPropertiesPanel: color status updated - stack {color_stack_id[:8]}")

    def update_composite_status(self, composite_id: str) -> None:
        if not composite_id:
            return
        self._current_composite_id = composite_id
        logger.debug(f"EditPropertiesPanel: composite status updated - comp {composite_id[:8]}")

    def set_current_frame(self, frame: int) -> None:
        """更新当前帧（驱动关键帧按钮状态 + R10 按帧求值回显）。"""
        frame = int(frame)
        if frame == self._current_frame:
            return
        self._current_frame = frame
        self._update_keyframe_buttons()
        self._refresh_animated_widget_values()

    def _refresh_animated_widget_values(self) -> None:
        """R10：播放头移动后按当前帧求值刷新已动画参数行显示。

        set 闭包内部 blockSignals——程序回显不得触发 _emit_section_param
        反写命令栈。未动画参数保持静态值不动。
        """
        if self._updating or not self._current_clips:
            return
        clip = self._current_clips[0]
        evaluate = getattr(clip, "get_param_at_frame", None)
        if not callable(evaluate):
            return
        for full_param, record in self._animated_value_widgets.items():
            if not getattr(clip, "is_param_animated", lambda _: False)(
                    full_param):
                continue
            try:
                value = float(evaluate(full_param, self._current_frame))
                record["set"](value)
            except (TypeError, ValueError, RuntimeError):
                logger.debug(
                    "animated value refresh failed for %s", full_param,
                    exc_info=True)

    # ============================================================
    # 播放头帧跟踪（AUD-20000）
    # ============================================================

    def set_frame_provider(self, provider: Optional[Callable[[], int]]) -> None:
        """注入当前播放头帧的查询回调（优先级最高的帧来源）。

        回调须返回 int 帧号，异常时面板回退到其他帧来源。未注入时，
        面板会从所在主窗口的 timeline_panel 拉取播放头位置。
        """
        self._frame_provider = provider

    def _find_timeline_panel(self) -> Optional[Any]:
        """沿父链向上查找持有 ``timeline_panel`` 属性的窗口（主窗口）。

        面板可能被 dock 到主窗口或浮动 dock 中，逐级向上找属性而不是
        只看 window()，避免浮动 dock 场景拿不到主窗口。
        """
        widget: Optional[QWidget] = self
        seen: set[int] = set()
        while widget is not None and id(widget) not in seen:
            seen.add(id(widget))
            candidate = getattr(widget, "timeline_panel", None)
            if candidate is not None and candidate is not widget:
                return candidate
            widget = widget.parent()
        return None

    def _ensure_frame_source_connected(self) -> None:
        """订阅播放头 frame_changed 信号（幂等；连不上时下次再试）。"""
        if self._frame_source_connected:
            return
        timeline_panel = self._find_timeline_panel()
        if timeline_panel is None:
            return
        try:
            controller = timeline_panel.get_playback_controller()
        except Exception:
            controller = None
        frame_changed = getattr(controller, "frame_changed", None)
        if frame_changed is None:
            return
        try:
            frame_changed.connect(self._on_external_frame_changed)
        except (TypeError, RuntimeError) as exc:
            logger.debug("EditPropertiesPanel: frame_changed connect failed: %s", exc)
            return
        self._frame_source_connected = True

    def _on_external_frame_changed(self, frame: int) -> None:
        """timeline 播放头变化 → 刷新关键帧按钮状态。"""
        self.set_current_frame(int(frame))

    def _resolve_current_frame(self) -> int:
        """解析当前播放头帧号（provider → timeline_panel → 缓存值）。"""
        if self._frame_provider is not None:
            try:
                return int(self._frame_provider())
            except Exception:
                logger.exception(
                    "EditPropertiesPanel: frame provider failed, "
                    "falling back to timeline panel")
        timeline_panel = self._find_timeline_panel()
        get_frame = getattr(timeline_panel, "get_current_frame", None)
        if get_frame is not None:
            try:
                return int(get_frame())
            except Exception:
                logger.exception(
                    "EditPropertiesPanel: timeline get_current_frame failed, "
                    "keeping last known frame")
        return self._current_frame

    def showEvent(self, event) -> None:
        """面板首次可见时尝试连接播放头帧来源（window() 此时可用）。"""
        super().showEvent(event)
        self._ensure_frame_source_connected()

    def update_current_clip(self, clip: ClipInstance) -> None:
        """Swap in a fresher ClipInstance without rebuilding the panel.

        Used after a command (e.g. AddKeyframeCommand on an effect param)
        mutates the clip's data model: the panel's widgets are still
        valid, but ``_current_clips`` holds the *old* clip reference, so
        :meth:`_update_keyframe_buttons` would query stale keyframe state
        and the ◆ button wouldn't reflect the just-added keyframe until
        the next full :meth:`set_clip`.

        Replaces the entry with the same ``clip_id`` in ``_current_clips``
        (or prepends if absent) and refreshes keyframe-button state.
        Does NOT touch the widget tree — callers that need the UI rebuilt
        should call :meth:`set_clip` instead.
        """
        if not getattr(clip, "clip_id", None):
            return
        for i, existing in enumerate(self._current_clips):
            if getattr(existing, "clip_id", None) == clip.clip_id:
                self._current_clips[i] = clip
                break
        else:
            self._current_clips.insert(0, clip)
        self._update_keyframe_buttons()
        # R10：关键帧增删/更新后已动画参数的当前帧值可能变化，按帧
        # 求值回显（blockSignals，不反写）。
        self._refresh_animated_widget_values()

    def set_fps(self, fps: float) -> None:
        self._fps = fps

    def update_clips(self, clips: List[ClipInstance]) -> None:
        """N12/T3-04：按 stable clip_id 批量替换快照并刷新控件显示。

        批量提交成功 / 领域拒绝 / Undo/Redo 后由 controller 从 canonical
        timeline 调用。此前面板 ``_current_clips`` 在批量提交后停留在
        提交前快照——下一个字段以旧快照为基派生整组 dataclass，静默覆
        盖刚提交的前一个字段（X→Y 后 X 回原值），控件显示与模型分叉。
        """
        updated_any = False
        for clip in clips:
            if not getattr(clip, "clip_id", None):
                continue
            for i, existing in enumerate(self._current_clips):
                if getattr(existing, "clip_id", None) == clip.clip_id:
                    if existing is not clip:
                        self._current_clips[i] = clip
                    updated_any = True
                    break
        if updated_any:
            self.refresh_clip_widgets_from_snapshots()

    @staticmethod
    def _transform_display_values(clip: ClipInstance) -> Dict[str, Any]:
        t = clip.transform
        return {
            "translate_x": t.translate_x, "translate_y": t.translate_y,
            "scale_x": t.scale_x, "scale_y": t.scale_y,
            "rotation": t.rotation,
            "pivot_mode": t.pivot_mode,
            "pivot_x": t.pivot_x, "pivot_y": t.pivot_y,
        }

    @staticmethod
    def _crop_display_values(clip: ClipInstance) -> Dict[str, Any]:
        c = clip.crop
        return {
            "left": c.left * 100.0, "right": c.right * 100.0,
            "top": c.top * 100.0, "bottom": c.bottom * 100.0,
            "feather": c.feather,
        }

    @staticmethod
    def _speed_display_values(clip: ClipInstance) -> Dict[str, Any]:
        if isinstance(clip.speed, SpeedParams):
            s = clip.speed
            return {
                "speed": s.speed, "reverse": s.reverse,
                "preserve_pitch": s.preserve_pitch,
            }
        return {
            "speed": float(clip.speed), "reverse": False,
            "preserve_pitch": False,
        }

    @staticmethod
    def _opacity_display_values(clip: ClipInstance) -> Dict[str, Any]:
        return {"value": clip.opacity * 100.0}

    @staticmethod
    def _raw_display_values(clip: ClipInstance) -> Optional[Dict[str, Any]]:
        raw = getattr(clip, "raw", None) or RawParams()
        return {
            name: getattr(raw, name) for name in (
                "wb_gain_r", "wb_gain_g", "wb_gain_b", "exposure_stops",
                "display_ootf", "black_level", "color_space", "gamma",
            ) if hasattr(raw, name)
        }

    def refresh_clip_widgets_from_snapshots(self) -> None:
        """把 primary clip 的 canonical 参数刷回原生参数控件（防递归）。

        只做显示归位（widget + slider + combo/checkbox），不发任何参数
        信号；N09 拒绝恢复与 N12 提交刷新共用同一路径，保证控件、面板
        快照与 canonical model 三者一致。
        """
        from PyQt6.QtWidgets import QComboBox

        if not self._current_clips:
            return
        primary = self._current_clips[0]
        clip_id = getattr(primary, "clip_id", None)
        if not clip_id:
            return
        groups = {
            "transform": self._transform_display_values(primary),
            "crop": self._crop_display_values(primary),
            "speed": self._speed_display_values(primary),
            "opacity": self._opacity_display_values(primary),
            "raw": self._raw_display_values(primary),
        }
        self._updating = True
        try:
            for group, values in groups.items():
                if values is None:
                    continue
                section = self._sections.get(f"clip:{clip_id}:{group}")
                if section is None:
                    continue
                section.parameters.update(values)
                for key, value in values.items():
                    info = section._param_widgets.get(key)
                    if not info:
                        continue
                    widget = info.get("widget")
                    display_values = getattr(
                        self, f"_{group}_display_values")
                    member_values = [
                        display_values(member) for member in self._current_clips
                    ]
                    mixed = any(
                        member is None or member.get(key) != value
                        for member in member_values)
                    info["mixed"] = mixed
                    if widget is not None:
                        if isinstance(widget, QCheckBox):
                            widget.setTristate(mixed)
                            widget.setCheckState(
                                Qt.CheckState.PartiallyChecked if mixed
                                else (Qt.CheckState.Checked if value
                                      else Qt.CheckState.Unchecked))
                        elif isinstance(widget, QComboBox):
                            options = info.get("options")
                            if options and value in options:
                                widget.setCurrentIndex(
                                    options.index(value))
                            else:
                                idx = widget.findData(value)
                                if idx < 0:
                                    idx = widget.findText(str(value))
                                if idx >= 0:
                                    widget.setCurrentIndex(idx)
                            if mixed:
                                widget.setPlaceholderText("—")
                                widget.setCurrentIndex(-1)
                        elif hasattr(widget, "setValue"):
                            widget.setValue(value)
                            set_mixed = getattr(widget, "setMixedValue", None)
                            if callable(set_mixed):
                                set_mixed(mixed)
                    slider = info.get("slider")
                    if (slider is not None and isinstance(value, (int, float))
                            and not isinstance(value, bool)):
                        slider.setValue(
                            self._value_to_slider(float(value), info))
        finally:
            self._updating = False
        self._update_keyframe_buttons()

    def _update_keyframe_buttons(self) -> None:
        """刷新所有关键帧按钮状态。

        Buttons registered with a plain ``param_name`` key query the
        clip's animated_params; buttons registered with the
        ``"effect:<eid>:<param>"`` composite key (Phase 3 / round 2)
        query the corresponding EffectInstance instead.
        """
        if not self._current_clips or self._updating:
            return
        # AUD-20000: 每次刷新先同步播放头帧，按钮状态不再依赖陈旧缓存。
        self._ensure_frame_source_connected()
        self._current_frame = self._resolve_current_frame()
        clip = self._current_clips[0]
        for key, button in self._keyframe_buttons.items():
            if key.startswith("effect:"):
                # effect-scoped button: parse "effect:<eid>:<param>"
                parts = key.split(":", 2)
                if len(parts) < 3:
                    continue
                effect_id, param_name = parts[1], parts[2]
                effect = clip.get_effect_by_id(effect_id) if hasattr(
                    clip, "get_effect_by_id") else None
                if effect is None:
                    button.set_state(False, False, self._current_frame, 0.0)
                    continue
                is_animated = effect.is_param_animated(param_name)
                has_keyframe = effect.has_keyframe_at(param_name, self._current_frame)
                current_value = effect.get_param_at_frame(param_name, self._current_frame)
                button.set_state(has_keyframe, is_animated, self._current_frame, current_value)
            else:
                param_name = key
                is_animated = getattr(clip, "is_param_animated", lambda _: False)(param_name)
                has_keyframe = getattr(clip, "has_keyframe_at", lambda *a: False)(param_name, self._current_frame)
                current_value = getattr(clip, "get_param_at_frame", lambda *a: 0.0)(param_name, self._current_frame)
                button.set_state(has_keyframe, is_animated, self._current_frame, current_value)


# ============================================================
# 各分组的 section 构建器（EditPropertiesPanel 的方法，通过 builder 回调注入 section）
# ============================================================

def _format_duration(frames: int, fps: float = 24.0) -> str:
    if fps <= 0:
        fps = 24.0
    total_seconds = frames / fps
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)
    if minutes > 0:
        return f"{minutes}:{seconds:02d}"
    return f"{seconds}s ({frames}f)"


# ── EditPropertiesPanel 的分组构建方法（追加到类） ──
# 由于 Python 类体已闭合，这里用方法扩展的形式补全。
# 实际通过下面的函数绑定到类上。


# ================================================================
# P10.3 字幕片段（SubtitleClip）分组构建方法
#
# section_id 格式：subtitle:<clip_id>:<track_id>:<group>
# 参数名直接对应 SubtitleClip 字段（与 TextNode.PARAM_DEFS 同名同语义）。
# controller 收到 subtitle_param_changed 后构造 UpdateSubtitleClipCommand
# 的 updates 字典，无需按 group 重建 dataclass。
# ================================================================

def _build_subtitle_info_section(self: EditPropertiesPanel, clip) -> None:
    """字幕片段元信息（只读）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:info"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_info", "Subtitle Info"),
        parameters={},
        param_defs={},
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.clip_id", "ID"), clip.clip_id[:16]))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.timeline", "Timeline"),
        f"{clip.timeline_start}-{clip.timeline_end} "
        f"({_format_duration(clip.timeline_duration, self._fps)})"))

    custom_box = QCheckBox(
        I18N.tr("edit.properties.subtitle_custom", "Custom subtitle (independent of track)")
    )
    custom_box.setChecked(self._subtitle_scope == "custom")
    custom_box.setToolTip(I18N.tr(
        "edit.properties.subtitle_custom_hint",
        "When enabled, this subtitle no longer follows the track's global style.",
    ))

    def _scope_changed(state: int) -> None:
        if self._updating:
            return
        scope = "custom" if bool(state) else "global"
        self._subtitle_scope = scope
        self.subtitle_scope_changed.emit(clip.clip_id, clip.track_id, scope)

    custom_box.stateChanged.connect(_scope_changed)
    cl.addWidget(custom_box)


def _build_subtitle_text_section(self: EditPropertiesPanel, clip) -> None:
    """字幕正文（多行可编辑）。

    SUB-P1-01: 文本编辑采用 debounce transient preview + 合并提交。
    - 每次按键只刷新本地缓存（不写命令栈），避免 undo 按字符膨胀与输入卡顿；
    - 250ms 停顿或 focus-out/editingFinished 时提交一条 UpdateSubtitleClipCommand
      （经 subtitle_params_changed 多字段信号），整个输入只产生 1 条 undo。
    """
    from PyQt6.QtCore import QTimer

    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:text"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_text", "Text"),
        parameters={"text": clip.text},
        param_defs={"text": ParamDef(name="text", ptype="str", default="")},
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    edit = QPlainTextEdit()
    edit.setPlainText(clip.text or "")
    edit.setPlaceholderText(I18N.tr("edit.properties.subtitle_text_hint",
                                    "Enter subtitle text..."))
    edit.setMinimumHeight(60)
    edit.setMaximumHeight(120)
    edit.setStyleSheet(
        f"QPlainTextEdit {{ background-color: {C.BG_INPUT}; color: {C.TEXT_BRIGHT}; "
        f"border: 1px solid {C.BORDER}; font-size: 11px; padding: 4px; }}"
    )
    # Keep a handle to the live widget so focus-out / tests can reach it.
    self._subtitle_text_edit = edit
    # AUD-20008: 注册进 _param_widgets，让 section Reset 能把文本归位
    # （_reset_section_widgets 会 blockSignals，不触发 textChanged 的 debounce）。
    section._param_widgets["text"] = {"type": "text", "widget": edit}

    # Per-clip debounce state. ``_pending_text`` holds the not-yet-committed
    # value; the timer coalesces rapid keystrokes into one commit.
    self._subtitle_text_timer = QTimer(self)
    self._subtitle_text_timer.setSingleShot(True)
    self._subtitle_text_timer.setInterval(250)
    self._subtitle_text_pending: str = clip.text or ""
    self._subtitle_text_clip_id: str = clip.clip_id
    self._subtitle_text_track_id: str = clip.track_id
    self._subtitle_text_last_committed: str = clip.text or ""

    def _commit_text() -> None:
        # Avoid emitting while the panel is being rebuilt programmatically.
        if self._updating:
            return
        text = self._subtitle_text_pending
        if text == self._subtitle_text_last_committed:
            return
        self._subtitle_text_last_committed = text
        self.subtitle_params_changed.emit(
            self._subtitle_text_clip_id,
            self._subtitle_text_track_id,
            {"text": text},
        )

    self._subtitle_text_timer.timeout.connect(_commit_text)

    def _on_text_changed() -> None:
        if self._updating:
            return
        # Record the latest value and (re)start the debounce window. No command
        # is emitted per keystroke — only a transient preview value is kept.
        self._subtitle_text_pending = edit.toPlainText()
        self._subtitle_text_timer.start()

    edit.textChanged.connect(_on_text_changed)

    def _on_focus_out(_event=None):
        # Commit immediately when the user leaves the field (don't wait for
        # the debounce window to expire).
        if self._subtitle_text_timer.isActive():
            self._subtitle_text_timer.stop()
        _commit_text()

    edit.focusOutEvent = _on_focus_out

    cl.addWidget(edit)


def _build_subtitle_font_section(self: EditPropertiesPanel, clip) -> None:
    """字体参数（family/size/weight/italic）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:font"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_font", "Font"),
        parameters={
            "font_family": clip.font_family,
            "font_size": clip.font_size,
            "font_weight": clip.font_weight,
            "italic": clip.italic,
        },
        param_defs=SUBTITLE_FONT_PARAM_DEFS,
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    # font_family — 可编辑 combo（从 QFontDatabase 填充）
    family_combo = ChevronComboBox()
    family_combo.setEditable(True)
    try:
        families = list(QFontDatabase.families())
    except Exception:
        families = ["Arial", "Helvetica", "Times New Roman", "Courier New"]
    if clip.font_family not in families:
        families.insert(0, clip.font_family)
    family_combo.addItems(families)
    family_combo.setCurrentText(clip.font_family)
    apply_combo_dropdown_style(family_combo)
    family_combo.setCurrentText(clip.font_family)

    # 用 _build_combo_row 体系构建一个 family 行（特殊处理：editable combo）
    family_row = QWidget()
    fh = QHBoxLayout(family_row)
    fh.setContentsMargins(0, 2, 0, 2)
    fh.setSpacing(4)
    family_label = self._make_param_label("Family", section)
    fh.addWidget(family_label)
    fh.addWidget(family_combo, 1)
    cl.addWidget(family_row)

    # font_family 提交（AUD-20005）：可编辑 combo 此前同时挂
    # currentIndexChanged 与 editTextChanged——Qt 中从弹出列表选择一项两个
    # 信号都发（一次选择 = 两条命令 = 两条 undo），手输则每键一条。
    # 改为单一入口 currentTextChanged（两条路径都覆盖）+ 与 Text 字段相同
    # 的 250ms debounce / focus-out 即时提交 / last-committed 去重。
    old_family_timer = getattr(self, "_subtitle_family_timer", None)
    if old_family_timer is not None:
        try:
            old_family_timer.stop()
            old_family_timer.timeout.disconnect()
        except RuntimeError:
            pass  # 旧 section 已析构，其定时器随之销毁
    family_timer = QTimer(family_combo)  # 随 section 销毁自动取消待触发的提交
    family_timer.setSingleShot(True)
    family_timer.setInterval(250)
    self._subtitle_family_timer = family_timer
    self._subtitle_family_last_committed = clip.font_family

    def _commit_family() -> None:
        if self._updating:
            return
        text = family_combo.currentText()
        if text == self._subtitle_family_last_committed:
            return
        self._subtitle_family_last_committed = text
        self._emit_section_param(section, "font_family", text)

    family_timer.timeout.connect(_commit_family)

    def _on_family_text_changed(_text: str) -> None:
        if self._updating:
            return
        # 只记录待提交窗口；不在每键/每选择上发命令。
        family_timer.start()

    family_combo.currentTextChanged.connect(_on_family_text_changed)

    def _on_family_focus_out(_event=None) -> None:
        # 离开输入框时立即冲刷（不等 debounce 窗口结束）。
        if family_timer.isActive():
            family_timer.stop()
        _commit_family()

    family_combo.focusOutEvent = _on_family_focus_out

    # AUD-20008: 注册进 _param_widgets，让 section Reset 能把显示归位
    # （_reset_section_widgets 会 blockSignals，不触发上面的 debounce）。
    section._param_widgets["font_family"] = {
        "type": "combo", "widget": family_combo, "options": families,
    }

    # font_size: 0 is stored as auto-fit, but the panel displays the same
    # concrete size that the renderer uses for this timeline canvas.
    display_font_size = effective_subtitle_font_size(
        clip.font_size, getattr(self, "_subtitle_canvas_height", 1080)
    )
    cl.addWidget(self._build_numeric_row(
        section, "font_size", display_font_size,
        SUBTITLE_FONT_PARAM_DEFS["font_size"]))

    # font_weight
    cl.addWidget(self._build_combo_row(
        section, "font_weight", clip.font_weight,
        _SUB_FONT_WEIGHTS, labels=_SUB_FONT_WEIGHTS))

    # italic
    cl.addWidget(self._build_bool_row(section, "italic", clip.italic))


def _build_subtitle_layout_section(self: EditPropertiesPanel, clip) -> None:
    """排版（halign/valign/letter_spacing/line_spacing）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:layout"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_layout", "Layout"),
        parameters={
            "halign": clip.halign,
            "valign": clip.valign,
            "letter_spacing": clip.letter_spacing,
            "line_spacing": clip.line_spacing,
        },
        param_defs=SUBTITLE_LAYOUT_PARAM_DEFS,
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    cl.addWidget(self._build_combo_row(
        section, "halign", clip.halign, _SUB_HALIGNS, labels=_SUB_HALIGNS))
    cl.addWidget(self._build_combo_row(
        section, "valign", clip.valign, _SUB_VALIGNS, labels=_SUB_VALIGNS))
    cl.addWidget(self._build_numeric_row(
        section, "letter_spacing", clip.letter_spacing,
        SUBTITLE_LAYOUT_PARAM_DEFS["letter_spacing"]))
    cl.addWidget(self._build_numeric_row(
        section, "line_spacing", clip.line_spacing,
        SUBTITLE_LAYOUT_PARAM_DEFS["line_spacing"]))


def _build_subtitle_transform_section(self: EditPropertiesPanel, clip) -> None:
    """变换（translate/scale/rotation/pivot）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:transform"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.transform", "Transform"),
        parameters={
            "translate_x": clip.translate_x,
            "translate_y": clip.translate_y,
            "scale_x": clip.scale_x,
            "scale_y": clip.scale_y,
            "rotation": clip.rotation,
            "pivot_mode": clip.pivot_mode,
            "pivot_x": clip.pivot_x,
            "pivot_y": clip.pivot_y,
        },
        param_defs=SUBTITLE_TRANSFORM_PARAM_DEFS,
        node_color="#555555",
    )
    cl = section.content_layout()

    cl.addWidget(self._build_numeric_row(
        section, "translate_x", clip.translate_x,
        SUBTITLE_TRANSFORM_PARAM_DEFS["translate_x"], enable_slider=False))
    cl.addWidget(self._build_numeric_row(
        section, "translate_y", clip.translate_y,
        SUBTITLE_TRANSFORM_PARAM_DEFS["translate_y"], enable_slider=False))
    cl.addWidget(self._build_numeric_row(
        section, "scale_x", clip.scale_x,
        SUBTITLE_TRANSFORM_PARAM_DEFS["scale_x"]))
    cl.addWidget(self._build_numeric_row(
        section, "scale_y", clip.scale_y,
        SUBTITLE_TRANSFORM_PARAM_DEFS["scale_y"]))
    cl.addWidget(self._build_numeric_row(
        section, "rotation", clip.rotation,
        SUBTITLE_TRANSFORM_PARAM_DEFS["rotation"]))
    cl.addWidget(self._build_combo_row(
        section, "pivot_mode", clip.pivot_mode,
        _SUB_PIVOT_MODES, labels=_SUB_PIVOT_MODES))
    cl.addWidget(self._build_numeric_row(
        section, "pivot_x", clip.pivot_x,
        SUBTITLE_TRANSFORM_PARAM_DEFS["pivot_x"], enable_slider=False))
    cl.addWidget(self._build_numeric_row(
        section, "pivot_y", clip.pivot_y,
        SUBTITLE_TRANSFORM_PARAM_DEFS["pivot_y"], enable_slider=False))


def _build_subtitle_color_button(self: EditPropertiesPanel, section,
                                 clip, group: str,
                                 r_key: str, g_key: str, b_key: str, a_key: str,
                                 label: str) -> QWidget:
    """字幕颜色按钮：[label] [色块按钮] [R DragValue] [G] [B] [A]。

    色块点击 → QColorDialog → 4 通道 emit。
    4 个 DragValue 单独编辑 → 单通道 emit。
    """
    param_defs = SUBTITLE_FILL_PARAM_DEFS if group == "fill" else (
        SUBTITLE_OUTLINE_PARAM_DEFS if group == "outline" else SUBTITLE_SHADOW_PARAM_DEFS
    )
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)

    lbl = self._make_param_label(label, section)
    h.addWidget(lbl)

    r_val = float(getattr(clip, r_key))
    g_val = float(getattr(clip, g_key))
    b_val = float(getattr(clip, b_key))
    a_val = float(getattr(clip, a_key))

    # 色块按钮
    swatch = QPushButton()
    swatch.setFixedSize(28, 20)
    swatch.setCursor(Qt.CursorShape.PointingHandCursor)

    def _update_swatch() -> None:
        ri = max(0, min(255, int(r_val * 255)))
        gi = max(0, min(255, int(g_val * 255)))
        bi = max(0, min(255, int(b_val * 255)))
        swatch.setStyleSheet(
            f"background-color: rgb({ri},{gi},{bi}); border: 1px solid {C.BORDER};"
        )

    _update_swatch()

    def _pick_color() -> None:
        nonlocal r_val, g_val, b_val, a_val
        init = QColor(
            max(0, min(255, int(r_val * 255))),
            max(0, min(255, int(g_val * 255))),
            max(0, min(255, int(b_val * 255))),
            max(0, min(255, int(a_val * 255))),
        )
        color = QColorDialog.getColor(init, self, "Select Color",
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return
        r_val = color.redF()
        g_val = color.greenF()
        b_val = color.blueF()
        a_val = color.alphaF()
        _update_swatch()
        # SUB-P1-01: 同步 4 个 DragValue，但只发一个合并 RGBA patch（一条 undo），
        # 而非 4 条逐通道命令。
        for k, v in ((r_key, r_val), (g_key, g_val), (b_key, b_val), (a_key, a_val)):
            widget_info = section._param_widgets.get(k)
            if widget_info and "widget" in widget_info:
                widget_info["widget"].blockSignals(True)
                widget_info["widget"].setValue(v)
                widget_info["widget"].blockSignals(False)
        color_updates = {r_key: r_val, g_key: g_val, b_key: b_val, a_key: a_val}
        self.subtitle_params_changed.emit(
            clip.clip_id, clip.track_id, color_updates,
        )

    swatch.clicked.connect(_pick_color)
    h.addWidget(swatch)

    # 4 个 DragValue（紧凑）。
    # SUB-P1-01: 拖动一个通道用 begin/update/end session —— 拖动期间累积
    # transient 值（不入栈），释放时发一个合并 RGBA patch（一条 undo），而非
    # 每个 valueChanged 一条逐通道命令。
    rgba_keys = (r_key, g_key, b_key, a_key)
    # session 状态：拖动开始前的 4 通道快照 + 当前累积的 pending 更新。
    session_state: dict = {"before": None, "pending": {}}

    def _read_rgba() -> dict:
        return {k: float(section._param_widgets[k]["widget"].value())
                for k in rgba_keys}

    def _begin_color_session() -> None:
        if self._updating:
            return
        session_state["before"] = _read_rgba()
        session_state["pending"] = {}

    def _commit_color_session() -> None:
        if self._updating:
            return
        before = session_state["before"]
        pending = session_state["pending"]
        session_state["before"] = None
        session_state["pending"] = {}
        if not pending or before is None:
            return
        # Only commit if something actually changed vs the pre-drag snapshot.
        final = _read_rgba()
        changes = {k: v for k, v in final.items() if abs(v - before[k]) > 1e-9}
        if not changes:
            return
        # Refresh swatch from final values.
        nonlocal r_val, g_val, b_val, a_val
        r_val, g_val, b_val, a_val = final[r_key], final[g_key], final[b_key], final[a_key]
        _update_swatch()
        self.subtitle_params_changed.emit(clip.clip_id, clip.track_id, changes)

    for key, val, display in (
        (r_key, r_val, "R"), (g_key, g_val, "G"),
        (b_key, b_val, "B"), (a_key, a_val, "A"),
    ):
        drag = DragValue(display,
                         param_defs[key].min_val, param_defs[key].max_val,
                         float(getattr(clip, key)),
                         decimals=param_defs[key].decimals)
        drag.setSingleStep(param_defs[key].step)
        drag.setFixedValueWidth(48)

        def _on_changed(v, k=key) -> None:
            if self._updating:
                return
            # During an active drag session, record the value but don't emit a
            # per-tick command — the merged patch is emitted on interaction_ended.
            if session_state["before"] is not None:
                session_state["pending"][k] = float(v)
                # Live swatch refresh for immediate feedback.
                nonlocal r_val, g_val, b_val, a_val
                cur = _read_rgba()
                r_val, g_val, b_val, a_val = cur[r_key], cur[g_key], cur[b_key], cur[a_key]
                _update_swatch()
            else:
                # No active drag (e.g. spinner step) → emit single field.
                self._emit_section_param(section, k, v)

        drag.valueChanged.connect(_on_changed)
        drag.interaction_started.connect(_begin_color_session)
        drag.interaction_ended.connect(_commit_color_session)
        h.addWidget(drag)
        section._param_widgets[key] = {
            "type": "float", "widget": drag,
            "min_val": param_defs[key].min_val, "max_val": param_defs[key].max_val,
            "step": param_defs[key].step, "decimals": param_defs[key].decimals,
        }

    h.addStretch()
    return row


def _build_subtitle_fill_section(self: EditPropertiesPanel, clip) -> None:
    """填充色（HDR RGBA + 色块）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:fill"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_fill", "Fill"),
        parameters={
            "fill_r": clip.fill_r, "fill_g": clip.fill_g,
            "fill_b": clip.fill_b, "fill_a": clip.fill_a,
        },
        param_defs=SUBTITLE_FILL_PARAM_DEFS,
        node_color="#4B7BEC",
    )
    cl = section.content_layout()
    cl.addWidget(self._build_subtitle_color_button(
        section, clip, "fill",
        "fill_r", "fill_g", "fill_b", "fill_a",
        I18N.tr("edit.properties.color", "Color")))


def _build_subtitle_outline_section(self: EditPropertiesPanel, clip) -> None:
    """描边（enable + width + color）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:outline"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_outline", "Outline"),
        parameters={
            "outline_enable": clip.outline_enable,
            "outline_width": clip.outline_width,
            "outline_r": clip.outline_r, "outline_g": clip.outline_g,
            "outline_b": clip.outline_b, "outline_a": clip.outline_a,
        },
        param_defs=SUBTITLE_OUTLINE_PARAM_DEFS,
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    cl.addWidget(self._build_bool_row(
        section, "outline_enable", clip.outline_enable,
        label_text=I18N.tr("edit.properties.enable", "Enable")))
    cl.addWidget(self._build_numeric_row(
        section, "outline_width", clip.outline_width,
        SUBTITLE_OUTLINE_PARAM_DEFS["outline_width"]))
    cl.addWidget(self._build_subtitle_color_button(
        section, clip, "outline",
        "outline_r", "outline_g", "outline_b", "outline_a",
        I18N.tr("edit.properties.color", "Color")))


def _build_subtitle_shadow_section(self: EditPropertiesPanel, clip) -> None:
    """阴影（enable + offset + blur + color）。"""
    section_id = f"subtitle:{clip.clip_id}:{clip.track_id}:shadow"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.subtitle_shadow", "Shadow"),
        parameters={
            "shadow_enable": clip.shadow_enable,
            "shadow_offset_x": clip.shadow_offset_x,
            "shadow_offset_y": clip.shadow_offset_y,
            "shadow_blur": clip.shadow_blur,
            "shadow_r": clip.shadow_r, "shadow_g": clip.shadow_g,
            "shadow_b": clip.shadow_b, "shadow_a": clip.shadow_a,
        },
        param_defs=SUBTITLE_SHADOW_PARAM_DEFS,
        node_color="#4B7BEC",
    )
    cl = section.content_layout()

    cl.addWidget(self._build_bool_row(
        section, "shadow_enable", clip.shadow_enable,
        label_text=I18N.tr("edit.properties.enable", "Enable")))
    cl.addWidget(self._build_numeric_row(
        section, "shadow_offset_x", clip.shadow_offset_x,
        SUBTITLE_SHADOW_PARAM_DEFS["shadow_offset_x"], enable_slider=False))
    cl.addWidget(self._build_numeric_row(
        section, "shadow_offset_y", clip.shadow_offset_y,
        SUBTITLE_SHADOW_PARAM_DEFS["shadow_offset_y"], enable_slider=False))
    cl.addWidget(self._build_numeric_row(
        section, "shadow_blur", clip.shadow_blur,
        SUBTITLE_SHADOW_PARAM_DEFS["shadow_blur"]))
    cl.addWidget(self._build_subtitle_color_button(
        section, clip, "shadow",
        "shadow_r", "shadow_g", "shadow_b", "shadow_a",
        I18N.tr("edit.properties.color", "Color")))


def _build_transform_section(self: EditPropertiesPanel, clip: ClipInstance,
                             collapsed: bool = False) -> None:
    """Transform section（translate/scale/rotation/pivot + scale lock）。

    像素空间模型，与合成页 TransformNode 完全一致。
    """
    section_id = f"clip:{clip.clip_id}:transform"
    t = clip.transform
    parameters = {
        "translate_x": t.translate_x, "translate_y": t.translate_y,
        "scale_x": t.scale_x, "scale_y": t.scale_y,
        "rotation": t.rotation,
        "pivot_mode": t.pivot_mode, "pivot_x": t.pivot_x, "pivot_y": t.pivot_y,
    }
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.transform", "Transform"),
        parameters=parameters,
        param_defs=TRANSFORM_PARAM_DEFS,
        node_color="#555555",
        collapsed=collapsed,
    )
    cl = section.content_layout()

    # 布局完全对齐合成页 TransformNode（无分组，扁平 6 行）：
    #   Translate(pair) → Scale(pair, 默认锁⛓, 1:1) → Rotation(slider) →
    #   Pivot Mode(combo) → Pivot(pair, center 灰显只读) → Filter(combo)

    # Translate X / Y（同一行，像素；G16：每轴 ◆ → transform.translate_x/y）
    cl.addWidget(self._build_xy_param_row(
        section, "translate", "translate_x", "translate_y",
        t.translate_x, t.translate_y,
        TRANSFORM_PARAM_DEFS["translate_x"], TRANSFORM_PARAM_DEFS["translate_y"],
        display_name=I18N.tr("edit.properties.translate", "Translate"),
        enable_keyframe=True, keyframe_prefix="transform.",
    ))

    # Scale X / Y（同一行，默认锁 ⛓ 1:1 等比，与合成页一致；G16：每轴 ◆）
    cl.addWidget(self._build_xy_param_row(
        section, "scale", "scale_x", "scale_y",
        t.scale_x, t.scale_y,
        TRANSFORM_PARAM_DEFS["scale_x"], TRANSFORM_PARAM_DEFS["scale_y"],
        display_name=I18N.tr("edit.properties.scale", "Scale"),
        enable_link=True, link_default_on=True,
        enable_keyframe=True, keyframe_prefix="transform.",
    ))

    # Rotation（G16：◆ → transform.rotation）
    cl.addWidget(self._build_numeric_row(section, "rotation", t.rotation,
                                         TRANSFORM_PARAM_DEFS["rotation"],
                                         enable_keyframe=True,
                                         keyframe_param="transform.rotation"))

    # Pivot Mode（下拉：Center / Custom）
    pivot_mode_row = self._build_combo_row(
        section, "pivot_mode", t.pivot_mode,
        _TRANSFORM_PIVOT_MODES, labels=_TRANSFORM_PIVOT_LABELS,
        pdef=TRANSFORM_PARAM_DEFS["pivot_mode"],
    )
    cl.addWidget(pivot_mode_row)

    # Pivot X / Y（同一行，无锁；center 模式下整行灰显只读，与合成页一致）
    pivot_row = self._build_xy_param_row(
        section, "pivot", "pivot_x", "pivot_y",
        t.pivot_x, t.pivot_y,
        TRANSFORM_PARAM_DEFS["pivot_x"], TRANSFORM_PARAM_DEFS["pivot_y"],
        display_name=I18N.tr("edit.properties.pivot", "Pivot"),
        enable_sliders=False,
    )
    pivot_editable = (t.pivot_mode == "custom")
    pivot_row.setEnabled(pivot_editable)
    cl.addWidget(pivot_row)

    # 钩在 pivot_mode combo 上：切换时启用/禁用 pivot 行（灰显只读）。
    combo_info = section._param_widgets.get("pivot_mode", {})
    pivot_combo = combo_info.get("widget") if combo_info else None
    if pivot_combo is not None:
        def _on_pivot_mode_changed(idx, pr=pivot_row,
                                   opts=list(_TRANSFORM_PIVOT_MODES)):
            if self._updating:
                return
            editable = (0 <= idx < len(opts) and opts[idx] == "custom")
            pr.setEnabled(editable)
        pivot_combo.currentIndexChanged.connect(_on_pivot_mode_changed)


def _on_scale_lock_toggled(self: EditPropertiesPanel, checked: bool) -> None:
    # make_link_button 自管图标状态（toggle 时切换橙/灰），这里只同步数据层标志，
    # 供 _emit_transform_param 决定 scale_x/scale_y 是否 1:1 联动。
    self._scale_locked = checked


def _build_crop_section(self: EditPropertiesPanel, clip: ClipInstance,
                        collapsed: bool = False) -> None:
    """Crop section（left/right/top/bottom 用百分比 0-50，feather 0-100）。"""
    section_id = f"clip:{clip.clip_id}:crop"
    c = clip.crop
    parameters = {
        "left": c.left * 100.0, "right": c.right * 100.0,
        "top": c.top * 100.0, "bottom": c.bottom * 100.0,
        "feather": c.feather,
    }
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.crop", "Crop"),
        parameters=parameters,
        param_defs=CROP_PARAM_DEFS,
        node_color="#555555",
        collapsed=collapsed,
    )
    cl = section.content_layout()
    for key in ("left", "right", "top", "bottom", "feather"):
        cl.addWidget(self._build_numeric_row(section, key, parameters[key],
                                             CROP_PARAM_DEFS[key]))


def _build_raw_section(self: EditPropertiesPanel, clip: ClipInstance,
                       collapsed: bool = False) -> None:
    """TRAW RAW section（批 2）：WB 增益 / EI 曝光 / 预览 OOTF。

    非破坏 uniform 路径：改动经 clip_param_changed("raw") → RawParams →
    debayer shader uniform，零重解码实时生效。仅 RAW 素材（TRAW）消费。
    """
    from src.features.edit.core.clip_params import RawParams as _RawParams

    current = getattr(clip, "raw", None) or _RawParams()
    section_id = f"clip:{clip.clip_id}:raw"
    parameters = {
        "wb_gain_r": current.wb_gain_r,
        "wb_gain_g": current.wb_gain_g,
        "wb_gain_b": current.wb_gain_b,
        "exposure_stops": current.exposure_stops,
        "display_ootf": current.display_ootf,
        "black_level": current.black_level,
        "color_space": current.color_space,
        "gamma": current.gamma,
    }
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.raw", "RAW"),
        parameters=parameters,
        param_defs=RAW_PARAM_DEFS,
        node_color="#8a6d3b",
        collapsed=collapsed,
    )
    cl = section.content_layout()
    for key in ("wb_gain_r", "wb_gain_g", "wb_gain_b", "exposure_stops"):
        cl.addWidget(self._build_numeric_row(section, key, parameters[key],
                                             RAW_PARAM_DEFS[key]))
    # 2026-09-21 复查：display_ootf 布尔复选框移除——RC3 起 gamma 字段
    # 是唯一查看伽马驱动（"preview-2.2"=OOTF 开 / "linear"=关 / "srgb"），
    # 复选框在 clip.raw 存在时永远不生效（RawParams.gamma 恒有值，布尔
    # 只在 gamma 为空时折算）。RawParams.display_ootf 字段保留做旧工程
    # 序列化兼容（resolve_debayer_params 的折算兜底）。
    # RC8 补全：黑位数值行（码值域；TRWM as-shot 值经 RC4 预览回退生效）
    cl.addWidget(self._build_numeric_row(section, "black_level",
                                         parameters["black_level"],
                                         RAW_PARAM_DEFS["black_level"]))
    # RC3：色彩空间（IDT 目标；Treat-As 语义，非破坏）+ 查看伽马
    from src.shared.video.camera_gamut_matrices import get_camera_gamut
    gamut_labels = [
        I18N.tr("edit.raw.gamut.native", "Native (Camera)")
        if gid == "native"
        else I18N.tr(f"edit.raw.gamut.{gid}",
                     get_camera_gamut(gid).label if get_camera_gamut(gid)
                     else gid)
        for gid in _RAW_COLOR_SPACES
    ]
    cl.addWidget(self._build_combo_row(
        section, "color_space", parameters["color_space"],
        _RAW_COLOR_SPACES,
        labels=gamut_labels,
        pdef=RAW_PARAM_DEFS["color_space"]))
    # i18n 键与选项 id 解耦：'preview-2.2' → preview_2_2（'.' 与 '-'
    # 都会破坏 I18N.tr 的按点分层遍历）
    gamma_labels = [
        I18N.tr(f"edit.raw.gamma.{g.replace('.', '_').replace('-', '_')}", g)
        for g in _RAW_GAMMAS]
    cl.addWidget(self._build_combo_row(
        section, "gamma", parameters["gamma"],
        _RAW_GAMMAS,
        labels=gamma_labels,
        pdef=RAW_PARAM_DEFS["gamma"]))


def _build_opacity_section(self: EditPropertiesPanel, clip: ClipInstance,
                           collapsed: bool = False) -> None:
    """Opacity section（百分比 0-100，内部存 0-1.0）。

    A1: 视觉片段在此分组下追加"混合模式"下拉（16 种标准模式），
    切换经 clip_param_changed("blend_mode") 走 SetClipBlendModeCommand。
    """
    section_id = f"clip:{clip.clip_id}:opacity"
    parameters = {"value": clip.opacity * 100.0}
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.opacity", "Opacity"),
        parameters=parameters,
        param_defs=OPACITY_PARAM_DEFS,
        node_color="#555555",
        collapsed=collapsed,
    )
    cl = section.content_layout()
    # G16：◆ 直接落在 opacity 参数上（动画参数名就是 "opacity"）。
    # R10：UI 显示 0-100%，模型域 0-1——注册取值/回显换算。
    cl.addWidget(self._build_numeric_row(
        section, "value", parameters["value"],
        OPACITY_PARAM_DEFS["value"],
        enable_keyframe=True,
        keyframe_param="opacity",
        to_model=lambda v: float(v) / 100.0,
        from_model=lambda v: float(v) * 100.0))

    # A1: Blend mode dropdown (visual clips only)
    media_type = getattr(self, "_current_clip_media_type", None)
    if media_type not in ("video", "image", "comp_clip", "motion_graphic"):
        return
    from PyQt6.QtWidgets import QComboBox, QLabel

    row = QHBoxLayout()
    row.addWidget(QLabel(I18N.tr("edit.properties.blend_mode", "Blend Mode")))
    combo = QComboBox()
    for mode in ClipBlendMode:
        label = I18N.tr(
            f"edit.blend_mode.{mode.value}", mode.value.replace('_', ' ').title())
        combo.addItem(label, mode.value)
    current = getattr(clip, "blend_mode", ClipBlendMode.NORMAL)
    index = combo.findData(getattr(current, "value", "normal"))
    if index >= 0:
        combo.setCurrentIndex(index)

    def _on_blend_mode_changed() -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self.clip_param_changed.emit(
                clip.clip_id, "blend_mode", "mode", combo.currentData())
        finally:
            self._updating = False

    combo.currentIndexChanged.connect(_on_blend_mode_changed)
    row.addWidget(combo, 1)
    cl.addLayout(row)


def _build_solid_color_section(self: EditPropertiesPanel, clip: ClipInstance,
                                collapsed: bool = False) -> None:
    """Solid Color generator: full Constant-node-style parameter UI.

    Supports all four generation modes (solid / checker / gradient / radial)
    with mode-aware parameter visibility, color pickers, and gradient presets.
    Mirrors the Composite page's ConstantNode properties panel.

    Parameters are persisted via :attr:`generator_params_changed` signal;
    the controller writes them to ``asset.metadata["generator_params"]``.
    """
    section_id = f"clip:{clip.clip_id}:solid_color"

    # Resolve current params (fall back to defaults for legacy assets)
    params = getattr(self, "_current_generator_params", None)
    if not params:
        rgb = getattr(self, "_current_solid_color", None) or (0.0, 0.6, 0.8)
        params = {
            "mode": "solid",
            "color_r": rgb[0], "color_g": rgb[1], "color_b": rgb[2], "color_a": 1.0,
            "color2_r": 1.0, "color2_g": 1.0, "color2_b": 1.0, "color2_a": 1.0,
            "checker_size": 64,
            "start_x": 0.0, "start_y": 0.5, "end_x": 1.0, "end_y": 0.5,
            "center_x": 0.5, "center_y": 0.5, "radial_radius": 0.5,
            "feather": 0.0,
        }

    mode = params.get("mode", "solid")

    # ParamDefs for numeric rows
    _PDEFS = {
        "checker_size": ParamDef("checker_size", "int", 64, min_val=2, max_val=400, step=1),
        "start_x": ParamDef("start_x", "float", 0.0, min_val=-2.0, max_val=2.0, step=0.01),
        "start_y": ParamDef("start_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        "end_x": ParamDef("end_x", "float", 1.0, min_val=-2.0, max_val=2.0, step=0.01),
        "end_y": ParamDef("end_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        "center_x": ParamDef("center_x", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        "center_y": ParamDef("center_y", "float", 0.5, min_val=-2.0, max_val=2.0, step=0.01),
        "radial_radius": ParamDef("radial_radius", "float", 0.5, min_val=0.001, max_val=2.0, step=0.01),
        "feather": ParamDef("feather", "float", 0.0, min_val=-1.0, max_val=1.0, step=0.01),
    }

    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.solid_color", "Solid Color"),
        parameters=dict(params),
        param_defs=_PDEFS,
        node_color="#6b5b95",
        collapsed=collapsed,
    )
    cl = section.content_layout()

    # ── Helper: emit full params to controller ──
    def _emit_params(*_args) -> None:
        if self._updating:
            return
        self.generator_params_changed.emit(clip.clip_id, clip.asset_id, dict(section.parameters))

    # ── Helper: update a param and emit ──
    def _set_param(key: str, value) -> None:
        section.parameters[key] = value
        _emit_params()

    # ── Helper: rewire a numeric row onto _set_param (AUD-20004) ──
    def _rewire_numeric(key: str, cast=float) -> None:
        """把数值行重接到 _set_param，同时保留双向控件同步。

        AUD-20004: 旧代码用裸 ``valueChanged.disconnect()`` 重接，把
        ``_build_numeric_row`` 内建的 drag→slider 同步闭包一并断开——此后
        直接拖 DragValue 时滑块 thumb 停在旧位置（滑块→数值方向仍通）。
        重接后的 drag handler 在提交参数前先把滑块 blockSignals 归位；
        slider handler 回写 drag 显示（对侧均 blockSignals 防递归）。
        """
        info = section._param_widgets.get(key)
        if not info or "widget" not in info:
            return
        drag = info["widget"]
        slider = info.get("slider")

        def _on_drag(v, sl=slider, inf=info, k=key, c=cast) -> None:
            if self._updating:
                return
            if sl is not None:
                sl.blockSignals(True)
                sl.setValue(self._value_to_slider(float(v), inf))
                sl.blockSignals(False)
            _set_param(k, c(v))

        drag.valueChanged.disconnect()
        drag.valueChanged.connect(_on_drag)

        if slider is None:
            return

        def _on_slider(v, d=drag, inf=info, k=key, c=cast) -> None:
            if self._updating:
                return
            fv = v if inf.get("type") == "int" else v * inf["step"] + inf["min_val"]
            d.blockSignals(True)
            d.setValue(fv)
            d.blockSignals(False)
            _set_param(k, c(fv))

        slider.valueChanged.disconnect()
        slider.valueChanged.connect(_on_slider)

    # ── Mode combo ──
    mode_options = ["solid", "checker", "gradient", "radial"]
    mode_labels = ["Solid", "Checker", "Gradient", "Radial"]
    mode_row = self._make_properties_row()
    mh = QHBoxLayout(mode_row)
    mh.setContentsMargins(0, 2, 0, 2)
    mh.setSpacing(4)
    mode_label = self._make_param_label("Mode", section)
    mh.addWidget(mode_label)
    mode_combo = ChevronComboBox()
    mode_combo.addItems(mode_labels)
    if mode in mode_options:
        mode_combo.setCurrentIndex(mode_options.index(mode))
    apply_combo_dropdown_style(mode_combo)

    def _on_mode_changed(idx: int) -> None:
        if self._updating or idx < 0 or idx >= len(mode_options):
            return
        new_mode = mode_options[idx]
        section.parameters["mode"] = new_mode
        self.generator_params_changed.emit(clip.clip_id, clip.asset_id, dict(section.parameters))
        # Rebuild the section to show/hide mode-specific controls
        self._rebuild_solid_color_section(clip)

    mode_combo.currentIndexChanged.connect(_on_mode_changed)
    mh.addWidget(mode_combo, 1)
    cl.addWidget(mode_row)

    # ── Primary Color button ──
    def _rgba_to_hex(r: float, g: float, b: float) -> str:
        ri = max(0, min(255, int(float(r) * 255.0 + 0.5)))
        gi = max(0, min(255, int(float(g) * 255.0 + 0.5)))
        bi = max(0, min(255, int(float(b) * 255.0 + 0.5)))
        return f"#{ri:02x}{gi:02x}{bi:02x}"

    def _make_color_button(label_text, r_key, g_key, b_key, a_key):
        from src.shared.assets.icon_manager import icon_color_picker
        row = self._make_properties_row()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 2, 0, 2)
        rh.setSpacing(4)
        lbl = self._make_param_label(label_text, section)
        rh.addWidget(lbl)

        # Square color swatch (28x28, no text — matches Constant node)
        btn = QPushButton()
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(f"{label_text} — click to pick color")

        def _update_swatch() -> None:
            r_i = max(0, min(255, int(float(section.parameters.get(r_key, 0)) * 255.0 + 0.5)))
            g_i = max(0, min(255, int(float(section.parameters.get(g_key, 0)) * 255.0 + 0.5)))
            b_i = max(0, min(255, int(float(section.parameters.get(b_key, 0)) * 255.0 + 0.5)))
            a_i = max(0, min(255, int(float(section.parameters.get(a_key, 1.0)) * 255.0 + 0.5)))
            btn.setStyleSheet(
                f"QPushButton {{ background-color: rgba({r_i}, {g_i}, {b_i}, {a_i}); "
                f"border: 1px solid #555; border-radius: 3px; }} "
                f"QPushButton:hover {{ border: 1px solid #888; }}"
            )

        _update_swatch()

        def _pick() -> None:
            from src.shared.color import get_color
            r_i = max(0, min(255, int(float(section.parameters.get(r_key, 0)) * 255.0 + 0.5)))
            g_i = max(0, min(255, int(float(section.parameters.get(g_key, 0)) * 255.0 + 0.5)))
            b_i = max(0, min(255, int(float(section.parameters.get(b_key, 0)) * 255.0 + 0.5)))
            a_i = max(0, min(255, int(float(section.parameters.get(a_key, 1.0)) * 255.0 + 0.5)))
            current = QColor(r_i, g_i, b_i, a_i)

            def _apply(qc: QColor) -> None:
                section.parameters[r_key] = qc.redF()
                section.parameters[g_key] = qc.greenF()
                section.parameters[b_key] = qc.blueF()
                section.parameters[a_key] = qc.alphaF()
                _update_swatch()
                _emit_params()

            def _on_live(qc: QColor) -> None:
                _apply(qc)

            result = get_color(parent=self, initial=current, on_live=_on_live)
            if result is not None:
                _apply(result)

        btn.clicked.connect(_pick)
        rh.addWidget(btn)

        # Eyedropper button (pick color from screen)
        picker_btn = QPushButton()
        picker_btn.setFixedSize(28, 28)
        picker_btn.setIcon(icon_color_picker())
        picker_btn.setIconSize(QSize(16, 16))
        picker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        picker_btn.setToolTip(I18N.tr("node_props.pick_color_from_screen", "Pick color from screen"))
        picker_btn.setStyleSheet(
            "QPushButton { background: #2c2d33; border: 1px solid #444; "
            "border-radius: 3px; } QPushButton:hover { background: #3a3b42; }"
        )

        def _start_eyedropper() -> None:
            from src.shared.color import EyeDropperOverlay
            # 存为实例属性防止 GC 回收（QWidget 引用丢失会被立即销毁）
            self._active_dropper = EyeDropperOverlay()
            self._active_dropper.colorPicked.connect(_apply_from_eyedropper)
            self._active_dropper.start()

        def _apply_from_eyedropper(qc: QColor) -> None:
            section.parameters[r_key] = qc.redF()
            section.parameters[g_key] = qc.greenF()
            section.parameters[b_key] = qc.blueF()
            section.parameters[a_key] = qc.alphaF()
            _update_swatch()
            _emit_params()

        picker_btn.clicked.connect(_start_eyedropper)
        rh.addWidget(picker_btn)
        rh.addStretch()
        return row

    cl.addWidget(_make_color_button("Color", "color_r", "color_g", "color_b", "color_a"))

    # ── Secondary Color + Swap (checker / gradient / radial) ──
    needs_color2 = mode in ("checker", "gradient", "radial")
    if needs_color2:
        # Swap button row
        swap_row = self._make_properties_row()
        sh = QHBoxLayout(swap_row)
        sh.setContentsMargins(0, 2, 0, 2)
        sh.setSpacing(4)
        swap_lbl = self._make_param_label("", section)
        sh.addWidget(swap_lbl)
        swap_btn = QToolButton()
        swap_btn.setText("\u21c4")
        swap_btn.setToolTip(I18N.tr("node_props.swap_colors", "Swap primary and secondary colors"))
        swap_btn.setFixedSize(28, 20)
        swap_btn.setStyleSheet(
            "QToolButton { background: #2c2d33; border: 1px solid #444; "
            "border-radius: 3px; color: #ccc; font-size: 11px; }"
            "QToolButton:hover { background: #3a3b42; }"
        )

        def _swap_colors() -> None:
            for suffix in ("_r", "_g", "_b", "_a"):
                k1, k2 = f"color{suffix}", f"color2{suffix}"
                section.parameters[k1], section.parameters[k2] = \
                    section.parameters.get(k2, 0), section.parameters.get(k1, 0)
            _emit_params()
            self._rebuild_solid_color_section(clip)

        swap_btn.clicked.connect(_swap_colors)
        sh.addWidget(swap_btn)
        sh.addStretch()
        cl.addWidget(swap_row)

        cl.addWidget(_make_color_button("Color 2", "color2_r", "color2_g", "color2_b", "color2_a"))

    # ── Checker Size (checker mode) ──
    if mode == "checker":
        cl.addWidget(self._build_numeric_row(
            section, "checker_size", float(params.get("checker_size", 64)),
            _PDEFS["checker_size"], enable_slider=True))
        # AUD-20004: 经 _rewire_numeric 重接（保留 drag→slider 同步）。
        _rewire_numeric("checker_size", cast=int)

    # ── Gradient controls ──
    if mode == "gradient":
        # Start point
        cl.addWidget(self._build_numeric_row(
            section, "start_x", float(params.get("start_x", 0.0)),
            _PDEFS["start_x"], enable_slider=True))
        cl.addWidget(self._build_numeric_row(
            section, "start_y", float(params.get("start_y", 0.5)),
            _PDEFS["start_y"], enable_slider=True))
        # End point
        cl.addWidget(self._build_numeric_row(
            section, "end_x", float(params.get("end_x", 1.0)),
            _PDEFS["end_x"], enable_slider=True))
        cl.addWidget(self._build_numeric_row(
            section, "end_y", float(params.get("end_y", 0.5)),
            _PDEFS["end_y"], enable_slider=True))

        # Wire numeric rows to our emit (AUD-20004: keep drag↔slider sync)
        for key in ("start_x", "start_y", "end_x", "end_y"):
            _rewire_numeric(key, cast=float)

        # Direction presets
        grad_presets = [
            ("\u2194", 0.0, 0.5, 1.0, 0.5),   # horizontal L→R
            ("\u2195", 0.5, 0.0, 0.5, 1.0),   # vertical T→B
            ("\u2921", 0.0, 0.0, 1.0, 1.0),   # diagonal TL→BR
            ("\u2922", 1.0, 0.0, 0.0, 1.0),   # diagonal TR→BL
        ]
        preset_row = self._make_properties_row()
        ph = QHBoxLayout(preset_row)
        ph.setContentsMargins(0, 2, 0, 2)
        ph.setSpacing(4)
        preset_lbl = self._make_param_label("Preset", section)
        ph.addWidget(preset_lbl)
        cur_sx = float(params.get("start_x", 0.0))
        cur_sy = float(params.get("start_y", 0.5))
        cur_ex = float(params.get("end_x", 1.0))
        cur_ey = float(params.get("end_y", 0.5))
        for icon, psx, psy, pex, pey in grad_presets:
            btn = QToolButton()
            btn.setText(icon)
            btn.setFixedSize(28, 20)
            is_active = (abs(cur_sx - psx) < 1e-3 and abs(cur_sy - psy) < 1e-3
                         and abs(cur_ex - pex) < 1e-3 and abs(cur_ey - pey) < 1e-3)
            base_style = ("QToolButton { background: #2c2d33; border: 1px solid #444; "
                          "border-radius: 3px; color: #ccc; font-size: 11px; }"
                          "QToolButton:hover { background: #3a3b42; }")
            if is_active:
                base_style = ("QToolButton { background: #3a6ea5; border: 1px solid #5a9fd4; "
                              "border-radius: 3px; color: #fff; font-size: 11px; }"
                              "QToolButton:hover { background: #4a7eb5; }")
            btn.setStyleSheet(base_style)

            def _apply_preset(_checked=False, sx=psx, sy=psy, ex=pex, ey=pey) -> None:
                section.parameters["start_x"] = sx
                section.parameters["start_y"] = sy
                section.parameters["end_x"] = ex
                section.parameters["end_y"] = ey
                _emit_params()
                self._rebuild_solid_color_section(clip)

            btn.clicked.connect(_apply_preset)
            ph.addWidget(btn)
        ph.addStretch()
        cl.addWidget(preset_row)

    # ── Radial controls ──
    if mode == "radial":
        cl.addWidget(self._build_numeric_row(
            section, "center_x", float(params.get("center_x", 0.5)),
            _PDEFS["center_x"], enable_slider=True))
        cl.addWidget(self._build_numeric_row(
            section, "center_y", float(params.get("center_y", 0.5)),
            _PDEFS["center_y"], enable_slider=True))
        cl.addWidget(self._build_numeric_row(
            section, "radial_radius", float(params.get("radial_radius", 0.5)),
            _PDEFS["radial_radius"], enable_slider=True))

        for key in ("center_x", "center_y", "radial_radius"):
            _rewire_numeric(key, cast=float)

    # ── Feather (gradient + radial) ──
    if mode in ("gradient", "radial"):
        cl.addWidget(self._build_numeric_row(
            section, "feather", float(params.get("feather", 0.0)),
            _PDEFS["feather"], enable_slider=True))
        _rewire_numeric("feather", cast=float)


def _rebuild_solid_color_section(self: EditPropertiesPanel, clip: ClipInstance) -> None:
    """Destroy and rebuild the solid color section (used after mode change)."""
    section_id = f"clip:{clip.clip_id}:solid_color"
    old = self._sections.pop(section_id, None)
    if old is not None:
        self._content_layout.removeWidget(old)
        old.deleteLater()
    self._build_solid_color_section(clip, collapsed=False)


def _reset_solid_color_section(self: EditPropertiesPanel, section_id: str) -> None:
    """Solid Color section 的 Reset（AUD-20007）。

    此前通用 Reset 分支把参数发给 ``_route_clip_param``，而它没有
    ``solid_color`` 分组——信号被静默丢弃，``_reset_section_widgets`` 却把
    控件显示归位：面板显示与 asset metadata 从此分叉且无命令/无撤销。

    修复：恢复为与 ``_build_solid_color_section`` 无历史参数时相同的默认
    参数表（mode=solid、主色取片段 solid 色、数值参数取 ParamDef 默认），
    经 ``generator_params_changed`` 一次提交，然后同步
    ``_current_generator_params`` 并重建 section（mode-aware 行随之重建）。
    """
    section = self._sections.get(section_id)
    if section is None:
        return
    parts = section_id.split(":")
    clip = None
    if len(parts) >= 2:
        clip = next(
            (c for c in self._current_clips if c.clip_id == parts[1]), None)
    if clip is None:
        return
    rgb = getattr(self, "_current_solid_color", None) or (0.0, 0.6, 0.8)
    section.parameters = {
        "mode": "solid",
        "color_r": rgb[0], "color_g": rgb[1], "color_b": rgb[2], "color_a": 1.0,
        "color2_r": 1.0, "color2_g": 1.0, "color2_b": 1.0, "color2_a": 1.0,
        "checker_size": 64,
        "start_x": 0.0, "start_y": 0.5, "end_x": 1.0, "end_y": 0.5,
        "center_x": 0.5, "center_y": 0.5, "radial_radius": 0.5,
        "feather": 0.0,
    }
    if not self._updating:
        self.generator_params_changed.emit(
            clip.clip_id, clip.asset_id, dict(section.parameters))
    # _build_solid_color_section 读取 _current_generator_params——重置后
    # 必须同步，否则重建会显示旧参数。
    self._current_generator_params = dict(section.parameters)
    self._rebuild_solid_color_section(clip)


def _reset_transition_section(self: EditPropertiesPanel,
                               transition: TransitionInstance) -> None:
    """Transition section 的 Reset（AUD-20008）。

    转场行不经 ``section.param_changed`` 路由（各自直发
    ``transition_param_changed``），且 section 的 ``param_defs`` 为空——
    通用 Reset 分支此前完全静默。这里按默认值表逐项发命令，并把已注册
    的行控件显示归位（blockSignals 防递归）。音频转场没有空间参数。
    """
    transition_id = transition.transition_id
    is_audio = transition.transition_type in AUDIO_TRANSITION_TYPE_OPTIONS
    defaults = {
        key: value
        for key, value in _TRANSITION_RESET_DEFAULTS.items()
        if not (is_audio and key in _TRANSITION_SPATIAL_KEYS)
    }
    for key, value in defaults.items():
        self.transition_param_changed.emit(transition_id, key, value)

    section = self._sections.get(f"transition:{transition_id}")
    if section is None:
        return
    for key, value in defaults.items():
        info = section._param_widgets.get(key)
        if not info:
            continue
        widget = info.get("widget")
        slider = info.get("slider")
        if widget is not None:
            widget.blockSignals(True)
        if slider is not None:
            slider.blockSignals(True)
        try:
            if info.get("type") == "combo":
                options = info.get("options") or []
                if widget is not None and value in options:
                    widget.setCurrentIndex(options.index(value))
            else:
                if widget is not None and hasattr(widget, "setValue"):
                    widget.setValue(value)
                if slider is not None:
                    slider_scale = info.get("slider_scale", 1)
                    slider.setValue(int(round(float(value) * slider_scale)))
        finally:
            if widget is not None:
                widget.blockSignals(False)
            if slider is not None:
                slider.blockSignals(False)


def _build_speed_section(self: EditPropertiesPanel, clip: ClipInstance,
                         collapsed: bool = False) -> None:
    """Speed section（speed + reverse + preserve-pitch）。"""
    section_id = f"clip:{clip.clip_id}:speed"
    if isinstance(clip.speed, SpeedParams):
        speed_val = clip.speed.speed
        reverse_val = clip.speed.reverse
        preserve_pitch_val = clip.speed.preserve_pitch
    else:
        speed_val = float(clip.speed)
        reverse_val = False
        preserve_pitch_val = False
    parameters = {
        "speed": speed_val,
        "reverse": reverse_val,
        "preserve_pitch": preserve_pitch_val,
    }
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.speed", "Speed"),
        parameters=parameters,
        param_defs=SPEED_PARAM_DEFS,
        node_color="#555555",
        collapsed=collapsed,
    )
    cl = section.content_layout()
    cl.addWidget(self._build_numeric_row(section, "speed", speed_val,
                                         SPEED_PARAM_DEFS["speed"], enable_slider=True))
    cl.addWidget(self._build_bool_row(section, "reverse", reverse_val,
                                      I18N.tr("edit.properties.reverse", "Reverse")))

    # A3.2: 帧混合插值下拉（只列 nearest / frame blend；optical_flow 不可用故隐藏）
    from PyQt6.QtWidgets import QComboBox, QLabel as _QLabel

    current_interp = (
        clip.speed.interpolation
        if isinstance(clip.speed, SpeedParams)
        and clip.speed.interpolation in ("nearest", "frame_blend")
        else "nearest"
    )
    interp_row = QHBoxLayout()
    interp_row.addWidget(_QLabel(I18N.tr(
        "edit.properties.interpolation", "Interpolation")))
    interp_combo = QComboBox()
    interp_combo.addItem(I18N.tr("edit.properties.interp_nearest", "Nearest"), "nearest")
    interp_combo.addItem(I18N.tr("edit.properties.interp_frame_blend", "Frame Blend"), "frame_blend")
    idx = interp_combo.findData(current_interp)
    if idx >= 0:
        interp_combo.setCurrentIndex(idx)

    def _on_interp_changed() -> None:
        if self._updating:
            return
        self._updating = True
        try:
            base = clip.speed if isinstance(clip.speed, SpeedParams) else SpeedParams(speed=float(clip.speed))
            updated = base.with_interpolation(interp_combo.currentData())
            self.clip_param_changed.emit(clip.clip_id, "speed", "params", updated)
        finally:
            self._updating = False

    interp_combo.currentIndexChanged.connect(_on_interp_changed)
    interp_row.addWidget(interp_combo, 1)
    cl.addLayout(interp_row)
    # P1-08/F3.2：保调变速。预览（流式 WSOLA）与导出（整段 WSOLA）同
    # 25ms/7.5ms 参数；倒放/速度曲线两路一致回退变调并留痕。
    pp_row = self._build_bool_row(
        section, "preserve_pitch", preserve_pitch_val,
        I18N.tr("edit.properties.preserve_pitch", "Preserve Pitch"),
    )
    pp_row.setToolTip(I18N.tr(
        "edit.properties.preserve_pitch_tooltip",
        "Pitch-preserving speed change (WSOLA).\n"
        "Preview and export share the same algorithm and parameters.\n"
        "保调变速：实时预览与导出同参数 WSOLA；\n"
        "倒放/速度曲线暂不支持，自动回退变调并留痕。",
    ))
    cl.addWidget(pp_row)


def _build_effect_sections(self: EditPropertiesPanel, clip: ClipInstance) -> None:
    """Build one independent collapsible section per clip effect.

    Replaces the legacy single "Effect Stack" section (which held a list +
    toggle/remove buttons + a parameter container). Each effect now gets
    its own ``EditSectionWidget`` so its parameters are visible directly
    in the panel — mirroring the composite page's per-node sections.

    Section id format: ``clip:<clip_id>:effect:<effect_id>`` — this is the
    same format ``_on_section_param_changed`` already routes for effect
    parameter changes.

    Selection: the section whose effect_id matches ``self._selected_effect_id``
    is expanded; all others are collapsed. ``_selected_effect_id`` is set
    externally by the controller after add/select operations.
    """
    effects = clip.effects
    if isinstance(effects, list):
        effects = tuple(effects)
    if not effects:
        return

    effect_count = len(effects)
    for effect in effects:
        # Tolerate both EffectInstance and raw dict (legacy serialised form).
        if hasattr(effect, "effect_id"):
            effect_id = effect.effect_id
            effect_type = effect.effect_type
            is_enabled = effect.is_enabled
            params = effect.params
            plugin_identity = getattr(effect, "plugin_identity", None)
        elif isinstance(effect, dict):
            effect_id = effect.get("effect_id", "")
            effect_type = effect.get("effect_type", "")
            is_enabled = effect.get("is_enabled", True)
            params = effect.get("params", {})
            plugin_identity = effect.get("plugin_identity")
        else:
            continue

        if not effect_id or not effect_type:
            continue

        # Prefer the unified EffectCatalog spec — it carries the same
        # ParamDef the composite page uses, so the panel renders identical
        # controls. Fall back to the legacy EffectDefinition only when the
        # effect_type isn't in the catalog (e.g. legacy audio effects).
        from src.features.shared.effects import get_effect_catalog
        spec = get_effect_catalog().get_spec(effect_type)
        if spec is not None:
            display_name = spec.name
            param_defs_tuple = spec.param_defs
            # Blur shares one schema across five shader variants. Variant
            # visibility is applied to the constructed rows below, rather
            # than removing ParamDefs here; keeping the complete set allows
            # legacy radius keyframe data to remain addressable while the
            # user still sees only controls relevant to the active type.
        else:
            effect_def = self._registry.get_definition(effect_type)
            if effect_def is None:
                if effect_type.startswith("plugin:"):
                    # Keep missing plugins visible so they cannot disappear
                    # from the Inspector as a silent no-op.
                    display_name = effect_type.removeprefix("plugin:")
                    param_defs_tuple = ()
                else:
                    continue
            else:
                display_name = effect_def.name
                # Legacy path: convert ParamSchema → ParamDef (lossy).
                param_defs_tuple = tuple(
                    ParamDef(
                        name=pn,
                        ptype=("float" if s.type == "float"
                               else "int" if s.type == "int"
                               else "bool" if s.type == "bool"
                               else "option" if s.type == "choice"
                               else "str"),
                        default=s.default,
                        min_val=float(s.min_value) if s.min_value is not None else 0.0,
                        max_val=float(s.max_value) if s.max_value is not None else 1.0,
                        step=s.step,
                        decimals=0 if s.type == "int" else 2,
                        labels=list(s.choices) if s.choices else None,
                    )
                    for pn, s in effect_def.params_schema.items()
                )

        section_id = f"clip:{clip.clip_id}:effect:{effect_id}"
        is_selected = (self._selected_effect_id == effect_id)

        # param_defs dict for _add_section (it computes label widths).
        param_defs_for_section: Dict[str, ParamDef] = {p.name: p for p in param_defs_tuple}

        # 折叠状态：记忆优先（按 effect_type 跨片段记忆，同类效果共享）；
        # 无记忆时保持原逻辑 —— 刚选中/添加的特效展开，其余折叠。
        effect_type_key = f"effect:{effect_type}" if effect_type else "effect:unknown"
        collapsed = self._collapsed_for(effect_type_key, not is_selected)

        def _builder(section, _effect_id=effect_id, _pdefs=param_defs_tuple,
                     _params=params, _clip=clip):
            cl = section.content_layout()
            variant_rows: Dict[str, QWidget] = {}

            def update_blur_visibility(value: Any) -> None:
                """Toggle variant-specific rows without rebuilding the panel.

                Effect edits intentionally do not rebuild the inspector during
                a drag. Blur type changes still need immediate UI feedback, so
                only the already-created rows are shown/hidden here.
                """
                from src.features.composite.nodes.blur_node import BlurNode
                if "blur_type" not in _params:
                    for row in variant_rows.values():
                        row.setVisible(True)
                    return
                blur_type = "motion" if value == "directional" else str(value)
                hidden = BlurNode.blur_type_param_visibility(blur_type)
                for name, row in variant_rows.items():
                    row.setVisible(name not in hidden)

            # Track params already consumed by an X/Y pair row so their peer
            # is skipped in the regular single-widget loop below.
            rendered_as_peer: set = set()
            rendered_channels = False
            channel_names = {"channel_r", "channel_g", "channel_b", "channel_a"}
            pdef_names = {p.name for p in _pdefs}
            for pdef in _pdefs:
                if pdef.hidden:
                    continue
                if effect_type == "blur" and pdef.name in channel_names:
                    if not rendered_channels:
                        channel_row = self._create_effect_channel_group(
                            _clip, _effect_id, _params,
                        )
                        cl.addWidget(channel_row)
                        rendered_channels = True
                    continue
                # X/Y pair: this pdef declares a linked peer that also exists
                # in this effect's param defs. Render both as one chained row.
                peer_name = getattr(pdef, "linked", None)
                if (peer_name and peer_name in pdef_names
                        and pdef.name not in rendered_as_peer):
                    peer_pdef = next(p for p in _pdefs if p.name == peer_name)
                    if peer_pdef.hidden:
                        # peer 隐藏时（如 blur_type=bilateral 让 radius_y 隐藏），
                        # 退回单参数行，避免渲染一个残缺的 X/Y 行。
                        param_value = _params.get(pdef.name, pdef.default)
                        widget = self._create_effect_param_widget_from_pdef(
                            pdef, param_value, _effect_id,
                            on_value_changed=(
                                update_blur_visibility
                                if effect_type == "blur" and pdef.name == "blur_type"
                                else None
                            ),
                        )
                        if widget is not None:
                            cl.addWidget(widget)
                            variant_rows[pdef.name] = widget
                        continue
                    # Convention: the "_x" param is the left/X axis.
                    if pdef.name.endswith("_x"):
                        x_pdef, y_pdef = pdef, peer_pdef
                    else:
                        x_pdef, y_pdef = peer_pdef, pdef
                    x_val = _params.get(x_pdef.name, x_pdef.default)
                    y_val = _params.get(y_pdef.name, y_pdef.default)
                    xy_row = self._create_effect_xy_row(
                        _clip, _effect_id, x_pdef, y_pdef, x_val, y_val)
                    cl.addWidget(xy_row)
                    if effect_type == "blur":
                        variant_rows[x_pdef.name] = xy_row
                        variant_rows[y_pdef.name] = xy_row
                    rendered_as_peer.add(peer_name)
                    continue
                if pdef.name in rendered_as_peer:
                    continue
                param_value = _params.get(pdef.name, pdef.default)
                widget = self._create_effect_param_widget_from_pdef(
                    pdef, param_value, _effect_id,
                    on_value_changed=(
                        update_blur_visibility
                        if effect_type == "blur" and pdef.name == "blur_type"
                        else None
                    ),
                )
                if widget is not None:
                    cl.addWidget(widget)
                    if effect_type == "blur":
                        variant_rows[pdef.name] = widget

            if effect_type == "blur":
                update_blur_visibility(_params.get("blur_type", "box"))

        section = self._add_section(
            section_id=section_id,
            title=display_name,
            parameters=params,
            param_defs=param_defs_for_section,
            node_color="#8B5CF6",  # 紫色：视觉上区分特效 section 与原生参数 section
            builder=_builder,
            collapsed=collapsed,
            effect_mode=True,
        )
        # Reflect the instance's enabled state without emitting a signal.
        section.set_effect_enabled(bool(is_enabled))
        section.set_reorder_enabled(
            effect_count > 1,
            I18N.tr(
                "edit.properties.reorder_single_effect",
                "Add another effect before reordering",
            ),
        )
        # P-clip-FX: 记录 effect_type，按需显示「打开面板」按钮（mixer audio 效果）。
        section.set_effect_type(effect_type)
        if effect_type.startswith("plugin:"):
            try:
                from src.features.shared.effects.plugin_identity import (
                    resolve_plugin_status,
                )
                section.set_plugin_status(resolve_plugin_status(
                    plugin_identity,
                    effect_type,
                    catalog_spec=spec,
                ))
            except Exception:  # noqa: BLE001 — status UI is best-effort
                logger.debug("Failed to resolve plugin status", exc_info=True)


def _on_effect_enable_toggled(self: EditPropertiesPanel, section_id: str, enabled: bool) -> None:
    """Effect-section enable button → re-emit effect_enabled_changed.

    Parses ``clip:<clip_id>:effect:<effect_id>`` from section_id. Reuses
    the existing signal so the controller handler is unchanged.
    """
    parts = section_id.split(":")
    if len(parts) < 4:
        return
    clip_id = parts[1]
    effect_id = parts[3]
    self.effect_enabled_changed.emit(clip_id, effect_id, enabled)


def _on_effect_delete_requested(self: EditPropertiesPanel, section_id: str) -> None:
    """Effect-section delete button → re-emit effect_remove_requested."""
    parts = section_id.split(":")
    if len(parts) < 4:
        return
    clip_id = parts[1]
    effect_id = parts[3]
    self.effect_remove_requested.emit(clip_id, effect_id)


def _on_effect_open_dialog_requested(self: EditPropertiesPanel, section_id: str, effect_type: str) -> None:
    """P-clip-FX: 「打开面板」按钮 → re-emit effect_open_dialog_requested.

    Parses ``clip:<clip_id>:effect:<effect_id>`` from section_id and forwards
    (clip_id, effect_id, effect_type) so the controller can open the
    standalone mixer dialog for this clip-level audio effect.
    """
    parts = section_id.split(":")
    if len(parts) < 4:
        return
    clip_id = parts[1]
    effect_id = parts[3]
    self.effect_open_dialog_requested.emit(clip_id, effect_id, effect_type)


def _scroll_to_effect(self: EditPropertiesPanel, effect_id: str) -> None:
    """Expand and scroll the panel so the given effect section is at the top.

    Called by the controller after a drag/double-click add. The panel has
    already been rebuilt by the command stack side effect, so the target
    section exists. This method:
      1. marks the effect as the selected one (so a subsequent ``set_clip``
         keeps it expanded),
      2. expands the section if it was built collapsed,
      3. scrolls the scroll area so the section header sits at the top,
      4. briefly flashes the header background to draw the user's eye.

    Falls through silently if the section is not present (e.g. the effect
    was filtered out as unknown) — better than crashing on a stale id.
    """
    self._selected_effect_id = effect_id
    target_section: Optional[EditSectionWidget] = None
    for sid, section in self._sections.items():
        if sid.endswith(f":effect:{effect_id}"):
            target_section = section
            break
    if target_section is None:
        return

    if target_section._is_collapsed:
        target_section.toggle_collapse()

    # Defer the scroll until the layout has settled (toggle_collapse
    # invalidates layouts asynchronously). ensureWidgetVisible handles the
    # common case; for the "scroll to top" feel we also force the scrollbar.
    from PyQt6.QtCore import QTimer
    def _do_scroll() -> None:
        try:
            self._scroll.ensureWidgetVisible(target_section, 0, 0)
            bar = self._scroll.verticalScrollBar()
            if bar is not None:
                # mapTo target's top-left into the scroll area's content
                # widget (``self._content``) so the section header lands at
                # the top of the viewport.
                target_y = target_section.mapTo(self._content, QPoint(0, 0)).y()
                bar.setValue(max(0, target_y))
        except RuntimeError:
            # Widget may have been torn down between the timer firing and now.
            pass
    QTimer.singleShot(0, _do_scroll)

    # Header highlight flash — restore via QTimer so the original style
    # (gradient set in _update_header_style) comes back after 400ms.
    original_style = target_section._header.styleSheet()
    def _flash() -> None:
        try:
            target_section._header.setStyleSheet(
                original_style + "background-color: #8B5CF6;"
            )
        except RuntimeError:
            pass
    def _unflash() -> None:
        try:
            target_section._header.setStyleSheet(original_style)
        except RuntimeError:
            pass
    _flash()
    QTimer.singleShot(400, _unflash)


def _create_effect_xy_row(
    self: EditPropertiesPanel,
    clip: ClipInstance,
    effect_id: str,
    x_pdef: ParamDef,
    y_pdef: ParamDef,
    x_val: Any,
    y_val: Any,
) -> QWidget:
    """Effect X/Y pair row with a ⛓ chain lock (mirrors composite page).

    Layout: ``[label] [⛓] [x slider] [x value] [y slider] [y value]``.

    - chain ON by default → 1:1 equal linkage (X == Y); changing one axis
      immediately pushes the same value to the peer axis.
    - chain OFF → each axis emits its own single-param change independently.
    - When linked, ONE drag emits a single ``effect_params_changed`` signal
      carrying both (x_key, v) and (y_key, v); the controller wraps them
      into a MacroCommand so one Ctrl+Z undoes the whole drag.

    The peer axis's display is kept in sync at the widget layer
    (``blockSignals`` around ``setValue``), matching the "effect param
    changes do not rebuild the panel" convention.
    """
    is_int_x = x_pdef.ptype == "int"
    is_int_y = y_pdef.ptype == "int"

    def _slider_steps(pd: ParamDef) -> int:
        return max(int((pd.max_val - pd.min_val) / (pd.step or 1.0)), 1)

    def _value_to_slider(v: float, pd: ParamDef, is_int: bool) -> int:
        if is_int:
            return int(v)
        return int((float(v) - pd.min_val) / (pd.step or 1.0))

    def _slider_to_value(sv: int, pd: ParamDef, is_int: bool) -> float:
        if is_int:
            return int(sv)
        return sv * (pd.step or 1.0) + pd.min_val

    # Display name: derive from x param ("radius_x" → "Radius").
    base_label = x_pdef.name
    if base_label.endswith("_x"):
        base_label = base_label[:-2]
    label_text = base_label[:1].upper() + base_label[1:]
    label_text = I18N.tr(f"edit.properties.{base_label}", label_text)

    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(label_text)
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)

    # Chain toggle — linked ON by default (1:1 equal, like composite page).
    # 统一小锁图标：锁定=橙 / 解锁=灰（见 icon_manager.make_link_button）。
    chain = make_link_button(
        initial_linked=True,
        tooltip=I18N.tr("edit.properties.link_xy", "Link X and Y"),
    )
    h.addWidget(chain)

    def axis_marker(letter: str) -> QLabel:
        m = QLabel(letter)
        m.setStyleSheet("color: #888; font-size: 10px;")
        m.setFixedWidth(10)
        m.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return m

    def build_axis(pd: ParamDef, val: Any, is_int: bool, letter: str):
        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.enable_velocity_drag = True
        if is_int:
            slider.setRange(int(pd.min_val), int(pd.max_val))
            slider.setValue(int(val))
        else:
            slider.setRange(0, _slider_steps(pd))
            slider.setValue(_value_to_slider(float(val), pd, is_int))
        h.addWidget(slider, 1)
        h.addWidget(axis_marker(letter))
        drag = DragValue("", pd.min_val, pd.max_val, float(val), decimals=pd.decimals)
        drag.setSingleStep(pd.step)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        h.addWidget(drag)
        return drag, slider

    drag_x, slider_x = build_axis(x_pdef, x_val, is_int_x, "x")
    drag_y, slider_y = build_axis(y_pdef, y_val, is_int_y, "y")

    x_key, y_key = x_pdef.name, y_pdef.name
    cid = clip.clip_id if clip is not None else ""

    def push_to_peer(peer_drag, peer_slider, peer_pd, peer_is_int,
                     peer_key, peer_value):
        """Clamp + set the peer drag AND slider, then emit peer's change.

        Both the drag and the slider must stay in sync — otherwise the peer
        axis shows the new value in its drag box while its slider thumb stays
        at the old position. blockSignals prevents the programmatic setValue
        from recursively triggering drag/slider handlers.
        """
        pv = max(float(peer_pd.min_val), min(float(peer_pd.max_val), float(peer_value)))
        peer_drag.blockSignals(True)
        peer_drag.setValue(pv)
        peer_drag.blockSignals(False)
        peer_slider.blockSignals(True)
        peer_slider.setValue(_value_to_slider(pv, peer_pd, peer_is_int))
        peer_slider.blockSignals(False)
        return (peer_key, int(pv) if peer_is_int else pv)

    def emit_linked(this_key: str, this_val, peer_val) -> None:
        """Emit a single batched signal covering both axes (one undo)."""
        peer_key = y_key if this_key == x_key else x_key
        peer_pdef = y_pdef if this_key == x_key else x_pdef
        peer_is_int = is_int_y if this_key == x_key else is_int_x
        peer_change = push_to_peer(
            drag_y if this_key == x_key else drag_x,
            slider_y if this_key == x_key else slider_x,
            peer_pdef, peer_is_int, peer_key, peer_val,
        )
        self.effect_params_changed.emit(
            cid, effect_id,
            [(this_key, this_val), peer_change],
        )

    def emit_single(key: str, value) -> None:
        """Emit a single-param change (chain unlocked path)."""
        self.effect_param_changed.emit(cid, effect_id, key, value)

    # Wire X axis.
    def _x_drag_changed(v):
        # AUD-20003: 两种链锁状态下都先同步自身滑块。链锁分支的
        # emit_linked/push_to_peer 只推**对侧** drag+slider，此前 X 轴滑块
        # thumb 停在旧值，与数值框、对轴滑块显示不一致（Y 轴同理）。
        slider_x.blockSignals(True)
        slider_x.setValue(_value_to_slider(v, x_pdef, is_int_x))
        slider_x.blockSignals(False)
        if chain.isChecked():
            emit_linked(x_key, int(v) if is_int_x else v, v)
        else:
            emit_single(x_key, int(v) if is_int_x else v)

    def _x_slider_changed(sv) -> None:
        fv = _slider_to_value(sv, x_pdef, is_int_x)
        drag_x.blockSignals(True)
        drag_x.setValue(fv)
        drag_x.blockSignals(False)
        if chain.isChecked():
            emit_linked(x_key, fv, fv)
        else:
            emit_single(x_key, fv)

    drag_x.valueChanged.connect(_x_drag_changed)
    slider_x.valueChanged.connect(_x_slider_changed)

    # Wire Y axis (symmetric).
    def _y_drag_changed(v):
        # AUD-20003: 同 X 轴——链锁分支也要同步自身滑块。
        slider_y.blockSignals(True)
        slider_y.setValue(_value_to_slider(v, y_pdef, is_int_y))
        slider_y.blockSignals(False)
        if chain.isChecked():
            emit_linked(y_key, int(v) if is_int_y else v, v)
        else:
            emit_single(y_key, int(v) if is_int_y else v)

    def _y_slider_changed(sv) -> None:
        fv = _slider_to_value(sv, y_pdef, is_int_y)
        drag_y.blockSignals(True)
        drag_y.setValue(fv)
        drag_y.blockSignals(False)
        if chain.isChecked():
            emit_linked(y_key, fv, fv)
        else:
            emit_single(y_key, fv)

    drag_y.valueChanged.connect(_y_drag_changed)
    slider_y.valueChanged.connect(_y_slider_changed)

    return row


def _create_effect_channel_group(
    self: EditPropertiesPanel,
    clip: Optional[ClipInstance],
    effect_id: str,
    params: Dict[str, Any],
) -> QWidget:
    """Create the compact RGB channel toggle row used by Composite Blur.

    Alpha remains part of the serialized/evaluator schema for compatibility,
    but is intentionally not exposed in the Edit page where it is rarely
    useful. The RGB buttons are independent toggles and emit the same
    ``effect_param_changed`` signal as the former checkbox rows.
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)

    label = QLabel(I18N.tr("edit.properties.channels", "Channels"))
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    h.addWidget(label)

    channels = (
        ("channel_r", "R", "#d44"),
        ("channel_g", "G", "#4d4"),
        ("channel_b", "B", "#44d"),
    )
    for key, text, color in channels:
        button = QPushButton(text, row)
        button.setCheckable(True)
        button.setChecked(bool(params.get(key, True)))
        button.setFixedSize(28, 20)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(I18N.tr(
            "edit.properties.toggle_channel",
            f"Toggle {text} channel blur",
        ))
        button.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid #555;
                border-radius: 3px;
                color: #888;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton:checked {{
                color: {color};
                border-color: {color};
                background: rgba(255, 255, 255, 0.05);
            }}
            QPushButton:hover {{ border-color: #888; }}
        """)

        def _on_toggle(checked: bool, param_name: str = key) -> None:
            if clip is not None:
                self.effect_param_changed.emit(
                    clip.clip_id, effect_id, param_name, bool(checked),
                )

        button.toggled.connect(_on_toggle)
        h.addWidget(button)

    h.addStretch()
    return row


def _create_effect_param_widget_from_pdef(
    self: EditPropertiesPanel,
    pdef: ParamDef,
    value: Any,
    effect_id: str,
    on_value_changed: Optional[Callable[[Any], None]] = None,
) -> Optional[QWidget]:
    """Build a parameter row from a composite ``ParamDef``.

    This is the Phase 2 control factory: it reads the same ``ParamDef``
    the composite node-properties panel uses, so effect controls look
    identical across both pages. Supported ptypes:

      * ``float``/``int`` → [label][ToposClickableSlider][DragValue]
      * ``bool``           → [label][QCheckBox]
      * ``option``         → [label][ChevronComboBox] (labels from pdef.labels)
      * others             → read-only [label][value] (rare for current effects)

    ``color``/``vec2``/``vec3``/``channel`` are not yet wired (none of
    the MVP/Phase 2 effects use them); they fall through to read-only.
    Phase 3 can add them here without touching the section builder.
    """
    label_text = pdef.name.replace("_", " ").title()
    clip = self._current_clips[0] if self._current_clips else None

    if pdef.ptype in ("float", "int"):
        is_int = pdef.ptype == "int"
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        h.addWidget(label)

        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.enable_velocity_drag = True
        if is_int:
            slider.setRange(int(pdef.min_val), int(pdef.max_val))
            slider.setValue(int(value))
        else:
            steps = max(int((pdef.max_val - pdef.min_val) / (pdef.step or 1.0)), 1)
            slider.setRange(0, steps)
            slider.setValue(int((float(value) - pdef.min_val) / (pdef.step or 1.0)))
        h.addWidget(slider, 1)

        drag = DragValue(
            "", pdef.min_val, pdef.max_val, float(value),
            decimals=pdef.decimals,
        )
        drag.setSingleStep(pdef.step)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

        def _slider_changed(v, pn=pdef.name, eid=effect_id, d=drag, inf={
            "type": "int" if is_int else "float",
            "min_val": pdef.min_val, "step": pdef.step,
        }):
            if self._updating:
                return  # AUD-20011: 程序化更新期间抑制重入发射
            d.blockSignals(True)
            fv = v if inf["type"] == "int" else v * inf["step"] + inf["min_val"]
            d.setValue(fv)
            d.blockSignals(False)
            if clip:
                self.effect_param_changed.emit(
                    clip.clip_id, eid, pn, int(fv) if inf["type"] == "int" else fv
                )

        def _drag_changed(v, pn=pdef.name, eid=effect_id, sl=slider, pd=pdef) -> None:
            if self._updating:
                return  # AUD-20011: 程序化更新期间抑制重入发射
            sl.blockSignals(True)
            if pd.ptype == "int":
                sl.setValue(int(v))
            else:
                sl.setValue(int((float(v) - pd.min_val) / (pd.step or 1.0)))
            sl.blockSignals(False)
            if clip:
                self.effect_param_changed.emit(
                    clip.clip_id, eid, pn, int(v) if pd.ptype == "int" else v
                )

        slider.valueChanged.connect(_slider_changed)
        drag.valueChanged.connect(_drag_changed)
        h.addWidget(drag)

        # Phase 3 / round 2: per-effect keyframe button for animatable
        # numeric params. Registered under a composite key
        # ("effect:<eid>:<param>") so _update_keyframe_buttons can route
        # the state query to the EffectInstance instead of the clip.
        if pdef.is_animatable() and clip is not None:
            kf_key = f"effect:{effect_id}:{pdef.name}"
            kf_btn = KeyframeIndicatorButton(pdef.name)
            kf_btn.keyframe_toggled.connect(
                lambda _pn, frame, value,
                eid=effect_id, pn=pdef.name, cid=clip.clip_id:
                self._on_effect_keyframe_button_toggled(eid, pn, cid, frame, value)
            )
            self._keyframe_buttons[kf_key] = kf_btn
            h.addWidget(kf_btn)
        return row

    if pdef.ptype == "bool":
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        h.addWidget(label)
        checkbox = QCheckBox()
        checkbox.setChecked(bool(value))
        checkbox.toggled.connect(
            lambda checked, pn=pdef.name, eid=effect_id:
                self.effect_param_changed.emit(clip.clip_id, eid, pn, checked)
                if clip and not self._updating else None  # AUD-20011
        )
        h.addWidget(checkbox)
        h.addStretch()
        return row

    if pdef.ptype == "option" and pdef.labels:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        h.addWidget(label)
        combo = ChevronComboBox()
        for choice in pdef.labels:
            combo.addItem(choice)
        if value in pdef.labels:
            combo.setCurrentText(value)
        combo.currentTextChanged.connect(
            lambda text, pn=pdef.name, eid=effect_id, callback=on_value_changed:
                (
                    callback(text) if callback is not None else None,
                    self.effect_param_changed.emit(clip.clip_id, eid, pn, text)
                    if clip and not self._updating else None,  # AUD-20011
                )
        )
        h.addWidget(combo, 1)
        return row

    # Read-only fallback for color/vec2/vec3/channel/string/...
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(label_text)
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    h.addWidget(label)
    val_label = QLabel(str(value))
    val_label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    h.addWidget(val_label, 1)
    return row


def _create_effect_param_widget(self: EditPropertiesPanel, param_name: str,
                                schema: ParamSchema, value: Any,
                                effect_id: str) -> Optional[QWidget]:
    """根据 schema 创建效果参数控件。"""
    label_text = schema.label or param_name
    if schema.unit:
        label_text += f" ({schema.unit})"

    if schema.type in ("float", "int"):
        is_int = schema.type == "int"
        pdef = ParamDef(
            name=param_name,
            ptype=schema.type,
            min_val=float(schema.min_value) if schema.min_value is not None else 0.0,
            max_val=float(schema.max_value) if schema.max_value is not None else 1.0,
            step=0.01 if not is_int else 1.0,
            decimals=0 if is_int else 2,
            default=value,
        )
        # 结构与 _build_numeric_row 一致：[label] [slider stretch=1] [DragValue 固定]。
        # slider 永远显示并吃掉多余宽度，DragValue 固定 64px 钉在右侧 —— 这样数值
        # 输入框与 transform/crop 等其它 section 的数值行视觉一致，且面板拖宽时
        # slider 撑开而非 DragValue 被拉伸（DragValue 拉伸会产生左对齐的观感）。
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        h.addWidget(label)
        clip = self._current_clips[0] if self._current_clips else None
        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.enable_velocity_drag = True
        if is_int:
            slider.setRange(int(pdef.min_val), int(pdef.max_val))
            slider.setValue(int(value))
        else:
            steps = max(int((pdef.max_val - pdef.min_val) / (pdef.step or 1.0)), 1)
            slider.setRange(0, steps)
            slider.setValue(int((float(value) - pdef.min_val) / (pdef.step or 1.0)))
        slider.setVisible(True)
        h.addWidget(slider, 1)
        drag = DragValue("", pdef.min_val, pdef.max_val, float(value), decimals=pdef.decimals)
        drag.setSingleStep(pdef.step)
        drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

        def _eff_slider_changed(v, pn=param_name, eid=effect_id, d=drag, inf={
            "type": "int" if is_int else "float",
            "min_val": pdef.min_val, "step": pdef.step,
        }):
            if self._updating:
                return  # AUD-20011: 程序化更新期间抑制重入发射
            d.blockSignals(True)
            fv = v if inf["type"] == "int" else v * inf["step"] + inf["min_val"]
            d.setValue(fv)
            d.blockSignals(False)
            self.effect_param_changed.emit(
                clip.clip_id, eid, pn, int(fv) if inf["type"] == "int" else fv
            ) if clip else None

        def _eff_drag_changed(v, pn=param_name, eid=effect_id, sl=slider, inf={
            "type": "int" if is_int else "float",
            "min_val": pdef.min_val, "step": pdef.step,
        }):
            if self._updating:
                return  # AUD-20011: 程序化更新期间抑制重入发射
            sl.blockSignals(True)
            if inf["type"] == "int":
                sl.setValue(int(v))
            else:
                steps = max(int((pdef.max_val - pdef.min_val) / (inf["step"] or 1.0)), 1)
                sl.setValue(int((float(v) - inf["min_val"]) / (inf["step"] or 1.0)))
                _ = steps
            sl.blockSignals(False)
            self.effect_param_changed.emit(
                clip.clip_id, eid, pn, int(v) if inf["type"] == "int" else v
            ) if clip else None

        slider.valueChanged.connect(_eff_slider_changed)
        drag.valueChanged.connect(_eff_drag_changed)
        h.addWidget(drag)
        return row

    elif schema.type == "bool":
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        h.addWidget(label)
        checkbox = QCheckBox()
        checkbox.setChecked(bool(value))
        clip = self._current_clips[0] if self._current_clips else None
        checkbox.toggled.connect(
            lambda checked, pn=param_name, eid=effect_id:
                self.effect_param_changed.emit(clip.clip_id, eid, pn, checked)
                if clip and not self._updating else None  # AUD-20011
        )
        h.addWidget(checkbox)
        h.addStretch()
        return row

    elif schema.type == "choice":
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(4)
        label = QLabel(label_text)
        label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        label.setMinimumWidth(60)
        h.addWidget(label)
        combo = ChevronComboBox()
        for choice in schema.choices:
            combo.addItem(choice)
        if value in schema.choices:
            combo.setCurrentText(value)
        clip = self._current_clips[0] if self._current_clips else None
        combo.currentTextChanged.connect(
            lambda text, pn=param_name, eid=effect_id:
                self.effect_param_changed.emit(clip.clip_id, eid, pn, text)
                if clip and not self._updating else None  # AUD-20011
        )
        h.addWidget(combo, 1)
        return row

    # 默认：只读显示
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(label_text)
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    h.addWidget(label)
    val_label = QLabel(str(value))
    val_label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    h.addWidget(val_label, 1)
    return row


# ── Transition / Track / Asset / Timeline section 构建器 ──

def _build_transition_section(self: EditPropertiesPanel, transition: TransitionInstance) -> None:
    """Transition section。"""
    section_id = f"transition:{transition.transition_id}"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.transition", "Transition"),
        parameters={},
        param_defs={},
        node_color="#6a4a4a",
    )
    cl = section.content_layout()

    # Audio transitions (audio:*) only show Type + Duration + Alignment.
    # The curve/direction/softness rows are video-specific (spatial mask params)
    # and have no meaning for an equal-power crossfade.
    is_audio = transition.transition_type in AUDIO_TRANSITION_TYPE_OPTIONS

    # Type 下拉
    if is_audio:
        type_options = AUDIO_TRANSITION_TYPE_OPTIONS
        type_labels = AUDIO_TRANSITION_TYPE_LABELS
    else:
        type_options = TRANSITION_TYPE_OPTIONS
        type_labels = TRANSITION_TYPE_LABELS
    type_index = (type_options.index(transition.transition_type)
                  if transition.transition_type in type_options else 0)
    type_row = self._build_transition_combo_row(
        section, "transition_type", type_options,
        type_labels, type_index, transition.transition_id
    )
    cl.addWidget(type_row)

    # Duration（int slider）
    dur_pdef = ParamDef(name="duration", ptype="int", min_val=1, max_val=300, step=1,
                        default=_TRANSITION_RESET_DEFAULTS["duration"])
    cl.addWidget(self._build_transition_int_row(
        section, "duration", transition.duration, dur_pdef, transition.transition_id
    ))

    # Alignment 下拉
    align_index = (TRANSITION_ALIGN_OPTIONS.index(transition.alignment)
                   if transition.alignment in TRANSITION_ALIGN_OPTIONS else 0)
    cl.addWidget(self._build_transition_combo_row(
        section, "alignment", TRANSITION_ALIGN_OPTIONS,
        TRANSITION_ALIGN_LABELS, align_index, transition.transition_id
    ))

    if is_audio:
        # Audio crossfades have no spatial params — stop here.
        return

    # Curve 下拉
    curve = transition.params.get("curve", "linear")
    curve_index = (TRANSITION_CURVE_OPTIONS.index(curve) if curve in TRANSITION_CURVE_OPTIONS else 0)
    cl.addWidget(self._build_transition_combo_row(
        section, "curve", TRANSITION_CURVE_OPTIONS,
        TRANSITION_CURVE_LABELS, curve_index, transition.transition_id
    ))

    direction = str(transition.params.get("direction", "left"))
    direction_index = (
        TRANSITION_DIRECTION_OPTIONS.index(direction)
        if direction in TRANSITION_DIRECTION_OPTIONS else 0
    )
    cl.addWidget(self._build_transition_combo_row(
        section, "direction", TRANSITION_DIRECTION_OPTIONS,
        TRANSITION_DIRECTION_LABELS, direction_index, transition.transition_id
    ))

    softness = float(transition.params.get("softness", 0.02))
    softness_def = ParamDef(name="softness", ptype="float", min_val=0.0,
                            max_val=0.5, step=0.01,
                            default=_TRANSITION_RESET_DEFAULTS["softness"])
    cl.addWidget(self._build_transition_float_row(
        section, "softness", softness, softness_def, transition.transition_id
    ))


def _build_transition_combo_row(self: EditPropertiesPanel, section: EditSectionWidget,
                                key: str, options: List[str], labels: List[str],
                                current_index: int, transition_id: str) -> QWidget:
    """转场下拉行（直接发 transition_param_changed）。"""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(key.replace("_", " ").title())
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(section._label_min_width)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    combo = ChevronComboBox()
    combo.addItems(labels)
    combo.setCurrentIndex(current_index)
    apply_combo_dropdown_style(combo)
    combo.currentIndexChanged.connect(
        lambda idx, k=key, opts=list(options):
            self.transition_param_changed.emit(transition_id, k, opts[idx]) if 0 <= idx < len(opts) else None
    )
    # AUD-20008: 注册行控件，让 Transition section 的 Reset 能归位显示。
    section._param_widgets[key] = {
        "type": "combo", "widget": combo, "options": list(options),
    }
    h.addWidget(combo, 1)
    return row


def _build_transition_int_row(self: EditPropertiesPanel, section: EditSectionWidget,
                              key: str, value: int, pdef: ParamDef,
                              transition_id: str) -> QWidget:
    """转场整数值行。"""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(key.replace("_", " ").title())
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(section._label_min_width)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    slider = ToposClickableSlider(Qt.Orientation.Horizontal)
    slider.enable_velocity_drag = True
    slider.setRange(int(pdef.min_val), int(pdef.max_val))
    slider.setValue(int(value))
    drag = DragValue("", pdef.min_val, pdef.max_val, float(value), decimals=0)
    drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

    def slider_changed(v) -> None:
        drag.blockSignals(True)
        drag.setValue(float(v))
        drag.blockSignals(False)
        self.transition_param_changed.emit(transition_id, key, int(v))

    def drag_changed(v) -> None:
        slider.blockSignals(True)
        slider.setValue(int(v))
        slider.blockSignals(False)
        self.transition_param_changed.emit(transition_id, key, int(v))

    slider.valueChanged.connect(slider_changed)
    drag.valueChanged.connect(drag_changed)
    h.addWidget(slider, 1)
    h.addWidget(drag)
    # AUD-20008: 注册行控件，让 Transition section 的 Reset 能归位显示。
    section._param_widgets[key] = {
        "type": "int", "widget": drag, "slider": slider, "slider_scale": 1,
    }
    return row


def _build_transition_float_row(self: EditPropertiesPanel, section: EditSectionWidget,
                                key: str, value: float, pdef: ParamDef,
                                transition_id: str) -> QWidget:
    """Floating-point transition parameter row."""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(key.replace("_", " ").title())
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(section._label_min_width)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    slider = ToposClickableSlider(Qt.Orientation.Horizontal)
    slider.enable_velocity_drag = True
    scale = 100
    slider.setRange(int(pdef.min_val * scale), int(pdef.max_val * scale))
    slider.setValue(int(round(value * scale)))
    drag = DragValue("", pdef.min_val, pdef.max_val, float(value), decimals=2)
    drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

    def slider_changed(v: int) -> None:
        float_value = v / scale
        drag.blockSignals(True)
        drag.setValue(float_value)
        drag.blockSignals(False)
        self.transition_param_changed.emit(transition_id, key, float_value)

    def drag_changed(v: float) -> None:
        slider.blockSignals(True)
        slider.setValue(int(round(float(v) * scale)))
        slider.blockSignals(False)
        self.transition_param_changed.emit(transition_id, key, float(v))

    slider.valueChanged.connect(slider_changed)
    drag.valueChanged.connect(drag_changed)
    h.addWidget(slider, 1)
    h.addWidget(drag)
    # AUD-20008: 注册行控件，让 Transition section 的 Reset 能归位显示
    # （float 行的滑块坐标 = 数值 × 100）。
    section._param_widgets[key] = {
        "type": "float", "widget": drag, "slider": slider, "slider_scale": scale,
    }
    return row


def _build_track_section(self: EditPropertiesPanel, track: EditTrack) -> None:
    """Track section。"""
    section_id = f"track:{track.track_id}"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.track", "Track"),
        parameters={},
        param_defs={},
        node_color="#4a5a6a",
    )
    cl = section.content_layout()

    # 名称（可编辑）
    name_row = QWidget()
    nh = QHBoxLayout(name_row)
    nh.setContentsMargins(0, 2, 0, 2)
    nh.setSpacing(4)
    name_label = QLabel(I18N.tr("edit.properties.track_name", "Name"))
    name_label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    name_label.setMinimumWidth(60)
    name_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    nh.addWidget(name_label)
    name_edit = QLineEdit(track.name)
    # AUD-20006: textChanged 逐字符提交 RenameTrackCommand（控制器一信号
    # 一命令，undo 栈被 "T"→"Tr"→"Tra" 中间态污染，且每键走完整命令链）。
    # 改为 debounce + editingFinished 冲刷 + last-committed 去重：整个重命名
    # 交互只产生一条命令。程序化回填经 _updating 状态锁防递归。
    old_name_timer = getattr(self, "_track_name_timer", None)
    if old_name_timer is not None:
        try:
            old_name_timer.stop()
            old_name_timer.timeout.disconnect()
        except RuntimeError:
            pass  # 旧 section 已析构，其定时器随之销毁
    name_timer = QTimer(name_edit)  # 随 section 销毁自动取消待触发的提交
    name_timer.setSingleShot(True)
    name_timer.setInterval(400)
    self._track_name_timer = name_timer
    self._track_name_last_committed = track.name
    self._track_name_track_id = track.track_id

    def _commit_track_name() -> None:
        if self._updating:
            return
        text = name_edit.text().strip()
        if not text or text == self._track_name_last_committed:
            return
        self._track_name_last_committed = text
        self.track_param_changed.emit(self._track_name_track_id, "name", text)

    name_timer.timeout.connect(_commit_track_name)

    def _on_name_text_changed(_text: str) -> None:
        if self._updating:
            return
        name_timer.start()

    name_edit.textChanged.connect(_on_name_text_changed)

    def _on_name_editing_finished() -> None:
        # 回车/失焦时立即提交最终名（不等 debounce 窗口结束）。
        if name_timer.isActive():
            name_timer.stop()
        _commit_track_name()

    name_edit.editingFinished.connect(_on_name_editing_finished)
    nh.addWidget(name_edit, 1)
    cl.addWidget(name_row)

    # 类型（只读）
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.track_type", "Type"), track.track_type.capitalize(), bright=False))

    # Caption tracks use preview visibility, never audio mute/solo semantics.
    if track.track_type == "subtitle":
        self._build_track_checkbox_row(
            cl, track, "is_visible",
            I18N.tr("edit.properties.visible", "Visible"), track.is_visible,
        )
    else:
        self._build_track_checkbox_row(cl, track, "is_muted",
                                       I18N.tr("edit.properties.mute", "Mute"), track.is_muted)
        self._build_track_checkbox_row(cl, track, "is_solo",
                                       I18N.tr("edit.properties.solo", "Solo"), track.is_solo)
    self._build_track_checkbox_row(cl, track, "is_locked",
                                   I18N.tr("edit.properties.lock", "Lock"), track.is_locked)
    self._build_track_checkbox_row(
        cl, track, "is_sync_locked",
        I18N.tr("edit.properties.sync_lock", "Sync Lock"),
        getattr(track, "is_sync_locked", True),
    )

    # 音量（仅音频轨）
    if track.track_type == "audio":
        vol_pdef = ParamDef(name="volume", ptype="float", min_val=0.0, max_val=200.0, step=0.1, decimals=1, default=100.0)
        cl.addWidget(self._build_track_volume_row(track, vol_pdef))


def _build_track_checkbox_row(self: EditPropertiesPanel, parent_layout: QVBoxLayout,
                              track: EditTrack, param_name: str,
                              label_text: str, value: bool) -> None:
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(label_text)
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    checkbox = QCheckBox()
    checkbox.setChecked(bool(value))
    checkbox.toggled.connect(
        lambda checked, pn=param_name, tid=track.track_id:
            self.track_param_changed.emit(tid, pn, checked)
    )
    h.addWidget(checkbox)
    h.addStretch()
    parent_layout.addWidget(row)


def _build_track_volume_row(self: EditPropertiesPanel, track: EditTrack, pdef: ParamDef) -> QWidget:
    """Volume row for an audio track (0–200%, track-level).

    采用与 TrackHeaderItem 一致的三段式节流：拖动期间只更新本地 slider/drag
    视觉并发出 track_param_preview（纯视觉同步轨道头，不进命令栈），松手时
    才发出一次 track_param_changed 提交 SetTrackVolumeCommand。这样滑块跟手、
    撤销栈干净（一次拖动 = 一次撤销项）。
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(I18N.tr("edit.properties.volume", "Volume"))
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    slider = ToposClickableSlider(Qt.Orientation.Horizontal)
    slider.enable_velocity_drag = True
    slider.setRange(int(pdef.min_val), int(pdef.max_val))
    slider.setValue(int(track.volume * 100.0))
    drag = DragValue("", pdef.min_val, pdef.max_val, track.volume * 100.0, decimals=1)
    drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

    # 节流状态：拖动期间为 True，期间只走 preview，松手时才提交命令
    dragging = {"active": False}

    def sync_visuals(v: float) -> None:
        """拖动期间同步 slider/drag 显示 + 发 preview（纯视觉，不进命令栈）。"""
        drag.blockSignals(True)
        drag.setValue(float(v))
        drag.blockSignals(False)
        slider.blockSignals(True)
        slider.setValue(int(v))
        slider.blockSignals(False)
        self.track_param_preview.emit(track.track_id, "volume", v / 100.0)

    def on_slider_changed(v) -> None:
        if dragging["active"]:
            # 拖动中：只更新 drag 显示 + 发 preview
            drag.blockSignals(True)
            drag.setValue(float(v))
            drag.blockSignals(False)
            self.track_param_preview.emit(track.track_id, "volume", v / 100.0)
        else:
            # 非拖动场景（键盘方向键、外部 setValue）：直接提交
            sync_visuals(v)
            self.track_param_changed.emit(track.track_id, "volume", v / 100.0)

    def on_drag_changed(v) -> None:
        if dragging["active"]:
            slider.blockSignals(True)
            slider.setValue(int(v))
            slider.blockSignals(False)
            self.track_param_preview.emit(track.track_id, "volume", v / 100.0)
        else:
            sync_visuals(v)
            self.track_param_changed.emit(track.track_id, "volume", v / 100.0)

    def start_drag() -> None:
        dragging["active"] = True

    def finish_drag() -> None:
        if not dragging["active"]:
            return
        dragging["active"] = False
        # 松手时提交一次最终值
        final_v = float(slider.value())
        self.track_param_changed.emit(track.track_id, "volume", final_v / 100.0)

    slider.valueChanged.connect(on_slider_changed)
    drag.valueChanged.connect(on_drag_changed)
    slider.sliderPressed.connect(start_drag)
    slider.sliderReleased.connect(finish_drag)
    drag.interaction_started.connect(start_drag)
    drag.interaction_ended.connect(finish_drag)
    h.addWidget(slider, 1)
    h.addWidget(drag)

    # 注册引用，供 reset / 外部刷新使用
    self._audio_widget_refs.setdefault(track.track_id, {})["volume"] = (slider, drag)
    return row


def _build_track_pan_row(self: EditPropertiesPanel, track: EditTrack, pdef: ParamDef) -> QWidget:
    """Pan row for an audio track (-100 left .. 0 center .. 100 right).

    同样采用三段式节流（参见 _build_track_volume_row 的说明）。
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(4)
    label = QLabel(I18N.tr("edit.properties.pan", "Pan"))
    label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
    label.setMinimumWidth(60)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    h.addWidget(label)
    slider = ToposClickableSlider(Qt.Orientation.Horizontal)
    slider.enable_velocity_drag = True
    slider.setRange(int(pdef.min_val), int(pdef.max_val))
    slider.setValue(int(track.pan * 100.0))
    drag = DragValue("", pdef.min_val, pdef.max_val, track.pan * 100.0, decimals=0)
    drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)

    dragging = {"active": False}

    def sync_visuals(v: float) -> None:
        drag.blockSignals(True)
        drag.setValue(float(v))
        drag.blockSignals(False)
        slider.blockSignals(True)
        slider.setValue(int(v))
        slider.blockSignals(False)
        self.track_param_preview.emit(track.track_id, "pan", v / 100.0)

    def on_slider_changed(v) -> None:
        if dragging["active"]:
            drag.blockSignals(True)
            drag.setValue(float(v))
            drag.blockSignals(False)
            self.track_param_preview.emit(track.track_id, "pan", v / 100.0)
        else:
            sync_visuals(v)
            self.track_param_changed.emit(track.track_id, "pan", v / 100.0)

    def on_drag_changed(v) -> None:
        if dragging["active"]:
            slider.blockSignals(True)
            slider.setValue(int(v))
            slider.blockSignals(False)
            self.track_param_preview.emit(track.track_id, "pan", v / 100.0)
        else:
            sync_visuals(v)
            self.track_param_changed.emit(track.track_id, "pan", v / 100.0)

    def start_drag() -> None:
        dragging["active"] = True

    def finish_drag() -> None:
        if not dragging["active"]:
            return
        dragging["active"] = False
        final_v = float(slider.value())
        self.track_param_changed.emit(track.track_id, "pan", final_v / 100.0)

    slider.valueChanged.connect(on_slider_changed)
    drag.valueChanged.connect(on_drag_changed)
    slider.sliderPressed.connect(start_drag)
    slider.sliderReleased.connect(finish_drag)
    drag.interaction_started.connect(start_drag)
    drag.interaction_ended.connect(finish_drag)
    h.addWidget(slider, 1)
    h.addWidget(drag)

    self._audio_widget_refs.setdefault(track.track_id, {})["pan"] = (slider, drag)
    return row


def _build_audio_clip_section(self: EditPropertiesPanel, clip: ClipInstance, track: EditTrack,
                               collapsed: bool = True) -> None:
    """Audio section shown when an audio clip is selected.

    Audio volume/pan are track-level data, so this section edits the clip's
    containing track. Changes emit track_param_changed, which the controller
    routes to SetTrackVolumeCommand / SetTrackPanCommand — the same commands
    the track-header volume slider uses, so the two stay in sync.

    ``collapsed`` defaults to True so the section follows the panel's
    "collapse all optional sections by default" convention.
    """
    section_id = f"track:{track.track_id}:audio"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.audio", "Audio"),
        parameters={},
        param_defs={},
        node_color="#4a6a5a",
        collapsed=collapsed,
    )
    cl = section.content_layout()

    # 所属轨道（只读提示）
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.track", "Track"), track.name or track.track_id[:8],
        bright=False))

    # 音量（0-200%，轨道级）
    vol_pdef = ParamDef(name="volume", ptype="float", min_val=0.0, max_val=200.0,
                        step=0.1, decimals=1, default=100.0)
    cl.addWidget(self._build_track_volume_row(track, vol_pdef))

    # 声像（-100 左 .. 0 中 .. 100 右，轨道级）
    pan_pdef = ParamDef(name="pan", ptype="float", min_val=-100.0, max_val=100.0,
                        step=1.0, decimals=0, default=0.0)
    cl.addWidget(self._build_track_pan_row(track, pan_pdef))

    # M14.3 采样级 UI：clip 级子帧偏移（@48k 参考率样本）——时间线侧
    # 延迟入点/源侧子帧修剪。经 clip_param_changed →
    # UpdateClipParamCommand（可 Undo；语义见 core/sample_time.py，
    # 不做帧进位归一化）。
    for _attr, _label_key, _default_label, _tip in (
        ("audio_sample_offset", "edit.properties.audio_offset",
         "Sample Offset",
         "时间线侧子帧偏移：clip 帧起点后延迟的样本数（@48k 参考率，≥0）"),
        ("source_sample_offset", "edit.properties.source_offset",
         "Sample Trim",
         "源侧子帧修剪：入点后跳过的源样本数（@48k 参考率，≥0）"),
    ):
        _row = self._make_properties_row()
        _h = QHBoxLayout(_row)
        _h.setContentsMargins(0, 2, 0, 2)
        _h.setSpacing(4)
        _h.addWidget(self._make_param_label(
            I18N.tr(_label_key, _default_label), section))
        _drag = DragValue(
            "", 0.0, 48000.0 * 60.0,
            float(getattr(clip, _attr, 0) or 0), decimals=0)
        _drag.setSingleStep(1.0)
        _drag.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        _drag.setToolTip(_tip)
        _h.addWidget(_drag)
        _h.addStretch()
        _cid, _ptype = clip.clip_id, _attr
        _drag.valueChanged.connect(
            lambda v, _cid=_cid, _ptype=_ptype: self.clip_param_changed.emit(
                _cid, _ptype, "value", max(0, int(round(float(v))))))
        cl.addWidget(_row)


def _build_asset_section(self: EditPropertiesPanel, asset: MediaAsset) -> None:
    """Asset section（只读信息 + relink）。"""
    section_id = "asset:info"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.asset", "Asset"),
        parameters={},
        param_defs={},
        node_color="#5a5a4a",
    )
    cl = section.content_layout()
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.filename", "Filename"), asset.display_name, bright=True))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.path", "Path"), asset.path))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.resolution", "Resolution"), asset.resolution_str))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.fps", "FPS"),
        str(asset.fps) if asset.fps else "N/A"))
    if asset.is_video and asset.duration_seconds:
        ds = asset.duration_seconds
        cl.addWidget(self._build_info_row(
            I18N.tr("edit.properties.duration", "Duration"),
            f"{int(ds // 60)}:{ds % 60:05.2f}"))
    codec = format_codec_label(
        asset.metadata.get("codec"), asset.metadata.get("codec_profile")) or "N/A"
    cl.addWidget(self._build_info_row(I18N.tr("edit.properties.codec", "Codec"), codec))
    proxy = getattr(asset, "proxy_status", "unknown")
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.proxy", "Proxy"), proxy.capitalize()))

    # Relink 按钮
    relink_row = QWidget()
    rh = QHBoxLayout(relink_row)
    rh.setContentsMargins(0, 4, 0, 0)
    rh.setSpacing(4)
    relink_btn = QToolButton()
    relink_btn.setIcon(get_icon('fa5s.link', IconColors.WHITE))
    relink_btn.setIconSize(QSize(14, 14))
    relink_btn.setFixedSize(24, 24)
    relink_btn.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
    relink_btn.setToolTip(I18N.tr("edit.properties.relink", "Relink"))
    relink_btn.clicked.connect(self._on_asset_relink)
    rh.addWidget(relink_btn)
    rh.addStretch()
    cl.addWidget(relink_row)


def _on_asset_relink(self: EditPropertiesPanel) -> None:
    if self._current_asset:
        logger.info(f"Asset relink requested: {self._current_asset.asset_id}")


def _build_timeline_section(self: EditPropertiesPanel, timeline: TimelineSequence) -> None:
    """Timeline section（只读信息）。"""
    section_id = "timeline:info"
    section = self._add_section(
        section_id=section_id,
        title=I18N.tr("edit.properties.timeline", "Timeline"),
        parameters={},
        param_defs={},
        node_color="#4a5a6a",
    )
    cl = section.content_layout()
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.timeline_name", "Name"), timeline.name, bright=True))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.resolution", "Resolution"),
        f"{timeline.width}x{timeline.height}"))
    fps_val = (timeline.fps.num / timeline.fps.den
               if hasattr(timeline.fps, "num") else float(timeline.fps))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.fps", "FPS"), f"{fps_val:.2f}"))
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.duration", "Duration"),
        _format_duration(timeline.duration_frames, fps_val)))
    video_count = len(timeline.video_tracks)
    audio_count = len(timeline.audio_tracks)
    cl.addWidget(self._build_info_row(
        I18N.tr("edit.properties.track_count", "Tracks"),
        f"{video_count} video, {audio_count} audio"))


# ============================================================
# 把模块级构建函数绑定为 EditPropertiesPanel 的方法
# ============================================================
#
# 这些分组构建函数都以 ``self`` 作为第一个参数（与类内方法签名一致）。
# 直接 setattr 到类上即可：Python 在属性查找时会把它们当作普通函数，
# 通过实例访问时自动包装成 bound method（self 由 Python 注入）。
# 这样既保持文件可读性（分组逻辑独立成函数），又能让它们访问实例状态。

for _name, _func in {
    "_build_transform_section": _build_transform_section,
    "_on_scale_lock_toggled": _on_scale_lock_toggled,
    "_build_crop_section": _build_crop_section,
    "_build_opacity_section": _build_opacity_section,
    "_build_raw_section": _build_raw_section,
    "_build_solid_color_section": _build_solid_color_section,
    "_rebuild_solid_color_section": _rebuild_solid_color_section,
    "_reset_solid_color_section": _reset_solid_color_section,
    "_reset_transition_section": _reset_transition_section,
    "_build_speed_section": _build_speed_section,
    "_build_effect_sections": _build_effect_sections,
    "_on_effect_enable_toggled": _on_effect_enable_toggled,
    "_on_effect_delete_requested": _on_effect_delete_requested,
    "_on_effect_open_dialog_requested": _on_effect_open_dialog_requested,
    "_create_effect_param_widget": _create_effect_param_widget,
    "_create_effect_param_widget_from_pdef": _create_effect_param_widget_from_pdef,
    "_create_effect_channel_group": _create_effect_channel_group,
    "_create_effect_xy_row": _create_effect_xy_row,
    "scroll_to_effect": _scroll_to_effect,
    "_build_transition_section": _build_transition_section,
    "_build_transition_combo_row": _build_transition_combo_row,
    "_build_transition_int_row": _build_transition_int_row,
    "_build_transition_float_row": _build_transition_float_row,
    "_build_track_section": _build_track_section,
    "_build_track_checkbox_row": _build_track_checkbox_row,
    "_build_track_volume_row": _build_track_volume_row,
    "_build_track_pan_row": _build_track_pan_row,
    "_build_audio_clip_section": _build_audio_clip_section,
    "_build_asset_section": _build_asset_section,
    "_on_asset_relink": _on_asset_relink,
    "_build_timeline_section": _build_timeline_section,
    # P10.3 字幕片段分组构建
    "_build_subtitle_info_section": _build_subtitle_info_section,
    "_build_subtitle_text_section": _build_subtitle_text_section,
    "_build_subtitle_font_section": _build_subtitle_font_section,
    "_build_subtitle_layout_section": _build_subtitle_layout_section,
    "_build_subtitle_transform_section": _build_subtitle_transform_section,
    "_build_subtitle_fill_section": _build_subtitle_fill_section,
    "_build_subtitle_outline_section": _build_subtitle_outline_section,
    "_build_subtitle_shadow_section": _build_subtitle_shadow_section,
    "_build_subtitle_color_button": _build_subtitle_color_button,
}.items():
    setattr(EditPropertiesPanel, _name, _func)
