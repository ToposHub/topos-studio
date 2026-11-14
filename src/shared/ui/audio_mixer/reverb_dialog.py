"""Acoustic Reverb Dialog — 声学房间混响器对话框（CW-A1 重构）。

自主旗舰立体声混响：3D 房间模型卡（H/L/W 拖拽手柄 + Distance 联动）+
实时脉冲响应衰减图谱（直达/早反射簇/晚期尾音，ENV/BARS 视图开关）+
4 张参数模块卡（Reverb 核心 / Early Reflection Tone / Reverb Tone / Output）。

对接项目契约：
  - 构造：``(track_id, track_name, instance_id, initial_params: ReverbParams, parent)``
  - 信号：``reverb_preview`` / ``reverb_committed`` (track_id, ReverbParams)
  - 三段节流 + A/B 对比槽 + 预设（Cathedral / Concert Hall / Scoring Stage /
    Dialogue Room / Vocal Plate）

遥测：``update_output_meter(level_db)`` 输出峰值电平表。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core.timeline_v2 import ReverbParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.audio_mixer.pro_widgets import (
    EditableValuePill,
    KnobParameterCell,
    apply_float_panel,
    glyph_icon,
    PowerToggleSwitch,
    ProModuleCard,
    PRO_DIALOG_QSS,
    format_freq_hz,
    parse_float_clean,
    parse_freq_hz,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

# 声速（m/s）：距离 → 延迟换算（与 DSP/图谱一致）
SPEED_OF_SOUND = 343.0

SUITE_PALETTE = {
    "reverb": "#28D8F0",   # 冰晶青: 核心混响 & 3D 空间
    "er":     "#F0A848",   # 琥珀金: 早期反射 (Early Reflections)
    "tone":   "#4CD490",   # 翡翠绿: 混响音色 (Reverb Tone)
    "output": "#6495FA",   # 钴蓝:   输出混音 (Output)
    "direct": "#F2F8FC",   # 亮白:   直达声脉冲 (Direct)
}

# 预设（完整 ReverbParams dict；与 timeline_v2 默认 Cathedral 对齐）
REVERB_PRESETS: list[dict[str, Any]] = [
    {"name": "Cathedral", "params": {
        "height": 10.00, "length": 30.00, "width": 40.00,
        "pre_delay": 42.0, "reverb_time": 2730.0, "distance": 19.87,
        "brightness": 50.00, "modulation": 2.00,
        "er_lo_freq": 300.0, "er_hi_freq": 10000.0, "er_lo_gain": 0.0, "er_hi_gain": -6.1,
        "rev_lo_freq": 291.0, "rev_hi_freq": 11100.0, "rev_lo_gain": 1.1, "rev_hi_gain": -2.2,
        "dry_wet": 100.00, "direct_db": 0.0, "er_db": 0.0, "reverb_db": 0.0,
    }},
    {"name": "Concert Hall", "params": {
        "height": 14.50, "length": 38.00, "width": 26.00,
        "pre_delay": 28.0, "reverb_time": 1950.0, "distance": 14.50,
        "brightness": 64.00, "modulation": 8.00,
        "er_lo_freq": 240.0, "er_hi_freq": 12500.0, "er_lo_gain": -1.5, "er_hi_gain": -2.5,
        "rev_lo_freq": 220.0, "rev_hi_freq": 12000.0, "rev_lo_gain": 0.5, "rev_hi_gain": -1.8,
        "dry_wet": 45.00, "direct_db": 0.0, "er_db": -1.5, "reverb_db": -2.0,
    }},
    {"name": "Scoring Stage", "params": {
        "height": 9.50, "length": 24.00, "width": 18.00,
        "pre_delay": 16.0, "reverb_time": 1280.0, "distance": 8.80,
        "brightness": 72.00, "modulation": 4.00,
        "er_lo_freq": 200.0, "er_hi_freq": 14000.0, "er_lo_gain": -2.0, "er_hi_gain": -1.0,
        "rev_lo_freq": 210.0, "rev_hi_freq": 13500.0, "rev_lo_gain": -1.0, "rev_hi_gain": -1.2,
        "dry_wet": 36.00, "direct_db": 0.0, "er_db": 0.0, "reverb_db": -3.0,
    }},
    {"name": "Dialogue Room", "params": {
        "height": 3.20, "length": 6.50, "width": 5.00,
        "pre_delay": 4.0, "reverb_time": 420.0, "distance": 2.20,
        "brightness": 58.00, "modulation": 0.00,
        "er_lo_freq": 320.0, "er_hi_freq": 8500.0, "er_lo_gain": -4.0, "er_hi_gain": -4.5,
        "rev_lo_freq": 350.0, "rev_hi_freq": 7800.0, "rev_lo_gain": -3.5, "rev_hi_gain": -5.0,
        "dry_wet": 22.00, "direct_db": 0.0, "er_db": -2.5, "reverb_db": -6.0,
    }},
    {"name": "Vocal Plate", "params": {
        "height": 4.00, "length": 14.00, "width": 12.00,
        "pre_delay": 24.0, "reverb_time": 1680.0, "distance": 4.50,
        "brightness": 84.00, "modulation": 18.00,
        "er_lo_freq": 260.0, "er_hi_freq": 15000.0, "er_lo_gain": -6.0, "er_hi_gain": 1.5,
        "rev_lo_freq": 250.0, "rev_hi_freq": 14500.0, "rev_lo_gain": -3.0, "rev_hi_gain": 2.0,
        "dry_wet": 30.00, "direct_db": 0.0, "er_db": -8.0, "reverb_db": 0.0,
    }},
]


# ═════════════════════════════════════════════════════════════════════════════
# 3D 声学空间模型卡片
# ═════════════════════════════════════════════════════════════════════════════

class AcousticRoomCard(QFrame):
    """交互式 3D 声学房间模型（H/L/W 拖拽手柄 + 声源/听音点距离指示）。"""

    dimensionsChanged = pyqtSignal(float, float, float)  # height, length, width

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.room_height = 10.00
        self.room_length = 30.00
        self.room_width = 40.00
        self.distance = 19.87
        self._active = True

        self._active_handle: Optional[str] = None
        self._hovered_handle: Optional[str] = None
        self._last_pos = QPointF()
        self._handles_pos: dict[str, QPointF] = {}

        self.setMinimumSize(305, 255)
        self.setMouseTracking(True)
        self._apply_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 上部 3D 渲染留白区（必须透明，避免子控件遮盖自绘房间）
        self.canvas_spacer = QWidget()
        self.canvas_spacer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.canvas_spacer.setStyleSheet("background-color: transparent;")
        root.addWidget(self.canvas_spacer, 1)

        # 底部 4 维控制栏
        dim_sec = QFrame()
        dim_sec.setStyleSheet("background-color: transparent; border-top: 1px solid #262932;")
        grid = QGridLayout(dim_sec)
        grid.setContentsMargins(10, 7, 10, 8)
        grid.setSpacing(6)

        for col, title in enumerate(["Height", "Length", "Width", "Room Size"]):
            lbl = QLabel(title)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #888D9C; font-size: 10px; font-weight: 600;")
            grid.addWidget(lbl, 0, col)

        self.pill_h = EditableValuePill("10.00", SUITE_PALETTE["reverb"], width=58)
        self.pill_l = EditableValuePill("30.00", SUITE_PALETTE["reverb"], width=58)
        self.pill_w = EditableValuePill("40.00", SUITE_PALETTE["reverb"], width=58)

        self.lbl_size = QLabel("1200m²")
        self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_size.setFixedSize(64, 21)
        self.lbl_size.setStyleSheet("""
            background-color: #101114;
            color: #28D8F0;
            border: 1px solid #282A32;
            border-radius: 3.5px;
            font-family: 'Segoe UI', 'SF Pro Display', 'Microsoft YaHei';
            font-size: 11px;
            font-weight: 600;
        """)

        self.pill_h.valueEdited.connect(lambda t: self.setDimensions(
            parse_float_clean(t, self.room_height), self.room_length, self.room_width, True))
        self.pill_l.valueEdited.connect(lambda t: self.setDimensions(
            self.room_height, parse_float_clean(t, self.room_length), self.room_width, True))
        self.pill_w.valueEdited.connect(lambda t: self.setDimensions(
            self.room_height, self.room_length, parse_float_clean(t, self.room_width), True))

        grid.addWidget(self.pill_h, 1, 0, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.pill_l, 1, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.pill_w, 1, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.lbl_size, 1, 3, alignment=Qt.AlignmentFlag.AlignCenter)

        root.addWidget(dim_sec)
        self._sync_values()

    def _apply_style(self) -> None:
        bg_col = "#1D1F25" if self._active else "#181A20"
        self.setStyleSheet(f"""
            AcousticRoomCard {{
                background-color: {bg_col};
                border: 1px solid #2A2D37;
                border-radius: 5px;
            }}
        """)

    def setModuleActive(self, active: bool) -> None:
        self._active = active
        self._apply_style()
        for pill in (self.pill_h, self.pill_l, self.pill_w):
            pill.setModuleActive(active)
        self.lbl_size.setStyleSheet(f"""
            background-color: #101114;
            color: {'#28D8F0' if active else '#7E8392'};
            border: 1px solid #282A32;
            border-radius: 3.5px;
            font-family: 'Segoe UI', 'SF Pro Display', 'Microsoft YaHei';
            font-size: 11px;
            font-weight: 600;
        """)
        self.update()

    def setDistance(self, dist: float) -> None:
        self.distance = dist
        self.update()

    def setDimensions(self, h: float, l: float, w: float, emit_signal: bool = False) -> None:
        self.room_height = max(2.0, min(30.0, h))
        self.room_length = max(3.0, min(80.0, l))
        self.room_width = max(3.0, min(80.0, w))
        self._sync_values()
        self.update()
        if emit_signal:
            self.dimensionsChanged.emit(self.room_height, self.room_length, self.room_width)

    def _sync_values(self) -> None:
        self.pill_h.setText(f"{self.room_height:.2f}")
        self.pill_l.setText(f"{self.room_length:.2f}")
        self.pill_w.setText(f"{self.room_width:.2f}")
        area = int(round(self.room_length * self.room_width))
        self.lbl_size.setText(f"{area}m²")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._hovered_handle:
            self._active_handle = self._hovered_handle
            self._last_pos = event.position()
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._active_handle:
            dx = pos.x() - self._last_pos.x()
            dy = pos.y() - self._last_pos.y()
            self._last_pos = pos
            scale = 0.25 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0

            if self._active_handle == "H":
                self.setDimensions(self.room_height - dy * 0.22 * scale,
                                   self.room_length, self.room_width, True)
            elif self._active_handle == "L":
                delta_l = (-dx * 0.55 + dy * 0.45) * 0.45 * scale
                self.setDimensions(self.room_height, self.room_length + delta_l,
                                   self.room_width, True)
            elif self._active_handle == "W":
                self.setDimensions(self.room_height, self.room_length,
                                   self.room_width + dx * 0.42 * scale, True)
            return

        hit = None
        for key, pt in self._handles_pos.items():
            if math.hypot(pos.x() - pt.x(), pos.y() - pt.y()) <= 11.5:
                hit = key
                break
        if hit != self._hovered_handle:
            self._hovered_handle = hit
            self.setCursor(Qt.CursorShape.SizeAllCursor if hit else Qt.CursorShape.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._active_handle:
            self._active_handle = None
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        plot = QRectF(8, 8, self.width() - 16, self.height() - 62)

        # 左上角悬浮容积 HUD 徽章
        vol = int(round(self.room_height * self.room_length * self.room_width))
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 134, 18)
        p.setPen(QPen(QColor("#323642"), 1.0))
        p.setBrush(QColor(16, 18, 23, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if self._active else QColor("#7E8392"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, f"SPACE  ·  {vol:,} m³")

        # 3D 斜二测投影几何
        draw_w = plot.width() - 36
        draw_h = plot.height() - 34

        w_px = 42.0 + (self.room_width / 80.0) * (draw_w * 0.48)
        h_px = 26.0 + (self.room_height / 30.0) * (draw_h * 0.44)
        l_px = 30.0 + (self.room_length / 80.0) * (draw_w * 0.42)

        zx = -0.58 * l_px
        zy = 0.40 * l_px

        total_w = w_px + abs(zx)
        total_h = h_px + zy

        ox = plot.left() + (plot.width() - total_w) / 2.0 + abs(zx)
        oy = plot.top() + (plot.height() - total_h) / 2.0 + h_px + 10.0

        b_bl = QPointF(ox, oy)
        b_br = QPointF(ox + w_px, oy)
        b_tl = QPointF(ox, oy - h_px)
        b_tr = QPointF(ox + w_px, oy - h_px)

        f_bl = QPointF(ox + zx, oy + zy)
        f_br = QPointF(ox + w_px + zx, oy + zy)
        f_tl = QPointF(ox + zx, oy - h_px + zy)
        f_tr = QPointF(ox + w_px + zx, oy - h_px + zy)

        accent = QColor(SUITE_PALETTE["reverb"] if self._active else "#565A66")

        # 1. 半透明声学地面渐变填充
        floor_poly = QPolygonF([b_bl, b_br, f_br, f_bl])
        floor_grad = QLinearGradient(b_bl, f_br)
        floor_grad.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 22))
        floor_grad.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 58))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(floor_grad)
        p.drawPolygon(floor_poly)

        # 2. 声学地面网格线
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 34), 1.0, Qt.PenStyle.DotLine))
        for i in range(1, 4):
            t = i / 4.0
            p.drawLine(QPointF(b_bl.x() + w_px * t, b_bl.y()), QPointF(f_bl.x() + w_px * t, f_bl.y()))
            p.drawLine(QPointF(b_bl.x() + zx * t, b_bl.y() + zy * t),
                       QPointF(b_br.x() + zx * t, b_br.y() + zy * t))

        # 3. 声源 (S) 与听音点 (L) 间距指示（随 Distance 实时联动）
        floor_center = QPointF((b_bl.x() + f_br.x()) / 2.0, (b_bl.y() + f_br.y()) / 2.0)
        dist_ratio = max(0.05, min(1.0, self.distance / 30.0))
        half_span_x = (w_px * 0.34) * dist_ratio
        half_span_y = (zy * 0.28) * dist_ratio
        src_pt = QPointF(floor_center.x() - half_span_x, floor_center.y() - half_span_y)
        lst_pt = QPointF(floor_center.x() + half_span_x, floor_center.y() + half_span_y)

        p.setPen(QPen(QColor(240, 168, 72, 135 if self._active else 45), 1.1, Qt.PenStyle.DashLine))
        p.drawLine(src_pt, lst_pt)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#F2F8FC" if self._active else "#686C78"))
        p.drawEllipse(src_pt, 2.8, 2.8)
        p.setBrush(QColor("#F0A848" if self._active else "#686C78"))
        p.drawEllipse(lst_pt, 2.8, 2.8)

        # 4. 背面虚线棱与前景实线棱
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 95), 1.15, Qt.PenStyle.DashLine))
        p.drawLine(b_bl, b_br)
        p.drawLine(b_bl, b_tl)
        p.drawLine(b_bl, f_bl)

        p.setPen(QPen(accent, 1.5))
        for p1, p2 in [
            (b_br, b_tr), (b_tl, b_tr),
            (b_br, f_br), (b_tl, f_tl), (b_tr, f_tr),
            (f_bl, f_br), (f_br, f_tr), (f_tr, f_tl), (f_tl, f_bl),
        ]:
            p.drawLine(p1, p2)

        # 5. H / L / W 交互控制节点
        self._handles_pos = {"H": b_tl, "L": f_bl, "W": f_br}
        offsets = {"H": QPointF(-6, -21), "L": QPointF(-17, -4), "W": QPointF(9, -4)}

        font = QFont("Segoe UI", 8)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)

        for key, pt in self._handles_pos.items():
            is_hi = (self._active_handle == key or self._hovered_handle == key)
            radius = 6.2 if is_hi else 5.0
            if is_hi and self._active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 65))
                p.drawEllipse(pt, radius + 4.2, radius + 4.2)

            p.setPen(QPen(accent, 1.8))
            p.setBrush(QColor("#14161C"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#F6F8FC") if is_hi else QColor("#9AA0B0"))
            off = offsets[key]
            p.drawText(QRectF(pt.x() + off.x(), pt.y() + off.y(), 18, 15),
                       Qt.AlignmentFlag.AlignCenter, key)


# ═════════════════════════════════════════════════════════════════════════════
# 脉冲响应衰减图谱
# ═════════════════════════════════════════════════════════════════════════════

class ImpulseResponseGraphWidget(QWidget):
    """实时脉冲响应衰减图谱（直达声 / 早反射簇 / 晚期尾音包络）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.params: dict = dict(REVERB_PRESETS[0]["params"])
        self._enabled = True
        self._show_envelope = True
        self._show_bars = True
        self._mouse_pos: Optional[QPointF] = None

        self._btn_env_rect = QRectF()
        self._btn_bars_rect = QRectF()

        self._ml = 44.0
        self._mr = 30.0
        self._mt = 8.0
        self._mb = 22.0

        self.setMinimumSize(540, 255)
        self.setMouseTracking(True)

    def setState(self, params: dict, enabled: bool = True) -> None:
        self.params = params.copy()
        self._enabled = enabled
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml, self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(10.0, self.height() - self._mt - self._mb),
        )

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = None
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse_pos = event.position()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if self._btn_env_rect.contains(pos):
                self._show_envelope = not self._show_envelope
                self.update()
            elif self._btn_bars_rect.contains(pos):
                self._show_bars = not self._show_bars
                self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 深邃夜空渐变底色
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#181A20"))
        bg_grad.setColorAt(1.0, QColor("#13151A"))
        p.fillRect(plot, bg_grad)

        # 2. 水平幅度网格线
        grid_pen = QPen(QColor("#242730"), 1.0)
        p.setFont(QFont("Segoe UI", 8))
        for i in range(6):
            y = plot.top() + (i / 5.0) * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # 纵轴竖排标题 Amplitude
        p.save()
        p.translate(14, plot.center().y())
        p.rotate(-90)
        p.setPen(QColor("#7E828E"))
        p.drawText(QRectF(-40, -8, 80, 16), Qt.AlignmentFlag.AlignCenter, "Amplitude")
        p.restore()

        # 3. 时间轴网格与刻度 (-200ms ~ 4000ms)
        min_ms, max_ms = -200.0, 4000.0
        span_ms = max_ms - min_ms

        def ms_to_x(ms: float) -> float:
            ratio = (ms - min_ms) / span_ms
            return plot.left() + max(0.0, min(1.0, ratio)) * plot.width()

        def x_to_ms(x: float) -> float:
            ratio = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
            return min_ms + ratio * span_ms

        ticks = [
            (-200, "-200ms"), (0, "0ms"), (200, "200ms"),
            (1000, "1000ms"), (2000, "2000ms"), (3000, "3000ms"), (4000, "4000ms"),
        ]
        for t_ms, _label in ticks:
            tx = ms_to_x(t_ms)
            p.setPen(QPen(QColor("#383D4B"), 1.1) if t_ms == 0 else grid_pen)
            p.drawLine(QPointF(tx, plot.top()), QPointF(tx, plot.bottom()))

        # 标签按优先级绘制（0ms 最优先），拥挤时自动省略
        p.setFont(QFont("Segoe UI", 8))
        drawn_boxes = []
        for idx in (1, 0, 6, 2, 3, 4, 5):
            t_ms, label = ticks[idx]
            tx = ms_to_x(t_ms)
            box = QRectF(tx - 26, plot.bottom() + 4, 52, 16)
            if any(box.intersects(b) for b in drawn_boxes):
                continue
            p.setPen(QColor("#B4BAC8") if t_ms == 0 else QColor("#7E828E"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
            drawn_boxes.append(box)

        p.setPen(QPen(QColor("#2E323D"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        alpha_scale = 1.0 if self._enabled else 0.25

        params = self.params
        pre_delay = params.get("pre_delay", 42.0)
        rt60 = params.get("reverb_time", 2730.0)
        dist = params.get("distance", 19.87)
        brightness = params.get("brightness", 50.0) / 100.0
        mod = params.get("modulation", 2.0) / 100.0
        dry_wet = params.get("dry_wet", 100.0) / 100.0

        def db_to_lin(db: float) -> float:
            if db <= -59.5:
                return 0.0
            return min(1.15, 10.0 ** (db / 20.0))

        direct_amp = db_to_lin(params.get("direct_db", 0.0)) * (1.0 - dry_wet * 0.30)
        er_amp = db_to_lin(params.get("er_db", 0.0)) * (0.25 + 0.75 * dry_wet)
        rev_amp = db_to_lin(params.get("reverb_db", 0.0)) * (0.20 + 0.80 * dry_wet)

        room_mean = (params.get("height", 10.0) + params.get("length", 30.0)
                     + params.get("width", 40.0)) / 3.0
        dist_delay_ms = (dist / SPEED_OF_SOUND) * 1000.0

        # 4. 直达声脉冲 (0ms - 亮白)
        x_zero = ms_to_x(0.0)
        if direct_amp > 0.01:
            d_h = plot.height() * 0.86 * min(1.0, direct_amp)
            y_top = plot.bottom() - d_h
            c_dir = QColor(SUITE_PALETTE["direct"])
            c_dir.setAlphaF(0.95 * alpha_scale)
            p.setPen(QPen(c_dir, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(x_zero, plot.bottom()), QPointF(x_zero, y_top))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c_dir)
            p.drawEllipse(QPointF(x_zero, y_top), 2.5, 2.5)

        # 5. 早期反射簇 (Early Reflections - 琥珀金)
        er_start_ms = max(-190.0, max(0.0, pre_delay) + dist_delay_ms)
        er_span_ms = max(45.0, room_mean * 3.8)
        er_tone_boost = 1.0 + (params.get("er_hi_gain", 0.0) + params.get("er_lo_gain", 0.0)) * 0.018
        er_weights = [0.85, 0.68, 0.76, 0.55, 0.64, 0.48, 0.58, 0.42, 0.50, 0.38]

        c_er = QColor(SUITE_PALETTE["er"])
        c_er.setAlphaF(0.90 * alpha_scale)
        p.setPen(QPen(c_er, 1.7))

        for i, w in enumerate(er_weights):
            t_ms = er_start_ms + (i / len(er_weights)) * er_span_ms
            if min_ms <= t_ms <= max_ms and er_amp > 0.01:
                rx = ms_to_x(t_ms)
                amp = er_amp * w * max(0.25, er_tone_boost)
                bar_h = plot.height() * 0.75 * min(1.0, amp)
                p.drawLine(QPointF(rx, plot.bottom()), QPointF(rx, plot.bottom() - bar_h))

        # 6. 晚期混响尾音包络与密度柱 (Late Reverb Tail - 冰晶青)
        tail_start_ms = min(max_ms - 50.0, er_start_ms + er_span_ms * 0.88)
        rev_tone_factor = 1.0 + (params.get("rev_hi_gain", 0.0) * 0.015) + (brightness - 0.5) * 0.25

        if rev_amp > 0.01 and rt60 > 10.0:
            env_path = QPainterPath()
            area_path = QPainterPath()
            x_start = ms_to_x(tail_start_ms)
            area_path.moveTo(x_start, plot.bottom())

            step_px = 3.0
            x_curr = x_start
            pts: list[QPointF] = []

            while x_curr <= plot.right() - 2:
                curr_ms = x_to_ms(x_curr)
                dt = max(0.0, curr_ms - tail_start_ms)
                decay = math.exp(-6.90775 * (dt / rt60))
                if decay < 0.008:
                    break

                ripple = 1.0 + mod * 0.08 * math.sin(dt * 0.045) + (brightness - 0.5) * 0.04 * math.cos(dt * 0.11)
                amp = rev_amp * 0.74 * decay * max(0.3, rev_tone_factor) * ripple
                y_top = plot.bottom() - plot.height() * min(0.92, amp)

                pt = QPointF(x_curr, y_top)
                pts.append(pt)
                if len(pts) == 1:
                    env_path.moveTo(pt)
                else:
                    env_path.lineTo(pt)
                area_path.lineTo(pt)
                x_curr += step_px

            if pts:
                area_path.lineTo(pts[-1].x(), plot.bottom())
                area_path.closeSubpath()

                tail_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                tail_grad.setColorAt(0.0, QColor(40, 216, 240, int(68 * alpha_scale)))
                tail_grad.setColorAt(1.0, QColor(40, 216, 240, int(10 * alpha_scale)))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tail_grad)
                p.drawPath(area_path)

                if self._show_bars:
                    c_bar = QColor(SUITE_PALETTE["reverb"])
                    c_bar.setAlphaF(0.68 * alpha_scale)
                    p.setPen(QPen(c_bar, 1.35))
                    for pt in pts:
                        p.drawLine(QPointF(pt.x(), plot.bottom()), pt)

                if self._show_envelope:
                    p.setPen(QPen(QColor(40, 216, 240, int(55 * alpha_scale)), 4.2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPath(env_path)

                    c_line = QColor("#8AF2FF")
                    c_line.setAlphaF(0.95 * alpha_scale)
                    p.setPen(QPen(c_line, 1.7))
                    p.drawPath(env_path)

        # 7. 右上角视图开关胶囊 (ENV / BARS)
        self._btn_env_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_bars_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_env_rect, "ENV", self._show_envelope),
            (self._btn_bars_rect, "BARS", self._show_bars),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#282B34"), 1.0))
            p.setBrush(QColor(24, 34, 46, 210) if active else QColor(18, 20, 25, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E727E"))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 8. 左上角 HUD 读数 + 悬停十字准星
        if self._mouse_pos is not None and plot.contains(self._mouse_pos):
            mx, my = self._mouse_pos.x(), self._mouse_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(mx, plot.top()), QPointF(mx, plot.bottom()))
            p.drawLine(QPointF(plot.left(), my), QPointF(plot.right(), my))

            cur_ms = x_to_ms(mx)
            amp_ratio = max(1e-4, 1.0 - (my - plot.top()) / plot.height())
            amp_db = 20.0 * math.log10(amp_ratio)
            hud_str = f"Time: {cur_ms:+.0f} ms   •   Level: {amp_db:.1f} dB"
            hud_w = 195
            hud_col = QColor("#28D8F0")
        else:
            hud_str = (f"RT60: {rt60 / 1000.0:.2f}s  •  Pre-Delay: {pre_delay:+.0f}ms"
                       f"  •  Dist: {dist_delay_ms:.1f}ms")
            hud_w = 248
            hud_col = QColor("#B4BAC8")

        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
        p.setPen(QPen(QColor("#323642"), 1.0))
        p.setBrush(QColor(16, 18, 23, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(hud_col)
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# ═════════════════════════════════════════════════════════════════════════════
# 输出电平表
# ═════════════════════════════════════════════════════════════════════════════

class ReverbOutputMeter(QWidget):
    """单条输出峰值电平表（-60..0 dB，点击顶部红灯清除削波）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._meter_db = -60.0
        self._clipped = False
        self.setFixedWidth(26)
        self.setToolTip(I18N.tr("audio.fx.output_meter_tip", "输出峰值电平表（点击顶部红灯清除削波）"))

    def setMeterLevel(self, level_db: float) -> None:
        self._meter_db = max(-60.0, min(0.0, level_db))
        if level_db >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 16.0:
            self._clipped = False
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        tr = QRectF(cx - 3.0, 12.0, 6.0, max(40.0, self.height() - 34.0))

        clip_rect = QRectF(cx - 3.0, 4.0, 6.0, 4.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#252832"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        p.setPen(QPen(QColor("#262932"), 1.0))
        p.setBrush(QColor("#0D0E12"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        m_norm = max(0.0, min(1.0, (self._meter_db + 60.0) / 60.0))
        if m_norm > 0.01:
            fill_h = tr.height() * m_norm
            fill_rect = QRectF(tr.left() + 0.5, tr.bottom() - fill_h, tr.width() - 1.0, fill_h)
            m_grad = QLinearGradient(tr.bottomLeft(), tr.topLeft())
            m_grad.setColorAt(0.0, QColor("#28C882"))
            m_grad.setColorAt(0.75, QColor("#E8B838"))
            m_grad.setColorAt(1.0, QColor("#F05050"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(m_grad)
            p.drawRoundedRect(fill_rect, 1.5, 1.5)


# ═════════════════════════════════════════════════════════════════════════════
# 主对话框
# ═════════════════════════════════════════════════════════════════════════════

class ReverbDialog(QDialog):
    """声学房间混响器对话框（3D 房间 + IR 图谱 + 4 模块卡片）。"""

    reverb_preview = pyqtSignal(str, object)    # 拖动期实时听感
    reverb_committed = pyqtSignal(str, object)  # 松手/预设/开关，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[ReverbParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: ReverbParams = initial_params or ReverbParams.default()
        self._dragging = False
        self._updating = False

        self.setWindowTitle(
            I18N.tr("audio.fx.acoustic_reverb", "Acoustic Reverb") + f" - {track_name or track_id}")
        self.setModal(False)
        apply_float_panel(self)
        self.resize(980, 640)
        self.setMinimumSize(880, 580)

        # A/B 对比槽（对话框内存行为）
        self._active_bank = "A"
        self._banks = {
            "A": self._current,
            "B": ReverbParams.from_dict(REVERB_PRESETS[1]["params"]),
        }
        self._presets: list[dict[str, Any]] = copy.deepcopy(REVERB_PRESETS)
        self._cells: dict[str, KnobParameterCell] = {}
        self._cards: list[ProModuleCard] = []

        self._setup_ui(track_name)
        self._refresh_all_views()

    # ── UI 构建 ───────────────────────────────────────────────

    def _setup_ui(self, track_name: str) -> None:
        self.setStyleSheet(PRO_DIALOG_QSS)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # 1. 顶部工具栏
        top_bar = QFrame()
        top_bar.setFixedHeight(44)
        top_bar.setStyleSheet("background-color: #13151A; border-bottom: 1px solid #232630;")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(14, 6, 14, 6)
        tb_lay.setSpacing(8)

        self.power_switch = PowerToggleSwitch(True, SUITE_PALETTE["reverb"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"ACOUSTIC REVERB  ·  {track_name}")
        title_lbl.setStyleSheet(
            "color: #EAEDF6; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton()
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setIcon(glyph_icon("plus"))
        add_preset_btn.setIconSize(QSize(12, 12))
        add_preset_btn.setFixedSize(23, 23)
        add_preset_btn.setToolTip(I18N.tr("audio.fx.save_preset", "保存为新预设"))
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(185)
        for p in self._presets:
            self.preset_combo.addItem(p["name"])
        self.preset_combo.activated.connect(self._on_preset_selected)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton()
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setIcon(glyph_icon("chevron-left"))
        prev_btn.setIconSize(QSize(12, 12))
        prev_btn.setFixedSize(21, 23)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton()
        next_btn.setObjectName("ToolBtn")
        next_btn.setIcon(glyph_icon("chevron-right"))
        next_btn.setIconSize(QSize(12, 12))
        next_btn.setFixedSize(21, 23)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(10)

        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 23)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton()
        self.btn_copy_ab.setObjectName("ToolBtn")
        self.btn_copy_ab.setIcon(glyph_icon("arrow-right"))
        self.btn_copy_ab.setIconSize(QSize(12, 12))
        self.btn_copy_ab.setFixedSize(24, 23)
        self.btn_copy_ab.setToolTip(I18N.tr("audio.fx.copy_bank", "复制当前参数到另一槽位"))
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 23)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)

        tb_lay.addStretch()

        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedHeight(23)
        reset_btn.setMinimumWidth(max(48, reset_btn.sizeHint().width() + 6))
        reset_btn.clicked.connect(self.reset_reverb)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # 2. 中部：3D 房间 + IR 图谱 + 输出电平表
        upper_row = QHBoxLayout()
        upper_row.setContentsMargins(14, 10, 12, 6)
        upper_row.setSpacing(8)

        self.room_card = AcousticRoomCard()
        self.room_card.dimensionsChanged.connect(self._on_room_changed)
        upper_row.addWidget(self.room_card, 36)

        self.ir_graph = ImpulseResponseGraphWidget()
        upper_row.addWidget(self.ir_graph, 64)

        out_col = QVBoxLayout()
        out_col.setSpacing(2)
        out_lbl = QLabel("OUT")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet(
            "color: #8E94A4; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        out_col.addWidget(out_lbl)

        self.output_meter = ReverbOutputMeter()
        out_col.addWidget(self.output_meter, 1)
        upper_row.addLayout(out_col)

        main_lay.addLayout(upper_row, 52)

        # 3. 底部 4 模块卡片
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(14, 4, 14, 14)
        cards_row.setSpacing(7)

        # Card 1: Reverb 核心（冰晶青）
        card_rev = ProModuleCard("Reverb", "CORE", SUITE_PALETTE["reverb"])
        rev_top = card_rev.add_section()
        rev_top.addWidget(self._add_ctrl(
            "pre_delay", I18N.tr("audio.fx.pre_delay", "Pre-Delay"),
            -200.0, 200.0, 42.0, "-200", "ms", "200",
            lambda v: f"{v:.0f}", parse_float_clean, mode="bipolar", bipolar_origin=0.0,
            knob_size=52))
        rev_top.addWidget(self._add_ctrl(
            "reverb_time", I18N.tr("audio.fx.reverb_time", "Reverb Time"),
            100.0, 4000.0, 2730.0, "100", "ms", "4.0s",
            lambda v: f"{v:.0f}", parse_float_clean, mode="log", knob_size=52))
        rev_bot = card_rev.add_section()
        rev_bot.addWidget(self._add_ctrl(
            "distance", I18N.tr("audio.fx.distance", "Distance"),
            0.0, 30.0, 19.87, "0", "m", "30",
            lambda v: f"{v:.2f}", parse_float_clean))
        rev_bot.addWidget(self._add_ctrl(
            "brightness", I18N.tr("audio.fx.brightness", "Brightness"),
            6.0, 100.0, 50.0, "6", "%", "100",
            lambda v: f"{v:.1f}", parse_float_clean))
        rev_bot.addWidget(self._add_ctrl(
            "modulation", I18N.tr("audio.fx.modulation", "Modulation"),
            0.0, 100.0, 2.0, "0", "%", "100",
            lambda v: f"{v:.1f}", parse_float_clean))

        # Card 2: Early Reflection Tone（琥珀金）
        card_er = ProModuleCard("Early Reflection Tone", "ER TONE", SUITE_PALETTE["er"])
        er_top = card_er.add_section()
        er_top.addWidget(self._add_ctrl(
            "er_lo_freq", I18N.tr("audio.fx.lo_freq", "Lo Freq"),
            150.0, 500.0, 300.0, "150", "Hz", "500",
            format_freq_hz, parse_freq_hz, mode="log", knob_size=48))
        er_top.addWidget(self._add_ctrl(
            "er_hi_freq", I18N.tr("audio.fx.hi_freq", "High Freq"),
            1000.0, 16000.0, 10000.0, "1k", "Hz", "16k",
            format_freq_hz, parse_freq_hz, mode="log", knob_size=48))
        er_bot = card_er.add_section()
        er_bot.addWidget(self._add_ctrl(
            "er_lo_gain", I18N.tr("audio.fx.lo_gain", "Lo Gain"),
            -18.0, 6.0, 0.0, "-18", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, mode="bipolar",
            bipolar_origin=0.0, knob_size=48))
        er_bot.addWidget(self._add_ctrl(
            "er_hi_gain", I18N.tr("audio.fx.hi_gain", "Hi Gain"),
            -18.0, 6.0, -6.1, "-18", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, mode="bipolar",
            bipolar_origin=0.0, knob_size=48))

        # Card 3: Reverb Tone（翡翠绿）
        card_tone = ProModuleCard("Reverb Tone", "TONE", SUITE_PALETTE["tone"])
        tone_top = card_tone.add_section()
        tone_top.addWidget(self._add_ctrl(
            "rev_lo_freq", I18N.tr("audio.fx.lo_freq", "Lo Freq"),
            150.0, 500.0, 291.0, "150", "Hz", "500",
            format_freq_hz, parse_freq_hz, mode="log", knob_size=48))
        tone_top.addWidget(self._add_ctrl(
            "rev_hi_freq", I18N.tr("audio.fx.hi_freq", "Hi Freq"),
            1000.0, 16000.0, 11100.0, "1k", "Hz", "16k",
            format_freq_hz, parse_freq_hz, mode="log", knob_size=48))
        tone_bot = card_tone.add_section()
        tone_bot.addWidget(self._add_ctrl(
            "rev_lo_gain", I18N.tr("audio.fx.lo_gain", "Lo Gain"),
            -18.0, 6.0, 1.1, "-18", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, mode="bipolar",
            bipolar_origin=0.0, knob_size=48))
        tone_bot.addWidget(self._add_ctrl(
            "rev_hi_gain", I18N.tr("audio.fx.hi_gain", "Hi Gain"),
            -18.0, 6.0, -2.2, "-18", "dB", "+6",
            lambda v: f"{v:.1f}", parse_float_clean, mode="bipolar",
            bipolar_origin=0.0, knob_size=48))

        # Card 4: Output（钴蓝）
        card_out = ProModuleCard("Output", "MASTER", SUITE_PALETTE["output"])
        out_top = card_out.add_section()
        out_top.addStretch()
        out_top.addWidget(self._add_ctrl(
            "dry_wet", I18N.tr("audio.fx.dry_wet", "Dry/Wet"),
            0.0, 100.0, 100.0, "Dry", "%", "Wet",
            lambda v: f"{v:.1f}", parse_float_clean, knob_size=52))
        out_top.addStretch()
        out_bot = card_out.add_section()
        out_bot.addWidget(self._add_ctrl(
            "direct_db", I18N.tr("audio.fx.direct", "Direct"),
            -60.0, 6.0, 0.0, "-Inf", "dB", "+6",
            lambda v: ("-Inf" if v <= -59.5 else f"{v:.1f}"), parse_float_clean,
            zero_marker_val=0.0))
        out_bot.addWidget(self._add_ctrl(
            "er_db", "ER", -60.0, 6.0, 0.0, "-Inf", "dB", "+6",
            lambda v: ("-Inf" if v <= -59.5 else f"{v:.1f}"), parse_float_clean,
            zero_marker_val=0.0))
        out_bot.addWidget(self._add_ctrl(
            "reverb_db", I18N.tr("audio.fx.reverb", "Reverb"),
            -60.0, 6.0, 0.0, "-Inf", "dB", "+6",
            lambda v: ("-Inf" if v <= -59.5 else f"{v:.1f}"), parse_float_clean,
            zero_marker_val=0.0))

        self._cards = [card_rev, card_er, card_tone, card_out]
        cards_row.addWidget(card_rev, 28)
        cards_row.addWidget(card_er, 23)
        cards_row.addWidget(card_tone, 23)
        cards_row.addWidget(card_out, 26)

        main_lay.addLayout(cards_row, 48)

    def _add_ctrl(self, key: str, title: str, min_v: float, max_v: float,
                  default_v: float, min_hint: str, mid_hint: str, max_hint: str,
                  formatter, parser, **kwargs) -> KnobParameterCell:
        cell = KnobParameterCell(
            title, min_v, max_v, default_v, self._accent_for(key),
            min_hint, mid_hint, max_hint, formatter, parser, **kwargs)
        cell.valueChanged.connect(lambda val, k=key: self._on_param_changed(k, val))
        cell.interactionStarted.connect(lambda: setattr(self, "_dragging", True))
        cell.interactionEnded.connect(self._on_drag_released)
        self._cells[key] = cell
        return cell

    # ── 状态与联动 ───────────────────────────────────────────

    def _st(self) -> ReverbParams:
        return self._banks[self._active_bank]

    def _update_ab_styles(self) -> None:
        active_css = """
            QPushButton {
                background-color: #13242C;
                color: #28D8F0;
                border: 1px solid #28D8F0;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
            QPushButton {
                background-color: #22252E;
                color: #9096A6;
                border: 1px solid #303440;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setIcon(glyph_icon(
            "arrow-right" if self._active_bank == "A" else "arrow-left"))

    def _accent_for(self, key: str) -> str:
        if key in ("er_lo_freq", "er_hi_freq", "er_lo_gain", "er_hi_gain"):
            return SUITE_PALETTE["er"]
        if key in ("rev_lo_freq", "rev_hi_freq", "rev_lo_gain", "rev_hi_gain"):
            return SUITE_PALETTE["tone"]
        if key in ("dry_wet", "direct_db", "er_db", "reverb_db"):
            return SUITE_PALETTE["output"]
        return SUITE_PALETTE["reverb"]

    def _refresh_all_views(self) -> None:
        st = self._st()
        self._updating = True
        self._update_ab_styles()

        self.power_switch.setChecked(st.enabled)
        self.room_card.setDimensions(st.height, st.length, st.width, emit_signal=False)
        self.room_card.setDistance(st.distance)
        self.room_card.setModuleActive(st.enabled)

        for key, cell in self._cells.items():
            if key in st.to_dict():
                cell.setValue(getattr(st, key))
            cell.setModuleActive(st.enabled)

        for card in self._cards:
            card.setModuleActive(st.enabled)

        self.ir_graph.setState(st.to_dict(), enabled=st.enabled)
        self._updating = False

    def _emit(self, commit: Optional[bool] = None) -> None:
        if self._updating:
            return
        use_commit = (not self._dragging) if commit is None else commit
        if use_commit:
            self.reverb_committed.emit(self._track_id, self._st())
        else:
            self.reverb_preview.emit(self._track_id, self._st())

    def _on_drag_released(self) -> None:
        if self._updating:
            return
        self._dragging = False
        self.reverb_committed.emit(self._track_id, self._st())

    # ── 事件处理 ─────────────────────────────────────────────

    def _switch_bank(self, bank: str) -> None:
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self._emit(commit=True)

    def _copy_bank(self) -> None:
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._st())

    def _on_power_toggled(self, enabled: bool) -> None:
        st = self._st()
        self._banks[self._active_bank] = ReverbParams(**{**st.to_dict(), "enabled": enabled})
        self._refresh_all_views()
        self._emit(commit=True)

    def _on_param_changed(self, key: str, val: float) -> None:
        st = self._st()
        data = st.to_dict()
        data[key] = val
        try:
            self._banks[self._active_bank] = ReverbParams.from_dict(data)
        except ValueError:
            return
        if key == "distance":
            self.room_card.setDistance(val)
        self.ir_graph.setState(self._st().to_dict(), enabled=self._st().enabled)
        self._emit()

    def _on_room_changed(self, h: float, l: float, w: float) -> None:
        st = self._st()
        data = st.to_dict()
        data["height"] = round(h, 2)
        data["length"] = round(l, 2)
        data["width"] = round(w, 2)
        self._banks[self._active_bank] = ReverbParams.from_dict(data)
        self.ir_graph.setState(self._st().to_dict(), enabled=st.enabled)
        self._emit()

    # ── 预设 ─────────────────────────────────────────────────

    def _on_preset_selected(self, combo_idx: int) -> None:
        if 0 <= combo_idx < len(self._presets):
            data = dict(self._presets[combo_idx]["params"])
            data["enabled"] = self._st().enabled  # 保留当前开关状态
            self._banks[self._active_bank] = ReverbParams.from_dict(data)
            self._refresh_all_views()
            # 保留选中项显示（旧逻辑置 -1 会让预设名消失）
            self._emit(commit=True)

    def _step_preset(self, step: int) -> None:
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex(
                (max(self.preset_combo.currentIndex(), 0) + step) % cnt)
            self._on_preset_selected(self.preset_combo.currentIndex())

    def _save_new_preset(self) -> None:
        name, ok = QInputDialog.getText(
            self, I18N.tr("audio.fx.save_preset_title", "保存混响预设"),
            I18N.tr("audio.fx.preset_name_prompt", "输入预设名称:"))
        if ok and name.strip():
            clean = name.strip()
            self._presets.append({"name": clean, "params": self._st().to_dict()})
            self.preset_combo.addItem(clean)

    # ── 公开 API ─────────────────────────────────────────────

    def reset_reverb(self) -> None:
        """重置为 Cathedral 默认（保留当前开关状态）。"""
        was_enabled = self._st().enabled
        data = dict(REVERB_PRESETS[0]["params"])
        data["enabled"] = was_enabled
        self._banks[self._active_bank] = ReverbParams.from_dict(data)
        self._refresh_all_views()
        self._emit(commit=True)

    def get_params(self) -> ReverbParams:
        return copy.deepcopy(self._st())

    def set_current_params(self, params: ReverbParams) -> None:
        """外部静默回填（timeline 变化后同步显示，不回发信号）。"""
        self._banks[self._active_bank] = copy.deepcopy(params)
        self._refresh_all_views()

    def update_output_meter(self, level_db: float) -> None:
        """接入真实音频引擎输出峰值电平 (-60.0 ~ 0.0 dBFS)。"""
        self.output_meter.setMeterLevel(level_db)


if __name__ == "__main__":  # pragma: no cover
    import sys
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = ReverbDialog("t1", "Audio 2", "inst-1", None, None)
    win.show()
    sys.exit(app.exec())
