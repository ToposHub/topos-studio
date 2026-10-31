"""Tracker 主面板（DaVinci 风格窗口布局）。

布局：标题栏 / 跟踪工具栏 / 双层控制栏（最窄时通道网格自动换行）/
可折叠分析参数 / 曲线图 / 底部交互栏。
配色：背景 ``#1A1A1A``，分隔线 ``#343434``，文本 ``#D8D8D8``，
次要文本 ``#8C8C8C``，选中/警告 ``#FF8A00``。

四态状态机（``PanelState``）决定控件启禁用：
- EMPTY：曲线区空态提示；
- RUNNING：进度显示，算法参数全部禁用，仅保留停止/取消；
- SUCCESS：绿色灯 + 统计（有效帧范围/平均置信度），重新跟踪/清除可用；
- PARTIAL_FAILED / FAILED：黄色/红色灯 + 失败原因。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.features.color.ui.tracker_graph import TrackerGraph
from src.features.color.ui.tracker_status import PanelState, TrackerStatusLight
from src.features.color.ui.tracker_toolbar import TrackerToolBar
from src.shared.i18n.i18n import tr
from src.shared.ui.widgets import ChevronComboBox

_CHECKMARK_ICON_FILE = (
    Path(__file__).resolve().parents[3] / "shared" / "assets" / "icons" / "checkmark.svg"
)
# 与 style_sheet.DARK_THEME 相同：QSS url() 必须用正斜杠路径，
# 原生的 Windows 反斜杠绝对路径会让整张面板样式表解析失败。
# （带引号的 url("file:...") URI 形式会被 QSS 解析成相对路径，不能用。）
_CHECKMARK_ICON_PATH = _CHECKMARK_ICON_FILE.resolve().as_posix()

PANEL_STYLE = """
TrackerPanel { background-color: #17181B; }
TrackerPanel QWidget#tracker_title_bar,
TrackerPanel QWidget#tracker_transport,
TrackerPanel QWidget#tracker_context_toolbar,
TrackerPanel QWidget#tracker_params,
TrackerPanel QWidget#tracker_bottom_bar { background-color: #202125; }
TrackerPanel QWidget#tracker_channels { background-color: #151619; }
TrackerPanel QLabel { color: #D8D8D8; font-size: 11px; background: transparent; }
TrackerPanel QLabel#tracker_secondary { color: #8C8C8C; font-size: 10px; }
TrackerPanel QLabel#tracker_title { font-weight: bold; font-size: 12px; }
TrackerPanel QFrame#tracker_separator,
TrackerPanel QFrame#tracker_vertical_separator { color: #34353A; background-color: #34353A; }
TrackerPanel QPushButton {
    background-color: #25262A; border: 1px solid #36373D; border-radius: 3px;
    color: #D8D8D8; font-size: 10px; padding: 2px 6px;
}
TrackerPanel QPushButton:hover { border-color: #64656C; background-color: #303136; }
TrackerPanel QPushButton:checked { background-color: #3A3025; border-color: #FF8A00; color: #FFB15C; }
TrackerPanel QPushButton:disabled { color: #55565C; border-color: #292A2E; }
TrackerPanel QPushButton#tracker_primary { color: #00D9A6; }
TrackerPanel QPushButton#tracker_danger:hover { border-color: #FF4D5E; }
TrackerPanel QPushButton#tracker_params { color: #BEBEC4; padding: 1px 7px; }
TrackerPanel QPushButton#tracker_transport_button,
TrackerPanel QPushButton#tracker_primary,
TrackerPanel QPushButton#tracker_danger,
TrackerPanel QPushButton#tracker_icon_action,
TrackerPanel QPushButton#tracker_params,
TrackerPanel QPushButton#tracker_title_menu {
    background-color: transparent; border: 1px solid transparent;
    padding: 0; border-radius: 3px;
}
TrackerPanel QPushButton#tracker_transport_button:hover,
TrackerPanel QPushButton#tracker_primary:hover,
TrackerPanel QPushButton#tracker_danger:hover,
TrackerPanel QPushButton#tracker_icon_action:hover,
TrackerPanel QPushButton#tracker_params:hover,
TrackerPanel QPushButton#tracker_title_menu:hover {
    background-color: rgba(255, 255, 255, 18); border-color: #505158;
}
TrackerPanel QPushButton#tracker_danger:hover { border-color: #FF4D5E; }
TrackerPanel QPushButton#tracker_transport_button:disabled,
TrackerPanel QPushButton#tracker_icon_action:disabled {
    background-color: transparent; border-color: transparent;
}
TrackerPanel QPushButton#tracker_primary:hover { color: #51F0C5; }
TrackerPanel QPushButton#tracker_segment {
    background-color: #1A1B1F; border: 1px solid #303136;
    border-radius: 3px; padding: 0; color: #9A9BA1;
}
TrackerPanel QPushButton#tracker_segment:hover { background-color: #27282D; }
TrackerPanel QPushButton#tracker_segment:checked {
    background-color: #0F1013; border-color: #505158; color: #F0F0F0;
}
TrackerPanel QComboBox, TrackerPanel QSpinBox, TrackerPanel QDoubleSpinBox {
    background-color: #232325; border: 1px solid #151517; border-radius: 6px;
    color: #E0E0E0; font-size: 11px; padding: 2px 8px;
}
TrackerPanel QComboBox:hover, TrackerPanel QSpinBox:hover,
TrackerPanel QDoubleSpinBox:hover { background-color: #2E2E34; }
TrackerPanel QComboBox:focus, TrackerPanel QSpinBox:focus,
TrackerPanel QDoubleSpinBox:focus { border-color: #FF8800; background-color: #2E2E34; }
TrackerPanel QCheckBox {
    color: #CCCCCC; font-size: 10px; spacing: 5px; background: transparent;
}
TrackerPanel QCheckBox::indicator {
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid #3A3A3A; background: #252525;
}
TrackerPanel QCheckBox::indicator:hover {
    border-color: #4A4A4A; background: #2A2A2A;
}
TrackerPanel QCheckBox::indicator:checked {
    background: #CC6600; border: 1px solid #CC6600;
    image: url("__CHECKMARK_ICON_URL__");
}
TrackerPanel QCheckBox::indicator:checked:hover {
    background: #FFAA44; border-color: #FFAA44;
}
TrackerPanel QCheckBox:checked { color: #CC6600; }
TrackerPanel QCheckBox#tracker_channel {
    font-size: 10px; font-weight: 600;
    padding: 0;
}
TrackerPanel QCheckBox#tracker_channel[channel="tx"] { color: #00E0B0; }
TrackerPanel QCheckBox#tracker_channel[channel="ty"] { color: #4DA3FF; }
TrackerPanel QCheckBox#tracker_channel[channel="scale"] { color: #FF38A8; }
TrackerPanel QCheckBox#tracker_channel[channel="rotation"] { color: #FFD43B; }
TrackerPanel QCheckBox#tracker_channel:disabled { color: #55565C; }
TrackerPanel QCheckBox#tracker_channel_tx { color: #00E0B0; }
TrackerPanel QCheckBox#tracker_channel_ty { color: #4DA3FF; }
TrackerPanel QCheckBox#tracker_channel_scale { color: #FF38A8; }
TrackerPanel QCheckBox#tracker_channel_rotation { color: #FFD43B; }
TrackerPanel QSlider::groove:horizontal { height: 3px; background: #3B3C42; }
TrackerPanel QSlider::handle:horizontal { width: 10px; margin: -4px 0; background: #FF8A00; border-radius: 5px; }
""".replace("__CHECKMARK_ICON_URL__", _CHECKMARK_ICON_PATH)

# 跟踪类型下拉：值 → 显示（3D 不在范围，显示禁用项）
MODE_ITEMS = [
    ("translation", "平移"),
    ("trs", "平移旋转缩放"),
    ("affine", "仿射"),
    ("homography", "透视"),
]
QUALITY_ITEMS = [("draft", "草稿"), ("standard", "标准"), ("high", "高质量")]
FAILURE_ITEMS = [
    ("stop", "停止"),
    ("hold", "保持上一帧"),
    ("recapture", "尝试重捕获"),
]

EMPTY_HINT = (
    "第 1 步：在 Mask 面板点击圆形/矩形等按钮，在画布上绘制窗口\n"
    "第 2 步：回到这里，点击 ▶▶ 向前跟踪\n"
    "（若没有 Mask 面板：菜单 Window → 面板 → Masks）"
)

# DaVinci Tracker action cells use one consistent hit target.  Keep this
# explicit instead of allowing each glyph's size hint to change the button.
TRACK_BUTTON_WIDTH = 30
TRACK_BUTTON_HEIGHT = 30


def _trf(key: str, default: str, **kwargs: object) -> str:
    """Translate a tracker label and format fallback text when needed."""
    text = tr(key, default, **kwargs)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("tracker_separator")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line



class TrackerPanel(QWidget):
    """DaVinci 风格 Tracker 面板。"""

    # 跟踪动作
    track_forward_requested = pyqtSignal()
    track_backward_requested = pyqtSignal()
    track_forward_one_requested = pyqtSignal()
    track_backward_one_requested = pyqtSignal()
    track_auto_requested = pyqtSignal()
    # 旧双向信号保留给兼容调用方；可见按钮使用 track_auto_requested。
    track_both_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    retrack_failed_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    # 帧导航：'first'/'prev'/'next'/'last'
    nav_requested = pyqtSignal(str)
    jump_frame_requested = pyqtSignal(int)
    mode_changed = pyqtSignal(str)  # clip / frame
    # 参数与显示
    params_changed = pyqtSignal(dict)
    display_option_changed = pyqtSignal(str, bool)  # points/path/boxes
    # 底部栏
    overlay_toggled = pyqtSignal(bool)
    delete_manual_requested = pyqtSignal()
    reset_current_requested = pyqtSignal()
    corner_pin_export_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TrackerPanel")
        self.setMinimumWidth(320)
        self.setMaximumWidth(900)
        self.setStyleSheet(PANEL_STYLE)

        self._state = PanelState.EMPTY
        self._bound_shape: Optional[str] = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_title_bar())
        root.addWidget(_separator())
        self.toolbar = TrackerToolBar(self)
        self.toolbar.setObjectName("tracker_context_toolbar")
        root.addWidget(self.toolbar)
        root.addWidget(_separator())
        root.addWidget(self._build_control_area())
        root.addWidget(_separator())
        self.params_area = self._build_params_area()
        # 默认呈现达芬奇的曲线窗口；算法参数通过标题栏“参数”按钮展开。
        self.params_area.setVisible(False)
        root.addWidget(self.params_area)
        root.addWidget(_separator())

        # 曲线图区（空态提示叠加在图上）
        graph_host = QWidget(self)
        gh_layout = QVBoxLayout(graph_host)
        gh_layout.setContentsMargins(0, 0, 0, 0)
        gh_layout.setSpacing(0)
        self.empty_hint = QLabel(tr("tracker.empty_hint", EMPTY_HINT), graph_host)
        self.empty_hint.setObjectName("tracker_secondary")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        gh_layout.addWidget(self.empty_hint)
        self.graph = TrackerGraph(graph_host, show_controls=False)
        gh_layout.addWidget(self.graph, 1)
        root.addWidget(graph_host, 1)

        root.addWidget(_separator())
        root.addWidget(self._build_bottom_bar())

        self.set_state(PanelState.EMPTY)

    # ================================================================
    # 区块构建
    # ================================================================

    def _build_title_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("tracker_title_bar")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(6)
        self.status_light = TrackerStatusLight(bar)
        layout.addWidget(self.status_light)
        title = QLabel(tr("tracker.title", "跟踪器 - 窗口"), bar)
        title.setObjectName("tracker_title")
        layout.addWidget(title)
        self.shape_name_label = QLabel("", bar)
        self.shape_name_label.setObjectName("tracker_secondary")
        # 目标名称已经在上下文工具栏的 Window 下拉框中显示，标题栏保持
        # DaVinci 的简洁 “Tracker - Window” 形式；文本仍保留供状态/测试读取。
        self.shape_name_label.setVisible(False)
        layout.addWidget(self.shape_name_label)
        layout.addStretch(1)
        self.btn_params = QPushButton("⚙", bar)
        self.btn_params.setObjectName("tracker_params")
        self.btn_params.setCheckable(True)
        self.btn_params.setChecked(False)
        self.btn_params.setFixedSize(28, 24)
        self.btn_params.setAccessibleName(tr("tracker.params", "跟踪分析参数"))
        self.btn_params.setToolTip(tr("tracker.params_tooltip", "显示/隐藏跟踪分析参数"))
        self.btn_params.toggled.connect(self.params_area_visibility_changed)
        layout.addWidget(self.btn_params)
        self.btn_title_menu = QPushButton("⋯", bar)
        self.btn_title_menu.setObjectName("tracker_title_menu")
        self.btn_title_menu.setFixedWidth(28)
        self.btn_title_menu.setToolTip(tr("tracker.options", "跟踪器选项"))
        layout.addWidget(self.btn_title_menu)
        return bar

    def params_area_visibility_changed(self, visible: bool) -> None:
        """切换高级分析参数区；主曲线区始终保留伸缩空间。"""
        if hasattr(self, "params_area"):
            self.params_area.setVisible(bool(visible))

    def resizeEvent(self, event) -> None:  # noqa: N802
        """按 Dock 宽度重排通道网格，主动作栏始终保持独立。"""
        super().resizeEvent(event)
        if hasattr(self, "_control_channel_grid"):
            self._update_channel_grid()
        if hasattr(self, "channel_3d"):
            # DaVinci 的完整名称在宽面板显示；当前右侧 Dock 较窄时只显示
            # “3D”，完整含义由 tooltip 保留，避免出现截断的半个单词。
            self.channel_3d.setText(
                self._perspective_3d_full_label
                if self.width() >= 760 else self._perspective_3d_short_label
            )

    def _on_auto_button_clicked(self) -> None:
        """执行达芬奇中间的自动方向按钮；运行中复用为取消。"""
        if self._state == PanelState.RUNNING:
            self.cancel_requested.emit()
            return
        self.track_auto_requested.emit()

    def _set_auto_button_state(self, running: bool) -> None:
        """更新中间按钮的 idle/取消语义，不新增播放按钮。"""
        if running:
            self.btn_auto.setText("■")
            self.btn_auto.setToolTip(tr("tracker.cancel_tracking", "取消跟踪"))
            self.btn_auto.setAccessibleName(tr("tracker.stop_tracking", "停止跟踪"))
        else:
            self.btn_auto.setText("↔")
            self.btn_auto.setToolTip(tr(
                "tracker.track_auto_tooltip",
                "根据播放头位置自动完成前向和后向跟踪",
            ))
            self.btn_auto.setAccessibleName(tr(
                "tracker.track_auto", "自动跟踪（根据播放头方向）"))

    def _build_control_area(self) -> QWidget:
        area = QWidget(self)
        area.setObjectName("tracker_transport")
        # 动作栏与通道栏固定分层，避免 Dock 变窄时把复选框文字压扁。
        area.setFixedHeight(62)
        root_layout = QVBoxLayout(area)
        root_layout.setContentsMargins(0, 2, 0, 2)
        root_layout.setSpacing(0)
        layout = QHBoxLayout()
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(2)
        self._control_area = area
        secondary = QWidget(area)
        secondary.setObjectName("tracker_channels")
        # 固定为两层：跟踪动作一层，通道与 Clip/Frame 一层。
        # 这样 Qt 没有机会把通道文字压缩到不可见。
        secondary_layout = QHBoxLayout(secondary)
        secondary_layout.setContentsMargins(8, 0, 8, 0)
        secondary_layout.setSpacing(6)
        channel_grid_host = QWidget(secondary)
        channel_grid = QGridLayout(channel_grid_host)
        channel_grid.setContentsMargins(0, 0, 0, 0)
        channel_grid.setHorizontalSpacing(5)
        channel_grid.setVerticalSpacing(3)
        channel_grid.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        segment_bar = QWidget(secondary)
        segment_layout = QHBoxLayout(segment_bar)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(2)
        segment_layout.addStretch(1)
        secondary_layout.addWidget(
            channel_grid_host, 0, Qt.AlignmentFlag.AlignVCenter)
        secondary_layout.addStretch(1)
        secondary_layout.addWidget(
            segment_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        secondary.setVisible(True)
        self._control_secondary_widget = secondary
        self._control_channel_grid = channel_grid
        root_layout.addLayout(layout)
        root_layout.addWidget(secondary)

        # 兼容旧控制器的帧导航对象不再加入布局；Tracker 面板不显示播放/跳帧按钮。
        self.nav_buttons: Dict[str, QPushButton] = {}
        for key, text, label_key, label_default in (
            ("first", "|◀", "tracker.first_frame", "首帧"),
            ("prev", "◀", "tracker.previous_frame", "前一帧"),
            ("next", "▶", "tracker.next_frame", "后一帧"),
            ("last", "▶|", "tracker.last_frame", "最后一帧"),
        ):
            btn = QPushButton(text, area)
            btn.setObjectName("tracker_transport_button")
            btn.setToolTip(tr(label_key, label_default))
            btn.setFixedSize(23, 24)
            btn.setAccessibleName(tr(label_key, label_default))
            btn.clicked.connect(
                lambda _c=False, k=key: self.nav_requested.emit(k))
            self.nav_buttons[key] = btn
            btn.setVisible(False)
        # DaVinci Tracker 的五个核心按钮：向后、向后单帧、自动方向、
        # 向前单帧、向前。播放控制不属于这一排。
        self.btn_backward = QPushButton("◀◀", area)
        self.btn_backward.setObjectName("tracker_primary")
        self.btn_backward.setToolTip(tr(
            "tracker.track_backward_tooltip", "从当前帧向参考帧跟踪"
        ))
        self.btn_backward.setAccessibleName(tr("tracker.track_backward", "向后跟踪"))
        self.btn_backward.clicked.connect(self.track_backward_requested.emit)
        self.btn_backward_one = QPushButton("◀|", area)
        self.btn_backward_one.setObjectName("tracker_transport_button")
        self.btn_backward_one.setToolTip(tr(
            "tracker.track_backward_one_tooltip", "向后跟踪一帧"))
        self.btn_backward_one.setAccessibleName(tr(
            "tracker.track_backward_one", "向后跟踪一帧"))
        self.btn_backward_one.clicked.connect(
            self.track_backward_one_requested.emit)
        self.btn_auto = QPushButton("↔", area)
        self.btn_auto.setObjectName("tracker_primary")
        self._set_auto_button_state(False)
        self.btn_auto.clicked.connect(self._on_auto_button_clicked)
        self.btn_forward_one = QPushButton("|▶", area)
        self.btn_forward_one.setObjectName("tracker_transport_button")
        self.btn_forward_one.setToolTip(tr(
            "tracker.track_forward_one_tooltip", "向前跟踪一帧"))
        self.btn_forward_one.setAccessibleName(tr(
            "tracker.track_forward_one", "向前跟踪一帧"))
        self.btn_forward_one.clicked.connect(
            self.track_forward_one_requested.emit)
        self.btn_forward = QPushButton("▶▶", area)
        self.btn_forward.setObjectName("tracker_primary")
        self.btn_forward.setToolTip(tr(
            "tracker.track_forward_tooltip", "从参考帧向前跟踪"
        ))
        self.btn_forward.setAccessibleName(tr("tracker.track_forward", "向前跟踪"))
        self.btn_forward.clicked.connect(self.track_forward_requested.emit)

        # 隐藏兼容控件：旧测试与旧脚本仍可发射取消/双向/重跟踪信号，
        # 但不会再占用 Tracker 的可见控制栏。
        self.btn_stop = QPushButton("■", area)
        self.btn_stop.setObjectName("tracker_danger")
        self.btn_stop.setToolTip(tr("tracker.cancel_tracking", "取消跟踪"))
        self.btn_stop.setAccessibleName(tr("tracker.stop_tracking", "停止跟踪"))
        self.btn_stop.clicked.connect(self.cancel_requested.emit)
        self.btn_stop.setVisible(False)
        self.btn_both = QPushButton("↔", area)
        self.btn_both.setObjectName("tracker_transport_button")
        self.btn_both.setToolTip(tr("tracker.track_both_tooltip", "前向 + 后向跟踪"))
        self.btn_both.setAccessibleName(tr("tracker.track_both", "双向跟踪"))
        self.btn_both.clicked.connect(self.track_both_requested.emit)
        self.btn_both.setVisible(False)
        self.btn_retrack = QPushButton("↻", area)
        self.btn_retrack.setObjectName("tracker_transport_button")
        self.btn_retrack.setToolTip(tr(
            "tracker.retrack_failed_tooltip", "仅重新跟踪失败帧"
        ))
        self.btn_retrack.setAccessibleName(tr("tracker.retrack_failed", "重新跟踪失败帧"))
        self.btn_retrack.clicked.connect(self.retrack_failed_requested.emit)
        self.btn_retrack.setVisible(False)
        for btn in (self.btn_backward, self.btn_backward_one, self.btn_auto,
                    self.btn_forward_one, self.btn_forward):
            btn.setFixedSize(TRACK_BUTTON_WIDTH, TRACK_BUTTON_HEIGHT)
            layout.addWidget(btn)

        # 通道名称使用 DaVinci 的短标签（Pan / Tilt / Zoom / Rotate），
        # 完整含义仍由 tooltip 提供，避免窄面板里出现截断文字。
        self._build_channel_controls(area)

        self.progress_label = QLabel("", area)
        self.progress_label.setObjectName("tracker_secondary")
        self.progress_label.setMaximumWidth(190)
        self.progress_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_label)

        self._control_spacer = QWidget(area)
        self._control_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._control_spacer, 1)
        for key in ("clip", "frame"):
            segment_layout.addWidget(self.segment_buttons[key])
        self._control_channel_widgets = [
            *self.channel_boxes.values(), self.channel_3d,
        ]
        self._update_channel_grid()
        return area

    def _build_channel_controls(self, area: QWidget) -> None:
        """创建通道和 Clip/Frame 控件，统一交给第二层布局管理。"""
        self.channel_boxes: Dict[str, QCheckBox] = {}
        for key, text_key, text_default, tip_key, tip_default in (
            ("tx", "tracker.channel_translation_x", "平移 X", "tracker.channel_translation_x_tooltip", "平移 X"),
            ("ty", "tracker.channel_translation_y", "平移 Y", "tracker.channel_translation_y_tooltip", "平移 Y"),
            ("scale", "tracker.channel_scale", "缩放", "tracker.channel_scale_tooltip", "缩放"),
            ("rotation", "tracker.channel_rotation", "旋转", "tracker.channel_rotation_tooltip", "旋转"),
        ):
            box = QCheckBox(tr(text_key, text_default), area)
            box.setObjectName("tracker_channel")
            box.setProperty("channel", key)
            box.setChecked(True)
            box.setFixedHeight(24)
            box.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            box.setToolTip(tr(tip_key, tip_default))
            box.toggled.connect(
                lambda checked, k=key: self.graph.set_component_visible(k, checked)
            )
            self.channel_boxes[key] = box

        self._perspective_3d_full_label = tr(
            "tracker.channel_perspective_3d", "Perspective 3D")
        self._perspective_3d_short_label = "3D"
        self.channel_3d = QCheckBox(self._perspective_3d_short_label, area)
        self.channel_3d.setObjectName("tracker_channel")
        self.channel_3d.setProperty("channel", "perspective")
        self.channel_3d.setToolTip(tr("tracker.camera_3d_unavailable", "3D 摄像机跟踪尚未开放"))
        self.channel_3d.setEnabled(False)
        self.channel_3d.setFixedHeight(24)

        self.segment_group = QButtonGroup(area)
        self.segment_group.setExclusive(True)
        self.segment_buttons: Dict[str, QPushButton] = {}
        for key, text_key, text_default, tip_key, tip_default in (
            ("clip", "tracker.clip", "片段", "tracker.clip_tooltip", "整段轨迹"),
            ("frame", "tracker.frame", "帧", "tracker.frame_tooltip", "参考帧附近轨迹"),
        ):
            btn = QPushButton(tr(text_key, text_default), area)
            btn.setObjectName("tracker_segment")
            btn.setCheckable(True)
            btn.setFixedSize(46, 24)
            btn.setToolTip(tr(tip_key, tip_default))
            btn.clicked.connect(
                lambda _checked, k=key: self._set_graph_mode(k)
            )
            self.segment_group.addButton(btn)
            self.segment_buttons[key] = btn
        self.segment_buttons["clip"].setChecked(True)

    def _update_channel_grid(self) -> None:
        """按 Dock 宽度重排通道，禁止标签被横向压扁。"""
        grid = self._control_channel_grid
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # takeAt 只移除布局关系，控件本身稍后会重新加入网格。
                widget.setParent(self._control_secondary_widget)

        # 五个通道在常规窄 Dock 中保持一行；最窄时改成三列，
        # 只增加一行，且 Clip/Frame 仍与通道网格处于同一控制区。
        columns = 3 if self.width() < 400 else 5
        for index, widget in enumerate(self._control_channel_widgets):
            grid.addWidget(widget, index // columns, index % columns)

        rows = (len(self._control_channel_widgets) + columns - 1) // columns
        secondary_height = max(
            rows * 24 + max(0, rows - 1) * 3,
            24,
        )
        self._control_area.setFixedHeight(4 + 34 + secondary_height)

    @staticmethod
    def graph_color(key: str) -> str:
        return {
            "tx": "#00E0B0",
            "ty": "#4DA3FF",
            "scale": "#FF38A8",
            "rotation": "#FFD43B",
        }.get(key, "#D8D8D8")

    def _set_graph_mode(self, mode: str) -> None:
        self.graph.set_mode(mode)
        self.mode_changed.emit(mode)

    def _build_params_area(self) -> QWidget:
        area = QWidget(self)
        grid = QGridLayout(area)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setColumnMinimumWidth(0, 78)

        def add_row(row: int, label_text: str, widget: QWidget) -> None:
            label = QLabel(label_text, area)
            label.setObjectName("tracker_secondary")
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)

        self.mode_combo = ChevronComboBox(area)
        for value, text in MODE_ITEMS:
            self.mode_combo.addItem(tr(f"tracker.mode_{value}", text), value)
        self.mode_combo.addItem(tr("tracker.mode_3d", "3D（暂未开放）"), "3d")
        # 3D 项禁用：保留显示但不可选
        model = self.mode_combo.model()
        item = model.item(self.mode_combo.count() - 1)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.mode_combo.setCurrentIndex(1)  # 默认平移旋转缩放
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        add_row(0, tr("tracker.tracking_type", "跟踪类型"), self.mode_combo)

        self.quality_combo = ChevronComboBox(area)
        for value, text in QUALITY_ITEMS:
            self.quality_combo.addItem(tr(f"tracker.quality_{value}", text), value)
        self.quality_combo.setCurrentIndex(1)
        self.quality_combo.currentIndexChanged.connect(
            self._emit_params_changed)
        add_row(1, tr("tracker.analysis_quality", "分析质量"), self.quality_combo)

        self.feature_spin = QSpinBox(area)
        self.feature_spin.setRange(4, 200)
        self.feature_spin.setValue(12)
        self.feature_spin.setToolTip(tr("tracker.feature_count_tooltip", "建议特征点数"))
        self.feature_spin.valueChanged.connect(self._emit_params_changed)
        add_row(2, tr("tracker.feature_count", "特征点数"), self.feature_spin)

        self.search_spin = QDoubleSpinBox(area)
        self.search_spin.setRange(1.0, 8.0)
        self.search_spin.setSingleStep(0.1)
        self.search_spin.setSuffix(" ×")
        self.search_spin.setValue(2.0)
        self.search_spin.setToolTip(tr("tracker.search_multiplier_tooltip", "搜索范围相对图案区域的倍数"))
        self.search_spin.valueChanged.connect(self._emit_params_changed)
        add_row(3, tr("tracker.search_range", "搜索范围"), self.search_spin)

        conf_row = QWidget(area)
        conf_layout = QHBoxLayout(conf_row)
        conf_layout.setContentsMargins(0, 0, 0, 0)
        conf_layout.setSpacing(4)
        self.conf_slider = QSlider(Qt.Orientation.Horizontal, conf_row)
        self.conf_slider.setRange(0, 100)
        self.conf_slider.setValue(35)
        self.conf_slider.setToolTip(tr("tracker.confidence_tooltip", "低于此值的帧将标记为低置信度"))
        self.conf_value = QLabel("0.35", conf_row)
        self.conf_value.setObjectName("tracker_secondary")
        self.conf_slider.valueChanged.connect(
            lambda v: self.conf_value.setText(f"{v / 100.0:.2f}"))
        self.conf_slider.valueChanged.connect(self._emit_params_changed)
        conf_layout.addWidget(self.conf_slider, 1)
        conf_layout.addWidget(self.conf_value)
        add_row(4, tr("tracker.minimum_confidence", "最低置信度"), conf_row)

        self.failure_combo = ChevronComboBox(area)
        for value, text in FAILURE_ITEMS:
            self.failure_combo.addItem(tr(f"tracker.failure_{value}", text), value)
        self.failure_combo.currentIndexChanged.connect(
            self._emit_params_changed)
        add_row(5, tr("tracker.failure_policy", "失败策略"), self.failure_combo)

        display_row = QWidget(area)
        d_layout = QHBoxLayout(display_row)
        d_layout.setContentsMargins(0, 0, 0, 0)
        d_layout.setSpacing(8)
        self.display_boxes: Dict[str, QCheckBox] = {}
        for key, text_key, text_default in (
            ("points", "tracker.show_points", "显示点"),
            ("path", "tracker.show_path", "显示路径"),
            ("boxes", "tracker.show_search_box", "显示搜索框"),
        ):
            box = QCheckBox(tr(text_key, text_default), display_row)
            box.setChecked(True)
            box.toggled.connect(
                lambda checked, k=key: self.display_option_changed.emit(k, checked))
            self.display_boxes[key] = box
            d_layout.addWidget(box)
        d_layout.addStretch(1)
        add_row(6, tr("tracker.display", "显示"), display_row)

        # 统计/原因标签（成功态/失败态）
        self.stats_label = QLabel("", area)
        self.stats_label.setObjectName("tracker_secondary")
        self.stats_label.setWordWrap(True)
        grid.addWidget(self.stats_label, 7, 0, 1, 2)

        self.homography_diagnostics_label = QLabel("", area)
        self.homography_diagnostics_label.setObjectName("tracker_secondary")
        self.homography_diagnostics_label.setWordWrap(True)
        self.homography_diagnostics_label.setVisible(False)
        grid.addWidget(self.homography_diagnostics_label, 8, 0, 1, 2)

        # 需要按状态启禁用的参数控件
        self._param_widgets: List[QWidget] = [
            self.mode_combo, self.quality_combo, self.feature_spin,
            self.search_spin, self.conf_slider, self.failure_combo,
        ]
        return area

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("tracker_bottom_bar")
        bar.setFixedHeight(32)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(3)

        self.interactive_box = QCheckBox(tr("tracker.interactive", "交互"), bar)
        self.interactive_box.setChecked(True)
        self.interactive_box.setToolTip(tr("tracker.interactive_tooltip", "交互模式"))
        layout.addWidget(self.interactive_box)

        self.overlay_btn = QPushButton("◉", bar)
        self.overlay_btn.setObjectName("tracker_icon_action")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setFixedSize(28, 24)
        self.overlay_btn.setAccessibleName(tr("tracker.toggle_overlay", "显示跟踪覆盖层"))
        self.overlay_btn.setToolTip(tr("tracker.toggle_overlay_tooltip", "显示/隐藏画布跟踪覆盖层"))
        self.overlay_btn.toggled.connect(self.overlay_toggled.emit)
        layout.addWidget(self.overlay_btn)

        self.btn_delete_manual = QPushButton("⌫", bar)
        self.btn_delete_manual.setObjectName("tracker_danger")
        self.btn_delete_manual.setFixedSize(28, 24)
        self.btn_delete_manual.setAccessibleName(tr("tracker.delete_manual", "删除手动修正"))
        self.btn_delete_manual.setToolTip(tr("tracker.delete_manual_tooltip", "删除手动修正"))
        self.btn_delete_manual.clicked.connect(self.delete_manual_requested.emit)
        layout.addWidget(self.btn_delete_manual)

        self.btn_reset_current = QPushButton("↺", bar)
        self.btn_reset_current.setObjectName("tracker_icon_action")
        self.btn_reset_current.setFixedSize(28, 24)
        self.btn_reset_current.setAccessibleName(tr("tracker.reset_current", "重置当前帧"))
        self.btn_reset_current.setToolTip(tr("tracker.reset_current_tooltip", "重置当前帧"))
        self.btn_reset_current.clicked.connect(self.reset_current_requested.emit)
        layout.addWidget(self.btn_reset_current)

        self.btn_export_corner_pin = QPushButton("⇧", bar)
        self.btn_export_corner_pin.setObjectName("tracker_icon_action")
        self.btn_export_corner_pin.setFixedSize(28, 24)
        self.btn_export_corner_pin.setAccessibleName(tr("tracker.export_corner_pin", "导出 Corner Pin"))
        self.btn_export_corner_pin.setToolTip(
            tr("tracker.export_corner_pin_tooltip", "导出逐帧透视四角 JSON")
        )
        self.btn_export_corner_pin.clicked.connect(
            self.corner_pin_export_requested.emit
        )
        layout.addWidget(self.btn_export_corner_pin)

        layout.addStretch(1)

        # 云跟踪下拉：云端不存在时禁用态 + tooltip（不做假按钮）
        self.cloud_combo = ChevronComboBox(bar)
        self.cloud_combo.addItems([
            tr("tracker.local", "本地跟踪"),
            tr("tracker.cloud", "云跟踪"),
            tr("tracker.auto", "自动"),
        ])
        self.cloud_combo.setCurrentIndex(0)
        self.cloud_available = False
        self._refresh_cloud_combo()
        layout.addWidget(self.cloud_combo)
        return bar

    def _refresh_cloud_combo(self) -> None:
        # index 1 = 云跟踪
        model = self.cloud_combo.model()
        item = model.item(1)
        if self.cloud_available:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
            self.cloud_combo.setToolTip("")
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.cloud_combo.setToolTip(
                tr("tracker.cloud_unavailable", "云端跟踪不可用，默认使用本地跟踪"))

    def set_cloud_available(self, available: bool) -> None:
        self.cloud_available = bool(available)
        self._refresh_cloud_combo()

    # ================================================================
    # 状态机
    # ================================================================

    def _emit_params_changed(self, *_args: object) -> None:
        """把参数控件的变化向控制器广播。

        之前面板虽然定义了 ``params_changed``，但只有读取参数的接口，
        控件本身没有连接到该信号，导致启用后的参数仍然不会影响跟踪器。
        """
        self.params_changed.emit(self.get_params())

    def _on_mode_changed(self, _index: int) -> None:
        self._apply_state()
        self._emit_params_changed()

    @property
    def state(self) -> PanelState:
        return self._state

    def set_bound_shape(self, shape_name: Optional[str]) -> None:
        self._bound_shape = shape_name
        self.shape_name_label.setText(shape_name or "")
        if shape_name is None and self._state != PanelState.RUNNING:
            self.set_state(PanelState.EMPTY)
        self._apply_state()

    def set_state(self, state: PanelState) -> None:
        if state == self._state:
            self._apply_state()
            return
        self._state = state
        self.status_light.set_from_state(state)
        self._apply_state()

    def _apply_state(self) -> None:
        state = self._state
        running = state == PanelState.RUNNING
        bound = self._bound_shape is not None

        # 参数控件：运行态全部禁用；空态（未绑定形状）同样禁用
        for widget in self._param_widgets:
            widget.setEnabled(not running and bound)
        for box in self.display_boxes.values():
            box.setEnabled(bound)
        for box in self.channel_boxes.values():
            box.setEnabled(bound and not running)
        self.channel_3d.setEnabled(False)
        for button in self.segment_buttons.values():
            button.setEnabled(bound and not running)

        # 跟踪动作按钮
        self.btn_forward.setEnabled(bound and not running)
        self.btn_backward.setEnabled(bound and not running)
        self.btn_forward_one.setEnabled(bound and not running)
        self.btn_backward_one.setEnabled(bound and not running)
        # 中间自动按钮运行中变为取消，保持五按钮结构不变。
        self.btn_auto.setEnabled(bound)
        self._set_auto_button_state(running)
        # 隐藏兼容按钮不参与可见布局。
        self.btn_both.setEnabled(bound and not running)
        self.btn_retrack.setEnabled(bound and not running
                                    and state in (PanelState.PARTIAL_FAILED,
                                                  PanelState.FAILED,
                                                  PanelState.SUCCESS))
        # 隐藏兼容取消按钮
        self.btn_stop.setEnabled(running)
        for btn in self.nav_buttons.values():
            btn.setEnabled(bound and not running)

        self.graph.setEnabled(bound)
        self.empty_hint.setVisible(not bound)
        self.graph.setVisible(bound)
        self.overlay_btn.setEnabled(bound)
        self.interactive_box.setEnabled(bound)
        self.btn_delete_manual.setEnabled(bound and not running)
        self.btn_reset_current.setEnabled(bound and not running)
        self.btn_export_corner_pin.setEnabled(
            bound and not running and self.mode_combo.currentData() == "homography"
        )
        if self.mode_combo.currentData() != "homography":
            self.homography_diagnostics_label.setVisible(False)
        self.toolbar.setEnabled(bound and not running)
        self.btn_params.setEnabled(bound and not running)

        if not running:
            self.progress_label.setText("")
            self.progress_label.setVisible(False)

    # ================================================================
    # 运行时数据
    # ================================================================

    def set_progress(self, frame: int, total: int,
                     points_ok: int, points_total: int) -> None:
        pct = int(frame * 100 / total) if total > 0 else 0
        self.progress_label.setText(_trf(
            "tracker.progress",
            "Frame {frame} / {total} · {pct}% · {points_ok}/{points_total} points",
            frame=frame, total=total, pct=pct,
            points_ok=points_ok, points_total=points_total,
        ))
        self.progress_label.setVisible(True)

    def set_result_stats(self, frame_start: int, frame_end: int,
                         avg_confidence: float) -> None:
        self.stats_label.setText(_trf(
            "tracker.result_stats",
            "有效帧 {frame_start}-{frame_end} · 平均置信度 {confidence:.2f}",
            frame_start=frame_start, frame_end=frame_end,
            confidence=avg_confidence,
        ))

    def set_failure_reason(self, reason: str) -> None:
        self.stats_label.setText(_trf("tracker.failure_reason", "跟踪失败：{reason}", reason=reason))

    def clear_stats(self) -> None:
        self.stats_label.setText("")

    def set_current_frame(self, frame: int) -> None:
        """更新曲线播放头。"""
        frame_number = int(frame)
        self.graph.set_current_frame(frame_number)

    def set_tracking_binding(self, binding: object | None) -> None:
        """把 TrackingBinding 转换为达芬奇曲线窗口需要的显示数据。

        绑定对象由跟踪核心维护，面板只读取其公开字段，不修改绑定，
        因此异步任务完成后可以安全地在 UI 线程刷新曲线。
        """
        if binding is None:
            self.graph.set_series({})
            self.graph.set_markers([], [], [])
            self.clear_stats()
            self.set_state(PanelState.EMPTY)
            return

        shape_name = str(getattr(binding, "shape_name", "") or "Window 1")
        self.set_bound_shape(shape_name)
        transforms = dict(getattr(binding, "transforms", {}) or {})
        manual = dict(getattr(binding, "manual_corrections", {}) or {})
        all_transforms = dict(transforms)
        all_transforms.update(manual)

        # AUD-25809: 原实现对同一批帧排序三次（series 循环、keyframes、
        # 加上 manual 独立排序），跟踪进行中每次进度回调都是 O(n) 重建，
        # 长片段高频刷新退化为 O(n²) 级 UI 开销——排序一次并复用。
        keyframes = sorted(all_transforms)

        series = {
            "tx": [],
            "ty": [],
            "scale": [],
            "rotation": [],
            "perspective": [],
        }
        for frame in keyframes:
            transform = all_transforms[frame]
            tx, ty = getattr(transform, "translation", (0.0, 0.0))
            sx, sy = getattr(transform, "scale", (1.0, 1.0))
            series["tx"].append((int(frame), float(tx)))
            series["ty"].append((int(frame), float(ty)))
            series["scale"].append((int(frame), (float(sx) + float(sy)) / 2.0))
            series["rotation"].append((int(frame), float(getattr(transform, "rotation", 0.0))))

            matrix = tuple(getattr(transform, "matrix", ()))
            if len(matrix) == 9:
                series["perspective"].append(
                    (int(frame), float(abs(matrix[6]) + abs(matrix[7])))
                )

        self.graph.set_series(series)
        manual_frames = sorted(manual)
        failed_frames = sorted(
            int(frame) for frame, transform in transforms.items()
            if getattr(getattr(transform, "status", None), "value", "") == "failed"
        )
        self.graph.set_markers(keyframes, manual_frames, failed_frames)
        self.graph.set_reference_frame(int(getattr(binding, "reference_frame", 0)))

        if all_transforms:
            confidences = [
                float(getattr(transform, "confidence", 0.0))
                for transform in all_transforms.values()
                if getattr(getattr(transform, "status", None), "value", "") != "failed"
            ]
            frame_start = min(keyframes)
            frame_end = max(keyframes)
            average = sum(confidences) / len(confidences) if confidences else 0.0
            self.set_result_stats(frame_start, frame_end, average)
            if failed_frames and confidences:
                self.set_state(PanelState.PARTIAL_FAILED)
            elif failed_frames:
                self.set_state(PanelState.FAILED)
            else:
                self.set_state(PanelState.SUCCESS)
        else:
            self.clear_stats()
            self.set_state(PanelState.EMPTY)

    def set_tracking_result(self, result: object) -> None:
        """兼容 TrackingService 的 TrackResult，供控制器合并后刷新。"""
        transforms = getattr(result, "transforms", {}) or {}
        observations = getattr(result, "observations", {}) or {}
        # TrackResult 不含绑定元数据，保留当前窗口和已存在的手动结果。
        class _ResultBinding:
            def __init__(self, shape_name: str, reference_frame: int) -> None:
                self.shape_name = shape_name
                self.reference_frame = reference_frame
                self.manual_corrections = {}

        binding = _ResultBinding(
            self._bound_shape or "Window 1",
            int(getattr(self.graph.view, "_reference_frame", 0)),
        )
        binding.transforms = transforms
        binding.observations = observations
        self.set_tracking_binding(binding)

    def set_homography_diagnostics(
        self,
        inlier_count: int,
        reprojection_error: float,
        condition_number: float,
        area_ratio: float,
        stable: bool = True,
        reason: str = "",
    ) -> None:
        """显示透视模式的稳定性诊断，不把数值塞进普通状态文本。"""
        state = tr("tracker.stable", "稳定") if stable else tr("tracker.unstable", "不稳定")
        suffix = f" · {reason}" if reason else ""
        self.homography_diagnostics_label.setText(
            _trf(
                "tracker.homography_diagnostics",
                "透视诊断：{state} · 内点 {inliers} · 重投影 {reprojection:.2f}px · "
                "条件数 {condition:.2g} · 面积比 {area:.3f}{suffix}",
                state=state, inliers=int(inlier_count),
                reprojection=float(reprojection_error),
                condition=float(condition_number), area=float(area_ratio),
                suffix=suffix,
            )
        )
        self.homography_diagnostics_label.setVisible(True)

    def clear_homography_diagnostics(self) -> None:
        self.homography_diagnostics_label.clear()
        self.homography_diagnostics_label.setVisible(False)

    # ================================================================
    # 参数读写
    # ================================================================

    def get_params(self) -> dict:
        return {
            "mode": self.mode_combo.currentData(),
            "quality": self.quality_combo.currentData(),
            "feature_count": self.feature_spin.value(),
            "search_multiplier": self.search_spin.value(),
            "min_confidence": self.conf_slider.value() / 100.0,
            "failure_policy": self.failure_combo.currentData(),
        }

    def set_params(self, params: dict) -> None:
        """程序化回填参数控件（undo/预设恢复等外部状态驱动）。

        AUD-25806：6 个参数控件全部直连 ``_emit_params_changed``，逐控件
        赋值会发出一串"半新半旧"的 ``params_changed`` 快照，且调用方
        （color_controller）随后又显式下发一次，造成双重执行。整个回填
        过程用信号锁包住；mode 变化原本经 ``_on_mode_changed`` 触发的
        ``_apply_state`` 刷新在解锁后补做，置信度数值标签同步更新。
        """
        widgets = (
            self.mode_combo,
            self.quality_combo,
            self.feature_spin,
            self.search_spin,
            self.conf_slider,
            self.failure_combo,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            if "mode" in params:
                idx = self.mode_combo.findData(params["mode"])
                if idx >= 0:
                    self.mode_combo.setCurrentIndex(idx)
            if "quality" in params:
                idx = self.quality_combo.findData(params["quality"])
                if idx >= 0:
                    self.quality_combo.setCurrentIndex(idx)
            if "feature_count" in params:
                self.feature_spin.setValue(int(params["feature_count"]))
            if "search_multiplier" in params:
                self.search_spin.setValue(float(params["search_multiplier"]))
            if "min_confidence" in params:
                self.conf_slider.setValue(int(params["min_confidence"] * 100))
            if "failure_policy" in params:
                idx = self.failure_combo.findData(params["failure_policy"])
                if idx >= 0:
                    self.failure_combo.setCurrentIndex(idx)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        # 被信号锁拦掉的纯 UI 侧效果在此补做（不广播 params_changed）。
        if "mode" in params:
            self._apply_state()
        if "min_confidence" in params:
            self.conf_value.setText(f"{self.conf_slider.value() / 100.0:.2f}")
