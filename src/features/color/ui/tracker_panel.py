"""Tracker 主面板（DaVinci 风格窗口布局）。

布局：标题栏 / 自适应传输与通道栏 / 可折叠分析参数 / 曲线图 / 底部交互栏。
配色：背景 ``#1A1A1A``，分隔线 ``#343434``，文本 ``#D8D8D8``，
次要文本 ``#8C8C8C``，选中/警告 ``#FF8A00``。

四态状态机（``PanelState``）决定控件启禁用：
- EMPTY：保留空曲线与时间标尺；
- RUNNING：进度显示，算法参数全部禁用，仅保留停止/取消；
- SUCCESS：绿色灯 + 统计（有效帧范围/平均置信度），重新跟踪/清除可用；
- PARTIAL_FAILED / FAILED：黄色/红色灯 + 失败原因。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QResizeEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QMenu,
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
from src.shared.assets.icon_manager import icon_reset
from src.shared.ui.widgets import ChevronComboBox

_CHECKMARK_ICON_FILE = (
    Path(__file__).resolve().parents[3] / "shared" / "assets" / "icons" / "checkmark.svg"
)
# 与 style_sheet.DARK_THEME 相同：QSS url() 必须用正斜杠路径，
# 原生的 Windows 反斜杠绝对路径会让整张面板样式表解析失败。
# （带引号的 url("file:...") URI 形式会被 QSS 解析成相对路径，不能用。）
_CHECKMARK_ICON_PATH = _CHECKMARK_ICON_FILE.resolve().as_posix()

PANEL_STYLE = """
TrackerPanel { background-color: #202024; }
TrackerPanel QWidget#tracker_transport,
TrackerPanel QWidget#tracker_context_toolbar,
TrackerPanel QWidget#tracker_params,
TrackerPanel QWidget#tracker_bottom_bar { background-color: #2C2D32; }
TrackerPanel QWidget#tracker_title_bar { background-color: #202024; }
TrackerPanel QWidget#tracker_channels { background-color: #2C2D32; }
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
    border-radius: 1px; padding: 0; color: #9A9BA1; font-size: 12px;
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
    color: #CCCCCC; font-size: 12px; spacing: 8px; background: transparent;
}
TrackerPanel QCheckBox::indicator {
    width: 14px; height: 14px; border-radius: 0px;
    border: 1px solid #090A0B; background: #252525;
}
TrackerPanel QCheckBox::indicator:hover {
    border-color: #4A4A4A; background: #2A2A2A;
}
TrackerPanel QCheckBox::indicator:checked {
    background: #17181A; border: 1px solid #08090A;
    image: url("__CHECKMARK_ICON_URL__");
}
TrackerPanel QCheckBox::indicator:checked:hover {
    background: #252629; border-color: #777777;
}
TrackerPanel QCheckBox:checked { color: #AAAAAA; }
TrackerPanel QCheckBox#tracker_channel {
    font-size: 12px; font-weight: 500;
    padding: 0;
}
TrackerPanel QCheckBox#tracker_channel[channel="tx"] { color: #00E0B0; }
TrackerPanel QCheckBox#tracker_channel[channel="ty"] { color: #4DA3FF; }
TrackerPanel QCheckBox#tracker_channel[channel="scale"] { color: #FF38A8; }
TrackerPanel QCheckBox#tracker_channel[channel="rotation"] { color: #FFD43B; }
TrackerPanel QCheckBox#tracker_channel[channel="perspective"] { color: #44DDFF; }
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
    clear_tracking_requested = pyqtSignal()
    # 帧导航：'first'/'prev'/'next'/'last'
    nav_requested = pyqtSignal(str)
    jump_frame_requested = pyqtSignal(int)
    mode_changed = pyqtSignal(str)  # clip / frame
    # 参数与显示
    params_changed = pyqtSignal(dict)
    display_option_changed = pyqtSignal(str, bool)  # points/path/boxes
    # 底部栏
    insert_point_requested = pyqtSignal()
    set_point_requested = pyqtSignal()
    delete_point_requested = pyqtSignal()
    interactive_changed = pyqtSignal(bool)
    overlay_toggled = pyqtSignal(bool)
    delete_manual_requested = pyqtSignal()
    reset_current_requested = pyqtSignal()
    corner_pin_export_requested = pyqtSignal()
    palette_mode_changed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TrackerPanel")
        self.setMinimumWidth(320)
        # Tracker shares a tab stack with Curves in the Color workspace.
        # QMainWindow applies the tightest member width to the whole stack,
        # so a 900px cap here silently capped Curves too, regardless of the
        # Curves dock's own 1800px maximum.
        self.setMaximumWidth(1800)
        self.setStyleSheet(PANEL_STYLE)

        self._state = PanelState.EMPTY
        self._bound_shape: Optional[str] = None
        self._source_frame_size: tuple[int, int] = (0, 0)
        self._selected_feature_id: Optional[int] = None
        self.clear_tracking_requested.connect(self.clear_requested.emit)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_title_bar())
        root.addWidget(_separator())
        self.toolbar = TrackerToolBar(self)
        self.toolbar.setObjectName("tracker_context_toolbar")
        self.toolbar.hide()
        self._window_control_area = self._build_control_area()
        root.addWidget(self._window_control_area)
        root.addWidget(_separator())
        self.params_area = self._build_params_area()
        # 默认呈现达芬奇的曲线窗口；算法参数通过标题栏“参数”按钮展开。
        self.params_area.setVisible(False)
        root.addWidget(self.params_area)

        # 曲线图区（空态提示叠加在图上）
        graph_host = QWidget(self)
        gh_layout = QVBoxLayout(graph_host)
        gh_layout.setContentsMargins(0, 0, 0, 0)
        gh_layout.setSpacing(0)
        self.empty_hint = QLabel(tr("tracker.empty_hint", EMPTY_HINT), graph_host)
        self.empty_hint.setObjectName("tracker_secondary")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.hide()
        self.graph = TrackerGraph(graph_host, show_controls=False)
        gh_layout.addWidget(self.graph, 1)
        self.graph.frame_clicked.connect(self.jump_frame_requested.emit)
        self._window_graph_host = graph_host
        root.addWidget(graph_host, 1)

        root.addWidget(_separator())
        self._window_bottom_bar = self._build_bottom_bar()
        root.addWidget(self._window_bottom_bar)
        from src.features.color.ui.stabilizer_panel import StabilizerPanel
        self.stabilizer = StabilizerPanel(self)
        root.addWidget(self.stabilizer, 1)
        self.stabilizer.hide()
        self.palette_mode = "window"
        self.stabilizer.state_changed.connect(self._sync_reset_button)

        self.set_state(PanelState.EMPTY)

    # ================================================================
    # 区块构建
    # ================================================================

    def set_palette_mode(self, mode: str) -> None:
        """Switch camera stabilization independently of the selected mask."""
        if mode not in ('window', 'stabilizer'):
            return
        self.palette_mode = mode
        stable = mode == 'stabilizer'
        for widget in (self._window_control_area, self._window_graph_host, self._window_bottom_bar):
            widget.setVisible(not stable)
        self.params_area.setVisible(not stable and self.btn_params.isChecked())
        self.stabilizer.setVisible(stable)
        self.title_label.setText(tr('tracker.stabilizer.title', 'Tracker - Stabilizer')
                                 if stable else tr('tracker.title', 'Tracker - Window'))
        for name, button in self.title_mode_buttons.items():
            button.setChecked(name == mode)
        self._sync_reset_button()
        self.palette_mode_changed.emit(mode)

    def _sync_reset_button(self) -> None:
        stable = self.palette_mode == 'stabilizer'
        enabled = (self.stabilizer.can_reset() if stable else
                   self._bound_shape is not None and self._state != PanelState.RUNNING)
        self.btn_clear.setEnabled(enabled)
        label = (tr('tracker.stabilizer.reset', 'Reset Stabilization') if stable else
                 tr('tracker.clear_all_points', 'Clear All Tracking Points'))
        self.btn_clear.setToolTip(label)
        self.btn_clear.setAccessibleName(label)

    def _reset_active_palette(self) -> None:
        if self.palette_mode == 'stabilizer':
            self.stabilizer.reset_requested.emit()
        else:
            self.clear_tracking_requested.emit()

    def _build_title_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("tracker_title_bar")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 2, 0)
        layout.setSpacing(0)
        self.status_light = TrackerStatusLight(bar)
        self.status_light.hide()
        title = QLabel(tr("tracker.title", "Tracker - Window"), bar)
        self.title_label = title
        title.setObjectName("tracker_title")
        layout.addWidget(title)
        self.shape_name_label = QLabel("", bar)
        self.shape_name_label.hide()
        layout.addStretch(1)
        self.title_mode_buttons = {}
        for key, label in (("window", "Window"), ("stabilizer", "Stabilizer"), ("fx", "FX")):
            button = QPushButton(bar)
            button.setObjectName("tracker_icon_action")
            button.setFixedSize(30, 30)
            button.setIcon(self._title_icon(key))
            button.setIconSize(QSize(20, 20))
            button.setStyleSheet("border:0; border-left:1px solid #101014; border-radius:0;")
            button.setAccessibleName(label)
            button.setToolTip(label if key in ("window", "stabilizer") else tr(
                "tracker.mode_unavailable", "{mode} is not available").format(mode=label))
            button.setEnabled(key != "fx")
            button.setCheckable(key != "fx")
            button.setChecked(key == "window")
            if key != "fx":
                button.clicked.connect(lambda _checked=False, mode=key: self.set_palette_mode(mode))
            self.title_mode_buttons[key] = button
            layout.addWidget(button)
        self.btn_clear = QPushButton(bar)
        self.btn_clear.setObjectName("tracker_icon_action")
        self.btn_clear.setFixedSize(38, 30)
        self.btn_clear.setIcon(icon_reset())
        self.btn_clear.setIconSize(QSize(14, 14))
        self.btn_clear.setStyleSheet("border:0; border-left:1px solid #101014; border-radius:0;")
        self.btn_clear.setToolTip(tr("tracker.clear_all_points", "Clear All Tracking Points"))
        self.btn_clear.clicked.connect(self._reset_active_palette)
        layout.addWidget(self.btn_clear)
        # Keep the public advanced-parameters toggle for controller/script callers.
        self.btn_params = QPushButton(bar)
        self.btn_params.setCheckable(True)
        self.btn_params.toggled.connect(self.params_area_visibility_changed)
        self.btn_params.hide()
        self.btn_title_menu = QPushButton(bar)
        self.btn_title_menu.setObjectName("tracker_title_menu")
        self.btn_title_menu.setFixedSize(38, 30)
        self.btn_title_menu.setIcon(self._title_icon("menu"))
        self.btn_title_menu.setIconSize(QSize(20, 20))
        self.btn_title_menu.setStyleSheet("border:0; border-left:1px solid #101014; border-radius:0;")
        self.btn_title_menu.setToolTip(tr("tracker.options", "Tracker options"))
        self.btn_title_menu.clicked.connect(self._show_options_menu)
        layout.addWidget(self.btn_title_menu)
        return bar

    def _show_options_menu(self) -> None:
        menu = QMenu(self)
        advanced = menu.addAction(tr("tracker.params", "Tracking parameters"))
        advanced.setCheckable(True)
        advanced.setChecked(self.btn_params.isChecked())
        advanced.setEnabled(self.btn_params.isEnabled())
        advanced.toggled.connect(self.btn_params.setChecked)
        targets = menu.addMenu(tr("tracker.target_tooltip", "Tracking target"))
        targets.setEnabled(self.toolbar.isEnabled())
        for index in range(self.toolbar.target_combo.count()):
            action = targets.addAction(self.toolbar.target_combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.toolbar.target_combo.currentIndex())
            action.triggered.connect(lambda _checked=False, i=index:
                                     self.toolbar.target_combo.setCurrentIndex(i))
        menu.addSeparator()
        retry = menu.addAction(tr("tracker.retrack_failed", "Retrack failed frames"))
        retry.setEnabled(self.btn_retrack.isEnabled())
        retry.triggered.connect(self.retrack_failed_requested.emit)
        export = menu.addAction(tr("tracker.export_corner_pin", "Export Corner Pin"))
        export.setEnabled(self.btn_export_corner_pin.isEnabled())
        export.triggered.connect(self.corner_pin_export_requested.emit)
        menu.exec(self.btn_title_menu.mapToGlobal(self.btn_title_menu.rect().bottomRight()))

    def params_area_visibility_changed(self, visible: bool) -> None:
        if hasattr(self, "params_area"):
            self.params_area.setVisible(bool(visible))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_control_channel_grid"):
            self._update_channel_grid()

    @staticmethod
    def _title_icon(kind: str) -> QIcon:
        """Draw palette modes on a retina-sized logical 20px canvas."""
        pixmap = QPixmap(40, 40)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#A2A4A4"), 1.15))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "window":
            painter.drawEllipse(QRectF(3.5, 3.5, 13.0, 13.0))
            painter.drawEllipse(QRectF(6.0, 6.0, 8.0, 8.0))
            for x1, y1, x2, y2 in ((10, 0, 10, 6), (10, 14, 10, 20), (0, 10, 6, 10), (14, 10, 20, 10)):
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        elif kind == "stabilizer":
            for points in (((2, 7), (8, 4), (13, 7), (13, 15), (7, 17), (2, 14), (2, 7)),
                           ((8, 4), (8, 12), (13, 15)), ((2, 7), (8, 10), (13, 7)),
                           ((14, 7), (18, 5), (18, 13), (14, 11), (14, 7))):
                painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        elif kind == "fx":
            path = QPainterPath(QPointF(4, 17))
            path.cubicTo(8, 17, 5, 3, 11, 3)
            path.lineTo(13, 4)
            painter.drawPath(path)
            painter.drawLine(QPointF(3, 8), QPointF(11, 8))
            painter.drawLine(QPointF(11, 10), QPointF(17, 16))
            painter.drawLine(QPointF(17, 10), QPointF(11, 16))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#969899"))
            for x in (4.0, 10.0, 16.0):
                painter.drawEllipse(QPointF(x, 10), 1.25, 1.25)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _transport_icon(kind: str) -> QIcon:
        pixmap = QPixmap(40, 40)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#999B98"))
        if kind == "stop":
            painter.drawRect(QRectF(5.5, 3.0, 3.5, 14.0))
            painter.drawRect(QRectF(11.5, 3.0, 3.5, 14.0))
        elif kind == "auto":
            for y, direction in ((6, 1), (14, -1)):
                painter.drawRect(QRectF(3.0, y - 1.0, 14.0, 2.0))
                x = 18 if direction == 1 else 2
                painter.drawPolygon(QPolygonF([QPointF(x, y),
                    QPointF(x - direction * 5, y - 4), QPointF(x - direction * 5, y + 4)]))
        else:
            backwards = "back" in kind
            x = 4 if backwards else 16
            direction = -1 if backwards else 1
            painter.drawPolygon(QPolygonF([QPointF(x, 10),
                QPointF(x - direction * 11, 3), QPointF(x - direction * 11, 17)]))
            if "one" in kind:
                painter.drawRect(QRectF(2.0 if backwards else 16.0, 3.0, 2.0, 14.0))
        painter.end()
        return QIcon(pixmap)

    def _build_control_area(self) -> QWidget:
        area = QWidget(self)
        area.setObjectName("tracker_transport")
        self._control_area = area
        self._control_channel_grid = QGridLayout(area)
        self._control_channel_grid.setContentsMargins(17, 6, 29, 6)
        self._control_channel_grid.setHorizontalSpacing(9)
        self._control_channel_grid.setVerticalSpacing(1)
        self._transport_widget = QWidget(area)
        actions = QHBoxLayout(self._transport_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(0)
        self.nav_buttons = {}
        for key in ("first", "prev", "next", "last"):
            button = QPushButton(area)
            button.clicked.connect(lambda _checked=False, k=key: self.nav_requested.emit(k))
            button.hide()
            self.nav_buttons[key] = button
        for name, kind, label, signal in (
            ("btn_backward_one", "back_one", "Track One Frame Reverse", self.track_backward_one_requested),
            ("btn_backward", "back", "Track Reverse", self.track_backward_requested),
            ("btn_stop", "stop", "Stop Tracking", self.cancel_requested),
            ("btn_auto", "auto", "Track Forward and Reverse", self.track_auto_requested),
            ("btn_forward", "forward", "Track Forward", self.track_forward_requested),
            ("btn_forward_one", "forward_one", "Track One Frame Forward", self.track_forward_one_requested),
        ):
            button = QPushButton(self._transport_widget)
            button.setObjectName("tracker_transport_button")
            button.setIcon(self._transport_icon(kind))
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(TRACK_BUTTON_WIDTH, TRACK_BUTTON_HEIGHT)
            button.setToolTip(tr("tracker.action_" + kind, label))
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(signal.emit)
            setattr(self, name, button)
            actions.addWidget(button)
        self.btn_both = QPushButton(area)
        self.btn_both.clicked.connect(self.track_both_requested.emit)
        self.btn_both.hide()
        self.btn_retrack = QPushButton(area)
        self.btn_retrack.clicked.connect(self.retrack_failed_requested.emit)
        self.btn_retrack.hide()
        self._channel_widget = QWidget(area)
        self._channel_layout = QGridLayout(self._channel_widget)
        self._channel_layout.setContentsMargins(0, 0, 0, 0)
        self._channel_layout.setHorizontalSpacing(0)
        self._channel_layout.setVerticalSpacing(0)
        self._build_channel_controls(area)
        self._segment_widget = QWidget(area)
        segments = QHBoxLayout(self._segment_widget)
        segments.setContentsMargins(0, 0, 0, 0)
        segments.setSpacing(0)
        for button in self.segment_buttons.values():
            segments.addWidget(button)
        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("tracker_secondary")
        self.progress_label.setVisible(False)
        self._control_secondary_widget = self._channel_widget
        self._update_channel_grid()
        return area

    def _build_channel_controls(self, area: QWidget) -> None:
        self.channel_boxes = {}
        for key, label in (("tx", "Pan"), ("ty", "Tilt"), ("scale", "Zoom"), ("rotation", "Rotate")):
            box = QCheckBox(label, area)
            box.setObjectName("tracker_channel")
            box.setProperty("channel", key)
            box.setChecked(True)
            box.setFixedHeight(24)
            box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            box.toggled.connect(lambda checked, k=key: self._on_channel_changed(k, checked))
            self.channel_boxes[key] = box
        self.channel_3d = QCheckBox("3D", area)
        self.channel_3d.setObjectName("tracker_channel")
        self.channel_3d.setProperty("channel", "perspective")
        self.channel_3d.setFixedHeight(24)
        self.channel_3d.setToolTip(tr("tracker.perspective", "Perspective motion (homography)"))
        self.channel_3d.toggled.connect(self._on_perspective_changed)
        self.segment_group = QButtonGroup(area)
        self.segment_group.setExclusive(True)
        self.segment_buttons = {}
        for key, label in (("clip", "Clip"), ("frame", "Frame")):
            button = QPushButton(label, area)
            button.setObjectName("tracker_segment")
            button.setCheckable(True)
            button.setFixedSize(90, 22)
            button.clicked.connect(lambda _checked=False, k=key: self._set_graph_mode(k))
            self.segment_group.addButton(button)
            self.segment_buttons[key] = button
        self.segment_buttons["clip"].setChecked(True)

    def _on_channel_changed(self, key: str, checked: bool) -> None:
        if hasattr(self, "graph"):
            self.graph.set_component_visible(key, checked)
        self._emit_params_changed()

    def _on_perspective_changed(self, checked: bool) -> None:
        index = self.mode_combo.findData("homography" if checked else "trs")
        self.mode_combo.setCurrentIndex(index)
        self.graph.set_component_visible("perspective", checked)

    def _update_channel_grid(self) -> None:
        grid = self._control_channel_grid
        while grid.count():
            grid.takeAt(0)
        while self._channel_layout.count():
            self._channel_layout.takeAt(0)
        columns = 3 if self.width() < 400 else 5
        for index, widget in enumerate([*self.channel_boxes.values(), self.channel_3d]):
            self._channel_layout.addWidget(widget, index // columns, index % columns)
        wide = self.width() >= 760
        grid.setContentsMargins(17 if wide else 8, 6, 29 if wide else 8, 6)
        for index, minimum in enumerate((67, 63, 76, 78, 40)):
            self._channel_layout.setColumnMinimumWidth(index, minimum if wide else 0)
        for button in self.segment_buttons.values():
            button.setFixedWidth(109 if wide else 46)
        if wide:
            grid.addWidget(self._transport_widget, 0, 0)
            grid.addWidget(self._channel_widget, 0, 1)
            grid.addWidget(self._segment_widget, 0, 2)
            grid.setColumnStretch(1, 1)
            area_height = 44
        else:
            grid.addWidget(self._transport_widget, 0, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(self._channel_widget, 1, 0)
            grid.addWidget(self._segment_widget, 1, 1)
            grid.setColumnStretch(1, 0)
            area_height = 94 if columns == 3 else 70
        self._control_area.setFixedHeight(area_height)
        if hasattr(self, "cloud_combo"):
            self.cloud_combo.setFixedWidth(150 if self.width() >= 400 else 112)
            self.interactive_box.setText("Interactive Mode" if self.width() >= 400 else "Interactive")

    @staticmethod
    def graph_color(key: str) -> str:
        return {
            "tx": "#00E0B0",
            "ty": "#4DA3FF",
            "scale": "#FF38A8",
            "rotation": "#FFD43B",
        }.get(key, "#D8D8D8")

    def _set_graph_mode(self, mode: str) -> None:
        self.mode_changed.emit(mode)
        self._emit_params_changed()

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
        self.feature_spin.setRange(4, 1000)
        self.feature_spin.setValue(64)
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
        self.failure_combo.setCurrentIndex(self.failure_combo.findData("recapture"))
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
        bar.setFixedHeight(42)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(21, 0, 29, 0)
        layout.setSpacing(8)

        self.interactive_box = QCheckBox("Interactive Mode", bar)
        self.interactive_box.setChecked(False)
        self.interactive_box.toggled.connect(self.interactive_changed.emit)
        self.interactive_box.toggled.connect(self._emit_params_changed)
        self.interactive_box.toggled.connect(self._on_point_interactive_changed)
        self.interactive_box.setToolTip(tr("tracker.interactive_tooltip", "交互模式"))
        layout.addWidget(self.interactive_box)

        self.overlay_btn = QPushButton("◉", bar)
        self.overlay_btn.setObjectName("tracker_icon_action")
        self.overlay_btn.setCheckable(True)
        self.overlay_btn.setFixedSize(28, 24)
        self.overlay_btn.setStyleSheet("font-size: 17px;")
        self.overlay_btn.setAccessibleName(tr("tracker.toggle_overlay", "显示跟踪覆盖层"))
        self.overlay_btn.setToolTip(tr("tracker.toggle_overlay_tooltip", "显示/隐藏画布跟踪覆盖层"))
        self.overlay_btn.toggled.connect(self.overlay_toggled.emit)
        self.overlay_btn.hide()

        self.btn_delete_manual = QPushButton("⌫", bar)
        self.btn_delete_manual.setObjectName("tracker_danger")
        self.btn_delete_manual.setFixedSize(28, 24)
        self.btn_delete_manual.setStyleSheet("font-size: 17px;")
        self.btn_delete_manual.setAccessibleName(tr("tracker.delete_manual", "删除手动修正"))
        self.btn_delete_manual.setToolTip(tr("tracker.delete_manual_tooltip", "删除手动修正"))
        self.btn_delete_manual.clicked.connect(self.delete_manual_requested.emit)
        self.btn_delete_manual.hide()

        self.btn_reset_current = QPushButton("↺", bar)
        self.btn_reset_current.setObjectName("tracker_icon_action")
        self.btn_reset_current.setText("")
        self.btn_reset_current.setIcon(icon_reset())
        self.btn_reset_current.setIconSize(QSize(14, 14))
        self.btn_reset_current.setFixedSize(28, 24)
        self.btn_reset_current.setStyleSheet("font-size: 17px;")
        self.btn_reset_current.setAccessibleName(tr("tracker.reset_current", "重置当前帧"))
        self.btn_reset_current.setToolTip(tr("tracker.reset_current_tooltip", "重置当前帧"))
        self.btn_reset_current.clicked.connect(self.reset_current_requested.emit)
        self.btn_reset_current.hide()
        for name, kind, tip, signal in (
            ("btn_insert_point", "insert", "Insert Point: click the viewer to add a tracking point", self.insert_point_requested),
            ("btn_set_point", "set", "Set Point: preserve the selected tracking point", self.set_point_requested),
            ("btn_delete_point", "delete", "Delete Point: exclude the selected tracking point", self.delete_point_requested),
        ):
            button = QPushButton(bar)
            button.setObjectName("tracker_icon_action")
            button.setFixedSize(28, 24)
            button.setIcon(self._point_tool_icon(kind))
            button.setIconSize(QSize(22, 18))
            button.setToolTip(tr("tracker.point_" + kind, tip))
            button.setAccessibleName(button.toolTip())
            button.setCheckable(kind == "insert")
            button.clicked.connect(signal.emit)
            setattr(self, name, button)
            layout.addWidget(button)

        self.btn_export_corner_pin = QPushButton("⇧", bar)
        self.btn_export_corner_pin.setObjectName("tracker_icon_action")
        self.btn_export_corner_pin.setFixedSize(28, 24)
        self.btn_export_corner_pin.setAccessibleName(tr("tracker.export_corner_pin", "导出 Corner Pin"))
        self.btn_export_corner_pin.setToolTip(
            tr("tracker.export_corner_pin_tooltip", "导出逐帧透视四角 JSON")
        )
        self.btn_export_corner_pin.hide()
        self.btn_export_corner_pin.clicked.connect(
            self.corner_pin_export_requested.emit
        )
        self.progress_label.setMaximumWidth(180)
        layout.addWidget(self.progress_label)

        layout.addStretch(1)

        # Resolve's Cloud Tracker is a local feature-cloud algorithm, not a cloud service.
        self.cloud_combo = ChevronComboBox(bar)
        self.cloud_combo.setObjectName("tracker_algorithm")
        self.cloud_combo.setStyleSheet(
            "QComboBox { border:1px solid #111215; border-radius:1px; padding:0 6px; "
            "background:#222327; color:#B5B6B9; font-size:12px; }")
        self.cloud_combo.addItem("Cloud Tracker", "region")
        self.cloud_combo.addItem("Point Tracker", "point")
        self.cloud_combo.setFixedSize(150, 22)
        self.cloud_combo.currentIndexChanged.connect(self._on_tracker_algorithm_changed)
        self.cloud_available = True
        self._refresh_cloud_combo()
        layout.addWidget(self.cloud_combo)
        return bar

    @staticmethod
    def _point_tool_icon(kind: str) -> QIcon:
        pixmap = QPixmap(44, 36)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#999B98"))
        if kind == "insert":
            pen = painter.pen()
            pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(pen)
            painter.drawRect(2, 3, 14, 12)
            pen.setStyle(Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.drawLine(14, 3, 20, 3)
            painter.drawLine(17, 0, 17, 6)
        elif kind == "set":
            painter.setBrush(QColor("#999B98"))
            painter.drawPolygon(QPolygonF([QPointF(4, 2), QPointF(4, 14), QPointF(12, 10)]))
            painter.drawLine(13, 12, 19, 12)
            painter.drawLine(16, 9, 16, 15)
        else:
            painter.drawRect(8, 6, 9, 10)
            painter.drawLine(6, 4, 19, 4)
            painter.drawLine(10, 2, 15, 2)
            painter.drawLine(11, 8, 11, 14)
            painter.drawLine(14, 8, 14, 14)
            painter.drawLine(1, 5, 5, 5)
            painter.drawLine(3, 3, 3, 7)
        painter.end()
        return QIcon(pixmap)

    def _on_point_interactive_changed(self, active: bool) -> None:
        if not active:
            self._selected_feature_id = None
            self.set_insert_mode_active(False)
        self._apply_point_tools_state()

    def set_insert_mode_active(self, active: bool) -> None:
        self.btn_insert_point.blockSignals(True)
        self.btn_insert_point.setChecked(bool(active))
        self.btn_insert_point.blockSignals(False)

    def set_selected_feature(self, feature_id: Optional[int]) -> None:
        self._selected_feature_id = int(feature_id) if feature_id is not None and feature_id >= 0 else None
        self._apply_point_tools_state()

    def _apply_point_tools_state(self) -> None:
        editable = (self._bound_shape is not None and self.interactive_box.isChecked()
                    and self._state != PanelState.RUNNING)
        self.btn_insert_point.setEnabled(editable)
        self.btn_set_point.setEnabled(editable and self._selected_feature_id is not None)
        self.btn_delete_point.setEnabled(editable and self._selected_feature_id is not None)
        if not editable:
            self.set_insert_mode_active(False)

    def _on_tracker_algorithm_changed(self, index: int) -> None:
        self.toolbar._on_tool_clicked(str(self.cloud_combo.itemData(index)))
        self._emit_params_changed()

    def _refresh_cloud_combo(self) -> None:
        self.cloud_combo.setToolTip(tr("tracker.algorithm_tooltip",
            "Cloud Tracker: local region features; Point Tracker: a selected point"))

    def set_cloud_available(self, available: bool) -> None:
        """Compatibility API; neither local algorithm needs a cloud connection."""
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
        self.channel_3d.blockSignals(True)
        self.channel_3d.setChecked(self.mode_combo.currentData() == "homography")
        self.channel_3d.blockSignals(False)
        self._apply_state()
        self._emit_params_changed()

    @property
    def state(self) -> PanelState:
        return self._state

    def set_bound_shape(self, shape_name: Optional[str]) -> None:
        if self._bound_shape != shape_name:
            self._selected_feature_id = None
            self.set_insert_mode_active(False)
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
        if running:
            self._selected_feature_id = None
        bound = self._bound_shape is not None

        # 参数控件：运行态全部禁用；空态（未绑定形状）同样禁用
        for widget in self._param_widgets:
            widget.setEnabled(not running and bound)
        for box in self.display_boxes.values():
            box.setEnabled(bound)
        for box in self.channel_boxes.values():
            box.setEnabled(not running)
        self.channel_3d.setEnabled(not running)
        for button in self.segment_buttons.values():
            button.setEnabled(not running)

        # 跟踪动作按钮
        self.btn_forward.setEnabled(bound and not running)
        self.btn_backward.setEnabled(bound and not running)
        self.btn_forward_one.setEnabled(bound and not running)
        self.btn_backward_one.setEnabled(bound and not running)
        # Dedicated stop button remains available during analysis.
        self.btn_auto.setEnabled(bound and not running)
        self._sync_reset_button()
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

        # The graph is also the media scrubber, independently of windows/data.
        self.graph.setEnabled(True)
        self.empty_hint.setVisible(False)
        self.graph.setVisible(True)
        self.overlay_btn.setEnabled(bound)
        self.interactive_box.setEnabled(bound and not running)
        self.cloud_combo.setEnabled(not running)
        self.btn_delete_manual.setEnabled(bound and not running)
        self.btn_reset_current.setEnabled(bound and not running)
        self.btn_export_corner_pin.setEnabled(
            bound and not running and self.mode_combo.currentData() == "homography"
        )
        if self.mode_combo.currentData() != "homography":
            self.homography_diagnostics_label.setVisible(False)
        self.toolbar.setEnabled(bound and not running)
        self.btn_params.setEnabled(bound and not running)
        self._apply_point_tools_state()

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

    def set_source_frame_size(self, width: int, height: int) -> None:
        """Set source pixel dimensions for window-centered motion readouts."""
        self._source_frame_size = (max(0, int(width)), max(0, int(height)))

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
        base = getattr(binding, "base_geometry", None) or {}
        center = base.get("center", (.5, .5))
        width, height = self._source_frame_size
        cx, cy = float(center[0]) * width, float(center[1]) * height
        for frame in keyframes:
            transform = all_transforms[frame]
            tx, ty = getattr(transform, "translation", (0.0, 0.0))
            sx, sy = getattr(transform, "scale", (1.0, 1.0))
            matrix = tuple(getattr(transform, "matrix", ()))
            if width > 0 and height > 0 and len(matrix) == 9:
                denominator = matrix[6] * cx + matrix[7] * cy + matrix[8]
                if abs(denominator) > 1e-12:
                    tx = (matrix[0] * cx + matrix[1] * cy + matrix[2]) / denominator - cx
                    ty = (matrix[3] * cx + matrix[4] * cy + matrix[5]) / denominator - cy
            series["tx"].append((int(frame), float(tx)))
            # Resolve reports displacement at the window pivot, positive Tilt
            # upwards, and Rotate in counterclockwise radians. Keep the stored
            # pixel matrix and degree-based renderer geometry unchanged.
            series["ty"].append((int(frame), -float(ty)))
            # Resolve's Zoom readout is a ratio: identity is 1.00.
            series["scale"].append((int(frame), (float(sx) + float(sy)) / 2.0))
            series["rotation"].append((int(frame), -math.radians(float(getattr(transform, "rotation", 0.0)))))

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
            "channels": {**{key: box.isChecked() for key, box in self.channel_boxes.items()},
                         "perspective": self.channel_3d.isChecked()},
            "correction_mode": "frame" if self.segment_buttons["frame"].isChecked() else "clip",
            "interactive": self.interactive_box.isChecked(),
            "algorithm": str(self.cloud_combo.currentData()),
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
            self.channel_3d.blockSignals(True)
            self.channel_3d.setChecked(self.mode_combo.currentData() == "homography")
            self.channel_3d.blockSignals(False)
            self._apply_state()
        for key, box in self.channel_boxes.items():
            if key in params.get("channels", {}):
                box.blockSignals(True)
                box.setChecked(bool(params["channels"][key]))
                box.blockSignals(False)
                self.graph.set_component_visible(key, box.isChecked())
        if "mode" not in params and "perspective" in params.get("channels", {}):
            enabled = bool(params["channels"]["perspective"])
            self.mode_combo.blockSignals(True)
            self.channel_3d.blockSignals(True)
            self.mode_combo.setCurrentIndex(self.mode_combo.findData("homography" if enabled else "trs"))
            self.channel_3d.setChecked(enabled)
            self.channel_3d.blockSignals(False)
            self.mode_combo.blockSignals(False)
            self._apply_state()
        self.graph.set_component_visible("perspective", self.channel_3d.isChecked())
        if "algorithm" in params:
            index = self.cloud_combo.findData(params["algorithm"])
            if index >= 0:
                self.cloud_combo.blockSignals(True)
                self.toolbar.blockSignals(True)
                try:
                    self.cloud_combo.setCurrentIndex(index)
                    self.toolbar._on_tool_clicked(str(params["algorithm"]))
                finally:
                    self.toolbar.blockSignals(False)
                    self.cloud_combo.blockSignals(False)
        if "correction_mode" in params and params["correction_mode"] in self.segment_buttons:
            self.segment_buttons[params["correction_mode"]].setChecked(True)
        if "interactive" in params:
            self.interactive_box.blockSignals(True)
            self.interactive_box.setChecked(bool(params["interactive"]))
            self.interactive_box.blockSignals(False)
            self._on_point_interactive_changed(bool(params["interactive"]))
        if "min_confidence" in params:
            self.conf_value.setText(f"{self.conf_slider.value() / 100.0:.2f}")
