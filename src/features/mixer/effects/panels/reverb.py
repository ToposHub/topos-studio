from __future__ import annotations

import sys
import math
import copy
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QComboBox, QFrame, QLineEdit, QSizePolicy, QInputDialog
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient, QRadialGradient,
    QPainterPath, QFont, QPolygonF
)


# =====================================================================
# 1. 全局色彩系统与声学常量（与 EQ-UI.py 保持完全同源）
# =====================================================================
# 与 AudioClipEditor-UI / EQ-UI 统一的达芬奇 Fairlight 视觉系统：
# #27272B 哑光中性灰基底 + 1px #141416 纯黑细线切分，字体栈一致
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

SUITE_PALETTE = {
    "reverb": "#28D8F0",   # 冰晶青: 核心混响 & 3D 空间
    "er":     "#F0A848",   # 琥珀金: 早期反射 (Early Reflections)
    "tone":   "#4CD490",   # 翡翠绿: 混响音色 (Reverb Tone)
    "output": "#6495FA",   # 钴蓝:   输出混音 (Output)
    "direct": "#F2F8FC",   # 亮白:   直达声脉冲 (Direct)
}

DEFAULT_PRESETS = {
    "Cathedral": {
        "enabled": True,
        "height": 10.00, "length": 30.00, "width": 40.00,
        "pre_delay": 42.0, "reverb_time": 2730.0, "distance": 19.87,
        "brightness": 50.00, "modulation": 2.00,
        "er_lo_freq": 300.0, "er_hi_freq": 10000.0, "er_lo_gain": 0.0, "er_hi_gain": -6.1,
        "rev_lo_freq": 291.0, "rev_hi_freq": 11100.0, "rev_lo_gain": 1.1, "rev_hi_gain": -2.2,
        "dry_wet": 100.00, "direct_db": 0.0, "er_db": 0.0, "reverb_db": 0.0,
    },
    "Concert Hall": {
        "enabled": True,
        "height": 14.50, "length": 38.00, "width": 26.00,
        "pre_delay": 28.0, "reverb_time": 1950.0, "distance": 14.50,
        "brightness": 64.00, "modulation": 8.00,
        "er_lo_freq": 240.0, "er_hi_freq": 12500.0, "er_lo_gain": -1.5, "er_hi_gain": -2.5,
        "rev_lo_freq": 220.0, "rev_hi_freq": 12000.0, "rev_lo_gain": 0.5, "rev_hi_gain": -1.8,
        "dry_wet": 45.00, "direct_db": 0.0, "er_db": -1.5, "reverb_db": -2.0,
    },
    "Scoring Stage": {
        "enabled": True,
        "height": 9.50, "length": 24.00, "width": 18.00,
        "pre_delay": 16.0, "reverb_time": 1280.0, "distance": 8.80,
        "brightness": 72.00, "modulation": 4.00,
        "er_lo_freq": 200.0, "er_hi_freq": 14000.0, "er_lo_gain": -2.0, "er_hi_gain": -1.0,
        "rev_lo_freq": 210.0, "rev_hi_freq": 13500.0, "rev_lo_gain": -1.0, "rev_hi_gain": -1.2,
        "dry_wet": 36.00, "direct_db": 0.0, "er_db": 0.0, "reverb_db": -3.0,
    },
    "Dialogue Room": {
        "enabled": True,
        "height": 3.20, "length": 6.50, "width": 5.00,
        "pre_delay": 4.0, "reverb_time": 420.0, "distance": 2.20,
        "brightness": 58.00, "modulation": 0.00,
        "er_lo_freq": 320.0, "er_hi_freq": 8500.0, "er_lo_gain": -4.0, "er_hi_gain": -4.5,
        "rev_lo_freq": 350.0, "rev_hi_freq": 7800.0, "rev_lo_gain": -3.5, "rev_hi_gain": -5.0,
        "dry_wet": 22.00, "direct_db": 0.0, "er_db": -2.5, "reverb_db": -6.0,
    },
    "Vocal Plate": {
        "enabled": True,
        "height": 4.00, "length": 14.00, "width": 12.00,
        "pre_delay": 24.0, "reverb_time": 1680.0, "distance": 4.50,
        "brightness": 84.00, "modulation": 18.00,
        "er_lo_freq": 260.0, "er_hi_freq": 15000.0, "er_lo_gain": -6.0, "er_hi_gain": 1.5,
        "rev_lo_freq": 250.0, "rev_hi_freq": 14500.0, "rev_lo_gain": -3.0, "rev_hi_gain": 2.0,
        "dry_wet": 30.00, "direct_db": 0.0, "er_db": -8.0, "reverb_db": 0.0,
    },
}


# =====================================================================
# 2. 控件：现代发光电源开关（与 EQ-UI.py 完全一致）
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool):
        if self._checked != val:
            self._checked = val
            self.update()
            self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#12262E") if self._checked else QColor("#121215")
        border = QColor("#28D8F0") if self._checked else QColor("#36363D")
        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.2
        cy = self.height() / 2.0
        cx = (self.width() - 8.8) if self._checked else 8.8
        knob_color = QColor("#28D8F0") if self._checked else QColor("#666670")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


# =====================================================================
# 3. 控件：内嵌可编辑数字胶囊（与 EQ-UI.py 风格一致，支持 k / -Inf）
# =====================================================================
class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(float)

    def __init__(
        self,
        min_val: float,
        max_val: float,
        decimals: int = 2,
        accent_color: str = "#28D8F0",
        allow_neg_inf: bool = False,
        k_format: bool = False,
        width: int = 56,
        parent=None,
    ):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val
        self._decimals = decimals
        self._accent = accent_color
        self._allow_neg_inf = allow_neg_inf
        self._k_format = k_format
        self._value = 0.0
        self._text = "0.0"
        self._active = True

        self.setFixedSize(width, 22)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(1, 1, width - 2, 20)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #151518;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 11px;
                font-weight: 600;
                padding: 0px 2px;
            }}
        """)
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def setValue(self, val: float):
        self._value = max(self._min, min(self._max, val))
        self._text = self._format_val(self._value)
        self.update()

    def setBandActive(self, active: bool):
        self._active = active
        self.update()

    def _format_val(self, val: float) -> str:
        if self._allow_neg_inf and val <= self._min + 0.05:
            return "-Inf"
        if self._k_format and abs(val) >= 1000.0:
            return f"{val / 1000.0:.1f}k"
        if self._decimals == 0:
            return f"{int(round(val))}"
        return f"{val:.{self._decimals}f}"

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._editor.setText(self._text)
            self._editor.show()
            self._editor.setFocus()
            self._editor.selectAll()

    def _commit_edit(self):
        if not self._editor.isVisible():
            return
        raw = self._editor.text().strip().lower()
        self._editor.hide()
        if not raw:
            return
        try:
            if self._allow_neg_inf and raw in ("-inf", "inf", "-oo"):
                parsed = self._min
            elif raw.endswith("k"):
                parsed = float(raw[:-1]) * 1000.0
            else:
                parsed = float(raw)
            parsed = max(self._min, min(self._max, parsed))
            self.setValue(parsed)
            self.valueEdited.emit(parsed)
        except ValueError:
            pass

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 5.0, 5.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 10)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# =====================================================================
# 4. 控件：拉丝阳极铝质感旋钮（完全复刻 EQ-UI.py ProRotaryKnob）
# =====================================================================
class ProRotaryKnob(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        mode: str = "linear",      # "linear" | "log" | "bipolar"
        origin_value: float = 0.0,
        zero_db_marker: bool = False,
        diameter: int = 38,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._origin_val = origin_value
        self._zero_db_marker = zero_db_marker
        self._diameter = diameter

        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(diameter + 6, diameter)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def value(self) -> float:
        return self._value

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setBandActive(self, active: bool):
        self._active = active
        self.update()

    def _val_to_norm(self, val: float) -> float:
        if self._max <= self._min:
            return 0.0
        if self._mode == "log":
            v = max(1e-3, val)
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return math.log(v / mn) / math.log(mx / mn)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        return self._min + n * (self._max - self._min)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.position().y()
        elif event.button() == Qt.MouseButton.RightButton:
            self.setValue(self._default, emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.03
        norm = self._val_to_norm(self._value) + delta * step
        self.setValue(self._norm_to_val(norm), emit=True)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        norm = max(0.0, min(1.0, self._val_to_norm(self._value)))

        start_deg = 225.0
        total_span = -270.0
        curr_deg = start_deg + norm * total_span

        arc_r = (self._diameter / 2.0) - 3.2
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        # 1. 深色凹槽底环
        p.setPen(QPen(QColor("#141417"), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        # 双极性模式在零点基准绘制刻度
        if self._mode == "bipolar":
            zero_norm = self._val_to_norm(self._origin_val)
            z_deg = start_deg + zero_norm * total_span
            z_rad = math.radians(z_deg)
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(cx + (arc_r - 0.8) * math.cos(z_rad), cy - (arc_r - 0.8) * math.sin(z_rad)),
                QPointF(cx + (arc_r + 2.4) * math.cos(z_rad), cy - (arc_r + 2.4) * math.sin(z_rad)),
            )

        # 0 dB 外刻度标（用于 Output 通道电平）
        if self._zero_db_marker:
            zero_norm = self._val_to_norm(0.0)
            z_rad = math.radians(start_deg + zero_norm * total_span)
            p1 = QPointF(cx + (arc_r + 1.0) * math.cos(z_rad), cy - (arc_r + 1.0) * math.sin(z_rad))
            p2 = QPointF(cx + (arc_r + 3.6) * math.cos(z_rad), cy - (arc_r + 3.6) * math.sin(z_rad))
            p.setPen(QPen(QColor("#9A9AA2"), 1.2))
            p.drawLine(p1, p2)

        # 2. 主题色激活光环
        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_norm = self._val_to_norm(self._origin_val)
            zero_deg = start_deg + zero_norm * total_span
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        # 3. 旋钮黑边凹槽 + 达芬奇风格哑光浅灰旋钮顶盖
        knob_r = arc_r - 3.8
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # 4. 指针刻线
        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * (knob_r * 0.32)
        iy1 = cy - math.sin(rad) * (knob_r * 0.32)
        ix2 = cx + math.cos(rad) * (knob_r - 1.5)
        iy2 = cy - math.sin(rad) * (knob_r - 1.5)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


# =====================================================================
# 5. 复合参数单元（支持水平拆分式 与 垂直紧凑式 两种布局）
# =====================================================================
class KnobControlUnit(QWidget):
    valueChanged = pyqtSignal(str, float)

    def __init__(
        self,
        param_key: str,
        title: str,
        min_val: float,
        max_val: float,
        default_val: float,
        unit: str = "",
        left_hint: str = "",
        right_hint: str = "",
        decimals: int = 2,
        accent_color: str = "#28D8F0",
        mode: str = "linear",
        origin_value: float = 0.0,
        allow_neg_inf: bool = False,
        k_format: bool = False,
        zero_db_marker: bool = False,
        knob_diameter: int = 42,           # 对标 Reverb-UI.py：核心 52 / 频率增益 48 / 紧凑 42
        layout_style: str = "compact",     # "compact" (标题→旋钮→刻度→胶囊 垂直堆叠，还原旧版布局)
        parent=None,
    ):
        super().__init__(parent)
        self.param_key = param_key
        self._accent = accent_color

        l_str = left_hint or self._fmt_hint(min_val, allow_neg_inf, k_format)
        r_str = right_hint or self._fmt_hint(max_val, False, k_format, show_plus=(min_val < 0 and max_val <= 24))

        knob_d = knob_diameter
        self.knob = ProRotaryKnob(
            min_val, max_val, default_val, default_val,
            accent_color=accent_color, mode=mode,
            origin_value=origin_value, zero_db_marker=zero_db_marker,
            diameter=knob_d,
        )
        self.pill = EditableValuePill(
            min_val, max_val, decimals=decimals, accent_color=accent_color,
            allow_neg_inf=allow_neg_inf, k_format=k_format,
            width=68,
        )
        self.pill.setValue(default_val)

        self.knob.valueChanged.connect(self._on_knob)
        self.pill.valueEdited.connect(self._on_pill)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("color: #9A9AA2; font-size: 11px; font-weight: 600;")

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(0, 0, 0, 0)
        hints_row.setSpacing(2)
        lbl_l = QLabel(l_str)
        lbl_u = QLabel(unit)
        lbl_r = QLabel(r_str)
        for lb in (lbl_l, lbl_u, lbl_r):
            lb.setStyleSheet("color: #787882; font-size: 9px;")
        hints_row.addWidget(lbl_l)
        hints_row.addStretch()
        if unit:
            hints_row.addWidget(lbl_u)
            hints_row.addStretch()
        hints_row.addWidget(lbl_r)

        if layout_style == "horizontal":
            root = QHBoxLayout(self)
            root.setContentsMargins(2, 2, 2, 2)
            root.setSpacing(4)

            left_col = QVBoxLayout()
            left_col.setSpacing(4)
            left_col.addWidget(self.title_lbl)
            left_col.addWidget(self.pill)
            left_col.addStretch()

            right_col = QVBoxLayout()
            right_col.setSpacing(1)
            right_col.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)
            right_col.addLayout(hints_row)

            root.addLayout(left_col)
            root.addStretch()
            root.addLayout(right_col)
        else:
            root = QVBoxLayout(self)
            root.setContentsMargins(1, 2, 1, 2)
            root.setSpacing(2)
            self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(self.title_lbl)
            root.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)
            root.addLayout(hints_row)
            root.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def _fmt_hint(val: float, neg_inf: bool, k_fmt: bool, show_plus: bool = False) -> str:
        if neg_inf:
            return "-Inf"
        if k_fmt and abs(val) >= 1000:
            return f"{int(val // 1000)}k"
        if val == int(val):
            iv = int(val)
            return f"+{iv}" if (show_plus and iv > 0) else str(iv)
        return f"{val:.1f}"

    def value(self) -> float:
        return self.knob.value()

    def setValue(self, val: float, emit: bool = False):
        self.knob.setValue(val, emit=False)
        self.pill.setValue(self.knob.value())
        if emit:
            self.valueChanged.emit(self.param_key, self.knob.value())

    def setBandActive(self, active: bool):
        self.knob.setBandActive(active)
        self.pill.setBandActive(active)
        self.title_lbl.setStyleSheet(
            f"color: {'#9A9AA2' if active else '#6E6E76'}; font-size: 11px; font-weight: 600;"
        )

    def _on_knob(self, val: float):
        self.pill.setValue(val)
        self.valueChanged.emit(self.param_key, val)

    def _on_pill(self, val: float):
        self.knob.setValue(val, emit=False)
        self.valueChanged.emit(self.param_key, val)


# =====================================================================
# 6. 控件：交互式 3D 声学空间模型卡片（左上视口）
# =====================================================================
class AcousticRoomCard(QFrame):
    dimensionsChanged = pyqtSignal(float, float, float)  # height, length, width

    def __init__(self, parent=None):
        super().__init__(parent)
        self.room_height = 10.00
        self.room_length = 30.00
        self.room_width = 40.00
        self.distance = 19.87
        self._active = True

        self._active_handle = None
        self._hovered_handle = None
        self._last_pos = QPointF()
        self._handles_pos: dict[str, QPointF] = {}

        self.setMinimumSize(305, 255)
        self.setMouseTracking(True)
        self._apply_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 上部 3D 渲染留白区（必须透明，否则子控件会遮盖 paintEvent 绘制的房间图形）
        self.canvas_spacer = QWidget()
        self.canvas_spacer.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.canvas_spacer.setStyleSheet("background-color: transparent;")
        root.addWidget(self.canvas_spacer, 1)

        # 底部 4 个维度控制栏（采用 EQ-UI.py SubSection 风格）
        dim_sec = QFrame()
        dim_sec.setStyleSheet("background-color: transparent; border-top: 1px solid #141416;")
        grid = QGridLayout(dim_sec)
        grid.setContentsMargins(10, 7, 10, 8)
        grid.setSpacing(6)

        for col, title in enumerate(["Height", "Length", "Width", "Room Size"]):
            lbl = QLabel(title)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #9A9AA2; font-size: 10px; font-weight: 600;")
            grid.addWidget(lbl, 0, col)

        self.pill_h = EditableValuePill(2.0, 30.0, decimals=2, accent_color=SUITE_PALETTE["reverb"], width=58)
        self.pill_l = EditableValuePill(3.0, 80.0, decimals=2, accent_color=SUITE_PALETTE["reverb"], width=58)
        self.pill_w = EditableValuePill(3.0, 80.0, decimals=2, accent_color=SUITE_PALETTE["reverb"], width=58)

        self.lbl_size = QLabel("1200m²")
        self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_size.setFixedSize(64, 21)
        self.lbl_size.setStyleSheet("""
            background-color: #101114;
            color: #28D8F0;
            border: 1px solid #282A32;
            border-radius: 3px;
            font-family: {MONO_FONT_CSS};
            font-size: 11px;
            font-weight: 600;
        """)

        self.pill_h.valueEdited.connect(lambda v: self.setDimensions(v, self.room_length, self.room_width, True))
        self.pill_l.valueEdited.connect(lambda v: self.setDimensions(self.room_height, v, self.room_width, True))
        self.pill_w.valueEdited.connect(lambda v: self.setDimensions(self.room_height, self.room_length, v, True))

        grid.addWidget(self.pill_h, 1, 0, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.pill_l, 1, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.pill_w, 1, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.lbl_size, 1, 3, alignment=Qt.AlignmentFlag.AlignCenter)

        root.addWidget(dim_sec)
        self._sync_values()

    def _apply_style(self):
        bg_col = "#27272B"
        self.setStyleSheet(f"""
            AcousticRoomCard {{
                background-color: {bg_col};
                border: none;
                border-right: 1px solid #141416;
                border-radius: 0px;
            }}
        """)

    def setBandActive(self, active: bool):
        self._active = active
        self._apply_style()
        for pill in (self.pill_h, self.pill_l, self.pill_w):
            pill.setBandActive(active)
        self.lbl_size.setStyleSheet(f"""
            background-color: #101114;
            color: {'#28D8F0' if active else '#7E8392'};
            border: 1px solid #282A32;
            border-radius: 3px;
            font-family: {MONO_FONT_CSS};
            font-size: 11px;
            font-weight: 600;
        """)
        self.update()

    def setDistance(self, dist: float):
        self.distance = dist
        self.update()

    def setDimensions(self, h: float, l: float, w: float, emit_signal: bool = False):
        self.room_height = max(2.0, min(30.0, h))
        self.room_length = max(3.0, min(80.0, l))
        self.room_width = max(3.0, min(80.0, w))
        self._sync_values()
        self.update()
        if emit_signal:
            self.dimensionsChanged.emit(self.room_height, self.room_length, self.room_width)

    def _sync_values(self):
        self.pill_h.setValue(self.room_height)
        self.pill_l.setValue(self.room_length)
        self.pill_w.setValue(self.room_width)
        area = int(round(self.room_length * self.room_width))
        self.lbl_size.setText(f"{area}m²")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._hovered_handle:
            self._active_handle = self._hovered_handle
            self._last_pos = event.position()
            self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._active_handle:
            dx = pos.x() - self._last_pos.x()
            dy = pos.y() - self._last_pos.y()
            self._last_pos = pos
            scale = 0.25 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0

            if self._active_handle == "H":
                self.setDimensions(self.room_height - dy * 0.22 * scale, self.room_length, self.room_width, True)
            elif self._active_handle == "L":
                delta_l = (-dx * 0.55 + dy * 0.45) * 0.45 * scale
                self.setDimensions(self.room_height, self.room_length + delta_l, self.room_width, True)
            elif self._active_handle == "W":
                self.setDimensions(self.room_height, self.room_length, self.room_width + dx * 0.42 * scale, True)
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

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._active_handle:
            self._active_handle = None
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 3D 绘图区几何范围（不绘制内层框，房间直接画在卡片底色上，与旧版一致的单层边框）
        plot = QRectF(8, 8, self.width() - 16, self.height() - 62)

        # 左上角悬浮容积 HUD 徽章
        vol = int(round(self.room_height * self.room_length * self.room_width))
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, 134, 18)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#28D8F0") if self._active else QColor("#7E8392"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, f"SPACE  ·  {vol:,} m³")

        # 3D 斜二测投影几何计算
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
            p.drawLine(QPointF(b_bl.x() + zx * t, b_bl.y() + zy * t), QPointF(b_br.x() + zx * t, b_br.y() + zy * t))

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

        # 5. H / L / W 交互控制节点（复刻 EQ-UI.py 节点光晕质感）
        self._handles_pos = {"H": b_tl, "L": f_bl, "W": f_br}
        offsets = {"H": QPointF(-6, -21), "L": QPointF(-17, -4), "W": QPointF(9, -4)}

        font = QFont(MONO_FONT_FAM, 8)
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
            p.setBrush(QColor("#151518"))
            p.drawEllipse(pt, radius, radius)

            p.setPen(QColor("#F6F8FC") if is_hi else QColor("#9A9AA2"))
            off = offsets[key]
            p.drawText(QRectF(pt.x() + off.x(), pt.y() + off.y(), 18, 15), Qt.AlignmentFlag.AlignCenter, key)


# =====================================================================
# 7. 控件：实时脉冲响应衰减图谱（完全对齐 ProEQGraphWidget 美学）
# =====================================================================
class ImpulseResponseGraphWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.params = DEFAULT_PRESETS["Cathedral"].copy()
        self._enabled = True
        self._show_envelope = True
        self._show_bars = True
        self._mouse_pos: QPointF | None = None

        self._btn_env_rect = QRectF()
        self._btn_bars_rect = QRectF()

        self._ml = 44.0
        self._mr = 30.0
        self._mt = 8.0
        self._mb = 22.0

        # 最小宽度保证两侧刻度标签（-200ms / 4000ms）不被裁切或挤压
        self.setMinimumSize(540, 255)
        self.setMouseTracking(True)

    def setState(self, params: dict, enabled: bool = True):
        self.params = params.copy()
        self._enabled = enabled
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(10.0, self.height() - self._mt - self._mb),
        )

    def leaveEvent(self, event):
        self._mouse_pos = None
        self.update()

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if self._btn_env_rect.contains(pos):
                self._show_envelope = not self._show_envelope
                self.update()
            elif self._btn_bars_rect.contains(pos):
                self._show_bars = not self._show_bars
                self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        # 1. 深邃夜空渐变底色（与 EQ-UI.py 完全一致）
        bg_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        bg_grad.setColorAt(0.0, QColor("#1D1D20"))
        bg_grad.setColorAt(1.0, QColor("#1D1D20"))
        p.fillRect(plot, bg_grad)

        # 2. 水平幅度网格线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))
        for i in range(6):
            y = plot.top() + (i / 5.0) * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # 纵轴竖排标题 Amplitude
        p.save()
        p.translate(14, plot.center().y())
        p.rotate(-90)
        p.setPen(QColor("#9A9AA2"))
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
        # 网格线始终全部绘制
        for t_ms, label in ticks:
            tx = ms_to_x(t_ms)
            p.setPen(QPen(QColor("#3A3A40"), 1.1) if t_ms == 0 else grid_pen)
            p.drawLine(QPointF(tx, plot.top()), QPointF(tx, plot.bottom()))

        # 标签按优先级绘制（0ms 最优先，其次 -200ms / 4000ms），拥挤时自动省略防止相互重叠
        p.setFont(QFont(MONO_FONT_FAM, 8))
        drawn_boxes = []
        for idx in (1, 0, 6, 2, 3, 4, 5):
            t_ms, label = ticks[idx]
            tx = ms_to_x(t_ms)
            box = QRectF(tx - 26, plot.bottom() + 4, 52, 16)
            if any(box.intersects(b) for b in drawn_boxes):
                continue
            p.setPen(QColor("#D0D0D8") if t_ms == 0 else QColor("#9A9AA2"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
            drawn_boxes.append(box)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        alpha_scale = 1.0 if self._enabled else 0.25

        # 提取声学参数
        pre_delay = self.params["pre_delay"]
        rt60 = self.params["reverb_time"]
        dist = self.params["distance"]
        brightness = self.params["brightness"] / 100.0
        mod = self.params["modulation"] / 100.0
        dry_wet = self.params["dry_wet"] / 100.0

        def db_to_lin(db: float) -> float:
            if db <= -59.5:
                return 0.0
            return min(1.15, 10.0 ** (db / 20.0))

        direct_amp = db_to_lin(self.params["direct_db"]) * (1.0 - dry_wet * 0.30)
        er_amp = db_to_lin(self.params["er_db"]) * (0.25 + 0.75 * dry_wet)
        rev_amp = db_to_lin(self.params["reverb_db"]) * (0.20 + 0.80 * dry_wet)

        room_mean = (self.params["height"] + self.params["length"] + self.params["width"]) / 3.0
        dist_delay_ms = (dist / 343.0) * 1000.0

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

        # 5. 早期反射簇 (Early Reflections - 琥珀金 #F0A848)
        er_start_ms = max(-190.0, pre_delay + dist_delay_ms)
        er_span_ms = max(45.0, room_mean * 3.8)
        er_tone_boost = 1.0 + (self.params["er_hi_gain"] + self.params["er_lo_gain"]) * 0.018
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

        # 6. 晚期混响尾音包络与密度柱 (Late Reverb Tail - 冰晶青 #28D8F0)
        tail_start_ms = min(max_ms - 50.0, er_start_ms + er_span_ms * 0.88)
        rev_tone_factor = 1.0 + (self.params["rev_hi_gain"] * 0.015) + (brightness - 0.5) * 0.25

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

                # 半透明青色垂直渐变底色
                tail_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
                tail_grad.setColorAt(0.0, QColor(40, 216, 240, int(68 * alpha_scale)))
                tail_grad.setColorAt(1.0, QColor(40, 216, 240, int(10 * alpha_scale)))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tail_grad)
                p.drawPath(area_path)

                # 密集垂直脉冲能量柱
                if self._show_bars:
                    c_bar = QColor(SUITE_PALETTE["reverb"])
                    c_bar.setAlphaF(0.68 * alpha_scale)
                    p.setPen(QPen(c_bar, 1.35))
                    for pt in pts:
                        p.drawLine(QPointF(pt.x(), plot.bottom()), pt)

                # 发光包络曲线（对标 EQ-UI.py 总曲线发光效果）
                if self._show_envelope:
                    p.setPen(QPen(QColor(40, 216, 240, int(55 * alpha_scale)), 4.2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPath(env_path)

                    c_line = QColor("#8AF2FF")
                    c_line.setAlphaF(0.95 * alpha_scale)
                    p.setPen(QPen(c_line, 1.7))
                    p.drawPath(env_path)

        # 7. 右上角悬浮视图开关胶囊 (ENV / BARS — 对标 SPEC / PEAK)
        self._btn_env_rect = QRectF(plot.right() - 88, plot.top() + 8, 38, 18)
        self._btn_bars_rect = QRectF(plot.right() - 46, plot.top() + 8, 38, 18)
        for rect, label, active in (
            (self._btn_env_rect, "ENV", self._show_envelope),
            (self._btn_bars_rect, "BARS", self._show_bars),
        ):
            p.setPen(QPen(QColor("#3A4252") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 46, 56, 210) if active else QColor(21, 21, 24, 180))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#6E6E76"))
            p.setFont(QFont(MONO_FONT_FAM, 7))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # 8. 左上角实时声学 HUD 读数胶囊 + 悬停十字准星（对标 EQ-UI.py）
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
            hud_str = f"RT60: {rt60 / 1000.0:.2f}s  •  Pre-Delay: {pre_delay:+.0f}ms  •  Dist: {dist_delay_ms:.1f}ms"
            hud_w = 248
            hud_col = QColor("#B4B4BE")

        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(hud_col)
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()


# =====================================================================
# 8. 控件：右侧输出峰值电平表（与 EQ-UI.py MasterOutputStrip 风格一致）
# =====================================================================
class ReverbOutputMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._meter_db = -24.0
        self._clipped = False
        self.setFixedWidth(26)
        self.setToolTip("输出峰值电平表（点击顶部红灯清除削波）")

    def setMeterLevel(self, level_db: float):
        self._meter_db = max(-60.0, min(0.0, level_db))
        if level_db >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 16.0:
            self._clipped = False
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        tr = QRectF(cx - 3.0, 12.0, 6.0, max(40.0, self.height() - 34.0))

        # 削波指示灯
        clip_rect = QRectF(cx - 3.0, 4.0, 6.0, 4.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)

        # 凹槽底框
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor("#0E0E11"))
        p.drawRoundedRect(tr, 2.0, 2.0)

        # 电平填充条
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


# =====================================================================
# 9. 控件：底部参数模块卡片（完全复刻 EQ-UI.py ProBandCard 卡片样式）
# =====================================================================
class StudioModuleCard(QFrame):
    def __init__(self, title: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._active = True

        self.setObjectName("ModuleCard")
        self._apply_style()

        self.card_layout = QVBoxLayout(self)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(0)

        # 顶部模块标题栏
        header = QWidget()
        header.setFixedHeight(30)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 4, 10, 2)
        h_lay.setSpacing(6)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(
            f"color: {accent_color}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

        h_lay.addStretch()
        h_lay.addWidget(self.title_lbl)
        h_lay.addStretch()

        self.card_layout.addWidget(header)

    def setBandActive(self, active: bool):
        self._active = active
        self._apply_style()
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if active else '#6E6E76'}; font-size: 11px; font-weight: 700; letter-spacing: 0.3px;"
        )

    def _apply_style(self):
        top_col = self._accent
        bg_col = "#27272B"
        self.setStyleSheet(f"""
            QFrame#ModuleCard {{
                background-color: {bg_col};
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }}
            QFrame#SubSection {{
                background-color: transparent;
                border-top: 1px solid #141416;
            }}
        """)

    def add_section(self) -> QHBoxLayout:
        sec = QFrame()
        sec.setObjectName("SubSection")
        lay = QHBoxLayout(sec)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)
        self.card_layout.addWidget(sec, 1)
        return lay


# =====================================================================
# 10. 主面板：自主旗舰立体声混响器 (Studio Acoustic Reverb)
# =====================================================================
class StudioAcousticReverbWidget(QWidget):
    # 对接后端音频引擎的完整信号接口
    reverbBypassChanged = pyqtSignal(bool)
    parameterChanged = pyqtSignal(str, float)
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Audio 2", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Acoustic Reverb — {track_name}")
        self.resize(980, 640)
        self.setMinimumSize(880, 580)

        self._active_bank = "A"
        self._banks = {
            "A": copy.deepcopy(DEFAULT_PRESETS["Cathedral"]),
            "B": copy.deepcopy(DEFAULT_PRESETS["Concert Hall"]),
        }
        self._presets = copy.deepcopy(DEFAULT_PRESETS)
        self._controls: dict[str, KnobControlUnit] = {}
        self._cards: list[StudioModuleCard] = []

        self._setup_ui(track_name)
        self._refresh_all_views()

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: 'Helvetica Neue', 'PingFang SC';
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ImpulseResponseGraphWidget, ReverbOutputMeter, AcousticRoomCard {{
                background-color: transparent;
            }}
            QComboBox {{
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 5px;
                font-size: 10px;
                font-weight: 600;
                min-height: 19px;
            }}
            QComboBox:hover {{ border-color: #484852; }}
            QComboBox::drop-down {{ border: none; width: 14px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 3.5px solid transparent;
                border-right: 3.5px solid transparent;
                border-top: 4.5px solid #9A9AA4;
                margin-right: 5px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #222226;
                color: #DCDCE2;
                border: 1px solid #141416;
                selection-background-color: #35353D;
                selection-color: #FFFFFF;
                outline: none;
            }}
            QPushButton#ToolBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton#ToolBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#ToolBtn:pressed {{
                background-color: #1D1D21;
                padding-top: 1px;
            }}
            QToolTip {{
                background-color: #1D1D21;
                color: #DCDCE2;
                border: 1px solid #141416;
                padding: 6px 9px;
                font-size: 11px;
            }}
        """)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部整合工具栏（与 EQ-UI.py 100% 像素级对齐）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setFixedHeight(38)
        top_bar.setObjectName("TopBar")
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True)
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"ACOUSTIC REVERB  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(12)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = QComboBox()
        self.preset_combo.setFixedWidth(185)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(21, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(21, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(10)

        # A / B 对比槽
        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(24, 22)
        self.btn_copy_ab.setToolTip("复制当前参数到另一槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)
        # 箭头按钮样式由 _update_ab_styles 统一设置（勿在入布局前绑定 objectName QSS，
        # 否则 Qt6 基线对齐分组会把它排到行底，相对 A/B 下沉 2px）

        tb_lay.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(48, 22)
        reset_btn.clicked.connect(self.reset_reverb)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部主图形区：3D 房间模型 + 脉冲响应图谱 + 右侧输出电平表
        # -------------------------------------------------------------
        upper_row = QHBoxLayout()
        upper_row.setContentsMargins(0, 0, 0, 0)
        upper_row.setSpacing(0)

        self.room_card = AcousticRoomCard()
        self.room_card.dimensionsChanged.connect(self._on_room_changed)
        upper_row.addWidget(self.room_card, 36)

        room_sep = QFrame()
        room_sep.setFixedWidth(1)
        room_sep.setStyleSheet("background-color: #141416; border: none;")
        upper_row.addWidget(room_sep)

        self.ir_graph = ImpulseResponseGraphWidget()
        upper_row.addWidget(self.ir_graph, 64)

        out_sep = QFrame()
        out_sep.setFixedWidth(1)
        out_sep.setStyleSheet("background-color: #141416; border: none;")
        upper_row.addWidget(out_sep)

        out_col = QVBoxLayout()
        out_col.setSpacing(2)
        out_lbl = QLabel("OUT")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;")
        out_col.addWidget(out_lbl)

        self.output_meter = ReverbOutputMeter()
        out_col.addWidget(self.output_meter, 1)
        meter_col = QFrame()
        meter_col.setStyleSheet("background-color: #222226; border-left: 1px solid #141416;")
        meter_col_lay = QVBoxLayout(meter_col)
        meter_col_lay.setContentsMargins(4, 6, 4, 6)
        meter_col_lay.addLayout(out_col)
        upper_row.addWidget(meter_col)

        main_lay.addLayout(upper_row, 52)

        # -------------------------------------------------------------
        # 3. 底部 4 个独立参数模块卡片
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        # Card 1: Reverb 核心参数（冰晶青 #28D8F0）
        card_rev = StudioModuleCard("Reverb", SUITE_PALETTE["reverb"])
        rev_top = card_rev.add_section()
        rev_top.addWidget(self._add_ctrl(
            "pre_delay", "Pre-Delay", -200, 200, 42, "ms",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], mode="bipolar", origin_value=0.0,
            knob_diameter=52, layout_style="compact"
        ))
        rev_top.addWidget(self._add_ctrl(
            "reverb_time", "Reverb Time", 100, 4000, 2730, "ms",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], mode="log", k_format=True,
            knob_diameter=52, layout_style="compact"
        ))

        rev_bot = card_rev.add_section()
        rev_bot.addWidget(self._add_ctrl(
            "distance", "Distance", 0, 30, 19.87, "m",
            decimals=2, accent_color=SUITE_PALETTE["reverb"], layout_style="compact"
        ))
        rev_bot.addWidget(self._add_ctrl(
            "brightness", "Brightness", 6, 100, 50.0, "%",
            decimals=2, accent_color=SUITE_PALETTE["reverb"], layout_style="compact"
        ))
        rev_bot.addWidget(self._add_ctrl(
            "modulation", "Modulation", 0, 100, 2.0, "%",
            decimals=2, accent_color=SUITE_PALETTE["reverb"], layout_style="compact"
        ))

        # Card 2: Early Reflection Tone（琥珀金 #F0A848）
        card_er = StudioModuleCard("Early Reflection Tone", SUITE_PALETTE["er"])
        er_top = card_er.add_section()
        er_top.addWidget(self._add_ctrl(
            "er_lo_freq", "Lo Freq", 150, 500, 300, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["er"], mode="log",
            knob_diameter=48, layout_style="compact"
        ))
        er_top.addWidget(self._add_ctrl(
            "er_hi_freq", "High Freq", 1000, 16000, 10000, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["er"], mode="log", k_format=True,
            knob_diameter=48, layout_style="compact"
        ))

        er_bot = card_er.add_section()
        er_bot.addWidget(self._add_ctrl(
            "er_lo_gain", "Lo Gain", -18, 6, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["er"], mode="bipolar", origin_value=0.0,
            knob_diameter=48, layout_style="compact"
        ))
        er_bot.addWidget(self._add_ctrl(
            "er_hi_gain", "Hi Gain", -18, 6, -6.1, "dB",
            decimals=1, accent_color=SUITE_PALETTE["er"], mode="bipolar", origin_value=0.0,
            knob_diameter=48, layout_style="compact"
        ))

        # Card 3: Reverb Tone（翡翠绿 #4CD490）
        card_tone = StudioModuleCard("Reverb Tone", SUITE_PALETTE["tone"])
        tone_top = card_tone.add_section()
        tone_top.addWidget(self._add_ctrl(
            "rev_lo_freq", "Lo Freq", 150, 500, 291, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["tone"], mode="log",
            knob_diameter=48, layout_style="compact"
        ))
        tone_top.addWidget(self._add_ctrl(
            "rev_hi_freq", "Hi Freq", 1000, 16000, 11100, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["tone"], mode="log", k_format=True,
            knob_diameter=48, layout_style="compact"
        ))

        tone_bot = card_tone.add_section()
        tone_bot.addWidget(self._add_ctrl(
            "rev_lo_gain", "Lo Gain", -18, 6, 1.1, "dB",
            decimals=1, accent_color=SUITE_PALETTE["tone"], mode="bipolar", origin_value=0.0,
            knob_diameter=48, layout_style="compact"
        ))
        tone_bot.addWidget(self._add_ctrl(
            "rev_hi_gain", "Hi Gain", -18, 6, -2.2, "dB",
            decimals=1, accent_color=SUITE_PALETTE["tone"], mode="bipolar", origin_value=0.0,
            knob_diameter=48, layout_style="compact"
        ))

        # Card 4: Output（钴蓝 #6495FA）
        card_out = StudioModuleCard("Output", SUITE_PALETTE["output"])
        out_top = card_out.add_section()
        out_top.addStretch()
        out_top.addWidget(self._add_ctrl(
            "dry_wet", "Dry/Wet", 0, 100, 100.0, "%",
            left_hint="Dry", right_hint="Wet", decimals=2,
            accent_color=SUITE_PALETTE["output"],
            knob_diameter=52, layout_style="compact"
        ))
        out_top.addStretch()

        out_bot = card_out.add_section()
        out_bot.addWidget(self._add_ctrl(
            "direct_db", "Direct", -60, 6, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, layout_style="compact"
        ))
        out_bot.addWidget(self._add_ctrl(
            "er_db", "ER", -60, 6, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, layout_style="compact"
        ))
        out_bot.addWidget(self._add_ctrl(
            "reverb_db", "Reverb", -60, 6, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, layout_style="compact"
        ))

        self._cards = [card_rev, card_er, card_tone, card_out]
        cards_row.addWidget(card_rev, 28)
        cards_row.addWidget(card_er, 23)
        cards_row.addWidget(card_tone, 23)
        cards_row.addWidget(card_out, 26)

        main_lay.addLayout(cards_row, 48)

    def _add_ctrl(self, key: str, title: str, min_v: float, max_v: float, def_v: float, unit: str, **kwargs) -> KnobControlUnit:
        ctrl = KnobControlUnit(key, title, min_v, max_v, def_v, unit, **kwargs)
        ctrl.valueChanged.connect(self._on_param_changed)
        self._controls[key] = ctrl
        return ctrl

    # -----------------------------------------------------------------
    # 状态管理与联动逻辑（与 EQ-UI.py 结构一致）
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_ab_styles(self):
        active_css = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_css = """
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_css if self._active_bank == "A" else inactive_css)
        self.btn_b.setStyleSheet(active_css if self._active_bank == "B" else inactive_css)
        self.btn_copy_ab.setStyleSheet(inactive_css)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

    def _refresh_all_views(self):
        st = self._current_state()
        en = bool(st.get("enabled", True))
        self._update_ab_styles()

        self.power_switch.blockSignals(True)
        self.power_switch.setChecked(en)
        self.power_switch.blockSignals(False)

        self.room_card.setDimensions(st["height"], st["length"], st["width"], emit_signal=False)
        self.room_card.setDistance(st["distance"])
        self.room_card.setBandActive(en)

        for key, ctrl in self._controls.items():
            if key in st:
                ctrl.setValue(st[key], emit=False)
            ctrl.setBandActive(en)

        for card in self._cards:
            card.setBandActive(en)

        self.ir_graph.setState(st, enabled=en)

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _on_power_toggled(self, enabled: bool):
        st = self._current_state()
        st["enabled"] = enabled
        self._refresh_all_views()
        self.reverbBypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_param_changed(self, key: str, val: float):
        st = self._current_state()
        st[key] = val
        if key == "distance":
            self.room_card.setDistance(val)
        self.ir_graph.setState(st, enabled=st["enabled"])
        self.parameterChanged.emit(key, val)
        self.stateChanged.emit(self.get_state())

    def _on_room_changed(self, h: float, l: float, w: float):
        st = self._current_state()
        st["height"] = round(h, 2)
        st["length"] = round(l, 2)
        st["width"] = round(w, 2)
        self.ir_graph.setState(st, enabled=st["enabled"])
        self.parameterChanged.emit("height", st["height"])
        self.parameterChanged.emit("length", st["length"])
        self.parameterChanged.emit("width", st["width"])
        self.stateChanged.emit(self.get_state())

    def _load_preset(self, name: str):
        if name in self._presets:
            self._banks[self._active_bank] = copy.deepcopy(self._presets[name])
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存混响预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    # -----------------------------------------------------------------
    # 公开 API：与 EQ-UI.py 方法签名一致，便于宿主统一调度
    # -----------------------------------------------------------------
    def reset_reverb(self):
        self._banks[self._active_bank] = copy.deepcopy(DEFAULT_PRESETS["Cathedral"])
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText("Cathedral")
        self.preset_combo.blockSignals(False)
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        merged = copy.deepcopy(self._current_state())
        merged.update(state)
        self._banks[self._active_bank] = merged
        self._refresh_all_views()

    def update_output_meter(self, level_db: float):
        """接入真实音频引擎输出峰值电平 (-60.0 ~ 0.0 dBFS)"""
        self.output_meter.setMeterLevel(level_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioAcousticReverbWidget("Audio 2")
    win.show()
    sys.exit(app.exec())